"""What the worker is told to run, and what it refuses to run.

The request is bound to the identity the job was *admitted* with, not to a fresh
read of the workspace at dispatch time. These tests compare the worker's own
economics against the direct engine path on the same frozen input, and prove that
content drift is refused both before the request is written and after it.
"""

from __future__ import annotations

import json
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
from ea.web.service import WebService
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
