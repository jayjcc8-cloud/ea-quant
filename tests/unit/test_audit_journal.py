from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from sys import getsizeof
from types import SimpleNamespace
from typing import Any

import pytest

import ea.experiments.audit as audit_module
from ea.composition.run import RunCompositionError, admit_recovered_run
from ea.core.audit import (
    MAX_AUDIT_RECORDS,
    AuditContractError,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_subject_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import Sha256Digest
from ea.experiments.audit import (
    AUDIT_JOURNAL_PREAMBLE,
    PosixAuditJournal,
    PosixAuditRecoveryRecordSource,
    _entry_resident_bytes,
    _JournalEntry,
    _OsAuditOps,
    create_posix_audit_journal,
    reopen_posix_audit_journal,
)
from ea.experiments.store import (
    AuditRunBinding,
    LocalResultStore,
    PreparedRun,
    RecoveredTerminalRun,
    StoreError,
    VerifiedIncompleteRecoveryBinding,
    VerifiedTerminalRecoveryBinding,
)
from unit.test_store import RUN_UUID, _root, _spec


def _batch_payload() -> bytes:
    return json.dumps(
        {
            "batch_sha256": "33" * 32,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_kind": "market",
            "dispatch_sequence": 1,
            "ingress_count": 0,
            "ordered_ingress_sha256s_sha256": "24" * 32,
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
            "schema": "ea.audit-matcher-dispatch-batch.v1",
            "trigger_root_key": {
                "adjustment": "raw",
                "available_at": "2026-01-02T09:31:00.000000Z",
                "domain_rank": 30,
                "event_time": "2026-01-02T09:31:00.000000Z",
                "instrument": {"symbol": "AAPL", "venue": "XNAS"},
                "interval_end": "2026-01-02T09:31:00.000000Z",
                "interval_start": "2026-01-02T09:30:00.000000Z",
                "kind_rank": 0,
                "revision": 0,
                "root_domain": "market_data",
                "source": "primary.raw",
                "source_sequence": 0,
            },
            "trigger_root_sha256": "44" * 32,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class _LowSpaceAuditOps(_OsAuditOps):
    def fstatvfs(self, descriptor: int) -> Any:
        del descriptor
        return SimpleNamespace(f_bavail=1, f_frsize=1)


class _PostFsyncFailureAuditOps(_OsAuditOps):
    armed = False

    def fsync(self, descriptor: int) -> None:
        super().fsync(descriptor)
        if self.armed:
            self.armed = False
            raise OSError("injected post-fsync return failure")


class _PostCommitReadFailureAuditOps(_OsAuditOps):
    armed = False

    def pread(self, journal_fd: int, size: int, offset: int) -> bytes:
        if self.armed:
            self.armed = False
            raise OSError("injected post-commit readback failure")
        return super().pread(journal_fd, size, offset)


class _ReadbackMismatchAuditOps(_OsAuditOps):
    armed = False

    def pread(self, journal_fd: int, size: int, offset: int) -> bytes:
        value = super().pread(journal_fd, size, offset)
        if self.armed and value:
            self.armed = False
            return value[:-1] + bytes((value[-1] ^ 1,))
        return value


class _SettlementReadbackMismatchAuditOps(_PostFsyncFailureAuditOps):
    mismatch_after_next_fsync = False
    corrupt_next_read = False

    def fsync(self, descriptor: int) -> None:
        super().fsync(descriptor)
        if self.mismatch_after_next_fsync:
            self.mismatch_after_next_fsync = False
            self.corrupt_next_read = True

    def pread(self, journal_fd: int, size: int, offset: int) -> bytes:
        value = super().pread(journal_fd, size, offset)
        if self.corrupt_next_read and value:
            self.corrupt_next_read = False
            return value[:-1] + bytes((value[-1] ^ 1,))
        return value


def test_fresh_journal_rejects_less_than_six_gib_free_before_creation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)

    with pytest.raises(StoreError, match="6 GiB"):
        create_posix_audit_journal(prepared.audit, _ops=_LowSpaceAuditOps())

    assert not (root / str(RUN_UUID) / "audit" / "audit-v1.journal").exists()


def test_reopen_rejects_less_than_six_gib_free_before_admission(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    journal.close()

    with pytest.raises(StoreError, match="6 GiB"):
        reopen_posix_audit_journal(prepared.audit, _ops=_LowSpaceAuditOps())


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


@pytest.mark.parametrize("ops_type", [_PostFsyncFailureAuditOps, _PostCommitReadFailureAuditOps])
def test_uncertain_physical_append_is_settled_by_rescan_fsync_and_readback(
    tmp_path: Path,
    ops_type: type[_OsAuditOps],
) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    ops: Any = ops_type()
    journal = create_posix_audit_journal(prepared.audit, _ops=ops)
    payload = _batch_payload()
    key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        Sha256Digest("33" * 32),
    )
    ops.armed = True

    with pytest.raises(AuditContractError, match="verified durability"):
        journal.append(
            record_kind=key.record_kind,
            subject_kind=key.subject_kind,
            subject_sha256=key.subject_sha256,
            canonical_payload=payload,
        )

    acknowledgement = journal.settle_append(
        logical_key=key,
        canonical_payload=payload,
    )

    assert acknowledgement is not None
    assert acknowledgement.record_id.owner_sequence == 2
    assert journal.settle_append(logical_key=key, canonical_payload=payload) is not None
    assert len(journal.records) == 2


def test_observed_readback_mismatch_fails_monotonically_without_settlement(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    ops = _ReadbackMismatchAuditOps()
    journal = create_posix_audit_journal(prepared.audit, _ops=ops)
    payload = _batch_payload()
    key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        Sha256Digest("33" * 32),
    )
    ops.armed = True

    with pytest.raises(AuditContractError, match="integrity evidence mismatched"):
        journal.append(
            record_kind=key.record_kind,
            subject_kind=key.subject_kind,
            subject_sha256=key.subject_sha256,
            canonical_payload=payload,
        )

    with pytest.raises(AuditContractError, match="failed state"):
        journal.settle_append(logical_key=key, canonical_payload=payload)


def test_settlement_readback_mismatch_fails_monotonically(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    ops = _SettlementReadbackMismatchAuditOps()
    journal = create_posix_audit_journal(prepared.audit, _ops=ops)
    payload = _batch_payload()
    key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        Sha256Digest("33" * 32),
    )
    ops.armed = True
    with pytest.raises(AuditContractError, match="verified durability"):
        journal.append(
            record_kind=key.record_kind,
            subject_kind=key.subject_kind,
            subject_sha256=key.subject_sha256,
            canonical_payload=payload,
        )
    ops.mismatch_after_next_fsync = True

    with pytest.raises(AuditContractError, match="integrity mismatched"):
        journal.settle_append(logical_key=key, canonical_payload=payload)

    with pytest.raises(AuditContractError, match="failed state"):
        journal.settle_append(logical_key=key, canonical_payload=payload)
    with pytest.raises(AuditContractError, match="failed state"):
        journal.append(
            record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
            subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
            subject_sha256=Sha256Digest("55" * 32),
            canonical_payload=payload,
        )


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


def test_reopen_rejects_index_that_exceeds_resident_memory_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    journal.close()
    monkeypatch.setattr(audit_module, "MAX_AUDIT_INDEX_RESIDENT_BYTES", 1)

    with pytest.raises(AuditContractError, match="256 MiB") as captured:
        reopen_posix_audit_journal(prepared.audit)

    assert captured.value.code is OutcomeCode.OUT_OF_RANGE


def test_maximum_compact_reopen_index_fits_the_256_mib_budget() -> None:
    count = MAX_AUDIT_RECORDS
    entry = _JournalEntry(
        offset=0,
        frame_length=20_528,
        payload_length=16_384,
        payload_sha256=b"0" * 32,
        record_sha256=b"1" * 32,
        chain_head_sha256=b"2" * 32,
    )
    max_key_length = (
        max(len(kind.value) for kind in AuditRecordKind)
        + 1
        + max(len(kind.value) for kind in AuditSubjectKind)
        + 1
        + 32
    )
    container_bytes = (
        getsizeof([None] * count)
        + getsizeof(dict.fromkeys(range(count)))
        + getsizeof(bytearray(count))
    )
    per_entry_bytes = (
        _entry_resident_bytes(entry) + getsizeof(b"x" * max_key_length) + getsizeof(count) + 1
    )

    assert container_bytes + count * per_entry_bytes <= audit_module.MAX_AUDIT_INDEX_RESIDENT_BYTES


def test_recovery_record_source_is_repeatable_and_snapshot_bound(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    prefix = journal.recovery_records
    initial = tuple(prefix)

    journal.append(
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=_batch_payload(),
    )

    assert tuple(prefix) == initial
    assert prefix.record_count == 1
    complete = journal.recovery_records
    complete_records = tuple(complete)
    assert complete_records == journal.records
    with pytest.raises(AuditContractError, match="prefix count"):
        PosixAuditRecoveryRecordSource(journal, record_count=True)
    assert complete.record_at(0) == initial[0]
    assert complete.record_at(1) == complete_records[1]
    assert tuple(complete.prefix(1)) == initial
    assert prefix.resolve_record(complete_records[1].logical_key) is None
    assert complete.resolve_record(complete_records[1].logical_key) == complete_records[1]
    with pytest.raises(AuditContractError, match="sub-prefix"):
        complete.prefix(True)
    with pytest.raises(AuditContractError, match="sub-prefix"):
        complete.prefix(3)
    with pytest.raises(AuditContractError, match="exact int"):
        complete.record_at(True)
    with pytest.raises(AuditContractError, match="out of range"):
        complete.record_at(2)
    with pytest.raises(AuditContractError, match="key must be exact"):
        complete.resolve_record(object())  # type: ignore[arg-type]
    journal.close()
    with pytest.raises(AuditContractError, match="no longer readable"):
        tuple(prefix)


def test_append_rejects_projected_index_growth_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    prepared = LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)
    journal = create_posix_audit_journal(prepared.audit)
    path = root / str(RUN_UUID) / "audit" / "audit-v1.journal"
    original_size = path.stat().st_size
    monkeypatch.setattr(
        audit_module,
        "MAX_AUDIT_INDEX_RESIDENT_BYTES",
        journal._index_resident_bytes,
    )

    with pytest.raises(AuditContractError, match="256 MiB"):
        journal.append(
            record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
            subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
            subject_sha256=Sha256Digest("33" * 32),
            canonical_payload=_batch_payload(),
        )

    assert path.stat().st_size == original_size
    assert len(journal.records) == 1


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


def _release_simulated_process_writer(store: LocalResultStore, prepared: PreparedRun) -> None:
    record = store._record_for(prepared._authority)
    os.close(record.writer_lock_fd)


def test_new_store_recovers_incomplete_attempt_with_one_use_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    original_store = LocalResultStore(root)
    prepared = original_store.prepare(_spec(), lambda: RUN_UUID)
    manifest = original_store.verify_manifest(prepared.manifest_verification)
    journal = create_posix_audit_journal(prepared.audit)
    journal.close()
    _release_simulated_process_writer(original_store, prepared)

    def reject_materialization(_journal: PosixAuditJournal) -> tuple[object, ...]:
        raise AssertionError("production recovery must not materialize journal.records")

    monkeypatch.setattr(PosixAuditJournal, "records", property(reject_materialization))

    recovered_store = LocalResultStore(root)
    verified = recovered_store.verify_recovery_attempt(manifest)
    assert type(verified) is VerifiedIncompleteRecoveryBinding
    recovered = recovered_store.recover_incomplete_attempt(verified)
    admitted_journals: list[PosixAuditJournal] = []

    def reopen_for_admission(prepared: AuditRunBinding) -> PosixAuditJournal:
        journal = reopen_posix_audit_journal(prepared)
        admitted_journals.append(journal)
        return journal

    admitted = admit_recovered_run(
        recovered,
        audit_factory=reopen_for_admission,
    )
    with pytest.raises(RunCompositionError, match="already admitted"):
        admit_recovered_run(recovered, audit_factory=reopen_for_admission)
    assert len(admitted_journals) == 1
    reopened = reopen_posix_audit_journal(recovered.audit)

    assert recovered.reference == prepared.reference
    assert recovered.record_count == 1
    assert admitted.records.record_at(0).record_kind is AuditRecordKind.RUN_PREPARED
    binding, admitted_audit, admitted_records = admitted._consume()
    assert binding == recovered.audit.binding
    assert admitted_audit.binding == binding
    assert tuple(admitted_records) == tuple(reopened.recovery_records)
    assert reopened.recovery_records.record_count == 1
    with pytest.raises(RunCompositionError, match="already consumed"):
        admitted._consume()
    with pytest.raises(StoreError, match="stale or foreign"):
        recovered_store.recover_incomplete_attempt(verified)
    reopened.close()
    admitted_journals[0].close()


def test_recovery_refuses_a_live_writer_before_journal_adoption(tmp_path: Path) -> None:
    root = _root(tmp_path)
    active_store = LocalResultStore(root)
    prepared = active_store.prepare(_spec(), lambda: RUN_UUID)
    manifest = active_store.verify_manifest(prepared.manifest_verification)
    create_posix_audit_journal(prepared.audit)

    with pytest.raises(StoreError, match="active owner"):
        LocalResultStore(root).verify_recovery_attempt(manifest)


def test_terminal_recovery_returns_read_only_lost_ack_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    original_store = LocalResultStore(root)
    prepared = original_store.prepare(_spec(), lambda: RUN_UUID)
    manifest = original_store.verify_manifest(prepared.manifest_verification)
    journal = create_posix_audit_journal(prepared.audit)
    previous_chain = audit_chain_head(journal.records[-1]).value
    payload = json.dumps(
        {
            "canonicalization": "ea-canonical-json-v1",
            "last_dispatch_sequence": 1,
            "last_trigger_root_sha256": "33" * 32,
            "pre_terminal_state_sha256": "44" * 32,
            "previous_chain_head_sha256": previous_chain,
            "run_id": str(RUN_UUID),
            "schema": "ea.audit-run-terminal.v1",
            "terminal_kind": "success",
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    journal.append(
        record_kind=AuditRecordKind.RUN_TERMINAL,
        subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
        subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        canonical_payload=payload,
    )
    journal.close()
    _release_simulated_process_writer(original_store, prepared)

    def reject_materialization(_journal: PosixAuditJournal) -> tuple[object, ...]:
        raise AssertionError("terminal recovery must not materialize journal.records")

    monkeypatch.setattr(PosixAuditJournal, "records", property(reject_materialization))

    recovered_store = LocalResultStore(root)
    verified = recovered_store.verify_recovery_attempt(manifest)
    assert type(verified) is VerifiedTerminalRecoveryBinding
    terminal = recovered_store.recover_terminal_attempt(verified)

    assert terminal.record_count == 2
    assert terminal.terminal_record.record_kind is AuditRecordKind.RUN_TERMINAL
    assert not hasattr(terminal, "records")
    assert not hasattr(terminal, "audit")
    assert not hasattr(terminal, "output")
    with pytest.raises(StoreError, match="store-issued"):
        RecoveredTerminalRun(
            object(),
            binding=terminal.binding,
            records=terminal._records,
            journal=terminal._journal,  # type: ignore[arg-type]
            terminal_record=terminal.terminal_record,
            terminal_acknowledgement=terminal.terminal_acknowledgement,
        )
    terminal_binding, terminal_records = terminal._consume()
    assert terminal_binding == terminal.binding
    assert terminal_records.record_at(terminal_records.record_count - 1) == terminal.terminal_record
    with pytest.raises(StoreError, match="already consumed"):
        terminal._consume()
    with pytest.raises(StoreError, match="stale or foreign"):
        recovered_store.recover_terminal_attempt(verified)
    terminal._finish()
