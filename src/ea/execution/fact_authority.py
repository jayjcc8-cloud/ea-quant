"""Trusted deterministic execution-fact processing and Order projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NoReturn, Protocol, cast, final

from ea.core.economics import CanonicalDecimal, EconomicValidationError
from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    ExecutionIdentityError,
    FactDedupKey,
    IngressIdentity,
    SourceNamespace,
)
from ea.core.execution_messages import (
    ExecutionFact,
    ExecutionFactIngress,
    ExecutionFactKind,
    ExecutionMessageError,
    Fill,
    IndependentFactDecodeContext,
    LifecycleFactPayload,
    Order,
    SubmissionQueryFactPayload,
    TradeFactPayload,
    VenueOrderId,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_fill_bytes,
    canonical_order_bytes,
    create_fill,
    decode_execution_fact,
    execution_fact_digest,
    execution_fact_ingress_digest,
    fill_digest,
    order_client_submission_key,
)
from ea.core.execution_state import (
    EXECUTION_FACT_ANOMALY_RANKS,
    ExecutionFactAction,
    ExecutionFactAnomaly,
    ExecutionFactProcessingOutcome,
    ExecutionStateError,
    OrderProjectionSnapshot,
    OrderProjectionState,
    OrderResolutionBinding,
    OrderResolutionKeyKind,
    canonical_execution_fact_processing_outcome_bytes,
    canonical_order_projection_snapshot_bytes,
    create_execution_fact_processing_outcome,
    create_order_projection_snapshot,
    create_order_resolution_binding,
    execution_fact_processing_outcome_digest,
    order_projection_snapshot_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunContractError, RunId, Sha256Digest
from ea.core.time import TimeValidationError

_MAX_UINT64 = (1 << 64) - 1
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)
_TERMINAL_STATES = frozenset(
    {
        OrderProjectionState.FILLED,
        OrderProjectionState.REJECTED,
        OrderProjectionState.EXPIRED,
        OrderProjectionState.CANCELLED,
        OrderProjectionState.DEFINITELY_NOT_SUBMITTED,
    }
)
_ALLOWED_NON_TERMINAL_TRANSITIONS = {
    OrderProjectionState.SUBMITTED: frozenset(
        {
            OrderProjectionState.ACKNOWLEDGED,
            OrderProjectionState.REJECTED,
            OrderProjectionState.PARTIALLY_FILLED,
            OrderProjectionState.FILLED,
            OrderProjectionState.EXPIRED,
            OrderProjectionState.CANCELLED,
        }
    ),
    OrderProjectionState.ACKNOWLEDGED: frozenset(
        {
            OrderProjectionState.PARTIALLY_FILLED,
            OrderProjectionState.FILLED,
            OrderProjectionState.EXPIRED,
            OrderProjectionState.CANCELLED,
        }
    ),
    OrderProjectionState.PARTIALLY_FILLED: frozenset(
        {
            OrderProjectionState.PARTIALLY_FILLED,
            OrderProjectionState.FILLED,
            OrderProjectionState.EXPIRED,
            OrderProjectionState.CANCELLED,
        }
    ),
}
_LIFECYCLE_PROJECTION = {
    ExecutionFactKind.ACKNOWLEDGEMENT: OrderProjectionState.ACKNOWLEDGED,
    ExecutionFactKind.REJECTION: OrderProjectionState.REJECTED,
    ExecutionFactKind.EXPIRY: OrderProjectionState.EXPIRED,
    ExecutionFactKind.CANCELLATION: OrderProjectionState.CANCELLED,
}
_QUERY_PROJECTION = {
    OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED: (OrderProjectionState.SUBMITTED),
    OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_NOT_SUBMITTED: (
        OrderProjectionState.DEFINITELY_NOT_SUBMITTED
    ),
    OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_REJECTED: (OrderProjectionState.REJECTED),
    OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED: OrderProjectionState.FILLED,
}


class ExecutionFactAuthorityError(ValueError):
    """Structured fail-closed fact-authority boundary error."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("fact-authority errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


class OrderResolutionVerifier(Protocol):
    """Execution-owned read-only port for issued Order resolution."""

    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    def resolve_issued_order_by_id(self, order_id: EconomicId) -> Order | None: ...

    def resolve_issued_order_by_client_submission_key(
        self,
        client_submission_key: Sha256Digest,
    ) -> Order | None: ...


class RuntimeFactDispatchVerifier(Protocol):
    """Execution-owned read-only port for the exact current fact dispatch."""

    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None: ...


@dataclass(frozen=True, slots=True)
class _IngressRecord:
    ingress: ExecutionFactIngress
    ingress_bytes: bytes
    fact_bytes: bytes
    outcome: ExecutionFactProcessingOutcome


@dataclass(frozen=True, slots=True)
class _FactRecord:
    fact: ExecutionFact
    fact_bytes: bytes
    first_ingress_identity: IngressIdentity


