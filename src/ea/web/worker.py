"""One research attempt computed by an independent worker process.

The Python control layer (`ea.web.service.WebService`) stays the sole authority
for task state, dispatch, cancellation and publication. A worker computes one
attempt and nothing else: it reads the frozen input files the request names,
writes the attempt and its formal report artifacts into its own staging root, and
writes one result document. It never publishes an official attempt, report or job
record, owns no durable state, and holds no identity that outlives it.

Protocol — versioned canonical JSON, references only, never bulk data:

    ``<control>/request.json``  ``ea.research-worker.request.v1``
    ``<control>/events.jsonl``  ``ea.research-worker.event.v1``   (bounded)
    ``<control>/result.json``   ``ea.research-worker.result.v1``

The control directory is the rendezvous; the request names the frozen scenario
and strategy artifact by absolute path plus SHA-256, and the staged attempt and
report by paths relative to that directory. Market data never crosses the
boundary as a payload: the worker reads the same frozen files the engine reads.

Recovery rules:

* The controller reserves the attempt identity and writes it to the durable job
  record *before* spawning, so a crash inside the spawn window is
  indistinguishable from one before it — both recover as interrupted and neither
  is re-run or re-dispatched.
* Ownership is an inherited fact, not an observation. The controller creates a
  liveness pipe before the spawn and only the read end is inherited, so a worker
  whose controller died at any point — including inside the spawn window, before
  the worker ran its first instruction — sees EOF and takes its own process group
  down with it. The controller signals only the group it captured from the live
  spawn handle, never a process id read from disk.
* A stop is confirmed against the whole owned process group, not the leader: a
  worker that exits while a child it started is still alive has not stopped, and
  the controller escalates TERM to KILL once, within one explicit bound.
* Staging is never served. Publication is a single controller-side promotion of
  verified staging paths; a promotion interrupted before the controller records
  the job as succeeded stays unavailable and is never read as an official report.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from ea.core import RunId
from ea.core.encoding import canonical_json_bytes
from ea.product import (
    BacktestRunFailure,
    BacktestScenarioError,
    LoadedBacktestScenario,
    generate_backtest_report,
    run_backtest_scenario,
)
from ea.product.equity_path import generate_equity_path_analysis
from ea.product.reporting import _fsync_directory, _write_report_file
from ea.product.scenario import load_backtest_scenario_bytes
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import read_regular, validate_package

REQUEST_SCHEMA = "ea.research-worker.request.v1"
EVENT_SCHEMA = "ea.research-worker.event.v1"
RESULT_SCHEMA = "ea.research-worker.result.v1"

#: A worker writes at most this many progress events. Past the cap it keeps
#: counting and stops writing, so a long run cannot grow the file without bound.
MAX_EVENTS = 64
#: The longest single event detail a worker may write.
MAX_EVENT_DETAIL = 512

#: Grace given to SIGTERM before the whole process group is killed.
STOP_GRACE_SECONDS = 5.0
#: Grace given to SIGKILL before the controller stops waiting for the group.
#: The bound a stop is promised within is exactly these two waits, applied once.
KILL_GRACE_SECONDS = 5.0
#: How often the supervising controller re-reads its own cancellation state.
SUPERVISE_SECONDS = 0.05
#: How often the worker reaps or re-probes a process tree while it stops.
REAP_SECONDS = 0.02

EXIT_COMPUTED = 0
EXIT_NO_RESULT = 2
EXIT_PARENT_LOST = 70

REQUEST_NAME = "request.json"
EVENTS_NAME = "events.jsonl"
RESULT_NAME = "result.json"
RUNS_NAME = "runs"
REPORTS_NAME = "reports"

ARTIFACTS = ("report.json", "summary.txt", "equity-path.json")

#: Failure codes the worker classifies itself. Anything else is an engine
#: outcome code carried through unchanged from ``BacktestRunFailure``.
CLASSIFIED_FAILURES = frozenset({"input_changed", "report_failed", "web.internal_failure"})


class WorkerError(ValueError):
    """A worker request or result document is unreadable or conflicts."""


def _canonical(document: object) -> bytes:
    return canonical_json_bytes(document) + b"\n"


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


# --- protocol documents -----------------------------------------------------


def build_request(
    *,
    job_id: str,
    attempt_id: str,
    scenario_path: Path,
    scenario_sha256: str,
    scenario_identity_sha256: str,
    research_input_bytes: bytes | None,
    package_path: Path | None,
    package_sha256: str | None,
    data_capture: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    """One frozen-input request. Paths and hashes only; no payloads.

    ``scenario_sha256`` is the digest of the frozen file's bytes and
    ``scenario_identity_sha256`` is the canonical identity of the *effective*
    scenario those bytes hold — the admitted scenario after the user's
    parameters were applied. A worker checks both: the first that it read the
    frozen bytes, the second that what it parsed is the scenario this job
    admitted rather than merely something that hashed correctly.

    ``data_capture`` names the controller's private snapshot of this attempt's
    confirmed source. A worker that is given one computes from that file, held to
    its own digest, instead of from the path the frozen scenario declares.
    """
    scenario: dict[str, Any] = {
        "path": str(scenario_path),
        "sha256": scenario_sha256,
        "identity_sha256": scenario_identity_sha256,
    }
    if research_input_bytes is not None:
        scenario["research_input"] = json.loads(research_input_bytes)
        scenario["research_input_sha256"] = _digest(research_input_bytes)
    package = None
    if package_path is not None and package_sha256 is not None:
        package = {"path": str(package_path), "sha256": package_sha256}
    request: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "scenario": scenario,
        "package": package,
        "staging": {"runs": RUNS_NAME, "reports": REPORTS_NAME},
    }
    if data_capture is not None:
        # Only a controller that captured this attempt's own source names one;
        # the key is absent rather than empty when there is nothing to point at.
        request["data_capture"] = {
            "path": str(data_capture[0]),
            "sha256": data_capture[1],
        }
    return request


def write_request(control: Path, request: dict[str, Any]) -> None:
    """Publish the request durably before any process is spawned."""
    _write_once(control / REQUEST_NAME, _canonical(request))


def _write_once(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def read_request(control: Path) -> dict[str, Any]:
    document = _read_document(control / REQUEST_NAME)
    if document.get("schema") != REQUEST_SCHEMA:
        raise WorkerError("worker request version is unsupported")
    return document


def read_result(control: Path) -> dict[str, Any] | None:
    """The worker's own result document, or ``None`` when it never wrote one."""
    try:
        document = _read_document(control / RESULT_NAME)
    except WorkerError:
        return None
    if document.get("schema") != RESULT_SCHEMA:
        return None
    return document


