from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

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
    create_audit_append_acknowledgement,
    create_audit_record,
)
from ea.core.execution_state import ExecutionFactProcessingOutcome
from ea.core.historical_matching import canonical_end_of_run_root_bytes
from ea.core.lifecycle import CoordinatorPhase
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.runtime.coordinator import create_phase1_lifecycle_coordinator
from ea.runtime.historical import historical_runtime_trace_digest
from unit.test_historical_matcher import _system


class _MemoryAudit:
    def __init__(self, binding: RunBinding) -> None:
        self.binding = binding
        self.records: list[AuditRecord] = []
        self.index: dict[AuditLogicalKey, tuple[bytes, AuditAppendAcknowledgement]] = {}

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
            document = {
                "clock_now": root_document["available_at"],
                "committed_cursor_as_of": None,
                "committed_event_count": 0,
                "data_sha256": "00" * 32,
                "dispatch_sequence": lease.dispatch_sequence,
                "root": root_document,
                "root_order_key": [],
                "run_id": self.run_id.value,
                "schema": "ea.historical-runtime-trace.v1",
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