@dataclass(frozen=True, slots=True)
class _FactAuthorityState:
    fill_next: int | None
    halt_requested: bool
    ingress_index: Mapping[IngressIdentity, _IngressRecord]
    fact_index: Mapping[FactDedupKey, _FactRecord]
    fill_index: Mapping[EconomicId, Fill]
    order_index: Mapping[EconomicId, Order]
    client_key_index: Mapping[Sha256Digest, Order]
    venue_index: Mapping[tuple[SourceNamespace, VenueOrderId], Order]
    projection_index: Mapping[EconomicId, OrderProjectionSnapshot]
    projection_history_index: Mapping[tuple[EconomicId, Sha256Digest], OrderProjectionSnapshot]
    observed_quantity: Mapping[EconomicId, CanonicalDecimal]
    ingresses: tuple[ExecutionFactIngress, ...]
    outcomes: tuple[ExecutionFactProcessingOutcome, ...]
    first_facts: tuple[ExecutionFact, ...]
    fills: tuple[Fill, ...]
    projections: tuple[OrderProjectionSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _Resolution:
    bindings: tuple[OrderResolutionBinding, ...]
    selected_order: Order | None
    resolved_orders: tuple[Order, ...]
    staged_venue_key: tuple[SourceNamespace, VenueOrderId] | None
    anomalies: frozenset[ExecutionFactAnomaly]


@dataclass(frozen=True, slots=True)
class _ProjectionResult:
    before: OrderProjectionSnapshot | None
    after: OrderProjectionSnapshot | None
    new_projection: OrderProjectionSnapshot | None
    observed_quantity: CanonicalDecimal | None
    anomalies: frozenset[ExecutionFactAnomaly]


@final
class Phase1ExecutionFactAuthority:
    """Sole mutable owner of trusted fact outcomes, Fills, and projections."""

    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet
    _spec_set_sha256: Sha256Digest
    _order_verifier: OrderResolutionVerifier
    _dispatch_verifier: RuntimeFactDispatchVerifier
    _state: _FactAuthorityState

    __slots__ = (
        "_dispatch_verifier",
        "_order_verifier",
        "_run_id",
        "_spec_set",
        "_spec_set_sha256",
        "_state",
    )

    def __init__(self) -> None:
        raise TypeError(
            "Phase1ExecutionFactAuthority values are created only by "
            "create_phase1_execution_fact_authority"
        )

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    @property
    def next_fill_sequence(self) -> int | None:
        return self._state.fill_next

    @property
    def halt_requested(self) -> bool:
        return self._state.halt_requested

    @property
    def ingresses(self) -> tuple[ExecutionFactIngress, ...]:
        return self._state.ingresses

    @property
    def outcomes(self) -> tuple[ExecutionFactProcessingOutcome, ...]:
        return self._state.outcomes

    @property
    def first_facts(self) -> tuple[ExecutionFact, ...]:
        return self._state.first_facts

    @property
    def fills(self) -> tuple[Fill, ...]:
        return self._state.fills

    @property
    def projections(self) -> tuple[OrderProjectionSnapshot, ...]:
        return self._state.projections

    def projection_for_order(
        self,
        order_id: EconomicId,
    ) -> OrderProjectionSnapshot | None:
        """Return the current immutable projection for one Order ID."""
        if type(order_id) is not EconomicId:
            raise ExecutionFactAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "order_id must be an exact EconomicId",
            )
        return self._state.projection_index.get(order_id)

    def resolve_processing_outcome(
        self,
        *,
        ingress_identity: IngressIdentity,
        ingress_sha256: Sha256Digest,
    ) -> ExecutionFactProcessingOutcome | None:
        """Resolve one exact retained outcome without scanning public history."""
        if (
            type(ingress_identity) is not IngressIdentity
            or type(ingress_sha256) is not Sha256Digest
        ):
            raise ExecutionFactAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "outcome resolution requires exact ingress identity and digest",
            )
        record = self._state.ingress_index.get(ingress_identity)
        if record is None or execution_fact_ingress_digest(record.ingress) != ingress_sha256:
            return None
        return record.outcome

    def resolve_fill(
        self,
        *,
        fill_id: EconomicId,
        fill_sha256: Sha256Digest,
    ) -> Fill | None:
        """Resolve one exact retained Fill by identity and canonical digest."""
        if type(fill_id) is not EconomicId or type(fill_sha256) is not Sha256Digest:
            raise ExecutionFactAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "Fill resolution requires exact identity and digest",
            )
        value = self._state.fill_index.get(fill_id)
        if value is None or fill_digest(value) != fill_sha256:
            return None
        return value

    def resolve_projection_after(
        self,
        *,
        order_id: EconomicId,
        projection_sha256: Sha256Digest,
    ) -> OrderProjectionSnapshot | None:
        """Resolve a historical projection version, not merely the current snapshot."""
        if type(order_id) is not EconomicId or type(projection_sha256) is not Sha256Digest:
            raise ExecutionFactAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "projection resolution requires exact identity and digest",
            )
        return self._state.projection_history_index.get((order_id, projection_sha256))

    def observed_quantity_for_order(
        self,
        order_id: EconomicId,
    ) -> CanonicalDecimal:
        """Return exact coherent observed Fill quantity for one Order."""
        if type(order_id) is not EconomicId:
            raise ExecutionFactAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "order_id must be an exact EconomicId",
            )
        return self._state.observed_quantity.get(order_id, CanonicalDecimal("0"))

    def process_ingress(
        self,
        ingress: ExecutionFactIngress,
    ) -> ExecutionFactProcessingOutcome:
        """Process one exact current source-issued ingress atomically."""
        try:
            submitted = _materialize_ingress(ingress)
            replay = self._classify_ingress_replay(
                ingress=ingress,
                ingress_bytes=submitted[0],
                fact_bytes=submitted[1],
            )
            if replay is not None:
                return replay
            dispatch_sequence = _require_active_dispatch(
                self._dispatch_verifier,
                ingress=ingress,
                ingress_bytes=submitted[0],
                fact_bytes=submitted[1],
            )
            fact = ingress.fact
            existing_fact = self._state.fact_index.get(fact.dedup_key)
            if existing_fact is not None:
                action = (
                    ExecutionFactAction.DUPLICATE
                    if existing_fact.fact_bytes == submitted[1]
                    else ExecutionFactAction.CONFLICT
                )
                return self._publish_short_circuit(
                    ingress=ingress,
                    ingress_bytes=submitted[0],
                    fact_bytes=submitted[1],
                    dispatch_sequence=dispatch_sequence,
                    action=action,
                )
            if not _is_context_valid(
                ingress,
                run_id=self._run_id,
                spec_set=self._spec_set,
                spec_set_sha256=self._spec_set_sha256,
            ):
                return self._publish_first_fact(
                    ingress=ingress,
                    ingress_bytes=submitted[0],
                    fact_bytes=submitted[1],
                    dispatch_sequence=dispatch_sequence,
                    action=ExecutionFactAction.INVALID,
                    anomalies=(ExecutionFactAnomaly.CONTEXT_INVALID,),
                    resolution=_Resolution((), None, (), None, frozenset()),
                    fill=None,
                    projection=_ProjectionResult(None, None, None, None, frozenset()),
                )

            resolution = self._resolve_orders(ingress)
            fill, fill_after = self._build_fill(ingress)
            anomalies = set(resolution.anomalies)
            if fill is not None and (
                fill.order_id is None or fill.correlation_id is None or fill.causation_id is None
            ):
                anomalies.add(ExecutionFactAnomaly.MISSING_ANCESTRY)
            projection = self._project(
                ingress=ingress,
                fill=fill,
                resolution=resolution,
                inherited_anomalies=frozenset(anomalies),
            )
            anomalies.update(projection.anomalies)
            ordered_anomalies = tuple(
                sorted(anomalies, key=EXECUTION_FACT_ANOMALY_RANKS.__getitem__)
            )
            action = (
                ExecutionFactAction.ACCEPTED
                if not ordered_anomalies
                else ExecutionFactAction.UNRESOLVED
            )
            return self._publish_first_fact(
                ingress=ingress,
                ingress_bytes=submitted[0],
                fact_bytes=submitted[1],
                dispatch_sequence=dispatch_sequence,
                action=action,
                anomalies=ordered_anomalies,
                resolution=resolution,
                fill=fill,
                projection=projection,
                fill_after=fill_after,
            )
        except ExecutionFactAuthorityError:
            raise
        except (
            EconomicValidationError,
            ExecutionIdentityError,
            ExecutionMessageError,
            ExecutionStateError,
            RunContractError,
            TimeValidationError,
        ) as error:
            _raise_public(error)
        except (AttributeError, TypeError) as error:
            raise ExecutionFactAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "submitted fact carrier is structurally incomplete",
            ) from error

    def _classify_ingress_replay(
        self,
        *,
        ingress: ExecutionFactIngress,
        ingress_bytes: bytes,
        fact_bytes: bytes,
    ) -> ExecutionFactProcessingOutcome | None:
        record = self._state.ingress_index.get(ingress.identity)
        if record is None:
            return None
        if record.ingress_bytes == ingress_bytes and record.fact_bytes == fact_bytes:
            return record.outcome
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "ingress identity is occupied by conflicting canonical bytes",
        )

    def _publish_short_circuit(
        self,
        *,
        ingress: ExecutionFactIngress,
        ingress_bytes: bytes,
        fact_bytes: bytes,
        dispatch_sequence: int,
        action: ExecutionFactAction,
    ) -> ExecutionFactProcessingOutcome:
        outcome = create_execution_fact_processing_outcome(
            run_id=self._run_id,
            runtime_dispatch_sequence=dispatch_sequence,
            ingress=ingress,
            action=action,
            anomalies=(),
            order_resolutions=(),
            resolved_order=None,
            fill=None,
            projection_before=None,
            projection_after=None,
        )
        next_state = _state_with_ingress_outcome(
            self._state,
            ingress=ingress,
            ingress_bytes=ingress_bytes,
            fact_bytes=fact_bytes,
            outcome=outcome,
            halt_requested=(self._state.halt_requested or action is ExecutionFactAction.CONFLICT),
        )
        _preflight_candidate(next_state, ingress=ingress, outcome=outcome)
        self._state = next_state
        return outcome

    def _resolve_orders(self, ingress: ExecutionFactIngress) -> _Resolution:
        fact = ingress.fact
        resolved: list[tuple[OrderResolutionKeyKind, Order | None]] = []
        orders: dict[EconomicId, Order] = {}
        if fact.order_id is not None:
            order = _resolve_order_id(self._order_verifier, fact.order_id)
            _record_resolved_order(
                order,
                run_id=self._run_id,
                spec_set=self._spec_set,
                spec_set_sha256=self._spec_set_sha256,
                orders=orders,
            )
            resolved.append((OrderResolutionKeyKind.ORDER_ID, order))
        if fact.client_submission_key is not None:
            order = _resolve_client_key(
                self._order_verifier,
                fact.client_submission_key,
            )
            _record_resolved_order(
                order,
                run_id=self._run_id,
                spec_set=self._spec_set,
                spec_set_sha256=self._spec_set_sha256,
                orders=orders,
            )
            resolved.append((OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY, order))

        order_client_values = tuple(order for _kind, order in resolved)
        non_null_base = tuple(order for order in order_client_values if order is not None)
        base_ids = {order.order_id for order in non_null_base}
        base_coherent = (
            bool(resolved) and len(non_null_base) == len(resolved) and len(base_ids) == 1
        )
        staged_venue_key: tuple[SourceNamespace, VenueOrderId] | None = None
        venue_identity_conflict = False
        if fact.venue_order_id is not None:
            venue_key = (fact.source_namespace, fact.venue_order_id)
            venue_order = self._state.venue_index.get(venue_key)
            if venue_order is None and base_coherent:
                candidate_order = non_null_base[0]
                existing_keys = tuple(
                    existing_key
                    for existing_key, mapped_order in self._state.venue_index.items()
                    if mapped_order.order_id == candidate_order.order_id
                )
                if existing_keys and venue_key not in existing_keys:
                    venue_identity_conflict = True
                else:
                    venue_order = candidate_order
                    staged_venue_key = venue_key
            if venue_order is not None:
                orders[venue_order.order_id] = venue_order
            resolved.append((OrderResolutionKeyKind.VENUE_ORDER_ID, venue_order))

        bindings = tuple(
            create_order_resolution_binding(key_kind=kind, resolved_order=order)
            for kind, order in resolved
        )
        non_null_ids = {
            binding.resolved_order_id
            for binding in bindings
            if binding.resolved_order_id is not None
        }
        selected_id = next(iter(non_null_ids)) if len(non_null_ids) == 1 else None
        selected = None if selected_id is None else orders[selected_id]
        anomalies: set[ExecutionFactAnomaly] = set()
        if not non_null_ids:
            anomalies.add(ExecutionFactAnomaly.UNKNOWN_ORDER)
        if len(non_null_ids) > 1:
            anomalies.add(ExecutionFactAnomaly.ORDER_BINDING_CONFLICT)
        if venue_identity_conflict:
            anomalies.add(ExecutionFactAnomaly.ORDER_BINDING_CONFLICT)
        base_present = tuple(
            (kind, order)
            for kind, order in resolved
            if kind
            in (
                OrderResolutionKeyKind.ORDER_ID,
                OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY,
            )
        )
        if any(order is None for _kind, order in base_present) and any(
            order is not None for _kind, order in base_present
        ):
            anomalies.add(ExecutionFactAnomaly.ORDER_BINDING_CONFLICT)
        if selected is not None and _fact_contradicts_order(fact, selected):
            anomalies.add(ExecutionFactAnomaly.ORDER_BINDING_CONFLICT)
        if (
            staged_venue_key is not None
            and ExecutionFactAnomaly.ORDER_BINDING_CONFLICT in anomalies
        ):
            staged_venue_key = None
            bindings = tuple(
                create_order_resolution_binding(
                    key_kind=binding.key_kind,
                    resolved_order=(
                        None
                        if binding.key_kind is OrderResolutionKeyKind.VENUE_ORDER_ID
                        else (
                            None
                            if binding.resolved_order_id is None
                            else orders[binding.resolved_order_id]
                        )
                    ),
                )
                for binding in bindings
            )
        return _Resolution(
            bindings=bindings,
            selected_order=selected,
            resolved_orders=tuple(
                sorted(
                    orders.values(),
                    key=lambda order: (
                        order.run_id.value,
                        order.order_id.owner_kind.value,
                        order.order_id.owner_sequence,
                    ),
                )
            ),
            staged_venue_key=staged_venue_key,
            anomalies=frozenset(anomalies),
        )

    def _build_fill(
        self,
        ingress: ExecutionFactIngress,
    ) -> tuple[Fill | None, int | None]:
        if (
            ingress.fact.kind is not ExecutionFactKind.TRADE
            or type(ingress.fact.payload) is not TradeFactPayload
        ):
            return None, self._state.fill_next
        sequence = self._state.fill_next
        if sequence is None:
            raise ExecutionFactAuthorityError(
                OutcomeCode.OUT_OF_RANGE,
                "execution Fill sequence is exhausted",
            )
        fill = create_fill(
            fill_id=EconomicId(
                self._run_id,
                EconomicOwnerKind.EXECUTION_FILL,
                sequence,
            ),
            fact=ingress.fact,
            spec_set=self._spec_set,
        )
        return fill, _advance(sequence)

    def _project(
        self,
        *,
        ingress: ExecutionFactIngress,
        fill: Fill | None,
        resolution: _Resolution,
        inherited_anomalies: frozenset[ExecutionFactAnomaly],
    ) -> _ProjectionResult:
        order = resolution.selected_order
        if order is None:
            return _ProjectionResult(None, None, None, None, frozenset())
        before = self._state.projection_index.get(order.order_id)
        if ExecutionFactAnomaly.ORDER_BINDING_CONFLICT in inherited_anomalies:
            return _ProjectionResult(before, before, None, None, frozenset())

        observed = self._state.observed_quantity.get(order.order_id, CanonicalDecimal("0"))
        if fill is not None:
            observed = _add_quantities(observed, fill.quantity)
        anomalies: set[ExecutionFactAnomaly] = set()
        fact = ingress.fact
        target_state: OrderProjectionState | None
        target_quantity: CanonicalDecimal
        if fact.kind is ExecutionFactKind.TRADE:
            if observed < order.quantity:
                target_state = OrderProjectionState.PARTIALLY_FILLED
                target_quantity = observed
            else:
                target_state = OrderProjectionState.FILLED
                target_quantity = order.quantity
                if observed > order.quantity:
                    anomalies.add(ExecutionFactAnomaly.OVERFILL)
        elif fact.kind in _LIFECYCLE_PROJECTION:
            target_state = _LIFECYCLE_PROJECTION[fact.kind]
            target_quantity = min(observed, order.quantity)
        elif (
            fact.kind is ExecutionFactKind.SUBMISSION_QUERY
            and type(fact.payload) is SubmissionQueryFactPayload
        ):
            target_state = _QUERY_PROJECTION.get(fact.payload.outcome_code)
            target_quantity = (
                order.quantity
                if target_state is OrderProjectionState.FILLED
                else min(observed, order.quantity)
            )
            if fact.payload.outcome_code is OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN:
                anomalies.add(ExecutionFactAnomaly.INSUFFICIENT_PROJECTION_EVIDENCE)
            if target_state is OrderProjectionState.FILLED and observed < order.quantity:
                anomalies.add(ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE)
        else:
            target_state = None
            target_quantity = CanonicalDecimal("0")
            anomalies.add(ExecutionFactAnomaly.INSUFFICIENT_PROJECTION_EVIDENCE)

        if before is not None and before.projection_state in _TERMINAL_STATES:
            if fact.kind is ExecutionFactKind.TRADE:
                anomalies.add(ExecutionFactAnomaly.LATE_AFTER_TERMINAL)
            elif target_state is not None and target_state is not before.projection_state:
                anomalies.add(ExecutionFactAnomaly.TERMINAL_STATE_CONFLICT)
            return _ProjectionResult(
                before,
                before,
                None,
                observed,
                frozenset(anomalies),
            )
        if target_state is None:
            return _ProjectionResult(
                before,
                before,
                None,
                observed,
                frozenset(anomalies),
            )
        if before is not None:
            allowed = (
                target_state is before.projection_state
                or target_state
                in _ALLOWED_NON_TERMINAL_TRANSITIONS.get(
                    before.projection_state,
                    frozenset(),
                )
            )
            if not allowed:
                anomalies.add(ExecutionFactAnomaly.PROJECTION_TRANSITION_CONFLICT)
                return _ProjectionResult(
                    before,
                    before,
                    None,
                    observed,
                    frozenset(anomalies),
                )
        venue_source, venue_id = _projection_venue(
            before=before,
            ingress=ingress,
            resolution=resolution,
        )
        semantic_change = before is None or (
            before.projection_state is not target_state
            or before.projected_executed_quantity != target_quantity
            or before.venue_source_namespace != venue_source
            or before.venue_order_id != venue_id
        )
        if not semantic_change:
            return _ProjectionResult(
                before,
                before,
                None,
                observed,
                frozenset(anomalies),
            )
        version = 1 if before is None else _advance_required(before.projection_version)
        after = create_order_projection_snapshot(
            order=order,
            spec_set=self._spec_set,
            projection_version=version,
            projection_state=target_state,
            projected_executed_quantity=target_quantity,
            venue_source_namespace=venue_source,
            venue_order_id=venue_id,
            last_fact_key=fact.dedup_key,
            last_fact_sha256=fact.fact_sha256,
        )
        return _ProjectionResult(
            before,
            after,
            after,
            observed,
            frozenset(anomalies),
        )

    def _publish_first_fact(
        self,
        *,
        ingress: ExecutionFactIngress,
        ingress_bytes: bytes,
        fact_bytes: bytes,
        dispatch_sequence: int,
        action: ExecutionFactAction,
        anomalies: tuple[ExecutionFactAnomaly, ...],
        resolution: _Resolution,
        fill: Fill | None,
        projection: _ProjectionResult,
        fill_after: int | None = None,
    ) -> ExecutionFactProcessingOutcome:
        outcome = create_execution_fact_processing_outcome(
            run_id=self._run_id,
            runtime_dispatch_sequence=dispatch_sequence,
            ingress=ingress,
            action=action,
            anomalies=anomalies,
            order_resolutions=resolution.bindings,
            resolved_order=resolution.selected_order,
            fill=fill,
            projection_before=projection.before,
            projection_after=projection.after,
        )
        ingress_index = dict(self._state.ingress_index)
        fact_index = dict(self._state.fact_index)
        fill_index = dict(self._state.fill_index)
        order_index = dict(self._state.order_index)
        client_index = dict(self._state.client_key_index)
        venue_index = dict(self._state.venue_index)
        projection_index = dict(self._state.projection_index)
        projection_history_index = dict(self._state.projection_history_index)
        observed_quantity = dict(self._state.observed_quantity)
        ingress_index[ingress.identity] = _IngressRecord(
            ingress=ingress,
            ingress_bytes=ingress_bytes,
            fact_bytes=fact_bytes,
            outcome=outcome,
        )
        fact_index[ingress.fact.dedup_key] = _FactRecord(
            fact=ingress.fact,
            fact_bytes=fact_bytes,
            first_ingress_identity=ingress.identity,
        )
        for order in resolution.resolved_orders:
            _insert_derived_order(order_index, client_index, order)
        if (
            resolution.staged_venue_key is not None
            and resolution.selected_order is not None
            and ExecutionFactAnomaly.ORDER_BINDING_CONFLICT not in anomalies
            and ExecutionFactAnomaly.UNKNOWN_ORDER not in anomalies
        ):
            venue_index[resolution.staged_venue_key] = resolution.selected_order
        fills = self._state.fills
        if fill is not None:
            fill_index[fill.fill_id] = fill
            fills = (*fills, fill)
        projections = self._state.projections
        if projection.new_projection is not None and resolution.selected_order is not None:
            projection_index[resolution.selected_order.order_id] = projection.new_projection
            projection_history_index[
                (
                    resolution.selected_order.order_id,
                    order_projection_snapshot_digest(projection.new_projection),
                )
            ] = projection.new_projection
            projections = (*projections, projection.new_projection)
        if projection.observed_quantity is not None and resolution.selected_order is not None:
            observed_quantity[resolution.selected_order.order_id] = projection.observed_quantity
        next_state = _FactAuthorityState(
            fill_next=self._state.fill_next if fill is None else fill_after,
            halt_requested=(
                self._state.halt_requested
                or action
                in (
                    ExecutionFactAction.INVALID,
                    ExecutionFactAction.UNRESOLVED,
                    ExecutionFactAction.CONFLICT,
                )
            ),
            ingress_index=MappingProxyType(dict(ingress_index)),
            fact_index=MappingProxyType(dict(fact_index)),
            fill_index=MappingProxyType(dict(fill_index)),
            order_index=MappingProxyType(dict(order_index)),
            client_key_index=MappingProxyType(dict(client_index)),
            venue_index=MappingProxyType(dict(venue_index)),
            projection_index=MappingProxyType(dict(projection_index)),
            projection_history_index=MappingProxyType(dict(projection_history_index)),
            observed_quantity=MappingProxyType(dict(observed_quantity)),
            ingresses=(*self._state.ingresses, ingress),
            outcomes=(*self._state.outcomes, outcome),
            first_facts=(*self._state.first_facts, ingress.fact),
            fills=fills,
            projections=projections,
        )
        _preflight_candidate(next_state, ingress=ingress, outcome=outcome)
        if fill is not None:
            _preflight_fill(fill)
        if projection.new_projection is not None:
            _preflight_projection(projection.new_projection)
        self._state = next_state
        return outcome


