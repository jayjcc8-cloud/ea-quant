from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactKind,
    ExecutionMessageError,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    ExternalFactId,
    FactDedupKey,
    FactProvenance,
    FactProvenanceId,
    FeeCode,
    FeeEntry,
    IndependentFactDecodeContext,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderKind,
    OrderSide,
    OutcomeCode,
    PriceDomain,
    RiskDecisionKind,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    SourceNativeSequence,
    SubmissionQueryFactDecodeContext,
    TargetLineageRef,
    TimeInForce,
    VenueId,
    VenueOrderId,
    allow_order_intent,
    build_instrument_spec_set,
    canonical_effective_order_intent_bytes,
    canonical_execution_approval_bytes,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_execution_fact_preimage_bytes,
    canonical_execution_request_bytes,
    canonical_fill_bytes,
    canonical_order_bytes,
    canonical_order_intent_bytes,
    canonical_risk_decision_bytes,
    classify_identity_replay,
    create_execution_fact_ingress,
    create_fill,
    create_lifecycle_execution_fact,
    create_order,
    create_order_intent,
    create_submission_query_execution_fact,
    create_trade_execution_fact,
    decode_execution_approval,
    decode_execution_fact,
    decode_execution_fact_ingress,
    decode_fill,
    decode_order,
    decode_order_intent,
    decode_risk_decision,
    effective_order_intent_digest,
    execution_approval_digest,
    execution_fact_digest,
    execution_fact_ingress_digest,
    execution_request_digest,
    fail_order_intent_evaluation,
    fill_digest,
    instrument_spec_set_digest,
    order_client_submission_key,
    order_digest,
    order_intent_digest,
    reject_order_intent,
    resize_order_intent,
    risk_decision_digest,
)
from ea.core.execution_messages import ExecutionFact, Order, OrderIntent, RiskDecision

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
SPEC = InstrumentExecutionSpec(
    instrument=INSTRUMENT,
    specification_id=InstrumentSpecId("xnas.aapl.v1"),
    price_quantum=CanonicalDecimal("0.01"),
    quantity_quantum=CanonicalDecimal("1"),
    settlement_currency=SettlementCurrency("USD"),
    currency_quantum=CanonicalDecimal("0.01"),
    contract_multiplier=CanonicalDecimal("1"),
    price_domain=PriceDomain.POSITIVE,
)
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.us-equities.v1"),
    [SPEC],
)
TARGET = TargetLineageRef(
    EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_TARGET, 2),
    Sha256Digest("1" * 64),
)
POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.next-bar-close.v1"),
    Sha256Digest("2" * 64),
)
CAUSAL_TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
SOURCE = SourceNamespace("sim.primary")
PROVENANCE = FactProvenance(
    FactProvenanceId("phase1.simulator.v1"),
    Sha256Digest("3" * 64),
)


def _id(kind: EconomicOwnerKind, sequence: int) -> EconomicId:
    return EconomicId(RUN_ID, kind, sequence)


def _intent(*, quantity: str = "10", dispatch_sequence: int = 7) -> OrderIntent:
    return create_order_intent(
        run_id=RUN_ID,
        intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, 3),
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        target_lineage=TARGET,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(quantity),
        portfolio_snapshot_version=5,
        causal_root_available_at=CAUSAL_TIME,
        dispatch_sequence=dispatch_sequence,
        spec_set=SPEC_SET,
        execution_policy=POLICY,
    )


def _allow(intent: OrderIntent | None = None) -> RiskDecision:
    selected = _intent() if intent is None else intent
    return allow_order_intent(
        decision_id=_id(EconomicOwnerKind.RISK_DECISION, 4),
        approval_id=_id(EconomicOwnerKind.RISK_APPROVAL, 5),
        intent=selected,
        spec_set=SPEC_SET,
        risk_state_version=11,
    )


def _order(
    intent: OrderIntent | None = None,
    decision: RiskDecision | None = None,
) -> Order:
    selected_intent = _intent() if intent is None else intent
    selected_decision = _allow(selected_intent) if decision is None else decision
    return create_order(
        order_id=_id(EconomicOwnerKind.EXECUTION_ORDER, 6),
        intent=selected_intent,
        decision=selected_decision,
        spec_set=SPEC_SET,
    )


def _trade_fact(*, resolved: bool = True) -> ExecutionFact:
    order = _order()
    return create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("trade-42"),
        occurred_at=CAUSAL_TIME + timedelta(minutes=1),
        provenance=PROVENANCE,
        spec_set=SPEC_SET,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal("10"),
        price=CanonicalDecimal("101.25"),
        client_submission_key=order.client_submission_key if resolved else None,
        venue_order_id=VenueOrderId("venue-order-7") if resolved else None,
        order_id=order.order_id if resolved else None,
        correlation_id=order.correlation_id if resolved else None,
        causation_id=order.order_id if resolved else None,
    )


def _json_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _assert_message_code(
    error: pytest.ExceptionInfo[ExecutionMessageError],
    code: OutcomeCode,
) -> None:
    assert error.value.code is code