def read_events(control: Path) -> list[dict[str, Any]]:
    """At most ``MAX_EVENTS`` bounded progress events, oldest first."""
    try:
        payload = (control / EVENTS_NAME).read_bytes()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in payload.splitlines()[:MAX_EVENTS]:
        try:
            document = json.loads(line)
        except ValueError:
            break
        if type(document) is dict and document.get("schema") == EVENT_SCHEMA:
            events.append(document)
    return events


def last_phase(control: Path) -> str | None:
    """The last bounded progress phase a worker recorded, if it recorded one."""
    events = read_events(control)
    if not events:
        return None
    phase = events[-1].get("phase")
    return phase if type(phase) is str and 0 < len(phase) <= 64 else None


def staged_path(control: Path, raw: object) -> Path:
    """Resolve one staged artifact reference, or refuse it.

    A worker describes what it staged with a path relative to the control
    directory. Anything absolute, escaping, or naming a symlink is not a staged
    output of this attempt and is refused rather than promoted.
    """
    if type(raw) is not str or not raw:
        raise WorkerError("staged artifact reference is missing")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkerError("staged artifact reference escapes the control directory")
    resolved = (control / candidate).resolve(strict=True)
    if not resolved.is_relative_to(control) or resolved.is_symlink():
        raise WorkerError("staged artifact reference escapes the control directory")
    return resolved


def _read_document(path: Path) -> dict[str, Any]:
    try:
        payload = read_regular(path, path.parent)
        document = json.loads(payload)
    except (OSError, ValueError):
        raise WorkerError("worker document is unreadable") from None
    if type(document) is not dict:
        raise WorkerError("worker document is not one mapping")
    return document


# --- controller side -------------------------------------------------------


