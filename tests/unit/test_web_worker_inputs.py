"""What the worker is told to run, and what it refuses to run.

The request is bound to the identity the job was *admitted* with, not to a fresh
read of the workspace at dispatch time. These tests compare the worker's own
economics against the direct engine path on the same frozen input, and prove that
content drift is refused both before the request is written and after it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
import yaml

from ea.core import RunId
from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
from ea.product.equity_path import generate_equity_path_analysis
from ea.web import worker
from ea.web.service import ReportUnavailableError, WebService
from unit.test_backtest_report import _priced_scenario
from unit.test_bounded_round_trips_v1 import bounded_scenario


def _zero_fee(root: Path) -> Path:
    """One mature fixture: the priced bounded-long scenario, no commission."""
    return _priced_scenario(root)


def _commission_round_trip(root: Path) -> Path:
    """One mature fixture: three round trips under a real commission policy."""
    return bounded_scenario(root, bars=12, limit=3)


def _service(tmp_path: Path, builder: Callable[[Path], Path]) -> tuple[WebService, str]:
    root = tmp_path / "scenarios"
    root.mkdir()
    source = builder(root)
    name = "fixture.yaml"
    source.rename(root / name)
    service = WebService(root, tmp_path / "workspace")
    service.start()
    return service, name


def _submit(service: WebService, scenario_id: str, request_id: str) -> Any:
    identity = service.validate_scenario(scenario_id)["input_identity"]
    record, _created = service.create_job(
        scenario_id=scenario_id,
        input_identity=cast(dict[str, object], identity),
        request_id=request_id,
    )
    return record


def _terminal(service: WebService, job_id: str, bound: float = 60.0) -> Any:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job.status not in {"accepted", "running", "stopping"}:
            return job
        time.sleep(0.02)
    raise AssertionError("the job did not settle")


def _direct_engine_artifacts(tmp_path: Path, frozen: Path, attempt_id: str) -> tuple[bytes, bytes]:
    """The legacy path: load, run the engine, publish the formal report, analyse."""
    scenario = load_backtest_scenario(frozen)
    attempt = run_backtest_scenario(
        scenario, tmp_path / "direct-runs", run_id=RunId(attempt_id)
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "direct-report")
    analysis = generate_equity_path_analysis(attempt, report.report)
    return report.report.canonical_bytes, analysis.canonical_bytes


def _spawn_through(wrapper_source: str) -> Callable[[WebService, Path], None]:
    """Install a launcher that runs the real worker with one in-process patch.

    The worker is a real process, so a race inside it can only be injected from
    its own side. This is the same shape the independent acceptance probe used:
    a thin script that patches the worker module and then runs its ``main``.
    """

    def install(service: WebService, tmp_path: Path) -> None:
        wrapper = tmp_path / "worker-wrapper.py"
        wrapper.write_text(wrapper_source, encoding="utf-8")

        def launch(control: Path) -> Any:
            read_fd, write_fd = os.pipe()
            try:
                process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(wrapper),
                        str(control),
                        "--parent-fd",
                        str(read_fd),
                    ],
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(read_fd,),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except BaseException:
                os.close(read_fd)
                os.close(write_fd)
                raise
            os.close(read_fd)
            return worker.WorkerProcess(process, worker._owned_group(process.pid), write_fd)

        service._spawn_worker = launch  # type: ignore[method-assign]

    return install


@pytest.mark.parametrize(
    "builder", [_zero_fee, _commission_round_trip], ids=["zero-fee", "commission-round-trip"]
)
def test_worker_economics_equal_the_direct_engine_path(
    tmp_path: Path, builder: Callable[[Path], Path]
) -> None:
    """The worker's official artifacts are the same bytes the engine path publishes.

    Both sides run on the frozen input file the job admitted and the same attempt
    identity, so only the execution location differs. Exact decimals, the full
    report document and the equity-path evidence are compared byte for byte.
    """
    service, scenario_id = _service(tmp_path, builder)
    try:
        record = _submit(service, scenario_id, "request-worker-inputs-0001")
        job = _terminal(service, record.job_id)
        assert job.status == "succeeded", job.message
        assert job.engine_run_id == record.attempt_id

        frozen = service.inputs_dir / f"{record.job_id}.json"
        expected_report, expected_path = _direct_engine_artifacts(
            tmp_path, frozen, cast(str, record.attempt_id)
        )
        report_dir = service.reports_dir / record.job_id
        worker_report = (report_dir / "report.json").read_bytes()
        worker_path = (report_dir / "equity-path.json").read_bytes()

        assert worker_report == expected_report
        assert worker_path == expected_path
        assert sha256(worker_report).hexdigest() == job.report_sha256
        assert sha256(worker_path).hexdigest() == job.equity_path_sha256
        # The served report is the file the reader downloads, unchanged.
        assert service.report(record.job_id) == expected_report
    finally:
        service.stop()


def test_the_request_binds_the_admitted_scenario_identity(tmp_path: Path) -> None:
    """The expected digest is the admitted document's, and it is checkable."""
    service, scenario_id = _service(tmp_path, _zero_fee)
    captured: dict[str, Any] = {}
    real_spawn = service._spawn_worker

    def capture(control: Path) -> Any:
        captured["request"] = json.loads((control / "request.json").read_bytes())
        return real_spawn(control)

    service._spawn_worker = capture
    try:
        record = _submit(service, scenario_id, "request-worker-inputs-0002")
        assert _terminal(service, record.job_id).status == "succeeded"

        frozen = service.inputs_dir / f"{record.job_id}.json"
        request = cast(dict[str, Any], captured["request"])
        assert request["schema"] == "ea.research-worker.request.v1"
        assert request["attempt_id"] == record.attempt_id
        assert request["scenario"]["path"] == str(frozen)
        assert request["scenario"]["sha256"] == sha256(frozen.read_bytes()).hexdigest()
        # The frozen file still reproduces the canonical identity the job was
        # admitted with, so the bytes and the admitted scenario cannot disagree.
        reloaded = load_backtest_scenario(frozen)
        identity = cast(dict[str, Any], record.input_identity)
        assert reloaded.scenario_sha256.value == identity["scenario_sha256"]
        assert request["scenario"]["identity_sha256"] == identity["scenario_sha256"]
    finally:
        service.stop()