def create_phase1_execution_fact_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    order_verifier: OrderResolutionVerifier,
    dispatch_verifier: RuntimeFactDispatchVerifier,
) -> Phase1ExecutionFactAuthority:
    """Create one empty fact authority bound to real read-only capabilities."""
    if type(run_id) is not RunId:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "run_id must be an exact RunId",
        )
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "spec_set must be an exact InstrumentExecutionSpecSet",
        )
    spec_sha256 = instrument_spec_set_digest(spec_set)
    order_port = _require_order_verifier(
        order_verifier,
        run_id=run_id,
        spec_set=spec_set,
        spec_sha256=spec_sha256,
    )
    dispatch_port = _require_dispatch_verifier(
        dispatch_verifier,
        run_id=run_id,
        spec_set=spec_set,
        spec_sha256=spec_sha256,
    )
    authority = object.__new__(Phase1ExecutionFactAuthority)
    authority._run_id = run_id
    authority._spec_set = spec_set
    authority._spec_set_sha256 = spec_sha256
    authority._order_verifier = order_port
    authority._dispatch_verifier = dispatch_port
    authority._state = _FactAuthorityState(
        fill_next=1,
        halt_requested=False,
        ingress_index=MappingProxyType({}),
        fact_index=MappingProxyType({}),
        fill_index=MappingProxyType({}),
        order_index=MappingProxyType({}),
        client_key_index=MappingProxyType({}),
        venue_index=MappingProxyType({}),
        projection_index=MappingProxyType({}),
        projection_history_index=MappingProxyType({}),
        observed_quantity=MappingProxyType({}),
        ingresses=(),
        outcomes=(),
        first_facts=(),
        fills=(),
        projections=(),
    )
    return authority


