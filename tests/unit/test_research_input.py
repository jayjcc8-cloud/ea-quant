from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ea.data import historical
from ea.product.scenario import load_backtest_scenario
from ea.web.service import JobRecord
from unit.test_web_api import _scenario_root


def test_full_capture_uses_existing_semantics(tmp_path: Path) -> None:
    root = _scenario_root(tmp_path)
    scenario = load_backtest_scenario(root / "bounded-long.yaml")
    inspect = getattr(historical, "read_full_capture_phase1_ohlcv_csv", None)
    assert callable(inspect), "full-capture inspection is missing"
    dataset = inspect(scenario.data_path)
    events = scenario.dataset.selection.events
    assert dataset.selection.fingerprint == scenario.dataset.selection.fingerprint
    assert dataset.replay_window.start_inclusive == min(e.available_at for e in events)
    assert dataset.replay_window.end_exclusive == max(e.available_at for e in events) + timedelta(
        microseconds=1
    )


def test_full_capture_rejects_invalid_csv(tmp_path: Path) -> None:
    path = tmp_path / "invalid.csv"
    path.write_text("invalid")
    inspect = getattr(historical, "read_full_capture_phase1_ohlcv_csv", None)
    assert callable(inspect), "full-capture inspection is missing"
    with pytest.raises(historical.HistoricalMarketDataError):
        inspect(path)


def test_registry_and_rebinding_are_path_independent(tmp_path: Path) -> None:
    import ea.product.scenario as scenarios

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    rebind = getattr(scenarios, "rebind_research_dataset", None)
    assert callable(rebind), "data-only rebinding is missing"
    target = tmp_path / "renamed.csv"
    target.write_bytes(base.data_path.read_bytes())
    data = historical.read_full_capture_phase1_ohlcv_csv(target)
    a = rebind(base, data_path=target, dataset=data, dataset_id="renamed.csv")
    b = rebind(base, data_path=base.data_path, dataset=data, dataset_id="other.csv")
    assert a.scenario_sha256 == b.scenario_sha256
    assert a.dataset is data
    import json

    old, new = json.loads(base.canonical_bytes), json.loads(a.canonical_bytes)
    old.pop("data")
    new.pop("data")
    assert old == new


