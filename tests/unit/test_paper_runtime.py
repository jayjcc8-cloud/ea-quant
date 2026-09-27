from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.core import AuditRecordKind, RunBinding, RunReference, Sha256Digest
from ea.product import load_backtest_scenario
from ea.product.market_stream import LocalSimulatedMarketSource, run_local_market_stream
from ea.product.offline_demo import _DemoAudit
from ea.product.paper import PaperTradingSession
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