def test_a_chosen_cash_override_is_the_admitted_identity_not_source_drift(tmp_path: Path) -> None:
    """A parameter the user chose is the admitted scenario, not a changed source.

    ``create_job`` holds the registered source identity separately from the
    scenario the user's parameters produce, so the worker must be held to the
    effective scenario this job admitted. Comparing it to the source identity
    would refuse every legitimate override instead of the drift it is meant to
    catch.
    """
    service, scenario_id = _service(tmp_path, _zero_fee)
    captured: dict[str, Any] = {}
    real_spawn = service._spawn_worker

    def capture(control: Path) -> Any:
        captured["request"] = json.loads((control / "request.json").read_bytes())
        return real_spawn(control)

    service._spawn_worker = capture
    try:
        source_identity = cast(
            dict[str, Any], service.validate_scenario(scenario_id)["input_identity"]
        )
        record, _created = service.create_job(
            scenario_id=scenario_id,
            input_identity=source_identity,
            parameters={"initial_cash": "25000", "quantity": "2"},
            request_id="request-worker-inputs-0007",
        )
        job = _terminal(service, record.job_id)
        assert job.status == "succeeded", job.message
        report = json.loads(service.report(record.job_id))
        assert report["economics"]["initial_funding"]["amount"] == "25000"

        # The request names the effective scenario, which is deliberately not the
        # registered source identity the override was derived from.
        frozen = service.inputs_dir / f"{record.job_id}.json"
        effective = load_backtest_scenario(frozen)
        request = cast(dict[str, Any], captured["request"])
        assert request["scenario"]["identity_sha256"] == effective.scenario_sha256.value
        assert request["scenario"]["identity_sha256"] != source_identity["scenario_sha256"]
    finally:
        service.stop()


def test_valid_content_drift_before_the_request_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen file edited into other valid content may not become its own expectation."""
    service, scenario_id = _service(tmp_path, _zero_fee)
    launches: list[Path] = []
    real_spawn = service._spawn_worker

    def counted(control: Path) -> Any:
        launches.append(control)
        return real_spawn(control)

    monkeypatch.setattr(service, "_spawn_worker", counted)
    original_run = service._run
    ready, release = Event(), Event()

    def gated(*args: Any, **kwargs: Any) -> None:
        ready.set()
        assert release.wait(30), "the test never released the supervisor"
        return original_run(*args, **kwargs)

    monkeypatch.setattr(service, "_run", gated)
    try:
        record = _submit(service, scenario_id, "request-worker-inputs-0003")
        assert ready.wait(30)
        frozen = service.inputs_dir / f"{record.job_id}.json"
        document = yaml.safe_load(frozen.read_bytes())
        document["funding"]["initial_cash"] = "50000"
        frozen.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        release.set()

        job = _terminal(service, record.job_id)
        assert job.status == "failed"
        assert job.error_code == "input_changed"
        assert job.report_sha256 is None
        assert launches == [], "a drifted frozen input must not start compute"
        assert list(service.runs_dir.iterdir()) == []
        assert list(service.reports_dir.iterdir()) == []
    finally:
        release.set()
        service.stop()


def test_valid_content_drift_after_the_request_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen file edited after the request is written is refused inside the worker."""
    service, scenario_id = _service(tmp_path, _zero_fee)
    real_spawn = service._spawn_worker
    tampered: dict[str, str] = {}

    def tamper_then_spawn(control: Path) -> Any:
        request = json.loads((control / "request.json").read_bytes())
        frozen = Path(request["scenario"]["path"])
        document = yaml.safe_load(frozen.read_bytes())
        document["funding"]["initial_cash"] = "50000"
        frozen.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        tampered["digest"] = sha256(frozen.read_bytes()).hexdigest()
        return real_spawn(control)

    monkeypatch.setattr(service, "_spawn_worker", tamper_then_spawn)
    try:
        record = _submit(service, scenario_id, "request-worker-inputs-0004")
        job = _terminal(service, record.job_id)
        assert job.status == "failed"
        assert job.error_code == "input_changed"
        assert job.report_sha256 is None
        assert tampered, "the tampering seam never ran"
        # The worker refused before the engine produced any attempt evidence.
        assert list(service.runs_dir.iterdir()) == []
        assert list(service.reports_dir.iterdir()) == []
    finally:
        service.stop()


