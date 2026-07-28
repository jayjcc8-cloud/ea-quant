from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

import pytest

from ea.core import (
    EXECUTION_FACT_ANOMALY_RANKS,
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactAction,
    ExecutionFactAnomaly,
    ExecutionFactKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    ExecutionStateError,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    Order,
    OrderIntent,
    OrderProjectionState,
    OrderResolutionKeyKind,
    OrderSide,
    OutcomeCode,
    Phase1RiskPolicy,
    PortfolioSnapshot,
    PriceDomain,
    RiskPolicyId,
    RunId,
    RuntimeOrderingError,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    TargetLineageRef,
    VenueId,
    VenueOrderId,
    build_instrument_spec_set,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_execution_fact_processing_outcome_bytes,
    canonical_order_projection_snapshot_bytes,
    create_execution_fact_ingress,
    create_execution_fact_processing_outcome,
    create_lifecycle_execution_fact,
    create_order_intent,
    create_order_projection_snapshot,
    create_phase1_risk_policy,
    create_submission_query_execution_fact,
    create_trade_execution_fact,
    decode_execution_fact_processing_outcome,
    decode_order_projection_snapshot,
    instrument_spec_set_digest,
    order_projection_snapshot_digest,
    prepare_bounded_runtime_roots,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.execution import (
    ExecutionFactAuthorityError,
    Phase1ExecutionFactAuthority,
    Phase1OrderAuthority,
    create_phase1_execution_fact_authority,
    create_phase1_order_authority,
)
from ea.execution import fact_authority as fact_authority_module
from ea.risk import create_phase1_risk_authority
from ea.runtime import (
    DeterministicRootQueue,
    Phase1ExecutionFactIngressAuthority,
    RuntimeDispatchLease,
    create_deterministic_root_queue,
    create_phase1_execution_fact_ingress_authority,
)
from ea.runtime import ingress as ingress_module
from ea.runtime import queue as queue_module

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
SOURCE = SourceNamespace("sim.primary")
TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
PROVENANCE = FactProvenance(
    FactProvenanceId("phase1.simulator.v1"),
    Sha256Digest("2" * 64),
)


def _id(
    owner: EconomicOwnerKind,
    sequence: int,
    *,
    run_id: RunId = RUN_ID,
) -> EconomicId:
    return EconomicId(run_id, owner, sequence)


def _spec_set() -> InstrumentExecutionSpecSet:
    return build_instrument_spec_set(
        InstrumentSpecSetId("phase1.test.v1"),
        (
            InstrumentExecutionSpec(
                instrument=INSTRUMENT,
                specification_id=InstrumentSpecId("xnas.aapl.v1"),
                price_quantum=CanonicalDecimal("0.01"),
                quantity_quantum=CanonicalDecimal("1"),
                settlement_currency=SettlementCurrency("USD"),
                currency_quantum=CanonicalDecimal("0.01"),
                contract_multiplier=CanonicalDecimal("1"),
                price_domain=PriceDomain.POSITIVE,
            ),
        ),
    )


def _policy(spec_set: InstrumentExecutionSpecSet) -> Phase1RiskPolicy:
    return create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        instrument_limits=(
            InstrumentRiskLimit(
                instrument=INSTRUMENT,
                maximum_order_quantity=CanonicalDecimal("10"),
                maximum_absolute_position=CanonicalDecimal("100"),
            ),
        ),
    )


def _snapshot(spec_set: InstrumentExecutionSpecSet) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        run_id=RUN_ID,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
        snapshot_version=0,
        ledger_sequence=0,
        last_entry_id=None,
        last_transaction_sha256=None,
        cash_balances=(),
        position_balances=(),
        rounding_balances=(),
        unresolved_fills=(),
    )


def _intent(
    spec_set: InstrumentExecutionSpecSet,
    *,
    sequence: int,
    quantity: str = "2",
) -> OrderIntent:
    return create_order_intent(
        run_id=RUN_ID,
        intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, sequence),
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, sequence),
        target_lineage=TargetLineageRef(
            _id(EconomicOwnerKind.PORTFOLIO_TARGET, sequence),
            Sha256Digest(f"{sequence:x}".rjust(64, "0")),
        ),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(quantity),
        portfolio_snapshot_version=0,
        causal_root_available_at=TIME,
        dispatch_sequence=sequence,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
    )


def _orders(
    count: int = 1,
    *,
    quantity: str = "2",
) -> tuple[InstrumentExecutionSpecSet, Phase1OrderAuthority, tuple[Order, ...]]:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = create_phase1_risk_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        policy=policy,
    )
    authority = create_phase1_order_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        risk_policy=policy,
        risk_result_verifier=risk,
    )
    orders: list[Order] = []
    for sequence in range(1, count + 1):
        intent = _intent(spec_set, sequence=sequence, quantity=quantity)
        result = risk.evaluate(intent, _snapshot(spec_set))
        orders.append(authority.create_order(intent, result))
    return spec_set, authority, tuple(orders)


def _lifecycle(
    order: Order | None,
    *,
    kind: ExecutionFactKind = ExecutionFactKind.ACKNOWLEDGEMENT,
    external_id: str = "lifecycle-1",
    occurred_at: datetime = TIME,
    client_submission_key: Sha256Digest | None = None,
    venue_order_id: VenueOrderId | None = None,
) -> Any:
    return create_lifecycle_execution_fact(
        kind=kind,
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId(external_id),
        occurred_at=occurred_at,
        provenance=PROVENANCE,
        instrument=None if order is None else order.instrument,
        client_submission_key=(
            client_submission_key
            if client_submission_key is not None
            else (None if order is None else order.client_submission_key)
        ),
        venue_order_id=venue_order_id,
        order_id=None if order is None else order.order_id,
        correlation_id=None if order is None else order.correlation_id,
        causation_id=None if order is None else order.order_id,
    )


def _trade(
    spec_set: InstrumentExecutionSpecSet,
    order: Order | None,
    *,
    external_id: str,
    quantity: str,
    occurred_at: datetime = TIME,
    run_id: RunId = RUN_ID,
) -> Any:
    return create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId(external_id),
        occurred_at=occurred_at,
        provenance=PROVENANCE,
        spec_set=spec_set,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(quantity),
        price=CanonicalDecimal("100"),
        client_submission_key=None if order is None else order.client_submission_key,
        order_id=(
            None
            if order is None
            else EconomicId(run_id, order.order_id.owner_kind, order.order_id.owner_sequence)
        ),
        correlation_id=(
            None
            if order is None
            else EconomicId(
                run_id,
                order.correlation_id.owner_kind,
                order.correlation_id.owner_sequence,
            )
        ),
        causation_id=(
            None
            if order is None
            else EconomicId(run_id, order.order_id.owner_kind, order.order_id.owner_sequence)
        ),
    )


def _ingress(
    fact: Any,
    *,
    sequence: int,
    available_at: datetime | None = None,
) -> Any:
    return create_execution_fact_ingress(
        available_at=fact.occurred_at if available_at is None else available_at,
        source_namespace=SOURCE,
        ingress_sequence=sequence,
        fact=fact,
    )


def _runtime(
    spec_set: InstrumentExecutionSpecSet,
    order_authority: Phase1OrderAuthority,
    ingresses: tuple[Any, ...],
) -> tuple[
    Phase1ExecutionFactIngressAuthority,
    DeterministicRootQueue,
    Phase1ExecutionFactAuthority,
]:
    source = create_phase1_execution_fact_ingress_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        source_namespace=SOURCE,
    )
    for ingress in ingresses:
        source.register_ingress(ingress)
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots(ingresses),
        fact_issuance_verifiers=(source,),
    )
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=order_authority,
        dispatch_verifier=queue,
    )
    return source, queue, authority


def _process_next(
    queue: DeterministicRootQueue,
    authority: Phase1ExecutionFactAuthority,
) -> tuple[RuntimeDispatchLease, Any]:
    lease = queue.pop()
    outcome = authority.process_ingress(cast(Any, lease.root))
    return lease, outcome


def _query(
    order: Order,
    *,
    external_id: str,
    outcome_code: OutcomeCode,
    occurred_at: datetime,
    sequence: int,
    venue_order_id: VenueOrderId | None = None,
) -> Any:
    fact = create_submission_query_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId(external_id),
        occurred_at=occurred_at,
        provenance=PROVENANCE,
        outcome_code=outcome_code,
        subject_order=order,
        venue_order_id=venue_order_id,
    )
    return _ingress(
        fact,
        sequence=sequence,
        available_at=occurred_at,
    )


def _clone_slots(value: Any, **changes: object) -> Any:
    replacement = object.__new__(type(value))
    for name in cast(tuple[str, ...], type(value).__slots__):
        object.__setattr__(
            replacement,
            name,
            changes.get(name, getattr(value, name)),
        )
    return replacement


