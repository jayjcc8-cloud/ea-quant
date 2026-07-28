"""Canonical execution-fact outcomes and observation-derived Order projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import NoReturn, final

from ea.core.economics import CanonicalDecimal, EconomicValidationError, require_quantized
from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    ExecutionIdentityError,
    ExternalFactId,
    FactDedupKey,
    IngressIdentity,
    SourceNamespace,
    SourceNativeSequence,
)
from ea.core.execution_messages import (
    ExecutionFactIngress,
    ExecutionFactKind,
    ExecutionMessageError,
    Fill,
    Order,
    TradeFactPayload,
    VenueOrderId,
    fill_digest,
    order_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunContractError, RunId, Sha256Digest

ORDER_PROJECTION_SNAPSHOT_SCHEMA_VERSION = 1
ORDER_PROJECTION_SNAPSHOT_CANONICALIZATION = "ea-order-projection-snapshot-v1"
ORDER_PROJECTION_SNAPSHOT_DIGEST_DOMAIN = b"ea.execution-order-projection.v1\0"

EXECUTION_FACT_PROCESSING_OUTCOME_SCHEMA_VERSION = 1
EXECUTION_FACT_PROCESSING_OUTCOME_CANONICALIZATION = "ea-execution-fact-processing-outcome-v1"
EXECUTION_FACT_PROCESSING_OUTCOME_DIGEST_DOMAIN = b"ea.execution-fact-processing-outcome.v1\0"

_MAX_UINT64 = (1 << 64) - 1
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class ExecutionStateError(ValueError):
    """Closed validation failure for execution-state values."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("execution-state errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> ExecutionStateError:
    return ExecutionStateError(code, message)


class OrderProjectionState(StrEnum):
    """Closed observation-derived Order projection states."""

    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DEFINITELY_NOT_SUBMITTED = "definitely_not_submitted"


