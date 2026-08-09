from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ea.core.audit import (
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
)
from ea.core.run import Sha256Digest
from ea.experiments.audit import (
    AUDIT_JOURNAL_PREAMBLE,
    create_posix_audit_journal,
    reopen_posix_audit_journal,
)
from ea.experiments.store import LocalResultStore
from unit.test_store import RUN_UUID, _root, _spec


def _batch_payload() -> bytes:
    return (
        b'{"batch_sha256":"'
        + b"33" * 32
        + b'","canonicalization":"ea-canonical-json-v1","dispatch_kind":"market",'
        b'"dispatch_sequence":1,"ingress_count":0,'
        b'"ordered_ingress_sha256s_sha256":"'
        + b"24"
        * 32
        + b'","run_id":"123e4567-e89b-42d3-a456-426614174000",'
        b'"schema":"ea.audit-matcher-dispatch-batch.v1","trigger_root_key":{},'
        b'"trigger_root_sha256":"' + b"44" * 32 + b'"}'
    )


def test_fresh_journal_is_prepared_before_general_append_and_exact_retry(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)

    path = root / str(RUN_UUID) / "audit" / "audit-v1.journal"
    lock_path = root / str(RUN_UUID) / "audit" / "writer-v1.lock"
    assert path.read_bytes().startswith(AUDIT_JOURNAL_PREAMBLE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert len(journal.records) == 1
    assert journal.records[0].record_kind is AuditRecordKind.RUN_PREPARED

    acknowledgement = journal.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )
    replay = journal.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )

    assert replay is acknowledgement
    assert acknowledgement.record_id.owner_sequence == 2
    assert audit_append_acknowledgement_digest(replay) == audit_append_acknowledgement_digest(
        acknowledgement
    )
    assert len(journal.records) == 2


def test_same_logical_key_with_different_payload_fails_monotonically(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    journal.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )

    with pytest.raises(AuditContractError, match="conflicts"):
        journal.append(
            record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
            subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
            subject_sha256=Sha256Digest("33" * 32),
            canonical_payload=b'{"different":true}',
        )
    with pytest.raises(AuditContractError, match="failed state"):
        journal.append(
            record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
            subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
            subject_sha256=Sha256Digest("55" * 32),
            canonical_payload=b'{"different":true}',
        )


def test_reopen_reconstructs_exact_index_and_acknowledgement(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    acknowledgement = journal.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )
    journal.close()

    reopened = reopen_posix_audit_journal(prepared.audit)
    replay = reopened.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )

    assert len(reopened.records) == 2
    assert replay == acknowledgement


def test_reopen_truncates_only_the_incomplete_final_frame(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    journal.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )
    journal.close()
    path = root / str(RUN_UUID) / "audit" / "audit-v1.journal"
    complete_size = path.stat().st_size
    path.write_bytes(path.read_bytes()[:-10])

    reopened = reopen_posix_audit_journal(prepared.audit)
    assert len(reopened.records) == 1
    assert path.stat().st_size < complete_size
    acknowledgement = reopened.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )
    assert acknowledgement.record_id.owner_sequence == 2


def test_reopen_rejects_complete_checksum_corruption_without_repair(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    journal.close()
    path = root / str(RUN_UUID) / "audit" / "audit-v1.journal"
    corrupted = bytearray(path.read_bytes())
    corrupted[-1] ^= 1
    path.write_bytes(corrupted)
    original = path.read_bytes()

    with pytest.raises(AuditContractError, match="checksum"):
        reopen_posix_audit_journal(prepared.audit)

    assert path.read_bytes() == original
