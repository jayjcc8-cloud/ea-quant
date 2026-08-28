"""Serialized Phase 1 historical lifecycle coordinator from Accepted ADR 0020."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Protocol, final

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
from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_messages import (
    ExecutionFactIngress,
    Fill,
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
    HistoricalMatcherDispatchBatch,
    HistoricalSubmissionReceipt,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_dispatch_batch_digest,
    historical_submission_receipt_digest,
)
from ea.core.ledger_integration import (
    LedgerHandoffAction,
    LedgerHandoffOutcome,
    PortfolioRiskRefresh,
    canonical_ledger_handoff_outcome_bytes,
    canonical_portfolio_risk_refresh_bytes,
    portfolio_risk_refresh_digest,
)
from ea.core.lifecycle import (
    ORDERED_INGRESS_DIGEST_DOMAIN,
    ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
    ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
    ORDERED_RECONCILIATION_FRONTIER_DIGEST_DOMAIN,
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
    LifecycleDispatchOutcome,
    LifecycleDispatchWindow,
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
    canonical_dispatch_completed_v3_audit_payload,
    canonical_failing_safety_audit_payload,
    canonical_matcher_batch_audit_payload,
    canonical_run_terminal_audit_payload,
    canonical_run_terminal_v2_audit_payload,
    coordinator_run_state_digest,
    create_audited_execution_fact_handoff,
    create_coordinator_dispatch_outcome,
    create_coordinator_run_state,
    create_coordinator_terminal_outcome,
    create_pre_terminal_coordinator_state,
    create_terminal_coordinator_state,
    dispatch_completed_subject_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    PortfolioSnapshot,
    open_reconciliation_aggregate_digest,
    portfolio_snapshot_digest,
)
from ea.core.reconciliation import (
    ReconciliationObservation,
    ReconciliationOutcome,
    canonical_reconciliation_outcome_bytes,
    reconciliation_observation_digest,
)
from ea.core.risk import RiskHaltReason, RiskPolicyId, RiskStateSnapshot, risk_state_snapshot_digest
from ea.core.run import RunBinding, RunId, Sha256Digest
from ea.core.runtime import (
    EndOfRunRoot,
    ReconciliationObservationRoot,
    RuntimeRoot,
    runtime_root_order_key,
)
from ea.runtime._coordinator_read_only import drive_read_only as _drive_read_only
from ea.runtime._coordinator_read_only import is_read_only_root as _is_read_only_root
from ea.runtime._coordinator_recovery import (
    _group_recovery_records as _group_recovery_records,
)
from ea.runtime._coordinator_recovery import (
    _is_pre_batch_failing_record as _is_pre_batch_failing_record,
)
from ea.runtime._coordinator_recovery import (
    _latest_active_chain_head as _latest_active_chain_head,
)
from ea.runtime._coordinator_recovery import (
    _latest_completion_chain_head as _latest_completion_chain_head,
)
from ea.runtime._coordinator_recovery import (
    _record_document as _record_document,
)
from ea.runtime._coordinator_recovery import (
    _recover_authorization_frontier as _recover_authorization_frontier,
)
from ea.runtime._coordinator_recovery import (
    _recover_dispatch as _recover_dispatch,
)
from ea.runtime._coordinator_recovery import (
    _recover_failing_transition as _recover_failing_transition,
)
from ea.runtime._coordinator_recovery import (
    _recover_ledger_frontier as _recover_ledger_frontier,
)
from ea.runtime._coordinator_recovery import (
    _recover_pre_batch_failing_transition as _recover_pre_batch_failing_transition,
)
from ea.runtime._coordinator_recovery import (
    _recovered_capture_state as _recovered_capture_state,
)
from ea.runtime._coordinator_recovery import (
    _recovered_completed_state as _recovered_completed_state,
)
from ea.runtime._coordinator_recovery import (
    _recovered_failed_logical_key as _recovered_failed_logical_key,
)
from ea.runtime._coordinator_recovery import (
    _recovery_record_at as _recovery_record_at,
)
from ea.runtime._coordinator_recovery import (
    _recovery_record_count as _recovery_record_count,
)
from ea.runtime._coordinator_recovery import (
    _recovery_record_prefix as _recovery_record_prefix,
)
from ea.runtime._coordinator_recovery import (
    _require_recovery_records as _require_recovery_records,
)
from ea.runtime._coordinator_recovery import (
    _require_recovery_stage_order as _require_recovery_stage_order,
)
from ea.runtime._coordinator_recovery import (
    _require_runtime_trace as _require_runtime_trace,
)
from ea.runtime._coordinator_recovery import (
    _root_digest as _root_digest,
)
from ea.runtime._coordinator_recovery import (
    _trace_root_order_key_document as _trace_root_order_key_document,
)


class _LedgerHandoffGatePort(Protocol):
    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    snapshot: PortfolioSnapshot
    retained_handoff_count: int

    def apply_handoff(
        self,
        *,
        handoff: AuditedExecutionFactHandoff,
        outcome: ExecutionFactProcessingOutcome,
        fill: Fill | None,
    ) -> LedgerHandoffOutcome: ...

    def resolve_handoff_outcome(
        self,
        handoff_sha256: Sha256Digest,
    ) -> LedgerHandoffOutcome | None: ...


class _RiskGatePort(Protocol):
    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    risk_state: RiskStateSnapshot

    def engage_halt(
        self,
        reason: RiskHaltReason,
        causal_root_available_at: datetime,
        dispatch_sequence: int,
    ) -> RiskStateSnapshot: ...


class _FrontierGatePort(Protocol):
    previous_refresh_sha256: Sha256Digest | None

    def advance(
        self,
        *,
        snapshot: PortfolioSnapshot,
        risk_state: RiskStateSnapshot,
        refresh: PortfolioRiskRefresh,
    ) -> None: ...

    def current_snapshot(self) -> PortfolioSnapshot: ...

    def current_state(self) -> RiskStateSnapshot: ...


class _RiskRefreshGatePort(Protocol):
    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    policy_id: RiskPolicyId
    policy_sha256: Sha256Digest
    next_sequence: int

    def resolve_refresh(
        self,
        *,
        dispatch_sequence: int,
        ordered_ledger_ack_frontier_sha256: Sha256Digest,
    ) -> PortfolioRiskRefresh | None: ...

    def create_refresh(
        self,
        *,
        snapshot: PortfolioSnapshot,
        risk_state: RiskStateSnapshot,
        dispatch_sequence: int,
        ordered_ledger_ack_frontier_sha256: Sha256Digest,
        coordinator_running: bool,
        publication_window_clear: bool,
        candidate_matches_internal: bool,
    ) -> PortfolioRiskRefresh: ...


class _ReadOnlyReconciliationAuthorityPort(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    def admit_observation(
        self, observation: ReconciliationObservation, *, dispatch_sequence: int
    ) -> ReconciliationOutcome: ...

    def resolve_outcome(self, observation: ReconciliationObservation) -> ReconciliationOutcome: ...


def _bound_ledger_gate(
    ledger: _LedgerHandoffGatePort | None,
    risk: _RiskGatePort | None,
    refresh: _RiskRefreshGatePort | None,
    frontier: _FrontierGatePort | None,
) -> (
    tuple[
        _LedgerHandoffGatePort,
        _RiskGatePort,
        _RiskRefreshGatePort,
        _FrontierGatePort,
    ]
    | None
):
    if ledger is None and risk is None and refresh is None and frontier is None:
        return None
    if ledger is None or risk is None or refresh is None or frontier is None:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "ledger, risk, refresh, and frontier must be bound together",
        )
    return ledger, risk, refresh, frontier


_MAX_UINT64 = (1 << 64) - 1
_FrontierState = tuple[Sha256Digest, Sha256Digest, Sha256Digest | None]


def _frontier_state(frontier: _FrontierGatePort) -> _FrontierState:
    return (
        portfolio_snapshot_digest(frontier.current_snapshot()),
        risk_state_snapshot_digest(frontier.current_state()),
        frontier.previous_refresh_sha256,
    )


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
    ledger_outcomes: list[LedgerHandoffOutcome | None] = field(default_factory=list)
    ledger_acks: list[AuditAppendAcknowledgement | None] = field(default_factory=list)
    ledger_snapshot: PortfolioSnapshot | None = None
    risk_state: RiskStateSnapshot | None = None
    refresh: PortfolioRiskRefresh | None = None
    refresh_ack: AuditAppendAcknowledgement | None = None
    refresh_sha256: Sha256Digest | None = None
    refresh_value_sha256: Sha256Digest | None = None
    prior_frontier: _FrontierState | None = None
    refresh_published: bool = False
    final_portfolio_snapshot_sha256: Sha256Digest | None = None
    final_risk_state_sha256: Sha256Digest | None = None
    read_only: object | None = None


@dataclass(slots=True)
class _RecoveredDispatch:
    sequence: int
    trigger_sha256: Sha256Digest | None = None
    read_only_outcome_record: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None = None
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
    ledger_records: list[tuple[int, AuditRecord, AuditAppendAcknowledgement]] = field(
        default_factory=list
    )
    refresh_record: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None = None


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
        "_final_refresh_value_sha256",
        "_frontier",
        "_ledger_handoff_authority",
        "_matcher",
        "_mutation_lock",
        "_resolver",
        "_risk_authority",
        "_reconciliation_authority",
        "_risk_refresh_authority",
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
    _final_refresh_value_sha256: Sha256Digest | None
    _frontier: _FrontierGatePort | None
    _ledger_handoff_authority: _LedgerHandoffGatePort | None
    _matcher: HistoricalMatcherPort
    _mutation_lock: Lock
    _resolver: ExecutionEvidenceResolverPort
    _risk_authority: _RiskGatePort | None
    _reconciliation_authority: _ReadOnlyReconciliationAuthorityPort | None
    _risk_refresh_authority: _RiskRefreshGatePort | None
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

    def begin_next_dispatch(self) -> LifecycleDispatchWindow:
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
            if _is_read_only_root(active.lease.root):
                return _drive_read_only(self, active, complete=False)
            return self._drive_to_window(active)
        finally:
            self._mutation_lock.release()

    def resume_active_dispatch(self) -> LifecycleDispatchWindow:
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
            if _is_read_only_root(active.lease.root):
                return _drive_read_only(self, active, complete=False)
            return self._drive_to_window(active)
        finally:
            self._mutation_lock.release()

    def complete_active_dispatch(
        self,
        window: LifecycleDispatchWindow,
    ) -> LifecycleDispatchOutcome:
        """Freeze and durably complete only the exact retained open window."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            if active is not None and _is_read_only_root(active.lease.root):
                if getattr(active.read_only, "window", None) is not window:
                    raise LifecycleError(
                        OutcomeCode.CONFLICTING_ID, "active dispatch window conflicts"
                    )
                return _drive_read_only(self, active, complete=True)
            if type(window) is not ActiveDispatchWindow:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active dispatch window conflicts")
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

    def retry_active_dispatch_completion(self) -> LifecycleDispatchOutcome:
        """Retry only a dispatch whose durable completion record already closed authorization."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            if active is not None and getattr(active.read_only, "completion_ack", None) is not None:
                return _drive_read_only(self, active, complete=True)
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

    def process_next_dispatch(self) -> LifecycleDispatchOutcome:
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
            if _is_read_only_root(active.lease.root):
                return _drive_read_only(self, active, complete=True)
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

    def retry_active_dispatch(self) -> LifecycleDispatchOutcome:
        """Replay only missing stages for the exact retained active lease."""
        if not self._mutation_lock.acquire(blocking=False):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "coordinator call is reentrant")
        try:
            active = self._active
            if active is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "no active dispatch is retained")
            if _is_read_only_root(active.lease.root):
                if getattr(active.read_only, "completion_ack", None) is None:
                    raise LifecycleError(
                        OutcomeCode.CONFLICTING_ID,
                        "read-only retry needs route ack",
                    )
                return _drive_read_only(self, active, complete=True)
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
        elif type(root) is ReconciliationObservationRoot:
            trigger_sha256 = root.observation_sha256
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

    def _require_read_only_outcome_authority(self, root: Any, outcome: Any, sequence: int) -> None:
        if (
            type(root) is not ReconciliationObservationRoot
            or root.observation.run_id != self._binding.reference.run_id
            or root.observation_sha256 != reconciliation_observation_digest(root.observation)
            or type(outcome) is not ReconciliationOutcome
            or outcome.run_id != self._binding.reference.run_id
            or outcome.dispatch_sequence != sequence
            or outcome.observation_sha256 != root.observation_sha256
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only recovery outcome conflicts")

    def _read_only_gate(
        self,
    ) -> tuple[_LedgerHandoffGatePort, _RiskGatePort, _RiskRefreshGatePort, _FrontierGatePort]:
        gate = _bound_ledger_gate(
            self._ledger_handoff_authority,
            self._risk_authority,
            self._risk_refresh_authority,
            self._frontier,
        )
        if gate is None or self._reconciliation_authority is None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID, "read-only reconciliation route is not bound"
            )
        return gate

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
                self._append_failing_safety()
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
            if self._ledger_handoff_authority is not None:
                try:
                    self._apply_ledger_gate(active)
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

    def _refresh_publication_window_clear(self, active: _ActiveDispatch) -> bool:
        batch = active.batch
        if batch is None:
            return False
        count = len(batch.ingresses)
        complete_frontiers = (
            active.outcomes,
            active.outcome_acks,
            active.handoffs,
            active.ledger_outcomes,
            active.ledger_acks,
        )
        return (
            active.batch_ack is not None
            and all(
                len(values) == count and all(value is not None for value in values)
                for values in complete_frontiers
            )
            and active.window is None
            and active.window_stage is None
            and active.authorization_order is None
            and active.authorization_ack is None
            and active.authorization_attempt is None
            and active.submission_receipt is None
            and active.completion_payload is None
            and active.completion_ack is None
            and (self._failing_payload is None or self._failing_ack is not None)
        )

    def _apply_ledger_gate(self, active: _ActiveDispatch) -> None:
        gate = _bound_ledger_gate(
            self._ledger_handoff_authority,
            self._risk_authority,
            self._risk_refresh_authority,
            self._frontier,
        )
        if gate is None:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "ledger gate is not bound")
        ledger, risk, refresh_authority, frontier = gate
        root = active.lease.root
        sequence = active.lease.dispatch_sequence
        if active.ledger_snapshot is None:
            active.ledger_snapshot = ledger.snapshot
        if active.risk_state is None:
            active.risk_state = risk.risk_state
        if getattr(active, "prior_frontier", None) is None:
            active.prior_frontier = _frontier_state(frontier)
        if not active.ledger_outcomes:
            active.ledger_outcomes = [None] * len(active.handoffs)
            active.ledger_acks = [None] * len(active.handoffs)
        halt_required = False
        for index, handoff in enumerate(active.handoffs):
            assert handoff is not None
            processing_outcome = active.outcomes[index]
            assert processing_outcome is not None
            ledger_outcome = active.ledger_outcomes[index]
            if ledger_outcome is None:
                fill = None
                if handoff.fill_id is not None:
                    assert handoff.fill_sha256 is not None
                    fill = self._resolver.resolve_fill(
                        fill_id=handoff.fill_id,
                        fill_sha256=handoff.fill_sha256,
                    )
                ledger_outcome = ledger.apply_handoff(
                    handoff=handoff,
                    outcome=processing_outcome,
                    fill=fill,
                )
                self._require_same_active_lease(active)
                active.ledger_outcomes[index] = ledger_outcome
                active.ledger_snapshot = ledger.snapshot
                self._rebind_active_authorities(active)
            if active.ledger_acks[index] is None:
                payload = canonical_ledger_handoff_outcome_bytes(ledger_outcome)
                acknowledgement = self._audit.append(
                    record_kind=AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                    subject_kind=AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                    subject_sha256=audit_subject_digest(
                        AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                        payload,
                    ),
                    canonical_payload=payload,
                )
                _require_exact_ack(
                    acknowledgement,
                    binding=self._binding,
                    key=AuditLogicalKey(
                        AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                        AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                        audit_subject_digest(
                            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                            payload,
                        ),
                    ),
                    payload=payload,
                )
                self._rebind_active_authorities(active)
                active.ledger_acks[index] = acknowledgement
            if (
                processing_outcome.halt_requested
                or ledger_outcome.requires_reconciliation
                or ledger_outcome.action is LedgerHandoffAction.FAILED
            ):
                halt_required = True
        if halt_required:
            active.risk_state = risk.engage_halt(
                RiskHaltReason.RECONCILIATION_REQUIRED,
                causal_root_available_at=_root_available_at(root),
                dispatch_sequence=sequence,
            )
            self._rebind_active_authorities(active)
        if active.refresh is None:
            ledger_acks = tuple(value for value in active.ledger_acks if value is not None)
            snapshot = ledger.snapshot
            risk_state = risk.risk_state
            active.ledger_snapshot = snapshot
            active.risk_state = risk_state
            refresh_frontier_sha256 = ordered_digest_tuple(
                ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
                tuple(audit_append_acknowledgement_digest(value) for value in ledger_acks),
            )
            refresh = refresh_authority.create_refresh(
                snapshot=snapshot,
                risk_state=risk_state,
                dispatch_sequence=sequence,
                ordered_ledger_ack_frontier_sha256=refresh_frontier_sha256,
                coordinator_running=self._state.phase is CoordinatorPhase.RUNNING,
                publication_window_clear=self._refresh_publication_window_clear(active),
                candidate_matches_internal=(
                    portfolio_snapshot_digest(snapshot)
                    == portfolio_snapshot_digest(ledger.snapshot)
                    and risk_state_snapshot_digest(risk_state)
                    == risk_state_snapshot_digest(risk.risk_state)
                ),
            )
            active.refresh = refresh
            refresh_payload = canonical_portfolio_risk_refresh_bytes(refresh)
            refresh_subject_sha256 = audit_subject_digest(
                AuditRecordKind.RISK_PORTFOLIO_REFRESH,
                refresh_payload,
            )
            active.refresh_sha256 = refresh_subject_sha256
            active.refresh_value_sha256 = portfolio_risk_refresh_digest(refresh)
        refresh = active.refresh
        snapshot = active.ledger_snapshot
        risk_state = active.risk_state
        if refresh is None or snapshot is None or risk_state is None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "retained refresh publication candidate is incomplete",
            )
        refresh_payload = canonical_portfolio_risk_refresh_bytes(refresh)
        refresh_subject_sha256 = audit_subject_digest(
            AuditRecordKind.RISK_PORTFOLIO_REFRESH,
            refresh_payload,
        )
        if active.refresh_ack is None:
            refresh_acknowledgement = self._audit.append(
                record_kind=AuditRecordKind.RISK_PORTFOLIO_REFRESH,
                subject_kind=AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
                subject_sha256=refresh_subject_sha256,
                canonical_payload=refresh_payload,
            )
            _require_exact_ack(
                refresh_acknowledgement,
                binding=self._binding,
                key=AuditLogicalKey(
                    AuditRecordKind.RISK_PORTFOLIO_REFRESH,
                    AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
                    refresh_subject_sha256,
                ),
                payload=refresh_payload,
            )
            self._rebind_active_authorities(active)
            active.refresh_ack = refresh_acknowledgement
        self._publish_retained_refresh(active, frontier)
        self._rebind_active_authorities(active)
        active.final_portfolio_snapshot_sha256 = portfolio_snapshot_digest(
            frontier.current_snapshot()
        )
        active.final_risk_state_sha256 = risk_state_snapshot_digest(frontier.current_state())

    def _publish_retained_refresh(
        self,
        active: _ActiveDispatch,
        frontier: _FrontierGatePort,
    ) -> None:
        refresh = active.refresh
        snapshot = active.ledger_snapshot
        risk_state = active.risk_state
        if getattr(active, "prior_frontier", None) is None:
            active.prior_frontier = _frontier_state(frontier)
        if (
            refresh is None
            or snapshot is None
            or risk_state is None
            or active.refresh_ack is None
            or active.prior_frontier is None
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "retained refresh publication evidence is incomplete",
            )
        expected = (
            portfolio_snapshot_digest(snapshot),
            risk_state_snapshot_digest(risk_state),
            portfolio_risk_refresh_digest(refresh),
        )
        current = _frontier_state(frontier)
        if current == expected:
            active.refresh_published = True
            return
        if current != active.prior_frontier:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "refresh publication frontier is neither exact old nor exact new",
            )
        try:
            frontier.advance(snapshot=snapshot, risk_state=risk_state, refresh=refresh)
        except BaseException as error:
            current = _frontier_state(frontier)
            if current == expected:
                active.refresh_published = True
                return
            if current != active.prior_frontier:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "refresh publication exception left an ambiguous frontier",
                ) from error
            raise
        if _frontier_state(frontier) != expected:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "refresh publication did not commit the exact candidate",
            )
        active.refresh_published = True

    def _resolve_read_only_outcome(
        self,
        root: ReconciliationObservationRoot,
        sequence: int,
        active: _ActiveDispatch | None = None,
    ) -> ReconciliationOutcome:
        authority = self._reconciliation_authority
        if authority is None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID, "read-only reconciliation authority is unavailable"
            )
        outcome = authority.admit_observation(root.observation, dispatch_sequence=sequence)
        if active is not None:
            self._require_same_active_lease(active)
        retained = authority.resolve_outcome(root.observation)
        retained_payload = canonical_reconciliation_outcome_bytes(retained)
        if retained_payload != canonical_reconciliation_outcome_bytes(outcome):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "read-only outcome conflicts")
        self._require_read_only_outcome_authority(root, retained, sequence)
        return retained

    def _completion_payload(
        self,
        active: _ActiveDispatch,
        outcome_acks: tuple[AuditAppendAcknowledgement, ...],
    ) -> bytes:
        batch = active.batch
        if batch is None:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion batch is missing")
        pre_ack_state_sha256 = coordinator_run_state_digest(
            self._pre_ack_state(active, outcome_acks)
        )
        receipts = () if active.submission_receipt is None else (active.submission_receipt,)
        if self._ledger_handoff_authority is None:
            return canonical_dispatch_completed_audit_payload(
                binding=self._binding,
                batch=batch,
                outcome_acknowledgements=outcome_acks,
                pre_ack_state_sha256=pre_ack_state_sha256,
                authorization_attempt_outcome=active.authorization_attempt,
                submission_receipts=receipts,
            )
        ledger_acks = tuple(value for value in active.ledger_acks if value is not None)
        if len(ledger_acks) != len(active.handoffs) or active.refresh_ack is None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "ledger and refresh frontier is incomplete",
            )
        if active.final_portfolio_snapshot_sha256 is None or active.final_risk_state_sha256 is None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "ledger final frontier digests are incomplete",
            )
        return canonical_dispatch_completed_v3_audit_payload(
            binding=self._binding,
            batch=batch,
            outcome_acknowledgements=outcome_acks,
            pre_ack_state_sha256=pre_ack_state_sha256,
            ledger_outcome_acknowledgements=ledger_acks,
            final_portfolio_snapshot_sha256=active.final_portfolio_snapshot_sha256,
            final_risk_state_sha256=active.final_risk_state_sha256,
            authorization_attempt_outcome=active.authorization_attempt,
            submission_receipts=receipts,
        )

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
                completion_payload = active.completion_payload
                if completion_payload is None:
                    completion_payload = self._completion_payload(active, outcome_acks)
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
            self._acknowledge_runtime(active)
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
            if (
                self._ledger_handoff_authority is not None
                and active.refresh_value_sha256 is not None
            ):
                self._final_refresh_value_sha256 = active.refresh_value_sha256
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
        ledger_gate = _bound_ledger_gate(
            self._ledger_handoff_authority,
            self._risk_authority,
            self._risk_refresh_authority,
            self._frontier,
        )
        if ledger_gate is not None:
            ledger, risk, _refresh, frontier = ledger_gate
            if portfolio_snapshot_digest(frontier.current_snapshot()) != portfolio_snapshot_digest(
                ledger.snapshot
            ) or risk_state_snapshot_digest(frontier.current_state()) != risk_state_snapshot_digest(
                risk.risk_state
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "internal and published frontiers diverge at terminalization",
                )
            if self._final_refresh_value_sha256 is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "terminalization requires the final risk refresh",
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

    def _acknowledge_runtime(self, active: _ActiveDispatch) -> None:
        try:
            self._runtime.acknowledge(active.lease)
        except Exception:
            if not self._confirm_committed_runtime_acknowledgement(active):
                raise

    def _confirm_committed_runtime_acknowledgement(
        self,
        active: _ActiveDispatch,
    ) -> bool:
        root = active.lease.root
        batch = active.batch
        if self._runtime.active_lease is not None:
            return False
        if type(root) is ReconciliationObservationRoot:
            root_order_key = _trace_root_order_key_document(runtime_root_order_key(root))
            is_terminal = False
        else:
            if batch is None or type(root) not in {MarketDataEnvelope, EndOfRunRoot}:
                return False
            root_order_key = _trace_root_order_key_document(batch.trigger_root_key)
            is_terminal = type(root) is EndOfRunRoot
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
        return (
            root_sha256 == active.trigger_sha256
            and document.get("root_order_key") == root_order_key
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
        payload = _terminal_payload(self, pre_terminal)
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
            terminal_payload=payload,
        )
        outcome = create_coordinator_terminal_outcome(
            pre_terminal_state=pre_terminal,
            terminal_acknowledgement=self._terminal_ack,
            terminal_state=terminal_state,
            terminal_payload=payload,
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
            self._rebind_economic_authorities(active)
            return
        self._require_batch(active, batch)
        for ingress, outcome in zip(batch.ingresses, active.outcomes, strict=False):
            if outcome is not None:
                self._require_authoritative_outcome(ingress, outcome)
                self._require_same_active_lease(active)
                self._require_evidence(outcome)
                self._require_same_active_lease(active)
        self._rebind_economic_authorities(active)

    def _rebind_economic_authorities(self, active: _ActiveDispatch) -> None:
        gate = _bound_ledger_gate(
            self._ledger_handoff_authority,
            self._risk_authority,
            self._risk_refresh_authority,
            self._frontier,
        )
        if gate is None:
            return
        ledger, risk, refresh_authority, frontier = gate
        for handoff, retained in zip(active.handoffs, active.ledger_outcomes, strict=False):
            if handoff is None or retained is None:
                continue
            resolved = ledger.resolve_handoff_outcome(
                audited_execution_fact_handoff_digest(handoff)
            )
            if resolved is None or canonical_ledger_handoff_outcome_bytes(resolved) != (
                canonical_ledger_handoff_outcome_bytes(retained)
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "ledger handoff outcome drifted",
                )
        if active.ledger_snapshot is not None and (
            portfolio_snapshot_digest(ledger.snapshot)
            != portfolio_snapshot_digest(active.ledger_snapshot)
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "ledger snapshot drifted")
        if active.risk_state is not None and (
            risk_state_snapshot_digest(risk.risk_state)
            != risk_state_snapshot_digest(active.risk_state)
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "risk state drifted")
        if active.refresh is not None:
            resolved_refresh = refresh_authority.resolve_refresh(
                dispatch_sequence=active.lease.dispatch_sequence,
                ordered_ledger_ack_frontier_sha256=(
                    active.refresh.ordered_ledger_ack_frontier_sha256
                ),
            )
            if resolved_refresh is None or canonical_portfolio_risk_refresh_bytes(
                resolved_refresh
            ) != canonical_portfolio_risk_refresh_bytes(active.refresh):
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "risk refresh drifted")
        if active.refresh_published:
            if (
                active.refresh is None
                or active.ledger_snapshot is None
                or active.risk_state is None
                or _frontier_state(frontier)
                != (
                    portfolio_snapshot_digest(active.ledger_snapshot),
                    risk_state_snapshot_digest(active.risk_state),
                    portfolio_risk_refresh_digest(active.refresh),
                )
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "published frontier drifted",
                )
        elif active.prior_frontier is not None and (
            _frontier_state(frontier) != active.prior_frontier
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "pending frontier drifted")

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
        for ledger_outcome, acknowledgement in zip(
            active.ledger_outcomes, active.ledger_acks, strict=True
        ):
            if ledger_outcome is not None and acknowledgement is None:
                payload = canonical_ledger_handoff_outcome_bytes(ledger_outcome)
                missing.append(
                    AuditLogicalKey(
                        AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                        AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                        audit_subject_digest(
                            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
                            payload,
                        ),
                    )
                )
        if (
            self._ledger_handoff_authority is not None
            and active.ledger_acks
            and active.refresh_ack is None
            and active.refresh_sha256 is not None
        ):
            missing.append(
                AuditLogicalKey(
                    AuditRecordKind.RISK_PORTFOLIO_REFRESH,
                    AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
                    active.refresh_sha256,
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
            last_audit_chain_head_sha256=_pre_ack_chain_head(active, outcome_acks),
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


_group_recovery_records.__globals__.update(
    {
        "Phase1HistoricalLifecycleCoordinator": Phase1HistoricalLifecycleCoordinator,
        "_ActiveDispatch": _ActiveDispatch,
        "_RecoveredDispatch": _RecoveredDispatch,
    }
)


def _pre_ack_chain_head(
    active: _ActiveDispatch,
    outcome_acks: tuple[AuditAppendAcknowledgement, ...],
) -> Sha256Digest:
    candidates = (
        *outcome_acks,
        *active.ledger_acks,
        active.refresh_ack,
        active.authorization_ack,
        active.batch_ack,
    )
    return max(
        (value for value in candidates if value is not None),
        key=lambda value: value.record_id.owner_sequence,
    ).chain_head_sha256


def _latest_ledger_ack_chain_head(active: _ActiveDispatch) -> Sha256Digest | None:
    for acknowledgement in reversed(active.ledger_acks):
        if acknowledgement is not None:
            return acknowledgement.chain_head_sha256
    return None


def _terminal_payload(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    pre_terminal: PreTerminalCoordinatorState,
) -> bytes:
    if coordinator._ledger_handoff_authority is None:
        return canonical_run_terminal_audit_payload(pre_terminal)
    if coordinator._final_refresh_value_sha256 is None:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "terminal evidence requires the final risk refresh",
        )
    snapshot = (
        coordinator._frontier.current_snapshot()
        if coordinator._frontier is not None
        else coordinator._ledger_handoff_authority.snapshot
    )
    return canonical_run_terminal_v2_audit_payload(
        pre_terminal,
        final_published_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        final_risk_refresh_sha256=coordinator._final_refresh_value_sha256,
        open_reconciliation_ref_aggregate_sha256=open_reconciliation_aggregate_digest(snapshot),
        ordered_reconciliation_frontier_sha256s_sha256=ordered_digest_tuple(
            ORDERED_RECONCILIATION_FRONTIER_DIGEST_DOMAIN,
            (),
        ),
    )


def _root_available_at(root: RuntimeRoot) -> datetime:
    if type(root) is MarketDataEnvelope:
        return root.available_at
    if type(root) is EndOfRunRoot:
        return root.available_at
    raise LifecycleError(OutcomeCode.INVALID_TYPE, "unsupported runtime root")


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
    ledger_handoff_authority: _LedgerHandoffGatePort | None = None,
    risk_authority: _RiskGatePort | None = None,
    risk_refresh_authority: _RiskRefreshGatePort | None = None,
    frontier: _FrontierGatePort | None = None,
    reconciliation_authority: _ReadOnlyReconciliationAuthorityPort | None = None,
) -> Phase1HistoricalLifecycleCoordinator:
    if type(binding) is not RunBinding:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "binding must be exact")
    if (authorization is None) != (authorization_capability is None):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "authorization port and capability must be bound together",
        )
    ledger_gate = _bound_ledger_gate(
        ledger_handoff_authority,
        risk_authority,
        risk_refresh_authority,
        frontier,
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
    if ledger_gate is not None:
        ledger_handoff_authority, risk_authority, risk_refresh_authority, frontier = ledger_gate
        _require_ledger_authority_bindings(
            binding=binding,
            matcher=matcher,
            ledger_handoff_authority=ledger_handoff_authority,
            risk_authority=risk_authority,
            risk_refresh_authority=risk_refresh_authority,
        )
        _require_fresh_economic_authorities(
            ledger_handoff_authority=ledger_handoff_authority,
            risk_authority=risk_authority,
            frontier=frontier,
        )
    if reconciliation_authority is not None and (
        ledger_gate is None
        or reconciliation_authority.run_id != binding.reference.run_id
        or instrument_spec_set_digest(reconciliation_authority.spec_set) != spec_digests[0]
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID, "reconciliation authority bindings conflict"
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
        ledger_handoff_authority=ledger_handoff_authority,
        risk_authority=risk_authority,
        risk_refresh_authority=risk_refresh_authority,
        frontier=frontier,
        reconciliation_authority=reconciliation_authority,
    )
    value._state = _admitted_state(binding, prepared_acknowledgement.chain_head_sha256)
    return value


def _require_ledger_authority_bindings(
    *,
    binding: RunBinding,
    matcher: HistoricalMatcherPort,
    ledger_handoff_authority: _LedgerHandoffGatePort,
    risk_authority: _RiskGatePort,
    risk_refresh_authority: _RiskRefreshGatePort,
) -> None:
    run_id = binding.reference.run_id
    if (
        ledger_handoff_authority.run_id != run_id
        or risk_authority.run_id != run_id
        or risk_refresh_authority.run_id != run_id
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "ledger authority runs conflict")
    spec_digest = instrument_spec_set_digest(matcher.spec_set)
    if (
        instrument_spec_set_digest(ledger_handoff_authority.spec_set) != spec_digest
        or instrument_spec_set_digest(risk_authority.spec_set) != spec_digest
        or instrument_spec_set_digest(risk_refresh_authority.spec_set) != spec_digest
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "ledger authority specification bindings conflict",
        )
    risk_state = risk_authority.risk_state
    if (
        risk_state.policy_id != risk_refresh_authority.policy_id
        or risk_state.policy_sha256 != risk_refresh_authority.policy_sha256
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "risk refresh policy binding conflicts",
        )


def _require_fresh_economic_authorities(
    *,
    ledger_handoff_authority: _LedgerHandoffGatePort,
    risk_authority: _RiskGatePort,
    frontier: _FrontierGatePort,
) -> None:
    snapshot = ledger_handoff_authority.snapshot
    risk_state = risk_authority.risk_state
    if (
        snapshot.snapshot_version != 0
        or ledger_handoff_authority.retained_handoff_count != 0
        or risk_state.risk_state_version != 0
        or risk_state.halted
        or risk_state.halt_reason is not None
        or risk_state.halt_causal_root_available_at is not None
        or risk_state.halt_dispatch_sequence is not None
        or risk_state.conflict_existing_intent_sha256 is not None
        or risk_state.conflict_submitted_intent_sha256 is not None
        or portfolio_snapshot_digest(frontier.current_snapshot())
        != portfolio_snapshot_digest(snapshot)
        or risk_state_snapshot_digest(frontier.current_state())
        != risk_state_snapshot_digest(risk_state)
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "coordinator requires fresh-empty economic authorities",
        )


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
    ledger_handoff_authority: _LedgerHandoffGatePort | None = None,
    risk_authority: _RiskGatePort | None = None,
    risk_refresh_authority: _RiskRefreshGatePort | None = None,
    frontier: _FrontierGatePort | None = None,
    reconciliation_authority: _ReadOnlyReconciliationAuthorityPort | None = None,
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
    ledger_gate = _bound_ledger_gate(
        ledger_handoff_authority,
        risk_authority,
        risk_refresh_authority,
        frontier,
    )
    if ledger_gate is not None:
        ledger_handoff_authority, risk_authority, risk_refresh_authority, frontier = ledger_gate
        _require_ledger_authority_bindings(
            binding=binding,
            matcher=matcher,
            ledger_handoff_authority=ledger_handoff_authority,
            risk_authority=risk_authority,
            risk_refresh_authority=risk_refresh_authority,
        )
        _require_fresh_economic_authorities(
            ledger_handoff_authority=ledger_handoff_authority,
            risk_authority=risk_authority,
            frontier=frontier,
        )
        if (
            risk_refresh_authority.next_sequence != 1
            or frontier.previous_refresh_sha256 is not None
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "full-journal recovery requires fresh-empty economic authorities and an "
                "unseeded refresh frontier",
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
        ledger_handoff_authority=ledger_handoff_authority,
        risk_authority=risk_authority,
        risk_refresh_authority=risk_refresh_authority,
        frontier=frontier,
        reconciliation_authority=reconciliation_authority,
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
    ledger_handoff_authority: _LedgerHandoffGatePort | None = None,
    risk_authority: _RiskGatePort | None = None,
    risk_refresh_authority: _RiskRefreshGatePort | None = None,
    frontier: _FrontierGatePort | None = None,
    reconciliation_authority: _ReadOnlyReconciliationAuthorityPort | None = None,
) -> RecoveredTerminalCoordinatorEvidence:
    """Reconstruct a closed run without issuing any mutation authority."""
    _bound_ledger_gate(
        ledger_handoff_authority,
        risk_authority,
        risk_refresh_authority,
        frontier,
    )
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
        ledger_handoff_authority=ledger_handoff_authority,
        risk_authority=risk_authority,
        risk_refresh_authority=risk_refresh_authority,
        frontier=frontier,
        reconciliation_authority=reconciliation_authority,
    )
    pre_terminal = coordinator.pre_terminal_state
    if pre_terminal is None or coordinator.terminal_state is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal prefix is not pre-terminal")
    payload = _terminal_payload(coordinator, pre_terminal)
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
        terminal_payload=payload,
    )
    terminal_outcome = create_coordinator_terminal_outcome(
        pre_terminal_state=pre_terminal,
        terminal_acknowledgement=terminal_ack,
        terminal_state=terminal_state,
        terminal_payload=payload,
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
    ledger_handoff_authority: _LedgerHandoffGatePort | None = None,
    risk_authority: _RiskGatePort | None = None,
    risk_refresh_authority: _RiskRefreshGatePort | None = None,
    frontier: _FrontierGatePort | None = None,
    reconciliation_authority: _ReadOnlyReconciliationAuthorityPort | None = None,
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
    value._final_refresh_value_sha256 = None
    value._frontier = frontier
    value._ledger_handoff_authority = ledger_handoff_authority
    value._risk_authority = risk_authority
    value._reconciliation_authority = reconciliation_authority
    value._risk_refresh_authority = risk_refresh_authority
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