def test_web_external_dataset_run_and_deleted_history(tmp_path: Path) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings, _wait

    settings = _settings(tmp_path)
    root = tmp_path / "data"
    root.mkdir()
    scenario = load_backtest_scenario(settings.scenario_root / "bounded-long.yaml")
    path = root / "research.csv"
    path.write_bytes(scenario.data_path.read_bytes())
    assert "data_root" in settings.__dataclass_fields__, "explicit data root is missing"
    settings = replace(settings, data_root=root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        catalog = client.get("/api/datasets").json()["datasets"]
        assert catalog[0]["dataset_id"] == path.name
        response = client.post(
            "/api/scenarios/bounded-long.yaml/validate",
            json={"dataset_id": path.name},
            headers=WRITE_HEADERS,
        )
        assert response.status_code == 200, response.text
        identity = response.json()["input_identity"]
        request = {
            "scenario_id": "bounded-long.yaml",
            "input_identity": identity,
            "request_id": "dataset-run-1",
        }
        response = client.post("/api/backtests", json=request, headers=WRITE_HEADERS)
        assert response.status_code == 202, response.text
        job = _wait(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job
        assert job["input_snapshot"]["research_input"]["dataset_id"] == path.name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        response = client.post(
            "/api/backtests", json={**request, "request_id": "dataset-run-2"}, headers=WRITE_HEADERS
        )
        assert response.status_code == 409, response.text
        path.unlink()
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get("/api/backtests/" + job["job_id"]).json()["status"] == "succeeded"
        assert client.get("/api/backtests/" + job["job_id"] + "/report").status_code == 200


def test_registered_holdout_uses_later_dataset(tmp_path: Path) -> None:
    from ea.web.service import WebService

    root = _scenario_root(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()
    base = load_backtest_scenario(root / "bounded-long.yaml")
    (data_root / "research.csv").write_bytes(base.data_path.read_bytes())
    (data_root / "later.csv").write_bytes(base.data_path.read_bytes().replace(b"2026-", b"2027-"))
    service = WebService(root, tmp_path / "workspace", data_root=data_root)
    service.start()
    try:
        summary = service.validate_scenario("bounded-long.yaml", dataset_id="research.csv")
        job, _ = service.create_job(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], summary["input_identity"]),
            request_id="research-source",
        )
        import time

        for _ in range(200):
            if service.get_job(job.job_id).status not in ("accepted", "running"):
                break
            time.sleep(0.02)
        assert service.get_job(job.job_id).status == "succeeded"
        candidates = service.holdout_candidates(job.job_id)
        assert any(
            cast(dict[str, object], c["input_identity"]).get("dataset_id") == "later.csv"
            for c in candidates
        ), candidates
    finally:
        service.stop()


def test_cli_inspect(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from ea.cli.app import app

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    result = CliRunner().invoke(app, ["data", "inspect", str(base.data_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source_sha256"] == base.dataset.source_bytes_sha256.value


def test_registry_invalid_and_unauthorized_entries(tmp_path: Path) -> None:
    from ea.web.datasets import LocalResearchDatasetRegistryV1

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "valid.csv").write_bytes(base.data_path.read_bytes())
    (data_root / "bad.csv").write_text("invalid")
    (data_root / "linked.csv").symlink_to(base.data_path)
    (data_root / "folder.csv").mkdir()
    registry = LocalResearchDatasetRegistryV1(data_root)
    catalog = {d["dataset_id"]: d["valid"] for d in registry.list()}
    assert catalog == {
        "valid.csv": True,
        "bad.csv": False,
        "linked.csv": False,
        "folder.csv": False,
    }
    for name in ("../prices.csv", str(base.data_path), "folder.csv/hidden.csv"):
        with pytest.raises(ValueError):
            registry.load(name)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_semantic_identity_and_raw_provenance(tmp_path: Path, version: int) -> None:
    from ea.product.scenario import rebind_research_dataset
    from unit.test_local_strategy_package import local_scenario
    from unit.test_scenario_v2 import write_v2

    strategy_root = None
    if version == 3:
        path, strategy_root, _ = local_scenario(tmp_path)
    elif version == 2:
        path = write_v2(tmp_path / "scenarios")
    else:
        path = _scenario_root(tmp_path) / "bounded-long.yaml"
    base = load_backtest_scenario(path, strategy_root=strategy_root)
    original_identity = base.scenario_sha256
    target = tmp_path / "external.csv"
    raw = base.data_path.read_bytes()
    target.write_bytes(raw)
    original = rebind_research_dataset(
        base,
        data_path=target,
        dataset=historical.read_full_capture_phase1_ohlcv_csv(target),
        dataset_id=target.name,
    )
    target.write_bytes(raw.replace(b"\n", b"\r\n"))
    representation = rebind_research_dataset(
        base,
        data_path=target,
        dataset=historical.read_full_capture_phase1_ohlcv_csv(target),
        dataset_id="renamed.csv",
    )
    assert representation.scenario_sha256 == original.scenario_sha256
    assert representation.dataset.source_bytes_sha256 != original.dataset.source_bytes_sha256
    assert (
        load_backtest_scenario(path, strategy_root=strategy_root).scenario_sha256
        == original_identity
    )
    # Volume is economic market-data identity even if this strategy does not consume it.
    rows = raw.decode().splitlines()
    fields = rows[1].split(",")
    fields[10] = "999"
    rows[1] = ",".join(fields)
    target.write_text("\n".join(rows) + "\n")
    changed = rebind_research_dataset(
        base,
        data_path=target,
        dataset=historical.read_full_capture_phase1_ohlcv_csv(target),
        dataset_id=target.name,
    )
    assert changed.dataset.selection.fingerprint != original.dataset.selection.fingerprint
    assert changed.scenario_sha256 != original.scenario_sha256


def test_dataset_changes_between_acceptance_and_execution_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.service import WebService

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    data = tmp_path / "data"
    data.mkdir()
    path = data / "research.csv"
    path.write_bytes(base.data_path.read_bytes())
    service = WebService(root, tmp_path / "workspace", data_root=data)
    calls = []
    monkeypatch.setattr(service._executor, "submit", lambda *args: calls.append(args))
    service.start()
    try:
        identity = service.validate_scenario("bounded-long.yaml", dataset_id=path.name)[
            "input_identity"
        ]
        job, _ = service.create_job(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], identity),
            request_id="captured-job",
        )
        assert service.get_job(job.job_id).status == "accepted"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        function, *args = calls[0]
        function(*args)
        assert service.get_job(job.job_id).error_code == "input_changed"
        assert not list(service.runs_dir.iterdir())
    finally:
        service.stop()


def test_registered_input_rejects_other_instrument_and_root_overlap(tmp_path: Path) -> None:
    from ea.web.service import WebBoundaryError, WebService

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "other.csv").write_bytes(base.data_path.read_bytes().replace(b",AAPL,", b",MSFT,"))
    service = WebService(root, tmp_path / "workspace", data_root=data)
    try:
        with pytest.raises(WebBoundaryError):
            service.validate_scenario("bounded-long.yaml", dataset_id="other.csv")
    finally:
        service.stop()
    with pytest.raises(WebBoundaryError):
        WebService(root, tmp_path / "workspace2", data_root=root)


def test_local_dataset_holdout_freezes_package_after_deletion(tmp_path: Path) -> None:
    import json
    import time

    from ea.web.service import WebService
    from unit.test_local_strategy_package import local_scenario

    path, strategy_root, package = local_scenario(tmp_path)
    base = load_backtest_scenario(path, strategy_root=strategy_root)
    data = tmp_path / "data"
    data.mkdir()
    (data / "research.csv").write_bytes(base.data_path.read_bytes())
    (data / "later.csv").write_bytes(base.data_path.read_bytes().replace(b"2026-", b"2027-"))
    service = WebService(
        path.parent, tmp_path / "workspace", strategy_root=strategy_root, data_root=data
    )
    service.start()

    def wait(job_id: str) -> JobRecord:
        for _ in range(200):
            job = service.get_job(job_id)
            if job.status not in {"accepted", "running"}:
                return job
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    try:
        identity = service.validate_scenario(path.name, dataset_id="research.csv")["input_identity"]
        source, _ = service.create_job(
            scenario_id=path.name,
            input_identity=cast(dict[str, object], identity),
            request_id="local-source",
        )
        assert wait(source.job_id).status == "succeeded"
        for artifact in strategy_root.glob("*.eastrategy"):
            artifact.unlink()
        (data / "research.csv").unlink()
        relation = service.create_holdout(
            source_job_id=source.job_id, scenario_id=path.name, dataset_id="later.csv"
        )
        holdout = wait(relation["holdout_job_id"])
        assert holdout.status == "succeeded", holdout
        assert holdout.strategy_package is not None
        assert holdout.strategy_package.artifact_bytes == package.artifact_bytes
        assert (
            json.loads(holdout.input_snapshot_bytes or b"{}")["research_input"]["dataset_id"]
            == "later.csv"
        )
    finally:
        service.stop()


def test_batch_members_execute_one_captured_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.service import WebService

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "research.csv").write_bytes(base.data_path.read_bytes())
    service = WebService(root, tmp_path / "workspace", data_root=data)
    calls = []
    monkeypatch.setattr(service._executor, "submit", lambda *args: calls.append(args))
    service.start()
    try:
        identity = service.validate_scenario("bounded-long.yaml", dataset_id="research.csv")[
            "input_identity"
        ]
        service.create_batch(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], identity),
            initial_cash="10000",
            runs=[
                {"target_quantity": "2", "entry_delay_bars": 0},
                {"target_quantity": "3", "entry_delay_bars": 0},
            ],
        )
        executable = calls[0][2]
        assert executable[0][1].dataset is executable[1][1].dataset
    finally:
        service.stop()