def test_order_side_compatibility_alias_is_single_canonical_enum() -> None:
    from ea.core.models import OrderSide as CompatibilityOrderSide

    assert CompatibilityOrderSide is OrderSide
    assert tuple(OrderSide) == (OrderSide.BUY, OrderSide.SELL)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ExecutionMessageError(OutcomeCode.RISK_ALLOWED, "not an error"),
        lambda: ExecutionPolicyId(cast(str, 1)),
        lambda: ExecutionPolicyId("Upper"),
        lambda: VenueOrderId(cast(str, 1)),
        lambda: VenueOrderId(""),
        lambda: TargetLineageRef(TARGET.target_id, cast(Sha256Digest, "bad")),
        lambda: ExecutionPolicyRef(cast(ExecutionPolicyId, "bad"), Sha256Digest("2" * 64)),
        lambda: ExecutionPolicyRef(
            ExecutionPolicyId("phase1.policy.v1"),
            cast(Sha256Digest, "bad"),
        ),
        lambda: FactProvenance(
            cast(FactProvenanceId, "bad"),
            Sha256Digest("3" * 64),
        ),
        lambda: FactProvenance(
            FactProvenanceId("phase1.provenance.v1"),
            cast(Sha256Digest, "bad"),
        ),
        lambda: FeeEntry(
            cast(FeeCode, "commission"),
            SettlementCurrency("USD"),
            CanonicalDecimal("0"),
        ),
        lambda: FeeEntry(
            FeeCode.COMMISSION,
            cast(SettlementCurrency, "USD"),
            CanonicalDecimal("0"),
        ),
        lambda: FeeEntry(
            FeeCode.COMMISSION,
            SettlementCurrency("USD"),
            cast(CanonicalDecimal, "0"),
        ),
        lambda: FeeEntry(
            FeeCode.COMMISSION,
            SettlementCurrency("USD"),
            CanonicalDecimal("0.01"),
        ),
    ],
)
def test_public_execution_message_values_reject_invalid_runtime_values(
    factory: Callable[[], object],
) -> None:
    with pytest.raises((ExecutionMessageError, TypeError)):
        factory()


def test_order_intent_is_factory_only_immutable_and_binds_causal_lineage() -> None:
    with pytest.raises(TypeError):
        OrderIntent()

    intent = _intent()
    assert intent.run_id == RUN_ID
    assert intent.causation_id == TARGET.target_id
    assert intent.correlation_id.owner_kind is EconomicOwnerKind.STRATEGY_SIGNAL
    assert intent.order_kind is OrderKind.MARKET
    assert intent.time_in_force is TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
    assert intent.price_constraint is None
    assert intent.instrument_specification_id == SPEC.specification_id
    assert intent.instrument_spec_set_id == SPEC_SET.identifier
    assert intent.instrument_spec_set_sha256 == instrument_spec_set_digest(SPEC_SET)
    assert intent.causal_root_available_at == CAUSAL_TIME
    assert intent.dispatch_sequence == 7

    with pytest.raises(FrozenInstanceError):
        intent.quantity = CanonicalDecimal("9")  # type: ignore[misc]


def test_intent_bytes_digest_and_effective_projection_are_stable() -> None:
    intent = _intent()
    intent_bytes = canonical_order_intent_bytes(intent)
    assert intent_bytes.startswith(
        b'{"canonicalization":"ea-order-intent-v1",'
        b'"causal_root_available_at":"2026-01-02T09:31:00.000000Z"'
    )
    assert b'"price_constraint":null' in intent_bytes
    assert b'"quantity":"10"' in intent_bytes
    assert order_intent_digest(intent) == order_intent_digest(intent)

    effective = canonical_effective_order_intent_bytes(
        intent,
        CanonicalDecimal("7"),
    )
    assert effective == (
        b'{"approved_quantity":"7",'
        b'"canonicalization":"ea-effective-order-intent-v1",'
        b'"intent_id":{"owner_kind":"portfolio.intent","owner_sequence":3,'
        b'"run_id":"12345678-1234-4234-8234-123456789abc"},'
        b'"original_intent_sha256":"'
        + order_intent_digest(intent).value.encode("ascii")
        + b'","projection_type":"effective_order_intent",'
        b'"run_id":"12345678-1234-4234-8234-123456789abc",'
        b'"schema_version":1}'
    )
    assert effective_order_intent_digest(intent, CanonicalDecimal("7")) != (
        effective_order_intent_digest(intent, CanonicalDecimal("8"))
    )


