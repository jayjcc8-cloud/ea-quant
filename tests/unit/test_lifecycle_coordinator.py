from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ea.runtime.coordinator as coordinator_module
from ea.core.audit import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditContractError,
    AuditLogicalKey,
    AuditRecord,
    AuditRecordKind,
    AuditSubjectKind,
    audit_chain_head,
    audit_record_digest,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import Fill, create_fill
from ea.core.execution_state import (
    ExecutionFactAction,
    ExecutionFactAnomaly,
    ExecutionFactProcessingOutcome,
    OrderProjectionSnapshot,
    OrderResolutionKeyKind,
    create_execution_fact_processing_outcome,
    create_order_resolution_binding,
)
from ea.core.historical_matching import canonical_end_of_run_root_bytes
from ea.core.lifecycle import CoordinatorPhase, LifecycleError
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.core.runtime import runtime_root_order_key
from ea.runtime.coordinator import (
    create_phase1_lifecycle_coordinator as _create_phase1_lifecycle_coordinator,
)
from ea.runtime.coordinator import (
    recover_phase1_lifecycle_coordinator,
    recover_phase1_terminal_evidence,
)
from ea.runtime.historical import (
    HISTORICAL_RUNTIME_TRACE_SCHEMA,
    historical_runtime_trace_digest,
)
from unit.test_historical_matcher import _system


class _MemoryAudit:
    def __init__(self, binding: RunBinding) -> None:
        self.binding = binding
        self.records: list[AuditRecord] = []
        self.index: dict[AuditLogicalKey, tuple[bytes, AuditAppendAcknowledgement]] = {}
        self.retry_keys: list[AuditLogicalKey] = []

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
        existing = self.index.get(key)
        if existing is not None:
            assert existing[0] == canonical_payload
            self.retry_keys.append(key)
            return existing[1]
        record = create_audit_record(
            binding=self.binding,
            owner_sequence=len(self.records) + 1,
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
            previous_record_sha256=(
                EMPTY_RECORD_SHA256 if not self.records else audit_record_digest(self.records[-1])
            ),
            previous_chain_head_sha256=(
                EMPTY_CHAIN_HEAD_SHA256 if not self.records else audit_chain_head(self.records[-1])
            ),
        )
        acknowledgement = create_audit_append_acknowledgement(record)
        self.records.append(record)
        self.index[key] = (canonical_payload, acknowledgement)
        return acknowledgement

    def resolve_record(self, logical_key: AuditLogicalKey) -> AuditRecord | None:
        return next(
            (record for record in self.records if record.logical_key == logical_key),
            None,
        )


def create_phase1_lifecycle_coordinator(
    *,
    binding: RunBinding,
    audit: _MemoryAudit,
    **ports: Any,
) -> Any:
    prepared_acknowledgement = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    return _create_phase1_lifecycle_coordinator(
        binding=binding,
        prepared_acknowledgement=prepared_acknowledgement,
        audit=audit,
        **ports,
    )


class _RejectRecoveredFrameAudit(_MemoryAudit):
    reject_retries = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
        if self.reject_retries and key in self.index:
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                "injected recovered frame acknowledgement failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


@dataclass(frozen=True, slots=True)
class _Lease:
    root: Any
    dispatch_sequence: int


class _Runtime:
    def __init__(self, matcher: Any, root: Any, *, dispatch_sequence: int = 1) -> None:
        self.run_id = matcher.run_id
        self.spec_set = matcher.spec_set
        self._lease = _Lease(root, dispatch_sequence)
        self.trace_records: tuple[bytes, ...] = ()
        self.trace_digest = historical_runtime_trace_digest(())
        self.terminal_acknowledged = False

    @property
    def active_lease(self) -> _Lease | None:
        return self._lease if self._popped else None

    def pop(self) -> _Lease:
        assert not self._popped
        self._popped = True
        return self._lease

    def acknowledge(self, lease: Any) -> None:
        assert lease is self._lease
        self._popped = False
        if type(lease.root).__name__ == "EndOfRunRoot":
            self.terminal_acknowledged = True
            root_document = json.loads(canonical_end_of_run_root_bytes(lease.root))
            root_order_key = []
            for value in runtime_root_order_key(lease.root).as_tuple():
                root_order_key.append(
                    value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    if type(value) is datetime
                    else value
                )
            document = {
                "clock_now": root_document["available_at"],
                "committed_cursor_as_of": None,
                "committed_event_count": 0,
                "data_sha256": "00" * 32,
                "dispatch_sequence": lease.dispatch_sequence,
                "root": root_document,
                "root_order_key": root_order_key,
                "run_id": self.run_id.value,
                "schema": HISTORICAL_RUNTIME_TRACE_SCHEMA,
                "terminal_acknowledged": True,
            }
            record = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            self.trace_records = (record,)
            self.trace_digest = historical_runtime_trace_digest(self.trace_records)

    _popped = False


