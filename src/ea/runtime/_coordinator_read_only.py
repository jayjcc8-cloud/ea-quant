"""Private rank-20 reconciliation dispatch support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, overload

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_subject_digest,
    ordered_digest_tuple,
    require_audit_acknowledgement,
)
from ea.core.ledger_integration import (
    PortfolioRiskRefresh,
    canonical_portfolio_risk_refresh_bytes,
    portfolio_risk_refresh_digest,
)
from ea.core.lifecycle import (
    _VALUE_SEAL,
    ORDERED_INGRESS_DIGEST_DOMAIN,
    ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
    ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
    CoordinatorRunState,
    LifecycleError,
    ReadOnlyReconciliationDispatchOutcome,
    ReadOnlyReconciliationDispatchWindow,
    _create_read_only_reconciliation_dispatch_outcome,
    _create_read_only_reconciliation_dispatch_window,
    _create_structurally_valid_read_only_reconciliation_carrier,
    _SubjectBoundReadOnlyReconciliationWitness,
    canonical_dispatch_completed_v4_audit_payload,
    coordinator_run_state_digest,
    create_coordinator_run_state,
    dispatch_completed_subject_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import portfolio_snapshot_digest
from ea.core.reconciliation import (
    ReconciliationOutcome,
    canonical_reconciliation_outcome_bytes,
    reconciliation_observation_digest,
)
from ea.core.risk import RiskHaltReason, risk_state_snapshot_digest
from ea.core.runtime import (
    ReconciliationObservationKind,
    ReconciliationObservationRoot,
    runtime_root_order_key,
)


@dataclass(slots=True)
class _ReadOnlyDispatch:
    outcome: ReconciliationOutcome | None = None
    outcome_ack: AuditAppendAcknowledgement | None = None
    refresh: PortfolioRiskRefresh | None = None
    refresh_ack: AuditAppendAcknowledgement | None = None
    window: ReadOnlyReconciliationDispatchWindow | None = None
    completion_ack: AuditAppendAcknowledgement | None = None
    pre_ack_state: CoordinatorRunState | None = None


def is_read_only_root(root: object) -> bool:
    return type(root) is ReconciliationObservationRoot


@overload
def drive_read_only(
    coordinator: Any,
    active: Any,
    *,
    complete: Literal[False],
) -> ReadOnlyReconciliationDispatchWindow: ...


@overload
def drive_read_only(
    coordinator: Any,
    active: Any,
    *,
    complete: Literal[True],
) -> ReadOnlyReconciliationDispatchOutcome: ...


def drive_read_only(
    coordinator: Any,
    active: Any,
    *,
    complete: bool,
) -> ReadOnlyReconciliationDispatchWindow | ReadOnlyReconciliationDispatchOutcome:
    """Journal-bind one allowed rank-20 observation without effect capabilities."""
    coordinator._require_same_active_lease(active)
    root = active.lease.root
    if type(root) is not ReconciliationObservationRoot:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "read-only dispatch root is invalid")
    if root.kind not in {
        ReconciliationObservationKind.ORDER_DETAIL,
        ReconciliationObservationKind.POSITION_SNAPSHOT,
        ReconciliationObservationKind.CASH_SNAPSHOT,
    }:
        raise LifecycleError(
            OutcomeCode.OUT_OF_RANGE, "reconciliation observation kind is unsupported"
        )
    if root.observation_sha256 != reconciliation_observation_digest(root.observation):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "reconciliation root digest conflicts")
    route = getattr(active, "read_only", None)
    if route is None:
        route = _ReadOnlyDispatch()
        active.read_only = route
    authority = coordinator._reconciliation_authority
    gate = coordinator._read_only_gate()
    ledger, risk, refresh_authority, frontier = gate
    sequence = active.lease.dispatch_sequence
    if route.outcome is None:
        outcome = authority.admit_observation(root.observation, dispatch_sequence=sequence)
        coordinator._require_same_active_lease(active)
        retained = authority.resolve_outcome(root.observation)
        if (
            type(retained) is not ReconciliationOutcome
            or canonical_reconciliation_outcome_bytes(retained)
            != canonical_reconciliation_outcome_bytes(outcome)
            or retained.observation_sha256 != root.observation_sha256
            or retained.dispatch_sequence != sequence
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID, "reconciliation outcome is not authoritative"
            )
        route.outcome = retained
    outcome = route.outcome
    assert outcome is not None
    outcome_payload = canonical_reconciliation_outcome_bytes(outcome)
    outcome_key = AuditLogicalKey(
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        AuditSubjectKind.RECONCILIATION_OUTCOME,
        audit_subject_digest(AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME, outcome_payload),
    )
    if route.outcome_ack is None:
        coordinator._audit.append(
            record_kind=outcome_key.record_kind,
            subject_kind=outcome_key.subject_kind,
            subject_sha256=outcome_key.subject_sha256,
            canonical_payload=outcome_payload,
        )
        acknowledgement = coordinator._audit.settle_append(
            logical_key=outcome_key, canonical_payload=outcome_payload
        )
        if acknowledgement is None:
            raise LifecycleError(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH, "outcome settlement is missing"
            )
        _require_ack(coordinator, acknowledgement, outcome_key, outcome_payload)
        route.outcome_ack = acknowledgement
    outcome_ack = route.outcome_ack
    assert outcome_ack is not None
    if outcome.halt_requested:
        risk.engage_halt(
            RiskHaltReason.RECONCILIATION_REQUIRED,
            causal_root_available_at=root.available_at,
            dispatch_sequence=sequence,
        )
        coordinator._require_same_active_lease(active)
    snapshot = ledger.snapshot
    risk_state = risk.risk_state
    empty_frontier = ordered_digest_tuple(ORDERED_LEDGER_ACK_DIGEST_DOMAIN, ())
    if route.refresh is None:
        route.refresh = refresh_authority.create_refresh(
            snapshot=snapshot,
            risk_state=risk_state,
            dispatch_sequence=sequence,
            ordered_ledger_ack_frontier_sha256=empty_frontier,
            coordinator_running=coordinator._state.phase.value == "running",
            publication_window_clear=True,
            candidate_matches_internal=True,
        )
    refresh = route.refresh
    refresh_payload = canonical_portfolio_risk_refresh_bytes(refresh)
    refresh_key = AuditLogicalKey(
        AuditRecordKind.RISK_PORTFOLIO_REFRESH,
        AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
        audit_subject_digest(AuditRecordKind.RISK_PORTFOLIO_REFRESH, refresh_payload),
    )
    if route.refresh_ack is None:
        coordinator._audit.append(
            record_kind=refresh_key.record_kind,
            subject_kind=refresh_key.subject_kind,
            subject_sha256=refresh_key.subject_sha256,
            canonical_payload=refresh_payload,
        )
        acknowledgement = coordinator._audit.settle_append(
            logical_key=refresh_key, canonical_payload=refresh_payload
        )
        if acknowledgement is None:
            raise LifecycleError(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH, "refresh settlement is missing"
            )
        _require_ack(coordinator, acknowledgement, refresh_key, refresh_payload)
        route.refresh_ack = acknowledgement
    refresh_ack = route.refresh_ack
    assert refresh_ack is not None
    if refresh_ack.record_id.owner_sequence != outcome_ack.record_id.owner_sequence + 1:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "outcome and refresh records are not contiguous"
        )
    if (
        refresh.dispatch_sequence != sequence
        or refresh.ordered_ledger_ack_frontier_sha256 != empty_frontier
        or refresh.portfolio_snapshot_sha256 != portfolio_snapshot_digest(snapshot)
        or refresh.risk_state_sha256 != risk_state_snapshot_digest(risk_state)
        or portfolio_risk_refresh_digest(refresh) != portfolio_risk_refresh_digest(refresh)
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only refresh frontier conflicts")
    frontier.advance(snapshot=snapshot, risk_state=risk_state, refresh=refresh)
    if route.pre_ack_state is None:
        route.pre_ack_state = _pre_ack_state(coordinator, active, outcome_ack, refresh_ack)
    pre_ack_sha256 = coordinator_run_state_digest(route.pre_ack_state)
    if route.window is None:
        route.window = _create_read_only_window(
            coordinator._binding,
            coordinator._state.state_version,
            active,
            outcome_ack,
            refresh,
            refresh_ack,
            snapshot,
            risk_state,
            pre_ack_sha256,
        )
    if not complete:
        return route.window
    completion_payload = canonical_dispatch_completed_v4_audit_payload(
        binding=coordinator._binding,
        dispatch_sequence=sequence,
        trigger_root_key=runtime_root_order_key(root),
        trigger_root_sha256=active.trigger_sha256,
        observation_sha256=root.observation_sha256,
        outcome_acknowledgement=outcome_ack,
        refresh_acknowledgement=refresh_ack,
        refresh_value_sha256=portfolio_risk_refresh_digest(refresh),
        final_portfolio_snapshot_sha256=portfolio_snapshot_digest(frontier.current_snapshot()),
        final_risk_state_sha256=risk_state_snapshot_digest(frontier.current_state()),
        pre_ack_state_sha256=pre_ack_sha256,
    )
    completion_key = AuditLogicalKey(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
        AuditSubjectKind.RUNTIME_DISPATCH,
        dispatch_completed_subject_digest(completion_payload),
    )
    if route.completion_ack is None:
        acknowledgement = coordinator._audit.append(
            record_kind=completion_key.record_kind,
            subject_kind=completion_key.subject_kind,
            subject_sha256=completion_key.subject_sha256,
            canonical_payload=completion_payload,
        )
        _require_ack(coordinator, acknowledgement, completion_key, completion_payload)
        route.completion_ack = acknowledgement
    completion_ack = route.completion_ack
    assert completion_ack is not None
    if completion_ack.record_id.owner_sequence != refresh_ack.record_id.owner_sequence + 1:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "refresh and completion records are not contiguous"
        )
    coordinator._runtime.acknowledge(active.lease)
    resulting_state = _completed_state(coordinator, active, completion_ack)
    coordinator._state = resulting_state
    coordinator._active = None
    return _create_read_only_reconciliation_dispatch_outcome(
        window=route.window,
        dispatch_completion_ack_sha256=audit_append_acknowledgement_digest(completion_ack),
        resulting_state=resulting_state,
    )


def _create_read_only_window(
    binding: Any,
    state_version: int,
    active: Any,
    outcome_ack: AuditAppendAcknowledgement,
    refresh: PortfolioRiskRefresh,
    refresh_ack: AuditAppendAcknowledgement,
    snapshot: Any,
    risk_state: Any,
    pre_ack_sha256: Any,
) -> ReadOnlyReconciliationDispatchWindow:
    root = active.lease.root
    carrier = _create_structurally_valid_read_only_reconciliation_carrier(
        binding=binding,
        coordinator_state_version=state_version,
        dispatch_sequence=active.lease.dispatch_sequence,
        trigger_root_key=runtime_root_order_key(root),
        trigger_root_sha256=active.trigger_sha256,
        observation_sha256=root.observation_sha256,
        outcome_ack_sha256=audit_append_acknowledgement_digest(outcome_ack),
        refresh_ack_sha256=audit_append_acknowledgement_digest(refresh_ack),
        refresh_value_sha256=portfolio_risk_refresh_digest(refresh),
        final_portfolio_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        final_risk_state_sha256=risk_state_snapshot_digest(risk_state),
        pre_ack_state_sha256=pre_ack_sha256,
    )
    witness = object.__new__(_SubjectBoundReadOnlyReconciliationWitness)
    object.__setattr__(witness, "carrier", carrier)
    object.__setattr__(witness, "_seal", _VALUE_SEAL)
    return _create_read_only_reconciliation_dispatch_window(witness=witness)


def _require_ack(
    coordinator: Any,
    acknowledgement: AuditAppendAcknowledgement,
    key: AuditLogicalKey,
    payload: bytes,
) -> None:
    try:
        require_audit_acknowledgement(
            acknowledgement,
            binding=coordinator._binding,
            logical_key=key,
            canonical_payload=payload,
        )
    except ValueError as error:
        raise LifecycleError(
            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH, "journal settlement conflicts"
        ) from error


def _pre_ack_state(
    coordinator: Any,
    active: Any,
    outcome_ack: AuditAppendAcknowledgement,
    refresh_ack: AuditAppendAcknowledgement,
) -> CoordinatorRunState:
    state = coordinator._state
    return create_coordinator_run_state(
        binding=coordinator._binding,
        state_version=state.state_version + 1,
        phase=state.phase,
        active_dispatch_sequence=active.lease.dispatch_sequence,
        active_trigger_root_sha256=active.trigger_sha256,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=audit_append_acknowledgement_digest(outcome_ack),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
        last_audit_chain_head_sha256=refresh_ack.chain_head_sha256,
    )


def _completed_state(
    coordinator: Any, active: Any, completion_ack: AuditAppendAcknowledgement
) -> CoordinatorRunState:
    state = coordinator._state
    return create_coordinator_run_state(
        binding=coordinator._binding,
        state_version=state.state_version + 2,
        phase=state.phase,
        active_dispatch_sequence=None,
        active_trigger_root_sha256=None,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN, ()
        ),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=active.lease.dispatch_sequence,
        last_audit_chain_head_sha256=completion_ack.chain_head_sha256,
    )