def _raise_injected(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("injected pre-publication failure")


def _assert_fact_state_unchanged(
    authority: Phase1ExecutionFactAuthority,
    state: Any,
) -> None:
    assert authority._state is state
    for field in (
        "ingress_index",
        "fact_index",
        "fill_index",
        "order_index",
        "client_key_index",
        "venue_index",
        "projection_index",
        "observed_quantity",
        "ingresses",
        "outcomes",
        "first_facts",
        "fills",
        "projections",
    ):
        assert getattr(authority._state, field) is getattr(state, field)


class _IssuanceStub:
    def __init__(
        self,
        spec_set: InstrumentExecutionSpecSet,
        *,
        run_id: object = RUN_ID,
        source_namespace: object = SOURCE,
        response: object = True,
        error: Exception | None = None,
    ) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.source_namespace = source_namespace
        self.response = response
        self.error = error

    def has_issued_ingress(
        self,
        *,
        ingress_identity: object,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> bool:
        assert ingress_identity is not None
        assert type(canonical_ingress_bytes) is bytes
        assert type(canonical_fact_bytes) is bytes
        if self.error is not None:
            raise self.error
        return cast(bool, self.response)


class _OrderVerifierStub:
    def __init__(
        self,
        spec_set: InstrumentExecutionSpecSet,
        *,
        run_id: object = RUN_ID,
        response: object = None,
        error: Exception | None = None,
    ) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.response = response
        self.error = error

    def resolve_issued_order_by_id(self, _order_id: EconomicId) -> Order | None:
        if self.error is not None:
            raise self.error
        return cast(Order | None, self.response)

    def resolve_issued_order_by_client_submission_key(
        self,
        _client_submission_key: Sha256Digest,
    ) -> Order | None:
        if self.error is not None:
            raise self.error
        return cast(Order | None, self.response)


class _DispatchStub:
    def __init__(
        self,
        spec_set: InstrumentExecutionSpecSet,
        *,
        run_id: object = RUN_ID,
        response: object = 1,
        error: Exception | None = None,
    ) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.response = response
        self.error = error

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: object,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None:
        assert ingress_identity is not None
        assert type(canonical_ingress_bytes) is bytes
        assert type(canonical_fact_bytes) is bytes
        if self.error is not None:
            raise self.error
        return cast(int | None, self.response)


def test_source_issuance_is_factory_only_replay_stable_and_conflict_atomic() -> None:
    spec_set, _orders_authority, orders = _orders()
    first = _ingress(_lifecycle(orders[0]), sequence=1)
    conflict = _ingress(
        _lifecycle(
            orders[0],
            kind=ExecutionFactKind.REJECTION,
            external_id="other",
        ),
        sequence=1,
    )
    with pytest.raises(TypeError):
        Phase1ExecutionFactIngressAuthority()
    source = create_phase1_execution_fact_ingress_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        source_namespace=SOURCE,
    )

    issued = source.register_ingress(first)
    assert source.register_ingress(first) is issued
    state = source._state
    with pytest.raises(Exception) as error:
        source.register_ingress(conflict)
    assert cast(Any, error.value).code is OutcomeCode.CONFLICTING_ID
    assert source._state is state
    assert source.has_issued_ingress(
        ingress_identity=first.identity,
        canonical_ingress_bytes=canonical_execution_fact_ingress_bytes(first),
        canonical_fact_bytes=canonical_execution_fact_bytes(first.fact),
    )


def test_source_rejects_pre_occurrence_availability_without_publication() -> None:
    spec_set, _orders_authority, orders = _orders()
    valid = _ingress(_lifecycle(orders[0]), sequence=1)
    invalid = _clone_slots(
        valid,
        available_at=valid.fact.occurred_at - timedelta(microseconds=1),
    )
    source = create_phase1_execution_fact_ingress_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        source_namespace=SOURCE,
    )
    state = source._state

    with pytest.raises(RuntimeOrderingError) as error:
        source.register_ingress(invalid)

    assert error.value.code is OutcomeCode.OUT_OF_RANGE
    assert source._state is state
    assert source.issued_ingresses == ()


def test_queue_requires_source_issuance_and_one_exact_live_lease() -> None:
    spec_set, _orders_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    source = create_phase1_execution_fact_ingress_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        source_namespace=SOURCE,
    )
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots((ingress,)),
        fact_issuance_verifiers=(source,),
    )
    assert (
        queue.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=b"x",
            canonical_fact_bytes=b"y",
        )
        is None
    )
    with pytest.raises(Exception) as unissued:
        queue.pop()
    assert cast(Any, unissued.value).code is OutcomeCode.CONFLICTING_ID
    assert queue.remaining == 1

    source.register_ingress(ingress)
    lease = queue.pop()
    assert lease.root is ingress
    assert lease.dispatch_sequence == 1
    with pytest.raises(FrozenInstanceError):
        cast(Any, lease)._root = object()
    with pytest.raises(FrozenInstanceError):
        cast(Any, lease)._dispatch_sequence = 999
    object.__setattr__(lease, "_dispatch_sequence", 999)
    assert (
        queue.resolve_active_issued_fact_dispatch(
            ingress_identity=cast(Any, ingress).identity,
            canonical_ingress_bytes=canonical_execution_fact_ingress_bytes(cast(Any, ingress)),
            canonical_fact_bytes=canonical_execution_fact_bytes(cast(Any, ingress).fact),
        )
        == 1
    )
    with pytest.raises(Exception) as active:
        queue.pop()
    assert cast(Any, active.value).code is OutcomeCode.CONFLICTING_ID
    wrong = object.__new__(RuntimeDispatchLease)
    with pytest.raises(Exception) as wrong_lease:
        queue.acknowledge(wrong)
    assert cast(Any, wrong_lease.value).code is OutcomeCode.CONFLICTING_ID
    queue.acknowledge(lease)
    assert queue.acknowledged_fact_dispatch_count == 1
    assert queue._state.acknowledged_fact_dispatches[-1].dispatch_sequence == 1
    with pytest.raises(Exception) as repeated:
        queue.acknowledge(lease)
    assert cast(Any, repeated.value).code is OutcomeCode.CONFLICTING_ID


def test_queue_construction_rejects_malformed_missing_extra_duplicate_and_ordered_ports() -> None:
    spec_set, _orders_authority, orders = _orders()
    primary_ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    primary_plan = prepare_bounded_runtime_roots((primary_ingress,))
    valid = _IssuanceStub(spec_set)
    with pytest.raises(RuntimeOrderingError) as non_tuple:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=spec_set,
            plan=primary_plan,
            fact_issuance_verifiers=cast(Any, [valid]),
        )
    assert non_tuple.value.code is OutcomeCode.INVALID_TYPE
    with pytest.raises(RuntimeOrderingError) as missing:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=spec_set,
            plan=primary_plan,
            fact_issuance_verifiers=(),
        )
    assert missing.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(RuntimeOrderingError) as wrong_run:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=spec_set,
            plan=primary_plan,
            fact_issuance_verifiers=(cast(Any, _IssuanceStub(spec_set, run_id=OTHER_RUN_ID)),),
        )
    assert wrong_run.value.code is OutcomeCode.CONFLICTING_ID

    secondary_namespace = SourceNamespace("sim.secondary")
    secondary_fact = create_lifecycle_execution_fact(
        kind=ExecutionFactKind.ACKNOWLEDGEMENT,
        source_namespace=secondary_namespace,
        dedup_identity=ExternalFactId("secondary"),
        occurred_at=TIME,
        provenance=PROVENANCE,
    )
    secondary_ingress = create_execution_fact_ingress(
        available_at=TIME,
        source_namespace=secondary_namespace,
        ingress_sequence=1,
        fact=secondary_fact,
    )
    secondary = _IssuanceStub(
        spec_set,
        source_namespace=secondary_namespace,
    )
    with pytest.raises(RuntimeOrderingError) as extra:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=spec_set,
            plan=primary_plan,
            fact_issuance_verifiers=cast(Any, (valid, secondary)),
        )
    assert extra.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(RuntimeOrderingError) as duplicate:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=spec_set,
            plan=primary_plan,
            fact_issuance_verifiers=cast(Any, (valid, valid)),
        )
    assert duplicate.value.code is OutcomeCode.CONFLICTING_ID
    two_source_plan = prepare_bounded_runtime_roots((primary_ingress, secondary_ingress))
    with pytest.raises(RuntimeOrderingError) as out_of_order:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=spec_set,
            plan=two_source_plan,
            fact_issuance_verifiers=cast(Any, (secondary, valid)),
        )
    assert out_of_order.value.code is OutcomeCode.CONFLICTING_ID


@pytest.mark.parametrize(
    ("response", "raised", "expected_code"),
    [
        (False, None, OutcomeCode.CONFLICTING_ID),
        (None, None, OutcomeCode.INVALID_TYPE),
        (1, None, OutcomeCode.INVALID_TYPE),
        (True, AttributeError("bad operation"), OutcomeCode.INVALID_TYPE),
        (True, TypeError("bad operation"), OutcomeCode.INVALID_TYPE),
    ],
)
def test_queue_issuance_verifier_result_matrix_is_atomic(
    response: object,
    raised: Exception | None,
    expected_code: OutcomeCode,
) -> None:
    spec_set, _orders_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots((ingress,)),
        fact_issuance_verifiers=(
            cast(
                Any,
                _IssuanceStub(
                    spec_set,
                    response=response,
                    error=raised,
                ),
            ),
        ),
    )
    state = queue._state

    with pytest.raises(RuntimeOrderingError) as error:
        queue.pop()
    assert error.value.code is expected_code
    assert queue._state is state


