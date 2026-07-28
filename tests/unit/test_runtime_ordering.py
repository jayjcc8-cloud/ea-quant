from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from inspect import getmembers, isfunction
from types import MappingProxyType
from typing import cast

import pytest

import ea.core.runtime as runtime_module
from ea.core import (
    END_OF_RUN_KIND_RANKS,
    EXECUTION_FACT_KIND_RANKS,
    MARKET_DATA_KIND_RANKS,
    RECONCILIATION_OBSERVATION_KIND_RANKS,
    RUNTIME_ROOT_DOMAIN_RANKS,
    SAFETY_KIND_RANKS,
    TIMER_KIND_RANKS,
    Adjustment,
    Bar,
    BoundedRuntimeRootPlan,
    CanonicalDecimal,
    EndOfRunKind,
    EndOfRunRoot,
    ExecutionFactIngress,
    ExecutionFactKind,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    MarketDataEnvelope,
    MarketDataKind,
    OutcomeCode,
    PriceDomain,
    ReconciliationObservationKind,
    RunId,
    RuntimeIdentifier,
    RuntimeOrderingError,
    RuntimeRoot,
    RuntimeRootDomain,
    SafetyKind,
    SafetyRoot,
    SafetySubject,
    SettlementCurrency,
    Sha256Digest,
    SourceId,
    SourceNamespace,
    TimerKind,
    TimerRoot,
    VenueId,
    admission_order_key,
    build_instrument_spec_set,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    prepare_bounded_runtime_roots,
    runtime_root_order_key,
)
from ea.runtime import DeterministicRootQueue, create_deterministic_root_queue

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
ROOT_TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
INTERVAL_START = ROOT_TIME - timedelta(minutes=1)
SOURCE_NAMESPACE = SourceNamespace("sim.primary")
PROVENANCE = FactProvenance(
    FactProvenanceId("phase1.simulator.v1"),
    Sha256Digest("3" * 64),
)
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("runtime-test-v1"),
    (
        InstrumentExecutionSpec(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            specification_id=InstrumentSpecId("xnas-aapl-v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        ),
    ),
)


def _market(
    *,
    available_at: datetime = ROOT_TIME,
    source_sequence: int = 7,
    revision: int = 0,
) -> MarketDataEnvelope:
    return MarketDataEnvelope(
        payload=Bar(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            interval_start=INTERVAL_START,
            interval_end=ROOT_TIME,
            adjustment=Adjustment.RAW,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
        ),
        source=SourceId("primary.raw"),
        available_at=available_at,
        source_sequence=source_sequence,
        revision=revision,
    )


def _fact_ingress(
    *,
    kind: ExecutionFactKind = ExecutionFactKind.ACKNOWLEDGEMENT,
    occurred_at: datetime = ROOT_TIME,
    available_at: datetime = ROOT_TIME,
    ingress_sequence: int = 4,
    external_id: str = "fact-4",
) -> ExecutionFactIngress:
    fact = create_lifecycle_execution_fact(
        kind=kind,
        source_namespace=SOURCE_NAMESPACE,
        dedup_identity=ExternalFactId(external_id),
        occurred_at=occurred_at,
        provenance=PROVENANCE,
    )
    return create_execution_fact_ingress(
        available_at=available_at,
        source_namespace=SOURCE_NAMESPACE,
        ingress_sequence=ingress_sequence,
        fact=fact,
    )


def _safety(
    *,
    available_at: datetime = ROOT_TIME,
    kind: SafetyKind = SafetyKind.HALT,
    sequence: int = 1,
    subject: SafetySubject | None = None,
) -> SafetyRoot:
    return SafetyRoot(
        available_at=available_at,
        kind=kind,
        producer_namespace=SourceNamespace("runtime.safety"),
        producer_sequence=sequence,
        subject=subject,
    )


def _timer(
    *,
    available_at: datetime = ROOT_TIME,
    kind: TimerKind = TimerKind.STRATEGY_TIMER,
    sequence: int = 2,
    timer_id: str = "strategy.primary",
) -> TimerRoot:
    return TimerRoot(
        available_at=available_at,
        kind=kind,
        timer_namespace=SourceNamespace("runtime.timer"),
        timer_id=RuntimeIdentifier(timer_id),
        producer_sequence=sequence,
    )


def _end(
    *,
    available_at: datetime = ROOT_TIME,
    kind: EndOfRunKind = EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED,
    sequence: int = 3,
) -> EndOfRunRoot:
    return EndOfRunRoot(
        available_at=available_at,
        kind=kind,
        producer_namespace=SourceNamespace("runtime.end"),
        producer_sequence=sequence,
        run_id=RUN_ID,
    )


def test_closed_rank_registries_match_accepted_adrs() -> None:
    assert type(RUNTIME_ROOT_DOMAIN_RANKS) is MappingProxyType
    assert dict(RUNTIME_ROOT_DOMAIN_RANKS) == {
        RuntimeRootDomain.SAFETY: 0,
        RuntimeRootDomain.EXECUTION_FACT: 10,
        RuntimeRootDomain.RECONCILIATION_OBSERVATION: 20,
        RuntimeRootDomain.MARKET_DATA: 30,
        RuntimeRootDomain.TIMER: 40,
        RuntimeRootDomain.END_OF_RUN: 50,
    }
    assert dict(SAFETY_KIND_RANKS) == {
        SafetyKind.HALT: 0,
        SafetyKind.FAILURE_CUTOVER: 10,
        SafetyKind.STOP_CUTOVER: 20,
    }
    assert dict(EXECUTION_FACT_KIND_RANKS) == {
        ExecutionFactKind.TRADE: 0,
        ExecutionFactKind.REJECTION: 10,
        ExecutionFactKind.ACKNOWLEDGEMENT: 20,
        ExecutionFactKind.EXPIRY: 30,
        ExecutionFactKind.CANCELLATION: 40,
        ExecutionFactKind.SUBMISSION_QUERY: 50,
    }
    assert dict(RECONCILIATION_OBSERVATION_KIND_RANKS) == {
        ReconciliationObservationKind.TRADE_DETAIL: 0,
        ReconciliationObservationKind.ORDER_DETAIL: 10,
        ReconciliationObservationKind.POSITION_SNAPSHOT: 20,
        ReconciliationObservationKind.CASH_SNAPSHOT: 30,
    }
    assert dict(MARKET_DATA_KIND_RANKS) == {MarketDataKind.BAR: 0}
    assert dict(TIMER_KIND_RANKS) == {
        TimerKind.SAFETY_DEADLINE: 0,
        TimerKind.STRATEGY_TIMER: 10,
        TimerKind.MAINTENANCE: 20,
    }
    assert dict(END_OF_RUN_KIND_RANKS) == {
        EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED: 0,
        EndOfRunKind.REQUESTED_END: 10,
    }


def test_literal_root_keys_freeze_every_supported_suffix() -> None:
    absent = _safety()
    present = _safety(
        sequence=2,
        subject=SafetySubject(
            RuntimeIdentifier("execution.order"),
            RuntimeIdentifier("order-7"),
        ),
    )
    fact = _fact_ingress()
    market = _market()
    timer = _timer()
    end = _end()

    assert runtime_root_order_key(absent).as_tuple() == (
        ROOT_TIME,
        0,
        0,
        "runtime.safety",
        1,
        0,
        "",
        "",
    )
    assert runtime_root_order_key(present).as_tuple() == (
        ROOT_TIME,
        0,
        0,
        "runtime.safety",
        2,
        1,
        "execution.order",
        "order-7",
    )
    assert runtime_root_order_key(fact).as_tuple() == (
        ROOT_TIME,
        10,
        20,
        "sim.primary",
        4,
    )
    assert runtime_root_order_key(market).as_tuple() == (
        ROOT_TIME,
        30,
        *admission_order_key(market)[1:],
    )
    assert runtime_root_order_key(timer).as_tuple() == (
        ROOT_TIME,
        40,
        10,
        "runtime.timer",
        "strategy.primary",
        2,
    )
    assert runtime_root_order_key(end).as_tuple() == (
        ROOT_TIME,
        50,
        0,
        "runtime.end",
        3,
        RUN_ID.value,
    )


def test_equal_availability_uses_domain_then_local_rank() -> None:
    roots: tuple[RuntimeRoot, ...] = (
        _end(),
        _timer(),
        _market(),
        _fact_ingress(kind=ExecutionFactKind.ACKNOWLEDGEMENT),
        _fact_ingress(
            kind=ExecutionFactKind.REJECTION,
            ingress_sequence=5,
            external_id="fact-5",
        ),
        _safety(),
    )

    plan = prepare_bounded_runtime_roots(roots)

    assert tuple(type(root) for root in plan.roots) == (
        SafetyRoot,
        ExecutionFactIngress,
        ExecutionFactIngress,
        MarketDataEnvelope,
        TimerRoot,
        EndOfRunRoot,
    )
    fact_kinds = tuple(root.fact.kind for root in plan.roots if type(root) is ExecutionFactIngress)
    assert fact_kinds == (
        ExecutionFactKind.REJECTION,
        ExecutionFactKind.ACKNOWLEDGEMENT,
    )


def test_fact_order_uses_ingress_availability_and_never_occurred_at() -> None:
    first = _fact_ingress(
        occurred_at=ROOT_TIME - timedelta(hours=3),
        available_at=ROOT_TIME,
        ingress_sequence=10,
        external_id="late-old-fact",
    )
    second = _fact_ingress(
        occurred_at=ROOT_TIME,
        available_at=ROOT_TIME + timedelta(microseconds=1),
        ingress_sequence=1,
        external_id="new-fact",
    )

    assert prepare_bounded_runtime_roots((second, first)).roots == (first, second)


def test_redelivery_has_a_new_ordered_ingress_root() -> None:
    first = _fact_ingress(ingress_sequence=4)
    second = create_execution_fact_ingress(
        available_at=first.available_at,
        source_namespace=first.source_namespace,
        ingress_sequence=5,
        fact=first.fact,
    )

    plan = prepare_bounded_runtime_roots((second, first))

    assert plan.roots == (first, second)
    assert first.fact == second.fact


def test_bounded_plan_materializes_once_and_is_permutation_invariant() -> None:
    roots: tuple[RuntimeRoot, ...] = (
        _safety(),
        _fact_ingress(),
        _market(),
        _timer(),
        _end(),
    )

    class OneShot:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> Iterator[RuntimeRoot]:
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("root input was reopened")
            return iter(reversed(roots))

    candidate = OneShot()

    reverse = prepare_bounded_runtime_roots(candidate)
    forward = prepare_bounded_runtime_roots(roots)

    assert candidate.iterations == 1
    assert reverse == forward


@pytest.mark.parametrize(
    ("roots", "message"),
    [
        ((_fact_ingress(), _fact_ingress()), "domain identity"),
        ((_safety(), _safety(kind=SafetyKind.STOP_CUTOVER)), "domain identity"),
        ((_timer(), _timer(timer_id="changed")), "domain identity"),
        ((_end(), _end(kind=EndOfRunKind.REQUESTED_END)), "domain identity"),
        ((_market(), _market()), "market root"),
    ],
)
def test_duplicate_domain_identities_fail_before_sort(
    roots: tuple[RuntimeRoot, RuntimeRoot],
    message: str,
) -> None:
    with pytest.raises(RuntimeOrderingError, match=message) as error:
        prepare_bounded_runtime_roots(roots)

    assert error.value.code is OutcomeCode.CONFLICTING_ID


def test_changed_availability_under_one_producer_sequence_conflicts() -> None:
    with pytest.raises(RuntimeOrderingError) as error:
        prepare_bounded_runtime_roots(
            (
                _safety(),
                _safety(available_at=ROOT_TIME + timedelta(microseconds=1)),
            )
        )

    assert error.value.code is OutcomeCode.CONFLICTING_ID


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: RuntimeIdentifier(cast(str, 1)), OutcomeCode.INVALID_TYPE),
        (lambda: RuntimeIdentifier(""), OutcomeCode.OUT_OF_RANGE),
        (lambda: RuntimeIdentifier("a b"), OutcomeCode.OUT_OF_RANGE),
        (
            lambda: SafetyRoot(
                ROOT_TIME,
                SafetyKind.HALT,
                SOURCE_NAMESPACE,
                cast(int, True),
            ),
            OutcomeCode.INVALID_TYPE,
        ),
        (
            lambda: TimerRoot(
                ROOT_TIME,
                TimerKind.MAINTENANCE,
                SOURCE_NAMESPACE,
                RuntimeIdentifier("timer"),
                -1,
            ),
            OutcomeCode.OUT_OF_RANGE,
        ),
        (
            lambda: EndOfRunRoot(
                ROOT_TIME.replace(tzinfo=timezone(timedelta(hours=1))),
                EndOfRunKind.REQUESTED_END,
                SOURCE_NAMESPACE,
                0,
                RUN_ID,
            ),
            OutcomeCode.OUT_OF_RANGE,
        ),
    ],
)
def test_new_values_have_closed_validation_codes(
    factory: Callable[[], object],
    expected: OutcomeCode,
) -> None:
    with pytest.raises(RuntimeOrderingError) as error:
        factory()

    assert error.value.code is expected