def test_risk_variants_have_closed_executable_states_and_causal_chain() -> None:
    intent = _intent()
    allowed = _allow(intent)
    resized = resize_order_intent(
        decision_id=_id(EconomicOwnerKind.RISK_DECISION, 8),
        approval_id=_id(EconomicOwnerKind.RISK_APPROVAL, 9),
        intent=intent,
        approved_quantity=CanonicalDecimal("7"),
        spec_set=SPEC_SET,
        risk_state_version=12,
    )
    rejected = reject_order_intent(
        decision_id=_id(EconomicOwnerKind.RISK_DECISION, 10),
        intent=intent,
        spec_set=SPEC_SET,
        risk_state_version=13,
    )
    failed = fail_order_intent_evaluation(
        decision_id=_id(EconomicOwnerKind.RISK_DECISION, 11),
        intent=intent,
        spec_set=SPEC_SET,
        risk_state_version=14,
    )

    assert allowed.kind is RiskDecisionKind.ALLOW
    assert allowed.outcome_code is OutcomeCode.RISK_ALLOWED
    assert allowed.approved_quantity == intent.quantity
    assert allowed.correlation_id == intent.correlation_id
    assert allowed.causation_id == intent.intent_id
    assert allowed.approval is not None
    assert allowed.approval.correlation_id == intent.correlation_id
    assert allowed.approval.causation_id == allowed.decision_id
    assert allowed.approval.causal_root_available_at == intent.causal_root_available_at
    assert allowed.approval.dispatch_sequence == intent.dispatch_sequence

    assert resized.kind is RiskDecisionKind.RESIZE
    assert resized.approved_quantity == CanonicalDecimal("7")
    assert resized.outcome_code is OutcomeCode.RISK_RESIZED
    for terminal, code in (
        (rejected, OutcomeCode.RISK_REJECTED),
        (failed, OutcomeCode.RISK_EVALUATION_FAILED),
    ):
        assert terminal.approval is None
        assert terminal.approved_quantity is None
        assert terminal.effective_intent_sha256 is None
        assert terminal.outcome_code is code


@pytest.mark.parametrize("quantity", ["10", "11", "0", "-1", "1.5"])
def test_resize_requires_strictly_smaller_positive_quantized_quantity(
    quantity: str,
) -> None:
    with pytest.raises((ExecutionMessageError, ValueError)):
        resize_order_intent(
            decision_id=_id(EconomicOwnerKind.RISK_DECISION, 8),
            approval_id=_id(EconomicOwnerKind.RISK_APPROVAL, 9),
            intent=_intent(),
            approved_quantity=CanonicalDecimal(quantity),
            spec_set=SPEC_SET,
            risk_state_version=1,
        )


def test_reject_and_evaluation_failure_cannot_create_order() -> None:
    intent = _intent()
    for decision in (
        reject_order_intent(
            decision_id=_id(EconomicOwnerKind.RISK_DECISION, 10),
            intent=intent,
            spec_set=SPEC_SET,
            risk_state_version=1,
        ),
        fail_order_intent_evaluation(
            decision_id=_id(EconomicOwnerKind.RISK_DECISION, 11),
            intent=intent,
            spec_set=SPEC_SET,
            risk_state_version=1,
        ),
    ):
        with pytest.raises(ExecutionMessageError) as error:
            create_order(
                order_id=_id(EconomicOwnerKind.EXECUTION_ORDER, 12),
                intent=intent,
                decision=decision,
                spec_set=SPEC_SET,
            )
        _assert_message_code(error, OutcomeCode.CONFLICTING_ID)


def test_order_copies_eligibility_and_derives_non_recursive_keys() -> None:
    intent = _intent()
    decision = _allow(intent)
    order = _order(intent, decision)

    assert order.eligible_after_available_at == intent.causal_root_available_at
    assert order.dispatch_sequence == intent.dispatch_sequence
    assert order.correlation_id == intent.correlation_id
    assert decision.approval is not None
    assert order.causation_id == decision.approval.approval_id
    assert order.client_submission_key == order_client_submission_key(order)
    assert order.client_submission_key.value.encode("ascii") not in canonical_order_bytes(order)
    assert order_digest(order) != order.client_submission_key
    request = canonical_execution_request_bytes(order)
    assert order.client_submission_key.value.encode("ascii") in request
    assert order_digest(order).value.encode("ascii") in request
    assert execution_request_digest(order) == execution_request_digest(order)


def test_lifecycle_fact_kind_and_outcome_pairing_is_closed() -> None:
    expected = {
        ExecutionFactKind.ACKNOWLEDGEMENT: OutcomeCode.ORDER_ACKNOWLEDGED,
        ExecutionFactKind.REJECTION: OutcomeCode.ORDER_REJECTED,
        ExecutionFactKind.EXPIRY: OutcomeCode.ORDER_EXPIRED,
        ExecutionFactKind.CANCELLATION: OutcomeCode.ORDER_CANCELLED,
    }
    order = _order()
    for index, (kind, code) in enumerate(expected.items()):
        fact = create_lifecycle_execution_fact(
            kind=kind,
            source_namespace=SOURCE,
            dedup_identity=ExternalFactId(f"fact-{index}"),
            occurred_at=CAUSAL_TIME + timedelta(minutes=1),
            provenance=PROVENANCE,
            instrument=INSTRUMENT,
            client_submission_key=order.client_submission_key,
            order_id=order.order_id,
            correlation_id=order.correlation_id,
            causation_id=order.order_id,
        )
        assert fact.payload.outcome_code is code  # type: ignore[union-attr]

    with pytest.raises(ExecutionMessageError):
        create_lifecycle_execution_fact(
            kind=ExecutionFactKind.TRADE,
            source_namespace=SOURCE,
            dedup_identity=ExternalFactId("not-lifecycle"),
            occurred_at=CAUSAL_TIME,
            provenance=PROVENANCE,
        )