class _NoFacts:
    def __init__(self, matcher: Any) -> None:
        self.run_id = matcher.run_id
        self.spec_set = matcher.spec_set

    def process_ingress(self, ingress: Any) -> ExecutionFactProcessingOutcome:
        raise AssertionError("empty matcher batch must not process an ingress")

    def resolve_processing_outcome(
        self,
        *,
        ingress_identity: Any,
        ingress_sha256: Any,
    ) -> None:
        return None


class _CommittedThenRaisedRuntime(_Runtime):
    def acknowledge(self, lease: Any) -> None:
        super().acknowledge(lease)
        raise RuntimeError("injected post-commit return failure")


class _CommittedMarketThenRaisedRuntime(_Runtime):
    acknowledgement_calls = 0

    def acknowledge(self, lease: Any) -> None:
        self.acknowledgement_calls += 1
        assert lease is self._lease
        self._popped = False
        root_document = json.loads(canonical_market_data_record_bytes(lease.root))
        root_order_key = []
        for value in runtime_root_order_key(lease.root).as_tuple():
            root_order_key.append(
                value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                if type(value) is datetime
                else value
            )
        document = {
            "clock_now": root_document["available_at"],
            "committed_cursor_as_of": root_document["available_at"],
            "committed_event_count": 1,
            "data_sha256": "00" * 32,
            "dispatch_sequence": lease.dispatch_sequence,
            "root": root_document,
            "root_order_key": root_order_key,
            "run_id": self.run_id.value,
            "schema": HISTORICAL_RUNTIME_TRACE_SCHEMA,
            "terminal_acknowledged": False,
        }
        record = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.trace_records = (record,)
        self.trace_digest = historical_runtime_trace_digest(self.trace_records)
        raise RuntimeError("injected committed market acknowledgement failure")


class _TracingMarketRuntime(_CommittedMarketThenRaisedRuntime):
    def acknowledge(self, lease: Any) -> None:
        try:
            super().acknowledge(lease)
        except RuntimeError as error:
            if str(error) != "injected committed market acknowledgement failure":
                raise


class _FailFirstRuntimeAcknowledgement(_Runtime):
    failed = False
    acknowledgement_calls = 0

    def acknowledge(self, lease: Any) -> None:
        self.acknowledgement_calls += 1
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected pre-commit acknowledgement failure")
        super().acknowledge(lease)


class _ResolverOnlyMatcher:
    def __init__(self, matcher: Any) -> None:
        self._matcher = matcher
        self.mutation_calls = 0

    @property
    def run_id(self) -> Any:
        return self._matcher.run_id

    @property
    def spec_set(self) -> Any:
        return self._matcher.spec_set

    @property
    def source_namespace(self) -> Any:
        return self._matcher.source_namespace

    def resolve_dispatch_batch(self, **values: Any) -> Any:
        return self._matcher.resolve_dispatch_batch(**values)

    def resolve_submission_receipt(self, **values: Any) -> Any:
        return self._matcher.resolve_submission_receipt(**values)

    def submit(
        self,
        order: Any,
        *,
        causal_market_root: Any,
        dispatch_sequence: int,
    ) -> Any:
        self.mutation_calls += 1
        return self._matcher.submit(
            order,
            causal_market_root=causal_market_root,
            dispatch_sequence=dispatch_sequence,
        )

    def match_active_market_root(self, root: Any, *, dispatch_sequence: int) -> Any:
        self.mutation_calls += 1
        return self._matcher.match_active_market_root(
            root,
            dispatch_sequence=dispatch_sequence,
        )

    def expire_at_active_end(self, root: Any, *, dispatch_sequence: int) -> Any:
        self.mutation_calls += 1
        return self._matcher.expire_at_active_end(
            root,
            dispatch_sequence=dispatch_sequence,
        )