def test_values_are_frozen_and_exact_root_types_are_required() -> None:
    root = _safety()
    with pytest.raises(FrozenInstanceError):
        root.producer_sequence = 2  # type: ignore[misc]

    class DerivedSafetyRoot(SafetyRoot):  # type: ignore[misc]
        pass

    derived = DerivedSafetyRoot(
        ROOT_TIME,
        SafetyKind.HALT,
        SourceNamespace("runtime.derived"),
        9,
    )
    with pytest.raises(RuntimeOrderingError) as error:
        prepare_bounded_runtime_roots((derived,))
    assert error.value.code is OutcomeCode.INVALID_TYPE


def test_plan_is_non_empty_factory_only_and_rejects_bad_outer_carriers() -> None:
    with pytest.raises(RuntimeOrderingError) as empty:
        prepare_bounded_runtime_roots(())
    assert empty.value.code is OutcomeCode.OUT_OF_RANGE

    with pytest.raises(RuntimeOrderingError) as outer:
        prepare_bounded_runtime_roots(cast(Iterable[RuntimeRoot], None))
    assert outer.value.code is OutcomeCode.INVALID_TYPE

    with pytest.raises(RuntimeOrderingError) as unsupported:
        prepare_bounded_runtime_roots(cast(Iterable[RuntimeRoot], (object(),)))
    assert unsupported.value.code is OutcomeCode.INVALID_TYPE

    with pytest.raises(RuntimeOrderingError) as forged:
        BoundedRuntimeRootPlan(object(), (_safety(),))
    assert forged.value.code is OutcomeCode.INVALID_TYPE