def test_queue_unexpected_verifier_exception_propagates_without_mutation() -> None:
    spec_set, _orders_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots((ingress,)),
        fact_issuance_verifiers=(
            cast(
                Any,
                _IssuanceStub(
                    spec_set,
                    error=RuntimeError("unexpected verifier failure"),
                ),
            ),
        ),
    )
    state = queue._state

    with pytest.raises(RuntimeError, match="unexpected verifier failure"):
        queue.pop()
    assert queue._state is state


def test_acknowledgement_projects_order_and_exact_ingress_replays_after_ack() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(
        _lifecycle(
            orders[0],
            venue_order_id=VenueOrderId("venue-order-1"),
        ),
        sequence=1,
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    with pytest.raises(ExecutionFactAuthorityError) as inactive:
        authority.process_ingress(ingress)
    assert inactive.value.code is OutcomeCode.CONFLICTING_ID

    lease, outcome = _process_next(queue, authority)
    assert outcome.action is ExecutionFactAction.ACCEPTED
    assert outcome.anomalies == ()
    assert outcome.resolved_order_id == orders[0].order_id
    assert tuple(binding.key_kind for binding in outcome.order_resolutions) == (
        OrderResolutionKeyKind.ORDER_ID,
        OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY,
        OrderResolutionKeyKind.VENUE_ORDER_ID,
    )
    projection = authority.projection_for_order(orders[0].order_id)
    assert projection is not None
    assert projection.projection_state is OrderProjectionState.ACKNOWLEDGED
    assert projection.projected_executed_quantity == CanonicalDecimal("0")
    assert projection.venue_order_id == VenueOrderId("venue-order-1")
    queue.acknowledge(lease)

    state = authority._state
    assert authority.process_ingress(ingress) is outcome
    assert authority._state is state


def test_fact_authority_construction_verifier_matrix() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    _source, queue, _authority = _runtime(
        spec_set,
        order_authority,
        (ingress,),
    )
    malformed_pairs = (
        (object(), queue),
        (order_authority, object()),
    )
    for order_verifier, dispatch_verifier in malformed_pairs:
        with pytest.raises(ExecutionFactAuthorityError) as malformed:
            create_phase1_execution_fact_authority(
                run_id=RUN_ID,
                spec_set=spec_set,
                order_verifier=cast(Any, order_verifier),
                dispatch_verifier=cast(Any, dispatch_verifier),
            )
        assert malformed.value.code is OutcomeCode.INVALID_TYPE
    with pytest.raises(ExecutionFactAuthorityError) as wrong_order_run:
        create_phase1_execution_fact_authority(
            run_id=RUN_ID,
            spec_set=spec_set,
            order_verifier=cast(
                Any,
                _OrderVerifierStub(spec_set, run_id=OTHER_RUN_ID),
            ),
            dispatch_verifier=queue,
        )
    assert wrong_order_run.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(ExecutionFactAuthorityError) as wrong_dispatch_run:
        create_phase1_execution_fact_authority(
            run_id=RUN_ID,
            spec_set=spec_set,
            order_verifier=order_authority,
            dispatch_verifier=cast(
                Any,
                _DispatchStub(spec_set, run_id=OTHER_RUN_ID),
            ),
        )
    assert wrong_dispatch_run.value.code is OutcomeCode.CONFLICTING_ID


@pytest.mark.parametrize(
    ("response", "raised", "expected_code"),
    [
        (None, None, OutcomeCode.CONFLICTING_ID),
        ("1", None, OutcomeCode.INVALID_TYPE),
        (True, None, OutcomeCode.INVALID_TYPE),
        (0, None, OutcomeCode.OUT_OF_RANGE),
        (1, AttributeError("dispatch shape"), OutcomeCode.INVALID_TYPE),
        (1, TypeError("dispatch shape"), OutcomeCode.INVALID_TYPE),
    ],
)
def test_dispatch_verifier_processing_matrix_is_atomic(
    response: object,
    raised: Exception | None,
    expected_code: OutcomeCode,
) -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=order_authority,
        dispatch_verifier=cast(
            Any,
            _DispatchStub(spec_set, response=response, error=raised),
        ),
    )
    state = authority._state

    with pytest.raises(ExecutionFactAuthorityError) as error:
        authority.process_ingress(ingress)
    assert error.value.code is expected_code
    _assert_fact_state_unchanged(authority, state)


def test_unexpected_dispatch_verifier_exception_propagates_atomically() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=order_authority,
        dispatch_verifier=cast(
            Any,
            _DispatchStub(
                spec_set,
                error=RuntimeError("unexpected dispatch failure"),
            ),
        ),
    )
    state = authority._state

    with pytest.raises(RuntimeError, match="unexpected dispatch failure"):
        authority.process_ingress(ingress)
    _assert_fact_state_unchanged(authority, state)


@pytest.mark.parametrize(
    ("response", "raised", "expected_code"),
    [
        ("not-order", None, OutcomeCode.INVALID_TYPE),
        (None, AttributeError("resolver shape"), OutcomeCode.INVALID_TYPE),
        (None, TypeError("resolver shape"), OutcomeCode.INVALID_TYPE),
    ],
)
def test_order_resolver_processing_matrix_is_atomic(
    response: object,
    raised: Exception | None,
    expected_code: OutcomeCode,
) -> None:
    spec_set, _order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=cast(
            Any,
            _OrderVerifierStub(spec_set, response=response, error=raised),
        ),
        dispatch_verifier=cast(Any, _DispatchStub(spec_set)),
    )
    state = authority._state

    with pytest.raises(ExecutionFactAuthorityError) as error:
        authority.process_ingress(ingress)
    assert error.value.code is expected_code
    _assert_fact_state_unchanged(authority, state)


def test_unexpected_order_resolver_exception_propagates_atomically() -> None:
    spec_set, _order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=cast(
            Any,
            _OrderVerifierStub(
                spec_set,
                error=RuntimeError("unexpected resolver failure"),
            ),
        ),
        dispatch_verifier=cast(Any, _DispatchStub(spec_set)),
    )
    state = authority._state

    with pytest.raises(RuntimeError, match="unexpected resolver failure"):
        authority.process_ingress(ingress)
    _assert_fact_state_unchanged(authority, state)


def test_distinct_redelivery_is_duplicate_and_stable_conflict_publishes_halt() -> None:
    spec_set, order_authority, orders = _orders()
    accepted_fact = _lifecycle(
        orders[0],
        kind=ExecutionFactKind.REJECTION,
        external_id="stable-1",
    )
    duplicate = _ingress(accepted_fact, sequence=2)
    accepted = _ingress(accepted_fact, sequence=1)
    conflicting = _ingress(
        _lifecycle(
            orders[0],
            kind=ExecutionFactKind.ACKNOWLEDGEMENT,
            external_id="stable-1",
        ),
        sequence=3,
    )
    _source, queue, authority = _runtime(
        spec_set,
        order_authority,
        (accepted, duplicate, conflicting),
    )
    first_lease, first = _process_next(queue, authority)
    queue.acknowledge(first_lease)
    duplicate_lease, second = _process_next(queue, authority)
    queue.acknowledge(duplicate_lease)
    conflict_lease, third = _process_next(queue, authority)

    assert first.action is ExecutionFactAction.ACCEPTED
    assert second.action is ExecutionFactAction.DUPLICATE
    assert second.fill_id is None
    assert second.order_resolutions == ()
    assert third.action is ExecutionFactAction.CONFLICT
    assert third.halt_requested
    assert authority.halt_requested
    assert authority.next_fill_sequence == 1
    assert len(authority.first_facts) == 1
    assert len(authority.outcomes) == 3
    queue.acknowledge(conflict_lease)


def test_coherent_trades_preserve_full_overfill_and_bound_projection() -> None:
    spec_set, order_authority, orders = _orders(quantity="2")
    first = _ingress(
        _trade(spec_set, orders[0], external_id="trade-1", quantity="1"),
        sequence=1,
    )
    second = _ingress(
        _trade(spec_set, orders[0], external_id="trade-2", quantity="2"),
        sequence=2,
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (first, second))
    first_lease, first_outcome = _process_next(queue, authority)
    queue.acknowledge(first_lease)
    second_lease, second_outcome = _process_next(queue, authority)

    assert first_outcome.action is ExecutionFactAction.ACCEPTED
    first_projection = authority.projections[0]
    assert first_projection.projection_state is OrderProjectionState.PARTIALLY_FILLED
    assert first_projection.projected_executed_quantity == CanonicalDecimal("1")
    assert second_outcome.action is ExecutionFactAction.UNRESOLVED
    assert second_outcome.anomalies == (ExecutionFactAnomaly.OVERFILL,)
    assert authority.fills[-1].quantity == CanonicalDecimal("2")
    assert authority.observed_quantity_for_order(orders[0].order_id) == CanonicalDecimal("3")
    final_projection = authority.projection_for_order(orders[0].order_id)
    assert final_projection is not None
    assert final_projection.projection_state is OrderProjectionState.FILLED
    assert final_projection.projected_executed_quantity == orders[0].quantity
    queue.acknowledge(second_lease)