class _FailFirstMatcher(_ResolverOnlyMatcher):
    def match_active_market_root(self, root: Any, *, dispatch_sequence: int) -> Any:
        self.mutation_calls += 1
        if self.mutation_calls == 1:
            raise RuntimeError("injected matcher pre-publication failure")
        return self._matcher.match_active_market_root(
            root,
            dispatch_sequence=dispatch_sequence,
        )


class _NoEvidence:
    def resolve_fill(self, *, fill_id: Any, fill_sha256: Any) -> None:
        return None

    def resolve_projection_after(
        self,
        *,
        order_id: Any,
        projection_sha256: Any,
    ) -> None:
        return None


class _RetainedOutcomeFacts:
    def __init__(self, matcher: Any, outcome: ExecutionFactProcessingOutcome) -> None:
        self.run_id = matcher.run_id
        self.spec_set = matcher.spec_set
        self.outcome = outcome

    def process_ingress(self, ingress: Any) -> ExecutionFactProcessingOutcome:
        assert ingress.identity == self.outcome.ingress_identity
        return self.outcome

    def resolve_processing_outcome(
        self,
        *,
        ingress_identity: Any,
        ingress_sha256: Any,
    ) -> ExecutionFactProcessingOutcome | None:
        if (
            ingress_identity == self.outcome.ingress_identity
            and ingress_sha256 == self.outcome.ingress_sha256
        ):
            return self.outcome
        return None


class _DriftingFillEvidence:
    available = True

    def __init__(self, fill: Fill) -> None:
        self.fill = fill

    def resolve_fill(self, *, fill_id: Any, fill_sha256: Any) -> Fill | None:
        del fill_id, fill_sha256
        return self.fill if self.available else None

    def resolve_projection_after(
        self,
        *,
        order_id: Any,
        projection_sha256: Any,
    ) -> OrderProjectionSnapshot | None:
        del order_id, projection_sha256
        return None


class _DriftFillOnCompletionAudit(_MemoryAudit):
    def __init__(self, binding: RunBinding, evidence: _DriftingFillEvidence) -> None:
        super().__init__(binding)
        self.evidence = evidence
        self.drifted = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        acknowledgement = super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )
        if record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED and not self.drifted:
            self.drifted = True
            self.evidence.available = False
        return acknowledgement


class _FailFirstBatchAudit(_MemoryAudit):
    failed = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        if record_kind is AuditRecordKind.MATCHER_DISPATCH_BATCH and not self.failed:
            self.failed = True
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected batch append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


class _FailFirstFailingAudit(_MemoryAudit):
    failed = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        if record_kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION and not self.failed:
            self.failed = True
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected failing-safety append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


class _FailFirstTerminalAudit(_MemoryAudit):
    failed = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        if record_kind is AuditRecordKind.RUN_TERMINAL and not self.failed:
            self.failed = True
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected terminal append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


class _WrongAckOnceAudit(_MemoryAudit):
    def __init__(self, binding: RunBinding, target: AuditRecordKind) -> None:
        super().__init__(binding)
        self.target = target
        self.returned_wrong_ack = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        if record_kind is self.target and not self.returned_wrong_ack:
            self.returned_wrong_ack = True
            return create_audit_append_acknowledgement(self.records[0])
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


class _WrongFailingAckAfterBatchFailureAudit(_MemoryAudit):
    batch_failed = False
    failing_ack_mismatched = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        if record_kind is AuditRecordKind.MATCHER_DISPATCH_BATCH and not self.batch_failed:
            self.batch_failed = True
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected batch append failure",
            )
        if (
            record_kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
            and not self.failing_ack_mismatched
        ):
            self.failing_ack_mismatched = True
            return create_audit_append_acknowledgement(self.records[0])
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


class _CountingFacts(_NoFacts):
    calls = 0

    def process_ingress(self, ingress: Any) -> ExecutionFactProcessingOutcome:
        self.calls += 1
        raise RuntimeError("injected fact processing failure")