def test_large_sequences_sort_without_decimal_string_conversion() -> None:
    large = 10**4999
    first = _timer(sequence=large, timer_id="same-timer")
    second = _timer(sequence=large + 1, timer_id="same-timer")

    first_key = runtime_root_order_key(first).as_tuple()
    second_key = runtime_root_order_key(second).as_tuple()

    assert first_key[:-1] == second_key[:-1]
    assert first_key[-1] == large
    assert second_key[-1] == large + 1
    assert type(first_key[-1]) is int
    assert type(second_key[-1]) is int

    plan = prepare_bounded_runtime_roots((second, first))

    assert plan.roots == (first, second)


def test_queue_consumes_exact_plan_once_without_mutating_on_exhaustion() -> None:
    plan = prepare_bounded_runtime_roots((_end(), _safety(), _market()))
    queue = create_deterministic_root_queue(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        plan=plan,
        fact_issuance_verifiers=(),
    )

    assert queue.remaining == 3
    assert queue.peek() is plan.roots[0]
    assert queue.remaining == 3
    observed = []
    for expected_sequence in range(1, 4):
        lease = queue.pop()
        observed.append(lease.root)
        assert lease.dispatch_sequence == expected_sequence
        queue.acknowledge(lease)
    assert tuple(observed) == plan.roots
    assert queue.remaining == 0

    for operation in (queue.peek, queue.pop):
        with pytest.raises(RuntimeOrderingError) as error:
            operation()
        assert error.value.code is OutcomeCode.OUT_OF_RANGE
        assert queue.remaining == 0


