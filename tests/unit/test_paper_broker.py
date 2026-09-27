from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ea.core import (
    CanonicalDecimal,
    ExecutionFactAction,
    ExecutionFactKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    InitialFunding,
    InstrumentRiskLimit,
    OrderProjectionState,
    OrderSide,
    OutcomeCode,
    RiskPolicyId,
    Sha256Digest,
    TargetLineageRef,
    TradeFactPayload,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    create_order_intent,
    create_phase1_risk_policy,
    prepare_bounded_runtime_roots,
)
from ea.core.commission import execution_latency_policy_identity
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import ExecutionFactIngress
from ea.core.market_data import Adjustment, Bar, MarketDataEnvelope, SourceId
from ea.execution import (
    Phase1ExecutionFactAuthority,
    Phase1OrderAuthority,
    create_phase1_execution_fact_authority,
    create_phase1_order_authority,
)
from ea.execution.order_lifecycle import (
    CancellationAttemptState,
    OrderCommandTracker,
    SubmissionAttemptState,
    SubmitCommand,
)
from ea.execution.paper_broker import PaperBroker, PaperBrokerError, PaperSubmitResult
from ea.portfolio import create_portfolio_ledger
from ea.risk import create_phase1_risk_authority
from ea.runtime import DeterministicRootQueue, create_deterministic_root_queue
from unit.test_execution_fact_authority import (
    INSTRUMENT,
    RUN_ID,
    SOURCE,
    TIME,
    _orders,
    _process_next,
    _snapshot,
    _spec_set,
)


def _paper_runtime(
    spec_set: InstrumentExecutionSpecSet,
    issued: Phase1OrderAuthority,
    broker: PaperBroker,
    ingresses: tuple[ExecutionFactIngress, ...],
) -> tuple[DeterministicRootQueue, Phase1ExecutionFactAuthority]:
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots(ingresses),
        fact_issuance_verifiers=(broker,),
    )
    facts = create_phase1_execution_fact_authority(
        run_id=RUN_ID, spec_set=spec_set, order_verifier=issued, dispatch_verifier=queue
    )
    return queue, facts


def _market(sequence: int, *, seconds: float, close: float = 100.0) -> MarketDataEnvelope:
    event_time = TIME + timedelta(seconds=seconds)
    return MarketDataEnvelope(
        payload=Bar(
            instrument=INSTRUMENT,
            interval_start=event_time - timedelta(seconds=1),
            interval_end=event_time,
            adjustment=Adjustment.RAW,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=100.0,
        ),
        source=SourceId("paper.test"),
        available_at=event_time,
        source_sequence=sequence,
        revision=0,
    )


def test_paper_submit_retry_next_event_and_unique_ledger_fill() -> None:
    spec_set, issued, (order,) = _orders(quantity="2")
    tracker = OrderCommandTracker(issued)
    broker = PaperBroker(issued, source_namespace=SOURCE)
    command = tracker.begin_submit(order)
    assert command is not None
    result = broker.submit(command, submitted_at=TIME)
    assert result.state is SubmissionAttemptState.SUBMITTED
    assert result.venue_order_id is not None
    ack = result.ingresses[0]
    assert broker.has_issued_ingress(
        ingress_identity=ack.identity,
        canonical_ingress_bytes=canonical_execution_fact_ingress_bytes(ack),
        canonical_fact_bytes=canonical_execution_fact_bytes(ack.fact),
    )
    assert not broker.has_issued_ingress(
        ingress_identity=ack.identity,
        canonical_ingress_bytes=b"forged",
        canonical_fact_bytes=canonical_execution_fact_bytes(ack.fact),
    )
    assert result is broker.submit(command, submitted_at=TIME)
    assert tracker.begin_submit(order) is None
    tracker.record_submit_result(command, result.state)
    with pytest.raises(PaperBrokerError):
        broker.submit(
            replace(command, execution_request_sha256=Sha256Digest("f" * 64)), submitted_at=TIME
        )

    assert broker.on_market(_market(1, seconds=0)) == ()
    matched = broker.on_market(_market(2, seconds=1))
    assert len(matched) == 1
    assert matched[0].fact.kind is ExecutionFactKind.TRADE
    assert broker.on_market(_market(2, seconds=1)) == ()
    query = broker.query(order.client_submission_key, observed_at=TIME + timedelta(seconds=2))
    assert query.code is OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED
    assert query.ingress is not None
    assert query.ingress.fact is matched[0].fact
    assert query.ingress.identity != matched[0].identity
    queue, facts = _paper_runtime(
        spec_set, issued, broker, (*result.ingresses, *matched, query.ingress)
    )
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    ledger.apply_initial_funding(
        InitialFunding(
            RUN_ID, spec_set.require(INSTRUMENT).settlement_currency, CanonicalDecimal("1000")
        )
    )
    actions = []
    while queue.remaining:
        lease, outcome = _process_next(queue, facts)
        actions.append(outcome.action)
        if outcome.fill_id is not None:
            assert ledger.apply_fill(facts.fills[-1]).code is OutcomeCode.LEDGER_APPLIED
        queue.acknowledge(lease)
    assert actions[-1] is ExecutionFactAction.DUPLICATE
    assert len(facts.fills) == 1
    projection = facts.projection_for_order(order.order_id)
    assert projection is not None
    assert projection.projection_state is OrderProjectionState.FILLED
    assert ledger.snapshot.cash_balances[0].amount == CanonicalDecimal("800")
    assert ledger.snapshot.position_balances[0].quantity == CanonicalDecimal("2")
    assert ledger.snapshot.ledger_sequence == 2