def test_empty_market_dispatch_is_audited_before_runtime_acknowledgement() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _Runtime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    outcome = coordinator.process_next_dispatch()

    assert outcome.dispatch_sequence == 1
    assert outcome.handoffs == ()
    assert outcome.runtime_acknowledged is True
    assert coordinator.state.phase is CoordinatorPhase.RUNNING
    assert coordinator.state.last_completed_dispatch_sequence == 1
    assert [record.record_kind for record in audit.records] == [
        AuditRecordKind.RUN_PREPARED,
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
    ]


def test_staged_market_dispatch_retains_one_window_before_completion() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _Runtime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    window = coordinator.begin_next_dispatch()

    assert coordinator.resume_active_dispatch() is window
    assert window.authorization_allowed is True
    assert runtime.active_lease is not None
    assert [record.record_kind for record in audit.records] == [
        AuditRecordKind.RUN_PREPARED,
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
    ]
    with pytest.raises(LifecycleError, match="active dispatch"):
        coordinator.begin_next_dispatch()

    outcome = coordinator.complete_active_dispatch(window)

    assert outcome.runtime_acknowledged is True
    assert runtime.active_lease is None
    assert [record.record_kind for record in audit.records] == [
        AuditRecordKind.RUN_PREPARED,
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
    ]
    with pytest.raises(LifecycleError, match="window conflicts"):
        coordinator.complete_active_dispatch(window)


def test_fresh_construction_consumes_preverified_prepared_ack_without_append() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    foreign_binding = RunBinding(
        binding.reference,
        Sha256Digest("33" * 32),
    )
    foreign_audit = _MemoryAudit(foreign_binding)
    foreign_ack = foreign_audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=foreign_binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(foreign_binding),
    )

    with pytest.raises(LifecycleError, match="acknowledgement conflicts"):
        _create_phase1_lifecycle_coordinator(
            binding=binding,
            prepared_acknowledgement=foreign_ack,
            audit=audit,
            runtime=_Runtime(matcher, delayed),
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_NoEvidence(),
        )

    assert audit.records == []


def test_recovery_rejects_completion_physically_before_required_batch() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    original = _MemoryAudit(binding)
    runtime = _Runtime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=original,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    coordinator.process_next_dispatch()
    prepared, batch, completion = original.records
    reordered = _MemoryAudit(binding)
    for record in (prepared, completion, batch):
        reordered.append(
            record_kind=record.record_kind,
            subject_kind=record.subject_kind,
            subject_sha256=record.subject_sha256,
            canonical_payload=record.canonical_payload,
        )

    with pytest.raises(LifecycleError, match="completion stage order"):
        recover_phase1_lifecycle_coordinator(
            binding=binding,
            audit=reordered,
            runtime=runtime,
            matcher=_ResolverOnlyMatcher(matcher),
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_NoEvidence(),
            records=tuple(reordered.records),
        )


@pytest.mark.parametrize(
    "target",
    (
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
    ),
)
def test_wrong_live_ack_is_not_retained_and_exact_retry_can_complete(
    target: AuditRecordKind,
) -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _WrongAckOnceAudit(binding, target)
    runtime = _Runtime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    with pytest.raises(LifecycleError, match="acknowledgement conflicts"):
        coordinator.process_next_dispatch()
    assert runtime.active_lease is not None

    outcome = coordinator.retry_active_dispatch()

    assert outcome.runtime_acknowledged is True
    assert runtime.active_lease is None
    assert [record.record_kind for record in audit.records].count(target) == 1


def test_wrong_failing_ack_is_not_retained_and_retry_reconfirms_it() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _WrongFailingAckAfterBatchFailureAudit(binding)
    runtime = _Runtime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    with pytest.raises(AuditContractError, match="injected batch"):
        coordinator.process_next_dispatch()
    assert coordinator.state.phase is CoordinatorPhase.FAILING
    assert AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION not in (
        record.record_kind for record in audit.records
    )

    outcome = coordinator.retry_active_dispatch()

    assert outcome.runtime_acknowledged is True
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
    ) == 1


