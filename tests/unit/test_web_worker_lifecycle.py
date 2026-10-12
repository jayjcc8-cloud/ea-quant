"""Process lifecycle around the research worker: stop proof, parent loss, shutdown.

Every test here drives real subprocesses. Two of them run a throwaway controller
process and kill it, because "the controller died" cannot be simulated inside the
process that is the controller.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

import ea.web.service as service_module
from ea.strategy.package import read_regular
from ea.web import worker
from ea.web.service import ReportUnavailableError, WebService
from unit.test_web_api import _scenario_root, _settings

#: The bound a stop is promised within: TERM grace, then KILL grace, applied once.
STOP_BOUND = worker.STOP_GRACE_SECONDS + worker.KILL_GRACE_SECONDS
#: Waiting longer than the promised bound plus this slack fails the test.
SETTLE_SLACK = 15.0

#: A child that inherits the worker's process group, records the TERM it refuses
#: and then keeps running until it is killed outright. It announces itself only
#: once its handler is installed, so "the descendant exists" also means "the
#: descendant will really ignore TERM".
_STUBBORN = """
import os
import signal
import sys
import time
from pathlib import Path

marker = Path(sys.argv[1])
ready = Path(sys.argv[2])


def on_term(signum, frame):
    marker.write_text("term-ignored", encoding="ascii")


signal.signal(signal.SIGTERM, on_term)
ready.write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(0.05)
"""

#: A worker-side hook: start one stubborn descendant, name both processes, then
#: keep "computing" so the attempt is live when the test stops it.
_COMPUTING_HOOK = """
import os
import subprocess
import sys
import time
from pathlib import Path

STUB = {stub!r}


def hook(*, scenario, control, request, worker):
    (control / "leader.pid").write_text(str(os.getpid()), encoding="ascii")
    stub = control / "stubborn.py"
    stub.write_text(STUB, encoding="ascii")
    subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(stub),
            str(control / "term.marker"),
            str(control / "descendant.ready"),
        ]
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (control / "descendant.ready").is_file():
            break
        time.sleep(0.01)
    while True:
        time.sleep(0.05)
"""

#: A worker-side hook that only blocks: the attempt is live, nothing else.
_BLOCKING_HOOK = """
import time


def hook(*, scenario, control, request, worker):
    (control / "computing.marker").write_text("computing", encoding="ascii")
    while True:
        time.sleep(0.05)
"""

_CRASH_CONTROLLER = """
import os
import sys
import time
from pathlib import Path

from ea.web.service import WebService

scenario_root, workspace, hook = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
service = WebService(scenario_root, workspace)
service._test_hook = hook
if os.environ.get("EA_TEST_CRASH_AT_SPAWN") == "1":
    real_spawn = service._spawn_worker

    def spawn_then_die(control):
        process = real_spawn(control)
        # The controller dies inside the spawn call, with a live worker it has
        # not registered anywhere and no chance to close its liveness pipe.
        print(process.process.pid, flush=True)
        os._exit(9)

    service._spawn_worker = spawn_then_die
    service.start()
    identity = service.validate_scenario("bounded-long.yaml")["input_identity"]
    service.create_job(
        scenario_id="bounded-long.yaml",
        input_identity=identity,
        request_id="request-crash-controller-0001",
    )
    # Only the worker thread can reach the spawn, so park this one until it does.
    while True:
        time.sleep(0.1)

service.start()
identity = service.validate_scenario("bounded-long.yaml")["input_identity"]
record, _created = service.create_job(
    scenario_id="bounded-long.yaml",
    input_identity=identity,
    request_id="request-crash-controller-0001",
)
control = service.worker_dir / record.job_id
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    if (control / "leader.pid").is_file() and (control / "descendant.ready").is_file():
        break
    time.sleep(0.02)
