from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.product import (
    BacktestRunFailure,
    generate_backtest_report,
    load_backtest_scenario,
    run_backtest_scenario,
)
from unit.test_commission import commission_scenario
from unit.test_slippage import add_slippage

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


def add_latency(path: Path, milliseconds: object = 60000) -> Path:
    document = yaml.safe_load(path.read_text())
    document["execution"]["latency"] = {
        "policy": "deterministic-latency-v1",
        "latency_ms": milliseconds,
    }
    path.write_text(yaml.safe_dump(document))
    return path


@pytest.mark.parametrize("milliseconds", [-1, 86400001, True, "60000", 1.5, None])
def test_invalid_latency(tmp_path: Path, milliseconds: object) -> None:
    with pytest.raises(ValueError):
        load_backtest_scenario(add_latency(commission_scenario(tmp_path), milliseconds))


@pytest.mark.parametrize(
    "milliseconds,price,pnl",
    [(0, "102.52", "12.91"), (59999, "102.52", "12.91"), (60000, "111.1", "-4.42")],
)
def test_delay_boundary_controls_actual_fill(
    tmp_path: Path, milliseconds: int, price: str, pnl: str
) -> None:
    path = add_latency(add_slippage(commission_scenario(tmp_path / "input")), milliseconds)
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    assert result["fill_evidence"]["price"] == price
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    assert report["economics"]["net_pnl"]["amount"] == pnl


@pytest.mark.parametrize("milliseconds", [120000, 86400000])
def test_latency_beyond_source_never_fills(tmp_path: Path, milliseconds: int) -> None:
    path = add_latency(commission_scenario(tmp_path / "input"), milliseconds)
    with pytest.raises(BacktestRunFailure) as caught:
        run_backtest_scenario(load_backtest_scenario(path), tmp_path / "runs")
    attempt = caught.value.output_directory
    assert not (attempt / "result.json").exists()
    failure = json.loads((attempt / "failure.json").read_bytes())
    assert failure["terminal_state"] == "failed"
    assert failure["fill"] is None
    assert failure["code"] == "order.expired.no_eligible_market_data"
    records = [json.loads(line) for line in (attempt / "audit.jsonl").read_text().splitlines()]
    outcomes = [
        row["payload"]
        for row in records
        if row["header"]["record_kind"] == "execution.fact_processing_outcome"
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["action"] == "accepted"
    assert outcomes[0]["fill"] is None


def test_latency_identity_binds_all_assumptions() -> None:
    from ea.core import CanonicalDecimal
    from ea.core.commission import (
        commission_bps_from_identity,
        execution_latency_policy_identity,
        latency_ms_from_identity,
        slippage_bps_from_identity,
    )

    identity = execution_latency_policy_identity(
        60000, CanonicalDecimal("25"), CanonicalDecimal("10")
    )
    assert latency_ms_from_identity(*identity) == 60000
    assert slippage_bps_from_identity(*identity) == CanonicalDecimal("25")
    assert commission_bps_from_identity(*identity) == CanonicalDecimal("10")
    with pytest.raises(ValueError):
        latency_ms_from_identity(identity[0], "0" * 64)


def test_independent_fact_check_rejects_early_matcher_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ea.execution.matcher as matcher

    path = add_latency(commission_scenario(tmp_path / "input"))
    monkeypatch.setattr(matcher, "execution_latency_allows", lambda *args, **kwargs: True)
    with pytest.raises(BacktestRunFailure) as caught:
        run_backtest_scenario(load_backtest_scenario(path), tmp_path / "runs")
    assert not (caught.value.output_directory / "result.json").exists()


@pytest.mark.parametrize("version", [1, 5])
@pytest.mark.parametrize("stage", ["funding_durable", "dispatch_durable", "reconciliation_durable"])
def test_fractional_delayed_resume_uses_original_submission_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int, stage: str
) -> None:
    from uuid import uuid4

    from ea.core import RunId
    from ea.product import backtest as b
    from unit.test_backtest_resume import _AbruptInterruption, _interrupt_at
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    path = (
        commission_scenario(tmp_path / "input")
        if version == 1
        else bounded_scenario(tmp_path / "input", bars=18)
    )
    add_latency(add_slippage(path))
    document = yaml.safe_load(path.read_text())
    document["instrument"]["quantity_quantum"] = "0.001"
    parameters = document["strategy"] if version == 1 else document["strategy"]["parameters"]
    parameters["target_quantity"] = "0.333"
    path.write_text(yaml.safe_dump(document))
    scenario = load_backtest_scenario(path)
    run_id = RunId(str(uuid4()))
    baseline = run_backtest_scenario(
        scenario, tmp_path / "baseline", run_id=run_id
    ).output_directory
    _interrupt_at(monkeypatch, stage)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, tmp_path / "resumed", run_id=run_id)
    monkeypatch.setattr(b, "_TEST_INTERRUPT", None)
    attempt = tmp_path / "resumed" / run_id.value
    b.resume_backtest_attempt(attempt)
    assert (attempt / "result.json").read_bytes() == (baseline / "result.json").read_bytes()
    assert generate_backtest_report(attempt, tmp_path / "report1").report.canonical_bytes == (
        generate_backtest_report(baseline, tmp_path / "report2").report.canonical_bytes
    )