def recover_phase1_execution_fact_authority_history(
    history: Phase1ExecutionFactAuthority,
    *,
    order_verifier: OrderResolutionVerifier,
    dispatch_verifier: RuntimeFactDispatchVerifier,
) -> Phase1ExecutionFactAuthority:
    """Clone one factory-issued canonical fact history onto fresh verifier bindings."""
    if type(history) is not Phase1ExecutionFactAuthority:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "fact recovery history must be an exact factory-issued authority",
        )
    recovered = create_phase1_execution_fact_authority(
        run_id=history.run_id,
        spec_set=history.spec_set,
        order_verifier=order_verifier,
        dispatch_verifier=dispatch_verifier,
    )
    recovered._state = history._state
    return recovered


def _materialize_ingress(ingress: object) -> tuple[bytes, bytes]:
    if type(ingress) is not ExecutionFactIngress:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "ingress must be an exact ExecutionFactIngress",
        )
    return (
        canonical_execution_fact_ingress_bytes(ingress),
        canonical_execution_fact_bytes(ingress.fact),
    )


def _require_active_dispatch(
    verifier: RuntimeFactDispatchVerifier,
    *,
    ingress: ExecutionFactIngress,
    ingress_bytes: bytes,
    fact_bytes: bytes,
) -> int:
    try:
        result = verifier.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=ingress_bytes,
            canonical_fact_bytes=fact_bytes,
        )
    except (AttributeError, TypeError) as error:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "dispatch verifier operation contract failed",
        ) from error
    if result is None:
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "ingress is not the exact current active issued fact dispatch",
        )
    if type(result) is not int:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "dispatch verifier must return an exact int or None",
        )
    if not 1 <= result <= _MAX_UINT64:
        raise ExecutionFactAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            "dispatch sequence must be in 1..2^64-1",
        )
    return result