def test_trade_fact_has_exact_zero_commission_spec_lineage_and_digest() -> None:
    fact = _trade_fact()
    assert fact.kind is ExecutionFactKind.TRADE
    assert fact.instrument == INSTRUMENT
    trade = fact.payload
    assert trade.fees[0].fee_code is FeeCode.COMMISSION  # type: ignore[union-attr]
    assert trade.fees[0].currency == SPEC.settlement_currency  # type: ignore[union-attr]
    assert trade.fees[0].amount == CanonicalDecimal("0")  # type: ignore[union-attr]
    assert fact.fact_sha256 == execution_fact_digest(fact)
    assert fact.fact_sha256.value.encode("ascii") not in (
        canonical_execution_fact_preimage_bytes(fact)
    )
    assert fact.fact_sha256.value.encode("ascii") in canonical_execution_fact_bytes(fact)


def test_submission_query_is_anchored_to_exact_subject_order() -> None:
    order = _order()
    fact = create_submission_query_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("query-1"),
        occurred_at=CAUSAL_TIME + timedelta(minutes=2),
        provenance=PROVENANCE,
        outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED,
        subject_order=order,
        venue_order_id=VenueOrderId("venue-order-7"),
    )

    assert fact.order_id == order.order_id
    assert fact.client_submission_key == order.client_submission_key
    assert fact.correlation_id == order.correlation_id
    assert fact.causation_id == order.order_id


def test_ingress_redelivery_changes_only_ingress_identity() -> None:
    fact = _trade_fact()
    first = create_execution_fact_ingress(
        available_at=fact.occurred_at,
        source_namespace=SOURCE,
        ingress_sequence=1,
        fact=fact,
    )
    redelivery = create_execution_fact_ingress(
        available_at=fact.occurred_at + timedelta(seconds=1),
        source_namespace=SOURCE,
        ingress_sequence=2,
        fact=fact,
    )

    assert first.identity != redelivery.identity
    assert first.fact.fact_sha256 == redelivery.fact.fact_sha256
    assert canonical_execution_fact_bytes(first.fact) == canonical_execution_fact_bytes(
        redelivery.fact
    )
    assert canonical_execution_fact_ingress_bytes(first) != (
        canonical_execution_fact_ingress_bytes(redelivery)
    )
    assert execution_fact_ingress_digest(first) != execution_fact_ingress_digest(redelivery)


def test_ingress_namespace_mismatch_fails_as_fact_invalid() -> None:
    with pytest.raises(ExecutionMessageError) as error:
        create_execution_fact_ingress(
            available_at=CAUSAL_TIME + timedelta(minutes=1),
            source_namespace=SourceNamespace("sim.secondary"),
            ingress_sequence=1,
            fact=_trade_fact(),
        )
    _assert_message_code(error, OutcomeCode.FACT_INVALID)


def test_fill_copies_complete_trade_and_allows_unresolved_ancestry() -> None:
    resolved_fact = _trade_fact()
    resolved = create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 12),
        fact=resolved_fact,
        spec_set=SPEC_SET,
    )
    assert resolved.fact_key == resolved_fact.dedup_key
    assert resolved.fact_sha256 == resolved_fact.fact_sha256
    assert resolved.provenance == resolved_fact.provenance
    assert resolved.order_id == resolved_fact.order_id
    assert resolved.correlation_id == resolved_fact.correlation_id
    assert resolved.fees == resolved_fact.payload.fees  # type: ignore[union-attr]
    assert fill_digest(resolved) == fill_digest(resolved)

    unresolved_fact = _trade_fact(resolved=False)
    unresolved = create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 13),
        fact=unresolved_fact,
        spec_set=SPEC_SET,
    )
    assert unresolved.order_id is None
    assert unresolved.correlation_id is None
    assert unresolved.causation_id is None
    assert unresolved.client_submission_key is None
    assert unresolved.venue_order_id is None


def test_non_trade_fact_cannot_create_fill() -> None:
    fact = create_lifecycle_execution_fact(
        kind=ExecutionFactKind.REJECTION,
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("reject-1"),
        occurred_at=CAUSAL_TIME,
        provenance=PROVENANCE,
    )
    with pytest.raises(ExecutionMessageError) as error:
        create_fill(
            fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 13),
            fact=fact,
            spec_set=SPEC_SET,
        )
    _assert_message_code(error, OutcomeCode.CONFLICTING_ID)


