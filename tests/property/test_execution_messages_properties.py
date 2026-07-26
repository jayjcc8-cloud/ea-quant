from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    IndependentFactDecodeContext,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderIntent,
    OrderSide,
    OutcomeCode,
    PriceDomain,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    TargetLineageRef,
    VenueId,
    build_instrument_spec_set,
    canonical_execution_fact_ingress_bytes,
    canonical_order_intent_bytes,
    classify_identity_replay,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    create_order_intent,
    decode_execution_fact_ingress,
    resize_order_intent,
)

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.us-equities.v1"),
    [
        InstrumentExecutionSpec(
            instrument=INSTRUMENT,
            specification_id=InstrumentSpecId("xnas.aapl.v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        )
    ],
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
LIFECYCLE_FACT = create_lifecycle_execution_fact(
    kind=ExecutionFactKind.ACKNOWLEDGEMENT,
    source_namespace=SOURCE,
    dedup_identity=ExternalFactId("ack-1"),
    occurred_at=CAUSAL_TIME,
    provenance=PROVENANCE,
)


def _intent(quantity: int) -> OrderIntent:
    return create_order_intent(
        run_id=RUN_ID,
        intent_id=EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 3),
        correlation_id=EconomicId(RUN_ID, EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        target_lineage=TARGET,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(str(quantity)),
        portfolio_snapshot_version=5,
        causal_root_available_at=CAUSAL_TIME,
        dispatch_sequence=7,
        spec_set=SPEC_SET,
        execution_policy=POLICY,
    )


@given(
    original_units=st.integers(min_value=2, max_value=10**12),
    selector=st.integers(min_value=0, max_value=10**12),
)
def test_resize_always_produces_a_strict_positive_subquantity(
    original_units: int,
    selector: int,
) -> None:
    intent = create_order_intent(
        run_id=RUN_ID,
        intent_id=EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 3),
        correlation_id=EconomicId(RUN_ID, EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        target_lineage=TARGET,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(str(original_units)),
        portfolio_snapshot_version=5,
        causal_root_available_at=CAUSAL_TIME,
        dispatch_sequence=7,
        spec_set=SPEC_SET,
        execution_policy=POLICY,
    )
    approved_units = 1 + selector % (original_units - 1)

    decision = resize_order_intent(
        decision_id=EconomicId(RUN_ID, EconomicOwnerKind.RISK_DECISION, 4),
        approval_id=EconomicId(RUN_ID, EconomicOwnerKind.RISK_APPROVAL, 5),
        intent=intent,
        approved_quantity=CanonicalDecimal(str(approved_units)),
        spec_set=SPEC_SET,
        risk_state_version=11,
    )

    assert decision.approved_quantity == CanonicalDecimal(str(approved_units))
    assert approved_units < original_units
    assert decision.outcome_code is OutcomeCode.RISK_RESIZED


@given(
    first_quantity=st.integers(min_value=1, max_value=10**12),
    second_quantity=st.integers(min_value=1, max_value=10**12),
)
def test_intent_replay_classification_is_argument_order_invariant(
    first_quantity: int,
    second_quantity: int,
) -> None:
    first = _intent(first_quantity)
    second = _intent(second_quantity)
    first_bytes = canonical_order_intent_bytes(first)
    second_bytes = canonical_order_intent_bytes(second)

    forward = classify_identity_replay(
        first.intent_id,
        first_bytes,
        second.intent_id,
        second_bytes,
    )
    reverse = classify_identity_replay(
        second.intent_id,
        second_bytes,
        first.intent_id,
        first_bytes,
    )

    assert forward is reverse
    assert (forward.value == "exact_replay") is (first_quantity == second_quantity)


@given(sequence=st.integers(min_value=0, max_value=10**100))
def test_ingress_sequence_round_trips_for_arbitrary_nonnegative_integers(
    sequence: int,
) -> None:
    ingress = create_execution_fact_ingress(
        available_at=CAUSAL_TIME,
        source_namespace=SOURCE,
        ingress_sequence=sequence,
        fact=LIFECYCLE_FACT,
    )

    decoded = decode_execution_fact_ingress(
        canonical_execution_fact_ingress_bytes(ingress),
        context=IndependentFactDecodeContext(SPEC_SET),
    )

    assert decoded == ingress