def _is_context_valid(
    ingress: ExecutionFactIngress,
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    spec_set_sha256: Sha256Digest,
) -> bool:
    try:
        fact = ingress.fact
        if (
            ingress.source_namespace != fact.source_namespace
            or ingress.available_at < fact.occurred_at
            or execution_fact_digest(fact) != fact.fact_sha256
        ):
            return False
        identities = tuple(
            identity
            for identity in (fact.order_id, fact.correlation_id, fact.causation_id)
            if identity is not None
        )
        if any(identity.run_id != run_id for identity in identities):
            return False
        specification = spec_set.require(fact.instrument) if fact.instrument is not None else None
        if fact.kind is ExecutionFactKind.SUBMISSION_QUERY:
            payload = fact.payload
            if type(payload) is not SubmissionQueryFactPayload:
                return False
            SubmissionQueryFactPayload(payload.outcome_code)
            return specification is not None
        if fact.kind is ExecutionFactKind.TRADE and (
            type(fact.payload) is not TradeFactPayload
            or fact.instrument is None
            or specification is None
            or fact.payload.instrument_spec_set_sha256 != spec_set_sha256
        ):
            return False
        if fact.kind in _LIFECYCLE_PROJECTION and type(fact.payload) is not LifecycleFactPayload:
            return False
        decode_execution_fact(
            canonical_execution_fact_bytes(fact),
            context=IndependentFactDecodeContext(spec_set),
        )
        return True
    except (
        EconomicValidationError,
        ExecutionIdentityError,
        ExecutionMessageError,
        RunContractError,
        TimeValidationError,
        AttributeError,
        TypeError,
    ):
        return False