def test_all_specific_readers_round_trip_exact_messages() -> None:
    intent = _intent()
    decision = _allow(intent)
    assert decision.approval is not None
    order = _order(intent, decision)
    fact = _trade_fact()
    ingress = create_execution_fact_ingress(
        available_at=fact.occurred_at,
        source_namespace=SOURCE,
        ingress_sequence=2**256 + 9,
        fact=fact,
    )
    fill = create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 12),
        fact=fact,
        spec_set=SPEC_SET,
    )
    fact_context = IndependentFactDecodeContext(SPEC_SET)

    decoded_intent = decode_order_intent(
        canonical_order_intent_bytes(intent),
        spec_set=SPEC_SET,
        target_lineage=TARGET,
        execution_policy=POLICY,
    )
    decoded_decision = decode_risk_decision(
        canonical_risk_decision_bytes(decision),
        intent=decoded_intent,
        spec_set=SPEC_SET,
    )
    decoded_approval = decode_execution_approval(
        canonical_execution_approval_bytes(decision.approval),
        intent=decoded_intent,
        decision=decoded_decision,
    )
    decoded_order = decode_order(
        canonical_order_bytes(order),
        intent=decoded_intent,
        decision=decoded_decision,
        spec_set=SPEC_SET,
    )
    decoded_fact = decode_execution_fact(
        canonical_execution_fact_bytes(fact),
        context=fact_context,
    )
    decoded_ingress = decode_execution_fact_ingress(
        canonical_execution_fact_ingress_bytes(ingress),
        context=fact_context,
    )
    decoded_fill = decode_fill(
        canonical_fill_bytes(fill),
        fact=decoded_fact,
        spec_set=SPEC_SET,
    )

    assert decoded_intent == intent
    assert decoded_decision == decision
    assert decoded_approval == decision.approval
    assert decoded_order == order
    assert decoded_fact == fact
    assert decoded_ingress == ingress
    assert decoded_fill == fill


def test_complete_message_family_has_frozen_golden_vectors() -> None:
    intent = _intent()
    decision = _allow(intent)
    assert decision.approval is not None
    order = _order(intent, decision)
    fact = _trade_fact()
    ingress = create_execution_fact_ingress(
        available_at=fact.occurred_at,
        source_namespace=SOURCE,
        ingress_sequence=9,
        fact=fact,
    )
    fill = create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 12),
        fact=fact,
        spec_set=SPEC_SET,
    )
    vectors = {
        "intent": (
            canonical_order_intent_bytes(intent),
            order_intent_digest(intent),
        ),
        "effective": (
            canonical_effective_order_intent_bytes(intent, intent.quantity),
            effective_order_intent_digest(intent, intent.quantity),
        ),
        "decision": (
            canonical_risk_decision_bytes(decision),
            risk_decision_digest(decision),
        ),
        "approval": (
            canonical_execution_approval_bytes(decision.approval),
            execution_approval_digest(decision.approval),
        ),
        "order": (
            canonical_order_bytes(order),
            order_digest(order),
        ),
        "request": (
            canonical_execution_request_bytes(order),
            execution_request_digest(order),
        ),
        "fact": (
            canonical_execution_fact_bytes(fact),
            execution_fact_digest(fact),
        ),
        "ingress": (
            canonical_execution_fact_ingress_bytes(ingress),
            execution_fact_ingress_digest(ingress),
        ),
        "fill": (
            canonical_fill_bytes(fill),
            fill_digest(fill),
        ),
    }
    expected = {
        "intent": (
            1337,
            "7166ebd230f1f4471f3db178fcc54e2d487f06aed5a8d2b57d5b642e51d08d8c",
            "192c7c4e079b5df6a45123736c7708eab3fc44a8b062b61553c4e607ef25a8bb",
        ),
        "effective": (
            391,
            "fbd7fa15bf49470371919c2c1edcea42c685b7d4f1c9e46853bce1b99b4893a7",
            "7b7158c40c4959d5e72ed64343d914deaf3586c282c1064caa4cf253157d89da",
        ),
        "decision": (
            2051,
            "fcc0051c0a2c3cb826d0faa8e325b07108926a6ee4a559f305849de393f1cd91",
            "b19e5264240a9f6ff6064febba9064472b1dad02923181e7c4e8fb981f234f8c",
        ),
        "approval": (
            1061,
            "c1717bbbe7e052d6e192b3bf28e6493190d3e12ee810a1cde512035d030e2ce3",
            "c274e7afe6f2be2a3b573c0d144959aceb6cd4a0392dfa9b688e1b100cc9a03b",
        ),
        "order": (
            1827,
            "3faf7ab0baf8f3082f165b16ad794e396bc4b57f20d20292a88967561a7fd05d",
            "d041fee3fea81d69ead4db20bdbea7c52db1ed59b05c50e265536aee7ea08d70",
        ),
        "request": (
            1023,
            "7ff229496fed534a9e9c996f00d563bec0033d547a799eb7452fbbb26b670e14",
            "65c94970febc8c0df4f6bf9e7317904c7fb7aaa7ae789b0bc2a84373d38f06bf",
        ),
        "fact": (
            1319,
            "575303e59edc9ee8b9d2c77373192002021863046905cd3de2a8202453c81edf",
            "d154f9b9f1abe5b81f9f9883d95477d1d9effdf198e461475b634291b04e9062",
        ),
        "ingress": (
            1536,
            "4900b84c8c383b48ca4999daf26cc38db94d77c3abdf8715113527e82cdb64ec",
            "0167380f4e3102b7401969c16e5299436f02029339d7fcba626458db39d22b95",
        ),
        "fill": (
            1407,
            "8121a4fdd007a3c9b841133348f35cda00a40e82286291e728207aaaf6b74589",
            "f3c215988da00919e8aff60894849b443da0e34fb8d503cc9c336cf72e0d649a",
        ),
    }

    for name, (payload, domain_digest) in vectors.items():
        expected_length, expected_wire_sha, expected_domain_digest = expected[name]
        assert len(payload) == expected_length
        assert hashlib.sha256(payload).hexdigest() == expected_wire_sha
        assert domain_digest.value == expected_domain_digest