def test_queue_rejects_duck_typed_or_subclassed_plan() -> None:
    with pytest.raises(TypeError):
        DeterministicRootQueue()

    class FakePlan:
        roots = (_safety(),)

    with pytest.raises(RuntimeOrderingError) as fake:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=SPEC_SET,
            plan=cast(BoundedRuntimeRootPlan, FakePlan()),
            fact_issuance_verifiers=(),
        )
    assert fake.value.code is OutcomeCode.INVALID_TYPE

    plan = prepare_bounded_runtime_roots((_safety(),))
    forged_exact = object.__new__(BoundedRuntimeRootPlan)
    object.__setattr__(forged_exact, "_roots", plan.roots)
    with pytest.raises(RuntimeOrderingError) as unsealed:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=SPEC_SET,
            plan=forged_exact,
            fact_issuance_verifiers=(),
        )
    assert unsealed.value.code is OutcomeCode.INVALID_TYPE

    class DerivedPlan(BoundedRuntimeRootPlan):  # type: ignore[misc]
        pass

    derived = object.__new__(DerivedPlan)
    object.__setattr__(derived, "_roots", plan.roots)
    with pytest.raises(RuntimeOrderingError) as subclass:
        create_deterministic_root_queue(
            run_id=RUN_ID,
            spec_set=SPEC_SET,
            plan=derived,
            fact_issuance_verifiers=(),
        )
    assert subclass.value.code is OutcomeCode.INVALID_TYPE


def test_public_boundary_has_no_generic_root_or_mutable_queue_escape_hatch() -> None:
    public_runtime_names = {
        name
        for name, value in getmembers(runtime_module)
        if not name.startswith("_") and (isfunction(value) or isinstance(value, type))
    }
    assert "ReconciliationObservationRoot" not in public_runtime_names
    assert "RuntimeRootEnvelope" not in public_runtime_names
    assert {
        name
        for name in dir(DeterministicRootQueue)
        if name in {"put", "push", "insert", "reschedule", "cancel", "advance_clock"}
    } == set()