def _resolve_order_id(
    verifier: OrderResolutionVerifier,
    order_id: EconomicId,
) -> Order | None:
    try:
        result = verifier.resolve_issued_order_by_id(order_id)
    except (AttributeError, TypeError) as error:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "Order-ID resolver operation contract failed",
        ) from error
    if result is not None and type(result) is not Order:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "Order-ID resolver must return an exact Order or None",
        )
    return result


def _resolve_client_key(
    verifier: OrderResolutionVerifier,
    key: Sha256Digest,
) -> Order | None:
    try:
        result = verifier.resolve_issued_order_by_client_submission_key(key)
    except (AttributeError, TypeError) as error:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "client-key resolver operation contract failed",
        ) from error
    if result is not None and type(result) is not Order:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "client-key resolver must return an exact Order or None",
        )
    return result


def _record_resolved_order(
    order: Order | None,
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    spec_set_sha256: Sha256Digest,
    orders: dict[EconomicId, Order],
) -> None:
    if order is None:
        return
    if (
        order.run_id != run_id
        or order.instrument_spec_set_id != spec_set.identifier
        or order.instrument_spec_set_sha256 != spec_set_sha256
        or type(order.order_id) is not EconomicId
        or order.order_id.owner_kind is not EconomicOwnerKind.EXECUTION_ORDER
        or canonical_order_bytes(order) == b""
        or order_client_submission_key(order) != order.client_submission_key
    ):
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "resolved Order conflicts with the frozen authority context",
        )
    existing = orders.get(order.order_id)
    if existing is not None and canonical_order_bytes(existing) != canonical_order_bytes(order):
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "one resolved Order ID produced conflicting canonical Orders",
        )
    orders[order.order_id] = order


