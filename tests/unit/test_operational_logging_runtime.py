from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ea.core import RunId
from ea.product import (
    BacktestRunFailure,
    generate_backtest_report,
    load_backtest_scenario,
    resume_backtest_attempt,
    run_backtest_scenario,
)
from unit.test_backtest_single_run import _scenario
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_single_round_trip_v1 import closed_scenario


def _events(attempt: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (attempt / "operational.jsonl").read_text().splitlines()]


def test_simulated_trade_reconstructs_causal_chain_from_json(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    events = _events(attempt)
    signal = next(e for e in events if e["event"] == "strategy.decision" and e["outcome"] == "long")
    correlated = [e for e in events if e.get("correlation_id") == signal["correlation_id"]]
    assert {e["event"] for e in correlated} >= {
        "strategy.decision",
        "risk.decision",
        "order.created",
        "broker.submitted",
        "execution.fact",
        "execution.fill",
        "portfolio.updated",
    }
    assert any(
        e["event"] == "market.event" and e["market_event_id"] == signal["market_event_id"]
        for e in events
    )
    reconciliations = [e for e in correlated if e["event"] == "reconciliation.result"]
    assert {e["scope"] for e in reconciliations} == {"cash", "position"}
    order = next(e for e in correlated if e["event"] == "order.created")
    for event in correlated:
        if event["event"] in {
            "broker.submitted",
            "execution.fact",
            "execution.fill",
            "portfolio.updated",
        }:
            assert event["order_id"] == order["order_id"]
            assert event["client_order_id"] == order["client_order_id"]
    fill = next(e for e in correlated if e["event"] == "execution.fill")
    assert all(e["fill_id"] == fill["fill_id"] for e in reconciliations)
    portfolio = next(e for e in correlated if e["event"] == "portfolio.updated")
    assert portfolio["fill_id"] == fill["fill_id"]
    assert portfolio["ledger_sequence"] == 2
    assert any(
        e["event"] == "reconciliation.result" and e["outcome"] == "reconciliation.match"
        for e in events
    )
    assert {e["run_id"] for e in events} == {attempt.name}
    assert {e["strategy_id"] for e in events} == {scenario.strategy_id.value}
    assert all(e["candidate_id"] is None and e["account_id"] is None for e in events)


@pytest.mark.parametrize("factory", [_scenario, closed_scenario, bounded_scenario])
@pytest.mark.parametrize("broken_sink", [False, True])
def test_logging_preserves_exact_economics_and_audit(
    tmp_path: Path, factory: Any, broken_sink: bool
) -> None:
    scenario = load_backtest_scenario(factory(tmp_path / "input"))
    run_id = RunId("00000000-0000-4000-8000-000000000004")
    disabled = run_backtest_scenario(
        scenario, tmp_path / "disabled", run_id=run_id, operational_logging=False
    ).output_directory

    def fail(_line: str) -> None:
        raise OSError("sink unavailable")

    enabled = run_backtest_scenario(
        scenario,
        tmp_path / "enabled",
        run_id=run_id,
        operational_sink=fail if broken_sink else None,
    ).output_directory
    for name in ("result.json", "audit.jsonl", "funding.json", "manifest.json"):
        assert (enabled / name).read_bytes() == (disabled / name).read_bytes()
    assert generate_backtest_report(
        enabled, tmp_path / "enabled-report"
    ).report.canonical_bytes == (
        generate_backtest_report(disabled, tmp_path / "disabled-report").report.canonical_bytes
    )
    assert not (disabled / "operational.jsonl").exists()
    if not broken_sink:
        events = _events(enabled)
        result = json.loads((enabled / "result.json").read_bytes())
        fills = (
            1
            if scenario.schema_version < 4
            else sum(leg["fill"] is not None for leg in result["execution_legs"])
        )
        assert sum(e["event"] == "execution.fill" for e in events) == fills


def test_risk_denial_retains_context_without_order(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input", funding__initial_cash="50"))
    with pytest.raises(BacktestRunFailure) as captured:
        run_backtest_scenario(scenario, tmp_path / "runs")
    events = _events(captured.value.output_directory)
    rejected = next(e for e in events if e["event"] == "risk.decision")
    assert rejected["outcome"] == "reject"
    assert rejected["correlation_id"]
    assert rejected["market_event_id"]
    assert any(
        e["event"] == "strategy.decision" and e["correlation_id"] == rejected["correlation_id"]
        for e in events
    )
    assert not any(e["event"] in {"order.created", "execution.fill"} for e in events)
    assert events[-1]["event"] == "run.failed"


def test_completed_verification_does_not_modify_operational_evidence(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    before = {p.relative_to(attempt): p.read_bytes() for p in attempt.rglob("*") if p.is_file()}
    resume_backtest_attempt(attempt)
    assert {
        p.relative_to(attempt): p.read_bytes() for p in attempt.rglob("*") if p.is_file()
    } == before

    class Capture(list[str]):
        def __call__(self, line: str) -> None:
            self.append(line)

    verification = Capture()
    assert not verification
    resume_backtest_attempt(attempt, operational_sink=verification)
    assert verification
    assert {json.loads(line)["operation"] for line in verification} == {"verify"}
    assert {
        p.relative_to(attempt): p.read_bytes() for p in attempt.rglob("*") if p.is_file()
    } == before


def test_resume_logs_replay_without_duplicate_economic_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ea.product.backtest as backtest
    from unit.test_backtest_resume import _AbruptInterruption, _interrupt_at

    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    _interrupt_at(monkeypatch, "dispatch_durable")
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, tmp_path / "runs")
    attempt = next((tmp_path / "runs").iterdir())
    monkeypatch.setattr(backtest, "_TEST_INTERRUPT", None)
    resume_backtest_attempt(attempt)
    events = _events(attempt)
    assert {e["operation"] for e in events} == {"run", "resume"}
    fills = [e for e in events if e["event"] == "execution.fill"]
    assert len({e["fill_id"] for e in fills}) == 1
    assert json.loads((attempt / "result.json").read_bytes())["ledger_sequence"] == 2


def test_observation_lookup_failure_cannot_change_economics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ea.product.operational as operational

    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    run_id = RunId("00000000-0000-4000-8000-000000000004")
    baseline = run_backtest_scenario(
        scenario, tmp_path / "baseline", run_id=run_id, operational_logging=False
    ).output_directory

    def unavailable(_market: Any) -> Any:
        raise RuntimeError("observation only")

    monkeypatch.setattr(operational, "causal_market_digest", unavailable)
    observed = run_backtest_scenario(
        scenario, tmp_path / "observed", run_id=run_id
    ).output_directory
    assert (baseline / "result.json").read_bytes() == (observed / "result.json").read_bytes()
    assert (baseline / "audit.jsonl").read_bytes() == (observed / "audit.jsonl").read_bytes()