def test_submission_query_reader_requires_exact_order_context() -> None:
    order = _order()
    fact = create_submission_query_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("query-1"),
        occurred_at=CAUSAL_TIME + timedelta(minutes=2),
        provenance=PROVENANCE,
        outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED,
        subject_order=order,
    )
    payload = canonical_execution_fact_bytes(fact)

    decoded = decode_execution_fact(
        payload,
        context=SubmissionQueryFactDecodeContext(SPEC_SET, order),
    )
    assert decoded == fact

    with pytest.raises(ExecutionMessageError) as error:
        decode_execution_fact(
            payload,
            context=IndependentFactDecodeContext(SPEC_SET),
        )
    _assert_message_code(error, OutcomeCode.INVALID_TYPE)


def test_fact_reader_missing_identity_precedence_is_reachable() -> None:
    document = json.loads(canonical_execution_fact_bytes(_trade_fact()))
    document.pop("dedup_identity")
    with pytest.raises(ExecutionMessageError) as omitted:
        decode_execution_fact(
            _json_bytes(document),
            context=IndependentFactDecodeContext(SPEC_SET),
        )
    _assert_message_code(
        omitted,
        OutcomeCode.FACT_INVALID_MISSING_DEDUP_IDENTITY,
    )

    document["dedup_identity"] = None
    with pytest.raises(ExecutionMessageError) as explicit_null:
        decode_execution_fact(
            _json_bytes(document),
            context=IndependentFactDecodeContext(SPEC_SET),
        )
    _assert_message_code(
        explicit_null,
        OutcomeCode.FACT_INVALID_MISSING_DEDUP_IDENTITY,
    )

    document["unknown"] = 1
    with pytest.raises(ExecutionMessageError) as unknown:
        decode_execution_fact(
            _json_bytes(document),
            context=IndependentFactDecodeContext(SPEC_SET),
        )
    _assert_message_code(unknown, OutcomeCode.OUT_OF_RANGE)