print(record.job_id, flush=True)
os._exit(9)
"""


def _write_hook(tmp_path: Path, name: str, source: str) -> str:
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return f"{path}:hook"


def _service(tmp_path: Path, hook: str | None = None) -> WebService:
    settings = _settings(tmp_path)
    service = WebService(settings.scenario_root, settings.workspace)
    service._test_hook = hook
    service.start()
    return service


def _submit(service: WebService, request_id: str) -> Any:
    identity = service.validate_scenario("bounded-long.yaml")["input_identity"]
    record, _created = service.create_job(
        scenario_id="bounded-long.yaml",
        input_identity=cast(dict[str, object], identity),
        request_id=request_id,
    )
    return record


def _wait_file(path: Path, bound: float = 30.0) -> None:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pids: list[int], bound: float = STOP_BOUND + 10.0) -> list[int]:
    """Wait until every pid is gone, returning any that never were."""
    deadline = time.monotonic() + bound
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _alive(pid)]
        if remaining:
            time.sleep(0.05)
    return remaining


def _reap(pids: list[int]) -> None:
    """Best-effort cleanup so a failing test never leaves an owned process."""
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _terminal(service: WebService, job_id: str, bound: float = STOP_BOUND + SETTLE_SLACK) -> Any:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job.status not in {"accepted", "running", "stopping"}:
            return job
        time.sleep(0.02)
    raise AssertionError("the job did not settle within the promised bound")


def test_cancellation_confirms_the_whole_group_not_just_the_leader(tmp_path: Path) -> None:
    """A TERM-ignoring descendant keeps the attempt unstopped until KILL lands.

    The worker leader exits on the TERM the controller sends, while the child it
    started refuses TERM and keeps running. Reaping only the leader would report
    ``stopped`` while compute is still alive, so the controller must escalate and
    publish ``stopped`` only once the owned group is confirmed gone.
    """
    hook = _write_hook(tmp_path, "stubborn", _COMPUTING_HOOK.format(stub=_STUBBORN))
    service = _service(tmp_path, hook)
    pids: list[int] = []
    try:
        record = _submit(service, "request-group-stop-0001")
        control = service.worker_dir / record.job_id
        _wait_file(control / "descendant.ready")
        pids = [
            int((control / "leader.pid").read_text(encoding="ascii")),
            int((control / "descendant.ready").read_text(encoding="ascii")),
        ]
        assert all(_alive(pid) for pid in pids)

        started = time.monotonic()
        assert service.stop_job(record.job_id).status == "stopping"
        final = _terminal(service, record.job_id)
        elapsed = time.monotonic() - started

        assert final.status == "stopped"
        assert final.report_sha256 is None and final.engine_run_id is None
        # Nothing derived from a stopped attempt is served.
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / record.job_id).exists()
        # The descendant provably refused the polite signal, so only the KILL
        # escalation can have stopped it.
        assert (control / "term.marker").is_file()
        # And the confirmation was honest: no owned process is left behind.
        assert _wait_gone(pids) == []
        assert elapsed <= STOP_BOUND + 5.0, f"the stop took {elapsed:.1f}s"
    finally:
        _reap(pids)
        service.stop()


def test_a_descendant_outliving_the_leader_is_killed_before_the_result_is_read(
    tmp_path: Path,
) -> None:
    """A leader that exits is not a stopped group; the leftover is killed first."""
    hook = _write_hook(
        tmp_path,
        "lingering",
        f"""
import subprocess
import sys
import time
from pathlib import Path

STUB = {_STUBBORN!r}


def hook(*, scenario, control, request, worker):
    stub = control / "stubborn.py"
    stub.write_text(STUB, encoding="ascii")
    subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(stub),
            str(control / "term.marker"),
            str(control / "descendant.ready"),
        ]
    )
    # Return with the descendant still running: the attempt itself completes and
    # the leader exits while its group is very much alive.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (control / "descendant.ready").is_file():
            break
        time.sleep(0.01)
