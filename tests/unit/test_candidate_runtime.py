from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.product import resume_backtest_attempt
from ea.product.candidate import (
    inspect_candidate_binding,
    load_accepted_candidate,
    run_accepted_candidate,
)
from ea.web.service import WebService
from unit.test_local_bounded_round_trips_v1 import local_v3


@pytest.fixture(scope="module")
def candidate_evidence(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("candidate-runtime")
    path, packages, _ = local_v3(root)
    document = yaml.safe_load(path.read_text())
    data_path = path.parent / document["data"]["path"]
    payload = data_path.read_bytes()
    old = document["data"]["start_utc"][:10]
    new = (datetime.fromisoformat(old) + timedelta(days=2)).strftime("%Y-%m-%d")
    payload = payload.replace(old.encode(), new.encode())
    (path.parent / "later.csv").write_bytes(payload)
    for name in ("start_utc", "end_utc"):
        document["data"][name] = document["data"][name].replace(old, new)
    document["data"]["path"] = "later.csv"
    selection = decode_phase1_ohlcv_csv(
        payload,
        replay_window=ReplayWindow(
            datetime.fromisoformat(document["data"]["start_utc"]),
            datetime.fromisoformat(document["data"]["end_utc"]),
        ),
    ).selection
    document["data"]["fingerprint"] = {
        "sha256": selection.fingerprint.sha256.value,
        "record_count": selection.fingerprint.record_count,
    }
    (path.parent / "later.yaml").write_text(yaml.safe_dump(document))
    service = WebService(path.parent, root / "workspace", strategy_root=packages)
    service.start()

    def wait(job_id: str) -> None:
        deadline = time.monotonic() + 20
        while service.get_job(job_id).status in {"accepted", "running"}:
            if time.monotonic() > deadline:
                raise AssertionError("candidate evidence run did not finish")
            time.sleep(0.01)
        assert service.get_job(job_id).status == "succeeded"

    try:
        summary: Any = service.validate_scenario(path.name)
        source, _ = service.create_job(
            scenario_id=path.name,
            input_identity=summary["input_identity"],
            request_id="candidate-source",
        )
        wait(source.job_id)
        relation = service.create_holdout(source_job_id=source.job_id, scenario_id="later.yaml")
        wait(str(relation["holdout_job_id"]))
        record = service.create_candidate(str(relation["validation_id"]))
        service.decide_candidate(record["candidate_id"], "ACCEPTED", "Retain exact evidence")
    finally:
        service.stop()
    (root / "selection.json").write_text(
        json.dumps({"candidate_id": record["candidate_id"], "scenario": path.name})
    )
    return root


@pytest.fixture
def selected(candidate_evidence: Path, tmp_path: Path) -> tuple[Path, str, Path]:
    copy = tmp_path / "inputs"
    shutil.copytree(candidate_evidence, copy)
    selection = json.loads((copy / "selection.json").read_text())
    return copy / "workspace", selection["candidate_id"], copy / "scenarios" / selection["scenario"]


def no_module(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import ea.strategy.package as package

    calls: list[str] = []

    def fail(source: bytes, digest: str) -> dict[str, Any]:
        calls.append(digest)
        raise AssertionError("unaccepted Python must not execute")

    monkeypatch.setattr(package, "_module", fail)
    return calls


def test_inspection_is_read_only_and_does_not_execute_frozen_python(
    selected: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, candidate_id, _ = selected
    before = {str(path): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    calls = no_module(monkeypatch)
    binding = inspect_candidate_binding(workspace, candidate_id)
    assert binding.candidate_id == candidate_id
    assert binding.document() == inspect_candidate_binding(workspace, candidate_id).document()
    assert calls == []
    assert before == {
        str(path): path.read_bytes() for path in workspace.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("state", ["EVALUATED", "REJECTED"])
def test_unaccepted_candidate_and_arbitrary_python_never_execute(
    selected: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    workspace, candidate_id, _ = selected
    from ea.web.candidates import canonical

    path = workspace / "candidates" / f"{candidate_id}.json"
    record = json.loads(path.read_bytes())
    record["status"] = state
    if state == "EVALUATED":
        record["decision"] = None
    else:
        record["decision"]["outcome"] = state
    path.write_bytes(canonical(record))
    calls = no_module(monkeypatch)
    with pytest.raises(ValueError):
        load_accepted_candidate(workspace, candidate_id)
    arbitrary = workspace / "arbitrary.py"
    arbitrary.write_text("raise AssertionError('must not execute')")
    with pytest.raises(ValueError):
        load_accepted_candidate(workspace, str(arbitrary))
    assert calls == []


@pytest.mark.parametrize("mismatch", ["artifact", "configuration", "parameters", "artifact_bytes"])
def test_identity_mismatch_rejects_before_python_execution(
    selected: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    workspace, candidate_id, _ = selected
    calls = no_module(monkeypatch)
    kwargs: dict[str, Any] = {}
    if mismatch == "artifact_bytes":
        path = next((workspace / "inputs").glob("*.eastrategy"))
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        kwargs[f"expected_{mismatch}_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        load_accepted_candidate(workspace, candidate_id, **kwargs)
    assert calls == []


@pytest.mark.parametrize("field", ["parameters", "artifact", "risk", "execution"])
def test_scenario_override_is_rejected_before_package_execution(
    selected: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch, field: str, tmp_path: Path
) -> None:
    workspace, candidate_id, scenario = selected
    binding = inspect_candidate_binding(workspace, candidate_id)
    document = yaml.safe_load(scenario.read_text())
    if field == "parameters":
        document["strategy"]["parameters"]["quantity"] = "1"
    elif field == "artifact":
        document["strategy"]["source"]["artifact_sha256"] = "0" * 64
    elif field == "risk":
        document["risk"]["max_order_quantity"] = "99"
    else:
        document["execution"]["latency"] = {"policy": "deterministic-latency-v1", "latency_ms": 0}
    scenario.write_text(yaml.safe_dump(document))
    calls = no_module(monkeypatch)
    with pytest.raises(ValueError):
        run_accepted_candidate(binding, scenario, tmp_path / "runs")
    assert calls == []
    assert not (tmp_path / "runs").exists()


def test_deterministic_loading_and_existing_engine_keep_candidate_identity(
    selected: tuple[Path, str, Path], tmp_path: Path
) -> None:
    workspace, candidate_id, scenario = selected
    a = load_accepted_candidate(workspace, candidate_id)
    b = load_accepted_candidate(workspace, candidate_id)
    assert a.document() == b.document()
    assert a.strategy_package is not None
    assert b.strategy_package is not None
    assert a.strategy_package.artifact_bytes == b.strategy_package.artifact_bytes
    attempt = run_accepted_candidate(a, scenario, tmp_path / "runs").output_directory
    binding = json.loads((attempt / "candidate-binding.json").read_bytes())
    assert binding["candidate_id"] == candidate_id
    assert binding["fingerprint"] == a.fingerprint
    assert binding["artifact_sha256"] == a.artifact_sha256
    assert binding["configuration_sha256"] == a.configuration_sha256
    assert binding["run_id"] == attempt.name
    records = [
        json.loads(line) for line in (attempt / "operational.jsonl").read_text().splitlines()
    ]
    assert records and {record["candidate_id"] for record in records} == {candidate_id}
    before = {path.name: path.read_bytes() for path in attempt.iterdir() if path.is_file()}
    resume_backtest_attempt(attempt)
    assert before == {path.name: path.read_bytes() for path in attempt.iterdir() if path.is_file()}
    lines: list[str] = []
    resume_backtest_attempt(attempt, operational_sink=lines.append)
    assert lines and {json.loads(line)["candidate_id"] for line in lines} == {candidate_id}
    assert before == {path.name: path.read_bytes() for path in attempt.iterdir() if path.is_file()}


def test_changed_ea_implementation_rejects_before_executing_strategy(
    selected: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.core.run import Sha256Digest
    from ea.product import candidate

    workspace, candidate_id, _ = selected
    calls = no_module(monkeypatch)
    monkeypatch.setattr(candidate, "_package_code_digest", lambda: Sha256Digest("0" * 64))
    assert inspect_candidate_binding(workspace, candidate_id).candidate_id == candidate_id
    with pytest.raises(ValueError, match="recorded EA code"):
        load_accepted_candidate(workspace, candidate_id)
    assert calls == []


def test_previously_loaded_binding_is_rechecked_before_running(
    selected: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, candidate_id, scenario = selected
    binding = inspect_candidate_binding(workspace, candidate_id)
    record = json.loads((workspace / "candidates" / f"{candidate_id}.json").read_bytes())
    source = record["projection"]["source"]["job_id"]
    report = workspace / "reports" / source / "report.json"
    report.write_bytes(report.read_bytes() + b" ")
    calls = no_module(monkeypatch)
    with pytest.raises(ValueError):
        run_accepted_candidate(binding, scenario, tmp_path / "runs")
    assert calls == []
    assert not (tmp_path / "runs").exists()