def test_completion_callback_fill_drift_stops_before_runtime_acknowledgement() -> None:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    ingress = batch.ingresses[0]
    fact = ingress.fact
    key_kinds = tuple(
        kind
        for present, kind in (
            (fact.order_id is not None, OrderResolutionKeyKind.ORDER_ID),
            (
                fact.client_submission_key is not None,
                OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY,
            ),
            (fact.venue_order_id is not None, OrderResolutionKeyKind.VENUE_ORDER_ID),
        )
        if present
    )
    fill = create_fill(
        fill_id=EconomicId(matcher.run_id, EconomicOwnerKind.EXECUTION_FILL, 1),
        fact=fact,
        spec_set=matcher.spec_set,
    )
    processing_outcome = create_execution_fact_processing_outcome(
        run_id=matcher.run_id,
        runtime_dispatch_sequence=8,
        ingress=ingress,
        action=ExecutionFactAction.UNRESOLVED,
        anomalies=(ExecutionFactAnomaly.UNKNOWN_ORDER,),
        order_resolutions=tuple(
            create_order_resolution_binding(key_kind=kind, resolved_order=None)
            for kind in key_kinds
        ),
        resolved_order=None,
        fill=fill,
        projection_before=None,
        projection_after=None,
    )
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    evidence = _DriftingFillEvidence(fill)
    audit = _DriftFillOnCompletionAudit(binding, evidence)
    runtime = _Runtime(matcher, delayed, dispatch_sequence=8)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, processing_outcome),
        evidence_resolver=evidence,
    )

    with pytest.raises(LifecycleError, match="Fill evidence is unresolved"):
        coordinator.process_next_dispatch()
    assert runtime.active_lease is not None
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ) == 1

    evidence.available = True
    dispatch = coordinator.retry_active_dispatch()

    assert dispatch.runtime_acknowledged is True
    assert runtime.active_lease is None