#: The independent acceptance race: the frozen file is rewritten in the window
#: between the worker's own verified read and whatever it parses. A worker that
#: re-opens the file to parse it runs the replaced economics.
_REREAD_WRAPPER = """
import json
import sys

from ea.web import worker

real_read = worker.read_regular
drifted = {"done": False}


def racing_read(path, root, **kwargs):
    payload = real_read(path, root, **kwargs)
    if not drifted["done"] and path.name != "request.json" and path.name.endswith(".json"):
        drifted["done"] = True
        document = json.loads(path.read_bytes())
        document["funding"]["initial_cash"] = "50000"
        path.write_text(json.dumps(document))
    return payload


worker.read_regular = racing_read
raise SystemExit(worker.main(sys.argv[1:]))
"""


def test_the_worker_parses_the_bytes_it_verified_not_the_file_again(tmp_path: Path) -> None:
    """A replacement in the verified-read window cannot become the run's input.

    The frozen file is rewritten with other valid economic content the instant the
    worker's verified read returns it. The engine must run the bytes that were
    verified — the funding this job admitted — so the recorded attempt carries the
    admitted economics and never the replacement's. Publication then fails closed,
    because the product's own source-drift validator binds the attempt to the
    frozen file it was read from and that file no longer holds what it did: a run
    whose source changed is not published as an official report.
    """
    service, scenario_id = _service(tmp_path, _zero_fee)
    install = _spawn_through(_REREAD_WRAPPER)
    install(service, tmp_path)
    try:
        record = _submit(service, scenario_id, "request-worker-inputs-0005")
        admitted = cast(dict[str, Any], record.input_identity)
        job = _terminal(service, record.job_id)

        attempt = service.runs_dir / cast(str, record.attempt_id)
        manifest = json.loads((attempt / "manifest.json").read_bytes())
        result = json.loads((attempt / "result.json").read_bytes())
        # The engine consumed the verified 10000 scenario, not the 50000 replacement.
        assert manifest["funding"]["amount"] == "10000"
        assert manifest["scenario"]["canonical"]["funding"]["initial_cash"] == "10000"
        assert result["initial_funding"]["amount"] == "10000"
        assert result["ending_cash"][0]["amount"] == "9797"
        # …and change those economics are never published as an official report.
        assert job.status == "failed"
        assert job.error_code == "report_failed"
        assert job.report_sha256 is None
        assert not (service.reports_dir / record.job_id).exists()
        with pytest.raises(ReportUnavailableError):
            service.report(record.job_id)

        # The file really was replaced, so the refusals above are about this race.
        frozen = service.inputs_dir / f"{record.job_id}.json"
        replacement = json.loads(frozen.read_bytes())
        assert replacement["funding"]["initial_cash"] == "50000"
        assert load_backtest_scenario(frozen).scenario_sha256.value != admitted["scenario_sha256"]
    finally:
        service.stop()


#: The same window, but the content the parser returns is not the admitted one:
#: whatever produced it, the worker must refuse it instead of running it.
_SUBSTITUTED_PARSE_WRAPPER = """
import json
import sys

from ea.web import worker

real_parse = worker.load_backtest_scenario_bytes


def substituted(payload, **kwargs):
    document = json.loads(payload)
    document["funding"]["initial_cash"] = "50000"
    return real_parse(json.dumps(document).encode("utf-8"), **kwargs)


worker.load_backtest_scenario_bytes = substituted
raise SystemExit(worker.main(sys.argv[1:]))
"""


def test_a_parsed_scenario_that_is_not_the_admitted_one_is_refused(tmp_path: Path) -> None:
    """Consuming verified bytes is not enough: they must reconstruct the admitted scenario."""
    service, scenario_id = _service(tmp_path, _zero_fee)
    install = _spawn_through(_SUBSTITUTED_PARSE_WRAPPER)
    install(service, tmp_path)
    try:
        record = _submit(service, scenario_id, "request-worker-inputs-0006")
        job = _terminal(service, record.job_id)
        assert job.status == "failed"
        assert job.error_code == "input_changed"
        assert job.report_sha256 is None
        assert list(service.runs_dir.iterdir()) == []
        assert list(service.reports_dir.iterdir()) == []
    finally:
        service.stop()
