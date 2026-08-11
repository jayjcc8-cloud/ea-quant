"""Serialized Phase 1 historical lifecycle coordinator from Accepted ADR 0020."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from typing import cast, final

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditAppendPort,
    AuditContractError,
    AuditLogicalKey,
    AuditRecord,
    AuditRecordKind,
    AuditRecoveryRecordSource,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_record_digest,
    audit_subject_digest,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    ordered_digest_tuple,
    require_audit_acknowledgement,
)
from ea.core.execution import instrument_spec_set_digest
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    ExecutionFactIngress,
    Order,
    execution_fact_ingress_digest,
    execution_request_digest,
    order_digest,
)
from ea.core.execution_state import (
    ExecutionFactProcessingOutcome,
    canonical_execution_fact_processing_outcome_bytes,
    execution_fact_processing_outcome_digest,
)
from ea.core.historical_matching import (
    HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN,
    HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN,
    HistoricalDispatchKind,
    HistoricalMatcherDispatchBatch,
    HistoricalSubmissionReceipt,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_dispatch_batch_digest,
    historical_submission_receipt_digest,
)
from ea.core.lifecycle import (
    ORDERED_INGRESS_DIGEST_DOMAIN,
    ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
    ActiveDispatchWindow,
    ActiveDispatchWindowStage,
    AuditedExecutionFactHandoff,
    CoordinatorDispatchOutcome,
    CoordinatorPhase,
    CoordinatorRunState,
    CoordinatorTerminalKind,
    CoordinatorTerminalOutcome,
    ExecutionEvidenceResolverPort,
    ExecutionFactAuthorityPort,
    HistoricalMatcherPort,
    LifecycleError,
    PreTerminalCoordinatorState,
    RuntimeDispatchLeaseView,
    RuntimeLifecyclePort,
    SubmissionAuthorizationAttemptOutcome,
    SubmissionAuthorizationAttemptStatus,
    SubmissionAuthorizationPreparationPort,
    TerminalCoordinatorState,
    _create_active_dispatch_window,
    active_dispatch_window_digest,
    audited_execution_fact_handoff_digest,
    canonical_dispatch_completed_audit_payload,
    canonical_failing_safety_audit_payload,
    canonical_matcher_batch_audit_payload,
    canonical_run_terminal_audit_payload,
    coordinator_run_state_digest,
    create_audited_execution_fact_handoff,
    create_coordinator_dispatch_outcome,
    create_coordinator_run_state,
    create_coordinator_terminal_outcome,
    create_pre_terminal_coordinator_state,
    create_submission_authorization_attempt_outcome,
    create_terminal_coordinator_state,
    decode_submission_authorization_attempt_outcome_document,
    dispatch_completed_subject_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, Sha256Digest
from ea.core.runtime import EndOfRunRoot, RuntimeRoot, RuntimeRootOrderKey
from ea.runtime.historical import (
    HISTORICAL_RUNTIME_TRACE_SCHEMA,
    historical_runtime_trace_digest,
)

_MAX_UINT64 = (1 << 64) - 1


@dataclass(slots=True)
class _ActiveDispatch:
    lease: RuntimeDispatchLeaseView
    trigger_sha256: Sha256Digest
    batch: HistoricalMatcherDispatchBatch | None = None
    batch_ack: AuditAppendAcknowledgement | None = None
    outcomes: list[ExecutionFactProcessingOutcome | None] = field(default_factory=list)
    outcome_acks: list[AuditAppendAcknowledgement | None] = field(default_factory=list)
    handoffs: list[AuditedExecutionFactHandoff | None] = field(default_factory=list)
    completion_payload: bytes | None = None
    completion_ack: AuditAppendAcknowledgement | None = None
    window: ActiveDispatchWindow | None = None
    window_stage: ActiveDispatchWindowStage | None = None
    authorization_order: Order | None = None
    authorization_ack: AuditAppendAcknowledgement | None = None
    authorization_attempt: SubmissionAuthorizationAttemptOutcome | None = None
    submission_receipt: HistoricalSubmissionReceipt | None = None


@dataclass(slots=True)
class _RecoveredDispatch:
    sequence: int
    trigger_sha256: Sha256Digest | None = None
    batch_record: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None = None
    outcome_records: dict[
        Sha256Digest,
        tuple[int, AuditRecord, AuditAppendAcknowledgement],
    ] = field(default_factory=dict)
    failing_record: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None = None
    completion_record: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None = None
    authorization_records: list[tuple[int, AuditRecord, AuditAppendAcknowledgement]] = field(
        default_factory=list
    )


@dataclass(frozen=True, slots=True)
class _RecoveredLease:
    root: RuntimeRoot
    dispatch_sequence: int


@final
@dataclass(frozen=True, slots=True)
class RecoveredTerminalCoordinatorEvidence:
    """Read-only terminal reconstruction with no append or effect capability."""

    pre_terminal_state: PreTerminalCoordinatorState
    terminal_state: TerminalCoordinatorState
    terminal_outcome: CoordinatorTerminalOutcome


@dataclass(frozen=True, slots=True)
class _ReadOnlyRecoveryAudit:
    binding: RunBinding
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...]

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
        retained = (
            self.records.resolve_record(key)
            if not isinstance(self.records, tuple)
            else next((record for record in self.records if record.logical_key == key), None)
        )
        if retained is not None:
            if retained.canonical_payload != canonical_payload:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "read-only terminal recovery payload conflicts",
                )
            return create_audit_append_acknowledgement(retained)
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal recovery record is missing")

    def settle_append(
        self,
        *,
        logical_key: AuditLogicalKey,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        retained = (
            self.records.resolve_record(logical_key)
            if not isinstance(self.records, tuple)
            else next(
                (record for record in self.records if record.logical_key == logical_key),
                None,
            )
        )
        if retained is None:
            return None
        if retained.canonical_payload != canonical_payload:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "read-only terminal settlement payload conflicts",
            )
        return create_audit_append_acknowledgement(retained)


@final
class Phase1HistoricalLifecycleCoordinator:
    """One non-reentrant owner of matcher/fact/audit/runtime stage order."""

    __slots__ = (
        "_active",
        "_authorization",
        "_authorization_capability",
        "_audit",
        "_binding",
        "_fact_authority",
        "_failing_ack",
        "_failing_key",
        "_failing_payload",
        "_matcher",
        "_mutation_lock",
        "_resolver",
        "_runtime",
        "_state",
        "_pre_terminal_state",
        "_terminal_ack",
        "_terminal_outcome",
        "_terminal_state",
    )
    _active: _ActiveDispatch | None
    _authorization: SubmissionAuthorizationPreparationPort | None
    _authorization_capability: object | None
    _audit: AuditAppendPort
    _binding: RunBinding
    _fact_authority: ExecutionFactAuthorityPort
    _failing_ack: AuditAppendAcknowledgement | None
    _failing_key: AuditLogicalKey | None
    _failing_payload: bytes | None
    _matcher: HistoricalMatcherPort
    _mutation_lock: Lock
    _resolver: ExecutionEvidenceResolverPort
    _runtime: RuntimeLifecyclePort
    _state: CoordinatorRunState
    _pre_terminal_state: PreTerminalCoordinatorState | None
    _terminal_ack: AuditAppendAcknowledgement | None
    _terminal_outcome: CoordinatorTerminalOutcome | None
    _terminal_state: TerminalCoordinatorState | None

    def __init__(self) -> None:
        raise TypeError("coordinators are created only by create_phase1_lifecycle_coordinator")

    @property
    def binding(self) -> RunBinding:
        return self._binding

    @property
    def state(self) -> CoordinatorRunState:
        return self._state

    @property
    def pre_terminal_state(self) -> PreTerminalCoordinatorState | None:
        return self._pre_terminal_state

    @property
    def terminal_state(self) -> TerminalCoordinatorState | None:
        return self._terminal_state

    @property
    def terminal_outcome(self) -> CoordinatorTerminalOutcome | None:
        return self._terminal_outcome

    def begin_next_dispatch(self) -> ActiveDispatchWindow:
        """Drive one new runtime lease only through the audited handoff frontier."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            if self._pre_terminal_state is not None or self._terminal_state is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "coordinator no longer admits roots",
                )
            if self._active is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "an active dispatch must be resumed before another root is popped",
                )
            active = self._capture_lease(self._runtime.pop())
            self._active = active
            return self._drive_to_window(active)
        finally:
            self._mutation_lock.release()

    def resume_active_dispatch(self) -> ActiveDispatchWindow:
        """Replay missing pre-completion stages and return the retained window."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            if active is None or active.completion_ack is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "coordinator has no resumable active window",
                )
            self._require_same_active_lease(active)
            return self._drive_to_window(active)
        finally:
            self._mutation_lock.release()

    def complete_active_dispatch(
        self,
        window: ActiveDispatchWindow,
    ) -> CoordinatorDispatchOutcome:
        """Freeze and durably complete only the exact retained open window."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._require_window(window)
            if active.window_stage not in {
                ActiveDispatchWindowStage.OPEN,
                ActiveDispatchWindowStage.COMPLETION_FROZEN,
            }:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "window requires completion-only retry",
                )
            if (
                active.authorization_attempt is not None
                and active.authorization_attempt.status
                is SubmissionAuthorizationAttemptStatus.AUTHORIZED
                and active.submission_receipt is None
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "authorized window requires its exact matcher receipt",
                )
            if (
                active.authorization_attempt is not None
                and active.authorization_attempt.status
                is SubmissionAuthorizationAttemptStatus.UNRESOLVED
            ):
                raise LifecycleError(
                    OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                    "unresolved authorization attempt blocks completion",
                )
            return self._complete_active(active)
        finally:
            self._mutation_lock.release()

    def retry_active_dispatch_completion(self) -> CoordinatorDispatchOutcome:
        """Retry only a dispatch whose durable completion record already closed authorization."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            if active is None or active.completion_ack is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "coordinator has no completion-only retry",
                )
            active.window_stage = ActiveDispatchWindowStage.COMPLETION_ACKNOWLEDGED
            return self._complete_active(active)
        finally:
            self._mutation_lock.release()

    def _reconcile_recovered_authorization(self) -> None:
        """Refresh one incomplete durable attempt after sealed authority activation."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            authorization = self._authorization
            order = None if active is None else active.authorization_order
            if active is None or order is None:
                return
            if authorization is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovered authorization authority is unavailable",
                )
            request_sha256 = execution_request_digest(order)
            if active.submission_receipt is not None:
                if (
                    active.authorization_attempt is None
                    or active.authorization_attempt.status
                    is not SubmissionAuthorizationAttemptStatus.AUTHORIZED
                    or active.authorization_ack is None
                ):
                    raise LifecycleError(
                        OutcomeCode.CONFLICTING_ID,
                        "recovered submission frontier is not authorized",
                    )
                return
            attempt = authorization.resolve_attempt(
                order_id=order.order_id,
                execution_request_sha256=request_sha256,
            )
            acknowledgement = authorization.resolve_attempt_acknowledgement(
                order_id=order.order_id,
                execution_request_sha256=request_sha256,
            )
            if (
                type(attempt) is not SubmissionAuthorizationAttemptOutcome
                or type(acknowledgement) is not AuditAppendAcknowledgement
                or attempt.order_id != order.order_id
                or attempt.execution_request_sha256 != request_sha256
                or attempt.acknowledgement_sha256
                != audit_append_acknowledgement_digest(acknowledgement)
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovered authorization activation conflicts",
                )
            active.authorization_attempt = attempt
            active.authorization_ack = acknowledgement
            active.window = None
            active.window_stage = None
        finally:
            self._mutation_lock.release()

    def process_next_dispatch(self) -> CoordinatorDispatchOutcome:
        """Pop and process exactly one root; never loop over the complete run."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            if self._pre_terminal_state is not None or self._terminal_state is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "coordinator no longer admits roots",
                )
            if self._active is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "an active dispatch must be retried before another root is popped",
                )
            lease = self._runtime.pop()
            active = self._capture_lease(lease)
            self._active = active
            return self._drive_active(active)
        finally:
            self._mutation_lock.release()

    def retry_terminalization(self) -> CoordinatorTerminalOutcome:
        """Retry only the exact retained terminal record append."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            if self._terminal_outcome is not None:
                return self._terminal_outcome
            if self._pre_terminal_state is None or self._active is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "coordinator has no retryable terminalization",
                )
            return self._finish_terminalization()
        finally:
            self._mutation_lock.release()

    def retry_active_dispatch(self) -> CoordinatorDispatchOutcome:
        """Replay only missing stages for the exact retained active lease."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            if active is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "no active dispatch is retained")
            if active.completion_ack is not None:
                return self._complete_active(active)
            self._require_same_active_lease(active)
            return self._drive_active(active)
        finally:
            self._mutation_lock.release()

    def prepare_submission_authorization(
        self,
        window: ActiveDispatchWindow,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
    ) -> AuditAppendAcknowledgement:
        """Delegate the one coordinator-private pre-effect preparation capability."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            authorization = self._authorization
            if authorization is None:
                raise LifecycleError(
                    OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                    "authorization authority is not bound",
                )
            active = self._require_window(window)
            if (
                active.window_stage is not ActiveDispatchWindowStage.OPEN
                or not window.authorization_allowed
                or self._state.phase is not CoordinatorPhase.RUNNING
                or type(active.lease.root) is not MarketDataEnvelope
                or active.lease.root is not causal_market_root
                or active.lease.dispatch_sequence != dispatch_sequence
            ):
                raise LifecycleError(
                    OutcomeCode.RISK_STALE_APPROVAL,
                    "authorization requires the exact active market dispatch",
                )
            request_sha256 = execution_request_digest(order)
            retained_order = active.authorization_order
            if retained_order is not None:
                if (
                    retained_order.order_id != order.order_id
                    or order_digest(retained_order) != order_digest(order)
                    or execution_request_digest(retained_order) != request_sha256
                ):
                    raise LifecycleError(
                        OutcomeCode.RISK_STALE_APPROVAL,
                        "active window authorization chain is occupied",
                    )
                retained_attempt = active.authorization_attempt
                if (
                    retained_attempt is not None
                    and retained_attempt.status is SubmissionAuthorizationAttemptStatus.AUTHORIZED
                    and active.authorization_ack is not None
                ):
                    return active.authorization_ack
                if (
                    retained_attempt is None
                    or retained_attempt.status
                    is not SubmissionAuthorizationAttemptStatus.UNRESOLVED
                ):
                    raise LifecycleError(
                        (
                            OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
                            if retained_attempt is None or retained_attempt.error_code is None
                            else retained_attempt.error_code
                        ),
                        "active authorization attempt did not authorize",
                    )
            try:
                try:
                    attempt = authorization.prepare_attempt(
                        order,
                        causal_market_root=causal_market_root,
                        dispatch_sequence=dispatch_sequence,
                        capability=self._authorization_capability,
                    )
                except Exception:
                    resolved_order = authorization.resolve_attempt_order(
                        order_id=order.order_id,
                        execution_request_sha256=request_sha256,
                    )
                    resolved_attempt = authorization.resolve_attempt(
                        order_id=order.order_id,
                        execution_request_sha256=request_sha256,
                    )
                    if (
                        type(resolved_order) is not Order
                        or order_digest(resolved_order) != order_digest(order)
                        or execution_request_digest(resolved_order) != request_sha256
                        or type(resolved_attempt) is not SubmissionAuthorizationAttemptOutcome
                    ):
                        raise LifecycleError(
                            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                            "authorization exception could not be settled",
                        ) from None
                    attempt = resolved_attempt
                if (
                    type(attempt) is not SubmissionAuthorizationAttemptOutcome
                    or attempt.binding != self._binding
                    or attempt.dispatch_sequence != dispatch_sequence
                    or attempt.trigger_root_sha256 != active.trigger_sha256
                    or attempt.order_id != order.order_id
                    or attempt.execution_request_sha256 != request_sha256
                ):
                    raise LifecycleError(
                        OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                        "authorization attempt outcome conflicts",
                    )
                active.authorization_order = order
                active.authorization_attempt = attempt
                if attempt.acknowledgement_sha256 is not None:
                    acknowledgement = authorization.resolve_attempt_acknowledgement(
                        order_id=order.order_id,
                        execution_request_sha256=request_sha256,
                    )
                    if (
                        type(acknowledgement) is not AuditAppendAcknowledgement
                        or acknowledgement.binding != self._binding
                        or acknowledgement.record_kind
                        is not AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
                        or acknowledgement.subject_kind
                        is not AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST
                        or audit_append_acknowledgement_digest(acknowledgement)
                        != attempt.acknowledgement_sha256
                    ):
                        raise LifecycleError(
                            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                            "authorization acknowledgement conflicts",
                        )
                    active.authorization_ack = acknowledgement
                self._require_window(window)
                if (
                    attempt.status is SubmissionAuthorizationAttemptStatus.AUTHORIZED
                    and active.authorization_ack is None
                ):
                    raise LifecycleError(
                        OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                        "authorized attempt acknowledgement is missing",
                    )
            except Exception as error:
                error_code = getattr(error, "code", OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED)
                if type(error_code) is not OutcomeCode:
                    error_code = OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
                self._enter_failing(active, self._missing_keys(active), code=error_code)
                active.window = None
                active.window_stage = None
                raise
            if attempt.status is SubmissionAuthorizationAttemptStatus.AUTHORIZED:
                assert active.authorization_ack is not None
                return active.authorization_ack
            error_code = attempt.error_code or OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
            if attempt.status is SubmissionAuthorizationAttemptStatus.FAILED:
                self._enter_failing(
                    active,
                    (() if attempt.logical_key is None else (attempt.logical_key,)),
                    code=error_code,
                )
                active.authorization_order = None
                active.authorization_attempt = None
                active.authorization_ack = None
                active.submission_receipt = None
                active.window = None
                active.window_stage = None
            raise LifecycleError(error_code, "submission authorization attempt did not authorize")
        finally:
            self._mutation_lock.release()

    def submit_authorized_order(
        self,
        window: ActiveDispatchWindow,
        order: Order,
    ) -> HistoricalSubmissionReceipt:
        """Submit one exact authorized Order while its causal lease remains open."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._require_window(window)
            retained_order = active.authorization_order
            attempt = active.authorization_attempt
            acknowledgement = active.authorization_ack
            root = active.lease.root
            if (
                active.window_stage is not ActiveDispatchWindowStage.OPEN
                or type(root) is not MarketDataEnvelope
                or retained_order is None
                or order_digest(retained_order) != order_digest(order)
                or execution_request_digest(retained_order) != execution_request_digest(order)
                or attempt is None
                or attempt.status is not SubmissionAuthorizationAttemptStatus.AUTHORIZED
                or acknowledgement is None
            ):
                raise LifecycleError(
                    OutcomeCode.RISK_STALE_APPROVAL,
                    "matcher submission requires the exact authorized window chain",
                )
            retained_receipt = active.submission_receipt
            if retained_receipt is not None:
                resolved = self._matcher.resolve_submission_receipt(
                    order_id=order.order_id,
                    execution_request_sha256=execution_request_digest(order),
                )
                if type(
                    resolved
                ) is not HistoricalSubmissionReceipt or historical_submission_receipt_digest(
                    resolved
                ) != historical_submission_receipt_digest(retained_receipt):
                    raise LifecycleError(
                        OutcomeCode.CONFLICTING_ID,
                        "retained matcher submission receipt drifted",
                    )
                return retained_receipt
            receipt = self._matcher.submit(
                order,
                causal_market_root=root,
                dispatch_sequence=active.lease.dispatch_sequence,
            )
            self._require_window(window)
            resolved = self._matcher.resolve_submission_receipt(
                order_id=order.order_id,
                execution_request_sha256=execution_request_digest(order),
            )
            if (
                type(receipt) is not HistoricalSubmissionReceipt
                or type(resolved) is not HistoricalSubmissionReceipt
                or historical_submission_receipt_digest(resolved)
                != historical_submission_receipt_digest(receipt)
                or receipt.order_id != order.order_id
                or receipt.execution_request_sha256 != execution_request_digest(order)
                or receipt.dispatch_sequence != active.lease.dispatch_sequence
                or receipt.causal_market_sha256 != active.trigger_sha256
                or receipt.audit_acknowledgement_sha256
                != audit_append_acknowledgement_digest(acknowledgement)
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "matcher submission receipt conflicts with authorization",
                )
            active.submission_receipt = receipt
            return receipt
        finally:
            self._mutation_lock.release()

    def _require_window(self, window: ActiveDispatchWindow) -> _ActiveDispatch:
        active = self._active
        if (
            type(window) is not ActiveDispatchWindow
            or active is None
            or active.window is not window
            or window.binding != self._binding
            or window.coordinator_state_version != self._state.state_version
            or window.dispatch_sequence != active.lease.dispatch_sequence
            or window.trigger_root_sha256 != active.trigger_sha256
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active dispatch window conflicts")
        active_dispatch_window_digest(window)
        self._require_same_active_lease(active)
        if active.batch is None or active.batch_ack is None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID, "window handoff frontier is incomplete"
            )
        self._rebind_active_authorities(active)
        return active

    def _capture_lease(self, lease: RuntimeDispatchLeaseView) -> _ActiveDispatch:
        try:
            root = lease.root
            sequence = lease.dispatch_sequence
        except (AttributeError, TypeError) as error:
            raise LifecycleError(OutcomeCode.INVALID_TYPE, "runtime lease is incomplete") from error
        if type(sequence) is not int or sequence < 1:
            raise LifecycleError(OutcomeCode.OUT_OF_RANGE, "dispatch sequence must be positive")
        if type(root) is MarketDataEnvelope:
            trigger_sha256 = historical_market_root_digest(root)
        elif type(root) is EndOfRunRoot:
            trigger_sha256 = historical_end_root_digest(root)
        else:
            raise LifecycleError(OutcomeCode.INVALID_TYPE, "unsupported runtime root")
        state = self._state
        self._state = create_coordinator_run_state(
            binding=self._binding,
            state_version=state.state_version + 1,
            phase=(
                CoordinatorPhase.DRAINING
                if type(root) is EndOfRunRoot
                else (
                    CoordinatorPhase.FAILING
                    if state.phase is CoordinatorPhase.FAILING
                    else CoordinatorPhase.RUNNING
                )
            ),
            active_dispatch_sequence=sequence,
            active_trigger_root_sha256=trigger_sha256,
            matcher_batch_sha256=None,
            ordered_ingress_sha256s_sha256=ordered_digest_tuple(
                ORDERED_INGRESS_DIGEST_DOMAIN,
                (),
            ),
            ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                (),
            ),
            missing_audit_logical_keys=(),
            failure_code=state.failure_code,
            last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
            last_audit_chain_head_sha256=state.last_audit_chain_head_sha256,
        )
        return _ActiveDispatch(lease=lease, trigger_sha256=trigger_sha256)

    def _require_same_active_lease(self, active: _ActiveDispatch) -> None:
        current = self._runtime.active_lease
        if current is not active.lease:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime active lease changed")
        if (
            current.dispatch_sequence != active.lease.dispatch_sequence
            or current.root is not active.lease.root
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime active evidence changed")

    def _drive_active(self, active: _ActiveDispatch) -> CoordinatorDispatchOutcome:
        """Temporary migration path for callers not yet moved to the staged API."""
        self._drive_to_window(active)
        return self._complete_active(active)

    def _drive_to_window(self, active: _ActiveDispatch) -> ActiveDispatchWindow:
        self._require_same_active_lease(active)
        root = active.lease.root
        sequence = active.lease.dispatch_sequence
        try:
            if active.batch is None:
                if type(root) is MarketDataEnvelope:
                    active.batch = self._matcher.match_active_market_root(
                        root,
                        dispatch_sequence=sequence,
                    )
                elif type(root) is EndOfRunRoot:
                    active.batch = self._matcher.expire_at_active_end(
                        root,
                        dispatch_sequence=sequence,
                    )
                else:
                    raise LifecycleError(OutcomeCode.INVALID_TYPE, "unsupported runtime root")
            batch = active.batch
            self._require_batch(active, batch)
            batch_digest = historical_matcher_dispatch_batch_digest(batch)
            batch_payload = canonical_matcher_batch_audit_payload(self._binding, batch)
            first_error: Exception | None = None
            if self._failing_payload is not None and self._failing_ack is None:
                try:
                    self._append_failing_safety()
                except Exception as error:
                    first_error = error
            if active.batch_ack is None:
                try:
                    batch_acknowledgement = self._audit.append(
                        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
                        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
                        subject_sha256=batch_digest,
                        canonical_payload=batch_payload,
                    )
                    _require_exact_ack(
                        batch_acknowledgement,
                        binding=self._binding,
                        key=AuditLogicalKey(
                            AuditRecordKind.MATCHER_DISPATCH_BATCH,
                            AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
                            batch_digest,
                        ),
                        payload=batch_payload,
                    )
                    self._rebind_active_authorities(active)
                    active.batch_ack = batch_acknowledgement
                except Exception as error:
                    first_error = first_error or error
            count = len(batch.ingresses)
            if not active.outcomes:
                active.outcomes = [None] * count
                active.outcome_acks = [None] * count
                active.handoffs = [None] * count
            for index, ingress in enumerate(batch.ingresses):
                try:
                    processing_outcome = active.outcomes[index]
                    if processing_outcome is None:
                        processing_outcome = self._fact_authority.process_ingress(ingress)
                        self._require_same_active_lease(active)
                        self._require_authoritative_outcome(ingress, processing_outcome)
                        self._require_same_active_lease(active)
                        active.outcomes[index] = processing_outcome
                    outcome_payload = canonical_execution_fact_processing_outcome_bytes(
                        processing_outcome
                    )
                    outcome_digest = execution_fact_processing_outcome_digest(processing_outcome)
                    acknowledgement = active.outcome_acks[index]
                    if acknowledgement is None:
                        candidate_acknowledgement = self._audit.append(
                            record_kind=AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                            subject_kind=AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                            subject_sha256=outcome_digest,
                            canonical_payload=outcome_payload,
                        )
                        _require_exact_ack(
                            candidate_acknowledgement,
                            binding=self._binding,
                            key=AuditLogicalKey(
                                AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                                AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                                outcome_digest,
                            ),
                            payload=outcome_payload,
                        )
                        self._rebind_active_authorities(active)
                        acknowledgement = candidate_acknowledgement
                        active.outcome_acks[index] = acknowledgement
                    if active.handoffs[index] is None and active.batch_ack is not None:
                        self._require_evidence(processing_outcome)
                        self._rebind_active_authorities(active)
                        active.handoffs[index] = create_audited_execution_fact_handoff(
                            outcome=processing_outcome,
                            batch_acknowledgement=active.batch_ack,
                            outcome_acknowledgement=acknowledgement,
                        )
                except Exception as error:
                    first_error = first_error or error
            missing = tuple(
                key
                for key in self._missing_keys(active)
                if key.record_kind is not AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
            )
            if first_error is not None or missing:
                self._enter_failing(active, missing)
                if first_error is not None:
                    raise first_error
                raise LifecycleError(
                    OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                    "active dispatch retains missing audit evidence",
                )
            outcome_acks = tuple(value for value in active.outcome_acks if value is not None)
            handoffs = tuple(value for value in active.handoffs if value is not None)
            batch_ack = active.batch_ack
            if batch_ack is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "batch acknowledgement is missing")
            window = active.window
            if window is None:
                window = _create_active_dispatch_window(
                    binding=self._binding,
                    coordinator_state_version=self._state.state_version,
                    dispatch_kind=batch.dispatch_kind,
                    dispatch_sequence=batch.dispatch_sequence,
                    trigger_root_key=batch.trigger_root_key,
                    trigger_root_sha256=batch.trigger_root_sha256,
                    batch_sha256=historical_matcher_dispatch_batch_digest(batch),
                    batch_ack_sha256=audit_append_acknowledgement_digest(batch_ack),
                    handoff_sha256s=tuple(
                        audited_execution_fact_handoff_digest(value) for value in handoffs
                    ),
                    audited_handoff_chain_head_sha256=(
                        batch_ack.chain_head_sha256
                        if not outcome_acks
                        else outcome_acks[-1].chain_head_sha256
                    ),
                    authorization_allowed=(
                        type(root) is MarketDataEnvelope
                        and self._state.phase is CoordinatorPhase.RUNNING
                    ),
                )
                active.window = window
                active.window_stage = ActiveDispatchWindowStage.OPEN
            elif active.window_stage is not ActiveDispatchWindowStage.OPEN:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active window is not open")
            if window.coordinator_state_version != self._state.state_version:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active window state drifted")
            return window
        except AuditContractError as error:
            if self._pre_terminal_state is not None:
                raise
            self._enter_failing(active, self._missing_keys(active), code=error.code)
            raise
        except LifecycleError as error:
            if self._pre_terminal_state is None:
                self._enter_failing(active, self._missing_keys(active), code=error.code)
            raise
        except Exception:
            if self._pre_terminal_state is not None:
                raise
            self._enter_failing(active, self._missing_keys(active))
            raise

    def _complete_active(self, active: _ActiveDispatch) -> CoordinatorDispatchOutcome:
        root = active.lease.root
        try:
            if active.completion_ack is None:
                self._require_same_active_lease(active)
            batch = active.batch
            batch_ack = active.batch_ack
            if batch is None or batch_ack is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "dispatch frontier is incomplete")
            if any(value is None for value in active.outcomes) or any(
                value is None for value in active.outcome_acks
            ):
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "outcome frontier is incomplete")
            outcome_acks = tuple(value for value in active.outcome_acks if value is not None)
            handoffs = tuple(value for value in active.handoffs if value is not None)
            if len(handoffs) != len(batch.ingresses):
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "handoff frontier is incomplete")
            self._rebind_active_authorities(active)
            if active.completion_ack is None:
                active.window_stage = ActiveDispatchWindowStage.COMPLETION_FROZEN
                pre_ack_state = self._pre_ack_state(active, outcome_acks)
                completion_payload = canonical_dispatch_completed_audit_payload(
                    binding=self._binding,
                    batch=batch,
                    outcome_acknowledgements=outcome_acks,
                    pre_ack_state_sha256=coordinator_run_state_digest(pre_ack_state),
                    authorization_attempt_outcome=active.authorization_attempt,
                    submission_receipts=(
                        () if active.submission_receipt is None else (active.submission_receipt,)
                    ),
                )
                active.completion_payload = completion_payload
                completion_key = AuditLogicalKey(
                    AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
                    AuditSubjectKind.RUNTIME_DISPATCH,
                    dispatch_completed_subject_digest(completion_payload),
                )
                try:
                    completion_acknowledgement = self._audit.append(
                        record_kind=completion_key.record_kind,
                        subject_kind=completion_key.subject_kind,
                        subject_sha256=completion_key.subject_sha256,
                        canonical_payload=completion_payload,
                    )
                except Exception:
                    resolved_acknowledgement = self._resolve_committed_audit_acknowledgement(
                        key=completion_key,
                        payload=completion_payload,
                    )
                    if resolved_acknowledgement is None:
                        raise
                    completion_acknowledgement = resolved_acknowledgement
                _require_exact_ack(
                    completion_acknowledgement,
                    binding=self._binding,
                    key=completion_key,
                    payload=completion_payload,
                )
                self._rebind_active_authorities(active)
                active.completion_ack = completion_acknowledgement
                active.window_stage = ActiveDispatchWindowStage.COMPLETION_ACKNOWLEDGED
            self._rebind_active_authorities(active)
            try:
                self._runtime.acknowledge(active.lease)
            except Exception:
                if not self._confirm_committed_runtime_acknowledgement(active):
                    raise
            if type(root) is EndOfRunRoot and not self._confirm_committed_terminal(active):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime terminal acknowledgement evidence conflicts",
                )
            resulting_state = self._completed_state(active, outcome_acks)
            completion_ack = active.completion_ack
            if completion_ack is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion evidence is missing")
            dispatch_outcome = create_coordinator_dispatch_outcome(
                batch=batch,
                batch_acknowledgement=batch_ack,
                handoffs=handoffs,
                dispatch_completion_acknowledgement=completion_ack,
                runtime_acknowledged=True,
                resulting_state=resulting_state,
            )
            active.window_stage = ActiveDispatchWindowStage.COMPLETED
            self._state = resulting_state
            self._active = None
            if type(root) is EndOfRunRoot:
                self._begin_terminalization(active, resulting_state)
                self._finish_terminalization()
            return dispatch_outcome
        except AuditContractError as error:
            if self._pre_terminal_state is not None:
                raise
            self._enter_failing(active, self._missing_keys(active), code=error.code)
            if active.completion_ack is None:
                active.window = None
                active.window_stage = None
            raise
        except LifecycleError as error:
            if self._pre_terminal_state is None:
                self._enter_failing(active, self._missing_keys(active), code=error.code)
            if active.completion_ack is None:
                active.window = None
                active.window_stage = None
            raise
        except Exception:
            if self._pre_terminal_state is not None:
                raise
            self._enter_failing(active, self._missing_keys(active))
            if active.completion_ack is None:
                active.window = None
                active.window_stage = None
            raise

    def _begin_terminalization(
        self,
        active: _ActiveDispatch,
        resulting_state: CoordinatorRunState,
    ) -> None:
        completion = active.completion_ack
        if (
            completion is None
            or self._runtime.active_lease is not None
            or self._runtime.terminal_acknowledged is not True
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "runtime terminal evidence is incomplete",
            )
        terminal_kind = (
            CoordinatorTerminalKind.SUCCESS
            if resulting_state.failure_code is None
            else CoordinatorTerminalKind.FAILED
        )
        self._pre_terminal_state = create_pre_terminal_coordinator_state(
            binding=self._binding,
            state_version=resulting_state.state_version + 1,
            terminal_kind=terminal_kind,
            last_dispatch_sequence=active.lease.dispatch_sequence,
            last_trigger_root_sha256=active.trigger_sha256,
            dispatch_completion_ack_sha256=audit_append_acknowledgement_digest(completion),
            previous_chain_head_sha256=resulting_state.last_audit_chain_head_sha256,
            failure_code=resulting_state.failure_code,
        )

    def _confirm_committed_terminal(self, active: _ActiveDispatch) -> bool:
        return type(
            active.lease.root
        ) is EndOfRunRoot and self._confirm_committed_runtime_acknowledgement(active)

    def _confirm_committed_runtime_acknowledgement(
        self,
        active: _ActiveDispatch,
    ) -> bool:
        root = active.lease.root
        batch = active.batch
        if (
            batch is None
            or self._runtime.active_lease is not None
            or type(root) not in {MarketDataEnvelope, EndOfRunRoot}
        ):
            return False
        try:
            trace = _require_runtime_trace(self._runtime, self._binding)
        except LifecycleError:
            return False
        if set(trace) != set(range(1, active.lease.dispatch_sequence + 1)):
            return False
        trace_entry = trace.get(active.lease.dispatch_sequence)
        if trace_entry is None:
            return False
        document, root_sha256 = trace_entry
        is_terminal = type(root) is EndOfRunRoot
        return (
            root_sha256 == active.trigger_sha256
            and document.get("root_order_key")
            == _trace_root_order_key_document(batch.trigger_root_key)
            and document.get("terminal_acknowledged") is is_terminal
            and self._runtime.terminal_acknowledged is is_terminal
        )

    def _resolve_committed_audit_acknowledgement(
        self,
        *,
        key: AuditLogicalKey,
        payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        acknowledgement = self._audit.settle_append(
            logical_key=key,
            canonical_payload=payload,
        )
        if acknowledgement is None:
            return None
        _require_exact_ack(
            acknowledgement,
            binding=self._binding,
            key=key,
            payload=payload,
        )
        return acknowledgement

    def _finish_terminalization(self) -> CoordinatorTerminalOutcome:
        pre_terminal = self._pre_terminal_state
        if pre_terminal is None:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "pre-terminal state is missing")
        if (
            self._runtime.active_lease is not None
            or self._runtime.terminal_acknowledged is not True
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime terminal evidence changed")
        payload = canonical_run_terminal_audit_payload(pre_terminal)
        if self._terminal_ack is None:
            terminal_acknowledgement = self._audit.append(
                record_kind=AuditRecordKind.RUN_TERMINAL,
                subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
                subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
                canonical_payload=payload,
            )
            _require_exact_ack(
                terminal_acknowledgement,
                binding=self._binding,
                key=AuditLogicalKey(
                    AuditRecordKind.RUN_TERMINAL,
                    AuditSubjectKind.RUN_TERMINAL_STATE,
                    audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
                ),
                payload=payload,
            )
            self._terminal_ack = terminal_acknowledgement
        terminal_state = create_terminal_coordinator_state(
            pre_terminal_state=pre_terminal,
            terminal_acknowledgement=self._terminal_ack,
        )
        outcome = create_coordinator_terminal_outcome(
            pre_terminal_state=pre_terminal,
            terminal_acknowledgement=self._terminal_ack,
            terminal_state=terminal_state,
        )
        self._terminal_state = terminal_state
        self._terminal_outcome = outcome
        return outcome

    def _require_batch(
        self,
        active: _ActiveDispatch,
        batch: HistoricalMatcherDispatchBatch,
    ) -> None:
        if (
            type(batch) is not HistoricalMatcherDispatchBatch
            or batch.run_id != self._binding.reference.run_id
            or batch.dispatch_sequence != active.lease.dispatch_sequence
            or batch.trigger_root_sha256 != active.trigger_sha256
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "matcher batch conflicts with lease")
        resolved = self._matcher.resolve_dispatch_batch(
            dispatch_sequence=batch.dispatch_sequence,
            trigger_root_sha256=batch.trigger_root_sha256,
        )
        if resolved != batch:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "matcher batch is not retained")
        self._require_same_active_lease(active)

    def _require_evidence(self, outcome: ExecutionFactProcessingOutcome) -> None:
        if outcome.fill_id is not None:
            if outcome.fill_sha256 is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "Fill digest is missing")
            fill = self._resolver.resolve_fill(
                fill_id=outcome.fill_id,
                fill_sha256=outcome.fill_sha256,
            )
            if fill is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "Fill evidence is unresolved")
        if outcome.projection_after_sha256 is not None:
            if outcome.resolved_order_id is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "projection has no resolved Order identity",
                )
            projection = self._resolver.resolve_projection_after(
                order_id=outcome.resolved_order_id,
                projection_sha256=outcome.projection_after_sha256,
            )
            if projection is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "projection evidence is unresolved",
                )

    def _require_authoritative_outcome(
        self,
        ingress: ExecutionFactIngress,
        outcome: ExecutionFactProcessingOutcome,
    ) -> None:
        try:
            identity = ingress.identity
            ingress_sha256 = execution_fact_ingress_digest(ingress)
        except (AttributeError, TypeError, ValueError) as error:
            raise LifecycleError(OutcomeCode.INVALID_TYPE, "ingress evidence is invalid") from error
        resolved = self._fact_authority.resolve_processing_outcome(
            ingress_identity=identity,
            ingress_sha256=ingress_sha256,
        )
        if (
            type(resolved) is not ExecutionFactProcessingOutcome
            or canonical_execution_fact_processing_outcome_bytes(resolved)
            != canonical_execution_fact_processing_outcome_bytes(outcome)
            or execution_fact_processing_outcome_digest(resolved)
            != execution_fact_processing_outcome_digest(outcome)
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "fact outcome is not authoritative")

    def _rebind_active_authorities(self, active: _ActiveDispatch) -> None:
        self._require_same_active_lease(active)
        batch = active.batch
        if batch is None:
            return
        self._require_batch(active, batch)
        for ingress, outcome in zip(batch.ingresses, active.outcomes, strict=False):
            if outcome is not None:
                self._require_authoritative_outcome(ingress, outcome)
                self._require_same_active_lease(active)
                self._require_evidence(outcome)
                self._require_same_active_lease(active)

    def _missing_keys(self, active: _ActiveDispatch) -> tuple[AuditLogicalKey, ...]:
        missing: list[AuditLogicalKey] = []
        batch = active.batch
        if batch is not None and active.batch_ack is None:
            missing.append(
                AuditLogicalKey(
                    AuditRecordKind.MATCHER_DISPATCH_BATCH,
                    AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
                    historical_matcher_dispatch_batch_digest(batch),
                )
            )
        for outcome, acknowledgement in zip(active.outcomes, active.outcome_acks, strict=True):
            if outcome is not None and acknowledgement is None:
                missing.append(
                    AuditLogicalKey(
                        AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                        AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                        execution_fact_processing_outcome_digest(outcome),
                    )
                )
        if active.completion_payload is not None and active.completion_ack is None:
            missing.append(
                AuditLogicalKey(
                    AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
                    AuditSubjectKind.RUNTIME_DISPATCH,
                    dispatch_completed_subject_digest(active.completion_payload),
                )
            )
        return tuple(missing)

    def _enter_failing(
        self,
        active: _ActiveDispatch,
        missing: tuple[AuditLogicalKey, ...],
        *,
        code: OutcomeCode = OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
    ) -> None:
        state = self._state
        if state.phase is CoordinatorPhase.FAILING:
            return
        failing_state = create_coordinator_run_state(
            binding=self._binding,
            state_version=state.state_version + 1,
            phase=CoordinatorPhase.FAILING,
            active_dispatch_sequence=active.lease.dispatch_sequence,
            active_trigger_root_sha256=active.trigger_sha256,
            matcher_batch_sha256=(
                None
                if active.batch is None
                else historical_matcher_dispatch_batch_digest(active.batch)
            ),
            ordered_ingress_sha256s_sha256=(
                ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ())
                if active.batch is None
                else ordered_digest_tuple(
                    ORDERED_INGRESS_DIGEST_DOMAIN,
                    active.batch.ingress_sha256s,
                )
            ),
            ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                tuple(
                    audit_append_acknowledgement_digest(acknowledgement)
                    for acknowledgement in active.outcome_acks
                    if acknowledgement is not None
                ),
            ),
            missing_audit_logical_keys=missing,
            failure_code=state.failure_code or code,
            last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
            last_audit_chain_head_sha256=_latest_active_chain_head(active, state),
        )
        self._state = failing_state
        failed_key = self._failed_logical_key(active, missing)
        payload = canonical_failing_safety_audit_payload(
            binding=self._binding,
            previous_state_sha256=coordinator_run_state_digest(state),
            failing_state_sha256=coordinator_run_state_digest(failing_state),
            failure_code=failing_state.failure_code or code,
            failed_logical_key=failed_key,
            dispatch_sequence=active.lease.dispatch_sequence,
            trigger_root_sha256=active.trigger_sha256,
        )
        self._failing_payload = payload
        self._failing_key = AuditLogicalKey(
            AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION,
            AuditSubjectKind.COORDINATOR_STATE,
            audit_subject_digest(AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION, payload),
        )
        with suppress(Exception):
            self._append_failing_safety()

    def _failed_logical_key(
        self,
        active: _ActiveDispatch,
        missing: tuple[AuditLogicalKey, ...],
    ) -> AuditLogicalKey:
        if missing:
            return missing[0]
        for acknowledgement in (
            active.completion_ack,
            *(value for value in reversed(active.outcome_acks) if value is not None),
            active.batch_ack,
        ):
            if acknowledgement is not None:
                return AuditLogicalKey(
                    acknowledgement.record_kind,
                    acknowledgement.subject_kind,
                    acknowledgement.subject_sha256,
                )
        return AuditLogicalKey(
            AuditRecordKind.MATCHER_DISPATCH_BATCH,
            AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
            active.trigger_sha256,
        )

    def _append_failing_safety(self) -> None:
        payload = self._failing_payload
        key = self._failing_key
        if payload is None or key is None or self._failing_ack is not None:
            return
        acknowledgement = self._audit.append(
            record_kind=key.record_kind,
            subject_kind=key.subject_kind,
            subject_sha256=key.subject_sha256,
            canonical_payload=payload,
        )
        _require_exact_ack(
            acknowledgement,
            binding=self._binding,
            key=key,
            payload=payload,
        )
        self._failing_ack = acknowledgement

    def _pre_ack_state(
        self,
        active: _ActiveDispatch,
        outcome_acks: tuple[AuditAppendAcknowledgement, ...],
    ) -> CoordinatorRunState:
        batch = active.batch
        if batch is None:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "batch is missing")
        state = self._state
        return create_coordinator_run_state(
            binding=self._binding,
            state_version=state.state_version + 1,
            phase=state.phase,
            active_dispatch_sequence=active.lease.dispatch_sequence,
            active_trigger_root_sha256=active.trigger_sha256,
            matcher_batch_sha256=historical_matcher_dispatch_batch_digest(batch),
            ordered_ingress_sha256s_sha256=ordered_digest_tuple(
                ORDERED_INGRESS_DIGEST_DOMAIN,
                batch.ingress_sha256s,
            ),
            ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                tuple(audit_append_acknowledgement_digest(value) for value in outcome_acks),
            ),
            missing_audit_logical_keys=(),
            failure_code=state.failure_code,
            last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
            last_audit_chain_head_sha256=(
                active.authorization_ack.chain_head_sha256
                if active.authorization_ack is not None
                else (
                    active.batch_ack.chain_head_sha256
                    if not outcome_acks and active.batch_ack is not None
                    else outcome_acks[-1].chain_head_sha256
                )
            ),
        )

    def _completed_state(
        self,
        active: _ActiveDispatch,
        outcome_acks: tuple[AuditAppendAcknowledgement, ...],
    ) -> CoordinatorRunState:
        batch = active.batch
        completion = active.completion_ack
        if batch is None or completion is None:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion evidence is missing")
        state = self._state
        return create_coordinator_run_state(
            binding=self._binding,
            state_version=state.state_version + 2,
            phase=(
                CoordinatorPhase.DRAINING
                if type(active.lease.root) is EndOfRunRoot
                else state.phase
            ),
            active_dispatch_sequence=None,
            active_trigger_root_sha256=None,
            matcher_batch_sha256=None,
            ordered_ingress_sha256s_sha256=ordered_digest_tuple(
                ORDERED_INGRESS_DIGEST_DOMAIN,
                (),
            ),
            ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                (),
            ),
            missing_audit_logical_keys=(),
            failure_code=state.failure_code,
            last_completed_dispatch_sequence=batch.dispatch_sequence,
            last_audit_chain_head_sha256=_latest_completion_chain_head(
                completion,
                self._failing_ack,
            ),
        )


def create_phase1_lifecycle_coordinator(
    *,
    binding: RunBinding,
    prepared_acknowledgement: AuditAppendAcknowledgement,
    audit: AuditAppendPort,
    runtime: RuntimeLifecyclePort,
    matcher: HistoricalMatcherPort,
    fact_authority: ExecutionFactAuthorityPort,
    evidence_resolver: ExecutionEvidenceResolverPort,
    authorization: SubmissionAuthorizationPreparationPort | None = None,
    authorization_capability: object | None = None,
) -> Phase1HistoricalLifecycleCoordinator:
    """Bind dependency-neutral owners after the journal preparation record is durable."""
    if type(binding) is not RunBinding:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "binding must be exact")
    if (authorization is None) != (authorization_capability is None):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "authorization port and capability must be bound together",
        )
    try:
        audit_binding = audit.binding
        run_ids = (runtime.run_id, matcher.run_id, fact_authority.run_id)
        spec_digests = tuple(
            instrument_spec_set_digest(value)
            for value in (runtime.spec_set, matcher.spec_set, fact_authority.spec_set)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE, "coordinator ports are incomplete"
        ) from error
    if (
        audit_binding != binding
        or any(value != binding.reference.run_id for value in run_ids)
        or len(set(spec_digests)) != 1
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator bindings conflict")
    _require_exact_ack(
        prepared_acknowledgement,
        binding=binding,
        key=AuditLogicalKey(
            AuditRecordKind.RUN_PREPARED,
            AuditSubjectKind.RUN_MANIFEST,
            binding.manifest_sha256,
        ),
        payload=canonical_run_prepared_audit_payload(binding),
    )
    value = _allocate_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=fact_authority,
        evidence_resolver=evidence_resolver,
        authorization=authorization,
        authorization_capability=authorization_capability,
    )
    value._state = _admitted_state(binding, prepared_acknowledgement.chain_head_sha256)
    return value


def recover_phase1_lifecycle_coordinator(
    *,
    binding: RunBinding,
    audit: AuditAppendPort,
    runtime: RuntimeLifecyclePort,
    matcher: HistoricalMatcherPort,
    fact_authority: ExecutionFactAuthorityPort,
    evidence_resolver: ExecutionEvidenceResolverPort,
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    authorization: SubmissionAuthorizationPreparationPort | None = None,
    authorization_capability: object | None = None,
) -> Phase1HistoricalLifecycleCoordinator:
    """Reconcile one reopened non-terminal journal with injected authority histories."""
    _require_static_bindings(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=fact_authority,
        authorization=authorization,
        authorization_capability=authorization_capability,
    )
    recovered = iter(_require_recovery_records(binding, records, audit=audit))
    try:
        _prepared, prepared_acknowledgement = next(recovered)
    except StopIteration as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery preparation record is missing",
        ) from error
    value = _allocate_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=fact_authority,
        evidence_resolver=evidence_resolver,
        authorization=authorization,
        authorization_capability=authorization_capability,
    )
    value._state = _admitted_state(binding, prepared_acknowledgement.chain_head_sha256)
    expected_sequence = 1
    trace_by_sequence = _require_runtime_trace(runtime, binding)
    for dispatch in _group_recovery_records(recovered):
        if dispatch.sequence != expected_sequence:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery dispatch sequence is not contiguous",
            )
        expected_sequence += 1
        _recover_dispatch(value, dispatch, trace_by_sequence)
    if value._active is None and runtime.active_lease is not None:
        lease = runtime.active_lease
        if lease.dispatch_sequence != expected_sequence:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "unrecorded active runtime sequence conflicts",
            )
        active = value._capture_lease(lease)
        retained_batch = matcher.resolve_dispatch_batch(
            dispatch_sequence=lease.dispatch_sequence,
            trigger_root_sha256=active.trigger_sha256,
        )
        if retained_batch is not None:
            value._require_batch(active, retained_batch)
            active.batch = retained_batch
        _recover_pre_batch_failing_transition(value, active, None)
        value._active = active
    trace_sequences = set(trace_by_sequence)
    completed_sequence = value._state.last_completed_dispatch_sequence
    expected_trace_sequences = (
        set() if completed_sequence is None else set(range(1, completed_sequence + 1))
    )
    if trace_sequences != expected_trace_sequences:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "runtime trace and recovered completion prefix conflict",
        )
    return value


def recover_phase1_terminal_evidence(
    *,
    binding: RunBinding,
    runtime: RuntimeLifecyclePort,
    matcher: HistoricalMatcherPort,
    fact_authority: ExecutionFactAuthorityPort,
    evidence_resolver: ExecutionEvidenceResolverPort,
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
) -> RecoveredTerminalCoordinatorEvidence:
    """Reconstruct a closed run without issuing any mutation authority."""
    record_count = _recovery_record_count(records)
    if record_count < 2:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "terminal recovery records are incomplete")
    terminal_record = _recovery_record_at(records, record_count - 1)
    if (
        type(terminal_record) is not AuditRecord
        or terminal_record.binding != binding
        or terminal_record.record_kind is not AuditRecordKind.RUN_TERMINAL
        or terminal_record.record_id.owner_sequence != record_count
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal recovery record conflicts")
    prefix = _recovery_record_prefix(records, record_count - 1)
    prior = _recovery_record_at(prefix, record_count - 2)
    if terminal_record.previous_record_sha256 != audit_record_digest(
        prior
    ) or terminal_record.previous_chain_head_sha256 != audit_chain_head(prior):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal recovery chain conflicts")
    coordinator = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=_ReadOnlyRecoveryAudit(binding, prefix),
        runtime=runtime,
        matcher=matcher,
        fact_authority=fact_authority,
        evidence_resolver=evidence_resolver,
        records=prefix,
    )
    pre_terminal = coordinator.pre_terminal_state
    if pre_terminal is None or coordinator.terminal_state is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal prefix is not pre-terminal")
    payload = canonical_run_terminal_audit_payload(pre_terminal)
    if terminal_record.canonical_payload != payload:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal recovery payload conflicts")
    terminal_ack = create_audit_append_acknowledgement(terminal_record)
    _require_exact_ack(
        terminal_ack,
        binding=binding,
        key=AuditLogicalKey(
            AuditRecordKind.RUN_TERMINAL,
            AuditSubjectKind.RUN_TERMINAL_STATE,
            audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        ),
        payload=payload,
    )
    terminal_state = create_terminal_coordinator_state(
        pre_terminal_state=pre_terminal,
        terminal_acknowledgement=terminal_ack,
    )
    terminal_outcome = create_coordinator_terminal_outcome(
        pre_terminal_state=pre_terminal,
        terminal_acknowledgement=terminal_ack,
        terminal_state=terminal_state,
    )
    return RecoveredTerminalCoordinatorEvidence(
        pre_terminal_state=pre_terminal,
        terminal_state=terminal_state,
        terminal_outcome=terminal_outcome,
    )


def _require_static_bindings(
    *,
    binding: RunBinding,
    audit: AuditAppendPort,
    runtime: RuntimeLifecyclePort,
    matcher: HistoricalMatcherPort,
    fact_authority: ExecutionFactAuthorityPort,
    authorization: SubmissionAuthorizationPreparationPort | None,
    authorization_capability: object | None,
) -> None:
    if type(binding) is not RunBinding:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "binding must be exact")
    if (authorization is None) != (authorization_capability is None):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "authorization port and capability must be bound together",
        )
    try:
        audit_binding = audit.binding
        run_ids = (runtime.run_id, matcher.run_id, fact_authority.run_id)
        spec_digests = tuple(
            instrument_spec_set_digest(candidate)
            for candidate in (runtime.spec_set, matcher.spec_set, fact_authority.spec_set)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "coordinator ports are incomplete",
        ) from error
    if (
        audit_binding != binding
        or any(run_id != binding.reference.run_id for run_id in run_ids)
        or len(set(spec_digests)) != 1
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator bindings conflict")


def _require_exact_ack(
    acknowledgement: AuditAppendAcknowledgement,
    *,
    binding: RunBinding,
    key: AuditLogicalKey,
    payload: bytes,
) -> None:
    try:
        require_audit_acknowledgement(
            acknowledgement,
            binding=binding,
            logical_key=key,
            canonical_payload=payload,
        )
    except AuditContractError as error:
        raise LifecycleError(error.code, "audit acknowledgement conflicts") from error


def _allocate_coordinator(
    *,
    binding: RunBinding,
    audit: AuditAppendPort,
    runtime: RuntimeLifecyclePort,
    matcher: HistoricalMatcherPort,
    fact_authority: ExecutionFactAuthorityPort,
    evidence_resolver: ExecutionEvidenceResolverPort,
    authorization: SubmissionAuthorizationPreparationPort | None,
    authorization_capability: object | None,
) -> Phase1HistoricalLifecycleCoordinator:
    value = object.__new__(Phase1HistoricalLifecycleCoordinator)
    value._binding = binding
    value._audit = audit
    value._runtime = runtime
    value._matcher = matcher
    value._fact_authority = fact_authority
    value._failing_ack = None
    value._failing_key = None
    value._failing_payload = None
    value._resolver = evidence_resolver
    value._authorization = authorization
    value._authorization_capability = authorization_capability
    value._active = None
    value._pre_terminal_state = None
    value._terminal_ack = None
    value._terminal_outcome = None
    value._terminal_state = None
    value._mutation_lock = Lock()
    return value


def _admitted_state(
    binding: RunBinding,
    prepared_chain_head: Sha256Digest,
) -> CoordinatorRunState:
    return create_coordinator_run_state(
        binding=binding,
        state_version=1,
        phase=CoordinatorPhase.ADMITTED,
        active_dispatch_sequence=None,
        active_trigger_root_sha256=None,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(
            ORDERED_INGRESS_DIGEST_DOMAIN,
            (),
        ),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=None,
        last_completed_dispatch_sequence=None,
        last_audit_chain_head_sha256=prepared_chain_head,
    )


def _require_recovery_records(
    binding: RunBinding,
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    *,
    audit: AuditAppendPort | None,
) -> Iterator[tuple[AuditRecord, AuditAppendAcknowledgement]]:
    expected_count = _recovery_record_count(records)
    if expected_count < 1:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "recovery records must be non-empty")
    previous_record_sha256 = None
    previous_chain_head_sha256 = None
    observed_count = 0
    for sequence, record in enumerate(records, start=1):
        observed_count = sequence
        if type(record) is not AuditRecord or record.binding != binding:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery record binding conflicts")
        if record.record_id.owner_sequence != sequence:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery record sequence conflicts")
        if sequence > 1 and (
            record.previous_record_sha256 != previous_record_sha256
            or record.previous_chain_head_sha256 != previous_chain_head_sha256
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery audit chain conflicts")
        if audit is None:
            acknowledgement = create_audit_append_acknowledgement(record)
        else:
            try:
                acknowledgement = audit.append(
                    record_kind=record.record_kind,
                    subject_kind=record.subject_kind,
                    subject_sha256=record.subject_sha256,
                    canonical_payload=record.canonical_payload,
                )
                require_audit_acknowledgement(
                    acknowledgement,
                    binding=binding,
                    logical_key=record.logical_key,
                    canonical_payload=record.canonical_payload,
                )
            except AuditContractError as error:
                raise LifecycleError(
                    error.code,
                    "recovery audit acknowledgement could not be reconfirmed",
                ) from error
        if sequence == 1 and (
            record.record_kind is not AuditRecordKind.RUN_PREPARED
            or record.canonical_payload != canonical_run_prepared_audit_payload(binding)
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery preparation record conflicts",
            )
        if record.record_kind is AuditRecordKind.RUN_TERMINAL:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "terminal journal must use read-only terminal recovery",
            )
        yield record, acknowledgement
        previous_record_sha256 = audit_record_digest(record)
        previous_chain_head_sha256 = audit_chain_head(record)
    if observed_count != expected_count:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery record source count changed",
        )


def _recovery_record_count(
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
) -> int:
    if isinstance(records, tuple):
        return len(records)
    try:
        count = records.record_count
        binding = records.binding
    except AttributeError as error:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "recovery records require one repeatable source",
        ) from error
    if type(count) is not int or count < 1 or type(binding) is not RunBinding:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "recovery record source is invalid")
    return count


def _recovery_record_at(
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    index: int,
) -> AuditRecord:
    if isinstance(records, tuple):
        return records[index]
    try:
        return records.record_at(index)
    except (AttributeError, AuditContractError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery record could not be resolved",
        ) from error


def _recovery_record_prefix(
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    record_count: int,
) -> AuditRecoveryRecordSource | tuple[AuditRecord, ...]:
    if isinstance(records, tuple):
        return records[:record_count]
    try:
        return records.prefix(record_count)
    except (AttributeError, AuditContractError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery record prefix could not be resolved",
        ) from error


def _record_document(record: AuditRecord) -> dict[str, object]:
    try:
        document = json.loads(record.canonical_payload)
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery payload is not JSON") from error
    if type(document) is not dict:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery payload must be one object")
    return document


def _group_recovery_records(
    records: Iterator[tuple[AuditRecord, AuditAppendAcknowledgement]],
) -> Iterator[_RecoveredDispatch]:
    current: _RecoveredDispatch | None = None
    for position, (record, acknowledgement) in enumerate(records, start=2):
        kind = record.record_kind
        document = _record_document(record)
        sequence_value = document.get(
            "runtime_dispatch_sequence"
            if kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME
            else "dispatch_sequence"
        )
        if type(sequence_value) is not int or not 1 <= sequence_value <= _MAX_UINT64:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery dispatch sequence is invalid",
            )
        if current is None:
            current = _RecoveredDispatch(sequence_value)
        elif sequence_value != current.sequence:
            if sequence_value != current.sequence + 1 or current.completion_record is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery dispatches are physically interleaved",
                )
            _require_recovery_stage_order(current)
            yield current
            current = _RecoveredDispatch(sequence_value)
        group = current
        if kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
            if group.authorization_records:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "duplicate recovered authorization",
                )
            group.authorization_records.append((position, record, acknowledgement))
            continue
        trigger_value = document.get("trigger_root_sha256")
        if trigger_value is not None:
            try:
                trigger_sha256 = Sha256Digest(cast(str, trigger_value))
            except (TypeError, ValueError) as error:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery trigger digest is invalid",
                ) from error
            if group.trigger_sha256 is not None and group.trigger_sha256 != trigger_sha256:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery trigger digest conflicts",
                )
            group.trigger_sha256 = trigger_sha256
        retained = (position, record, acknowledgement)
        if kind is AuditRecordKind.MATCHER_DISPATCH_BATCH:
            if group.batch_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered batch")
            group.batch_record = retained
        elif kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME:
            if record.subject_sha256 in group.outcome_records:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered outcome")
            group.outcome_records[record.subject_sha256] = retained
        elif kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION:
            if group.failing_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate failing transition")
            group.failing_record = retained
        elif kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED:
            if group.completion_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered completion")
            group.completion_record = retained
        else:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "unsupported recovery record kind")
    if current is not None:
        _require_recovery_stage_order(current)
        yield current


def _require_recovery_stage_order(group: _RecoveredDispatch) -> None:
    positions = [
        position
        for entry in (
            group.batch_record,
            group.failing_record,
            group.completion_record,
        )
        if entry is not None
        for position in (entry[0],)
    ]
    positions.extend(entry[0] for entry in group.outcome_records.values())
    positions.extend(entry[0] for entry in group.authorization_records)
    if not positions:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery dispatch is empty")
    if group.authorization_records:
        authorization_position = group.authorization_records[0][0]
        inbound_positions = [
            entry[0]
            for entry in (group.batch_record, *group.outcome_records.values())
            if entry is not None
        ]
        if group.batch_record is None or any(
            position >= authorization_position for position in inbound_positions
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery authorization stage order conflicts",
            )
    completion = group.completion_record
    if completion is not None:
        completion_position = completion[0]
        required_before_completion = [
            entry[0]
            for entry in (group.batch_record, *group.outcome_records.values())
            if entry is not None
        ]
        required_before_completion.extend(entry[0] for entry in group.authorization_records)
        if group.batch_record is None or any(
            position >= completion_position for position in required_before_completion
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery completion stage order conflicts",
            )


def _require_runtime_trace(
    runtime: RuntimeLifecyclePort,
    binding: RunBinding,
) -> dict[int, tuple[dict[str, object], Sha256Digest]]:
    try:
        records = runtime.trace_records
        digest = runtime.trace_digest
    except (AttributeError, TypeError) as error:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "runtime trace evidence is incomplete",
        ) from error
    if type(records) is not tuple or digest != historical_runtime_trace_digest(records):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace digest conflicts")
    result: dict[int, tuple[dict[str, object], Sha256Digest]] = {}
    for canonical_record in records:
        try:
            document = json.loads(canonical_record)
            canonical = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            sequence = document["dispatch_sequence"]
            root = document["root"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "runtime trace record is invalid",
            ) from error
        if (
            type(document) is not dict
            or canonical != canonical_record
            or document.get("schema") != HISTORICAL_RUNTIME_TRACE_SCHEMA
            or document.get("run_id") != binding.reference.run_id.value
            or type(sequence) is not int
            or sequence in result
            or type(root) is not dict
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace record conflicts")
        root_bytes = json.dumps(
            root,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if (
            set(root)
            == {
                "available_at",
                "kind",
                "producer_namespace",
                "producer_sequence",
                "run_id",
                "type",
            }
            and root.get("type") == "end_of_run"
        ):
            domain = HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN
        elif set(root) == {
            "adjustment",
            "available_at",
            "close_bits",
            "high_bits",
            "interval_end",
            "interval_start",
            "kind",
            "low_bits",
            "open_bits",
            "revision",
            "source",
            "source_sequence",
            "symbol",
            "venue",
            "volume_bits",
        }:
            domain = HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN
        else:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace root kind is invalid")
        root_sha256 = Sha256Digest(
            sha256(domain + len(root_bytes).to_bytes(8, "big") + root_bytes).hexdigest()
        )
        result[sequence] = (document, root_sha256)
    if set(result) != set(range(1, len(result) + 1)):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace sequence is not contiguous")
    return result


def _recover_dispatch(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    recovered: _RecoveredDispatch,
    trace_by_sequence: dict[int, tuple[dict[str, object], Sha256Digest]],
) -> None:
    if coordinator._active is not None or coordinator._pre_terminal_state is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery dispatch follows open state")
    runtime_lease = coordinator._runtime.active_lease
    trigger_sha256 = recovered.trigger_sha256
    if trigger_sha256 is None and runtime_lease is not None:
        if runtime_lease.dispatch_sequence != recovered.sequence:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active recovery sequence conflicts")
        trigger_sha256 = _root_digest(runtime_lease.root)
    if trigger_sha256 is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery trigger evidence is missing")
    batch = coordinator._matcher.resolve_dispatch_batch(
        dispatch_sequence=recovered.sequence,
        trigger_root_sha256=trigger_sha256,
    )
    if batch is None and (
        recovered.batch_record is not None
        or recovered.outcome_records
        or recovered.completion_record is not None
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "matcher recovery evidence is missing")
    if batch is not None and (
        batch.dispatch_sequence != recovered.sequence or batch.trigger_root_sha256 != trigger_sha256
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "matcher recovery evidence conflicts")
    is_end = (batch is not None and batch.dispatch_kind is HistoricalDispatchKind.END_OF_RUN) or (
        runtime_lease is not None and type(runtime_lease.root) is EndOfRunRoot
    )
    state_before = coordinator._state
    coordinator._state = _recovered_capture_state(
        state_before,
        sequence=recovered.sequence,
        trigger_sha256=trigger_sha256,
        is_end=is_end,
    )
    lease: RuntimeDispatchLeaseView
    if runtime_lease is not None and runtime_lease.dispatch_sequence == recovered.sequence:
        if _root_digest(runtime_lease.root) != trigger_sha256:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active recovery root conflicts")
        lease = runtime_lease
    else:
        lease = _RecoveredLease(cast(RuntimeRoot, object()), recovered.sequence)
    active = _ActiveDispatch(lease=lease, trigger_sha256=trigger_sha256, batch=batch)
    if batch is None:
        if runtime_lease is not lease:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "unresolved matcher has no active lease",
            )
        _recover_pre_batch_failing_transition(coordinator, active, recovered.failing_record)
        coordinator._active = active
        return

    batch_entry = recovered.batch_record
    failing_entry = recovered.failing_record
    pre_batch_failure = (
        failing_entry is not None
        and (batch_entry is None or failing_entry[0] < batch_entry[0])
        and _is_pre_batch_failing_record(failing_entry[1], active.trigger_sha256)
    )
    if pre_batch_failure:
        _recover_pre_batch_failing_transition(coordinator, active, failing_entry)
    if batch_entry is not None:
        _batch_position, batch_record, batch_ack = batch_entry
        expected_batch_payload = canonical_matcher_batch_audit_payload(coordinator._binding, batch)
        if batch_record.canonical_payload != expected_batch_payload:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered batch payload conflicts")
        active.batch_ack = batch_ack
    count = len(batch.ingresses)
    active.outcomes = [None] * count
    active.outcome_acks = [None] * count
    active.handoffs = [None] * count
    matched_outcome_digests: set[Sha256Digest] = set()
    for index, ingress in enumerate(batch.ingresses):
        outcome = coordinator._fact_authority.resolve_processing_outcome(
            ingress_identity=ingress.identity,
            ingress_sha256=execution_fact_ingress_digest(ingress),
        )
        if outcome is not None and type(outcome) is not ExecutionFactProcessingOutcome:
            raise LifecycleError(OutcomeCode.INVALID_TYPE, "fact recovery returned invalid outcome")
        active.outcomes[index] = outcome
        if outcome is None:
            continue
        outcome_digest = execution_fact_processing_outcome_digest(outcome)
        outcome_entry = recovered.outcome_records.get(outcome_digest)
        if outcome_entry is None:
            continue
        _position, outcome_record, outcome_ack = outcome_entry
        if outcome_record.canonical_payload != canonical_execution_fact_processing_outcome_bytes(
            outcome
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered outcome payload conflicts")
        active.outcome_acks[index] = outcome_ack
        matched_outcome_digests.add(outcome_digest)
    if matched_outcome_digests != set(recovered.outcome_records):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "orphan recovered outcome record")
    _recover_authorization_frontier(coordinator, active, recovered)
    if not pre_batch_failure:
        _recover_failing_transition(coordinator, active, recovered)
    for index, outcome in enumerate(active.outcomes):
        acknowledgement = active.outcome_acks[index]
        if outcome is not None and acknowledgement is not None and active.batch_ack is not None:
            coordinator._require_evidence(outcome)
            active.handoffs[index] = create_audited_execution_fact_handoff(
                outcome=outcome,
                batch_acknowledgement=active.batch_ack,
                outcome_acknowledgement=acknowledgement,
            )
    completion_entry = recovered.completion_record
    if completion_entry is None:
        if runtime_lease is not lease:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "incomplete dispatch has no active lease",
            )
        coordinator._active = active
        return
    if (
        active.batch_ack is None
        or any(value is None for value in active.outcomes)
        or any(value is None for value in active.outcome_acks)
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion lacks authoritative evidence")
    outcome_acks = tuple(value for value in active.outcome_acks if value is not None)
    expected_completion_payload = active.completion_payload
    if expected_completion_payload is None:
        pre_ack_state = coordinator._pre_ack_state(active, outcome_acks)
        expected_completion_payload = canonical_dispatch_completed_audit_payload(
            binding=coordinator._binding,
            batch=batch,
            outcome_acknowledgements=outcome_acks,
            pre_ack_state_sha256=coordinator_run_state_digest(pre_ack_state),
            authorization_attempt_outcome=active.authorization_attempt,
            submission_receipts=(
                () if active.submission_receipt is None else (active.submission_receipt,)
            ),
        )
        active.completion_payload = expected_completion_payload
    _completion_position, completion_record, completion_ack = completion_entry
    if completion_record.canonical_payload != expected_completion_payload:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered completion payload conflicts")
    active.completion_ack = completion_ack
    trace_entry = trace_by_sequence.get(recovered.sequence)
    if trace_entry is None:
        if runtime_lease is not lease:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "completion/runtime acknowledgement conflicts",
            )
        coordinator._active = active
        return
    trace_document, trace_root_sha256 = trace_entry
    if trace_root_sha256 != trigger_sha256 or trace_document.get(
        "root_order_key"
    ) != _trace_root_order_key_document(batch.trigger_root_key):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace root conflicts")
    if runtime_lease is not None and runtime_lease.dispatch_sequence == recovered.sequence:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime retained an acknowledged lease")
    resulting_state = _recovered_completed_state(
        coordinator._state,
        active,
        completion_ack,
        failing_ack=coordinator._failing_ack,
        is_end=is_end,
    )
    coordinator._state = resulting_state
    if is_end:
        if trace_document.get("terminal_acknowledged") is not True or (
            coordinator._runtime.terminal_acknowledged is not True
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal runtime trace conflicts")
        coordinator._pre_terminal_state = create_pre_terminal_coordinator_state(
            binding=coordinator._binding,
            state_version=resulting_state.state_version + 1,
            terminal_kind=(
                CoordinatorTerminalKind.SUCCESS
                if resulting_state.failure_code is None
                else CoordinatorTerminalKind.FAILED
            ),
            last_dispatch_sequence=recovered.sequence,
            last_trigger_root_sha256=trigger_sha256,
            dispatch_completion_ack_sha256=audit_append_acknowledgement_digest(completion_ack),
            previous_chain_head_sha256=resulting_state.last_audit_chain_head_sha256,
            failure_code=resulting_state.failure_code,
        )
    elif trace_document.get("terminal_acknowledged") is not False:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "market trace claims terminal state")


def _recover_authorization_frontier(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    recovered: _RecoveredDispatch,
) -> None:
    completion_attempt: SubmissionAuthorizationAttemptOutcome | None = None
    if recovered.completion_record is not None:
        completion_document = _record_document(recovered.completion_record[1])
        attempt_count = completion_document.get("authorization_attempt_count")
        nested_attempt = completion_document.get("authorization_attempt_outcome")
        if attempt_count == 1:
            completion_attempt = decode_submission_authorization_attempt_outcome_document(
                nested_attempt
            )
            if (
                completion_attempt.binding != coordinator._binding
                or completion_attempt.dispatch_sequence != active.lease.dispatch_sequence
                or completion_attempt.trigger_root_sha256 != active.trigger_sha256
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization frontier conflicts",
                )
        elif attempt_count != 0 or nested_attempt is not None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "completion authorization frontier is invalid",
            )
    if not recovered.authorization_records:
        if completion_attempt is not None:
            if (
                completion_attempt.status
                in {
                    SubmissionAuthorizationAttemptStatus.AUTHORIZED,
                    SubmissionAuthorizationAttemptStatus.BURNED,
                }
                or completion_attempt.acknowledgement_sha256 is not None
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization record is missing",
                )
            active.authorization_attempt = completion_attempt
        return
    authorization = coordinator._authorization
    if authorization is None or len(recovered.authorization_records) != 1:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered authorization authority is unavailable",
        )
    _position, record, record_acknowledgement = recovered.authorization_records[0]
    document = _record_document(record)
    order_document = document.get("order_id")
    try:
        if type(order_document) is not dict:
            raise TypeError("order identity is not an object")
        order_id = EconomicId(
            coordinator._binding.reference.run_id,
            EconomicOwnerKind(order_document["owner_kind"]),
            order_document["owner_sequence"],
        )
        request_sha256 = Sha256Digest(cast(str, document["execution_request_sha256"]))
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered authorization identity is invalid",
        ) from error
    order = authorization.resolve_attempt_order(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    attempt = authorization.resolve_attempt(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    acknowledgement = authorization.resolve_attempt_acknowledgement(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    if (
        type(order) is not Order
        or type(attempt) is not SubmissionAuthorizationAttemptOutcome
        or type(acknowledgement) is not AuditAppendAcknowledgement
        or audit_append_acknowledgement_digest(acknowledgement)
        != audit_append_acknowledgement_digest(record_acknowledgement)
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered authorization evidence conflicts",
        )
    receipt = coordinator._matcher.resolve_submission_receipt(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    if receipt is not None and type(receipt) is not HistoricalSubmissionReceipt:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "recovered matcher receipt is invalid",
        )
    if completion_attempt is not None:
        if (
            completion_attempt.order_id != order_id
            or completion_attempt.execution_request_sha256 != request_sha256
            or completion_attempt.acknowledgement_sha256
            != audit_append_acknowledgement_digest(record_acknowledgement)
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "completion authorization frontier conflicts",
            )
        attempt = completion_attempt
    elif recovered.completion_record is not None:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "completion authorization attempt is missing",
        )
    elif receipt is not None:
        attempt = create_submission_authorization_attempt_outcome(
            binding=coordinator._binding,
            dispatch_sequence=active.lease.dispatch_sequence,
            trigger_root_sha256=active.trigger_sha256,
            order_id=order_id,
            execution_request_sha256=request_sha256,
            authorization_payload_sha256=record_acknowledgement.payload_sha256,
            status=SubmissionAuthorizationAttemptStatus.AUTHORIZED,
            logical_key=record.logical_key,
            acknowledgement_sha256=audit_append_acknowledgement_digest(record_acknowledgement),
            error_code=None,
        )
    if (
        receipt is not None
        and receipt.audit_acknowledgement_sha256
        != audit_append_acknowledgement_digest(record_acknowledgement)
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered matcher receipt authorization conflicts",
        )
    active.authorization_order = order
    active.authorization_ack = acknowledgement
    active.authorization_attempt = attempt
    active.submission_receipt = receipt


def _recover_failing_transition(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    recovered: _RecoveredDispatch,
) -> None:
    entry = recovered.failing_record
    if entry is None:
        return
    if coordinator._state.phase is CoordinatorPhase.FAILING:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failing transition is not monotone")
    position, record, acknowledgement = entry
    document = _record_document(record)
    try:
        failure_code = OutcomeCode(cast(str, document["failure_code"]))
        failed_key = AuditLogicalKey(
            AuditRecordKind(cast(str, document["failed_record_kind"])),
            AuditSubjectKind(cast(str, document["failed_subject_kind"])),
            Sha256Digest(cast(str, document["failed_subject_sha256"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "failing recovery key is invalid",
        ) from error
    logical_positions: list[tuple[AuditLogicalKey, int | None]] = []
    if active.batch is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failing recovery batch is missing")
    batch_key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        historical_matcher_dispatch_batch_digest(active.batch),
    )
    logical_positions.append(
        (batch_key, None if recovered.batch_record is None else recovered.batch_record[0])
    )
    acknowledgement_by_key: dict[AuditLogicalKey, AuditAppendAcknowledgement] = {}
    if recovered.batch_record is not None and recovered.batch_record[0] < position:
        acknowledgement_by_key[batch_key] = recovered.batch_record[2]
    for outcome in active.outcomes:
        if outcome is None:
            continue
        digest = execution_fact_processing_outcome_digest(outcome)
        key = AuditLogicalKey(
            AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            digest,
        )
        outcome_entry = recovered.outcome_records.get(digest)
        outcome_position = None if outcome_entry is None else outcome_entry[0]
        logical_positions.append((key, outcome_position))
        if outcome_entry is not None and outcome_entry[0] < position:
            acknowledgement_by_key[key] = outcome_entry[2]
    if failed_key.record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
        if failed_key.subject_kind is not AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "failed authorization key conflicts",
            )
        attempt = active.authorization_attempt
        if attempt is not None and (
            attempt.status is not SubmissionAuthorizationAttemptStatus.FAILED
            or attempt.logical_key != failed_key
            or attempt.acknowledgement_sha256 is not None
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "failed authorization frontier conflicts",
            )
        logical_positions.append((failed_key, None))
    if failed_key.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED:
        if active.batch_ack is None or any(value is None for value in active.outcome_acks):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "failed completion has incomplete inbound evidence",
            )
        ordered_outcome_acks = tuple(value for value in active.outcome_acks if value is not None)
        pre_ack_state = coordinator._pre_ack_state(active, ordered_outcome_acks)
        active.completion_payload = canonical_dispatch_completed_audit_payload(
            binding=coordinator._binding,
            batch=active.batch,
            outcome_acknowledgements=ordered_outcome_acks,
            pre_ack_state_sha256=coordinator_run_state_digest(pre_ack_state),
            authorization_attempt_outcome=active.authorization_attempt,
            submission_receipts=(
                () if active.submission_receipt is None else (active.submission_receipt,)
            ),
        )
        completion_key = AuditLogicalKey(
            AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
            AuditSubjectKind.RUNTIME_DISPATCH,
            dispatch_completed_subject_digest(active.completion_payload),
        )
        if completion_key != failed_key:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failed completion key conflicts")
        logical_positions.append(
            (
                completion_key,
                None if recovered.completion_record is None else recovered.completion_record[0],
            )
        )
        if recovered.completion_record is not None and recovered.completion_record[0] < position:
            acknowledgement_by_key[completion_key] = recovered.completion_record[2]
    baseline_missing = tuple(
        key
        for key, record_position in logical_positions
        if record_position is None or record_position > position or key == failed_key
    )
    previous_state = coordinator._state
    missing_candidates = [baseline_missing]
    if failed_key in acknowledgement_by_key:
        missing_candidates.append(tuple(key for key in baseline_missing if key != failed_key))
    matched: tuple[CoordinatorRunState, bytes] | None = None
    for missing in missing_candidates:
        acknowledged = tuple(
            acknowledgement
            for key, acknowledgement in acknowledgement_by_key.items()
            if key not in missing
        )
        outcome_acknowledgements = tuple(
            acknowledgement_by_key[key]
            for outcome in active.outcomes
            if outcome is not None
            for key in (
                AuditLogicalKey(
                    AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                    AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                    execution_fact_processing_outcome_digest(outcome),
                ),
            )
            if key in acknowledgement_by_key and key not in missing
        )
        candidate_state = create_coordinator_run_state(
            binding=coordinator._binding,
            state_version=previous_state.state_version + 1,
            phase=CoordinatorPhase.FAILING,
            active_dispatch_sequence=active.lease.dispatch_sequence,
            active_trigger_root_sha256=active.trigger_sha256,
            matcher_batch_sha256=historical_matcher_dispatch_batch_digest(active.batch),
            ordered_ingress_sha256s_sha256=ordered_digest_tuple(
                ORDERED_INGRESS_DIGEST_DOMAIN,
                active.batch.ingress_sha256s,
            ),
            ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                tuple(
                    audit_append_acknowledgement_digest(candidate)
                    for candidate in outcome_acknowledgements
                ),
            ),
            missing_audit_logical_keys=missing,
            failure_code=previous_state.failure_code or failure_code,
            last_completed_dispatch_sequence=previous_state.last_completed_dispatch_sequence,
            last_audit_chain_head_sha256=(
                previous_state.last_audit_chain_head_sha256
                if not acknowledged
                else max(
                    acknowledged,
                    key=lambda candidate: candidate.record_id.owner_sequence,
                ).chain_head_sha256
            ),
        )
        candidate_payload = canonical_failing_safety_audit_payload(
            binding=coordinator._binding,
            previous_state_sha256=coordinator_run_state_digest(previous_state),
            failing_state_sha256=coordinator_run_state_digest(candidate_state),
            failure_code=candidate_state.failure_code or failure_code,
            failed_logical_key=failed_key,
            dispatch_sequence=active.lease.dispatch_sequence,
            trigger_root_sha256=active.trigger_sha256,
        )
        if record.canonical_payload == candidate_payload:
            if matched is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "failing recovery state is ambiguous",
                )
            matched = (candidate_state, candidate_payload)
    if matched is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failing recovery payload conflicts")
    failing_state, expected_payload = matched
    coordinator._state = failing_state
    coordinator._failing_payload = expected_payload
    coordinator._failing_key = record.logical_key
    coordinator._failing_ack = acknowledgement


def _recover_pre_batch_failing_transition(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    entry: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None,
) -> None:
    previous_state = coordinator._state
    failure_code = OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
    failed_key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        active.trigger_sha256,
    )
    record: AuditRecord | None = None
    acknowledgement: AuditAppendAcknowledgement | None = None
    if entry is not None:
        _position, record, acknowledgement = entry
        document = _record_document(record)
        try:
            failure_code = OutcomeCode(cast(str, document["failure_code"]))
            retained_key = AuditLogicalKey(
                AuditRecordKind(cast(str, document["failed_record_kind"])),
                AuditSubjectKind(cast(str, document["failed_subject_kind"])),
                Sha256Digest(cast(str, document["failed_subject_sha256"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "pre-batch failing recovery key is invalid",
            ) from error
        if retained_key != failed_key:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "pre-batch failing recovery key conflicts",
            )
    failing_state = create_coordinator_run_state(
        binding=coordinator._binding,
        state_version=previous_state.state_version + 1,
        phase=CoordinatorPhase.FAILING,
        active_dispatch_sequence=active.lease.dispatch_sequence,
        active_trigger_root_sha256=active.trigger_sha256,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(
            ORDERED_INGRESS_DIGEST_DOMAIN,
            (),
        ),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=previous_state.failure_code or failure_code,
        last_completed_dispatch_sequence=previous_state.last_completed_dispatch_sequence,
        last_audit_chain_head_sha256=previous_state.last_audit_chain_head_sha256,
    )
    payload = canonical_failing_safety_audit_payload(
        binding=coordinator._binding,
        previous_state_sha256=coordinator_run_state_digest(previous_state),
        failing_state_sha256=coordinator_run_state_digest(failing_state),
        failure_code=failing_state.failure_code or failure_code,
        failed_logical_key=failed_key,
        dispatch_sequence=active.lease.dispatch_sequence,
        trigger_root_sha256=active.trigger_sha256,
    )
    if record is not None and record.canonical_payload != payload:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "pre-batch failing recovery payload conflicts",
        )
    coordinator._state = failing_state
    coordinator._failing_payload = payload
    coordinator._failing_key = AuditLogicalKey(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION,
        AuditSubjectKind.COORDINATOR_STATE,
        audit_subject_digest(AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION, payload),
    )
    coordinator._failing_ack = acknowledgement


def _is_pre_batch_failing_record(record: AuditRecord, trigger_sha256: Sha256Digest) -> bool:
    document = _record_document(record)
    return (
        document.get("failed_record_kind") == AuditRecordKind.MATCHER_DISPATCH_BATCH.value
        and document.get("failed_subject_kind")
        == AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH.value
        and document.get("failed_subject_sha256") == trigger_sha256.value
    )


def _root_digest(root: object) -> Sha256Digest:
    if type(root) is MarketDataEnvelope:
        return historical_market_root_digest(root)
    if type(root) is EndOfRunRoot:
        return historical_end_root_digest(root)
    raise LifecycleError(OutcomeCode.INVALID_TYPE, "unsupported recovered runtime root")


def _trace_root_order_key_document(key: RuntimeRootOrderKey) -> list[str | int]:
    try:
        values = key.as_tuple()
    except (AttributeError, TypeError) as error:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "runtime root key is incomplete") from error
    document: list[str | int] = []
    for value in values:
        if type(value) is datetime:
            if value.tzinfo is None or value.utcoffset() is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime root key time is naive")
            document.append(value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        elif type(value) in (str, int):
            document.append(cast(str | int, value))
        else:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime root key value is invalid")
    return document


def _latest_active_chain_head(
    active: _ActiveDispatch,
    state: CoordinatorRunState,
) -> Sha256Digest:
    acknowledgements = tuple(
        acknowledgement
        for acknowledgement in (
            active.batch_ack,
            *active.outcome_acks,
            active.authorization_ack,
            active.completion_ack,
        )
        if acknowledgement is not None
    )
    if not acknowledgements:
        return state.last_audit_chain_head_sha256
    return max(
        acknowledgements,
        key=lambda acknowledgement: acknowledgement.record_id.owner_sequence,
    ).chain_head_sha256


def _recovered_capture_state(
    state: CoordinatorRunState,
    *,
    sequence: int,
    trigger_sha256: Sha256Digest,
    is_end: bool,
) -> CoordinatorRunState:
    if state.state_version == _MAX_UINT64:
        raise LifecycleError(OutcomeCode.OUT_OF_RANGE, "recovered state version overflow")
    return create_coordinator_run_state(
        binding=state.binding,
        state_version=state.state_version + 1,
        phase=(
            CoordinatorPhase.DRAINING
            if is_end
            else (
                CoordinatorPhase.FAILING
                if state.phase is CoordinatorPhase.FAILING
                else CoordinatorPhase.RUNNING
            )
        ),
        active_dispatch_sequence=sequence,
        active_trigger_root_sha256=trigger_sha256,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
        last_audit_chain_head_sha256=state.last_audit_chain_head_sha256,
    )


def _recovered_completed_state(
    state: CoordinatorRunState,
    active: _ActiveDispatch,
    completion_ack: AuditAppendAcknowledgement,
    *,
    failing_ack: AuditAppendAcknowledgement | None,
    is_end: bool,
) -> CoordinatorRunState:
    if state.state_version > _MAX_UINT64 - 2:
        raise LifecycleError(OutcomeCode.OUT_OF_RANGE, "recovered state version overflow")
    return create_coordinator_run_state(
        binding=state.binding,
        state_version=state.state_version + 2,
        phase=CoordinatorPhase.DRAINING if is_end else state.phase,
        active_dispatch_sequence=None,
        active_trigger_root_sha256=None,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=active.lease.dispatch_sequence,
        last_audit_chain_head_sha256=_latest_completion_chain_head(
            completion_ack,
            failing_ack,
        ),
    )


def _latest_completion_chain_head(
    completion: AuditAppendAcknowledgement,
    failing: AuditAppendAcknowledgement | None,
) -> Sha256Digest:
    candidates = (completion,) if failing is None else (completion, failing)
    return max(
        candidates,
        key=lambda acknowledgement: acknowledgement.record_id.owner_sequence,
    ).chain_head_sha256
