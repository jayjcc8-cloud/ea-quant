from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.product import load_backtest_scenario, run_backtest_scenario
from unit.test_backtest_single_run import _scenario


def round_trip_scenario(tmp_path: Path, *, delay: int = 0, hold: int = 1) -> Path:
    path = _scenario(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["schema_version"] = 4
    document["strategy"] = {
        "id": "single-long-hold-roots-v1",
        "version": 1,
        "action_contract": "V2",
        "position_lifecycle": "single-long-round-trip-v1",
        "parameters": {"target_quantity": "2", "entry_delay": delay, "hold_root_count": hold},
    }
    document["execution"]["commission"] = {
        "policy": "deterministic-commission-v1",
        "commission_bps": "10",
    }
    path.write_text(yaml.safe_dump(document))
    return path


def test_scenario_v4_has_separate_identity(tmp_path: Path) -> None:
    loaded = load_backtest_scenario(round_trip_scenario(tmp_path))
    assert loaded.schema_version == 4
    assert json.loads(loaded.canonical_bytes)["canonicalization"] == "ea-backtest-scenario-v4"
    assert loaded.strategy_parameters == {
        "target_quantity": "2",
        "entry_delay": 0,
        "hold_root_count": 1,
    }


@pytest.mark.parametrize("delay, state, fills", [(99, "FLAT_INITIAL", 0), (0, "LONG_OPEN", 1)])
def test_round_trip_short_replay_never_forces_exit(
    tmp_path: Path,
    delay: int,
    state: str,
    fills: int,
) -> None:
    loaded = load_backtest_scenario(round_trip_scenario(tmp_path / "input", delay=delay))
    completed = run_backtest_scenario(loaded, tmp_path / "runs")
    result = json.loads((completed.output_directory / "result.json").read_bytes())
    assert result["schema"] == "ea.backtest-single-run-result.v3"
    assert result["position_state"] == state
    assert sum(leg["fill"] is not None for leg in result["execution_legs"]) == fills
    assert result["ledger_sequence"] == fills + 1


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "EXIT_LONG", "quantity": "2"},
        {"action": "HOLD", "quantity": None},
        {"action": "ENTER_LONG", "quantity": "0"},
        {"action": "ENTER_LONG", "quantity": "2.0"},
        {"action": "ENTER_LONG"},
        {"action": "SHORT"},
        {"action": "HOLD", "other": 1},
    ],
)
def test_action_v2_rejects_ambiguous_payload(payload: dict[str, object]) -> None:
    from ea.strategy.sdk_v2 import decode_action

    with pytest.raises(ValueError):
        decode_action(payload)


def closed_scenario(tmp_path: Path) -> Path:
    from datetime import UTC, datetime

    from ea.core import ReplayWindow
    from ea.data import decode_phase1_ohlcv_csv

    path = round_trip_scenario(tmp_path)
    csv = tmp_path / "prices.csv"
    with csv.open("a") as stream:
        for minute, price in [(33, 110), (34, 120), (35, 125)]:
            stream.write(
                f"1,XNAS,AAPL,2026-01-02T09:{minute - 1}:00.000000Z,"
                f"2026-01-02T09:{minute}:00.000000Z,"
                f"raw,{price},{price},{price},{price},10,fixture.raw,{minute - 30},0,"
                f"2026-01-02T09:{minute}:00.000000Z\n"
            )
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC), datetime(2026, 1, 2, 9, 36, tzinfo=UTC)
    )
    data = decode_phase1_ohlcv_csv(csv.read_bytes(), replay_window=window)
    document = yaml.safe_load(path.read_text())
    document["data"]["end_utc"] = "2026-01-02T09:36:00.000000Z"
    document["data"]["fingerprint"] = {
        "sha256": data.selection.fingerprint.sha256.value,
        "record_count": data.selection.fingerprint.record_count,
    }
    path.write_text(yaml.safe_dump(document))
    return path