@pytest.mark.parametrize(
    ("evidence", "expected_state", "expected_anomalies", "changes_projection"),
    [
        ("expiry", OrderProjectionState.EXPIRED, (), True),
        ("cancellation", OrderProjectionState.CANCELLED, (), True),
        (
            "rejection",
            OrderProjectionState.PARTIALLY_FILLED,
            (ExecutionFactAnomaly.PROJECTION_TRANSITION_CONFLICT,),
            False,
        ),
        (
            "query_rejected",
            OrderProjectionState.PARTIALLY_FILLED,
            (ExecutionFactAnomaly.PROJECTION_TRANSITION_CONFLICT,),
            False,
        ),
    ],
)
def test_partial_fill_terminal_evidence_preserves_cumulative_quantity(
    evidence: str,
    expected_state: OrderProjectionState,
    expected_anomalies: tuple[ExecutionFactAnomaly, ...],
    changes_projection: bool,
) -> None:
    spec_set, order_authority, orders = _orders(quantity="2")
    order = orders[0]
    trade = _ingress(
        _trade(
            spec_set,
            order,
            external_id=f"partial-before-{evidence}",
            quantity="1",
        ),
        sequence=1,
    )
    second_time = TIME + timedelta(seconds=1)
    if evidence == "query_rejected":
        terminal = _query(
            order,
            external_id="query-rejected-after-partial",
            outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_REJECTED,
            occurred_at=second_time,
            sequence=2,
        )
    else:
        kind = {
            "expiry": ExecutionFactKind.EXPIRY,
            "cancellation": ExecutionFactKind.CANCELLATION,
            "rejection": ExecutionFactKind.REJECTION,
        }[evidence]
        terminal = _ingress(
            _lifecycle(
                order,
                kind=kind,
                external_id=f"{evidence}-after-partial",
                occurred_at=second_time,
            ),
            sequence=2,
            available_at=second_time,
        )
    _source, queue, authority = _runtime(
        spec_set,
        order_authority,
        (trade, terminal),
    )
    trade_lease, _trade_outcome = _process_next(queue, authority)
    before = authority.projection_for_order(order.order_id)
    assert before is not None
    assert before.projection_state is OrderProjectionState.PARTIALLY_FILLED
    assert before.projected_executed_quantity == CanonicalDecimal("1")
    before_digest = order_projection_snapshot_digest(before)
    queue.acknowledge(trade_lease)

    _terminal_lease, outcome = _process_next(queue, authority)
    after = authority.projection_for_order(order.order_id)

    assert outcome.anomalies == expected_anomalies
    assert after is not None
    assert after.projection_state is expected_state
    assert after.projected_executed_quantity == CanonicalDecimal("1")
    assert authority.observed_quantity_for_order(order.order_id) == CanonicalDecimal("1")
    if changes_projection:
        assert after is not before
        assert after.projection_version == 2
        assert order_projection_snapshot_digest(after) != before_digest
    else:
        assert after is before
        assert after.projection_version == 1
        assert order_projection_snapshot_digest(after) == before_digest


def test_unknown_trade_retains_full_fill_and_missing_ancestry() -> None:
    spec_set, order_authority, _orders_values = _orders()
    ingress = _ingress(
        _trade(spec_set, None, external_id="unknown-trade", quantity="2"),
        sequence=1,
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)

    assert outcome.action is ExecutionFactAction.UNRESOLVED
    assert outcome.anomalies == (
        ExecutionFactAnomaly.UNKNOWN_ORDER,
        ExecutionFactAnomaly.MISSING_ANCESTRY,
    )
    assert outcome.resolved_order_id is None
    assert outcome.fill_id is not None
    assert authority.fills[0].quantity == CanonicalDecimal("2")
    assert authority.fills[0].order_id is None
    assert authority.projections == ()


def test_terminal_projection_never_reopens_for_late_trade() -> None:
    spec_set, order_authority, orders = _orders(quantity="2")
    rejected = _ingress(
        _lifecycle(
            orders[0],
            kind=ExecutionFactKind.REJECTION,
            external_id="rejected",
            occurred_at=TIME,
        ),
        sequence=1,
        available_at=TIME,
    )
    late = _ingress(
        _trade(
            spec_set,
            orders[0],
            external_id="late-trade",
            quantity="1",
            occurred_at=TIME + timedelta(seconds=1),
        ),
        sequence=2,
        available_at=TIME + timedelta(seconds=1),
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (rejected, late))
    first_lease, _first = _process_next(queue, authority)
    terminal = authority.projection_for_order(orders[0].order_id)
    assert terminal is not None
    terminal_digest = order_projection_snapshot_digest(terminal)
    queue.acknowledge(first_lease)
    _second_lease, second = _process_next(queue, authority)

    assert second.anomalies == (ExecutionFactAnomaly.LATE_AFTER_TERMINAL,)
    assert authority.fills[-1].quantity == CanonicalDecimal("1")
    after = authority.projection_for_order(orders[0].order_id)
    assert after is terminal
    assert after is not None
    assert order_projection_snapshot_digest(after) == terminal_digest


def test_query_confirmed_fill_projects_without_fill_then_late_trade_retains_terminal() -> None:
    spec_set, order_authority, orders = _orders(quantity="2")
    query_fact = create_submission_query_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("query-filled"),
        occurred_at=TIME,
        provenance=PROVENANCE,
        outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED,
        subject_order=orders[0],
    )
    query = _ingress(query_fact, sequence=1, available_at=TIME)
    trade = _ingress(
        _trade(
            spec_set,
            orders[0],
            external_id="trade-detail",
            quantity="2",
            occurred_at=TIME + timedelta(seconds=1),
        ),
        sequence=2,
        available_at=TIME + timedelta(seconds=1),
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (query, trade))
    query_lease, query_outcome = _process_next(queue, authority)
    query_projection = authority.projection_for_order(orders[0].order_id)
    assert query_outcome.anomalies == (ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,)
    assert query_outcome.fill_id is None
    assert query_projection is not None
    assert query_projection.projection_state is OrderProjectionState.FILLED
    queue.acknowledge(query_lease)

    _trade_lease, trade_outcome = _process_next(queue, authority)
    assert trade_outcome.anomalies == (ExecutionFactAnomaly.LATE_AFTER_TERMINAL,)
    assert authority.projection_for_order(orders[0].order_id) is query_projection
    assert authority.observed_quantity_for_order(orders[0].order_id) == CanonicalDecimal("2")


def test_multi_order_binding_conflict_preserves_every_binding_and_stages_no_venue() -> None:
    spec_set, order_authority, orders = _orders(2)
    fact = _lifecycle(
        orders[0],
        external_id="binding-conflict",
        client_submission_key=orders[1].client_submission_key,
        venue_order_id=VenueOrderId("new-venue-key"),
    )
    ingress = _ingress(fact, sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)

    assert outcome.action is ExecutionFactAction.UNRESOLVED
    assert outcome.anomalies == (ExecutionFactAnomaly.ORDER_BINDING_CONFLICT,)
    assert outcome.resolved_order_id is None
    assert tuple(binding.resolved_order_id for binding in outcome.order_resolutions) == (
        orders[0].order_id,
        orders[1].order_id,
        None,
    )
    assert authority.projections == ()
    assert authority._state.venue_index == {}


def test_first_venue_binding_can_be_learned_by_order_id_or_client_key() -> None:
    for key_kind in ("order_id", "client"):
        spec_set, order_authority, orders = _orders()
        order = orders[0]
        fact = create_lifecycle_execution_fact(
            kind=ExecutionFactKind.ACKNOWLEDGEMENT,
            source_namespace=SOURCE,
            dedup_identity=ExternalFactId(f"venue-by-{key_kind}"),
            occurred_at=TIME,
            provenance=PROVENANCE,
            instrument=order.instrument,
            client_submission_key=(order.client_submission_key if key_kind == "client" else None),
            venue_order_id=VenueOrderId("learned-venue"),
            order_id=order.order_id if key_kind == "order_id" else None,
            correlation_id=order.correlation_id,
            causation_id=order.order_id,
        )
        ingress = _ingress(fact, sequence=1)
        _source, queue, authority = _runtime(
            spec_set,
            order_authority,
            (ingress,),
        )
        _lease, outcome = _process_next(queue, authority)

        assert outcome.action is ExecutionFactAction.ACCEPTED
        venue_binding = outcome.order_resolutions[-1]
        assert venue_binding.key_kind is OrderResolutionKeyKind.VENUE_ORDER_ID
        assert venue_binding.resolved_order_id == order.order_id
        assert authority._state.venue_index[(SOURCE, VenueOrderId("learned-venue"))] is order


