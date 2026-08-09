"""Serialized Phase 1 historical lifecycle coordinator from Accepted ADR 0020."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass, field
from threading import Lock
from typing import final

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditAppendPort,
    AuditContractError,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_subject_digest,
    canonical_run_prepared_audit_payload,
    ordered_digest_tuple,
)
from ea.core.execution import instrument_spec_set_digest
from ea.core.execution_messages import Order
from ea.core.execution_state import (
    ExecutionFactProcessingOutcome,
    canonical_execution_fact_processing_outcome_bytes,
    execution_fact_processing_outcome_digest,
)
from ea.core.historical_matching import (
    HistoricalMatcherDispatchBatch,
    canonical_end_of_run_root_bytes,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_dispatch_batch_digest,
)
from ea.core.lifecycle import (
    ORDERED_INGRESS_DIGEST_DOMAIN,
    ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
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
    SubmissionAuthorizationPreparationPort,
    TerminalCoordinatorState,
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
    create_terminal_coordinator_state,
    dispatch_completed_subject_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, Sha256Digest
from ea.core.runtime import EndOfRunRoot
from ea.runtime.historical import historical_runtime_trace_digest


@dataclass(slots=True)
class _ActiveDispatch:
    lease: RuntimeDispatchLeaseView
    trigger_sha256: Sha256Digest
    batch: HistoricalMatcherDispatchBatch | None = None
    batch_ack: AuditAppendAcknowledgement | None = None
    outcomes: list[ExecutionFactProcessingOutcome | None] = field(default_factory=list)
    outcome_acks: list[AuditAppendAcknowledgement | None] = field(default_factory=list)
    handoffs: list[AuditedExecutionFactHandoff | None] = field(default_factory=list)
    completion_ack: AuditAppendAcknowledgement | None = None


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
            self._require_same_active_lease(active)
            return self._drive_active(active)
        finally:
            self._mutation_lock.release()

    def prepare_submission_authorization(
        self,
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
            active = self._active
            if authorization is None:
                raise LifecycleError(
                    OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                    "authorization authority is not bound",
                )
            if (
                self._state.phase in (CoordinatorPhase.FAILING, CoordinatorPhase.DRAINING)
                or active is None
                or active.lease.root is not causal_market_root
                or active.lease.dispatch_sequence != dispatch_sequence
            ):
                raise LifecycleError(
                    OutcomeCode.RISK_STALE_APPROVAL,
                    "authorization requires the exact active market dispatch",
                )
            return authorization.prepare(
                order,
                causal_market_root=causal_market_root,
                dispatch_sequence=dispatch_sequence,
                capability=self._authorization_capability,
            )
        finally:
            self._mutation_lock.release()

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
                    active.batch_ack = self._audit.append(
                        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
                        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
                        subject_sha256=batch_digest,
                        canonical_payload=batch_payload,
                    )
                    self._require_same_active_lease(active)
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
                        active.outcomes[index] = processing_outcome
                    outcome_payload = canonical_execution_fact_processing_outcome_bytes(
                        processing_outcome
                    )
                    outcome_digest = execution_fact_processing_outcome_digest(processing_outcome)
                    acknowledgement = active.outcome_acks[index]
                    if acknowledgement is None:
                        acknowledgement = self._audit.append(
                            record_kind=AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                            subject_kind=AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                            subject_sha256=outcome_digest,
                            canonical_payload=outcome_payload,
                        )
                        active.outcome_acks[index] = acknowledgement
                        self._require_same_active_lease(active)
                    if active.handoffs[index] is None and active.batch_ack is not None:
                        self._require_evidence(processing_outcome)
                        self._require_same_active_lease(active)
                        active.handoffs[index] = create_audited_execution_fact_handoff(
                            outcome=processing_outcome,
                            batch_acknowledgement=active.batch_ack,
                            outcome_acknowledgement=acknowledgement,
                        )
                except Exception as error:
                    first_error = first_error or error
            missing = self._missing_keys(active)
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
            pre_ack_state = self._pre_ack_state(active, outcome_acks)
            completion_payload = canonical_dispatch_completed_audit_payload(
                binding=self._binding,
                batch=batch,
                outcome_acknowledgements=outcome_acks,
                pre_ack_state_sha256=coordinator_run_state_digest(pre_ack_state),
            )
            if active.completion_ack is None:
                active.completion_ack = self._audit.append(
                    record_kind=AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
                    subject_kind=AuditSubjectKind.RUNTIME_DISPATCH,
                    subject_sha256=dispatch_completed_subject_digest(completion_payload),
                    canonical_payload=completion_payload,
                )
                self._require_same_active_lease(active)
            self._require_same_active_lease(active)
            try:
                self._runtime.acknowledge(active.lease)
            except Exception:
                if type(root) is not EndOfRunRoot or not self._confirm_committed_terminal(active):
                    raise
            if type(root) is EndOfRunRoot and not self._confirm_committed_terminal(active):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime terminal acknowledgement evidence conflicts",
                )
            resulting_state = self._completed_state(active, outcome_acks)
            batch_ack = active.batch_ack
            completion_ack = active.completion_ack
            if batch_ack is None or completion_ack is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "dispatch evidence is incomplete")
            dispatch_outcome = create_coordinator_dispatch_outcome(
                batch=batch,
                batch_acknowledgement=batch_ack,
                handoffs=handoffs,
                dispatch_completion_acknowledgement=completion_ack,
                runtime_acknowledged=True,
                resulting_state=resulting_state,
            )
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
            previous_chain_head_sha256=completion.chain_head_sha256,
            failure_code=resulting_state.failure_code,
        )

    def _confirm_committed_terminal(self, active: _ActiveDispatch) -> bool:
        root = active.lease.root
        if (
            type(root) is not EndOfRunRoot
            or self._runtime.active_lease is not None
            or self._runtime.terminal_acknowledged is not True
        ):
            return False
        records = self._runtime.trace_records
        if (
            type(records) is not tuple
            or not records
            or any(type(record) is not bytes for record in records)
            or self._runtime.trace_digest != historical_runtime_trace_digest(records)
        ):
            return False
        try:
            document = json.loads(records[-1])
            canonical = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            root_document = json.loads(canonical_end_of_run_root_bytes(root))
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        return (
            type(document) is dict
            and canonical == records[-1]
            and document.get("run_id") == self._binding.reference.run_id.value
            and document.get("dispatch_sequence") == active.lease.dispatch_sequence
            and document.get("terminal_acknowledged") is True
            and document.get("root") == root_document
        )

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
            self._terminal_ack = self._audit.append(
                record_kind=AuditRecordKind.RUN_TERMINAL,
                subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
                subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
                canonical_payload=payload,
            )
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
            last_audit_chain_head_sha256=state.last_audit_chain_head_sha256,
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
        self._failing_ack = self._audit.append(
            record_kind=key.record_kind,
            subject_kind=key.subject_kind,
            subject_sha256=key.subject_sha256,
            canonical_payload=payload,
        )

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
                active.batch_ack.chain_head_sha256
                if not outcome_acks and active.batch_ack is not None
                else outcome_acks[-1].chain_head_sha256
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
            last_audit_chain_head_sha256=completion.chain_head_sha256,
        )


def create_phase1_lifecycle_coordinator(
    *,
    binding: RunBinding,
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
    prepared_ack = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
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
    value._state = create_coordinator_run_state(
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
        last_audit_chain_head_sha256=prepared_ack.chain_head_sha256,
    )
    return value