def test_bounded_end_closes_with_one_exact_terminal_record() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _Runtime(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    dispatch = coordinator.process_next_dispatch()
    terminal = coordinator.terminal_outcome

    assert dispatch.runtime_acknowledged is True
    assert terminal is not None
    assert coordinator.retry_terminalization() is terminal
    assert coordinator.pre_terminal_state is not None
    assert coordinator.terminal_state is not None
    assert [record.record_kind for record in audit.records] == [
        AuditRecordKind.RUN_PREPARED,
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
        AuditRecordKind.RUN_TERMINAL,
    ]


def test_terminal_after_runtime_retry_binds_latest_failing_chain_head() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _FailFirstRuntimeAcknowledgement(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    with pytest.raises(RuntimeError, match="pre-commit"):
        coordinator.process_next_dispatch()
    failing_record = audit.records[-1]
    assert failing_record.record_kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION

    dispatch = coordinator.retry_active_dispatch()
    pre_terminal = coordinator.pre_terminal_state

    assert dispatch.runtime_acknowledged is True
    assert pre_terminal is not None
    assert coordinator.state.last_audit_chain_head_sha256 == audit_chain_head(failing_record)
    assert pre_terminal.previous_chain_head_sha256 == audit_chain_head(failing_record)
    assert audit.records[-1].record_kind is AuditRecordKind.RUN_TERMINAL


def test_wrong_terminal_ack_is_not_retained_and_terminal_retry_can_complete() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _WrongAckOnceAudit(binding, AuditRecordKind.RUN_TERMINAL)
    runtime = _Runtime(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    with pytest.raises(LifecycleError, match="acknowledgement conflicts"):
        coordinator.process_next_dispatch()
    assert coordinator.pre_terminal_state is not None
    assert coordinator.terminal_outcome is None

    terminal = coordinator.retry_terminalization()

    assert terminal is coordinator.terminal_outcome
    assert [record.record_kind for record in audit.records].count(AuditRecordKind.RUN_TERMINAL) == 1


def test_batch_audit_failure_still_drains_every_issued_ingress() -> None:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert len(batch.ingresses) == 1
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _FailFirstBatchAudit(binding)
    runtime = _Runtime(matcher, delayed, dispatch_sequence=8)
    facts = _CountingFacts(matcher)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=facts,
        evidence_resolver=_NoEvidence(),
    )

    with pytest.raises(AuditContractError, match="injected batch"):
        coordinator.process_next_dispatch()

    assert facts.calls == 1
    assert runtime.active_lease is not None
    assert coordinator.state.phase is CoordinatorPhase.FAILING
    assert [record.record_kind for record in audit.records] == [
        AuditRecordKind.RUN_PREPARED,
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION,
    ]


def test_committed_terminal_ack_exception_uses_exact_retained_trace() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _CommittedThenRaisedRuntime(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    dispatch = coordinator.process_next_dispatch()

    assert dispatch.runtime_acknowledged is True
    assert coordinator.terminal_state is not None
    assert audit.records[-1].record_kind is AuditRecordKind.RUN_TERMINAL


def test_terminal_append_failure_retains_exact_retryable_preterminal_state() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _FailFirstTerminalAudit(binding)
    runtime = _Runtime(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )

    with pytest.raises(AuditContractError, match="terminal append"):
        coordinator.process_next_dispatch()
    retained = coordinator.pre_terminal_state

    terminal = coordinator.retry_terminalization()

    assert retained is not None
    assert coordinator.pre_terminal_state is retained
    assert coordinator.terminal_outcome is terminal
    assert [record.record_kind for record in audit.records].count(AuditRecordKind.RUN_TERMINAL) == 1


def test_recovery_reuses_resolved_batch_when_batch_audit_was_missing() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _FailFirstBatchAudit(binding)
    runtime = _Runtime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    with pytest.raises(AuditContractError, match="injected batch"):
        coordinator.process_next_dispatch()
    resolver_only = _ResolverOnlyMatcher(matcher)

    recovered = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=resolver_only,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
        records=tuple(audit.records),
    )
    dispatch = recovered.retry_active_dispatch()

    assert dispatch.runtime_acknowledged is True
    assert resolver_only.mutation_calls == 0
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.MATCHER_DISPATCH_BATCH
    ) == 1


@pytest.mark.parametrize("durable_failing_record", (False, True))
def test_recovery_keeps_pre_batch_matcher_failure_monotone(
    durable_failing_record: bool,
) -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit: _MemoryAudit = (
        _MemoryAudit(binding) if durable_failing_record else _FailFirstFailingAudit(binding)
    )
    runtime = _TracingMarketRuntime(matcher, delayed)
    fail_first = _FailFirstMatcher(matcher)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=fail_first,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    with pytest.raises(RuntimeError, match="pre-publication"):
        coordinator.process_next_dispatch()
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
    ) == int(durable_failing_record)

    recovered = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=fail_first,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
        records=tuple(audit.records),
    )

    assert recovered.state.phase is CoordinatorPhase.FAILING
    dispatch = recovered.retry_active_dispatch()

    assert dispatch.runtime_acknowledged is True
    assert fail_first.mutation_calls == 2
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
    ) == 1

    resolver_only = _ResolverOnlyMatcher(matcher)
    restarted = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=resolver_only,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
        records=tuple(audit.records),
    )

    assert restarted.state.phase is CoordinatorPhase.FAILING
    assert restarted.state.last_completed_dispatch_sequence == 1
    assert resolver_only.mutation_calls == 0
    with pytest.raises(LifecycleError, match="no active dispatch"):
        restarted.retry_active_dispatch()


def test_recovery_with_durable_completion_retries_only_runtime_acknowledgement() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _FailFirstRuntimeAcknowledgement(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    with pytest.raises(RuntimeError, match="pre-commit"):
        coordinator.process_next_dispatch()
    resolver_only = _ResolverOnlyMatcher(matcher)
    retained_keys = tuple(record.logical_key for record in audit.records)

    recovered = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=resolver_only,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
        records=tuple(audit.records),
    )
    assert tuple(audit.retry_keys) == retained_keys
    dispatch = recovered.retry_active_dispatch()

    assert dispatch.runtime_acknowledged is True
    assert runtime.acknowledgement_calls == 2
    assert resolver_only.mutation_calls == 0
    assert recovered.state.last_audit_chain_head_sha256 == audit_chain_head(audit.records[-1])
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ) == 1