""",
    )
    service = _service(tmp_path, hook)
    pids: list[int] = []
    try:
        record = _submit(service, "request-lingering-group-0001")
        control = service.worker_dir / record.job_id
        _wait_file(control / "descendant.ready")
        pids = [int((control / "descendant.ready").read_text(encoding="ascii"))]
        final = _terminal(service, record.job_id)
        # The attempt's own engine result is untouched by the stray child; the
        # child itself may not survive it.
        assert final.status == "succeeded"
        assert pids and _wait_gone(pids) == []
        assert service.report(record.job_id)
    finally:
        _reap(pids)
        service.stop()


def test_an_unconfirmed_group_never_publishes_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the owned group cannot be shown gone, nothing of the attempt is published."""
    monkeypatch.setattr(worker.WorkerProcess, "group_alive", lambda self: True)
    service = _service(tmp_path)
    try:
        record = _submit(service, "request-unconfirmed-group-0001")
        final = _terminal(service, record.job_id)
        assert final.status == "failed"
        assert final.error_code == "worker_group_unconfirmed"
        assert final.report_sha256 is None
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / record.job_id).exists()
    finally:
        service.stop()


def test_no_verified_group_is_an_unconfirmed_group_and_never_a_confirmed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown group identity is never evidence that compute stopped.

    ``group_alive`` cannot answer for a group that was never captured, so treating
    that ignorance as "gone" would publish a confirmed stop for a worker that is
    still running. A live worker is held with an unverified group: the attempt
    stays ``stopping`` with its own code instead of being confirmed stopped, and
    the controller still ends the compute it owns through the liveness channel.
    """
    hook = _write_hook(tmp_path, "blocking", _BLOCKING_HOOK)
    service = _service(tmp_path, hook)
    spawned: list[worker.WorkerProcess] = []
    real_spawn = service._spawn_worker

    def without_identity(control: Path) -> Any:
        process = replace(real_spawn(control), process_group=None)
        spawned.append(process)
        return process

    monkeypatch.setattr(service, "_spawn_worker", without_identity)
    try:
        record = _submit(service, "request-unconfirmed-group-0002")
        _wait_file(service.worker_dir / record.job_id / "computing.marker")
        assert service.stop_job(record.job_id).status == "stopping"
        _wait_supervisor_idle(service)
        final = service.get_job(record.job_id)
        assert final.status == "stopping", final.status
        assert final.error_code == "worker_stop_unconfirmed"
        assert final.report_sha256 is None and final.engine_run_id is None
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / record.job_id).exists()
        # Closing the liveness channel still ends the compute this controller owns.
        assert spawned
        deadline = time.monotonic() + 15
        while spawned[0].process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert spawned[0].process.poll() is not None, "owned compute outlived its controller"
    finally:
        service.stop()


def _wait_supervisor_idle(service: WebService, bound: float = STOP_BOUND + 20.0) -> None:
    """Wait until the supervising thread has released the active-job slot.

    The worker handle is dropped before the commit gate runs, so the released
    active slot — not an empty handle table — is the barrier that is ordered
    after the final write.
    """
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        with service._lock:
            if service._active is None:
                return
        time.sleep(0.02)
    raise AssertionError("the supervisor never released its active slot")


def test_a_store_failure_whose_reread_also_fails_freezes_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unprovable job write is never overwritten and never shown as running.

    The write replaces the durable record with a success and then fails on the
    directory barrier, and the read-back that would prove it fails too. Nothing
    here may write that job again — not a stop, not the finalizer, not shutdown —
    and the reader is told the state is uncertain instead of being shown a run
    that appears to still be going.
    """
    service = _service(tmp_path)
    real_write = service._write
    real_read = read_regular
    failed = {"once": False}
    stopped = False
    try:
        record = _submit(service, "request-store-failure-0001")
        job_path = service._job_path(record.job_id)

        def flaky_write(written: Any) -> None:
            real_write(written)
            if written.status == "succeeded" and not failed["once"]:
                failed["once"] = True
                raise OSError("directory barrier failed after the record was replaced")

        def unreadable(path: Path, root: Path, **kwargs: Any) -> bytes:
            if path == job_path:
                raise OSError("the durable record cannot be read back")
            return real_read(path, root, **kwargs)

        monkeypatch.setattr(service, "_write", flaky_write)
        monkeypatch.setattr(service_module, "read_regular", unreadable)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not failed["once"]:
            time.sleep(0.02)
        assert failed["once"], "the injected write barrier failure never happened"
        _wait_supervisor_idle(service)
        # The success may already be committed on disk; nothing here may rewrite it.
        assert json.loads(job_path.read_bytes())["status"] == "succeeded"

        frozen = service.get_job(record.job_id)
        assert frozen.status == "interrupted"
        assert frozen.error_code == "job_state_uncertain"
        assert service.stop_job(record.job_id) == frozen
        assert json.loads(job_path.read_bytes())["status"] == "succeeded"
        # An unproven record is never served as a verified report.
        with pytest.raises(ReportUnavailableError):
            service.report(record.job_id)

        service.stop()
        stopped = True
        assert json.loads(job_path.read_bytes())["status"] == "succeeded"
    finally:
        if not stopped:
            service.stop()