def test_engine_entry_rejects_representation_change_after_web_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import ea.web.service as service_module
    from ea.web.service import WebService

    root = _scenario_root(tmp_path)
    base = load_backtest_scenario(root / "bounded-long.yaml")
    data = tmp_path / "data"
    data.mkdir()
    path = data / "research.csv"
    path.write_bytes(base.data_path.read_bytes())
    from ea.product import run_backtest_scenario as original

    def change_then_run(scenario: Any, *args: Any, **kwargs: Any) -> Any:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        return original(scenario, *args, **kwargs)

    monkeypatch.setattr(service_module, "run_backtest_scenario", change_then_run)
    service = WebService(root, tmp_path / "workspace", data_root=data)
    service.start()
    try:
        identity = service.validate_scenario("bounded-long.yaml", dataset_id=path.name)[
            "input_identity"
        ]
        accepted, _ = service.create_job(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], identity),
            request_id="engine-entry-race",
        )
        for _ in range(200):
            job = service.get_job(accepted.job_id)
            if job.status not in {"accepted", "running"}:
                break
            time.sleep(0.02)
        assert job.status == "failed", "changed raw provenance must never become a successful job"
        assert job.error_code == "input_changed"
        assert not list(service.reports_dir.iterdir())
    finally:
        service.stop()