def test_existing_venue_mapping_resolves_alone_and_occupied_other_conflicts() -> None:
    spec_set, order_authority, orders = _orders(2)
    first = _ingress(
        _lifecycle(
            orders[0],
            external_id="learn-venue",
            venue_order_id=VenueOrderId("shared-venue"),
        ),
        sequence=1,
        available_at=TIME,
    )
    venue_only_fact = create_lifecycle_execution_fact(
        kind=ExecutionFactKind.ACKNOWLEDGEMENT,
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("venue-only"),
        occurred_at=TIME + timedelta(seconds=1),
        provenance=PROVENANCE,
        venue_order_id=VenueOrderId("shared-venue"),
    )
    venue_only = _ingress(
        venue_only_fact,
        sequence=2,
        available_at=TIME + timedelta(seconds=1),
    )
    occupied_other = _ingress(
        _lifecycle(
            orders[1],
            external_id="occupied-other",
            occurred_at=TIME + timedelta(seconds=2),
            venue_order_id=VenueOrderId("shared-venue"),
        ),
        sequence=3,
        available_at=TIME + timedelta(seconds=2),
    )
    _source, queue, authority = _runtime(
        spec_set,
        order_authority,
        (first, venue_only, occupied_other),
    )
    first_lease, _first = _process_next(queue, authority)
    queue.acknowledge(first_lease)
    second_lease, second = _process_next(queue, authority)
    queue.acknowledge(second_lease)
    _third_lease, third = _process_next(queue, authority)

    assert second.action is ExecutionFactAction.ACCEPTED
    assert second.resolved_order_id == orders[0].order_id
    assert third.action is ExecutionFactAction.UNRESOLVED
    assert third.anomalies == (ExecutionFactAnomaly.ORDER_BINDING_CONFLICT,)
    assert third.resolved_order_id is None
    assert authority._state.venue_index[(SOURCE, VenueOrderId("shared-venue"))] is orders[0]


@pytest.mark.parametrize(
    "second_source",
    [SOURCE, SourceNamespace("sim.secondary")],
)
def test_order_rejects_second_distinct_venue_identity(
    second_source: SourceNamespace,
) -> None:
    spec_set, order_authority, orders = _orders()
    order = orders[0]
    first = _ingress(
        _lifecycle(
            order,
            external_id="first-venue",
            venue_order_id=VenueOrderId("venue-a"),
        ),
        sequence=1,
        available_at=TIME,
    )
    second_fact = create_lifecycle_execution_fact(
        kind=ExecutionFactKind.ACKNOWLEDGEMENT,
        source_namespace=second_source,
        dedup_identity=ExternalFactId("second-venue"),
        occurred_at=TIME + timedelta(seconds=1),
        provenance=PROVENANCE,
        instrument=order.instrument,
        client_submission_key=order.client_submission_key,
        venue_order_id=VenueOrderId("venue-b"),
        order_id=order.order_id,
        correlation_id=order.correlation_id,
        causation_id=order.order_id,
    )
    second = create_execution_fact_ingress(
        available_at=second_fact.occurred_at,
        source_namespace=second_source,
        ingress_sequence=2,
        fact=second_fact,
    )
    authorities = {
        source: create_phase1_execution_fact_ingress_authority(
            run_id=RUN_ID,
            spec_set=spec_set,
            source_namespace=source,
        )
        for source in {SOURCE, second_source}
    }
    authorities[SOURCE].register_ingress(first)
    authorities[second_source].register_ingress(second)
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots((first, second)),
        fact_issuance_verifiers=tuple(
            authorities[source] for source in sorted(authorities, key=lambda item: item.value)
        ),
    )
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=order_authority,
        dispatch_verifier=queue,
    )

    first_lease, _first_outcome = _process_next(queue, authority)
    first_projection = authority.projection_for_order(order.order_id)
    assert first_projection is not None
    first_digest = order_projection_snapshot_digest(first_projection)
    queue.acknowledge(first_lease)
    second_lease, outcome = _process_next(queue, authority)

    assert outcome.action is ExecutionFactAction.UNRESOLVED
    assert outcome.anomalies == (ExecutionFactAnomaly.ORDER_BINDING_CONFLICT,)
    assert outcome.resolved_order_id == order.order_id
    assert outcome.order_resolutions[-1].resolved_order_id is None
    assert authority.projection_for_order(order.order_id) is first_projection
    assert order_projection_snapshot_digest(first_projection) == first_digest
    assert authority._state.venue_index == {
        (SOURCE, VenueOrderId("venue-a")): order,
    }
    queue.acknowledge(second_lease)
    state = authority._state
    assert authority.process_ingress(second) is outcome
    assert authority._state is state


def test_new_venue_with_unresolved_order_key_is_not_staged() -> None:
    spec_set, order_authority, orders = _orders()
    order = orders[0]
    fact = create_lifecycle_execution_fact(
        kind=ExecutionFactKind.ACKNOWLEDGEMENT,
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("unresolved-order-key"),
        occurred_at=TIME,
        provenance=PROVENANCE,
        instrument=order.instrument,
        client_submission_key=order.client_submission_key,
        venue_order_id=VenueOrderId("must-not-stage"),
        order_id=_id(EconomicOwnerKind.EXECUTION_ORDER, 999),
        correlation_id=order.correlation_id,
        causation_id=_id(EconomicOwnerKind.EXECUTION_ORDER, 999),
    )
    ingress = _ingress(fact, sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)

    assert outcome.anomalies == (ExecutionFactAnomaly.ORDER_BINDING_CONFLICT,)
    assert outcome.resolved_order_id == order.order_id
    assert outcome.order_resolutions[-1].resolved_order_id is None
    assert authority._state.venue_index == {}
    assert outcome.projection_before_sha256 is None
    assert outcome.projection_after_sha256 is None


def test_query_binding_conflict_creates_no_projection_mapping_or_query_anomaly() -> None:
    spec_set, order_authority, orders = _orders(2)
    original = create_submission_query_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("conflicting-query"),
        occurred_at=TIME,
        provenance=PROVENANCE,
        outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED,
        subject_order=orders[0],
        venue_order_id=VenueOrderId("query-venue"),
    )
    conflicting = _clone_slots(
        original,
        client_submission_key=orders[1].client_submission_key,
    )
    ingress = _ingress(conflicting, sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)

    assert outcome.anomalies == (ExecutionFactAnomaly.ORDER_BINDING_CONFLICT,)
    assert ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE not in outcome.anomalies
    assert outcome.resolved_order_id is None
    assert authority.projections == ()
    assert authority._state.venue_index == {}


@pytest.mark.parametrize(
    ("before_kind", "expected_anomalies", "expected_state"),
    [
        (None, (ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,), "filled"),
        (
            "submitted",
            (ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,),
            "filled",
        ),
        (
            "acknowledged",
            (ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,),
            "filled",
        ),
        (
            "partial",
            (ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,),
            "filled",
        ),
        ("filled_from_trade", (), "filled"),
        (
            "filled_from_query",
            (ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,),
            "filled",
        ),
        (
            "rejected",
            (
                ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,
                ExecutionFactAnomaly.TERMINAL_STATE_CONFLICT,
            ),
            "rejected",
        ),
        (
            "expired",
            (
                ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,
                ExecutionFactAnomaly.TERMINAL_STATE_CONFLICT,
            ),
            "expired",
        ),
        (
            "cancelled",
            (
                ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,
                ExecutionFactAnomaly.TERMINAL_STATE_CONFLICT,
            ),
            "cancelled",
        ),
        (
            "definitely_not_submitted",
            (
                ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE,
                ExecutionFactAnomaly.TERMINAL_STATE_CONFLICT,
            ),
            "definitely_not_submitted",
        ),
    ],
)
def test_query_confirmed_filled_matrix(
    before_kind: str | None,
    expected_anomalies: tuple[ExecutionFactAnomaly, ...],
    expected_state: str,
) -> None:
    spec_set, order_authority, orders = _orders(quantity="2")
    order = orders[0]
    ingresses: list[Any] = []
    if before_kind == "submitted":
        ingresses.append(
            _query(
                order,
                external_id="before-submitted",
                outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED,
                occurred_at=TIME,
                sequence=1,
            )
        )
    elif before_kind == "acknowledged":
        ingresses.append(_ingress(_lifecycle(order), sequence=1))
    elif before_kind == "partial":
        ingresses.append(
            _ingress(
                _trade(spec_set, order, external_id="before-partial", quantity="1"),
                sequence=1,
            )
        )
    elif before_kind == "filled_from_trade":
        ingresses.append(
            _ingress(
                _trade(spec_set, order, external_id="before-filled", quantity="2"),
                sequence=1,
            )
        )
    elif before_kind == "filled_from_query":
        ingresses.append(
            _query(
                order,
                external_id="before-query-filled",
                outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED,
                occurred_at=TIME,
                sequence=1,
            )
        )
    elif before_kind in {"rejected", "expired", "cancelled"}:
        kind = {
            "rejected": ExecutionFactKind.REJECTION,
            "expired": ExecutionFactKind.EXPIRY,
            "cancelled": ExecutionFactKind.CANCELLATION,
        }[before_kind]
        ingresses.append(
            _ingress(
                _lifecycle(order, kind=kind, external_id=f"before-{before_kind}"),
                sequence=1,
            )
        )
    elif before_kind == "definitely_not_submitted":
        ingresses.append(
            _query(
                order,
                external_id="before-not-submitted",
                outcome_code=(OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_NOT_SUBMITTED),
                occurred_at=TIME,
                sequence=1,
            )
        )
    query_time = TIME if not ingresses else TIME + timedelta(seconds=1)
    query = _query(
        order,
        external_id="matrix-query-filled",
        outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED,
        occurred_at=query_time,
        sequence=len(ingresses) + 1,
    )
    ingresses.append(query)
    _source, queue, authority = _runtime(
        spec_set,
        order_authority,
        tuple(ingresses),
    )
    if len(ingresses) == 2:
        first_lease, _first = _process_next(queue, authority)
        queue.acknowledge(first_lease)
    _query_lease, outcome = _process_next(queue, authority)

    assert outcome.anomalies == expected_anomalies
    projection = authority.projection_for_order(order.order_id)
    assert projection is not None
    assert projection.projection_state.value == expected_state