def test_strict_readers_reject_duplicate_unknown_and_noncanonical_wire() -> None:
    intent = _intent()
    payload = canonical_order_intent_bytes(intent)
    duplicate = payload[:-1] + b',"schema_version":1}'
    with pytest.raises(ExecutionMessageError) as duplicate_error:
        decode_order_intent(
            duplicate,
            spec_set=SPEC_SET,
            target_lineage=TARGET,
            execution_policy=POLICY,
        )
    _assert_message_code(duplicate_error, OutcomeCode.OUT_OF_RANGE)

    unknown_document = json.loads(payload)
    unknown_document["unknown"] = 1
    with pytest.raises(ExecutionMessageError) as unknown_error:
        decode_order_intent(
            _json_bytes(unknown_document),
            spec_set=SPEC_SET,
            target_lineage=TARGET,
            execution_policy=POLICY,
        )
    _assert_message_code(unknown_error, OutcomeCode.OUT_OF_RANGE)

    noncanonical = json.dumps(
        json.loads(payload),
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    ).encode()
    with pytest.raises(ExecutionMessageError) as canonical_error:
        decode_order_intent(
            noncanonical,
            spec_set=SPEC_SET,
            target_lineage=TARGET,
            execution_policy=POLICY,
        )
    _assert_message_code(canonical_error, OutcomeCode.OUT_OF_RANGE)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing", OutcomeCode.OUT_OF_RANGE),
        ("envelope", OutcomeCode.OUT_OF_RANGE),
        ("run_type", OutcomeCode.INVALID_TYPE),
        ("run_value", OutcomeCode.OUT_OF_RANGE),
        ("id_type", OutcomeCode.INVALID_TYPE),
        ("id_keys", OutcomeCode.OUT_OF_RANGE),
        ("owner_type", OutcomeCode.INVALID_TYPE),
        ("owner_unknown", OutcomeCode.OUT_OF_RANGE),
        ("sequence_type", OutcomeCode.INVALID_TYPE),
        ("sequence_negative", OutcomeCode.OUT_OF_RANGE),
        ("instrument_keys", OutcomeCode.OUT_OF_RANGE),
        ("instrument_values", OutcomeCode.INVALID_TYPE),
        ("instrument_invalid", OutcomeCode.OUT_OF_RANGE),
        ("quantity_type", OutcomeCode.INVALID_TYPE),
        ("time_type", OutcomeCode.INVALID_TYPE),
        ("time_invalid", OutcomeCode.OUT_OF_RANGE),
        ("time_noncanonical", OutcomeCode.OUT_OF_RANGE),
        ("side_type", OutcomeCode.INVALID_TYPE),
        ("side_unknown", OutcomeCode.OUT_OF_RANGE),
        ("target_keys", OutcomeCode.OUT_OF_RANGE),
        ("policy_keys", OutcomeCode.OUT_OF_RANGE),
        ("policy_id_type", OutcomeCode.INVALID_TYPE),
        ("digest_type", OutcomeCode.INVALID_TYPE),
        ("digest_invalid", OutcomeCode.OUT_OF_RANGE),
        ("spec_ids_type", OutcomeCode.INVALID_TYPE),
        ("dispatch_negative", OutcomeCode.OUT_OF_RANGE),
        ("price_constraint", OutcomeCode.OUT_OF_RANGE),
    ],
)
def test_order_intent_reader_rejects_invalid_closed_fields(
    case: str,
    expected_code: OutcomeCode,
) -> None:
    document = cast(
        dict[str, object],
        json.loads(canonical_order_intent_bytes(_intent())),
    )
    intent_id = cast(dict[str, object], document["intent_id"])
    instrument = cast(dict[str, object], document["instrument"])
    target = cast(dict[str, object], document["target_lineage"])
    policy = cast(dict[str, object], document["execution_policy"])

    if case == "missing":
        document.pop("instrument_spec_set_sha256")
    elif case == "envelope":
        document["schema_version"] = 2
    elif case == "run_type":
        document["run_id"] = 1
    elif case == "run_value":
        document["run_id"] = "not-a-run-id"
    elif case == "id_type":
        document["intent_id"] = []
    elif case == "id_keys":
        intent_id.pop("owner_sequence")
    elif case == "owner_type":
        intent_id["owner_kind"] = 1
    elif case == "owner_unknown":
        intent_id["owner_kind"] = "unknown.owner"
    elif case == "sequence_type":
        intent_id["owner_sequence"] = "3"
    elif case == "sequence_negative":
        intent_id["owner_sequence"] = -1
    elif case == "instrument_keys":
        instrument.pop("symbol")
    elif case == "instrument_values":
        instrument["symbol"] = 1
    elif case == "instrument_invalid":
        instrument["symbol"] = ""
    elif case == "quantity_type":
        document["quantity"] = 10
    elif case == "time_type":
        document["causal_root_available_at"] = 1
    elif case == "time_invalid":
        document["causal_root_available_at"] = "not-a-time"
    elif case == "time_noncanonical":
        document["causal_root_available_at"] = "2026-01-02T09:31:00.0Z"
    elif case == "side_type":
        document["side"] = 1
    elif case == "side_unknown":
        document["side"] = "hold"
    elif case == "target_keys":
        target.pop("target_sha256")
    elif case == "policy_keys":
        policy.pop("execution_policy_sha256")
    elif case == "policy_id_type":
        policy["execution_policy_id"] = 1
    elif case == "digest_type":
        policy["execution_policy_sha256"] = 1
    elif case == "digest_invalid":
        policy["execution_policy_sha256"] = "not-a-digest"
    elif case == "spec_ids_type":
        document["instrument_specification_id"] = 1
    elif case == "dispatch_negative":
        document["dispatch_sequence"] = -1
    elif case == "price_constraint":
        document["price_constraint"] = "100"
    else:  # pragma: no cover - parametrization is the closed case registry
        raise AssertionError(case)

    with pytest.raises(ExecutionMessageError) as error:
        decode_order_intent(
            _json_bytes(document),
            spec_set=SPEC_SET,
            target_lineage=TARGET,
            execution_policy=POLICY,
        )
    _assert_message_code(error, expected_code)


@pytest.mark.parametrize(
    "payload",
    [
        bytearray(canonical_order_intent_bytes(_intent())),
        b"[]",
        b'{"value":1.5}',
        b'{"value":NaN}',
        b"\xff",
    ],
)
def test_order_intent_reader_rejects_non_message_json(
    payload: object,
) -> None:
    with pytest.raises(ExecutionMessageError):
        decode_order_intent(
            cast(bytes, payload),
            spec_set=SPEC_SET,
            target_lineage=TARGET,
            execution_policy=POLICY,
        )