def _run_crash_controller(
    tmp_path: Path, hook: str, *, at_spawn: bool
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if at_spawn:
        environment["EA_TEST_CRASH_AT_SPAWN"] = "1"
    else:
        environment.pop("EA_TEST_CRASH_AT_SPAWN", None)
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _CRASH_CONTROLLER,
            str(_scenario_root(tmp_path)),
            str(tmp_path / "workspace"),
            hook,
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=180,
        check=False,
    )


def test_controller_crash_inside_the_spawn_window_leaves_no_compute(tmp_path: Path) -> None:
    """A worker spawned by a controller that dies immediately still stops itself."""
    hook = _write_hook(tmp_path, "blocking", _BLOCKING_HOOK)
    completed = _run_crash_controller(tmp_path, hook, at_spawn=True)
    assert completed.returncode == 9, completed.stderr
    leader = int(completed.stdout.strip().splitlines()[-1])
    workspace = tmp_path / "workspace"
    try:
        assert _wait_gone([leader]) == [], "an orphaned worker outlived its controller"
        assert list((workspace / "runs").iterdir()) == []
        assert list((workspace / "reports").iterdir()) == []
    finally:
        _reap([leader])


def test_controller_crash_while_computing_takes_its_descendants_with_it(tmp_path: Path) -> None:
    """A crashed controller leaves neither the worker nor anything the worker started."""
    hook = _write_hook(tmp_path, "computing", _COMPUTING_HOOK.format(stub=_STUBBORN))
    completed = _run_crash_controller(tmp_path, hook, at_spawn=False)
    assert completed.returncode == 9, completed.stderr
    job_id = completed.stdout.strip().splitlines()[-1]
    workspace = tmp_path / "workspace"
    control = workspace / ".worker" / job_id
    pids = [
        int((control / "leader.pid").read_text(encoding="ascii")),
        int((control / "descendant.ready").read_text(encoding="ascii")),
    ]
    try:
        assert _wait_gone(pids) == [], "orphaned compute survived its controller"
        # The reserved identity and staging survive as evidence; nothing of them
        # is an official result, and no persisted pid was signalled on restart.
        assert control.is_dir()
        assert list((workspace / "runs").iterdir()) == []
        assert list((workspace / "reports").iterdir()) == []
    finally:
        _reap(pids)