def test_paper_cancel_request_and_query_redelivery_have_no_economic_effect() -> None:
    spec_set, issued, (order,) = _orders()
    tracker = OrderCommandTracker(issued)
    broker = PaperBroker(issued, source_namespace=SOURCE)
    command = tracker.begin_submit(order)
    assert command is not None
    submitted = broker.submit(command, submitted_at=TIME)
    tracker.record_submit_result(command, submitted.state)
    before_cancel = broker.query(
        order.client_submission_key, observed_at=TIME + timedelta(seconds=1)
    )
    assert before_cancel.code is OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED
    assert before_cancel.ingress is not None

    _queue, initial_facts = _paper_runtime(spec_set, issued, broker, submitted.ingresses)
    cancel = tracker.begin_cancel(order, initial_facts)
    assert cancel is not None
    cancelled = broker.cancel(cancel, requested_at=TIME + timedelta(seconds=2))
    assert cancelled.state is CancellationAttemptState.ACCEPTED
    assert cancelled is broker.cancel(cancel, requested_at=TIME + timedelta(seconds=2))
    tracker.record_cancel_result(cancel, cancelled.state)
    assert broker.on_market(_market(3, seconds=3)) == ()
    after_cancel = broker.query(
        order.client_submission_key, observed_at=TIME + timedelta(seconds=4)
    )
    assert after_cancel.code is OutcomeCode.ORDER_CANCELLED
    assert after_cancel.ingress is not None
    assert after_cancel.ingress.fact is cancelled.ingresses[0].fact
    queue, facts = _paper_runtime(
        spec_set,
        issued,
        broker,
        (*submitted.ingresses, before_cancel.ingress, *cancelled.ingresses, after_cancel.ingress),
    )
    actions = []
    while queue.remaining:
        lease, outcome = _process_next(queue, facts)
        actions.append(outcome.action)
        queue.acknowledge(lease)
    assert actions[-1] is ExecutionFactAction.DUPLICATE
    assert facts.fills == ()
    projection = facts.projection_for_order(order.order_id)
    assert projection is not None
    assert projection.projection_state is OrderProjectionState.CANCELLED


class _TestTransport:
    """Inject outcomes at the port boundary while retaining real Paper effects."""

    def __init__(self, broker: PaperBroker) -> None:
        self.broker = broker

    def refuse_before_effect(
        self, _command: SubmitCommand, *, submitted_at: datetime
    ) -> PaperSubmitResult:
        assert submitted_at == TIME
        return PaperSubmitResult(SubmissionAttemptState.DEFINITELY_NOT_SUBMITTED, None, ())

    def lose_reply_after_effect(
        self, command: SubmitCommand, *, submitted_at: datetime
    ) -> PaperSubmitResult:
        self.broker.submit(command, submitted_at=submitted_at)
        return PaperSubmitResult(SubmissionAttemptState.UNCERTAIN, None, ())


def test_paper_unknown_history_and_transport_outcomes_do_not_allow_resubmit() -> None:
    _spec, issued, orders = _orders(2)
    refused_order, order = orders
    tracker = OrderCommandTracker(issued)
    broker = PaperBroker(issued, source_namespace=SOURCE)
    transport = _TestTransport(broker)
    refused_command = tracker.begin_submit(refused_order)
    assert refused_command is not None
    refusal = transport.refuse_before_effect(refused_command, submitted_at=TIME)
    assert refusal.state is SubmissionAttemptState.DEFINITELY_NOT_SUBMITTED
    tracker.record_submit_result(refused_command, refusal.state)
    assert tracker.begin_submit(refused_order) is None
    assert broker.query(refused_order.client_submission_key, observed_at=TIME).code is (
        OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN
    )
    assert broker.query(order.client_submission_key, observed_at=TIME).code is (
        OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN
    )
    command = tracker.begin_submit(order)
    assert command is not None
    dropped = transport.lose_reply_after_effect(command, submitted_at=TIME)
    assert dropped.state is SubmissionAttemptState.UNCERTAIN
    tracker.record_submit_result(command, dropped.state)
    assert tracker.begin_submit(order) is None
    query = broker.query(order.client_submission_key, observed_at=TIME + timedelta(seconds=1))
    assert query.code is OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED
    assert query.ingress is not None
    assert broker.query(Sha256Digest("a" * 64), observed_at=TIME).code is (
        OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN
    )


