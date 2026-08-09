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
from ea.experiments.audit import AUDIT_JOURNAL_PREAMBLE, create_posix_audit_journal
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