def test_still_unknown_with_and_without_projection_is_retained() -> None:
    for with_projection in (False, True):
        spec_set, order_authority, orders = _orders()
        order = orders[0]
        ingresses: list[Any] = []
        if with_projection:
            ingresses.append(_ingress(_lifecycle(order), sequence=1))
        ingresses.append(
            _query(
                order,
                external_id=f"still-unknown-{with_projection}",
                outcome_code=OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN,
                occurred_at=(TIME + timedelta(seconds=1) if with_projection else TIME),
                sequence=len(ingresses) + 1,
            )
        )
        _source, queue, authority = _runtime(
            spec_set,
            order_authority,
            tuple(ingresses),
        )
        before = None
        if with_projection:
            first_lease, _first = _process_next(queue, authority)
            before = authority.projection_for_order(order.order_id)
            queue.acknowledge(first_lease)
        _query_lease, outcome = _process_next(queue, authority)

        assert outcome.anomalies == (ExecutionFactAnomaly.INSUFFICIENT_PROJECTION_EVIDENCE,)
        assert authority.projection_for_order(order.order_id) is before
        if before is None:
            assert outcome.projection_before_sha256 is None
            assert outcome.projection_after_sha256 is None
        else:
            assert (
                outcome.projection_before_sha256
                == outcome.projection_after_sha256
                == order_projection_snapshot_digest(before)
            )


def test_cross_run_trade_is_contextual_invalid_without_fill_or_resolution() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(
        _trade(
            spec_set,
            orders[0],
            external_id="cross-run",
            quantity="1",
            run_id=OTHER_RUN_ID,
        ),
        sequence=1,
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)

    assert outcome.action is ExecutionFactAction.INVALID
    assert outcome.anomalies == (ExecutionFactAnomaly.CONTEXT_INVALID,)
    assert outcome.fill_id is None
    assert outcome.order_resolutions == ()
    assert authority.fills == ()
    assert authority.halt_requested


@pytest.mark.parametrize("invalid_kind", ["off_grid_trade", "lifecycle_mismatch"])
def test_canonical_context_invalid_fact_publishes_invalid_atomically(
    invalid_kind: str,
) -> None:
    spec_set, order_authority, orders = _orders()
    order = orders[0]
    if invalid_kind == "off_grid_trade":
        valid_fact = _trade(
            spec_set,
            order,
            external_id="off-grid-trade",
            quantity="1",
        )
        invalid_payload = _clone_slots(
            valid_fact.payload,
            quantity=CanonicalDecimal("0.5"),
        )
        invalid_fact = _clone_slots(valid_fact, payload=invalid_payload)
    else:
        acknowledgement = _lifecycle(order, external_id="lifecycle-mismatch")
        rejection = _lifecycle(
            order,
            kind=ExecutionFactKind.REJECTION,
            external_id="payload-source",
        )
        invalid_fact = _clone_slots(acknowledgement, payload=rejection.payload)
    ingress = _ingress(invalid_fact, sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    before = authority._state

    lease, outcome = _process_next(queue, authority)

    assert outcome.action is ExecutionFactAction.INVALID
    assert outcome.anomalies == (ExecutionFactAnomaly.CONTEXT_INVALID,)
    assert outcome.order_resolutions == ()
    assert outcome.fill_id is None
    assert outcome.projection_before_sha256 is None
    assert outcome.projection_after_sha256 is None
    assert authority.next_fill_sequence == 1
    assert authority.fills == ()
    assert authority.projections == ()
    assert authority.halt_requested
    assert authority._state is not before
    assert authority.process_ingress(ingress) is outcome
    queue.acknowledge(lease)


def test_pre_occurrence_ingress_is_defensively_context_invalid() -> None:
    spec_set, order_authority, orders = _orders()
    valid = _ingress(_lifecycle(orders[0]), sequence=1)
    invalid = _clone_slots(
        valid,
        available_at=valid.fact.occurred_at - timedelta(microseconds=1),
    )
    authority = create_phase1_execution_fact_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        order_verifier=order_authority,
        dispatch_verifier=cast(Any, _DispatchStub(spec_set)),
    )

    outcome = authority.process_ingress(invalid)

    assert outcome.action is ExecutionFactAction.INVALID
    assert outcome.anomalies == (ExecutionFactAnomaly.CONTEXT_INVALID,)
    assert outcome.fill_id is None
    assert authority.halt_requested


def test_projection_and_outcome_readers_are_strict_and_contextual() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)
    projection = authority.projection_for_order(orders[0].order_id)
    assert projection is not None

    projection_bytes = canonical_order_projection_snapshot_bytes(projection)
    outcome_bytes = canonical_execution_fact_processing_outcome_bytes(outcome)
    assert (
        decode_order_projection_snapshot(
            projection_bytes,
            order=orders[0],
            spec_set=spec_set,
        )
        == projection
    )
    assert (
        decode_execution_fact_processing_outcome(
            outcome_bytes,
            ingress=ingress,
            fill=None,
            projection_before=None,
            projection_after=projection,
            resolved_orders=(orders[0],),
        )
        == outcome
    )

    altered = json.loads(outcome_bytes)
    altered["halt_requested"] = True
    altered_bytes = json.dumps(
        altered,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with pytest.raises(ExecutionStateError):
        decode_execution_fact_processing_outcome(
            altered_bytes,
            ingress=ingress,
            fill=None,
            projection_before=None,
            projection_after=projection,
            resolved_orders=(orders[0],),
        )


def test_outcome_rejects_projection_from_different_order_economics() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)
    projection = authority.projection_for_order(orders[0].order_id)
    assert projection is not None
    alternate_order = _clone_slots(
        orders[0],
        quantity=CanonicalDecimal("1"),
    )
    alternate_projection = create_order_projection_snapshot(
        order=alternate_order,
        spec_set=spec_set,
        projection_version=projection.projection_version,
        projection_state=projection.projection_state,
        projected_executed_quantity=projection.projected_executed_quantity,
        venue_source_namespace=projection.venue_source_namespace,
        venue_order_id=projection.venue_order_id,
        last_fact_key=projection.last_fact_key,
        last_fact_sha256=projection.last_fact_sha256,
    )

    with pytest.raises(ExecutionStateError) as error:
        create_execution_fact_processing_outcome(
            run_id=RUN_ID,
            runtime_dispatch_sequence=outcome.runtime_dispatch_sequence,
            ingress=ingress,
            action=outcome.action,
            anomalies=outcome.anomalies,
            order_resolutions=outcome.order_resolutions,
            resolved_order=orders[0],
            fill=None,
            projection_before=None,
            projection_after=alternate_projection,
        )

    assert error.value.code is OutcomeCode.CONFLICTING_ID


