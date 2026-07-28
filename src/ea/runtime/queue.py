"""Run-bound single-active dispatch over one sealed bounded root plan."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, cast, final

from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import IngressIdentity, SourceNamespace
from ea.core.execution_messages import (
    ExecutionFactIngress,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId
from ea.core.runtime import (
    BoundedRuntimeRootPlan,
    RuntimeOrderingError,
    RuntimeRoot,
    _require_bounded_runtime_root_plan,
)

_MAX_UINT64 = (1 << 64) - 1


class ExecutionFactIssuanceVerifier(Protocol):
    """Queue-owned structural port for trusted source issuance."""

    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    @property
    def source_namespace(self) -> SourceNamespace: ...

    def has_issued_ingress(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> bool: ...


@final
@dataclass(frozen=True, slots=True, init=False)
class RuntimeDispatchLease:
    """Ephemeral exact in-process capability for one active root dispatch."""

    _root: RuntimeRoot
    _dispatch_sequence: int

    def __init__(self) -> None:
        raise TypeError(
            "RuntimeDispatchLease values are created only by DeterministicRootQueue.pop"
        )

    @property
    def root(self) -> RuntimeRoot:
        return self._root

    @property
    def dispatch_sequence(self) -> int:
        return self._dispatch_sequence


@dataclass(frozen=True, slots=True)
class _ActiveDispatch:
    lease: RuntimeDispatchLease
    root: RuntimeRoot
    dispatch_sequence: int
    ingress_identity: IngressIdentity | None
    ingress_bytes: bytes | None
    fact_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class _AcknowledgedFactDispatch:
    dispatch_sequence: int
    ingress_identity: IngressIdentity
    ingress_bytes: bytes
    fact_bytes: bytes


@dataclass(frozen=True, slots=True)
class _QueueState:
    cursor: int
    dispatch_next: int | None
    active: _ActiveDispatch | None
    acknowledged_fact_dispatches: tuple[_AcknowledgedFactDispatch, ...]


@final
class DeterministicRootQueue:
    """Consume one immutable plan with exactly one live dispatch lease."""

    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet
    _plan: BoundedRuntimeRootPlan
    _fact_issuance_verifiers: MappingProxyType[
        SourceNamespace,
        ExecutionFactIssuanceVerifier,
    ]
    _state: _QueueState

    __slots__ = (
        "_fact_issuance_verifiers",
        "_plan",
        "_run_id",
        "_spec_set",
        "_state",
    )

    def __init__(self) -> None:
        raise TypeError(
            "DeterministicRootQueue values are created only by create_deterministic_root_queue"
        )

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    @property
    def remaining(self) -> int:
        return len(self._plan) - self._state.cursor

    @property
    def active_lease(self) -> RuntimeDispatchLease | None:
        active = self._state.active
        return None if active is None else active.lease

    @property
    def acknowledged_fact_dispatch_count(self) -> int:
        return len(self._state.acknowledged_fact_dispatches)

    def _current(self) -> RuntimeRoot:
        if self._state.cursor >= len(self._plan):
            raise RuntimeOrderingError(
                OutcomeCode.OUT_OF_RANGE,
                "bounded runtime root queue is exhausted",
            )
        return self._plan.roots[self._state.cursor]

    def peek(self) -> RuntimeRoot:
        """Return the next root without creating dispatch evidence."""
        return self._current()

    def pop(self) -> RuntimeDispatchLease:
        """Begin exactly one active dispatch and return its live lease."""
        if self._state.active is not None:
            raise RuntimeOrderingError(
                OutcomeCode.CONFLICTING_ID,
                "another runtime root dispatch remains active",
            )
        root = self._current()
        dispatch_sequence = self._state.dispatch_next
        if dispatch_sequence is None:
            raise RuntimeOrderingError(
                OutcomeCode.OUT_OF_RANGE,
                "runtime dispatch sequence is exhausted",
            )
        ingress_identity: IngressIdentity | None = None
        ingress_bytes: bytes | None = None
        fact_bytes: bytes | None = None
        if type(root) is ExecutionFactIngress:
            ingress_identity = root.identity
            ingress_bytes = canonical_execution_fact_ingress_bytes(root)
            fact_bytes = canonical_execution_fact_bytes(root.fact)
            verifier = self._fact_issuance_verifiers[root.source_namespace]
            try:
                issued = verifier.has_issued_ingress(
                    ingress_identity=ingress_identity,
                    canonical_ingress_bytes=ingress_bytes,
                    canonical_fact_bytes=fact_bytes,
                )
            except (AttributeError, TypeError) as error:
                raise RuntimeOrderingError(
                    OutcomeCode.INVALID_TYPE,
                    "fact issuance verifier operation contract failed",
                ) from error
            if type(issued) is not bool:
                raise RuntimeOrderingError(
                    OutcomeCode.INVALID_TYPE,
                    "fact issuance verifier must return an exact bool",
                )
            if not issued:
                raise RuntimeOrderingError(
                    OutcomeCode.CONFLICTING_ID,
                    "fact root was not issued by its bound source authority",
                )
        lease = object.__new__(RuntimeDispatchLease)
        object.__setattr__(lease, "_root", root)
        object.__setattr__(lease, "_dispatch_sequence", dispatch_sequence)
        active = _ActiveDispatch(
            lease=lease,
            root=root,
            dispatch_sequence=dispatch_sequence,
            ingress_identity=ingress_identity,
            ingress_bytes=ingress_bytes,
            fact_bytes=fact_bytes,
        )
        next_state = _QueueState(
            cursor=self._state.cursor + 1,
            dispatch_next=_advance(dispatch_sequence),
            active=active,
            acknowledged_fact_dispatches=self._state.acknowledged_fact_dispatches,
        )
        _preflight_pop(next_state, root=root, lease=lease)
        self._state = next_state
        return lease

    def acknowledge(self, lease: RuntimeDispatchLease) -> None:
        """Consume the exact live lease and clear the active dispatch."""
        if type(lease) is not RuntimeDispatchLease:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "acknowledgement requires an exact RuntimeDispatchLease",
            )
        active = self._state.active
        if active is None or active.lease is not lease:
            raise RuntimeOrderingError(
                OutcomeCode.CONFLICTING_ID,
                "acknowledgement does not match the exact live dispatch lease",
            )
        history = self._state.acknowledged_fact_dispatches
        if (
            active.ingress_identity is not None
            and active.ingress_bytes is not None
            and active.fact_bytes is not None
        ):
            history = (
                *history,
                _AcknowledgedFactDispatch(
                    dispatch_sequence=active.dispatch_sequence,
                    ingress_identity=active.ingress_identity,
                    ingress_bytes=active.ingress_bytes,
                    fact_bytes=active.fact_bytes,
                ),
            )
        next_state = _QueueState(
            cursor=self._state.cursor,
            dispatch_next=self._state.dispatch_next,
            active=None,
            acknowledged_fact_dispatches=history,
        )
        _preflight_acknowledge(next_state, active=active)
        self._state = next_state

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None:
        """Resolve the only exact current issued fact dispatch, if any."""
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "dispatch lookup requires exact identity and canonical bytes",
            )
        active = self._state.active
        if (
            active is None
            or active.ingress_identity != ingress_identity
            or active.ingress_bytes != canonical_ingress_bytes
            or active.fact_bytes != canonical_fact_bytes
        ):
            return None
        return active.dispatch_sequence


def create_deterministic_root_queue(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    plan: BoundedRuntimeRootPlan,
    fact_issuance_verifiers: tuple[ExecutionFactIssuanceVerifier, ...],
) -> DeterministicRootQueue:
    """Create one run/spec-bound queue with exact source capabilities."""
    if type(run_id) is not RunId:
        raise RuntimeOrderingError(
            OutcomeCode.INVALID_TYPE,
            "run_id must be an exact RunId",
        )
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise RuntimeOrderingError(
            OutcomeCode.INVALID_TYPE,
            "spec_set must be an exact InstrumentExecutionSpecSet",
        )
    bounded_plan = _require_bounded_runtime_root_plan(plan)
    if type(fact_issuance_verifiers) is not tuple:
        raise RuntimeOrderingError(
            OutcomeCode.INVALID_TYPE,
            "fact_issuance_verifiers must be an exact tuple",
        )
    required_sources = tuple(
        sorted(
            {
                root.source_namespace
                for root in bounded_plan.roots
                if type(root) is ExecutionFactIngress
            },
            key=lambda source: source.value,
        )
    )
    source_map: dict[SourceNamespace, ExecutionFactIssuanceVerifier] = {}
    seen_sources: list[SourceNamespace] = []
    expected_spec_sha256 = instrument_spec_set_digest(spec_set)
    for verifier_object in fact_issuance_verifiers:
        verifier = _require_verifier_shape(verifier_object)
        try:
            verifier_run_id = verifier.run_id
            verifier_spec_set = verifier.spec_set
            verifier_source = verifier.source_namespace
        except (AttributeError, TypeError) as error:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "fact issuance verifier has an incomplete construction contract",
            ) from error
        if (
            type(verifier_run_id) is not RunId
            or type(verifier_spec_set) is not InstrumentExecutionSpecSet
            or type(verifier_source) is not SourceNamespace
        ):
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "fact issuance verifier bindings require exact canonical types",
            )
        if (
            verifier_run_id != run_id
            or verifier_spec_set.identifier != spec_set.identifier
            or instrument_spec_set_digest(verifier_spec_set) != expected_spec_sha256
        ):
            raise RuntimeOrderingError(
                OutcomeCode.CONFLICTING_ID,
                "fact issuance verifier run/specification binding conflicts",
            )
        seen_sources.append(verifier_source)
        if verifier_source in source_map:
            raise RuntimeOrderingError(
                OutcomeCode.CONFLICTING_ID,
                "fact issuance verifier tuple contains a duplicate source",
            )
        source_map[verifier_source] = verifier
    if tuple(source.value for source in seen_sources) != tuple(
        sorted(source.value for source in seen_sources)
    ):
        raise RuntimeOrderingError(
            OutcomeCode.CONFLICTING_ID,
            "fact issuance verifiers are not in canonical source order",
        )
    if tuple(seen_sources) != required_sources:
        raise RuntimeOrderingError(
            OutcomeCode.CONFLICTING_ID,
            "fact issuance verifier tuple is missing, extra, or source-mismatched",
        )
    queue = object.__new__(DeterministicRootQueue)
    queue._run_id = run_id
    queue._spec_set = spec_set
    queue._plan = bounded_plan
    queue._fact_issuance_verifiers = MappingProxyType(dict(source_map))
    queue._state = _QueueState(
        cursor=0,
        dispatch_next=1,
        active=None,
        acknowledged_fact_dispatches=(),
    )
    return queue


def _require_verifier_shape(
    verifier: object,
) -> ExecutionFactIssuanceVerifier:
    candidate = cast(Any, verifier)
    try:
        membership = candidate.has_issued_ingress
    except (AttributeError, TypeError) as error:
        raise RuntimeOrderingError(
            OutcomeCode.INVALID_TYPE,
            "fact issuance verifier lacks its membership operation",
        ) from error
    if not callable(membership):
        raise RuntimeOrderingError(
            OutcomeCode.INVALID_TYPE,
            "fact issuance membership operation must be callable",
        )
    return cast(ExecutionFactIssuanceVerifier, verifier)


def _advance(value: int) -> int | None:
    if type(value) is not int or not 1 <= value <= _MAX_UINT64:
        raise AssertionError("runtime dispatch sequence state is invalid")
    return None if value == _MAX_UINT64 else value + 1


def _preflight_pop(
    state: _QueueState,
    *,
    root: RuntimeRoot,
    lease: RuntimeDispatchLease,
) -> None:
    if (
        state.active is None
        or state.active.lease is not lease
        or state.active.root is not root
        or state.active.dispatch_sequence != lease.dispatch_sequence
        or lease.root is not root
        or lease.dispatch_sequence < 1
    ):
        raise AssertionError("candidate runtime pop state failed preflight")


def _preflight_acknowledge(
    state: _QueueState,
    *,
    active: _ActiveDispatch,
) -> None:
    if state.active is not None:
        raise AssertionError("candidate acknowledgement did not clear active dispatch")
    if active.ingress_identity is not None and (
        not state.acknowledged_fact_dispatches
        or state.acknowledged_fact_dispatches[-1].dispatch_sequence != active.dispatch_sequence
    ):
        raise AssertionError("candidate acknowledgement lost fact dispatch history")