def test_complete_round_trip_uses_actual_exit_and_two_fees(tmp_path: Path) -> None:
    loaded = load_backtest_scenario(closed_scenario(tmp_path / "input"))
    completed = run_backtest_scenario(loaded, tmp_path / "runs")
    result = json.loads((completed.output_directory / "result.json").read_bytes())
    assert result["position_state"] == "FLAT_CLOSED"
    assert result["ledger_sequence"] == 3
    assert result["final_quantity"] == "0"
    assert [leg["fill"]["side"] for leg in result["execution_legs"]] == ["buy", "sell"]
    assert [leg["fill"]["price"] for leg in result["execution_legs"]] == ["101.5", "120"]
    assert result["ending_cash"] == [{"amount": "10036.56", "currency": "USD"}]


@pytest.mark.parametrize(
    "stage, occurrence",
    [
        ("funding_durable", 1),
        ("dispatch_durable", 1),
        ("dispatch_durable", 2),
        ("reconciliation_durable", 1),
        ("terminal_before_publication", 1),
    ],
)
def test_two_leg_resume_is_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    occurrence: int,
) -> None:
    from uuid import uuid4

    from ea.core import RunId
    from ea.product import backtest as b
    from unit.test_backtest_resume import _AbruptInterruption

    loaded = load_backtest_scenario(closed_scenario(tmp_path / "input"))
    run_id = RunId(str(uuid4()))
    baseline = run_backtest_scenario(loaded, tmp_path / "baseline", run_id=run_id).output_directory
    seen = 0

    def interrupt(current: str) -> None:
        nonlocal seen
        if current == stage:
            seen += 1
            if seen == occurrence:
                raise _AbruptInterruption()

    monkeypatch.setattr(b, "_TEST_INTERRUPT", interrupt)
    publish = b._publish_success
    if stage == "terminal_before_publication":

        def interrupted_publication(*args: Any) -> None:
            raise _AbruptInterruption()

        monkeypatch.setattr(b, "_publish_success", interrupted_publication)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(loaded, tmp_path / "interrupted", run_id=run_id)
    monkeypatch.setattr(b, "_TEST_INTERRUPT", None)
    monkeypatch.setattr(b, "_publish_success", publish)
    attempt = tmp_path / "interrupted" / run_id.value
    resumed = b.resume_backtest_attempt(attempt)
    for filename in ("result.json", "audit.jsonl", "funding.json"):
        assert (resumed.output_directory / filename).read_bytes() == (
            baseline / filename
        ).read_bytes()
    assert b.resume_backtest_attempt(attempt) == resumed


@pytest.mark.parametrize("closed", [False, True])
def test_formal_report_v2_exact_accounting(tmp_path: Path, closed: bool) -> None:
    from ea.product import BacktestReportV1, generate_backtest_report

    path = (closed_scenario if closed else round_trip_scenario)(tmp_path / "input")
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    assert not isinstance(report, BacktestReportV1)
    doc = json.loads(report.canonical_bytes)
    assert doc["schema"] == "ea.backtest-report.v2"
    assert doc["economics"]["realized_pnl"]["amount"] == ("36.56" if closed else "0")
    assert doc["economics"]["gross_unrealized_pnl"]["amount"] == "0"
    assert doc["economics"]["fees"]["amount"] == ("0.44" if closed else "0.2")
    assert doc["economics"]["net_pnl"]["amount"] == ("36.56" if closed else "-0.2")
    assert (
        generate_backtest_report(attempt, tmp_path / "report").report.canonical_bytes
        == report.canonical_bytes
    )