def _fact_contradicts_order(fact: ExecutionFact, order: Order) -> bool:
    payload = fact.payload
    return (
        (fact.order_id is not None and fact.order_id != order.order_id)
        or (
            fact.client_submission_key is not None
            and fact.client_submission_key != order.client_submission_key
        )
        or (fact.instrument is not None and fact.instrument != order.instrument)
        or (type(payload) is TradeFactPayload and payload.side is not order.side)
        or (fact.correlation_id is not None and fact.correlation_id != order.correlation_id)
        or (fact.causation_id is not None and fact.causation_id != order.order_id)
    )


def _projection_venue(
    *,
    before: OrderProjectionSnapshot | None,
    ingress: ExecutionFactIngress,
    resolution: _Resolution,
) -> tuple[SourceNamespace | None, VenueOrderId | None]:
    if before is not None and before.venue_order_id is not None:
        return before.venue_source_namespace, before.venue_order_id
    if (
        ingress.fact.venue_order_id is not None
        and resolution.selected_order is not None
        and (
            resolution.staged_venue_key is not None
            or any(
                binding.key_kind is OrderResolutionKeyKind.VENUE_ORDER_ID
                and binding.resolved_order_id == resolution.selected_order.order_id
                for binding in resolution.bindings
            )
        )
    ):
        return ingress.fact.source_namespace, ingress.fact.venue_order_id
    return None, None


def _state_with_ingress_outcome(
    state: _FactAuthorityState,
    *,
    ingress: ExecutionFactIngress,
    ingress_bytes: bytes,
    fact_bytes: bytes,
    outcome: ExecutionFactProcessingOutcome,
    halt_requested: bool,
) -> _FactAuthorityState:
    ingress_index = dict(state.ingress_index)
    ingress_index[ingress.identity] = _IngressRecord(
        ingress=ingress,
        ingress_bytes=ingress_bytes,
        fact_bytes=fact_bytes,
        outcome=outcome,
    )
    return _FactAuthorityState(
        fill_next=state.fill_next,
        halt_requested=halt_requested,
        ingress_index=MappingProxyType(dict(ingress_index)),
        fact_index=state.fact_index,
        fill_index=state.fill_index,
        order_index=state.order_index,
        client_key_index=state.client_key_index,
        venue_index=state.venue_index,
        projection_index=state.projection_index,
        projection_history_index=state.projection_history_index,
        observed_quantity=state.observed_quantity,
        ingresses=(*state.ingresses, ingress),
        outcomes=(*state.outcomes, outcome),
        first_facts=state.first_facts,
        fills=state.fills,
        projections=state.projections,
    )