def test_outcome_reader_requires_exact_resolved_order_context() -> None:
    spec_set, order_authority, orders = _orders(3)

    single_ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    _source, single_queue, single_authority = _runtime(
        spec_set,
        order_authority,
        (single_ingress,),
    )
    _single_lease, single_outcome = _process_next(single_queue, single_authority)
    single_projection = single_authority.projection_for_order(orders[0].order_id)
    assert single_projection is not None
    single_bytes = canonical_execution_fact_processing_outcome_bytes(single_outcome)
    assert (
        decode_execution_fact_processing_outcome(
            single_bytes,
            ingress=single_ingress,
            fill=None,
            projection_before=None,
            projection_after=single_projection,
            resolved_orders=(orders[0],),
        )
        is not None
    )
    with pytest.raises(ExecutionStateError):
        decode_execution_fact_processing_outcome(
            single_bytes,
            ingress=single_ingress,
            fill=None,
            projection_before=None,
            projection_after=single_projection,
            resolved_orders=(orders[0], orders[1]),
        )

    unknown_fact = create_lifecycle_execution_fact(
        kind=ExecutionFactKind.ACKNOWLEDGEMENT,
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("reader-unknown"),
        occurred_at=TIME,
        provenance=PROVENANCE,
    )
    unknown_ingress = _ingress(unknown_fact, sequence=1)
    _source, unknown_queue, unknown_authority = _runtime(
        spec_set,
        order_authority,
        (unknown_ingress,),
    )
    _unknown_lease, unknown_outcome = _process_next(unknown_queue, unknown_authority)
    unknown_bytes = canonical_execution_fact_processing_outcome_bytes(unknown_outcome)
    assert (
        decode_execution_fact_processing_outcome(
            unknown_bytes,
            ingress=unknown_ingress,
            fill=None,
            projection_before=None,
            projection_after=None,
            resolved_orders=(),
        )
        == unknown_outcome
    )
    with pytest.raises(ExecutionStateError):
        decode_execution_fact_processing_outcome(
            unknown_bytes,
            ingress=unknown_ingress,
            fill=None,
            projection_before=None,
            projection_after=None,
            resolved_orders=(orders[0],),
        )

    duplicate_fact = _lifecycle(orders[0], external_id="reader-duplicate")
    first_duplicate_ingress = _ingress(duplicate_fact, sequence=1)
    duplicate_ingress = _ingress(duplicate_fact, sequence=2)
    _source, duplicate_queue, duplicate_authority = _runtime(
        spec_set,
        order_authority,
        (first_duplicate_ingress, duplicate_ingress),
    )
    first_lease, _first_outcome = _process_next(duplicate_queue, duplicate_authority)
    duplicate_queue.acknowledge(first_lease)
    _duplicate_lease, duplicate_outcome = _process_next(
        duplicate_queue,
        duplicate_authority,
    )
    duplicate_bytes = canonical_execution_fact_processing_outcome_bytes(duplicate_outcome)
    assert (
        decode_execution_fact_processing_outcome(
            duplicate_bytes,
            ingress=duplicate_ingress,
            fill=None,
            projection_before=None,
            projection_after=None,
            resolved_orders=(),
        )
        == duplicate_outcome
    )
    with pytest.raises(ExecutionStateError):
        decode_execution_fact_processing_outcome(
            duplicate_bytes,
            ingress=duplicate_ingress,
            fill=None,
            projection_before=None,
            projection_after=None,
            resolved_orders=(orders[0],),
        )

    conflict_fact = _lifecycle(
        orders[0],
        external_id="reader-multi-order",
        client_submission_key=orders[1].client_submission_key,
    )
    conflict_ingress = _ingress(conflict_fact, sequence=1)
    _source, conflict_queue, conflict_authority = _runtime(
        spec_set,
        order_authority,
        (conflict_ingress,),
    )
    _conflict_lease, conflict_outcome = _process_next(
        conflict_queue,
        conflict_authority,
    )
    conflict_bytes = canonical_execution_fact_processing_outcome_bytes(conflict_outcome)
    exact_context = (orders[0], orders[1])
    assert (
        decode_execution_fact_processing_outcome(
            conflict_bytes,
            ingress=conflict_ingress,
            fill=None,
            projection_before=None,
            projection_after=None,
            resolved_orders=exact_context,
        )
        == conflict_outcome
    )
    for wrong_context in ((orders[0],), (*exact_context, orders[2])):
        with pytest.raises(ExecutionStateError):
            decode_execution_fact_processing_outcome(
                conflict_bytes,
                ingress=conflict_ingress,
                fill=None,
                projection_before=None,
                projection_after=None,
                resolved_orders=wrong_context,
            )


def test_readers_reject_schema_key_rank_enum_digest_and_noncanonical_mutations() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    _lease, outcome = _process_next(queue, authority)
    projection = authority.projection_for_order(orders[0].order_id)
    assert projection is not None
    outcome_bytes = canonical_execution_fact_processing_outcome_bytes(outcome)
    projection_bytes = canonical_order_projection_snapshot_bytes(projection)

    outcome_mutations: list[dict[str, object]] = []
    extra = json.loads(outcome_bytes)
    extra["extra"] = 1
    outcome_mutations.append(extra)
    unknown_enum = json.loads(outcome_bytes)
    unknown_enum["action"] = "not-an-action"
    outcome_mutations.append(unknown_enum)
    wrong_rank = json.loads(outcome_bytes)
    wrong_rank["action"] = "unresolved"
    wrong_rank["anomalies"] = ["unknown_order", "order_binding_conflict"]
    outcome_mutations.append(wrong_rank)
    wrong_code = json.loads(outcome_bytes)
    wrong_code["outcome_code"] = OutcomeCode.FACT_CONFLICT.value
    outcome_mutations.append(wrong_code)
    wrong_boolean = json.loads(outcome_bytes)
    wrong_boolean["requires_reconciliation"] = True
    outcome_mutations.append(wrong_boolean)
    wrong_digest = json.loads(outcome_bytes)
    wrong_digest["projection_after_sha256"] = "9" * 64
    outcome_mutations.append(wrong_digest)
    wrong_nesting = json.loads(outcome_bytes)
    cast(dict[str, object], wrong_nesting["reported_order_id"])["extra"] = 1
    outcome_mutations.append(wrong_nesting)

    for document in outcome_mutations:
        mutated = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with pytest.raises(ExecutionStateError):
            decode_execution_fact_processing_outcome(
                mutated,
                ingress=ingress,
                fill=None,
                projection_before=None,
                projection_after=projection,
                resolved_orders=(orders[0],),
            )
    with pytest.raises(ExecutionStateError):
        decode_execution_fact_processing_outcome(
            b" " + outcome_bytes,
            ingress=ingress,
            fill=None,
            projection_before=None,
            projection_after=projection,
            resolved_orders=(orders[0],),
        )
    with pytest.raises(ExecutionStateError):
        decode_execution_fact_processing_outcome(
            outcome_bytes,
            ingress=ingress,
            fill=None,
            projection_before=None,
            projection_after=projection,
            resolved_orders=(),
        )

    projection_mutations: list[dict[str, object]] = []
    projection_extra = json.loads(projection_bytes)
    projection_extra["extra"] = 1
    projection_mutations.append(projection_extra)
    projection_enum = json.loads(projection_bytes)
    projection_enum["projection_state"] = "not-a-state"
    projection_mutations.append(projection_enum)
    projection_quantity = json.loads(projection_bytes)
    projection_quantity["projected_executed_quantity"] = "999"
    projection_mutations.append(projection_quantity)
    projection_digest = json.loads(projection_bytes)
    projection_digest["order_sha256"] = "9" * 64
    projection_mutations.append(projection_digest)
    for document in projection_mutations:
        mutated = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with pytest.raises(ExecutionStateError):
            decode_order_projection_snapshot(
                mutated,
                order=orders[0],
                spec_set=spec_set,
            )
    with pytest.raises(ExecutionStateError):
        decode_order_projection_snapshot(
            projection_bytes + b"\n",
            order=orders[0],
            spec_set=spec_set,
        )


@pytest.mark.parametrize(
    ("state", "quantity"),
    [
        (OrderProjectionState.PARTIALLY_FILLED, "0"),
        (OrderProjectionState.PARTIALLY_FILLED, "2"),
        (OrderProjectionState.FILLED, "1"),
        (OrderProjectionState.SUBMITTED, "1"),
        (OrderProjectionState.ACKNOWLEDGED, "1"),
        (OrderProjectionState.REJECTED, "1"),
        (OrderProjectionState.DEFINITELY_NOT_SUBMITTED, "1"),
        (OrderProjectionState.EXPIRED, "2"),
        (OrderProjectionState.CANCELLED, "2"),
    ],
)
def test_projection_factory_rejects_state_quantity_contradictions(
    state: OrderProjectionState,
    quantity: str,
) -> None:
    spec_set, _order_authority, orders = _orders(quantity="2")
    fact = _lifecycle(orders[0])

    with pytest.raises(ExecutionStateError) as error:
        create_order_projection_snapshot(
            order=orders[0],
            spec_set=spec_set,
            projection_version=1,
            projection_state=state,
            projected_executed_quantity=CanonicalDecimal(quantity),
            venue_source_namespace=None,
            venue_order_id=None,
            last_fact_key=fact.dedup_key,
            last_fact_sha256=fact.fact_sha256,
        )

    assert error.value.code is OutcomeCode.OUT_OF_RANGE


def test_fill_exhaustion_is_atomic() -> None:
    spec_set, order_authority, orders = _orders()
    ingress = _ingress(
        _trade(spec_set, orders[0], external_id="exhausted", quantity="1"),
        sequence=1,
    )
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    authority._state = replace(authority._state, fill_next=None)
    state = authority._state
    queue.pop()

    with pytest.raises(ExecutionFactAuthorityError) as error:
        authority.process_ingress(ingress)
    assert error.value.code is OutcomeCode.OUT_OF_RANGE
    assert authority._state is state


def test_maximum_dispatch_sequence_is_published_once_then_exhausts_atomically() -> None:
    spec_set, order_authority, orders = _orders()
    ingresses = (
        _ingress(
            _lifecycle(orders[0], external_id="dispatch-max-1"),
            sequence=1,
        ),
        _ingress(
            _lifecycle(
                orders[0],
                kind=ExecutionFactKind.CANCELLATION,
                external_id="dispatch-max-2",
            ),
            sequence=2,
        ),
    )
    _source, queue, _authority = _runtime(
        spec_set,
        order_authority,
        ingresses,
    )
    queue._state = replace(queue._state, dispatch_next=(1 << 64) - 1)
    lease = queue.pop()
    assert lease.dispatch_sequence == (1 << 64) - 1
    queue.acknowledge(lease)
    state = queue._state

    with pytest.raises(RuntimeOrderingError) as exhausted:
        queue.pop()
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE
    assert queue._state is state
    assert queue.remaining == 1