def test_recovery_classifies_two_leg_reconciliation(tmp_path: Path) -> None:
    from ea.product import backtest as b
    from ea.product import reporting as r

    loaded = load_backtest_scenario(closed_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(loaded, tmp_path / "runs").output_directory
    _, run, _, _, _, manifest = b._load_verified_attempt(attempt)
    records = r._read_audit_journal(attempt, manifest.binding)
    assert (
        b._verified_persisted_frontier(
            scenario=loaded, run_id=run, attempt=attempt, records=records
        )
        == "reconciliation_durable"
    )


def test_entry_resize_exit_uses_committed_quantity(tmp_path: Path) -> None:
    path = closed_scenario(tmp_path / "input")
    doc = yaml.safe_load(path.read_text())
    doc["strategy"]["parameters"]["target_quantity"] = "4"
    doc["risk"]["max_order_quantity"] = "1"
    path.write_text(yaml.safe_dump(doc))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    assert [leg["fill"]["quantity"] for leg in result["execution_legs"]] == ["1", "1"]
    assert result["execution_legs"][0]["risk"]["decision"] == "resize"
    assert result["execution_legs"][1]["risk"]["decision"] == "allow"


@pytest.mark.parametrize("decision", ["reject", "resize"])
def test_exit_risk_failure_prevents_second_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    from types import SimpleNamespace

    from ea.core import RiskDecisionKind
    from ea.product import BacktestRunFailure
    from ea.risk.authority import Phase1RiskAuthority

    original = Phase1RiskAuthority.evaluate

    def evaluate(self: Any, intent: Any, snapshot: Any) -> Any:
        result = original(self, intent, snapshot)
        if intent.side.value == "sell":
            return SimpleNamespace(decision=SimpleNamespace(kind=RiskDecisionKind(decision)))
        return result

    monkeypatch.setattr(Phase1RiskAuthority, "evaluate", evaluate)
    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(
            load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
        )
    attempt = next((tmp_path / "runs").iterdir())
    assert not (attempt / "result.json").exists()
    assert (
        json.loads((attempt / "failure.json").read_bytes())["last_durable_frontier"]
        == "dispatch_durable"
    )


@pytest.mark.parametrize("action", ["ENTER_LONG", "EXIT_LONG"])
def test_invalid_position_action_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    from ea.product import BacktestRunFailure
    from ea.strategy.sdk_v2 import HoldRootsLogic, PositionState

    original = HoldRootsLogic.on_bar

    def invalid(self: Any, bar: Any, position: Any) -> dict[str, str]:
        if action == "EXIT_LONG" or position.state is PositionState.LONG_OPEN:
            return {"action": action, **({"quantity": "2"} if action == "ENTER_LONG" else {})}
        return original(self, bar, position)

    monkeypatch.setattr(HoldRootsLogic, "on_bar", invalid)
    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(
            load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
        )
    assert not (next((tmp_path / "runs").iterdir()) / "result.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("position_state", "LONG_OPEN"),
        ("final_equity", "99999"),
        ("final_quantity", "1"),
        ("completed_round_trips", 0),
    ],
)
def test_report_rejects_tampered_result(tmp_path: Path, field: str, value: object) -> None:
    from ea.product import BacktestReportError, generate_backtest_report

    attempt = run_backtest_scenario(
        load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
    ).output_directory
    path = attempt / "result.json"
    result = json.loads(path.read_bytes())
    result[field] = value
    path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(BacktestReportError):
        generate_backtest_report(attempt, tmp_path / "report")
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize("tamper", ["partial", "unknown_order", "third_ingress", "side"])
def test_closed_matcher_rejects_tampered_ingress_before_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    from ea.core import CanonicalDecimal, EconomicId, OrderSide
    from ea.execution.matcher import Phase1HistoricalMatcher
    from ea.product import BacktestRunFailure
    from ea.product import round_trip as rt

    original_match = Phase1HistoricalMatcher.match_active_market_root
    from ea.composition.lifecycle import create_phase1_historical_economic_gate

    original_gate = create_phase1_historical_economic_gate
    gates: list[Any] = []
    injected: list[bool] = []

    def gate(**kwargs: Any) -> Any:
        result = original_gate(**kwargs)
        gates.append(result)
        return result

    def match(self: Any, *args: Any, **kwargs: Any) -> Any:
        batch = original_match(self, *args, **kwargs)
        if batch.ingresses and not injected:
            ingress = batch.ingresses[0]
            if tamper == "partial":
                object.__setattr__(ingress.fact.payload, "quantity", CanonicalDecimal("1"))
            elif tamper == "unknown_order":
                identity = ingress.fact.order_id
                assert identity is not None
                object.__setattr__(
                    ingress.fact, "order_id", EconomicId(identity.run_id, identity.owner_kind, 99)
                )
            elif tamper == "side":
                object.__setattr__(ingress.fact.payload, "side", OrderSide.SELL)
            else:
                object.__setattr__(batch, "ingresses", batch.ingresses * 3)
            injected.append(True)
        return batch

    monkeypatch.setattr(rt, "create_phase1_historical_economic_gate", gate)
    monkeypatch.setattr(Phase1HistoricalMatcher, "match_active_market_root", match)
    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(
            load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
        )
    assert injected
    assert gates[0].ledger.snapshot.ledger_sequence == 1
    assert gates[0].ledger.snapshot.cash_balances[0].amount.text == "10000"


