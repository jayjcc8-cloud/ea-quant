from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    DataFingerprint,
    ReplayWindow,
    RunBinding,
    RunId,
    RunReference,
    Sha256Digest,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
)
from ea.experiments.binding import (
    BoundaryBindingError,
    BoundAuditPort,
    BoundOutputPort,
)
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    LineageInputs,
    NormalizedConfiguration,
    RuntimeEvidence,
    build_lineage_spec,
)
from ea.experiments.store import (
    AuditCapability,
    LocalResultStore,
    OutputCapability,
    PreparedRun,
    StoreError,
)


def _binding(*, run_id: str = "123e4567-e89b-42d3-a456-426614174000") -> RunBinding:
    return RunBinding(
        reference=RunReference(
            run_id=RunId(run_id),
            lineage_sha256=Sha256Digest("1" * 64),
        ),
        manifest_sha256=Sha256Digest("2" * 64),
    )


def _prepared(tmp_path: Path) -> PreparedRun:
    root = tmp_path / "results"
    root.mkdir()
    spec = build_lineage_spec(
        LineageInputs(
            code=CodeEvidence("0123456789abcdef0123456789abcdef01234567"),
            configuration=NormalizedConfiguration(1, "development", "backtest"),
            data=DataFingerprint(Sha256Digest("1" * 64), 1),
            replay_window=ReplayWindow(
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 2, 1, tzinfo=UTC),
            ),
            parameters=(),
            runtime=RuntimeEvidence(
                ea_version="0.1.1",
                python_implementation="cpython",
                python_version="3.12.13",
                python_cache_tag="cpython-312",
                sys_platform="darwin",
                platform_tag="macosx-11.0-arm64",
                distributions=(DistributionIdentity("ea-quant", "0.1.1"),),
                uv_lock_bytes=b"version = 1\n",
            ),
            master_seed=0,
            stream_labels=(),
        )
    )
    return LocalResultStore(root.resolve()).prepare(
        spec,
        lambda: UUID("123e4567-e89b-42d3-a456-426614174000"),
    )


class RecordingAudit:
    def __init__(
        self,
        binding: RunBinding,
        acknowledgement: AuditAppendAcknowledgement | None = None,
    ) -> None:
        self.binding = binding
        self.acknowledgement = acknowledgement
        self.calls: list[tuple[AuditRecordKind, AuditSubjectKind, Sha256Digest, bytes]] = []
        self.settlements: list[tuple[AuditLogicalKey, bytes]] = []

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        self.calls.append((record_kind, subject_kind, subject_sha256, canonical_payload))
        return self.acknowledgement or _prepared_ack(self.binding)

    def settle_append(
        self,
        *,
        logical_key: AuditLogicalKey,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        self.settlements.append((logical_key, canonical_payload))
        return self.acknowledgement


def _prepared_ack(binding: RunBinding) -> AuditAppendAcknowledgement:
    return create_audit_append_acknowledgement(
        create_audit_record(
            binding=binding,
            owner_sequence=1,
            record_kind=AuditRecordKind.RUN_PREPARED,
            subject_kind=AuditSubjectKind.RUN_MANIFEST,
            subject_sha256=binding.manifest_sha256,
            canonical_payload=canonical_run_prepared_audit_payload(binding),
            previous_record_sha256=EMPTY_RECORD_SHA256,
            previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
        )
    )


class RecordingOutput:
    def __init__(self, acknowledgement: RunBinding) -> None:
        self.acknowledgement = acknowledgement
        self.calls: list[tuple[RunBinding, OutputCapability, bytes]] = []

    def write(
        self,
        binding: RunBinding,
        capability: OutputCapability,
        payload: bytes,
    ) -> RunBinding:
        self.calls.append((binding, capability, payload))
        return self.acknowledgement


def test_bound_audit_and_output_forward_only_store_binding_and_capability(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    binding = prepared.audit.binding
    output_capability = prepared.output.capability
    audit_raw = RecordingAudit(binding)
    output_raw = RecordingOutput(binding)
    audit = BoundAuditPort(prepared.audit, audit_raw)
    output = BoundOutputPort(prepared.output, output_raw)

    payload = canonical_run_prepared_audit_payload(binding)
    audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=payload,
    )
    output.write(binding.reference, b"output")

    assert audit.reference == output.reference == binding.reference
    assert audit_raw.calls == [
        (
            AuditRecordKind.RUN_PREPARED,
            AuditSubjectKind.RUN_MANIFEST,
            binding.manifest_sha256,
            payload,
        )
    ]
    assert output_raw.calls == [(binding, output_capability, b"output")]


def test_bound_audit_settlement_revalidates_exact_acknowledgement(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    binding = prepared.audit.binding
    acknowledgement = _prepared_ack(binding)
    raw = RecordingAudit(binding, acknowledgement)
    audit = BoundAuditPort(prepared.audit, raw)
    payload = canonical_run_prepared_audit_payload(binding)
    key = AuditLogicalKey(
        AuditRecordKind.RUN_PREPARED,
        AuditSubjectKind.RUN_MANIFEST,
        binding.manifest_sha256,
    )

    settled = audit.settle_append(logical_key=key, canonical_payload=payload)

    assert settled is acknowledgement
    assert raw.settlements == [(key, payload)]


def test_attempt_or_lineage_mismatch_fails_before_raw_persistence(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    expected = prepared.audit.binding
    other_attempt = _binding(run_id="123e4567-e89b-42d3-b456-426614174000")
    raw = RecordingAudit(other_attempt)
    with pytest.raises(BoundaryBindingError, match="binding"):
        BoundAuditPort(prepared.audit, raw)
    assert expected != other_attempt
    assert raw.calls == []


def test_raw_acknowledgement_mismatch_fails_after_persistence(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    expected = prepared.output.binding
    wrong = _binding(run_id="123e4567-e89b-42d3-b456-426614174000")
    raw = RecordingOutput(wrong)
    port = BoundOutputPort(prepared.output, raw)

    with pytest.raises(BoundaryBindingError, match="acknowledgement"):
        port.write(expected.reference, b"output")

    assert len(raw.calls) == 1


def test_audit_and_output_capabilities_cannot_be_interchanged(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    with pytest.raises(BoundaryBindingError, match="AuditRunBinding"):
        BoundAuditPort(
            prepared.output,  # type: ignore[arg-type]
            RecordingAudit(prepared.audit.binding),
        )


def test_non_bytes_payload_fails_before_raw_persistence(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    binding = prepared.output.binding
    raw = RecordingOutput(binding)
    port = BoundOutputPort(prepared.output, raw)

    with pytest.raises(BoundaryBindingError, match="exact bytes"):
        port.write(binding.reference, bytearray(b"not exact"))  # type: ignore[arg-type]

    assert raw.calls == []


def test_capabilities_are_store_issued_pathless_and_immutable(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)

    with pytest.raises(StoreError, match="only be issued"):
        AuditCapability(object())
    with pytest.raises(StoreError, match="only be issued"):
        OutputCapability(object())
    assert not hasattr(prepared.audit.capability, "__dict__")
    assert not hasattr(prepared.audit.capability, "_directory")
    with pytest.raises(AttributeError):
        prepared.audit.capability.path = tmp_path
