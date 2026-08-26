"""Private recovery for the journal-bound read-only reconciliation route."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from ea.core.audit import AuditRecordKind, audit_append_acknowledgement_digest
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.lifecycle import LifecycleError
from ea.core.outcomes import OutcomeCode
from ea.core.reconciliation import decode_reconciliation_observation
from ea.core.run import Sha256Digest
from ea.core.runtime import (
    ReconciliationObservationKind,
    ReconciliationObservationRoot,
    create_reconciliation_observation_root,
    runtime_root_order_key,
)
from ea.runtime._coordinator_read_only import drive_read_only


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
    if complete and trace is None and lease is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completed read-only recovery lacks trace")
    if not complete and trace is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "incomplete read-only recovery has trace")
    if lease is not None:
        active = coordinator._capture_lease(lease)
        coordinator._active = active
        if complete:
            drive_read_only(coordinator, active, complete=True)
        else:
            drive_read_only(coordinator, active, complete=False)
    else:
        recovered_lease = _RecoveredLease(root, sequence)
        acknowledged_runtime = _AcknowledgedRuntime(recovered_lease)
        active = coordinator._capture_lease(recovered_lease)
        coordinator._active = active
        coordinator._runtime = acknowledged_runtime
        try:
            drive_read_only(coordinator, active, complete=True)
        finally:
            coordinator._runtime = runtime
    _require_retained_family(active, recovered)
    if complete and lease is not None and not getattr(runtime, "trace_records", ()):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion acknowledgement is missing")


def _require_retained_family(active: Any, recovered: Any) -> None:
    route = active.read_only
    for acknowledgement, retained in zip(
        (route.outcome_ack, route.refresh_ack, route.completion_ack),
        (
            recovered.read_only_outcome_record,
            recovered.refresh_record,
            recovered.completion_record,
        ),
        strict=True,
    ):
        if retained is None:
            continue
        if acknowledgement is None or (
            audit_append_acknowledgement_digest(acknowledgement)
            != audit_append_acknowledgement_digest(retained[2])
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID, "read-only recovery settlement conflicts"
            )


def _trace_order_key(root: ReconciliationObservationRoot) -> list[str | int]:
    return [
        value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if type(value) is datetime
        else cast(str | int, value)
        for value in runtime_root_order_key(root).as_tuple()
    ]