@pytest.mark.parametrize("exit_leg", [False, True])
def test_expired_order_does_not_fabricate_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_leg: bool
) -> None:
    from ea.product import generate_backtest_report
    from ea.product import round_trip as rt

    path = closed_scenario(tmp_path / "input")
    doc = yaml.safe_load(path.read_text())
    doc["strategy"]["parameters"].update(entry_delay=0 if exit_leg else 4, hold_root_count=3)
    path.write_text(yaml.safe_dump(doc))
    # Exercise the existing matcher expiry path with a decision at the last raw root.
    monkeypatch.setattr(rt, "_next_bar_entry_delay_maximum", lambda dataset: 99)
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    assert result["execution_legs"][-1]["outcome"] == "expired"
    assert result["execution_legs"][-1]["fill"] is None
    assert result["position_state"] == ("LONG_OPEN" if exit_leg else "FLAT_INITIAL")
    assert result["ledger_sequence"] == (2 if exit_leg else 1)
    generate_backtest_report(attempt, tmp_path / "report")


def test_report_binds_order_bytes_and_hash(tmp_path: Path) -> None:
    from ea.product import generate_backtest_report

    result = run_backtest_scenario(
        load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
    )
    document: Any = generate_backtest_report(
        result.output_directory, tmp_path / "report"
    ).report.document
    legs = document["economics"]["execution_legs"]
    assert all(len(leg["order_sha256"]) == 64 and len(leg["fill_sha256"]) == 64 for leg in legs)


def test_second_leg_corrupt_journal_cannot_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.product import BacktestResumeFailure
    from ea.product import backtest as b
    from unit.test_backtest_resume import _AbruptInterruption

    count = 0

    def interrupt(stage: str) -> None:
        nonlocal count
        if stage == "dispatch_durable":
            count += 1
            if count == 2:
                raise _AbruptInterruption()

    monkeypatch.setattr(b, "_TEST_INTERRUPT", interrupt)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(
            load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
        )
    monkeypatch.setattr(b, "_TEST_INTERRUPT", None)
    attempt = next((tmp_path / "runs").iterdir())
    journal = attempt / "audit" / "audit-v1.journal"
    corrupted = bytearray(journal.read_bytes())
    corrupted[-1] ^= 1
    journal.write_bytes(corrupted)
    with pytest.raises(BacktestResumeFailure):
        b.resume_backtest_attempt(attempt)
    assert journal.read_bytes() == corrupted
    assert not (attempt / "result.json").exists()


def test_v2_reader_rejects_unknown_economic_field(tmp_path: Path) -> None:
    from ea.product import BacktestReportError, BacktestReportV2, generate_backtest_report

    result = run_backtest_scenario(
        load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
    )
    report = generate_backtest_report(result.output_directory, tmp_path / "report").report
    document: Any = report.document
    document["economics"]["invented_metric"] = "123"
    with pytest.raises(BacktestReportError):
        BacktestReportV2(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            report.summary_bytes,
        )
