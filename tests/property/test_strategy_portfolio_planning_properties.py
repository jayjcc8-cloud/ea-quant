from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    MarketDataEnvelope,
    OrderSide,
    Phase1PortfolioPolicyEntry,
    PlanningOutcomeKind,
    PortfolioPolicyId,
    PriceDomain,
    ReplayWindow,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SignalDirection,
    SourceNamespace,
    StrategySignal,
    VenueId,
    VenueOrderId,
    build_instrument_spec_set,
    create_fill,
    create_phase1_portfolio_policy,
    create_trade_execution_fact,
)
from ea.core.execution_messages import Fill
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.portfolio import create_portfolio_ledger, create_portfolio_planning_authority
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.strategy-planning-property.v1"),
    (
        InstrumentExecutionSpec(
            instrument=INSTRUMENT,
            specification_id=InstrumentSpecId("xnas.aapl.property.v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("0.1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        ),
    ),
)
EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
HEADER = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
ROW = (
    "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,"
    "raw,100.0,101.0,99.0,100.5,10.0,property.raw,0,0,"
    "2026-01-02T09:31:00.000000Z"
)
WINDOW = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
)


def _id(kind: EconomicOwnerKind, sequence: int) -> EconomicId:
    return EconomicId(RUN_ID, kind, sequence)


def _signal(direction: SignalDirection) -> StrategySignal:
    dataset = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, ROW)) + "\n").encode(),
        replay_window=WINDOW,
    )
    source = create_phase1_historical_market_data_source(dataset)
    bridge = create_phase1_historical_market_source_bridge(source)
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=bridge,
    )
    lease = runtime.pop()
    root = cast(MarketDataEnvelope, lease.root)
    authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )
    return authority.issue(
        root,
        dispatch_sequence=lease.dispatch_sequence,
        direction=direction,
    )


def _tenth_text(ticks: int) -> str:
    sign = "-" if ticks < 0 else ""
    absolute = abs(ticks)
    whole, fractional = divmod(absolute, 10)
    return f"{sign}{whole}" if fractional == 0 else f"{sign}{whole}.{fractional}"


def _position_fill(position_ticks: int) -> Fill:
    side = OrderSide.BUY if position_ticks > 0 else OrderSide.SELL
    order_id = _id(EconomicOwnerKind.EXECUTION_ORDER, 1)
    fact = create_trade_execution_fact(
        source_namespace=SourceNamespace("sim.property"),
        dedup_identity=ExternalFactId("position"),
        occurred_at=datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        provenance=FactProvenance(
            FactProvenanceId("phase1.simulator.v1"),
            Sha256Digest("3" * 64),
        ),
        spec_set=SPEC_SET,
        instrument=INSTRUMENT,
        side=side,
        quantity=CanonicalDecimal(_tenth_text(abs(position_ticks))),
        price=CanonicalDecimal("100"),
        client_submission_key=Sha256Digest("4" * 64),
        venue_order_id=VenueOrderId("property-order"),
        order_id=order_id,
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        causation_id=order_id,
    )
    return create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 1),
        fact=fact,
        spec_set=SPEC_SET,
    )


@given(
    direction=st.sampled_from(tuple(SignalDirection)),
    current_ticks=st.integers(min_value=-200, max_value=200),
    target_quantity_ticks=st.integers(min_value=1, max_value=200),
)
@settings(max_examples=90)
def test_target_current_delta_has_exact_side_quantity_and_noop(
    direction: SignalDirection,
    current_ticks: int,
    target_quantity_ticks: int,
) -> None:
    signal = _signal(direction)
    ledger = create_portfolio_ledger(run_id=RUN_ID, spec_set=SPEC_SET)
    if current_ticks != 0:
        ledger.apply_fill(_position_fill(current_ticks))
    policy = create_phase1_portfolio_policy(
        policy_id=PortfolioPolicyId("phase1.strategy-planning-property.v1"),
        entries=(
            Phase1PortfolioPolicyEntry(
                instrument=INSTRUMENT,
                target_quantity=CanonicalDecimal(_tenth_text(target_quantity_ticks)),
            ),
        ),
        spec_set=SPEC_SET,
    )
    authority = create_portfolio_planning_authority(
        run_id=RUN_ID,
        ledger=ledger,
        spec_set=SPEC_SET,
        policy=policy,
        execution_policy=EXECUTION_POLICY,
    )
    result = authority.plan(signal)
    expected_target_ticks = {
        SignalDirection.LONG: target_quantity_ticks,
        SignalDirection.FLAT: 0,
        SignalDirection.SHORT: -target_quantity_ticks,
    }[direction]
    expected_delta_ticks = expected_target_ticks - current_ticks

    assert result.target.target_position.text == _tenth_text(expected_target_ticks)
    assert result.outcome.delta.text == _tenth_text(expected_delta_ticks)
    if expected_delta_ticks == 0:
        assert result.outcome.kind is PlanningOutcomeKind.ALREADY_AT_TARGET
        assert result.intent is None
    else:
        assert result.outcome.kind is PlanningOutcomeKind.INTENT_EMITTED
        assert result.intent is not None
        assert result.intent.quantity.text == _tenth_text(abs(expected_delta_ticks))
        assert result.intent.side is (OrderSide.BUY if expected_delta_ticks > 0 else OrderSide.SELL)
    first_state = authority.state
    assert authority.plan(signal) is result
    assert authority.state is first_state