@dataclass(slots=True)
class WorkerProcess:
    """The live handle of one spawned worker, owned by the running controller.

    The handle owns two things the controller must never guess at: the process
    *group* the worker leads, captured from the live ``Popen`` at spawn, and the
    write end of the liveness pipe whose read end the worker inherited. Nothing
    here ever signals a process id recovered from disk, and a group whose
    identity could not be verified is not signalled at all.
    """

    process: subprocess.Popen[bytes]
    #: The verified process group this worker leads, or ``None`` when that
    #: identity is unavailable. Only a group id captured here is ever signalled.
    process_group: int | None
    #: Controller-side write end of the worker's liveness pipe. Held for exactly
    #: as long as this controller owns the worker; closing it (or dying) tells
    #: the worker its owner is gone.
    liveness: int | None = None

    def leader_reaped(self) -> bool:
        """Whether the worker process itself has been reaped."""
        return self.process.poll() is not None

    def group_alive(self) -> bool:
        """Whether any process of the owned group still exists.

        ``killpg(group, 0)`` proves group membership without sending a signal:
        it fails with ``ProcessLookupError`` only when the group is empty. A
        reaped leader therefore does not make this false while a descendant it
        started is still alive.
        """
        if self.process_group is None:
            return False
        try:
            os.killpg(self.process_group, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # The group exists but is not ours to signal: it is still there.
            return True
        return True

    def stopped(self) -> bool:
        """Whether the worker and everything it started are confirmed gone.

        A group whose identity was never captured is never a confirmed stop.
        ``group_alive`` cannot answer for an unknown group, and treating that
        ignorance as "gone" would publish a confirmed stop for compute nobody
        proved had ended; the attempt is left unconfirmed instead.
        """
        if self.process_group is None:
            return False
        return self.leader_reaped() and not self.group_alive()

    def request_stop(self) -> None:
        """Ask the whole process group to stop. Idempotent."""
        _signal_group(self.process_group, signal.SIGTERM)

    def kill(self) -> None:
        """Kill the whole process group. Idempotent."""
        _signal_group(self.process_group, signal.SIGKILL)

    def wait(self, timeout: float) -> bool:
        """Whether the worker process was reaped within ``timeout`` seconds."""
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def returncode(self) -> int | None:
        return self.process.returncode

    def close(self) -> None:
        """Release the liveness channel and reap the leader if it already exited."""
        self.wait(0)
        if self.liveness is not None:
            with contextlib.suppress(OSError):
                os.close(self.liveness)
            self.liveness = None


def _signal_group(process_group: int | None, signal_number: int) -> None:
    """Signal one verified group, or nothing at all.

    There is deliberately no single-process fallback: a group id that was not
    captured from a live spawn is not evidence about any process, and signalling
    it could reach a process this controller does not own.
    """
    if process_group is None or process_group <= 1:
        return
    if process_group == os.getpgrp():
        return
    with contextlib.suppress(OSError):
        os.killpg(process_group, signal_number)


def spawn(
    control: Path,
    *,
    test_hook: str | None = None,
    executable: str | None = None,
) -> WorkerProcess:
    """Start one isolated worker for the request already written to ``control``.

    The child gets its own session, so it leads the process group the controller
    cancels as a unit, and it runs ``-I -B``: no user site, no environment
    influence and no bytecode writes, so its economics stay the frozen ones.
    An ordinary child inherits that group and is stopped with it; a descendant
    that deliberately leaves the group is outside what this owns. An ordinary
    child process is not an untrusted-code security sandbox, and this module does
    not present one: it is a lifecycle boundary for trusted local compute.

    The liveness pipe is created here, before the spawn, and only the read end is
    inherited: from the child's first instruction its owner's existence is an
    inherited fact rather than a later observation, so a controller that dies
    inside the spawn window still leaves no unowned compute. Its write end stays
    in this process alone; every other descriptor is closed in the child.

    ``test_hook`` is a test-only seam: the product never passes it. It names one
    ``path/to/hook.py:callable`` the worker runs immediately before the engine
    reads market data, which is how a test reproduces a change landing in exactly
    that window.
    """
    read_fd, write_fd = os.pipe()
    try:
        command = [
            executable or sys.executable,
            "-I",
            "-B",
            "-m",
            "ea.web.worker",
            str(control),
            "--parent-fd",
            str(read_fd),
        ]
        if test_hook is not None:
            command += ["--test-hook", test_hook]
        process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(read_fd,),
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(read_fd)
    return WorkerProcess(process, _owned_group(process.pid), write_fd)


def _owned_group(pid: int) -> int | None:
    """The group id of a just-spawned session leader, or ``None`` if unproven."""
    try:
        group = os.getpgid(pid)
    except OSError:
        # The child already exited: its group died with it, so there is nothing
        # to cancel and nothing to confirm.
        return None
    return group if group == pid else None


# --- worker side -----------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    control_argument: str | None = None
    hook: str | None = None
    parent_fd: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--test-hook", "--parent-fd"} and index + 1 < len(arguments):
            if argument == "--test-hook":
                hook = arguments[index + 1]
            else:
                parent_fd = arguments[index + 1]
            index += 2
            continue
        if control_argument is not None:
            return EXIT_NO_RESULT
        control_argument = argument
        index += 1
    if control_argument is None:
        return EXIT_NO_RESULT
    # First, before any other work: make this process's existence conditional on
    # the controller that spawned it. The channel was created before the spawn,
    # so even a controller that dies inside the spawn window leaves no orphan.
    _watch_controller(_parent_channel(parent_fd))
    try:
        control = _control_directory(control_argument)
    except WorkerError:
        return EXIT_NO_RESULT
    try:
        request = read_request(control)
    except WorkerError:
        return EXIT_NO_RESULT
    writer = _EventWriter(control)
    try:
        result = _compute(control, request, writer, hook)
    except Exception:  # noqa: BLE001 — a worker never publishes a partial success
        writer.emit("unavailable")
        result = {
            "schema": RESULT_SCHEMA,
            "job_id": request.get("job_id"),
            "attempt_id": request.get("attempt_id"),
            "status": "failed",
            "code": "web.internal_failure",
            "message": None,
            "engine_run_id": None,
            "attempt_staged": False,
        }
    try:
        _write_once(control / RESULT_NAME, _canonical(result))
    except OSError:
        return EXIT_NO_RESULT
    return EXIT_COMPUTED


def _control_directory(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise WorkerError("worker control directory must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise WorkerError("worker control directory is unavailable") from None
    if resolved != path or not resolved.is_dir():
        raise WorkerError("worker control directory identity is invalid")
    return resolved


def _parent_channel(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        descriptor = int(raw)
    except ValueError:
        return None
    return descriptor if descriptor > 2 else None


def _watch_controller(descriptor: int | None) -> None:
    """Exit when the controller holding the other end of this pipe is gone.

    The controller never writes to the pipe, so a blocking read returns only when
    every copy of the write end is closed — which happens when the controller
    exits, crashes or is killed. This is an inherited fact about the owner, not a
    later guess about it: the channel exists before this process does, so it also
    covers a controller that died before this code ran.
    """
    if descriptor is None:
        return

    def watch() -> None:
        try:
            while os.read(descriptor, 1):
                pass
        except OSError:
            pass
        _abandon_owned_group()

    threading.Thread(target=watch, name="ea-worker-liveness", daemon=True).start()


def _abandon_owned_group() -> None:
    """Take everything this worker started with it, then exit.

    Compute whose owner is gone must not keep running: the whole group is killed
    so a child this worker spawned does not outlive the controller either. The
    group is signalled only when this process really leads it, so the kill can
    never reach a group this worker does not own.
    """
    try:
        group = os.getpgrp()
        if group == os.getpid() and group > 1:
            os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
    os._exit(EXIT_PARENT_LOST)


class _EventWriter:
    """Bounded progress: at most ``MAX_EVENTS`` small lines, then silence."""

    def __init__(self, control: Path) -> None:
        self._path = control / EVENTS_NAME
        self._written = 0
        self._lock = threading.Lock()

    def emit(self, phase: str, detail: str | None = None) -> None:
        with self._lock:
            if self._written >= MAX_EVENTS:
                return
            self._written += 1
            document: dict[str, Any] = {
                "schema": EVENT_SCHEMA,
                "seq": self._written,
                "phase": phase[:64],
            }
            if detail is not None:
                document["detail"] = detail[:MAX_EVENT_DETAIL]
            try:
                with self._path.open("ab") as handle:
                    handle.write(_canonical(document))
                    handle.flush()
            except OSError:
                self._written = MAX_EVENTS


def _staging(control: Path, name: str) -> Path:
    path = control / name
    path.mkdir(mode=0o700, exist_ok=True)
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_dir():
        raise WorkerError("worker staging root identity is invalid")
    return resolved


def _compute(
    control: Path,
    request: dict[str, Any],
    writer: _EventWriter,
    hook: str | None,
) -> dict[str, Any]:
    """Compute one attempt in staging and describe it; never publish it."""
    job_id = request.get("job_id")
    attempt_id = request.get("attempt_id")
    if type(job_id) is not str or type(attempt_id) is not str:
        raise WorkerError("worker request names no attempt")
    runs = _staging(control, RUNS_NAME)
    reports = _staging(control, REPORTS_NAME)
    writer.emit("reading_input")
    try:
        scenario = _load_frozen_scenario(request)
    except (WorkerError, BacktestScenarioError, OSError, ValueError):
        # The frozen input this request names could not be re-read or re-verified:
        # that is an input change at the engine entry, not a business outcome.
        return {
            "schema": RESULT_SCHEMA,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "code": "input_changed",
            "message": None,
            "engine_run_id": None,
            "attempt_staged": False,
        }
    if hook is not None:
        _run_hook(hook, scenario, control, request)
    writer.emit("engine")
    try:
        computed = run_backtest_scenario(scenario, runs, run_id=RunId(attempt_id))
    except BacktestRunFailure as failure:
        return {
            "schema": RESULT_SCHEMA,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "code": failure.code.value,
            "message": str(failure),
            "engine_run_id": failure.output_directory.name,
            "attempt_staged": failure.output_directory.name == attempt_id,
        }
    except BacktestScenarioError:
        return {
            "schema": RESULT_SCHEMA,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "code": "input_changed",
            "message": None,
            "engine_run_id": None,
            "attempt_staged": False,
        }
    attempt = computed.output_directory
    if attempt.name != attempt_id:
        return {
            "schema": RESULT_SCHEMA,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "code": "web.internal_failure",
            "message": None,
            "engine_run_id": attempt.name,
            "attempt_staged": False,
        }
    writer.emit("report")
    report_dir = reports / job_id
    try:
        generated = generate_backtest_report(attempt, report_dir)
        analysis = generate_equity_path_analysis(attempt, generated.report)
        _write_report_file(report_dir / "equity-path.json", analysis.canonical_bytes)
        _fsync_directory(report_dir)
        artifacts = {
            name: _digest(read_regular(report_dir / name, report_dir)) for name in ARTIFACTS
        }
    except Exception:  # noqa: BLE001 — the engine's evidence is kept, no report is
        return {
            "schema": RESULT_SCHEMA,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "code": "report_failed",
            "message": None,
            "engine_run_id": attempt_id,
            "attempt_staged": True,
        }
    writer.emit("finished")
    return {
        "schema": RESULT_SCHEMA,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "status": "succeeded",
        "code": None,
        "message": None,
        "engine_run_id": attempt_id,
        "attempt_staged": True,
        "attempt_relative": f"{RUNS_NAME}/{attempt_id}",
        "report_relative": f"{REPORTS_NAME}/{job_id}",
        "artifacts": artifacts,
    }


def _run_hook(
    reference: str, scenario: LoadedBacktestScenario, control: Path, request: dict[str, Any]
) -> None:
    """Test-only seam: run one ``path/to/hook.py:callable`` before the engine.

    The callable receives the frozen scenario, the control directory, the
    request and this module, so a test can reproduce a change landing in exactly
    this window or replace one worker-side step inside the child process. The
    product never passes a hook.
    """
    path_text, _, attribute = reference.partition(":")
    path = Path(path_text)
    if not path.is_absolute() or not attribute:
        raise WorkerError("test hook reference is invalid")
    spec = importlib.util.spec_from_file_location("ea_worker_test_hook", path)
    if spec is None or spec.loader is None:
        raise WorkerError("test hook module is unreadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook = cast(Callable[..., Any], getattr(module, attribute))
    hook(
        scenario=scenario,
        control=control,
        request=request,
        worker=sys.modules[__name__],
    )


def _load_frozen_scenario(request: dict[str, Any]) -> LoadedBacktestScenario:
    """Reload the controller's frozen input from the bytes it was verified as.

    The verified payload is what is parsed, through the same strict parser every
    other load uses: the file is never opened a second time, so a replacement
    landing between the check and the parse cannot become the run's input. The
    reconstructed scenario is then held to the identity this job admitted, so
    whatever the parse produced must be the scenario the user's parameters were
    applied to.
    """
    scenario_reference = request.get("scenario")
    if type(scenario_reference) is not dict:
        raise WorkerError("worker request names no scenario")
    path = _absolute(scenario_reference.get("path"))
    expected = scenario_reference.get("sha256")
    if type(expected) is not str:
        raise WorkerError("worker request names no scenario digest")
    admitted = scenario_reference.get("identity_sha256")
    if type(admitted) is not str:
        raise WorkerError("worker request names no admitted scenario identity")
    payload = read_regular(path, path.parent)
    if _digest(payload) != expected:
        raise WorkerError("frozen scenario changed before the worker read it")
    package_reference = request.get("package")
    package = None
    if type(package_reference) is dict:
        package_path = _absolute(package_reference.get("path"))
        package_sha256 = package_reference.get("sha256")
        if type(package_sha256) is not str:
            raise WorkerError("worker request names no strategy artifact digest")
        package = validate_package(
            read_regular(package_path, package_path.parent), expected_sha256=package_sha256
        )
    catalog = ResearchStrategyCatalogV1(() if package is None else (package,))
    # The controller's private snapshot of this run's confirmed source replaces
    # the scenario file's own declared data path. It was captured by the input
    # check and is held here to its own digest, so the engine computes from the
    # bytes this attempt was confirmed on and not from whatever the original
    # source path holds now.
    payload = _with_data_capture(payload, request.get("data_capture"))
    scenario = load_backtest_scenario_bytes(payload, scenario_path=path, catalog=catalog)
    if scenario.scenario_sha256.value != admitted:
        raise WorkerError("the parsed scenario is not the one this job admitted")
    evidence = scenario_reference.get("research_input")
    if evidence is not None:
        expected_evidence = scenario_reference.get("research_input_sha256")
        if type(expected_evidence) is not str:
            raise WorkerError("worker request names no research input digest")
        encoded = canonical_json_bytes(evidence)
        if _digest(encoded) != expected_evidence:
            raise WorkerError("frozen research input evidence conflicts")
        # The evidence this worker was handed must be evidence *about the source
        # it just read*. Its own digest only proves the document is intact, so
        # the loaded dataset's real source digest, selected fingerprint, record
        # count and replay window are what the evidence is held to — a source
        # whose selected fingerprint is unchanged but whose bytes are not the
        # confirmed ones is refused before the engine is reached.
        if type(evidence) is not dict:
            raise WorkerError("frozen research input evidence is not one mapping")
        dataset = scenario.dataset
        window = dataset.replay_window
        if (
            evidence.get("source_sha256") != dataset.source_bytes_sha256.value
            or evidence.get("data_sha256") != dataset.selection.fingerprint.sha256.value
            or evidence.get("record_count") != dataset.selection.fingerprint.record_count
            or evidence.get("replay_start_utc")
            != window.start_inclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            or evidence.get("replay_end_utc")
            != window.end_exclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ):
            raise WorkerError("the research input evidence is not this job's source")
        scenario = replace(scenario, research_input_bytes=encoded)
    return scenario


def _with_data_capture(payload: bytes, reference: Any) -> bytes:
    """The frozen scenario document, reading its data from the private snapshot.

    The snapshot is the controller's own capture of the confirmed source. It is
    held to the digest the controller recorded for it before anything is parsed,
    and only its path is substituted: every other field of the frozen document —
    including the data fingerprint the strict loader re-checks against the parsed
    selection — is exactly what was admitted, so a snapshot that does not parse
    to the confirmed selection is refused rather than run.
    """
    if reference is None:
        return payload
    if type(reference) is not dict:
        raise WorkerError("worker request names an unreadable private snapshot")
    capture_path = _absolute(reference.get("path"))
    expected = reference.get("sha256")
    if type(expected) is not str:
        raise WorkerError("worker request names no private snapshot digest")
    captured = read_regular(capture_path, capture_path.parent)
    if _digest(captured) != expected:
        raise WorkerError("the private snapshot changed before the worker read it")
    try:
        document = json.loads(payload)
    except ValueError:
        raise WorkerError("frozen scenario is not readable") from None
    data = document.get("data") if type(document) is dict else None
    if type(data) is not dict:
        raise WorkerError("frozen scenario names no data")
    data["path"] = str(capture_path)
    return canonical_json_bytes(document)


def _absolute(raw: object) -> Path:
    if type(raw) is not str:
        raise WorkerError("worker request path is missing")
    path = Path(raw)
    if not path.is_absolute():
        raise WorkerError("worker request path must be absolute")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