def test_recovery_stops_before_runtime_retry_when_durable_ack_cannot_be_reconfirmed() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _RejectRecoveredFrameAudit(binding)
    runtime = _FailFirstRuntimeAcknowledgement(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    with pytest.raises(RuntimeError, match="pre-commit"):
        coordinator.process_next_dispatch()
    audit.reject_retries = True

    with pytest.raises(LifecycleError, match="could not be reconfirmed"):
        recover_phase1_lifecycle_coordinator(
            binding=binding,
            audit=audit,
            runtime=runtime,
            matcher=_ResolverOnlyMatcher(matcher),
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_NoEvidence(),
            records=tuple(audit.records),
        )

    assert runtime.acknowledgement_calls == 1


def test_recovery_accepts_committed_market_trace_without_second_acknowledgement() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _CommittedMarketThenRaisedRuntime(matcher, delayed)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    with pytest.raises(RuntimeError, match="committed market"):
        coordinator.process_next_dispatch()
    resolver_only = _ResolverOnlyMatcher(matcher)

    recovered = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=resolver_only,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
        records=tuple(audit.records),
    )

    assert recovered.state.last_completed_dispatch_sequence == 1
    assert recovered.state.phase is CoordinatorPhase.FAILING
    assert recovered.state.last_audit_chain_head_sha256 == audit_chain_head(audit.records[-1])
    assert runtime.acknowledgement_calls == 1
    assert resolver_only.mutation_calls == 0
    with pytest.raises(LifecycleError, match="no active dispatch"):
        recovered.retry_active_dispatch()


