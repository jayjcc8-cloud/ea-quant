"""Private recovery for the journal-bound read-only reconciliation route."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from ea.core.audit import (
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    audit_subject_digest,
    ordered_digest_tuple,
    require_audit_acknowledgement,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.ledger_integration import (
    canonical_portfolio_risk_refresh_bytes,
    portfolio_risk_refresh_digest,
)
from ea.core.lifecycle import (
    ORDERED_INGRESS_DIGEST_DOMAIN,
    ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
    CoordinatorPhase,
    LifecycleError,
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
    decode_reconciliation_observation,
    reconciliation_observation_digest,
)
from ea.core.risk import RiskHaltReason, risk_state_snapshot_digest
from ea.core.run import Sha256Digest
from ea.core.runtime import (
    ReconciliationObservationKind,
    ReconciliationObservationRoot,
    create_reconciliation_observation_root,
    runtime_root_order_key,
)
from ea.runtime._coordinator_read_only import _pre_ack_state, _ReadOnlyDispatch, drive_read_only


@dataclass(frozen=True, slots=True)
class _RecoveredLease:
    root: ReconciliationObservationRoot
    dispatch_sequence: int


@dataclass(slots=True)
class _AcknowledgedRuntime:
    """Supply a retained lease while replaying an already acknowledged trace."""

    lease: _RecoveredLease

    @property
    def active_lease(self) -> _RecoveredLease:
        return self.lease

    def acknowledge(self, lease: object) -> None:
        if lease is not self.lease:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery lease conflicts")


def _decode_read_only_trace_root(
    document: dict[str, object], spec_set: object
) -> ReconciliationObservationRoot:
    root_document = document.get("root")
    if type(root_document) is not dict:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only trace root is invalid")
    try:
        payload = json.dumps(
            root_document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        root = create_reconciliation_observation_root(
            decode_reconciliation_observation(payload, cast(InstrumentExecutionSpecSet, spec_set))
        )
    except (TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "read-only trace root conflicts"
        ) from error
    if root.kind not in {
        ReconciliationObservationKind.ORDER_DETAIL,
        ReconciliationObservationKind.POSITION_SNAPSHOT,
        ReconciliationObservationKind.CASH_SNAPSHOT,
    }:
        raise LifecycleError(OutcomeCode.OUT_OF_RANGE, "read-only trace kind is unsupported")
    return root


def _retain_read_only_recovery_record(group: Any, retained: tuple[int, Any, Any]) -> None:
    if group.read_only_outcome_record is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate read-only recovery outcome")
    group.read_only_outcome_record = retained


def _require_read_only_recovery_order(group: Any) -> None:
    outcome = group.read_only_outcome_record
    if outcome is None or any(
        (
            group.batch_record,
            group.outcome_records,
            group.authorization_records,
            group.ledger_records,
            group.failing_record,
        )
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery family conflicts")
    entries = (outcome, group.refresh_record, group.completion_record)
    expected_kinds = (
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        AuditRecordKind.RISK_PORTFOLIO_REFRESH,
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
    )
    if group.refresh_record is None and group.completion_record is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery prefix conflicts")
    previous = outcome[0] - 1
    for entry, kind in zip(entries, expected_kinds, strict=True):
        if entry is None:
            break
        if entry[1].record_kind is not kind or entry[0] != previous + 1:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery prefix conflicts")
        previous = entry[0]


def recover_read_only_dispatch(
    coordinator: Any,
    recovered: Any,
    trace_by_sequence: Mapping[int, tuple[dict[str, object], Sha256Digest]],
) -> None:
    """Replay exactly one retained read-only journal family without effect owners."""
    _require_read_only_recovery_order(recovered)
    sequence = recovered.sequence
    trace = trace_by_sequence.get(sequence)
    runtime = coordinator._runtime
    lease = runtime.active_lease
    if lease is not None and lease.dispatch_sequence != sequence:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery sequence conflicts")
    if lease is not None:
        root = lease.root
        if type(root) is not ReconciliationObservationRoot:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery root conflicts")
    elif trace is not None:
        root = _decode_read_only_trace_root(trace[0], runtime.spec_set)
    else:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "incomplete read-only recovery has no lease"
        )
    if recovered.trigger_sha256 is not None and recovered.trigger_sha256 != root.observation_sha256:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery trigger conflicts")
    if trace is not None and (
        trace[1] != root.observation_sha256
        or trace[0].get("root_order_key") != _trace_order_key(root)
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery trace conflicts")
    complete = recovered.completion_record is not None
    if complete and trace is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion acknowledgement is missing")
    if not complete and trace is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "incomplete read-only recovery has trace")
    if not complete:
        assert lease is not None
        coordinator._active = coordinator._capture_lease(lease)
        return
    if lease is not None:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "runtime retained an acknowledged read-only lease"
        )
    recovered_lease = _RecoveredLease(root, sequence)
    active = _recovery_active(recovered_lease, root.observation_sha256)
    route = _prove_completed_read_only_family(coordinator, active, recovered)
    acknowledged_runtime = _AcknowledgedRuntime(recovered_lease)
    coordinator._runtime = acknowledged_runtime
    try:
        active = coordinator._capture_lease(recovered_lease)
        active.read_only = route
        coordinator._active = active
        drive_read_only(coordinator, active, complete=True)
    finally:
        coordinator._runtime = runtime


def _recovery_active(lease: _RecoveredLease, trigger_sha256: Sha256Digest) -> Any:
    return type("_RecoveryActive", (), {"lease": lease, "trigger_sha256": trigger_sha256})()


def _prove_completed_read_only_family(
    coordinator: Any, active: Any, recovered: Any
) -> _ReadOnlyDispatch:
    root = active.lease.root
    sequence = active.lease.dispatch_sequence
    authority = coordinator._reconciliation_authority
    admitted = authority.admit_observation(root.observation, dispatch_sequence=sequence)
    outcome = authority.resolve_outcome(root.observation)
    if (
        type(outcome) is not ReconciliationOutcome
        or canonical_reconciliation_outcome_bytes(outcome)
        != canonical_reconciliation_outcome_bytes(admitted)
        or outcome.observation_sha256 != reconciliation_observation_digest(root.observation)
        or outcome.dispatch_sequence != sequence
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery outcome conflicts")
    outcome_payload = canonical_reconciliation_outcome_bytes(outcome)
    outcome_key = AuditLogicalKey(
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        AuditSubjectKind.RECONCILIATION_OUTCOME,
        audit_subject_digest(AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME, outcome_payload),
    )
    outcome_ack = _require_retained_ack(
        coordinator, recovered.read_only_outcome_record, outcome_key, outcome_payload
    )
    ledger, risk, refresh_authority, _frontier = coordinator._read_only_gate()
    if outcome.halt_requested:
        risk.engage_halt(
            RiskHaltReason.RECONCILIATION_REQUIRED,
            causal_root_available_at=root.available_at,
            dispatch_sequence=sequence,
        )
    snapshot = ledger.snapshot
    risk_state = risk.risk_state
    refresh = refresh_authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=sequence,
        ordered_ledger_ack_frontier_sha256=_empty_ledger_frontier(),
        coordinator_running=coordinator._state.phase.value == "running",
        publication_window_clear=True,
        candidate_matches_internal=True,
    )
    refresh_payload = canonical_portfolio_risk_refresh_bytes(refresh)
    refresh_key = AuditLogicalKey(
        AuditRecordKind.RISK_PORTFOLIO_REFRESH,
        AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
        audit_subject_digest(AuditRecordKind.RISK_PORTFOLIO_REFRESH, refresh_payload),
    )
    refresh_ack = _require_retained_ack(
        coordinator, recovered.refresh_record, refresh_key, refresh_payload
    )
    if refresh_ack.record_id.owner_sequence != outcome_ack.record_id.owner_sequence + 1:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "outcome and refresh records are not contiguous"
        )
    pre_ack_state = _pre_ack_state(
        _recovery_state_view(coordinator, active), active, outcome_ack, refresh_ack
    )
    completion_payload = canonical_dispatch_completed_v4_audit_payload(
        binding=coordinator._binding,
        dispatch_sequence=sequence,
        trigger_root_key=runtime_root_order_key(root),
        trigger_root_sha256=active.trigger_sha256,
        observation_sha256=root.observation_sha256,
        outcome_acknowledgement=outcome_ack,
        refresh_acknowledgement=refresh_ack,
        refresh_value_sha256=portfolio_risk_refresh_digest(refresh),
        final_portfolio_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        final_risk_state_sha256=risk_state_snapshot_digest(risk_state),
        pre_ack_state_sha256=coordinator_run_state_digest(pre_ack_state),
    )
    completion_key = AuditLogicalKey(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
        AuditSubjectKind.RUNTIME_DISPATCH,
        dispatch_completed_subject_digest(completion_payload),
    )
    completion_ack = _require_retained_ack(
        coordinator, recovered.completion_record, completion_key, completion_payload
    )
    if completion_ack.record_id.owner_sequence != refresh_ack.record_id.owner_sequence + 1:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "refresh and completion records are not contiguous"
        )
    return _ReadOnlyDispatch(
        outcome=outcome,
        outcome_ack=outcome_ack,
        refresh=refresh,
        refresh_ack=refresh_ack,
        completion_ack=completion_ack,
        pre_ack_state=pre_ack_state,
    )


def _require_retained_ack(
    coordinator: Any, retained: Any, key: AuditLogicalKey, payload: bytes
) -> Any:
    if retained is None or retained[1].canonical_payload != payload:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery payload conflicts")
    acknowledgement = retained[2]
    try:
        require_audit_acknowledgement(
            acknowledgement,
            binding=coordinator._binding,
            logical_key=key,
            canonical_payload=payload,
        )
    except ValueError as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "read-only recovery settlement conflicts"
        ) from error
    return acknowledgement


def _empty_ledger_frontier() -> Sha256Digest:
    from ea.core.lifecycle import ORDERED_LEDGER_ACK_DIGEST_DOMAIN

    return ordered_digest_tuple(ORDERED_LEDGER_ACK_DIGEST_DOMAIN, ())


def _recovery_state_view(coordinator: Any, active: Any) -> Any:
    state = coordinator._state
    phase = (
        CoordinatorPhase.FAILING
        if state.phase is CoordinatorPhase.FAILING
        else CoordinatorPhase.RUNNING
    )
    captured = create_coordinator_run_state(
        binding=coordinator._binding,
        state_version=state.state_version + 1,
        phase=phase,
        active_dispatch_sequence=active.lease.dispatch_sequence,
        active_trigger_root_sha256=active.trigger_sha256,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN, ()
        ),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
        last_audit_chain_head_sha256=state.last_audit_chain_head_sha256,
    )
    return type("_RecoveryStateView", (), {"_binding": coordinator._binding, "_state": captured})()


def _trace_order_key(root: ReconciliationObservationRoot) -> list[str | int]:
    return [
        value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if type(value) is datetime
        else cast(str | int, value)
        for value in runtime_root_order_key(root).as_tuple()
    ]
