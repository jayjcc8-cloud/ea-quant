from __future__ import annotations

import json
from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderIntent,
    OrderSide,
    Phase1RiskPolicy,
    PortfolioSnapshot,
    PositionBalance,
    PriceDomain,
    RiskDecisionKind,
    RiskHaltReason,
    RiskPolicyId,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    TargetLineageRef,
    VenueId,
    build_instrument_spec_set,
    canonical_risk_state_snapshot_bytes,
    create_order_intent,
    create_phase1_risk_policy,
    instrument_spec_set_digest,
)
from ea.risk import Phase1RiskAuthority, create_phase1_risk_authority

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
SPEC = InstrumentExecutionSpec(
    instrument=INSTRUMENT,
    specification_id=InstrumentSpecId("xnas.aapl.risk-property.v1"),
    price_quantum=CanonicalDecimal("0.01"),
    quantity_quantum=CanonicalDecimal("1"),
    settlement_currency=SettlementCurrency("USD"),
    currency_quantum=CanonicalDecimal("0.01"),
    contract_multiplier=CanonicalDecimal("1"),
    price_domain=PriceDomain.POSITIVE,
)
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.risk-property.v1"),
    (SPEC,),
)
EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)


def _id(owner: EconomicOwnerKind, sequence: int) -> EconomicId:
    return EconomicId(RUN_ID, owner, sequence)


def _authority(order_limit: int, position_limit: int) -> Phase1RiskAuthority:
    policy: Phase1RiskPolicy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk-property.v1"),
        spec_set=SPEC_SET,
        execution_policy=EXECUTION_POLICY,
        instrument_limits=(
            InstrumentRiskLimit(
                instrument=INSTRUMENT,
                maximum_order_quantity=CanonicalDecimal(str(order_limit)),
                maximum_absolute_position=CanonicalDecimal(str(position_limit)),
            ),
        ),
    )
    return create_phase1_risk_authority(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        execution_policy=EXECUTION_POLICY,
        policy=policy,
    )


def _snapshot(position: int) -> PortfolioSnapshot:
    version = 0 if position == 0 else 1
    return PortfolioSnapshot(
        run_id=RUN_ID,
        instrument_spec_set_id=SPEC_SET.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(SPEC_SET),
        snapshot_version=version,
        ledger_sequence=version,
        last_entry_id=None if version == 0 else _id(EconomicOwnerKind.LEDGER_ENTRY, 1),
        last_transaction_sha256=None if version == 0 else Sha256Digest("2" * 64),
        cash_balances=(),
        position_balances=(
            ()
            if position == 0
            else (
                PositionBalance(
                    instrument=INSTRUMENT,
                    quantity_quantum=CanonicalDecimal("1"),
                    quantity=CanonicalDecimal(str(position)),
                ),
            )
        ),
        rounding_balances=(),
        unresolved_fills=(),
    )


def _intent(
    *,
    sequence: int,
    dispatch_sequence: int | None = None,
    side: OrderSide,
    quantity: int,
    snapshot_version: int,
) -> OrderIntent:
    return create_order_intent(
        run_id=RUN_ID,
        intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, sequence),
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        target_lineage=TargetLineageRef(
            _id(EconomicOwnerKind.PORTFOLIO_TARGET, 1),
            Sha256Digest("3" * 64),
        ),
        instrument=INSTRUMENT,
        side=side,
        quantity=CanonicalDecimal(str(quantity)),
        portfolio_snapshot_version=snapshot_version,
        causal_root_available_at=TIME,
        dispatch_sequence=sequence if dispatch_sequence is None else dispatch_sequence,
        spec_set=SPEC_SET,
        execution_policy=EXECUTION_POLICY,
    )


@given(
    side=st.sampled_from((OrderSide.BUY, OrderSide.SELL)),
    position=st.integers(min_value=-30, max_value=30),
    quantity=st.integers(min_value=1, max_value=30),
    order_limit=st.integers(min_value=1, max_value=20),
    position_limit=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=160)