def test_terminal_recovery_reconstructs_read_only_terminal_evidence() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _Runtime(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    coordinator.process_next_dispatch()
    original_terminal = coordinator.terminal_outcome

    recovered = recover_phase1_terminal_evidence(
        binding=binding,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
        records=tuple(audit.records),
    )

    assert original_terminal is not None
    assert recovered.terminal_outcome == original_terminal
    assert recovered.terminal_state.final_chain_head_sha256 == audit_chain_head(audit.records[-1])


def test_recovery_validation_rejects_malformed_prefix_groups_and_runtime_traces() -> None:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _Runtime(matcher, end)
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_NoEvidence(),
    )
    coordinator.process_next_dispatch()
    records = tuple(audit.records)
    prefix = records[:-1]
    checked = tuple(coordinator_module._require_recovery_records(binding, prefix, audit=None))

    invalid_prefixes: tuple[object, ...] = ([], (), (object(),))
    for invalid in invalid_prefixes:
        with pytest.raises(LifecycleError):
            tuple(
                coordinator_module._require_recovery_records(
                    binding,
                    cast(Any, invalid),
                    audit=None,
                )
            )
    with pytest.raises(LifecycleError, match="terminal journal"):
        tuple(coordinator_module._require_recovery_records(binding, records, audit=None))

    prepared = prefix[0]
    original_binding = prepared.binding
    object.__setattr__(prepared, "binding", RunBinding(binding.reference, Sha256Digest("aa" * 32)))
    with pytest.raises(LifecycleError, match="binding conflicts"):
        tuple(coordinator_module._require_recovery_records(binding, prefix, audit=None))
    object.__setattr__(prepared, "binding", original_binding)

    original_record_id = prepared.record_id
    object.__setattr__(
        prepared,
        "record_id",
        EconomicId(binding.reference.run_id, EconomicOwnerKind.AUDIT_RECORD, 2),
    )
    with pytest.raises(LifecycleError, match="sequence conflicts"):
        tuple(coordinator_module._require_recovery_records(binding, prefix, audit=None))
    object.__setattr__(prepared, "record_id", original_record_id)

    completion = prefix[-1]
    original_previous = completion.previous_record_sha256
    object.__setattr__(completion, "previous_record_sha256", Sha256Digest("bb" * 32))
    with pytest.raises(LifecycleError, match="chain conflicts"):
        tuple(coordinator_module._require_recovery_records(binding, prefix, audit=None))
    object.__setattr__(completion, "previous_record_sha256", original_previous)

    batch_pair, completion_pair = checked[1:]
    batch = batch_pair[0]
    original_batch_payload = batch.canonical_payload
    for malformed, message in ((b"{", "not JSON"), (b"[]", "one object")):
        object.__setattr__(batch, "canonical_payload", malformed)
        with pytest.raises(LifecycleError, match=message):
            coordinator_module._record_document(batch)
    object.__setattr__(batch, "canonical_payload", original_batch_payload)

    batch_document = json.loads(original_batch_payload)

    def encoded(document: dict[str, object]) -> bytes:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    invalid_sequence = dict(batch_document)
    invalid_sequence["dispatch_sequence"] = 0
    object.__setattr__(batch, "canonical_payload", encoded(invalid_sequence))
    with pytest.raises(LifecycleError, match="sequence is invalid"):
        tuple(coordinator_module._group_recovery_records(iter((batch_pair,))))

    invalid_trigger = dict(batch_document)
    invalid_trigger["trigger_root_sha256"] = "invalid"
    object.__setattr__(batch, "canonical_payload", encoded(invalid_trigger))
    with pytest.raises(LifecycleError, match="trigger digest is invalid"):
        tuple(coordinator_module._group_recovery_records(iter((batch_pair,))))
    object.__setattr__(batch, "canonical_payload", original_batch_payload)

    with pytest.raises(LifecycleError, match="duplicate recovered batch"):
        tuple(coordinator_module._group_recovery_records(iter((batch_pair, batch_pair))))
    with pytest.raises(LifecycleError, match="duplicate recovered completion"):
        tuple(
            coordinator_module._group_recovery_records(
                iter((batch_pair, completion_pair, completion_pair))
            )
        )

    original_prepared_kind = prepared.record_kind
    original_prepared_payload = prepared.canonical_payload
    object.__setattr__(prepared, "canonical_payload", encoded({"dispatch_sequence": 1}))
    with pytest.raises(LifecycleError, match="unsupported recovery record kind"):
        tuple(coordinator_module._group_recovery_records(iter(((prepared, checked[0][1]),))))
    object.__setattr__(prepared, "record_kind", AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION)
    with pytest.raises(LifecycleError, match="authorization stage order"):
        tuple(coordinator_module._group_recovery_records(iter(((prepared, checked[0][1]),))))
    object.__setattr__(prepared, "record_kind", original_prepared_kind)
    object.__setattr__(prepared, "canonical_payload", original_prepared_payload)

    empty_group = coordinator_module._RecoveredDispatch(1)
    with pytest.raises(LifecycleError, match="dispatch is empty"):
        coordinator_module._require_recovery_stage_order(empty_group)
    completion_only = coordinator_module._RecoveredDispatch(1)
    completion_only.completion_record = (2, *completion_pair)
    with pytest.raises(LifecycleError, match="completion stage order"):
        coordinator_module._require_recovery_stage_order(completion_only)

    with pytest.raises(LifecycleError, match="trace evidence is incomplete"):
        coordinator_module._require_runtime_trace(SimpleNamespace(), binding)
    with pytest.raises(LifecycleError, match="trace digest conflicts"):
        coordinator_module._require_runtime_trace(
            SimpleNamespace(trace_records=[], trace_digest=runtime.trace_digest),
            binding,
        )

    valid_trace = runtime.trace_records[0]

    def trace_runtime(trace_records: tuple[bytes, ...]) -> SimpleNamespace:
        return SimpleNamespace(
            trace_records=trace_records,
            trace_digest=historical_runtime_trace_digest(trace_records),
        )

    with pytest.raises(LifecycleError, match="trace record is invalid"):
        coordinator_module._require_runtime_trace(trace_runtime((b"{",)), binding)
    with pytest.raises(LifecycleError, match="trace record conflicts"):
        coordinator_module._require_runtime_trace(trace_runtime((b" " + valid_trace,)), binding)

    trace_document = json.loads(valid_trace)
    for field, value in (
        ("schema", "invalid"),
        ("run_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("dispatch_sequence", "1"),
        ("root", []),
    ):
        changed = dict(trace_document)
        changed[field] = value
        with pytest.raises(LifecycleError, match="trace record conflicts"):
            coordinator_module._require_runtime_trace(
                trace_runtime((encoded(changed),)),
                binding,
            )
    unsupported_root = dict(trace_document)
    unsupported_root["root"] = {}
    with pytest.raises(LifecycleError, match="root kind is invalid"):
        coordinator_module._require_runtime_trace(
            trace_runtime((encoded(unsupported_root),)),
            binding,
        )
    sequence_two = dict(trace_document)
    sequence_two["dispatch_sequence"] = 2
    with pytest.raises(LifecycleError, match="sequence is not contiguous"):
        coordinator_module._require_runtime_trace(
            trace_runtime((encoded(sequence_two),)),
            binding,
        )
    with pytest.raises(LifecycleError, match="trace record conflicts"):
        coordinator_module._require_runtime_trace(
            trace_runtime((valid_trace, valid_trace)),
            binding,
        )