def test_shutdown_stops_every_admitted_job_including_a_queued_member(tmp_path: Path) -> None:
    """Shutdown covers accepted jobs, not only the ones holding a live handle."""
    hook = _write_hook(tmp_path, "blocking", _BLOCKING_HOOK)
    service = _service(tmp_path, hook)
    stopped = False
    try:
        identity = service.validate_scenario("bounded-long.yaml")["input_identity"]
        accepted = service.create_batch(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], identity),
            initial_cash="10000",
            runs=[
                {"target_quantity": "1", "entry_delay_bars": 0},
                {"target_quantity": "2", "entry_delay_bars": 0},
            ],
        )
        members = cast(list[dict[str, Any]], accepted["members"])
        deadline = time.monotonic() + 30
        started_controls: list[Path] = []
        while time.monotonic() < deadline:
            started_controls = [
                path
                for path in service.worker_dir.iterdir()
                if (path / "computing.marker").is_file()
            ]
            if started_controls:
                break
            time.sleep(0.02)
        assert started_controls, "the first member never started computing"
        # The second member is only accepted: it holds no live handle at all.
        assert len(list(service.worker_dir.iterdir())) == 1
        assert service.get_job(members[1]["job_id"]).status == "accepted"
        with service._lock:
            assert service._runs, "no live worker handle was registered"

        started = time.monotonic()
        service.stop()
        stopped = True
        elapsed = time.monotonic() - started
        assert elapsed <= STOP_BOUND + 5.0, f"shutdown took {elapsed:.1f}s"

        for member in members:
            job = service.get_job(member["job_id"])
            assert job.status in {"stopped", "failed", "interrupted"}, job.status
            assert job.report_sha256 is None
        # The queued member never spawned compute of its own.
        assert len(list(service.worker_dir.iterdir())) == 1
    finally:
        if not stopped:
            service.stop()


def test_promotion_without_registration_stays_unavailable_after_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artifacts promoted by a controller that died before registering stay unserved."""
    settings = _settings(tmp_path)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    promoted = Event()
    release = Event()
    try:
        real_store = service._store_adopting

        def pause_before_registration(record: Any) -> None:
            if record.status == "succeeded":
                promoted.set()
                assert release.wait(60), "the test never released the commit"
            return real_store(record)

        monkeypatch.setattr(service, "_store_adopting", pause_before_registration)
        record = _submit(service, "request-promotion-crash-0001")
        assert promoted.wait(60), "the attempt never reached its promotion"
        # The artifacts are published while the durable record is still running.
        # The commit thread holds the service lock, so read the served roots
        # straight from disk rather than asking the service.
        assert (settings.workspace / "reports" / record.job_id / "report.json").is_file()
        assert (settings.workspace / "runs" / cast(str, record.attempt_id)).is_dir()

        snapshot = tmp_path / "crashed-workspace"
        shutil.copytree(settings.workspace, snapshot)
        release.set()
        assert _terminal(service, record.job_id).status == "succeeded"

        recovered = WebService(settings.scenario_root, snapshot)
        recovered.start()
        try:
            interrupted = recovered.get_job(record.job_id)
            assert interrupted.status == "interrupted"
            assert interrupted.attempt_id == record.attempt_id
            assert interrupted.report_sha256 is None
            with pytest.raises(ReportUnavailableError):
                recovered.report(record.job_id)
            # No redispatch: the same one job is all the workspace has.
            assert len(recovered.list_jobs()) == 1
        finally:
            recovered.stop()
    finally:
        release.set()
        service.stop()


def test_a_store_failure_after_replacement_never_rewrites_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfirmed job write adopts the durable record instead of a stale one."""
    service = _service(tmp_path)
    real_write = service._write
    failed = {"once": False}
    try:
        record = _submit(service, "request-store-barrier-0001")

        def flaky_write(record_to_write: Any) -> None:
            real_write(record_to_write)
            if record_to_write.status == "succeeded" and not failed["once"]:
                failed["once"] = True
                raise OSError("directory barrier failed after the record was replaced")

        monkeypatch.setattr(service, "_write", flaky_write)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not failed["once"]:
            time.sleep(0.02)
        assert failed["once"], "the injected write barrier failure never happened"
        # The success is durably committed; in memory it must be the same record,
        # so a later cancellation cannot overwrite it with a stale running state.
        assert service.get_job(record.job_id).status == "succeeded"
        assert service.stop_job(record.job_id).status == "succeeded"
        assert service.get_job(record.job_id).status == "succeeded"
        assert service.report(record.job_id)
    finally:
        service.stop()
