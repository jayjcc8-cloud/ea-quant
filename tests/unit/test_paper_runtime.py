from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ea.core import AuditRecordKind, RunBinding, RunReference, Sha256Digest
from ea.product import load_backtest_scenario
from ea.product.market_stream import LocalSimulatedMarketSource, run_local_market_stream
from ea.product.offline_demo import _DemoAudit
from ea.product.paper import PAPER_MARKET_AUDIT_FRAMES, PaperTradingSession
from ea.risk.operational_safety import OperationalSafetyLimits
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_streaming import RUN_ID, SOURCE, ControlledClock


def session(
    tmp_path: Path,
    *,
    fail_kind: AuditRecordKind | None = None,
    fail_on_fill: bool = False,
    cash: str = "1000",
    kill_switch: Any = None,
    operational_limits: Any = None,
) -> tuple[PaperTradingSession, ControlledClock]:
    path = bounded_scenario(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["funding"]["initial_cash"] = cash
    doc["risk"]["max_notional"] = "1000"
    doc["execution"]["slippage"] = {"policy": "deterministic-slippage-v1", "slippage_bps": "100"}
    path.write_text(yaml.safe_dump(doc))
    scenario = load_backtest_scenario(path)
    binding = RunBinding(RunReference(RUN_ID, Sha256Digest("1" * 64)), Sha256Digest("2" * 64))

    class FailingAudit(_DemoAudit):
        def append(self, **kwargs: Any) -> Any:
            payload = json.loads(kwargs["canonical_payload"])
            if kwargs["record_kind"] is fail_kind and (
                not fail_on_fill
                or payload.get("fill_id") is not None
                or payload.get("fill") is not None
            ):
                raise OSError("audit unavailable")
            return super().append(**kwargs)

    clock = ControlledClock()
    engine = PaperTradingSession(
        scenario,
        binding=binding,
        audit=FailingAudit(binding, scenario.spec_set),
        clock=clock,
        monotonic=clock.monotonic,
        source_id=SOURCE,
        prices=(100.0,),
        stop_requested=lambda: False,
        kill_switch=kill_switch,
        operational_limits=operational_limits,
    )
    return engine, clock


def drive(engine: PaperTradingSession, clock: ControlledClock, *, count: int = 20) -> None:
    source = LocalSimulatedMarketSource(
        instrument=engine.scenario.instrument,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=None,
        bar_seconds=1.0,
        event_limit=count,
    )
    run_local_market_stream(
        source=source,
        runtime=engine.runtime,
        clock=clock,
        sleep=clock.advance,
        poll_interval_seconds=1.0,
        on_market=engine.on_market,
        on_fact=engine.on_fact,
    )


def test_continuous_three_trips_have_independent_money_and_duplicate_protection(
    tmp_path: Path,
) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    status = engine.status()
    # Six settlements: buy 101/sell 99, quantity 2, 0.20 fee per leg => 1000 - 13.20.
    assert status["cash"] == "986.8"
    assert status["equity"] == "986.8"
    assert status["position"] == "0"
    assert status["fills"] == 6
    assert status["duplicate_facts"] == 6
    assert status["ledger_sequence"] == 7
    assert status["completed_round_trips"] == 3
    assert status["market_events"] == 20
    assert status["reason"] == "source_exhausted"
    assert engine.reconcile() == "match"


@pytest.mark.parametrize(
    "fail_kind,observed_fills",
    [
        (AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION, 0),
        (AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME, 0),
        (AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME, 0),
    ],
)
def test_audit_failure_cuts_off_risk_and_keeps_acknowledged_cash(
    tmp_path: Path,
    fail_kind: AuditRecordKind,
    observed_fills: int,
) -> None:
    engine, clock = session(tmp_path, fail_kind=fail_kind)
    with pytest.raises(OSError, match="audit unavailable"):
        drive(engine, clock)
    assert engine.status()["cash"] == "1000"
    assert engine.status()["fills"] == observed_fills
    assert engine.status()["reason"] == "callback_error"
    if fail_kind is AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION:
        assert not engine.broker._records


@pytest.mark.parametrize(
    "kind,internal_sequence",
    [
        (AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME, 1),
        (AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME, 2),
    ],
)
def test_observed_fill_without_final_audit_ack_is_not_published_as_cash(
    tmp_path: Path,
    kind: AuditRecordKind,
    internal_sequence: int,
) -> None:
    engine, clock = session(tmp_path, fail_kind=kind, fail_on_fill=True)
    with pytest.raises(OSError, match="audit unavailable"):
        drive(engine, clock)
    status = engine.status()
    assert status["observed_fills"] == 1
    assert status["fills"] == 0
    assert status["cash"] == "1000"
    assert status["ledger_sequence"] == 1
    assert status["internal_ledger_sequence"] == internal_sequence
    assert status["pending_facts"] == 1
    assert status["incomplete"] is True
    assert len(engine.broker._records) == 1


def test_new_entry_uses_current_cash_after_realized_loss(tmp_path: Path) -> None:
    engine, clock = session(tmp_path, cash="202.2")
    with pytest.raises(ValueError, match="current cash/notional"):
        drive(engine, clock)
    status = engine.status()
    # Buy consumes 202.20, sell returns 197.80. A second 202.20 buy must stop.
    assert status["cash"] == "197.8"
    assert status["position"] == "0"
    assert status["fills"] == 2
    assert len(engine.broker._records) == 2
    assert status["reason"] == "callback_error"


def test_stop_arriving_during_pre_effect_fsync_prevents_transport(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    stop = False
    append = engine.audit.append

    def stop_after_ack(**kwargs: Any) -> Any:
        nonlocal stop
        result = append(**kwargs)
        if kwargs["record_kind"] is AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION:
            stop = True
        return result

    engine.audit.append = stop_after_ack  # type: ignore[method-assign]
    engine.stop_requested = lambda: stop
    drive(engine, clock)
    assert engine.status()["reason"] == "stop_requested"
    assert engine.status()["orders"] == 0
    assert not engine.broker._records


def test_every_dispatch_stays_inside_the_checked_four_frame_market_bound(tmp_path: Path) -> None:
    """The capacity envelope is funded per dispatch, never from an average."""
    engine, clock = session(tmp_path)
    durable: dict[int, int] = {}
    markets: list[int] = []
    append = engine.audit.append
    on_market = engine.on_market

    def counting_append(**kwargs: Any) -> Any:
        audit = cast(_DemoAudit, engine.audit)
        before = len(audit.records)
        result = append(**kwargs)
        sequence = engine.runtime.status().dispatch_sequence
        durable[sequence] = durable.get(sequence, 0) + len(audit.records) - before
        return result

    def recording_market(root: Any, sequence: int) -> None:
        markets.append(sequence)
        on_market(root, sequence)

    engine.audit.append = counting_append  # type: ignore[method-assign]
    engine.on_market = recording_market  # type: ignore[method-assign]
    drive(engine, clock)
    status = engine.status()
    assert status["market_events"] == 20
    assert len(markets) == status["market_events"]
    assert max(durable.values()) == PAPER_MARKET_AUDIT_FRAMES
    orders = 2 * engine.max_round_trips
    assert (
        sorted(durable[sequence] for sequence in markets)
        == [1] * (len(markets) - orders) + [PAPER_MARKET_AUDIT_FRAMES] * orders
    )
    fact_sequences = [sequence for sequence in durable if sequence not in set(markets)]
    # Every fact dispatch closes in exactly one budgeted four-frame cascade: the
    # acknowledgement, the trade itself and the broker query child of that trade.
    # The query child is economically a duplicate (no Fill and no ledger effect)
    # yet it is a distinct retained record set, so it still costs its own frames.
    assert len(fact_sequences) == 3 * orders
    assert sum(durable[sequence] for sequence in fact_sequences) == (
        PAPER_MARKET_AUDIT_FRAMES * 3 * orders
    )
    assert {durable[sequence] for sequence in fact_sequences} == {PAPER_MARKET_AUDIT_FRAMES}
    assert status["duplicate_facts"] == orders


def _limits(**overrides: Any) -> OperationalSafetyLimits:
    values: dict[str, Any] = {
        "max_market_age_seconds": 5.0,
        "max_daily_loss": Decimal("1000"),
        "max_total_exposure": Decimal("1000"),
        "max_open_orders": 2,
        "max_order_rate": 20,
        "order_rate_window_seconds": 60.0,
        "max_price_deviation_bps": 250,
    }
    values.update(overrides)
    return OperationalSafetyLimits(**values)


@pytest.mark.parametrize(
    "limits,verdict,fill_cap",
    [
        (_limits(max_total_exposure=Decimal("1")), "deny", 0),
        (_limits(max_daily_loss=Decimal("0.01")), "halt", 6),
    ],
)
def test_non_allow_safety_markets_stay_inside_the_four_frame_bound(
    tmp_path: Path,
    limits: OperationalSafetyLimits,
    verdict: str,
    fill_cap: int,
) -> None:
    """A denied or halted market is still one budgeted four-frame dispatch."""
    engine, clock = session(tmp_path, operational_limits=limits)
    durable: dict[int, int] = {}
    markets: list[int] = []
    append = engine.audit.append
    on_market = engine.on_market

    def counting_append(**kwargs: Any) -> Any:
        audit = cast(_DemoAudit, engine.audit)
        before = len(audit.records)
        result = append(**kwargs)
        sequence = engine.runtime.status().dispatch_sequence
        durable[sequence] = durable.get(sequence, 0) + len(audit.records) - before
        return result

    def recording_market(root: Any, sequence: int) -> None:
        markets.append(sequence)
        on_market(root, sequence)

    engine.audit.append = counting_append  # type: ignore[method-assign]
    engine.on_market = recording_market  # type: ignore[method-assign]
    drive(engine, clock)
    status = engine.status()
    safety = [
        json.loads(record.canonical_payload)
        for record in cast(_DemoAudit, engine.audit).records
        if record.record_kind is AuditRecordKind.PAPER_OPERATIONAL_SAFETY
    ]
    # The non-ALLOW branch really ran, and no market or fact dispatch ever
    # exceeded the four-frame budget. A DENY refuses only that one effect and
    # keeps streaming, while a HALT engages the authoritative safety halt and
    # stops the runtime, so its market count is bounded rather than fixed.
    assert safety and {payload["verdict"] for payload in safety} == {verdict}
    assert len(markets) == status["market_events"]
    assert 1 <= status["market_events"] <= 20
    assert status["risk_halted"] is (verdict == "halt")
    assert status["fills"] <= fill_cap
    assert max(durable.values()) <= PAPER_MARKET_AUDIT_FRAMES
    assert all(durable[sequence] <= PAPER_MARKET_AUDIT_FRAMES for sequence in markets)


def test_trade_query_duplicate_reuses_the_trade_fact_without_new_effects(tmp_path: Path) -> None:
    """A query child carries the trade dedup identity and cannot recurse."""
    engine, clock = session(tmp_path)
    drive(engine, clock)
    dispatches: dict[str, list[dict[str, Any]]] = {}
    for record in cast(_DemoAudit, engine.audit).records:
        if record.record_kind is not AuditRecordKind.PAPER_FACT_DISPATCH:
            continue
        document = json.loads(record.canonical_payload)
        dispatches.setdefault(document["ingress"]["fact"]["fact_sha256"], []).append(document)
    status = engine.status()
    assert max(len(group) for group in dispatches.values()) == 2
    revisited = [group for group in dispatches.values() if len(group) == 2]
    assert len(revisited) == status["fills"] == 6
    assert status["duplicate_facts"] == len(revisited)
    assert status["observed_fills"] == status["fills"]
    assert status["ledger_sequence"] == 7
    # The child is dispatched once as the identical retained fact, so the ledger
    # and the retained Fill set hold exactly one effect per order.
    for group in revisited:
        first, second = (entry["ingress"]["fact"] for entry in group)
        assert first["fact_sha256"] == second["fact_sha256"]
        assert first["dedup_identity"] == second["dedup_identity"]
        assert group[0]["dispatch_sequence"] != group[1]["dispatch_sequence"]