def test_every_executable_quantity_obeys_piecewise_position_safety(
    side: OrderSide,
    position: int,
    quantity: int,
    order_limit: int,
    position_limit: int,
) -> None:
    authority = _authority(order_limit, position_limit)
    snapshot = _snapshot(position)
    intent = _intent(
        sequence=1,
        side=side,
        quantity=quantity,
        snapshot_version=snapshot.snapshot_version,
    )

    result = authority.evaluate(intent, snapshot)
    approved = (
        0
        if result.decision.approved_quantity is None
        else result.decision.approved_quantity.coefficient
    )

    assert 0 <= approved <= quantity
    assert approved <= order_limit
    projected = position + (approved if side is OrderSide.BUY else -approved)
    if -position_limit <= position <= position_limit:
        assert -position_limit <= projected <= position_limit
    elif position > position_limit:
        assert -position_limit <= projected <= position
        assert projected <= position
    else:
        assert position <= projected <= position_limit
        assert projected >= position
    if result.decision.kind in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE):
        assert approved > 0
    else:
        assert approved == 0


@given(
    position=st.integers(min_value=-30, max_value=30),
    quantity=st.integers(min_value=1, max_value=30),
    order_limit=st.integers(min_value=1, max_value=20),
    position_limit=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=120)
def test_buy_sell_and_long_short_are_exactly_symmetric(
    position: int,
    quantity: int,
    order_limit: int,
    position_limit: int,
) -> None:
    buy_authority = _authority(order_limit, position_limit)
    sell_authority = _authority(order_limit, position_limit)
    buy_snapshot = _snapshot(position)
    sell_snapshot = _snapshot(-position)

    buy = buy_authority.evaluate(
        _intent(
            sequence=1,
            side=OrderSide.BUY,
            quantity=quantity,
            snapshot_version=buy_snapshot.snapshot_version,
        ),
        buy_snapshot,
    )
    sell = sell_authority.evaluate(
        _intent(
            sequence=1,
            side=OrderSide.SELL,
            quantity=quantity,
            snapshot_version=sell_snapshot.snapshot_version,
        ),
        sell_snapshot,
    )

    assert buy.decision.kind is sell.decision.kind
    assert buy.decision.approved_quantity == sell.decision.approved_quantity
    assert buy.evidence.reason_code is sell.evidence.reason_code


@given(later_count=st.integers(min_value=0, max_value=20))
@settings(max_examples=21)
def test_exact_replay_never_allocates_after_any_later_history(later_count: int) -> None:
    authority = _authority(20, 20)
    snapshot = _snapshot(0)
    original_intent = _intent(
        sequence=1,
        side=OrderSide.BUY,
        quantity=1,
        snapshot_version=0,
    )
    original = authority.evaluate(original_intent, snapshot)
    for offset in range(later_count):
        later = _intent(
            sequence=offset + 2,
            side=OrderSide.SELL if offset % 2 else OrderSide.BUY,
            quantity=offset + 1,
            snapshot_version=0,
        )
        authority.evaluate(later, snapshot)
    before = authority.results

    replay = authority.evaluate(original_intent, snapshot)

    assert replay is original
    assert authority.results is before


@given(
    dispatch=st.integers(
        min_value=1 << 64,
        max_value=(1 << 4096) - 1,
    )
)
@settings(max_examples=40)
def test_generated_unbounded_dispatch_is_preserved_by_evaluation_and_halt(
    dispatch: int,
) -> None:
    authority = _authority(20, 20)
    snapshot = _snapshot(0)
    intent = _intent(
        sequence=1,
        dispatch_sequence=dispatch,
        side=OrderSide.BUY,
        quantity=1,
        snapshot_version=0,
    )

    original = authority.evaluate(intent, snapshot)
    replay = authority.evaluate(intent, snapshot)
    state = authority.engage_halt(
        RiskHaltReason.EXTERNAL_SAFETY_HALT,
        TIME,
        dispatch,
    )

    assert replay is original
    assert original.decision.dispatch_sequence == dispatch
    assert state.halt_dispatch_sequence == dispatch
    assert (
        json.loads(canonical_risk_state_snapshot_bytes(state))["halt_dispatch_sequence"] == dispatch
    )
