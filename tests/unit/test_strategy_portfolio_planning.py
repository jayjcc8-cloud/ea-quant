from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import ea.portfolio.planning as planning_module
import ea.strategy.authority as signal_authority_module
from ea.core import (
    ActiveMarketDispatchProof,
    AuthorityConflictEvidence,
    AuthorityConflictKind,
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
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    MarketDataEnvelope,
    OrderSide,
    OutcomeCode,
    Phase1PortfolioPolicy,
    Phase1PortfolioPolicyEntry,
    PlanningOutcome,
    PlanningOutcomeKind,
    PortfolioPlanningAuthorityState,
    PortfolioPlanningError,
    PortfolioPlanningResult,
    PortfolioPolicyId,
    PortfolioTarget,
    PositionBalance,
    PriceDomain,
    ReplayWindow,
    RiskPolicyId,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SignalDirection,
    SourceNamespace,
    StrategyContractError,
    StrategySignal,
    StrategySignalAuthorityState,
    VenueId,
    VenueOrderId,
    build_instrument_spec_set,
    canonical_phase1_portfolio_policy_bytes,
    canonical_portfolio_planning_result_bytes,
    canonical_strategy_signal_authority_state_bytes,
    canonical_strategy_signal_bytes,
    create_fill,
    create_phase1_portfolio_policy,
    create_phase1_risk_policy,
    create_trade_execution_fact,
    portfolio_planning_result_digest,
    strategy_signal_digest,
    validate_portfolio_planning_risk_handoff,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_messages import Fill
from ea.core.portfolio_planning import _create_portfolio_planning_authority_state
from ea.core.strategy import _create_strategy_signal_authority_state
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.portfolio import (
    PortfolioPlanningAuthority,
    create_portfolio_ledger,
    create_portfolio_planning_authority,
)
from ea.portfolio.ledger import PortfolioLedger
from ea.risk import create_phase1_risk_authority
from ea.runtime import (
    Phase1HistoricalMarketRuntime,
    RuntimeDispatchLease,
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import StrategySignalAuthority, create_strategy_signal_authority

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
USD = SettlementCurrency("USD")
EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
WINDOW = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "phase1_ohlcv_v1.csv"


def _spec_set(*, quantity_quantum: str = "1") -> InstrumentExecutionSpecSet:
    return build_instrument_spec_set(
        InstrumentSpecSetId("phase1.strategy-planning.v1"),
        (
            InstrumentExecutionSpec(
                instrument=INSTRUMENT,
                specification_id=InstrumentSpecId("xnas.aapl.v1"),
                price_quantum=CanonicalDecimal("0.01"),
                quantity_quantum=CanonicalDecimal(quantity_quantum),
                settlement_currency=USD,
                currency_quantum=CanonicalDecimal("0.01"),
                contract_multiplier=CanonicalDecimal("1"),
                price_domain=PriceDomain.POSITIVE,
            ),
        ),
    )


def _runtime(
    spec_set: InstrumentExecutionSpecSet,
    *,
    run_id: RunId = RUN_ID,
) -> Phase1HistoricalMarketRuntime:
    dataset = decode_phase1_ohlcv_csv(FIXTURE.read_bytes(), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)
    bridge = create_phase1_historical_market_source_bridge(source)
    return create_phase1_historical_market_runtime(
        run_id=run_id,
        spec_set=spec_set,
        source=bridge,
    )


def _signal(
    direction: SignalDirection = SignalDirection.LONG,
    *,
    spec_set: InstrumentExecutionSpecSet | None = None,
    run_id: RunId = RUN_ID,
) -> tuple[
    Phase1HistoricalMarketRuntime,
    RuntimeDispatchLease,
    StrategySignalAuthority,
    StrategySignal,
]:
    selected_specs = spec_set or _spec_set()
    runtime = _runtime(selected_specs, run_id=run_id)
    lease = runtime.pop()
    assert type(lease.root) is MarketDataEnvelope
    verifier = create_active_market_dispatch_verifier(runtime)
    authority = create_strategy_signal_authority(run_id=run_id, verifier=verifier)
    signal = authority.issue(
        lease.root,
        dispatch_sequence=lease.dispatch_sequence,
        direction=direction,
    )
    return runtime, lease, authority, signal


def _policy(
    spec_set: InstrumentExecutionSpecSet,
    *,
    target: str = "10",
) -> Phase1PortfolioPolicy:
    return create_phase1_portfolio_policy(
        policy_id=PortfolioPolicyId("phase1.strategy-planning.v1"),
        entries=(
            Phase1PortfolioPolicyEntry(
                instrument=INSTRUMENT,
                target_quantity=CanonicalDecimal(target),
            ),
        ),
        spec_set=spec_set,
    )


def _ledger_and_planner(
    spec_set: InstrumentExecutionSpecSet,
    *,
    ledger: PortfolioLedger | None = None,
    target: str = "10",
) -> tuple[PortfolioLedger, PortfolioPlanningAuthority]:
    selected_ledger = ledger or create_portfolio_ledger(run_id=RUN_ID, spec_set=spec_set)
    planner = create_portfolio_planning_authority(
        run_id=RUN_ID,
        ledger=selected_ledger,
        spec_set=spec_set,
        policy=_policy(spec_set, target=target),
        execution_policy=EXECUTION_POLICY,
    )
    return selected_ledger, planner


def _id(kind: EconomicOwnerKind, sequence: int) -> EconomicId:
    return EconomicId(RUN_ID, kind, sequence)


def _fill(
    spec_set: InstrumentExecutionSpecSet,
    *,
    fill_sequence: int,
    quantity: str,
    resolved: bool,
) -> Fill:
    order_id = _id(EconomicOwnerKind.EXECUTION_ORDER, fill_sequence)
    fact = create_trade_execution_fact(
        source_namespace=SourceNamespace("sim.primary"),
        dedup_identity=ExternalFactId(f"trade-{fill_sequence}"),
        occurred_at=datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        provenance=FactProvenance(
            FactProvenanceId("phase1.simulator.v1"),
            Sha256Digest("3" * 64),
        ),
        spec_set=spec_set,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(quantity),
        price=CanonicalDecimal("100"),
        client_submission_key=Sha256Digest("4" * 64) if resolved else None,
        venue_order_id=VenueOrderId(f"venue-{fill_sequence}") if resolved else None,
        order_id=order_id if resolved else None,
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1) if resolved else None,
        causation_id=order_id if resolved else None,
    )
    return create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, fill_sequence),
        fact=fact,
        spec_set=spec_set,
    )