def test_paper_retained_source_proof_is_bounded_without_eviction() -> None:
    _spec, issued, (first, second) = _orders(2)
    tracker = OrderCommandTracker(issued)
    broker = PaperBroker(issued, source_namespace=SOURCE, max_orders=1)
    first_command = tracker.begin_submit(first)
    second_command = tracker.begin_submit(second)
    assert first_command is not None and second_command is not None
    submitted = broker.submit(first_command, submitted_at=TIME)
    rejected = broker.submit(second_command, submitted_at=TIME)
    assert rejected.state is SubmissionAttemptState.DEFINITELY_NOT_SUBMITTED
    assert rejected.ingresses == ()
    assert broker.submit(second_command, submitted_at=TIME) == rejected
    for seconds in range(1, 8):
        observation = broker.query(
            first.client_submission_key, observed_at=TIME + timedelta(seconds=seconds)
        )
        assert observation.ingress is not None
    with pytest.raises(PaperBrokerError) as error:
        broker.query(first.client_submission_key, observed_at=TIME + timedelta(seconds=8))
    assert error.value.code is OutcomeCode.OUT_OF_RANGE
    ack = submitted.ingresses[0]
    assert broker.has_issued_ingress(
        ingress_identity=ack.identity,
        canonical_ingress_bytes=canonical_execution_fact_ingress_bytes(ack),
        canonical_fact_bytes=canonical_execution_fact_bytes(ack.fact),
    )


def test_paper_latency_slippage_and_commission_follow_existing_policy() -> None:
    spec_set = _spec_set()
    policy_id, policy_sha = execution_latency_policy_identity(
        500, CanonicalDecimal("100"), CanonicalDecimal("100")
    )
    policy = ExecutionPolicyRef(ExecutionPolicyId(policy_id), Sha256Digest(policy_sha))
    risk_policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("paper.risk.v1"),
        spec_set=spec_set,
        execution_policy=policy,
        instrument_limits=(
            InstrumentRiskLimit(
                instrument=INSTRUMENT,
                maximum_order_quantity=CanonicalDecimal("10"),
                maximum_absolute_position=CanonicalDecimal("100"),
            ),
        ),
    )
    risk = create_phase1_risk_authority(
        run_id=RUN_ID, spec_set=spec_set, execution_policy=policy, policy=risk_policy
    )
    issued = create_phase1_order_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=policy,
        risk_policy=risk_policy,
        risk_result_verifier=risk,
    )
    intent = create_order_intent(
        run_id=RUN_ID,
        intent_id=EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 1),
        correlation_id=EconomicId(RUN_ID, EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        target_lineage=TargetLineageRef(
            EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_TARGET, 1), Sha256Digest("1" * 64)
        ),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal("2"),
        portfolio_snapshot_version=0,
        causal_root_available_at=TIME,
        dispatch_sequence=1,
        spec_set=spec_set,
        execution_policy=policy,
    )
    order = issued.create_order(intent, risk.evaluate(intent, _snapshot(spec_set)))
    tracker = OrderCommandTracker(issued)
    command = tracker.begin_submit(order)
    assert command is not None
    broker = PaperBroker(issued, source_namespace=SOURCE)
    submitted = broker.submit(command, submitted_at=TIME)
    assert broker.on_market(_market(1, seconds=0.5)) == ()
    matched = broker.on_market(_market(2, seconds=1))
    assert len(matched) == 1
    payload = matched[0].fact.payload
    assert type(payload) is TradeFactPayload
    assert payload.price == CanonicalDecimal("101")
    assert payload.fees[0].amount == CanonicalDecimal("2.02")
    queue, facts = _paper_runtime(spec_set, issued, broker, (*submitted.ingresses, *matched))
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    ledger.apply_initial_funding(
        InitialFunding(
            RUN_ID, spec_set.require(INSTRUMENT).settlement_currency, CanonicalDecimal("1000")
        )
    )
    while queue.remaining:
        lease, outcome = _process_next(queue, facts)
        if outcome.fill_id is not None:
            assert ledger.apply_fill(facts.fills[-1]).code is OutcomeCode.LEDGER_APPLIED
        queue.acknowledge(lease)
    assert ledger.snapshot.cash_balances[0].amount == CanonicalDecimal("795.98")
    assert ledger.snapshot.position_balances[0].quantity == CanonicalDecimal("2")
