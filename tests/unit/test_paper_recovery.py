"""Restart-visible outbound intent classification tests (PPV-12 core)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ea.core import (
    AuditRecord,
    AuditRecordKind,
    AuditSubjectKind,
    RunBinding,
    RunId,
    RunReference,
    Sha256Digest,
    audit_subject_digest,
    create_audit_record,
)
from ea.core.execution_messages import ExecutionFactKind, order_digest
from ea.core.ledger_integration import portfolio_risk_refresh_digest
from ea.core.portfolio import portfolio_snapshot_digest
from ea.execution import create_phase1_order_authority
from ea.product import load_backtest_scenario
from ea.product.market_stream import LocalSimulatedMarketSource
from ea.product.offline_demo import _DemoAudit
from ea.product.paper import PaperTradingSession
from ea.product.paper_recovery import (
    PaperOutboundClassification,
    PaperTerminalState,
    reconcile_paper_recovery,
    recover_paper_broker,
    recover_paper_broker_observation,
    recover_paper_continuation,
    recover_paper_economic_state,
    recover_paper_fact_authority,
    recover_paper_fills,
    recover_paper_order_contexts,
    recover_paper_orders,
    recover_paper_risk_refreshes,
    scan_paper_outbound_intents,
)
from ea.reconciliation.broker import (
    BrokerObservedOrderState,
    BrokerOrderObservation,
)
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_paper_runtime import drive, session
from unit.test_streaming import SOURCE, ControlledClock

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")


def _binding() -> RunBinding:
    return RunBinding(RunReference(RUN_ID, Sha256Digest("11" * 32)), Sha256Digest("22" * 32))


def _result_record(sequence: int, state: str) -> AuditRecord:
    payload = json.dumps(
        {
            "schema": "ea.audit-paper-submission-result.v1",
            "canonicalization": "ea-canonical-json-v1",
            "run_id": RUN_ID.value,
            "dispatch_sequence": sequence,
            "order_id": {
                "owner_kind": "execution_order",
                "owner_sequence": sequence,
                "run_id": RUN_ID.value,
            },
            "client_submission_key": "1" * 64,
            "execution_request_sha256": "2" * 64,
            "submitted_at": "2026-01-01T00:00:00.000000Z",
            "submission_state": state,
            "venue_order_id": None,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return create_audit_record(
        binding=_binding(),
        owner_sequence=sequence + 1,
        record_kind=AuditRecordKind.PAPER_SUBMISSION_RESULT,
        subject_kind=AuditSubjectKind.PAPER_SUBMISSION_RESULT,
        subject_sha256=audit_subject_digest(AuditRecordKind.PAPER_SUBMISSION_RESULT, payload),
        canonical_payload=payload,
        previous_record_sha256=Sha256Digest("3" * 64),
        previous_chain_head_sha256=Sha256Digest("4" * 64),
    )


def test_empty_stream_has_no_intents() -> None:
    scan = scan_paper_outbound_intents(())
    assert scan.intents == ()
    assert scan.incomplete == ()
    assert scan.reconciliation_required is False


def test_uncertain_submission_result_is_unknown_and_incomplete() -> None:
    scan = scan_paper_outbound_intents((_result_record(1, "uncertain"),))
    assert scan.reconciliation_required is True
    assert scan.intents[0].classification is PaperOutboundClassification.UNKNOWN
    assert scan.incomplete == scan.intents


def test_definitely_not_submitted_is_not_incomplete() -> None:
    scan = scan_paper_outbound_intents((_result_record(1, "definitely_not_submitted"),))
    assert scan.intents[0].classification is PaperOutboundClassification.DEFINITELY_NOT_SENT
    assert scan.incomplete == ()
    assert scan.reconciliation_required is False


def test_submitted_without_terminal_is_incomplete() -> None:
    scan = scan_paper_outbound_intents((_result_record(1, "submitted"),))
    assert scan.intents[0].classification is PaperOutboundClassification.SENT_CONFIRMED
    assert scan.intents[0].terminal is None
    assert scan.reconciliation_required is True


def test_committed_run_is_sent_confirmed_and_complete(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    scan = scan_paper_outbound_intents(engine.audit.records)
    assert len(scan.intents) == 6
    assert all(
        intent.classification is PaperOutboundClassification.SENT_CONFIRMED
        and intent.terminal is PaperTerminalState.FILLED
        for intent in scan.intents
    )
    assert scan.reconciliation_required is False


def test_intent_without_result_or_fact_is_unknown_and_incomplete(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    authorizations = [
        record
        for record in engine.audit.records
        if record.record_kind is AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION
    ]
    # An authorization record with no result and no fact mirrors a crash after
    # intent but before any transport classification was durably retained.
    scan = scan_paper_outbound_intents((authorizations[0],))
    assert scan.reconciliation_required is True
    assert scan.intents[0].classification is PaperOutboundClassification.UNKNOWN


def test_joint_acceptance_recovers_a_committed_run_exactly_once(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    scan = scan_paper_outbound_intents(records)
    broker_observations = recover_paper_broker_observation(records)
    joint = reconcile_paper_recovery(scan, broker_observations)
    assert joint.recovered is True
    assert joint.exactly_once is True
    assert joint.recovered_orders == 6
    assert joint.recovered_fills == 6
    assert joint.open_orders == ()
    assert joint.not_sent_orders == ()
    assert joint.divergent_orders == ()
    assert joint.reason is None


def test_joint_acceptance_keeps_an_open_submitted_order_for_continuation() -> None:
    # Crash after broker accepted but before the local ACK: a durable "submitted"
    # result with no terminal fact. The order is open at the broker, never resent.
    records = (_result_record(1, "submitted"),)
    scan = scan_paper_outbound_intents(records)
    joint = reconcile_paper_recovery(scan, recover_paper_broker_observation(records))
    assert joint.recovered is True
    assert joint.open_orders == (1,)
    assert joint.recovered_fills == 0
    assert joint.divergent_orders == ()


def test_joint_acceptance_resolves_no_broker_effect_to_not_sent() -> None:
    records = (_result_record(1, "uncertain"),)
    scan = scan_paper_outbound_intents(records)
    joint = reconcile_paper_recovery(scan, recover_paper_broker_observation(records))
    assert joint.recovered is True
    assert joint.not_sent_orders == (1,)
    assert joint.recovered_fills == 0


def test_joint_acceptance_detects_broker_divergence(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    scan = scan_paper_outbound_intents(records)
    # Simulate an external broker that lost every fill: local knows six Fills,
    # the broker reports no order. This must halt, not silently recover.
    broker_observations = {
        sequence: BrokerOrderObservation(
            Sha256Digest("1" * 64), BrokerObservedOrderState.UNKNOWN, None
        )
        for sequence in range(1, 7)
    }
    joint = reconcile_paper_recovery(scan, broker_observations)
    assert joint.recovered is False
    assert joint.divergent_orders == tuple(range(1, 7))
    assert joint.reason is not None


def test_continuation_reconstructs_economic_state_exactly_once(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    state = recover_paper_continuation(
        records,
        spec_set=engine.scenario.spec_set,
        funding=engine.funding,
        instrument=engine.scenario.instrument,
    )
    assert state.fills == 6
    assert state.round_trips == 3
    assert state.cash_text == "986.8"
    assert state.position_quantity_text == "0"
    assert state.open_orders == ()
    assert state.exactly_once is True


def test_recover_paper_orders_reconstructs_issued_orders(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    recovered = recover_paper_orders(
        records,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
    )
    assert len(recovered) == len(engine.issued)
    for original, reconstructed in zip(engine.issued, recovered, strict=True):
        assert order_digest(reconstructed) == order_digest(original)


def test_recover_paper_order_contexts_reissues_orders(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    policy = engine.gate.risk_authority.policy
    contexts = recover_paper_order_contexts(
        records,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=policy,
    )
    authority = create_phase1_order_authority(
        run_id=engine.run_id,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=policy,
        risk_result_verifier=engine.gate.risk_authority,
    )
    reissued = [authority.create_order(intent, result) for intent, result in contexts]
    assert len(reissued) == len(engine.issued)
    for original, rebuilt in zip(engine.issued, reissued, strict=True):
        assert order_digest(rebuilt) == order_digest(original)


def _crash_session(
    tmp_path: Path,
    *,
    crash_after: AuditRecordKind,
) -> tuple[PaperTradingSession, ControlledClock]:
    path = bounded_scenario(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["funding"]["initial_cash"] = "1000"
    document["risk"]["max_notional"] = "1000"
    document["execution"]["slippage"] = {
        "policy": "deterministic-slippage-v1",
        "slippage_bps": "100",
    }
    path.write_text(yaml.safe_dump(document))
    scenario = load_backtest_scenario(path)
    binding = RunBinding(RunReference(RUN_ID, Sha256Digest("1" * 64)), Sha256Digest("2" * 64))
    clock = ControlledClock()
    engine = PaperTradingSession(
        scenario,
        binding=binding,
        audit=_DemoAudit(binding, scenario.spec_set),
        clock=clock,
        monotonic=clock.monotonic,
        source_id=SOURCE,
        prices=(100.0,),
        stop_requested=lambda: False,
        crash_after=crash_after,
    )
    return engine, clock


def _recover_broker(engine: PaperTradingSession, records: tuple[AuditRecord, ...]) -> object:
    policy = engine.gate.risk_authority.policy
    contexts = recover_paper_order_contexts(
        records,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=policy,
    )
    authority = create_phase1_order_authority(
        run_id=engine.run_id,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=policy,
        risk_result_verifier=engine.gate.risk_authority,
    )
    for intent, result in contexts:
        authority.create_order(intent, result)
    return recover_paper_broker(
        records, orders=authority, max_orders=2 * engine.max_round_trips
    )


def test_recover_paper_broker_pins_filled_orders_and_round_trips_submitted_at(
    tmp_path: Path,
) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    recovery = _recover_broker(engine, records)
    assert len(recovery.filled_orders) == 6
    assert recovery.open_orders == ()
    assert recovery.not_sent_orders == ()
    assert len(recovery.broker._records) == 6
    for key, original in engine.broker._records.items():
        rebuilt = recovery.broker._records[key]
        assert rebuilt.submitted_at == original.submitted_at
        assert rebuilt.venue_order_id == original.venue_order_id
        assert rebuilt.state.value == "filled"
    # A fresh eligible market event must not re-match any already-filled Order.
    source = LocalSimulatedMarketSource(
        instrument=engine.scenario.instrument,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=None,
        bar_seconds=1.0,
        event_limit=1,
    )
    event = source.next_event(clock.now())
    assert event is not None
    assert recovery.broker.on_market(event) == ()


def test_recover_paper_broker_keeps_open_order_for_continuation(tmp_path: Path) -> None:
    engine, clock = _crash_session(tmp_path, crash_after=AuditRecordKind.PAPER_SUBMISSION_RESULT)
    with pytest.raises(RuntimeError, match="crash injection"):
        drive(engine, clock)
    records = tuple(engine.audit.records)
    recovery = _recover_broker(engine, records)
    assert len(recovery.open_orders) == 1
    assert recovery.filled_orders == ()
    assert recovery.not_sent_orders == ()
    assert len(recovery.broker._records) == 1
    (record,) = recovery.broker._records.values()
    assert record.state.value == "open"
    # The open Order still matches at the next eligible market event.
    clock.advance(1.0)
    source = LocalSimulatedMarketSource(
        instrument=engine.scenario.instrument,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=None,
        bar_seconds=1.0,
        event_limit=1,
    )
    event = source.next_event(clock.now())
    assert event is not None
    ingresses = recovery.broker.on_market(event)
    assert len(ingresses) == 1
    assert ingresses[0].fact.kind is ExecutionFactKind.TRADE


def test_recover_paper_risk_refreshes_decodes_acknowledged_chain(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    refreshes = recover_paper_risk_refreshes(tuple(engine.audit.records))
    assert len(refreshes) > 0
    assert refreshes[0].previous_refresh_sha256 is None
    for earlier, later in zip(refreshes, refreshes[1:], strict=False):
        assert later.previous_refresh_sha256 == portfolio_risk_refresh_digest(earlier)


def test_recover_paper_fact_authority_replays_exact_fills(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    contexts = recover_paper_order_contexts(
        records,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=engine.gate.risk_authority.policy,
    )
    authority = create_phase1_order_authority(
        run_id=engine.run_id,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=engine.gate.risk_authority.policy,
        risk_result_verifier=engine.gate.risk_authority,
    )
    for intent, result in contexts:
        authority.create_order(intent, result)
    facts = recover_paper_fact_authority(records, orders=authority)
    fills = recover_paper_fills(
        records, spec_set=engine.scenario.spec_set, funding=engine.funding
    )
    assert len(facts.fills) == 6
    assert [f.fill_id.owner_sequence for f in facts.fills] == [
        f.fill_id.owner_sequence for f in fills
    ]


def test_recover_paper_economic_state_reconstructs_acknowledged_ledger(
    tmp_path: Path,
) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    records = tuple(engine.audit.records)
    economic = recover_paper_economic_state(
        records,
        run_id=engine.run_id,
        spec_set=engine.scenario.spec_set,
        execution_policy=engine.scenario.execution_policy,
        risk_policy=engine.gate.risk_authority.policy,
        funding=engine.funding,
    )
    cash = next(
        balance.amount for balance in economic.ledger.snapshot.cash_balances
    )
    assert cash.text == "986.8"
    assert economic.final_refresh is not None
    assert (
        portfolio_snapshot_digest(economic.ledger.snapshot)
        == economic.final_refresh.portfolio_snapshot_sha256
    )
    assert len(economic.fills) == 6