def test_fact_reader_rejects_digest_mismatch_and_third_identity_tag() -> None:
    fact = _trade_fact()
    document = json.loads(canonical_execution_fact_bytes(fact))
    document["fact_sha256"] = "0" * 64
    with pytest.raises(ExecutionMessageError) as digest_error:
        decode_execution_fact(
            _json_bytes(document),
            context=IndependentFactDecodeContext(SPEC_SET),
        )
    _assert_message_code(digest_error, OutcomeCode.FACT_INVALID)

    document = json.loads(canonical_execution_fact_bytes(fact))
    document["dedup_identity"] = {"kind": "payload_hash", "value": "0" * 64}
    with pytest.raises(ExecutionMessageError) as tag_error:
        decode_execution_fact(
            _json_bytes(document),
            context=IndependentFactDecodeContext(SPEC_SET),
        )
    _assert_message_code(tag_error, OutcomeCode.OUT_OF_RANGE)


def test_unbounded_ingress_sequence_round_trips_beyond_digit_limit() -> None:
    fact = _trade_fact()
    huge = 10**5000
    ingress = create_execution_fact_ingress(
        available_at=fact.occurred_at,
        source_namespace=SOURCE,
        ingress_sequence=huge,
        fact=fact,
    )
    payload = canonical_execution_fact_ingress_bytes(ingress)
    assert b'"ingress_sequence":1' + b"0" * 5000 in payload
    decoded = decode_execution_fact_ingress(
        payload,
        context=IndependentFactDecodeContext(SPEC_SET),
    )
    assert decoded.ingress_sequence == huge


def test_exact_message_identity_replay_and_conflict_are_pure() -> None:
    first = _intent()
    replay = _intent()
    changed = _intent(quantity="9")
    assert (
        classify_identity_replay(
            first.intent_id,
            canonical_order_intent_bytes(first),
            replay.intent_id,
            canonical_order_intent_bytes(replay),
        ).value
        == "exact_replay"
    )
    assert (
        classify_identity_replay(
            first.intent_id,
            canonical_order_intent_bytes(first),
            changed.intent_id,
            canonical_order_intent_bytes(changed),
        ).value
        == "conflict"
    )

    fact = _trade_fact()
    assert (
        classify_identity_replay(
            FactDedupKey(fact.source_namespace, fact.dedup_identity),
            canonical_execution_fact_bytes(fact),
            fact.dedup_key,
            canonical_execution_fact_bytes(fact),
        ).value
        == "exact_replay"
    )


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (
            lambda: create_order_intent(
                run_id=RUN_ID,
                intent_id=EconomicId(
                    RUN_ID,
                    EconomicOwnerKind.EXECUTION_ORDER,
                    3,
                ),
                correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1),
                target_lineage=TARGET,
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=CanonicalDecimal("10"),
                portfolio_snapshot_version=5,
                causal_root_available_at=CAUSAL_TIME,
                dispatch_sequence=7,
                spec_set=SPEC_SET,
                execution_policy=POLICY,
            ),
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            lambda: create_order_intent(
                run_id=RUN_ID,
                intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, 3),
                correlation_id=EconomicId(
                    OTHER_RUN_ID,
                    EconomicOwnerKind.STRATEGY_SIGNAL,
                    1,
                ),
                target_lineage=TARGET,
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=CanonicalDecimal("10"),
                portfolio_snapshot_version=5,
                causal_root_available_at=CAUSAL_TIME,
                dispatch_sequence=7,
                spec_set=SPEC_SET,
                execution_policy=POLICY,
            ),
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            lambda: create_order_intent(
                run_id=RUN_ID,
                intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, 3),
                correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1),
                target_lineage=TARGET,
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=CanonicalDecimal("10"),
                portfolio_snapshot_version=cast(int, True),
                causal_root_available_at=CAUSAL_TIME,
                dispatch_sequence=7,
                spec_set=SPEC_SET,
                execution_policy=POLICY,
            ),
            OutcomeCode.INVALID_TYPE,
        ),
    ],
)
def test_owner_run_and_exact_version_failures_have_frozen_codes(
    factory: Callable[[], object],
    code: OutcomeCode,
) -> None:
    with pytest.raises(ExecutionMessageError) as error:
        factory()
    _assert_message_code(error, code)


def test_all_message_digests_change_with_relevant_content() -> None:
    first_intent = _intent()
    second_intent = _intent(quantity="9")
    assert order_intent_digest(first_intent) != order_intent_digest(second_intent)

    first_decision = _allow(first_intent)
    second_decision = _allow(second_intent)
    assert risk_decision_digest(first_decision) != risk_decision_digest(second_decision)
    assert first_decision.approval is not None
    assert second_decision.approval is not None
    assert execution_approval_digest(first_decision.approval) != (
        execution_approval_digest(second_decision.approval)
    )

    first_order = _order(first_intent, first_decision)
    second_order = _order(second_intent, second_decision)
    assert order_digest(first_order) != order_digest(second_order)

    first_fact = _trade_fact()
    second_fact = create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=SourceNativeSequence(42),
        occurred_at=first_fact.occurred_at,
        provenance=PROVENANCE,
        spec_set=SPEC_SET,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal("10"),
        price=CanonicalDecimal("101.25"),
    )
    assert execution_fact_digest(first_fact) != execution_fact_digest(second_fact)