def test_maximum_fill_sequence_is_allocated_once_then_exhausts_atomically() -> None:
    spec_set, order_authority, orders = _orders(quantity="10")
    ingresses = (
        _ingress(
            _trade(spec_set, orders[0], external_id="fill-max-1", quantity="1"),
            sequence=1,
        ),
        _ingress(
            _trade(spec_set, orders[0], external_id="fill-max-2", quantity="1"),
            sequence=2,
        ),
    )
    _source, queue, authority = _runtime(
        spec_set,
        order_authority,
        ingresses,
    )
    authority._state = replace(authority._state, fill_next=(1 << 64) - 1)
    first_lease, first = _process_next(queue, authority)
    assert first.fill_id is not None
    assert first.fill_id.owner_sequence == (1 << 64) - 1
    assert authority.next_fill_sequence is None
    queue.acknowledge(first_lease)
    queue.pop()
    state = authority._state

    with pytest.raises(ExecutionFactAuthorityError) as exhausted:
        authority.process_ingress(ingresses[1])
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE
    _assert_fact_state_unchanged(authority, state)


def test_maximum_projection_version_cannot_advance_and_failure_is_atomic() -> None:
    spec_set, order_authority, orders = _orders(quantity="10")
    acknowledgement = _ingress(
        _lifecycle(orders[0], external_id="projection-max-before"),
        sequence=1,
        available_at=TIME,
    )
    trade = _ingress(
        _trade(
            spec_set,
            orders[0],
            external_id="projection-max-trade",
            quantity="1",
            occurred_at=TIME + timedelta(seconds=1),
        ),
        sequence=2,
        available_at=TIME + timedelta(seconds=1),
    )
    _source, queue, authority = _runtime(
        spec_set,
        order_authority,
        (acknowledgement, trade),
    )
    first_lease, _first = _process_next(queue, authority)
    queue.acknowledge(first_lease)
    current = authority.projection_for_order(orders[0].order_id)
    assert current is not None
    maximum = create_order_projection_snapshot(
        order=orders[0],
        spec_set=spec_set,
        projection_version=(1 << 64) - 1,
        projection_state=current.projection_state,
        projected_executed_quantity=current.projected_executed_quantity,
        venue_source_namespace=current.venue_source_namespace,
        venue_order_id=current.venue_order_id,
        last_fact_key=current.last_fact_key,
        last_fact_sha256=current.last_fact_sha256,
    )
    authority._state = replace(
        authority._state,
        projection_index=MappingProxyType({orders[0].order_id: maximum}),
        projections=(maximum,),
    )
    queue.pop()
    state = authority._state

    with pytest.raises(ExecutionFactAuthorityError) as exhausted:
        authority.process_ingress(trade)
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE
    _assert_fact_state_unchanged(authority, state)


def test_malformed_exact_carrier_fails_before_dispatch_or_publication() -> None:
    spec_set, order_authority, orders = _orders()
    valid = _ingress(_lifecycle(orders[0]), sequence=1)
    malformed = _clone_slots(valid, fact=object())
    _source, _queue, authority = _runtime(
        spec_set,
        order_authority,
        (valid,),
    )
    state = authority._state

    with pytest.raises(ExecutionFactAuthorityError) as error:
        authority.process_ingress(malformed)
    assert error.value.code is OutcomeCode.INVALID_TYPE
    _assert_fact_state_unchanged(authority, state)


@pytest.mark.parametrize(
    "failure_point",
    [
        "canonical_ingress",
        "canonical_fact",
        "freeze",
        "preflight",
    ],
)
def test_source_registration_prepublication_failures_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    spec_set, _order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    source = create_phase1_execution_fact_ingress_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        source_namespace=SOURCE,
    )
    state = source._state
    target = {
        "canonical_ingress": "canonical_execution_fact_ingress_bytes",
        "canonical_fact": "canonical_execution_fact_bytes",
        "freeze": "MappingProxyType",
        "preflight": "_preflight_state",
    }[failure_point]
    monkeypatch.setattr(ingress_module, target, _raise_injected)

    with pytest.raises(RuntimeError, match="injected pre-publication failure"):
        source.register_ingress(ingress)
    assert source._state is state
    assert source._state.ingress_index is state.ingress_index
    assert source._state.issued_ingresses is state.issued_ingresses


@pytest.mark.parametrize(
    "failure_point",
    [
        "canonical_ingress",
        "canonical_fact",
        "issuance_verifier",
        "advance",
        "preflight",
    ],
)
def test_queue_pop_prepublication_failures_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    spec_set, _order_authority, orders = _orders()
    ingress = _ingress(_lifecycle(orders[0]), sequence=1)
    source = create_phase1_execution_fact_ingress_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        source_namespace=SOURCE,
    )
    source.register_ingress(ingress)
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=spec_set,
        plan=prepare_bounded_runtime_roots((ingress,)),
        fact_issuance_verifiers=(source,),
    )
    state = queue._state
    if failure_point == "canonical_ingress":
        monkeypatch.setattr(
            queue_module,
            "canonical_execution_fact_ingress_bytes",
            _raise_injected,
        )
    elif failure_point == "canonical_fact":
        monkeypatch.setattr(
            queue_module,
            "canonical_execution_fact_bytes",
            _raise_injected,
        )
    elif failure_point == "issuance_verifier":
        monkeypatch.setattr(
            Phase1ExecutionFactIngressAuthority,
            "has_issued_ingress",
            _raise_injected,
        )
    elif failure_point == "advance":
        monkeypatch.setattr(queue_module, "_advance", _raise_injected)
    else:
        monkeypatch.setattr(queue_module, "_preflight_pop", _raise_injected)

    with pytest.raises(RuntimeError, match="injected pre-publication failure"):
        queue.pop()
    assert queue._state is state
    assert queue._state.acknowledged_fact_dispatches is state.acknowledged_fact_dispatches


@pytest.mark.parametrize(
    ("carrier", "failure_point"),
    [
        ("ack", "materialize"),
        ("ack", "dispatch"),
        ("ack", "resolve"),
        ("ack", "projection"),
        ("trade", "fill"),
        ("ack", "outcome"),
        ("ack", "freeze"),
        ("ack", "preflight"),
    ],
)
def test_fact_authority_prepublication_failures_preserve_all_nested_state(
    monkeypatch: pytest.MonkeyPatch,
    carrier: str,
    failure_point: str,
) -> None:
    spec_set, order_authority, orders = _orders()
    fact = (
        _lifecycle(orders[0])
        if carrier == "ack"
        else _trade(spec_set, orders[0], external_id="injected-trade", quantity="1")
    )
    ingress = _ingress(fact, sequence=1)
    _source, queue, authority = _runtime(spec_set, order_authority, (ingress,))
    queue.pop()
    state = authority._state
    if failure_point == "materialize":
        monkeypatch.setattr(
            fact_authority_module,
            "canonical_execution_fact_ingress_bytes",
            _raise_injected,
        )
    elif failure_point == "dispatch":
        monkeypatch.setattr(
            DeterministicRootQueue,
            "resolve_active_issued_fact_dispatch",
            _raise_injected,
        )
    elif failure_point == "resolve":
        monkeypatch.setattr(
            Phase1OrderAuthority,
            "resolve_issued_order_by_id",
            _raise_injected,
        )
    elif failure_point == "projection":
        monkeypatch.setattr(
            fact_authority_module,
            "create_order_projection_snapshot",
            _raise_injected,
        )
    elif failure_point == "fill":
        monkeypatch.setattr(fact_authority_module, "create_fill", _raise_injected)
    elif failure_point == "outcome":
        monkeypatch.setattr(
            fact_authority_module,
            "create_execution_fact_processing_outcome",
            _raise_injected,
        )
    elif failure_point == "freeze":
        monkeypatch.setattr(
            fact_authority_module,
            "MappingProxyType",
            _raise_injected,
        )
    else:
        monkeypatch.setattr(
            fact_authority_module,
            "_preflight_candidate",
            _raise_injected,
        )

    with pytest.raises(RuntimeError, match="injected pre-publication failure"):
        authority.process_ingress(ingress)
    _assert_fact_state_unchanged(authority, state)


def test_fact_and_queue_authorities_are_factory_only() -> None:
    assert type(EXECUTION_FACT_ANOMALY_RANKS) is MappingProxyType
    with pytest.raises(TypeError):
        cast(Any, EXECUTION_FACT_ANOMALY_RANKS)[ExecutionFactAnomaly.OVERFILL] = 0
    with pytest.raises(TypeError):
        Phase1ExecutionFactAuthority()
    with pytest.raises(TypeError):
        DeterministicRootQueue()
    with pytest.raises(TypeError):
        RuntimeDispatchLease()