class ExecutionFactAction(StrEnum):
    """Closed primary fact-processing actions."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"


class ExecutionFactAnomaly(StrEnum):
    """Closed anomaly vocabulary in canonical rank order."""

    CONTEXT_INVALID = "context_invalid"
    ORDER_BINDING_CONFLICT = "order_binding_conflict"
    UNKNOWN_ORDER = "unknown_order"
    MISSING_ANCESTRY = "missing_ancestry"
    INSUFFICIENT_PROJECTION_EVIDENCE = "insufficient_projection_evidence"
    CONFIRMED_FILL_WITHOUT_TRADE = "confirmed_fill_without_trade"
    OVERFILL = "overfill"
    LATE_AFTER_TERMINAL = "late_after_terminal"
    PROJECTION_TRANSITION_CONFLICT = "projection_transition_conflict"
    TERMINAL_STATE_CONFLICT = "terminal_state_conflict"


EXECUTION_FACT_ANOMALY_RANKS: Mapping[ExecutionFactAnomaly, int] = MappingProxyType(
    {
        ExecutionFactAnomaly.CONTEXT_INVALID: 0,
        ExecutionFactAnomaly.ORDER_BINDING_CONFLICT: 10,
        ExecutionFactAnomaly.UNKNOWN_ORDER: 20,
        ExecutionFactAnomaly.MISSING_ANCESTRY: 30,
        ExecutionFactAnomaly.INSUFFICIENT_PROJECTION_EVIDENCE: 40,
        ExecutionFactAnomaly.CONFIRMED_FILL_WITHOUT_TRADE: 50,
        ExecutionFactAnomaly.OVERFILL: 60,
        ExecutionFactAnomaly.LATE_AFTER_TERMINAL: 70,
        ExecutionFactAnomaly.PROJECTION_TRANSITION_CONFLICT: 80,
        ExecutionFactAnomaly.TERMINAL_STATE_CONFLICT: 90,
    }
)


class OrderResolutionKeyKind(StrEnum):
    """Canonical ordering of presented Order-correlation keys."""

    ORDER_ID = "order_id"
    CLIENT_SUBMISSION_KEY = "client_submission_key"
    VENUE_ORDER_ID = "venue_order_id"


_ORDER_RESOLUTION_RANKS: Mapping[OrderResolutionKeyKind, int] = MappingProxyType(
    {
        OrderResolutionKeyKind.ORDER_ID: 0,
        OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY: 10,
        OrderResolutionKeyKind.VENUE_ORDER_ID: 20,
    }
)
_ACTION_OUTCOME: Mapping[ExecutionFactAction, OutcomeCode] = MappingProxyType(
    {
        ExecutionFactAction.ACCEPTED: OutcomeCode.FACT_ACCEPTED,
        ExecutionFactAction.DUPLICATE: OutcomeCode.FACT_DUPLICATE,
        ExecutionFactAction.CONFLICT: OutcomeCode.FACT_CONFLICT,
        ExecutionFactAction.INVALID: OutcomeCode.FACT_INVALID,
        ExecutionFactAction.UNRESOLVED: OutcomeCode.FACT_UNRESOLVED,
    }
)
_TERMINAL_PROJECTION_STATES = frozenset(
    {
        OrderProjectionState.FILLED,
        OrderProjectionState.REJECTED,
        OrderProjectionState.EXPIRED,
        OrderProjectionState.CANCELLED,
        OrderProjectionState.DEFINITELY_NOT_SUBMITTED,
    }
)


@final
@dataclass(frozen=True, slots=True, init=False)
class OrderProjectionSnapshot:
    """One immutable version of the observation-derived Order projection."""

    run_id: RunId
    order_id: EconomicId
    order_sha256: Sha256Digest
    client_submission_key: Sha256Digest
    projection_version: int
    projection_state: OrderProjectionState
    projected_executed_quantity: CanonicalDecimal
    venue_source_namespace: SourceNamespace | None
    venue_order_id: VenueOrderId | None
    last_fact_key: FactDedupKey
    last_fact_sha256: Sha256Digest

    def __init__(self) -> None:
        raise TypeError(
            "OrderProjectionSnapshot values are created only by create_order_projection_snapshot"
        )

    @property
    def is_terminal(self) -> bool:
        return self.projection_state in _TERMINAL_PROJECTION_STATES


@final
@dataclass(frozen=True, slots=True, init=False)
class OrderResolutionBinding:
    """One presented correlation-key lookup result."""

    key_kind: OrderResolutionKeyKind
    resolved_order_id: EconomicId | None

    def __init__(self) -> None:
        raise TypeError(
            "OrderResolutionBinding values are created only by create_order_resolution_binding"
        )


@final
@dataclass(frozen=True, slots=True, init=False)
class ExecutionFactProcessingOutcome:
    """Replay-stable canonical result of processing one admitted ingress."""

    run_id: RunId
    runtime_dispatch_sequence: int
    ingress_identity: IngressIdentity
    ingress_sha256: Sha256Digest
    fact_key: FactDedupKey
    fact_sha256: Sha256Digest
    action: ExecutionFactAction
    anomalies: tuple[ExecutionFactAnomaly, ...]
    outcome_code: OutcomeCode
    reported_order_id: EconomicId | None
    client_submission_key: Sha256Digest | None
    reported_venue_source_namespace: SourceNamespace | None
    reported_venue_order_id: VenueOrderId | None
    order_resolutions: tuple[OrderResolutionBinding, ...]
    resolved_order_id: EconomicId | None
    fill_id: EconomicId | None
    fill_sha256: Sha256Digest | None
    projection_before_sha256: Sha256Digest | None
    projection_after_sha256: Sha256Digest | None
    requires_reconciliation: bool
    halt_requested: bool

    def __init__(self) -> None:
        raise TypeError(
            "ExecutionFactProcessingOutcome values are created only by "
            "create_execution_fact_processing_outcome"
        )


def create_order_projection_snapshot(
    *,
    order: Order,
    spec_set: InstrumentExecutionSpecSet,
    projection_version: int,
    projection_state: OrderProjectionState,
    projected_executed_quantity: CanonicalDecimal,
    venue_source_namespace: SourceNamespace | None,
    venue_order_id: VenueOrderId | None,
    last_fact_key: FactDedupKey,
    last_fact_sha256: Sha256Digest,
) -> OrderProjectionSnapshot:
    """Construct one exact projection snapshot against its Order context."""
    try:
        if type(order) is not Order or type(spec_set) is not InstrumentExecutionSpecSet:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "projection construction requires exact Order and specification set",
            )
        if (
            order.instrument_spec_set_id != spec_set.identifier
            or order.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "projection Order conflicts with the specification set",
            )
        if type(projection_version) is not int:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "projection_version must have exact runtime type int",
            )
        if not 1 <= projection_version <= _MAX_UINT64:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "projection_version must be in 1..2^64-1",
            )
        if type(projection_state) is not OrderProjectionState:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "projection_state must be an exact OrderProjectionState",
            )
        if type(projected_executed_quantity) is not CanonicalDecimal:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "projected_executed_quantity must be an exact CanonicalDecimal",
            )
        specification = spec_set.require(order.instrument)
        require_quantized(
            projected_executed_quantity,
            specification.quantity_quantum,
            field_name="projected_executed_quantity",
        )
        if (
            projected_executed_quantity < CanonicalDecimal("0")
            or projected_executed_quantity > order.quantity
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "projected quantity must be between zero and Order quantity",
            )
        zero = CanonicalDecimal("0")
        if (
            projection_state is OrderProjectionState.PARTIALLY_FILLED
            and not zero < projected_executed_quantity < order.quantity
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "partially-filled projection quantity must be positive and below Order quantity",
            )
        if (
            projection_state is OrderProjectionState.FILLED
            and projected_executed_quantity != order.quantity
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "filled projection quantity must equal Order quantity",
            )
        if (
            projection_state
            in (
                OrderProjectionState.SUBMITTED,
                OrderProjectionState.ACKNOWLEDGED,
                OrderProjectionState.REJECTED,
                OrderProjectionState.DEFINITELY_NOT_SUBMITTED,
            )
            and projected_executed_quantity != zero
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                f"{projection_state.value} projection quantity must be zero",
            )
        if (
            projection_state
            in (
                OrderProjectionState.EXPIRED,
                OrderProjectionState.CANCELLED,
            )
            and projected_executed_quantity >= order.quantity
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                f"{projection_state.value} projection quantity must be below Order quantity",
            )
        _require_venue_pair(venue_source_namespace, venue_order_id)
        if type(last_fact_key) is not FactDedupKey:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "last_fact_key must be an exact FactDedupKey",
            )
        if type(last_fact_sha256) is not Sha256Digest:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "last_fact_sha256 must be an exact Sha256Digest",
            )
        value = object.__new__(OrderProjectionSnapshot)
        object.__setattr__(value, "run_id", order.run_id)
        object.__setattr__(value, "order_id", order.order_id)
        object.__setattr__(value, "order_sha256", order_digest(order))
        object.__setattr__(value, "client_submission_key", order.client_submission_key)
        object.__setattr__(value, "projection_version", projection_version)
        object.__setattr__(value, "projection_state", projection_state)
        object.__setattr__(
            value,
            "projected_executed_quantity",
            projected_executed_quantity,
        )
        object.__setattr__(value, "venue_source_namespace", venue_source_namespace)
        object.__setattr__(value, "venue_order_id", venue_order_id)
        object.__setattr__(value, "last_fact_key", last_fact_key)
        object.__setattr__(value, "last_fact_sha256", last_fact_sha256)
        return value
    except ExecutionStateError:
        raise
    except (
        EconomicValidationError,
        ExecutionIdentityError,
        ExecutionMessageError,
        RunContractError,
    ) as error:
        _raise_public(error)


def create_order_resolution_binding(
    *,
    key_kind: OrderResolutionKeyKind,
    resolved_order: Order | None,
) -> OrderResolutionBinding:
    """Create one immutable presented-key resolution result."""
    if type(key_kind) is not OrderResolutionKeyKind:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "key_kind must be an exact OrderResolutionKeyKind",
        )
    if resolved_order is not None and type(resolved_order) is not Order:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "resolved_order must be an exact Order or None",
        )
    value = object.__new__(OrderResolutionBinding)
    object.__setattr__(value, "key_kind", key_kind)
    object.__setattr__(
        value,
        "resolved_order_id",
        None if resolved_order is None else resolved_order.order_id,
    )
    return value


def create_execution_fact_processing_outcome(
    *,
    run_id: RunId,
    runtime_dispatch_sequence: int,
    ingress: ExecutionFactIngress,
    action: ExecutionFactAction,
    anomalies: tuple[ExecutionFactAnomaly, ...],
    order_resolutions: tuple[OrderResolutionBinding, ...],
    resolved_order: Order | None,
    fill: Fill | None,
    projection_before: OrderProjectionSnapshot | None,
    projection_after: OrderProjectionSnapshot | None,
) -> ExecutionFactProcessingOutcome:
    """Construct the exhaustive canonical outcome for one ingress."""
    try:
        if type(run_id) is not RunId or type(ingress) is not ExecutionFactIngress:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "outcome construction requires exact run_id and ingress",
            )
        if type(runtime_dispatch_sequence) is not int:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "runtime_dispatch_sequence must have exact runtime type int",
            )
        if not 1 <= runtime_dispatch_sequence <= _MAX_UINT64:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "runtime_dispatch_sequence must be in 1..2^64-1",
            )
        _require_action_anomalies(action, anomalies)
        _require_resolution_tuple(ingress, action, order_resolutions)
        if resolved_order is not None and type(resolved_order) is not Order:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "resolved_order must be an exact Order or None",
            )
        resolved_id = _derived_selected_order_id(order_resolutions)
        if resolved_id != (None if resolved_order is None else resolved_order.order_id):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "resolved_order conflicts with the binding tuple",
            )
        if resolved_order is not None and resolved_order.run_id != run_id:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "resolved Order conflicts with the outcome run",
            )
        _require_fill_context(run_id, ingress, action, fill)
        _require_projection_context(
            run_id,
            resolved_order,
            projection_before,
            projection_after,
        )
        if action in (
            ExecutionFactAction.DUPLICATE,
            ExecutionFactAction.CONFLICT,
            ExecutionFactAction.INVALID,
        ) and (
            order_resolutions
            or resolved_order is not None
            or fill is not None
            or projection_before is not None
            or projection_after is not None
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "short-circuit actions cannot carry resolution, Fill, or projection context",
            )
        fact = ingress.fact
        reconciliation = action in (
            ExecutionFactAction.CONFLICT,
            ExecutionFactAction.INVALID,
            ExecutionFactAction.UNRESOLVED,
        )
        value = object.__new__(ExecutionFactProcessingOutcome)
        object.__setattr__(value, "run_id", run_id)
        object.__setattr__(
            value,
            "runtime_dispatch_sequence",
            runtime_dispatch_sequence,
        )
        object.__setattr__(value, "ingress_identity", ingress.identity)
        object.__setattr__(
            value,
            "ingress_sha256",
            _ingress_digest(ingress),
        )
        object.__setattr__(value, "fact_key", fact.dedup_key)
        object.__setattr__(value, "fact_sha256", fact.fact_sha256)
        object.__setattr__(value, "action", action)
        object.__setattr__(value, "anomalies", anomalies)
        object.__setattr__(value, "outcome_code", _ACTION_OUTCOME[action])
        object.__setattr__(value, "reported_order_id", fact.order_id)
        object.__setattr__(
            value,
            "client_submission_key",
            fact.client_submission_key,
        )
        object.__setattr__(
            value,
            "reported_venue_source_namespace",
            None if fact.venue_order_id is None else fact.source_namespace,
        )
        object.__setattr__(value, "reported_venue_order_id", fact.venue_order_id)
        object.__setattr__(value, "order_resolutions", order_resolutions)
        object.__setattr__(value, "resolved_order_id", resolved_id)
        object.__setattr__(value, "fill_id", None if fill is None else fill.fill_id)
        object.__setattr__(
            value,
            "fill_sha256",
            None if fill is None else fill_digest(fill),
        )
        object.__setattr__(
            value,
            "projection_before_sha256",
            None
            if projection_before is None
            else order_projection_snapshot_digest(projection_before),
        )
        object.__setattr__(
            value,
            "projection_after_sha256",
            None
            if projection_after is None
            else order_projection_snapshot_digest(projection_after),
        )
        object.__setattr__(value, "requires_reconciliation", reconciliation)
        object.__setattr__(value, "halt_requested", reconciliation)
        return value
    except ExecutionStateError:
        raise
    except (
        EconomicValidationError,
        ExecutionIdentityError,
        ExecutionMessageError,
        RunContractError,
    ) as error:
        _raise_public(error)


def _require_venue_pair(
    source_namespace: SourceNamespace | None,
    venue_order_id: VenueOrderId | None,
) -> None:
    if (source_namespace is None) != (venue_order_id is None):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "venue source namespace and venue Order ID must be jointly present",
        )
    if source_namespace is not None and type(source_namespace) is not SourceNamespace:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "venue source namespace must be exact or None",
        )
    if venue_order_id is not None and type(venue_order_id) is not VenueOrderId:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "venue Order ID must be exact or None",
        )


def _require_action_anomalies(
    action: object,
    anomalies: object,
) -> None:
    if type(action) is not ExecutionFactAction:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "action must be an exact ExecutionFactAction",
        )
    if type(anomalies) is not tuple or any(
        type(anomaly) is not ExecutionFactAnomaly for anomaly in anomalies
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "anomalies must be an exact tuple of ExecutionFactAnomaly values",
        )
    ranks = tuple(EXECUTION_FACT_ANOMALY_RANKS[anomaly] for anomaly in anomalies)
    if ranks != tuple(sorted(set(ranks))):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "anomalies must be unique and strictly ascending by canonical rank",
        )
    valid = (
        (action in (ExecutionFactAction.ACCEPTED, ExecutionFactAction.DUPLICATE) and not anomalies)
        or (action is ExecutionFactAction.CONFLICT and not anomalies)
        or (
            action is ExecutionFactAction.INVALID
            and anomalies == (ExecutionFactAnomaly.CONTEXT_INVALID,)
        )
        or (
            action is ExecutionFactAction.UNRESOLVED
            and bool(anomalies)
            and ExecutionFactAnomaly.CONTEXT_INVALID not in anomalies
        )
    )
    if not valid:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "action and anomaly tuple violate the closed classification matrix",
        )


def _require_resolution_tuple(
    ingress: ExecutionFactIngress,
    action: ExecutionFactAction,
    bindings: object,
) -> None:
    if type(bindings) is not tuple or any(
        type(binding) is not OrderResolutionBinding for binding in bindings
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "order_resolutions must be an exact tuple of OrderResolutionBinding values",
        )
    ranks = tuple(_ORDER_RESOLUTION_RANKS[binding.key_kind] for binding in bindings)
    if ranks != tuple(sorted(set(ranks))):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "Order resolution bindings must be unique and canonically ordered",
        )
    if action in (
        ExecutionFactAction.DUPLICATE,
        ExecutionFactAction.CONFLICT,
        ExecutionFactAction.INVALID,
    ):
        if bindings:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "short-circuit actions require an empty resolution tuple",
            )
        return
    fact = ingress.fact
    expected: list[OrderResolutionKeyKind] = []
    if fact.order_id is not None:
        expected.append(OrderResolutionKeyKind.ORDER_ID)
    if fact.client_submission_key is not None:
        expected.append(OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY)
    if fact.venue_order_id is not None:
        expected.append(OrderResolutionKeyKind.VENUE_ORDER_ID)
    if tuple(binding.key_kind for binding in bindings) != tuple(expected):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "Order resolution bindings do not match the presented fact keys",
        )


def _derived_selected_order_id(
    bindings: tuple[OrderResolutionBinding, ...],
) -> EconomicId | None:
    resolved = {binding.resolved_order_id for binding in bindings if binding.resolved_order_id}
    return next(iter(resolved)) if len(resolved) == 1 else None


def _require_fill_context(
    run_id: RunId,
    ingress: ExecutionFactIngress,
    action: ExecutionFactAction,
    fill: Fill | None,
) -> None:
    if fill is not None and type(fill) is not Fill:
        raise _fail(OutcomeCode.INVALID_TYPE, "fill must be an exact Fill or None")
    requires_fill = (
        ingress.fact.kind is ExecutionFactKind.TRADE
        and type(ingress.fact.payload) is TradeFactPayload
        and action in (ExecutionFactAction.ACCEPTED, ExecutionFactAction.UNRESOLVED)
    )
    if (fill is not None) != requires_fill:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "Fill presence violates the exhaustive trade rule",
        )
    if fill is None:
        return
    if action not in (ExecutionFactAction.ACCEPTED, ExecutionFactAction.UNRESOLVED):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "only accepted or unresolved actions can carry a Fill",
        )
    if (
        fill.run_id != run_id
        or fill.fact_key != ingress.fact.dedup_key
        or fill.fact_sha256 != ingress.fact.fact_sha256
    ):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "Fill context conflicts with the ingress",
        )


def _require_projection_context(
    run_id: RunId,
    resolved_order: Order | None,
    before: OrderProjectionSnapshot | None,
    after: OrderProjectionSnapshot | None,
) -> None:
    for value in (before, after):
        if value is not None and type(value) is not OrderProjectionSnapshot:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "projection context must contain exact snapshots or None",
            )
    if resolved_order is None:
        if before is not None or after is not None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "projection context requires one selected Order",
            )
        return
    for value in (before, after):
        if value is not None and (
            value.run_id != run_id or value.order_id != resolved_order.order_id
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "projection context conflicts with the selected Order",
            )


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _optional_economic_id_document(identity: EconomicId | None) -> dict[str, object] | None:
    return None if identity is None else _economic_id_document(identity)


def _fact_key_document(key: FactDedupKey) -> dict[str, object]:
    identity = key.identity
    return {
        "dedup_identity": {
            "kind": "external_id" if type(identity) is ExternalFactId else "source_native_sequence",
            "value": identity.value,
        },
        "source_namespace": key.source_namespace.value,
    }


def _ingress_identity_document(identity: IngressIdentity) -> dict[str, object]:
    return {
        "ingress_sequence": identity.ingress_sequence,
        "source_namespace": identity.source_namespace.value,
    }


def _venue_document(
    source_namespace: SourceNamespace | None,
    venue_order_id: VenueOrderId | None,
) -> dict[str, object] | None:
    if source_namespace is None or venue_order_id is None:
        return None
    return {
        "source_namespace": source_namespace.value,
        "venue_order_id": venue_order_id.value,
    }


def _projection_document(snapshot: OrderProjectionSnapshot) -> dict[str, object]:
    if type(snapshot) is not OrderProjectionSnapshot:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "snapshot must be an exact OrderProjectionSnapshot",
        )
    return {
        "canonicalization": ORDER_PROJECTION_SNAPSHOT_CANONICALIZATION,
        "client_submission_key": snapshot.client_submission_key.value,
        "last_fact_key": _fact_key_document(snapshot.last_fact_key),
        "last_fact_sha256": snapshot.last_fact_sha256.value,
        "message_type": "order_projection_snapshot",
        "order_id": _economic_id_document(snapshot.order_id),
        "order_sha256": snapshot.order_sha256.value,
        "projected_executed_quantity": snapshot.projected_executed_quantity.text,
        "projection_state": snapshot.projection_state.value,
        "projection_version": snapshot.projection_version,
        "run_id": snapshot.run_id.value,
        "schema_version": ORDER_PROJECTION_SNAPSHOT_SCHEMA_VERSION,
        "venue_order": _venue_document(
            snapshot.venue_source_namespace,
            snapshot.venue_order_id,
        ),
    }


def canonical_order_projection_snapshot_bytes(
    snapshot: OrderProjectionSnapshot,
) -> bytes:
    """Return strict canonical JSON bytes for one projection."""
    return _encode_json(_projection_document(snapshot))


def order_projection_snapshot_digest(
    snapshot: OrderProjectionSnapshot,
) -> Sha256Digest:
    """Return the domain-separated digest for one projection."""
    return Sha256Digest(
        sha256(
            ORDER_PROJECTION_SNAPSHOT_DIGEST_DOMAIN
            + canonical_order_projection_snapshot_bytes(snapshot)
        ).hexdigest()
    )


def _binding_document(binding: OrderResolutionBinding) -> dict[str, object]:
    return {
        "key_kind": binding.key_kind.value,
        "resolved_order_id": _optional_economic_id_document(binding.resolved_order_id),
    }


def _outcome_document(outcome: ExecutionFactProcessingOutcome) -> dict[str, object]:
    if type(outcome) is not ExecutionFactProcessingOutcome:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "outcome must be an exact ExecutionFactProcessingOutcome",
        )
    fill_document = (
        None
        if outcome.fill_id is None or outcome.fill_sha256 is None
        else {
            "fill_id": _economic_id_document(outcome.fill_id),
            "fill_sha256": outcome.fill_sha256.value,
        }
    )
    return {
        "action": outcome.action.value,
        "anomalies": [anomaly.value for anomaly in outcome.anomalies],
        "canonicalization": EXECUTION_FACT_PROCESSING_OUTCOME_CANONICALIZATION,
        "client_submission_key": (
            None if outcome.client_submission_key is None else outcome.client_submission_key.value
        ),
        "fact_key": _fact_key_document(outcome.fact_key),
        "fact_sha256": outcome.fact_sha256.value,
        "fill": fill_document,
        "halt_requested": outcome.halt_requested,
        "ingress_identity": _ingress_identity_document(outcome.ingress_identity),
        "ingress_sha256": outcome.ingress_sha256.value,
        "message_type": "execution_fact_processing_outcome",
        "order_resolutions": [_binding_document(binding) for binding in outcome.order_resolutions],
        "outcome_code": outcome.outcome_code.value,
        "projection_after_sha256": (
            None
            if outcome.projection_after_sha256 is None
            else outcome.projection_after_sha256.value
        ),
        "projection_before_sha256": (
            None
            if outcome.projection_before_sha256 is None
            else outcome.projection_before_sha256.value
        ),
        "reported_order_id": _optional_economic_id_document(outcome.reported_order_id),
        "reported_venue_order": _venue_document(
            outcome.reported_venue_source_namespace,
            outcome.reported_venue_order_id,
        ),
        "requires_reconciliation": outcome.requires_reconciliation,
        "resolved_order_id": _optional_economic_id_document(outcome.resolved_order_id),
        "run_id": outcome.run_id.value,
        "runtime_dispatch_sequence": outcome.runtime_dispatch_sequence,
        "schema_version": EXECUTION_FACT_PROCESSING_OUTCOME_SCHEMA_VERSION,
    }


def canonical_execution_fact_processing_outcome_bytes(
    outcome: ExecutionFactProcessingOutcome,
) -> bytes:
    """Return strict canonical JSON bytes for one processing outcome."""
    return _encode_json(_outcome_document(outcome))


def execution_fact_processing_outcome_digest(
    outcome: ExecutionFactProcessingOutcome,
) -> Sha256Digest:
    """Return the domain-separated digest for one processing outcome."""
    return Sha256Digest(
        sha256(
            EXECUTION_FACT_PROCESSING_OUTCOME_DIGEST_DOMAIN
            + canonical_execution_fact_processing_outcome_bytes(outcome)
        ).hexdigest()
    )


def decode_order_projection_snapshot(
    payload: bytes,
    *,
    order: Order,
    spec_set: InstrumentExecutionSpecSet,
) -> OrderProjectionSnapshot:
    """Strictly decode one canonical projection against exact context."""
    document = _decode_json_document(payload)
    try:
        _require_exact_keys(
            document,
            {
                "canonicalization",
                "client_submission_key",
                "last_fact_key",
                "last_fact_sha256",
                "message_type",
                "order_id",
                "order_sha256",
                "projected_executed_quantity",
                "projection_state",
                "projection_version",
                "run_id",
                "schema_version",
                "venue_order",
            },
            "projection document",
        )
        key = _parse_fact_key(document["last_fact_key"])
        venue_source, venue_id = _parse_venue(document["venue_order"])
        snapshot = create_order_projection_snapshot(
            order=order,
            spec_set=spec_set,
            projection_version=_require_int(
                document["projection_version"],
                field_name="projection_version",
            ),
            projection_state=OrderProjectionState(
                _require_str(document["projection_state"], field_name="projection_state")
            ),
            projected_executed_quantity=CanonicalDecimal(
                _require_str(
                    document["projected_executed_quantity"],
                    field_name="projected_executed_quantity",
                )
            ),
            venue_source_namespace=venue_source,
            venue_order_id=venue_id,
            last_fact_key=key,
            last_fact_sha256=Sha256Digest(
                _require_str(document["last_fact_sha256"], field_name="last_fact_sha256")
            ),
        )
    except (KeyError, ValueError) as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "projection payload contains a noncanonical value",
        ) from error
    if (
        _projection_document(snapshot) != document
        or canonical_order_projection_snapshot_bytes(snapshot) != payload
    ):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "projection payload conflicts with its canonical contextual reconstruction",
        )
    return snapshot


def decode_execution_fact_processing_outcome(
    payload: bytes,
    *,
    ingress: ExecutionFactIngress,
    fill: Fill | None,
    projection_before: OrderProjectionSnapshot | None,
    projection_after: OrderProjectionSnapshot | None,
    resolved_orders: tuple[Order, ...],
) -> ExecutionFactProcessingOutcome:
    """Strictly decode one outcome against every referenced exact value."""
    document = _decode_json_document(payload)
    try:
        _require_exact_keys(
            document,
            {
                "action",
                "anomalies",
                "canonicalization",
                "client_submission_key",
                "fact_key",
                "fact_sha256",
                "fill",
                "halt_requested",
                "ingress_identity",
                "ingress_sha256",
                "message_type",
                "order_resolutions",
                "outcome_code",
                "projection_after_sha256",
                "projection_before_sha256",
                "reported_order_id",
                "reported_venue_order",
                "requires_reconciliation",
                "resolved_order_id",
                "run_id",
                "runtime_dispatch_sequence",
                "schema_version",
            },
            "outcome document",
        )
        order_by_id = _require_resolved_orders(resolved_orders)
        binding_documents = document["order_resolutions"]
        if type(binding_documents) is not list:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "order_resolutions must be a JSON array",
            )
        bindings: list[OrderResolutionBinding] = []
        for binding_document in binding_documents:
            mapping = _require_mapping(binding_document, field_name="order_resolution")
            _require_exact_keys(
                mapping,
                {"key_kind", "resolved_order_id"},
                "order resolution",
            )
            resolved_id = _parse_optional_economic_id(mapping["resolved_order_id"])
            resolved = None if resolved_id is None else order_by_id.get(resolved_id)
            if resolved_id is not None and resolved is None:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "outcome references an absent resolved Order",
                )
            bindings.append(
                create_order_resolution_binding(
                    key_kind=OrderResolutionKeyKind(
                        _require_str(mapping["key_kind"], field_name="key_kind")
                    ),
                    resolved_order=resolved,
                )
            )
        contextual_order_ids = tuple(order.order_id for order in resolved_orders)
        referenced_order_ids = tuple(
            sorted(
                {
                    binding.resolved_order_id
                    for binding in bindings
                    if binding.resolved_order_id is not None
                },
                key=lambda identity: (
                    identity.run_id.value,
                    identity.owner_kind.value,
                    identity.owner_sequence,
                ),
            )
        )
        if contextual_order_ids != referenced_order_ids:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "resolved_orders must exactly match distinct non-null binding results",
            )
        selected_id = _parse_optional_economic_id(document["resolved_order_id"])
        selected = None if selected_id is None else order_by_id.get(selected_id)
        if selected_id is not None and selected is None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "outcome selected Order is absent from resolved_orders",
            )
        anomaly_values = document["anomalies"]
        if type(anomaly_values) is not list:
            raise _fail(OutcomeCode.INVALID_TYPE, "anomalies must be a JSON array")
        outcome = create_execution_fact_processing_outcome(
            run_id=RunId(_require_str(document["run_id"], field_name="run_id")),
            runtime_dispatch_sequence=_require_int(
                document["runtime_dispatch_sequence"],
                field_name="runtime_dispatch_sequence",
            ),
            ingress=ingress,
            action=ExecutionFactAction(_require_str(document["action"], field_name="action")),
            anomalies=tuple(
                ExecutionFactAnomaly(_require_str(value, field_name="anomaly"))
                for value in anomaly_values
            ),
            order_resolutions=tuple(bindings),
            resolved_order=selected,
            fill=fill,
            projection_before=projection_before,
            projection_after=projection_after,
        )
    except (KeyError, ValueError) as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "outcome payload contains a noncanonical value",
        ) from error
    if (
        _outcome_document(outcome) != document
        or canonical_execution_fact_processing_outcome_bytes(outcome) != payload
    ):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "outcome payload conflicts with its canonical contextual reconstruction",
        )
    return outcome


def _require_resolved_orders(
    resolved_orders: object,
) -> dict[EconomicId, Order]:
    if type(resolved_orders) is not tuple or any(
        type(order) is not Order for order in resolved_orders
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "resolved_orders must be an exact tuple of Order values",
        )
    keys = tuple(
        (order.run_id.value, order.order_id.owner_kind.value, order.order_id.owner_sequence)
        for order in resolved_orders
    )
    if keys != tuple(sorted(set(keys))):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "resolved_orders must be unique and canonically ordered",
        )
    return {order.order_id: order for order in resolved_orders}


def _parse_optional_economic_id(value: object) -> EconomicId | None:
    if value is None:
        return None
    mapping = _require_mapping(value, field_name="economic_id")
    _require_exact_keys(mapping, {"owner_kind", "owner_sequence", "run_id"}, "economic ID")
    return EconomicId(
        RunId(_require_str(mapping["run_id"], field_name="run_id")),
        EconomicOwnerKind(_require_str(mapping["owner_kind"], field_name="owner_kind")),
        _require_int(mapping["owner_sequence"], field_name="owner_sequence"),
    )


def _parse_fact_key(value: object) -> FactDedupKey:
    mapping = _require_mapping(value, field_name="fact_key")
    _require_exact_keys(
        mapping,
        {"dedup_identity", "source_namespace"},
        "fact key",
    )
    identity_mapping = _require_mapping(
        mapping["dedup_identity"],
        field_name="dedup_identity",
    )
    _require_exact_keys(identity_mapping, {"kind", "value"}, "dedup identity")
    kind = _require_str(identity_mapping["kind"], field_name="dedup kind")
    raw_value = identity_mapping["value"]
    identity: ExternalFactId | SourceNativeSequence
    if kind == "external_id":
        identity = ExternalFactId(_require_str(raw_value, field_name="dedup value"))
    elif kind == "source_native_sequence":
        identity = SourceNativeSequence(_require_int(raw_value, field_name="dedup value"))
    else:
        raise _fail(OutcomeCode.CONFLICTING_ID, "unknown fact dedup identity kind")
    return FactDedupKey(
        SourceNamespace(_require_str(mapping["source_namespace"], field_name="source_namespace")),
        identity,
    )


def _parse_venue(
    value: object,
) -> tuple[SourceNamespace | None, VenueOrderId | None]:
    if value is None:
        return None, None
    mapping = _require_mapping(value, field_name="venue_order")
    _require_exact_keys(
        mapping,
        {"source_namespace", "venue_order_id"},
        "venue Order",
    )
    return (
        SourceNamespace(_require_str(mapping["source_namespace"], field_name="source_namespace")),
        VenueOrderId(_require_str(mapping["venue_order_id"], field_name="venue_order_id")),
    )


def _decode_json_document(payload: object) -> dict[str, object]:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical payload must be exact bytes")

    def pairs(pairs_value: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs_value:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_number(_value: str) -> NoReturn:
        raise ValueError("JSON floating point values are forbidden")

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "payload is not strict canonical JSON") from error
    return _require_mapping(decoded, field_name="payload")


def _require_mapping(value: object, *, field_name: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must be a JSON object with string keys",
        )
    return value


def _require_exact_keys(
    mapping: dict[str, object],
    expected: set[str],
    field_name: str,
) -> None:
    if set(mapping) != expected:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            f"{field_name} keys are not the closed schema",
        )


def _require_str(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact JSON string type",
        )
    return value


def _require_int(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact JSON integer type",
        )
    return value


def _encode_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _ingress_digest(ingress: ExecutionFactIngress) -> Sha256Digest:
    # Local import avoids widening the module's public dependency surface.
    from ea.core.execution_messages import execution_fact_ingress_digest

    return execution_fact_ingress_digest(ingress)


def _raise_public(error: object) -> NoReturn:
    code = getattr(error, "code", None)
    if type(code) is OutcomeCode:
        if code is OutcomeCode.INVALID_TYPE:
            raise _fail(OutcomeCode.INVALID_TYPE, str(error)) from (
                error if isinstance(error, BaseException) else None
            )
        if code in (
            OutcomeCode.OUT_OF_RANGE,
            OutcomeCode.NOT_QUANTIZED,
            OutcomeCode.PRICE_DOMAIN,
            OutcomeCode.ARITHMETIC_OVERFLOW,
            OutcomeCode.NON_FINITE,
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from (
                error if isinstance(error, BaseException) else None
            )
        raise _fail(OutcomeCode.CONFLICTING_ID, str(error)) from (
            error if isinstance(error, BaseException) else None
        )
    raise AssertionError("unexpected validation outcome at execution-state boundary") from (
        error if isinstance(error, BaseException) else None
    )