def _insert_derived_order(
    order_index: dict[EconomicId, Order],
    client_index: dict[Sha256Digest, Order],
    order: Order,
) -> None:
    existing_id = order_index.get(order.order_id)
    existing_client = client_index.get(order.client_submission_key)
    if (
        existing_id is not None
        and canonical_order_bytes(existing_id) != canonical_order_bytes(order)
    ) or (
        existing_client is not None
        and canonical_order_bytes(existing_client) != canonical_order_bytes(order)
    ):
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "derived Order cache received conflicting canonical evidence",
        )
    order_index[order.order_id] = order
    client_index[order.client_submission_key] = order


def _preflight_candidate(
    state: _FactAuthorityState,
    *,
    ingress: ExecutionFactIngress,
    outcome: ExecutionFactProcessingOutcome,
) -> None:
    record = state.ingress_index.get(ingress.identity)
    if (
        record is None
        or record.outcome is not outcome
        or not state.ingresses
        or state.ingresses[-1] is not ingress
        or not state.outcomes
        or state.outcomes[-1] is not outcome
        or canonical_execution_fact_processing_outcome_bytes(outcome) == b""
        or execution_fact_processing_outcome_digest(outcome).value == ""
    ):
        raise AssertionError("candidate fact-authority state failed preflight")


def _preflight_fill(fill: Fill) -> None:
    if canonical_fill_bytes(fill) == b"" or fill_digest(fill).value == "":
        raise AssertionError("candidate Fill failed canonical preflight")


def _preflight_projection(projection: OrderProjectionSnapshot) -> None:
    if (
        canonical_order_projection_snapshot_bytes(projection) == b""
        or order_projection_snapshot_digest(projection).value == ""
    ):
        raise AssertionError("candidate projection failed canonical preflight")


def _add_quantities(
    left: CanonicalDecimal,
    right: CanonicalDecimal,
) -> CanonicalDecimal:
    scale = max(left.scale, right.scale)
    coefficient = left.coefficient * (10 ** (scale - left.scale)) + right.coefficient * (
        10 ** (scale - right.scale)
    )
    while scale > 0 and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return CanonicalDecimal(f"{sign}{digits}")
    digits = digits.rjust(scale + 1, "0")
    return CanonicalDecimal(f"{sign}{digits[:-scale]}.{digits[-scale:]}")


def _advance(value: int) -> int | None:
    if type(value) is not int or not 1 <= value <= _MAX_UINT64:
        raise AssertionError("Fill allocation state is invalid")
    return None if value == _MAX_UINT64 else value + 1


def _advance_required(value: int) -> int:
    result = _advance(value)
    if result is None:
        raise ExecutionFactAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            "projection version is exhausted",
        )
    return result


def _require_order_verifier(
    verifier: object,
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    spec_sha256: Sha256Digest,
) -> OrderResolutionVerifier:
    candidate = cast(Any, verifier)
    try:
        verifier_run_id = candidate.run_id
        verifier_spec_set = candidate.spec_set
        by_id = candidate.resolve_issued_order_by_id
        by_client = candidate.resolve_issued_order_by_client_submission_key
    except (AttributeError, TypeError) as error:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "Order verifier has an incomplete construction contract",
        ) from error
    if (
        type(verifier_run_id) is not RunId
        or type(verifier_spec_set) is not InstrumentExecutionSpecSet
        or not callable(by_id)
        or not callable(by_client)
    ):
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "Order verifier bindings require exact types and callable operations",
        )
    if (
        verifier_run_id != run_id
        or verifier_spec_set.identifier != spec_set.identifier
        or instrument_spec_set_digest(verifier_spec_set) != spec_sha256
    ):
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "Order verifier construction binding conflicts",
        )
    return cast(OrderResolutionVerifier, verifier)


def _require_dispatch_verifier(
    verifier: object,
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    spec_sha256: Sha256Digest,
) -> RuntimeFactDispatchVerifier:
    candidate = cast(Any, verifier)
    try:
        verifier_run_id = candidate.run_id
        verifier_spec_set = candidate.spec_set
        operation = candidate.resolve_active_issued_fact_dispatch
    except (AttributeError, TypeError) as error:
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "dispatch verifier has an incomplete construction contract",
        ) from error
    if (
        type(verifier_run_id) is not RunId
        or type(verifier_spec_set) is not InstrumentExecutionSpecSet
        or not callable(operation)
    ):
        raise ExecutionFactAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "dispatch verifier bindings require exact types and callable operation",
        )
    if (
        verifier_run_id != run_id
        or verifier_spec_set.identifier != spec_set.identifier
        or instrument_spec_set_digest(verifier_spec_set) != spec_sha256
    ):
        raise ExecutionFactAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "dispatch verifier construction binding conflicts",
        )
    return cast(RuntimeFactDispatchVerifier, verifier)


def _raise_public(error: object) -> NoReturn:
    code = getattr(error, "code", None)
    if type(code) is OutcomeCode:
        if code is OutcomeCode.INVALID_TYPE:
            raise ExecutionFactAuthorityError(OutcomeCode.INVALID_TYPE, str(error)) from (
                error if isinstance(error, BaseException) else None
            )
        if code in (
            OutcomeCode.OUT_OF_RANGE,
            OutcomeCode.NOT_QUANTIZED,
            OutcomeCode.PRICE_DOMAIN,
            OutcomeCode.ARITHMETIC_OVERFLOW,
            OutcomeCode.NON_FINITE,
        ):
            raise ExecutionFactAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from (
                error if isinstance(error, BaseException) else None
            )
        raise ExecutionFactAuthorityError(OutcomeCode.CONFLICTING_ID, str(error)) from (
            error if isinstance(error, BaseException) else None
        )
    raise AssertionError("unexpected validation outcome at fact-authority boundary") from (
        error if isinstance(error, BaseException) else None
    )