@pytest.mark.parametrize("version", [2, 3])
def test_local_action_packages_delay_fills(tmp_path: Path, version: int) -> None:
    from unit.test_local_action_v2_package import local_v2, set_prices
    from unit.test_local_bounded_round_trips_v1 import local_v3

    path, root, _ = (local_v2 if version == 2 else local_v3)(tmp_path / "input")
    if version == 2:
        set_prices(path, [10, 9, 11, 12, 13, 8, 7, 6])
    add_latency(path)
    attempt = run_backtest_scenario(
        load_backtest_scenario(path, strategy_root=root), tmp_path / "runs"
    ).output_directory
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    result = json.loads((attempt / "result.json").read_bytes())
    assert result["execution_legs"][0]["fill"]["price"] == "13"
    assert report["economics"]["counts"]["fills"] >= 2


def test_explicit_zero_preserves_economics(tmp_path: Path) -> None:
    path = add_slippage(commission_scenario(tmp_path / "input"))
    original = load_backtest_scenario(path)
    baseline = run_backtest_scenario(original, tmp_path / "baseline").output_directory
    first: Any = generate_backtest_report(baseline, tmp_path / "report1").report.document
    add_latency(path, 0)
    delayed = load_backtest_scenario(path)
    assert delayed.scenario_sha256 != original.scenario_sha256
    attempt = run_backtest_scenario(delayed, tmp_path / "runs").output_directory
    second: Any = generate_backtest_report(attempt, tmp_path / "report2").report.document
    assert first["economics"] == second["economics"]


def test_bounded_lifecycle_expiry_retains_no_fill_economics(tmp_path: Path) -> None:
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    path = add_latency(bounded_scenario(tmp_path / "input"), 86400000)
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    assert report["economics"]["counts"]["fills"] == 0
    assert report["economics"]["equity"]["amount"] == "10000"
    assert report["economics"]["fees"]["amount"] == "0"


def test_timestamp_boundary_is_exact_and_overflow_safe() -> None:
    from datetime import UTC, datetime, timedelta

    from ea.core.commission import execution_latency_allows, execution_latency_policy_identity

    identity = execution_latency_policy_identity(60000, None, None)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert not execution_latency_allows(start + timedelta(seconds=60), start, *identity)
    assert execution_latency_allows(start + timedelta(seconds=60, microseconds=1), start, *identity)
    latest = datetime.max.replace(tzinfo=UTC)
    assert not execution_latency_allows(latest, latest, *identity)


def test_web_holdout_freezes_and_reopens_latency(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_api import ORIGIN, _settings, _validate, _wait
    from unit.test_web_holdout import _request, _source, _target

    settings = _settings(tmp_path)
    path = add_latency(settings.scenario_root / "bounded-long.yaml")
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert _validate(client, path.name)["summary"]["latency"]["latency_ms"] == 60000
        source = _source(client)
        response = _request(client, source)
        assert response.status_code == 202, response.text
        holdout = _wait(client, response.json()["holdout_job_id"])
        assert holdout["status"] == "succeeded"
        assert (
            holdout["input_snapshot"]["scenario"]["execution"]
            == source["input_snapshot"]["scenario"]["execution"]
        )
        reports = [
            client.get(f"/api/backtests/{job['job_id']}/report").json() for job in (source, holdout)
        ]
        assert reports[0]["economics"]["execution"]["fill"]["price"] == "110"
    path.unlink()
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        for job, report in zip((source, holdout), reports, strict=True):
            assert client.get(f"/api/backtests/{job['job_id']}/report").json() == report