def _forge_signal(signal: StrategySignal, **changes: object) -> StrategySignal:
    value = object.__new__(StrategySignal)
    for name in StrategySignal.__slots__:
        object.__setattr__(value, name, changes.get(name, getattr(signal, name)))
    return value


def test_signal_requires_exact_active_dispatch_and_replays_after_acknowledgement() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    lease = runtime.pop()
    assert type(lease.root) is MarketDataEnvelope
    verifier = create_active_market_dispatch_verifier(runtime)
    authority = create_strategy_signal_authority(run_id=RUN_ID, verifier=verifier)

    equal_copy = replace(lease.root)
    assert equal_copy == lease.root
    assert equal_copy is not lease.root
    before = canonical_strategy_signal_authority_state_bytes(authority.state)
    with pytest.raises(StrategyContractError) as copied:
        authority.issue(
            equal_copy,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert copied.value.code is OutcomeCode.CONFLICTING_ID
    assert canonical_strategy_signal_authority_state_bytes(authority.state) == before

    signal = authority.issue(
        lease.root,
        dispatch_sequence=lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    runtime.acknowledge(lease)
    replay = authority.issue(
        lease.root,
        dispatch_sequence=lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    assert replay is signal
    assert authority.lookup_by_dispatch_sequence(lease.dispatch_sequence) is signal


def test_signal_conflict_halts_once_but_exact_replay_survives() -> None:
    runtime, lease, authority, signal = _signal()
    root = cast(MarketDataEnvelope, lease.root)
    runtime.acknowledge(lease)
    with pytest.raises(StrategyContractError) as conflict:
        authority.issue(
            root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.SHORT,
        )
    assert conflict.value.code is OutcomeCode.CONFLICTING_ID
    assert authority.state.halted is True
    assert authority.state.conflict is not None
    assert authority.state.conflict.conflict_kind is AuthorityConflictKind.IDENTITY_REUSE
    first_conflict = authority.state.conflict
    assert (
        authority.issue(
            root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
        is signal
    )
    with pytest.raises(StrategyContractError):
        authority.issue(
            replace(root),
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.FLAT,
        )
    assert authority.state.conflict is first_conflict


def test_signal_skips_dispatches_and_non_monotone_input_halts_before_verification() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )
    first_lease = runtime.pop()
    first = authority.issue(
        cast(MarketDataEnvelope, first_lease.root),
        dispatch_sequence=first_lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    runtime.acknowledge(first_lease)
    skipped_lease = runtime.pop()
    runtime.acknowledge(skipped_lease)
    third_lease = runtime.pop()
    third_root = cast(MarketDataEnvelope, third_lease.root)
    third = authority.issue(
        third_root,
        dispatch_sequence=third_lease.dispatch_sequence,
        direction=SignalDirection.FLAT,
    )
    assert first.signal_id.owner_sequence == 1
    assert third.signal_id.owner_sequence == 2
    assert third.dispatch_sequence == 3
    with pytest.raises(StrategyContractError) as non_monotone:
        authority.issue(
            third_root,
            dispatch_sequence=2,
            direction=SignalDirection.SHORT,
        )
    assert non_monotone.value.code is OutcomeCode.CONFLICTING_ID
    assert authority.state.halted is True
    assert authority.state.conflict is not None
    assert authority.state.conflict.conflict_kind is AuthorityConflictKind.NON_MONOTONE_DISPATCH
    assert authority.state.conflict.occupied_owner_id is None


def test_active_dispatch_verifier_rejects_unadmitted_foreign_run_and_stale_root() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    verifier = create_active_market_dispatch_verifier(runtime)
    foreign_runtime = _runtime(spec_set, run_id=OTHER_RUN_ID)
    foreign_lease = foreign_runtime.pop()
    foreign_root = cast(MarketDataEnvelope, foreign_lease.root)
    with pytest.raises(StrategyContractError) as unadmitted:
        verifier.verify_active_market_dispatch(
            foreign_root,
            dispatch_sequence=foreign_lease.dispatch_sequence,
        )
    assert unadmitted.value.code is OutcomeCode.CONFLICTING_ID

    lease = runtime.pop()
    root = cast(MarketDataEnvelope, lease.root)
    runtime.acknowledge(lease)
    with pytest.raises(StrategyContractError) as stale:
        verifier.verify_active_market_dispatch(
            root,
            dispatch_sequence=lease.dispatch_sequence,
        )
    assert stale.value.code is OutcomeCode.CONFLICTING_ID

    foreign_authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(foreign_runtime),
    )
    with pytest.raises(StrategyContractError) as foreign_run:
        foreign_authority.issue(
            foreign_root,
            dispatch_sequence=foreign_lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert foreign_run.value.code is OutcomeCode.CONFLICTING_ID
    assert foreign_authority.state.issuance_count == 0


def test_signal_rejects_foreign_proof_and_malformed_carrier_without_mutation() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    lease = runtime.pop()
    assert type(lease.root) is MarketDataEnvelope
    real_verifier = create_active_market_dispatch_verifier(runtime)

    class ProxyVerifier:
        def verify_active_market_dispatch(
            self,
            market_root: MarketDataEnvelope,
            *,
            dispatch_sequence: int,
        ) -> ActiveMarketDispatchProof:
            return real_verifier.verify_active_market_dispatch(
                market_root,
                dispatch_sequence=dispatch_sequence,
            )

    authority = create_strategy_signal_authority(run_id=RUN_ID, verifier=ProxyVerifier())
    before = authority.state
    with pytest.raises(StrategyContractError) as foreign:
        authority.issue(
            lease.root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert foreign.value.code is OutcomeCode.CONFLICTING_ID
    assert authority.state is before
    malformed = object.__new__(MarketDataEnvelope)
    with pytest.raises(StrategyContractError) as invalid:
        authority.issue(
            malformed,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert invalid.value.code is OutcomeCode.INVALID_TYPE
    assert authority.state is before


def test_signal_and_policy_have_closed_canonical_documents_and_factory_seals() -> None:
    spec_set = _spec_set()
    _, _, _, signal = _signal(spec_set=spec_set)
    signal_document = json.loads(canonical_strategy_signal_bytes(signal))
    assert signal_document["schema"] == "ea.phase1-strategy-signal.v1"
    assert signal_document["direction"] == "long"
    assert signal_document["signal_id"]["owner_kind"] == "strategy.signal"
    assert len(strategy_signal_digest(signal).value) == 64
    policy = _policy(spec_set, target="10")
    policy_document = json.loads(canonical_phase1_portfolio_policy_bytes(policy))
    assert policy_document["entries"][0]["target_quantity"] == "10"
    assert set(policy_document) == {
        "canonicalization",
        "entries",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "policy_id",
        "schema",
    }
    with pytest.raises(TypeError):
        StrategySignal()
    with pytest.raises(TypeError):
        ActiveMarketDispatchProof()
    for factory_only in (
        AuthorityConflictEvidence,
        StrategySignalAuthorityState,
        Phase1PortfolioPolicy,
        PortfolioTarget,
        PlanningOutcome,
        PortfolioPlanningResult,
        PortfolioPlanningAuthorityState,
    ):
        with pytest.raises(TypeError):
            factory_only()
    with pytest.raises(FrozenInstanceError):
        cast(Any, policy).entries = ()
    object.__setattr__(
        policy.entries[0],
        "target_quantity",
        CanonicalDecimal("-10"),
    )
    with pytest.raises(PortfolioPlanningError) as mutated_policy:
        canonical_phase1_portfolio_policy_bytes(policy)
    assert mutated_policy.value.code is OutcomeCode.OUT_OF_RANGE


@pytest.mark.parametrize(
    ("direction", "kind", "side", "quantity", "target"),
    [
        (SignalDirection.LONG, PlanningOutcomeKind.INTENT_EMITTED, OrderSide.BUY, "10", "10"),
        (SignalDirection.FLAT, PlanningOutcomeKind.ALREADY_AT_TARGET, None, None, "0"),
        (SignalDirection.SHORT, PlanningOutcomeKind.INTENT_EMITTED, OrderSide.SELL, "10", "-10"),
    ],
)
def test_planning_maps_direction_to_target_and_optional_intent(
    direction: SignalDirection,
    kind: PlanningOutcomeKind,
    side: OrderSide | None,
    quantity: str | None,
    target: str,
) -> None:
    spec_set = _spec_set()
    _, _, _, signal = _signal(direction, spec_set=spec_set)
    ledger, planner = _ledger_and_planner(spec_set)
    result = planner.plan(signal)

    assert result.signal is signal
    assert result.portfolio_snapshot is ledger.snapshot
    assert result.target.target_position.text == target
    assert result.outcome.kind is kind
    if side is None:
        assert result.intent is None
        assert result.outcome.intent_id is None
    else:
        assert result.intent is not None
        assert result.intent.side is side
        assert result.intent.quantity.text == quantity
        assert result.intent.correlation_id == signal.signal_id
        assert result.intent.causation_id == result.target.target_id
    assert json.loads(canonical_portfolio_planning_result_bytes(result))["schema"] == (
        "ea.phase1-planning-result.v1"
    )
    assert len(portfolio_planning_result_digest(result).value) == 64


def test_planning_uses_target_minus_current_exactly() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(run_id=RUN_ID, spec_set=spec_set)
    ledger.apply_fill(_fill(spec_set, fill_sequence=1, quantity="4", resolved=True))
    _, _, _, long_signal = _signal(SignalDirection.LONG, spec_set=spec_set)
    _, planner = _ledger_and_planner(spec_set, ledger=ledger, target="10")
    long_result = planner.plan(long_signal)
    assert long_result.outcome.delta.text == "6"
    assert long_result.intent is not None
    assert long_result.intent.side is OrderSide.BUY
    assert long_result.intent.quantity.text == "6"


def test_unresolved_fill_precedes_zero_delta_and_emits_no_intent() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(run_id=RUN_ID, spec_set=spec_set)
    ledger.apply_fill(_fill(spec_set, fill_sequence=1, quantity="2", resolved=False))
    assert ledger.snapshot.unresolved_fills
    _, _, _, long_signal = _signal(SignalDirection.LONG, spec_set=spec_set)
    _, planner = _ledger_and_planner(spec_set, ledger=ledger, target="2")
    result = planner.plan(long_signal)
    assert result.outcome.delta.text == "0"
    assert result.outcome.kind is PlanningOutcomeKind.BLOCKED_UNRESOLVED_FILLS
    assert result.intent is None


def test_planning_replay_retains_exact_snapshot_after_ledger_progress() -> None:
    spec_set = _spec_set()
    _, _, _, signal = _signal(SignalDirection.LONG, spec_set=spec_set)
    ledger, planner = _ledger_and_planner(spec_set)
    first = planner.plan(signal)
    retained = first.portfolio_snapshot
    ledger.apply_fill(_fill(spec_set, fill_sequence=1, quantity="2", resolved=True))
    assert ledger.snapshot is not retained
    assert ledger.snapshot.snapshot_version == 1
    replay = planner.plan(signal)
    assert replay is first
    assert replay.portfolio_snapshot is retained
    assert replay.portfolio_snapshot.snapshot_version == 0
    assert planner.state.result_count == 1


def test_planning_conflict_halts_and_malformed_signal_does_not() -> None:
    spec_set = _spec_set()
    _, _, _, signal = _signal(SignalDirection.LONG, spec_set=spec_set)
    _, planner = _ledger_and_planner(spec_set)
    result = planner.plan(signal)
    malformed = object.__new__(StrategySignal)
    before = planner.state
    with pytest.raises(PortfolioPlanningError) as invalid:
        planner.plan(malformed)
    assert invalid.value.code is OutcomeCode.INVALID_TYPE
    assert planner.state is before

    changed = _forge_signal(signal, direction=SignalDirection.SHORT)
    with pytest.raises(PortfolioPlanningError) as conflict:
        planner.plan(changed)
    assert conflict.value.code is OutcomeCode.CONFLICTING_ID
    assert planner.state.halted is True
    first_conflict = planner.state.conflict
    assert planner.plan(signal) is result
    assert planner.state.conflict is first_conflict


def test_planning_non_monotone_new_signal_halts_and_retains_exact_replays() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    signal_authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )
    first_lease = runtime.pop()
    first = signal_authority.issue(
        cast(MarketDataEnvelope, first_lease.root),
        dispatch_sequence=first_lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    runtime.acknowledge(first_lease)
    skipped = runtime.pop()
    runtime.acknowledge(skipped)
    third_lease = runtime.pop()
    third = signal_authority.issue(
        cast(MarketDataEnvelope, third_lease.root),
        dispatch_sequence=third_lease.dispatch_sequence,
        direction=SignalDirection.SHORT,
    )
    _, planner = _ledger_and_planner(spec_set)
    retained = planner.plan(third)
    with pytest.raises(PortfolioPlanningError) as non_monotone:
        planner.plan(first)
    assert non_monotone.value.code is OutcomeCode.CONFLICTING_ID
    assert planner.state.halted is True
    assert planner.state.conflict is not None
    assert planner.state.conflict.conflict_kind is AuthorityConflictKind.NON_MONOTONE_DISPATCH
    assert planner.state.conflict.occupied_owner_id is None
    assert planner.plan(third) is retained


def test_policy_rejects_off_grid_and_foreign_spec_binding() -> None:
    spec_set = _spec_set(quantity_quantum="2")
    with pytest.raises(PortfolioPlanningError) as off_grid:
        _policy(spec_set, target="3")
    assert off_grid.value.code is OutcomeCode.NOT_QUANTIZED
    foreign = _spec_set(quantity_quantum="1")
    policy = _policy(spec_set, target="4")
    ledger = create_portfolio_ledger(run_id=RUN_ID, spec_set=foreign)
    with pytest.raises(PortfolioPlanningError) as mismatch:
        create_portfolio_planning_authority(
            run_id=RUN_ID,
            ledger=ledger,
            spec_set=foreign,
            policy=policy,
            execution_policy=EXECUTION_POLICY,
        )
    assert mismatch.value.code is OutcomeCode.CONFLICTING_ID


def test_signal_uint64_max_publishes_null_then_exhausts_without_mutation() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    first_lease = runtime.pop()
    first_root = cast(MarketDataEnvelope, first_lease.root)
    authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )
    prior = authority._state
    authority._state = signal_authority_module._AuthorityState(
        public=_create_strategy_signal_authority_state(
            run_id=RUN_ID,
            halted=False,
            signal_next=(1 << 64) - 1,
            last_new_dispatch_sequence=None,
            issuance_count=0,
            conflict=None,
        ),
        replay_by_dispatch=prior.replay_by_dispatch,
    )
    signal = authority.issue(
        first_root,
        dispatch_sequence=first_lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    assert signal.signal_id.owner_sequence == (1 << 64) - 1
    assert authority.state.signal_next is None

    runtime.acknowledge(first_lease)
    second_lease = runtime.pop()
    second_root = cast(MarketDataEnvelope, second_lease.root)
    before = authority.state
    with pytest.raises(StrategyContractError) as exhausted:
        authority.issue(
            second_root,
            dispatch_sequence=second_lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE
    assert authority.state is before


def test_planning_uint64_max_consumes_target_and_intent_then_exhausts() -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    signal_authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )
    first_lease = runtime.pop()
    first_signal = signal_authority.issue(
        cast(MarketDataEnvelope, first_lease.root),
        dispatch_sequence=first_lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    runtime.acknowledge(first_lease)
    second_lease = runtime.pop()
    second_signal = signal_authority.issue(
        cast(MarketDataEnvelope, second_lease.root),
        dispatch_sequence=second_lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    _, planner = _ledger_and_planner(spec_set)
    prior = planner._state
    planner._state = planning_module._AuthorityState(
        public=_create_portfolio_planning_authority_state(
            run_id=RUN_ID,
            halted=False,
            target_next=(1 << 64) - 1,
            intent_next=(1 << 64) - 1,
            last_new_signal_dispatch_sequence=None,
            result_count=0,
            conflict=None,
        ),
        replay_by_signal_id=prior.replay_by_signal_id,
    )
    result = planner.plan(first_signal)
    assert result.target.target_id.owner_sequence == (1 << 64) - 1
    assert result.intent is not None
    assert result.intent.intent_id.owner_sequence == (1 << 64) - 1
    assert planner.state.target_next is None
    assert planner.state.intent_next is None

    before = planner.state
    with pytest.raises(PortfolioPlanningError) as exhausted:
        planner.plan(second_signal)
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE
    assert planner.state is before


def test_unexpected_verifier_and_prepublication_failures_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_set = _spec_set()
    runtime = _runtime(spec_set)
    lease = runtime.pop()
    root = cast(MarketDataEnvelope, lease.root)

    class ExplodingVerifier:
        def verify_active_market_dispatch(
            self,
            market_root: MarketDataEnvelope,
            *,
            dispatch_sequence: int,
        ) -> ActiveMarketDispatchProof:
            raise RuntimeError("secret subordinate detail")

    authority = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=ExplodingVerifier(),
    )
    before = authority.state
    with pytest.raises(StrategyContractError) as failed:
        authority.issue(
            root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert failed.value.code is OutcomeCode.INVALID_TYPE
    assert str(failed.value) == "active market verifier operation contract failed"
    assert authority.state is before

    valid = create_strategy_signal_authority(
        run_id=RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )
    valid_before = valid.state
    monkeypatch.setattr(
        signal_authority_module,
        "strategy_signal_authority_state_digest",
        lambda state: (_ for _ in ()).throw(RuntimeError("prepublish")),
    )
    with pytest.raises(StrategyContractError) as prepublication:
        valid.issue(
            root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
    assert prepublication.value.code is OutcomeCode.INVALID_TYPE
    assert str(prepublication.value) == "strategy signal state publication preflight failed"
    assert valid.state is valid_before


def test_conflict_and_intent_publication_failures_are_classified_and_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_set = _spec_set()
    runtime, lease, signal_authority, _ = _signal(spec_set=spec_set)
    root = cast(MarketDataEnvelope, lease.root)
    signal_before = signal_authority.state
    monkeypatch.setattr(
        signal_authority_module,
        "strategy_signal_authority_state_digest",
        lambda state: (_ for _ in ()).throw(RuntimeError("conflict secret")),
    )
    with pytest.raises(StrategyContractError) as signal_failure:
        signal_authority.issue(
            root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.SHORT,
        )
    assert signal_failure.value.code is OutcomeCode.INVALID_TYPE
    assert str(signal_failure.value) == "strategy conflict publication preflight failed"
    assert signal_authority.state is signal_before
    runtime.acknowledge(lease)

    monkeypatch.undo()
    _, _, _, signal = _signal(spec_set=spec_set)
    _, planner = _ledger_and_planner(spec_set)
    planner_before = planner.state
    monkeypatch.setattr(
        planning_module,
        "order_intent_digest",
        lambda intent: (_ for _ in ()).throw(RuntimeError("intent secret")),
    )
    with pytest.raises(PortfolioPlanningError) as intent_failure:
        planner.plan(signal)
    assert intent_failure.value.code is OutcomeCode.INVALID_TYPE
    assert str(intent_failure.value) == "portfolio intent publication preflight failed"
    assert planner.state is planner_before


def test_planning_snapshot_and_prepublication_failures_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_set = _spec_set()
    _, _, _, signal = _signal(spec_set=spec_set)
    _, planner = _ledger_and_planner(spec_set)
    before = planner.state
    monkeypatch.setattr(
        planning_module,
        "canonical_portfolio_snapshot_bytes",
        lambda snapshot: (_ for _ in ()).throw(RuntimeError("snapshot secret")),
    )
    with pytest.raises(PortfolioPlanningError) as snapshot_failure:
        planner.plan(signal)
    assert snapshot_failure.value.code is OutcomeCode.INVALID_TYPE
    assert str(snapshot_failure.value) == "portfolio snapshot canonicalization failed"
    assert planner.state is before

    monkeypatch.undo()
    result_before = planner.state
    monkeypatch.setattr(
        planning_module,
        "portfolio_planning_result_digest",
        lambda result: (_ for _ in ()).throw(RuntimeError("prepublish")),
    )
    with pytest.raises(PortfolioPlanningError) as prepublication:
        planner.plan(signal)
    assert prepublication.value.code is OutcomeCode.INVALID_TYPE
    assert str(prepublication.value) == "portfolio planning publication preflight failed"
    assert planner.state is result_before


def test_risk_handoff_rejects_substitution_but_direct_risk_replay_keeps_adr0011() -> None:
    spec_set = _spec_set()
    _, _, _, signal = _signal(spec_set=spec_set)
    ledger = create_portfolio_ledger(run_id=RUN_ID, spec_set=spec_set)
    ledger.apply_fill(_fill(spec_set, fill_sequence=1, quantity="2", resolved=True))
    _, planner = _ledger_and_planner(spec_set, ledger=ledger)
    planning_result = planner.plan(signal)
    assert planning_result.intent is not None
    retained = planning_result.portfolio_snapshot
    validate_portfolio_planning_risk_handoff(
        planning_result,
        intent=planning_result.intent,
        portfolio_snapshot=retained,
    )
    risk_policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.strategy-planning-risk.v1"),
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        instrument_limits=(
            InstrumentRiskLimit(
                instrument=INSTRUMENT,
                maximum_order_quantity=CanonicalDecimal("20"),
                maximum_absolute_position=CanonicalDecimal("20"),
            ),
        ),
    )
    risk = create_phase1_risk_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        policy=risk_policy,
    )
    first = risk.evaluate(planning_result.intent, retained)
    ledger.apply_fill(_fill(spec_set, fill_sequence=2, quantity="2", resolved=True))
    replacement = ledger.snapshot
    assert replacement is not retained
    same_version_replacement = replace(
        retained,
        position_balances=(
            PositionBalance(
                instrument=INSTRUMENT,
                quantity_quantum=CanonicalDecimal("1"),
                quantity=CanonicalDecimal("1"),
            ),
        ),
    )
    assert same_version_replacement.snapshot_version == retained.snapshot_version
    with pytest.raises(PortfolioPlanningError) as same_version_substitution:
        validate_portfolio_planning_risk_handoff(
            planning_result,
            intent=planning_result.intent,
            portfolio_snapshot=same_version_replacement,
        )
    assert same_version_substitution.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(PortfolioPlanningError) as substituted:
        validate_portfolio_planning_risk_handoff(
            planning_result,
            intent=planning_result.intent,
            portfolio_snapshot=replacement,
        )
    assert substituted.value.code is OutcomeCode.CONFLICTING_ID
    assert risk.evaluate(planning_result.intent, replacement) is first
