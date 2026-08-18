"""Concrete POSIX v1 append-only audit journal from Accepted ADR 0020."""

from __future__ import annotations

import os
import stat
from array import array
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from sys import getsizeof
from threading import Lock
from typing import Protocol, final
from weakref import WeakValueDictionary

from ea.core.audit import (
    AUDIT_FRAME_DIGEST_DOMAIN,
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    MAX_AUDIT_HEADER_BYTES,
    MAX_AUDIT_RECORDS,
    MAX_LARGE_AUDIT_PAYLOAD_BYTES,
    AuditAppendAcknowledgement,
    AuditContractError,
    AuditLogicalKey,
    AuditRecord,
    AuditRecordKind,
    AuditSubjectKind,
    audit_chain_head,
    audit_frame_checksum,
    audit_record_digest,
    canonical_audit_record_bytes,
    canonical_audit_record_header_bytes,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
    decode_audit_record,
    require_audit_acknowledgement,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, Sha256Digest
from ea.experiments.store import AuditRunBinding, StoreError

AUDIT_JOURNAL_PREAMBLE = b"EA-AUDIT-V1\n"
AUDIT_JOURNAL_NAME = "audit-v1.journal"
MAX_AUDIT_JOURNAL_BYTES = 13 * 1024 * 1024 * 1024
MIN_AUDIT_FILESYSTEM_FREE_BYTES = MAX_AUDIT_JOURNAL_BYTES
MAX_AUDIT_INDEX_RESIDENT_BYTES = 256 * 1024 * 1024


class _AuditOps(Protocol):
    def create_journal(self, audit_fd: int) -> int: ...

    def open_journal(self, audit_fd: int) -> int: ...

    def pread(self, journal_fd: int, size: int, offset: int) -> bytes: ...

    def pwrite(self, journal_fd: int, data: memoryview, offset: int) -> int: ...

    def fsync(self, descriptor: int) -> None: ...

    def fstat(self, descriptor: int) -> os.stat_result: ...

    def fstatvfs(self, descriptor: int) -> os.statvfs_result: ...

    def ftruncate(self, descriptor: int, length: int) -> None: ...

    def close(self, descriptor: int) -> None: ...


class _OsAuditOps:
    def create_journal(self, audit_fd: int) -> int:
        return os.open(
            AUDIT_JOURNAL_NAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=audit_fd,
        )

    def open_journal(self, audit_fd: int) -> int:
        return os.open(
            AUDIT_JOURNAL_NAME,
            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=audit_fd,
        )

    def pread(self, journal_fd: int, size: int, offset: int) -> bytes:
        return os.pread(journal_fd, size, offset)

    def pwrite(self, journal_fd: int, data: memoryview, offset: int) -> int:
        return os.pwrite(journal_fd, data, offset)

    def fsync(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def fstat(self, descriptor: int) -> os.stat_result:
        return os.fstat(descriptor)

    def fstatvfs(self, descriptor: int) -> os.statvfs_result:
        return os.fstatvfs(descriptor)

    def ftruncate(self, descriptor: int, length: int) -> None:
        os.ftruncate(descriptor, length)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)


class _ObservedAuditMismatch(Exception):
    """A completed readback proved bytes or derived integrity evidence differ."""


def _audit_error(code: OutcomeCode, message: str) -> AuditContractError:
    return AuditContractError(code, message)


def _write_all(ops: _AuditOps, descriptor: int, payload: bytes, offset: int) -> None:
    remaining = memoryview(payload)
    position = offset
    while remaining:
        written = ops.pwrite(descriptor, remaining, position)
        if type(written) is not int or written <= 0 or written > len(remaining):
            raise OSError("audit write made no valid forward progress")
        position += written
        remaining = remaining[written:]


def _pread_exact(ops: _AuditOps, descriptor: int, size: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    position = offset
    while remaining:
        chunk = ops.pread(descriptor, remaining, position)
        if type(chunk) is not bytes:
            raise OSError("audit read returned a non-bytes value")
        if not chunk:
            break
        chunks.append(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _frame_bytes(record: AuditRecord) -> bytes:
    return canonical_audit_record_bytes(record) + bytes.fromhex(audit_frame_checksum(record).value)


@dataclass(frozen=True, slots=True)
class _JournalEntry:
    offset: int
    frame_length: int
    payload_length: int
    payload_sha256: bytes
    record_sha256: bytes
    chain_head_sha256: bytes


class _JournalEntryIndex:
    """Fixed-width per-record offsets and digests without retained Python entries."""

    __slots__ = (
        "_chain_head_sha256s",
        "_frame_lengths",
        "_offsets",
        "_payload_lengths",
        "_payload_sha256s",
        "_record_sha256s",
    )

    def __init__(self) -> None:
        self._offsets = array("Q")
        self._frame_lengths = array("I")
        self._payload_lengths = array("I")
        if self._offsets.itemsize != 8 or self._frame_lengths.itemsize != 4:
            raise RuntimeError("audit compact index requires 64-bit Q and 32-bit I arrays")
        self._payload_sha256s = bytearray()
        self._record_sha256s = bytearray()
        self._chain_head_sha256s = bytearray()

    def __len__(self) -> int:
        return len(self._offsets)

    def __iter__(self) -> Iterator[_JournalEntry]:
        for index in range(len(self)):
            yield self[index]

    def __getitem__(self, index: int) -> _JournalEntry:
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        digest_offset = index * 32
        digest_end = digest_offset + 32
        return _JournalEntry(
            offset=self._offsets[index],
            frame_length=self._frame_lengths[index],
            payload_length=self._payload_lengths[index],
            payload_sha256=bytes(self._payload_sha256s[digest_offset:digest_end]),
            record_sha256=bytes(self._record_sha256s[digest_offset:digest_end]),
            chain_head_sha256=bytes(self._chain_head_sha256s[digest_offset:digest_end]),
        )

    def append(self, entry: _JournalEntry) -> None:
        if (
            type(entry) is not _JournalEntry
            or not 0 <= entry.offset <= (1 << 64) - 1
            or not 0 <= entry.frame_length <= (1 << 32) - 1
            or not 0 <= entry.payload_length <= (1 << 32) - 1
            or len(entry.payload_sha256) != 32
            or len(entry.record_sha256) != 32
            or len(entry.chain_head_sha256) != 32
        ):
            raise ValueError("audit compact entry is invalid")
        self._offsets.append(entry.offset)
        self._frame_lengths.append(entry.frame_length)
        self._payload_lengths.append(entry.payload_length)
        self._payload_sha256s.extend(entry.payload_sha256)
        self._record_sha256s.extend(entry.record_sha256)
        self._chain_head_sha256s.extend(entry.chain_head_sha256)

    def pop(self) -> _JournalEntry:
        entry = self[-1]
        self._offsets.pop()
        self._frame_lengths.pop()
        self._payload_lengths.pop()
        del self._payload_sha256s[-32:]
        del self._record_sha256s[-32:]
        del self._chain_head_sha256s[-32:]
        return entry

    def resident_bytes(self) -> int:
        return getsizeof(self) + sum(
            getsizeof(value)
            for value in (
                self._offsets,
                self._frame_lengths,
                self._payload_lengths,
                self._payload_sha256s,
                self._record_sha256s,
                self._chain_head_sha256s,
            )
        )


_AUDIT_RECORD_KIND_RANK = {kind: rank + 1 for rank, kind in enumerate(AuditRecordKind)}
_AUDIT_SUBJECT_KIND_RANK = {kind: rank + 1 for rank, kind in enumerate(AuditSubjectKind)}


class _LogicalKeyIndex:
    """Fixed-width open-addressed logical-key index with deterministic probing."""

    __slots__ = (
        "_count",
        "_digests",
        "_record_kinds",
        "_sequences",
        "_subject_kinds",
    )

    def __init__(self, capacity: int = 8) -> None:
        if capacity < 8 or capacity & (capacity - 1):
            raise ValueError("audit logical-key capacity must be a power of two")
        self._count = 0
        self._record_kinds = bytearray(capacity)
        self._subject_kinds = bytearray(capacity)
        self._digests = bytearray(capacity * 32)
        self._sequences = array("I", [0]) * capacity
        if self._sequences.itemsize != 4:
            raise RuntimeError("audit compact index requires a 32-bit I array")

    def __len__(self) -> int:
        return self._count

    @property
    def capacity(self) -> int:
        return len(self._sequences)

    @staticmethod
    def _parts(logical_key: AuditLogicalKey) -> tuple[int, int, bytes]:
        return (
            _AUDIT_RECORD_KIND_RANK[logical_key.record_kind],
            _AUDIT_SUBJECT_KIND_RANK[logical_key.subject_kind],
            bytes.fromhex(logical_key.subject_sha256.value),
        )

    @staticmethod
    def _initial_slot(record_rank: int, subject_rank: int, digest: bytes, mask: int) -> int:
        return (
            int.from_bytes(sha256(bytes((record_rank, subject_rank)) + digest).digest()[:8], "big")
            & mask
        )

    def _find_slot(self, record_rank: int, subject_rank: int, digest: bytes) -> tuple[int, bool]:
        mask = self.capacity - 1
        slot = self._initial_slot(record_rank, subject_rank, digest, mask)
        while self._sequences[slot] != 0:
            digest_offset = slot * 32
            if (
                self._record_kinds[slot] == record_rank
                and self._subject_kinds[slot] == subject_rank
                and self._digests[digest_offset : digest_offset + 32] == digest
            ):
                return slot, True
            slot = (slot + 1) & mask
        return slot, False

    def get(self, logical_key: AuditLogicalKey) -> int | None:
        record_rank, subject_rank, digest = self._parts(logical_key)
        slot, found = self._find_slot(record_rank, subject_rank, digest)
        return self._sequences[slot] if found else None

    def reserve_for_insert(self) -> None:
        if (self._count + 1) * 4 > self.capacity * 3:
            self._resize(self.capacity * 2)

    def insert(self, logical_key: AuditLogicalKey, sequence: int) -> None:
        if type(sequence) is not int or not 1 <= sequence <= MAX_AUDIT_RECORDS:
            raise ValueError("audit logical-key sequence is invalid")
        self.reserve_for_insert()
        record_rank, subject_rank, digest = self._parts(logical_key)
        slot, found = self._find_slot(record_rank, subject_rank, digest)
        if found:
            raise ValueError("audit logical key already exists")
        self._record_kinds[slot] = record_rank
        self._subject_kinds[slot] = subject_rank
        digest_offset = slot * 32
        self._digests[digest_offset : digest_offset + 32] = digest
        self._sequences[slot] = sequence
        self._count += 1

    def _resize(self, capacity: int) -> None:
        replacement = _LogicalKeyIndex(capacity)
        for slot, sequence in enumerate(self._sequences):
            if sequence == 0:
                continue
            digest_offset = slot * 32
            digest = bytes(self._digests[digest_offset : digest_offset + 32])
            record_rank = self._record_kinds[slot]
            subject_rank = self._subject_kinds[slot]
            replacement_slot, found = replacement._find_slot(record_rank, subject_rank, digest)
            if found:
                raise RuntimeError("audit logical-key rehash produced a duplicate")
            replacement._record_kinds[replacement_slot] = record_rank
            replacement._subject_kinds[replacement_slot] = subject_rank
            replacement_digest_offset = replacement_slot * 32
            replacement._digests[replacement_digest_offset : replacement_digest_offset + 32] = (
                digest
            )
            replacement._sequences[replacement_slot] = sequence
            replacement._count += 1
        self._record_kinds = replacement._record_kinds
        self._subject_kinds = replacement._subject_kinds
        self._digests = replacement._digests
        self._sequences = replacement._sequences

    def resident_bytes(self) -> int:
        return getsizeof(self) + sum(
            getsizeof(value)
            for value in (
                self._record_kinds,
                self._subject_kinds,
                self._digests,
                self._sequences,
            )
        )


def _compact_index_resident_bytes(
    entries: _JournalEntryIndex,
    index: _LogicalKeyIndex,
    recovered_sequences: bytearray,
) -> int:
    return entries.resident_bytes() + index.resident_bytes() + getsizeof(recovered_sequences)


def _require_index_resident_budget(resident_bytes: int) -> None:
    if resident_bytes > MAX_AUDIT_INDEX_RESIDENT_BYTES:
        raise _audit_error(
            OutcomeCode.OUT_OF_RANGE,
            "audit reopen index exceeds the 256 MiB resident-memory bound",
        )


@final
class PosixAuditRecoveryRecordSource:
    """Repeatable O(1)-materialization view over one verified journal prefix."""

    __slots__ = ("_journal", "_record_count", "_verified_eof", "binding")

    def __init__(
        self,
        journal: PosixAuditJournal,
        *,
        record_count: int | None = None,
    ) -> None:
        self._journal = journal
        available = len(journal._entries)
        selected = available if record_count is None else record_count
        if type(selected) is not int or not 1 <= selected <= available:
            raise _audit_error(
                OutcomeCode.OUT_OF_RANGE,
                "audit recovery prefix count is outside the verified journal",
            )
        self._record_count = selected
        entry = journal._entries[selected - 1]
        self._verified_eof = entry.offset + entry.frame_length
        self.binding = journal.binding

    @property
    def record_count(self) -> int:
        return self._record_count

    def __iter__(self) -> Iterator[AuditRecord]:
        self._require_readable_prefix()
        journal = self._journal
        for index in range(self._record_count):
            yield journal._read_entry_record(journal._entries[index])

    def prefix(self, record_count: int) -> PosixAuditRecoveryRecordSource:
        if type(record_count) is not int or not 1 <= record_count <= self._record_count:
            raise _audit_error(
                OutcomeCode.OUT_OF_RANGE,
                "audit recovery sub-prefix count is outside the verified prefix",
            )
        return PosixAuditRecoveryRecordSource(self._journal, record_count=record_count)

    def record_at(self, index: int) -> AuditRecord:
        if type(index) is not int:
            raise _audit_error(OutcomeCode.INVALID_TYPE, "audit recovery index must be exact int")
        if not 0 <= index < self._record_count:
            raise _audit_error(OutcomeCode.OUT_OF_RANGE, "audit recovery index is out of range")
        self._require_readable_prefix()
        return self._journal._read_entry_record(self._journal._entries[index])

    def resolve_record(self, logical_key: AuditLogicalKey) -> AuditRecord | None:
        if type(logical_key) is not AuditLogicalKey:
            raise _audit_error(OutcomeCode.INVALID_TYPE, "audit recovery key must be exact")
        self._require_readable_prefix()
        sequence = self._journal._index.get(logical_key)
        if sequence is None or sequence > self._record_count:
            return None
        return self._journal._read_entry_record(self._journal._entries[sequence - 1])

    def _require_readable_prefix(self) -> None:
        journal = self._journal
        if journal._closed or journal._failed or journal._needs_rescan:
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "audit recovery record source is no longer readable",
            )
        file_stat = journal._require_file_identity()
        if (
            file_stat.st_size < self._verified_eof
            or len(journal._entries) < self._record_count
            or (
                self._record_count > 0
                and journal._entries[self._record_count - 1].offset
                + journal._entries[self._record_count - 1].frame_length
                != self._verified_eof
            )
        ):
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                "audit recovery record prefix changed",
            )


@final
class PosixAuditJournal:
    """One serialized journal owner bound to an opaque prepared attempt."""

    __slots__ = (
        "_audit_fd",
        "_ack_cache",
        "_authority",
        "_binding",
        "_closed",
        "_entries",
        "_failed",
        "_index",
        "_index_resident_bytes",
        "_journal_fd",
        "_journal_identity",
        "_lock",
        "_needs_rescan",
        "_ops",
        "_recovered_sequences",
        "_terminal",
        "_verified_eof",
    )

    def __init__(
        self,
        *,
        authority: object,
        binding: RunBinding,
        audit_fd: int,
        journal_fd: int,
        journal_identity: tuple[int, int],
        ops: _AuditOps,
    ) -> None:
        self._authority = authority
        self._binding = binding
        self._ack_cache: WeakValueDictionary[int, AuditAppendAcknowledgement] = (
            WeakValueDictionary()
        )
        self._audit_fd = audit_fd
        self._journal_fd = journal_fd
        self._journal_identity = journal_identity
        self._ops = ops
        self._lock = Lock()
        self._entries = _JournalEntryIndex()
        self._index = _LogicalKeyIndex()
        self._recovered_sequences = bytearray()
        self._index_resident_bytes = _compact_index_resident_bytes(
            self._entries, self._index, self._recovered_sequences
        )
        self._verified_eof = len(AUDIT_JOURNAL_PREAMBLE)
        self._needs_rescan = False
        self._failed = False
        self._terminal = False
        self._closed = False

    @property
    def binding(self) -> RunBinding:
        return self._binding

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._read_entry_record(entry) for entry in self._entries)

    @property
    def recovery_records(self) -> PosixAuditRecoveryRecordSource:
        """Return a repeatable prefix stream without materializing every AuditRecord."""
        if self._closed or self._failed or self._needs_rescan:
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "audit recovery record source is unavailable",
            )
        return PosixAuditRecoveryRecordSource(self)

    @property
    def terminal(self) -> bool:
        return self._terminal

    def _read_entry_record(self, entry: _JournalEntry) -> AuditRecord:
        frame = _pread_exact(self._ops, self._journal_fd, entry.frame_length, entry.offset)
        if len(frame) != entry.frame_length:
            raise _audit_error(OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH, "audit frame is short")
        header_length = int.from_bytes(frame[:8], "big")
        payload_length_offset = 8 + header_length
        payload_length = int.from_bytes(
            frame[payload_length_offset : payload_length_offset + 8], "big"
        )
        payload_offset = payload_length_offset + 8
        payload_end = payload_offset + payload_length
        checksum = frame[payload_end:]
        if (
            payload_length != entry.payload_length
            or len(checksum) != 32
            or checksum != sha256(AUDIT_FRAME_DIGEST_DOMAIN + frame[:payload_end]).digest()
        ):
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                "audit frame compact index conflicts",
            )
        payload = frame[payload_offset:payload_end]
        if sha256(payload).digest() != entry.payload_sha256:
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                "audit payload compact index conflicts",
            )
        record = decode_audit_record(
            binding=self._binding,
            canonical_header=frame[8 : 8 + header_length],
            canonical_payload=payload,
        )
        if (
            bytes.fromhex(audit_record_digest(record).value) != entry.record_sha256
            or bytes.fromhex(audit_chain_head(record).value) != entry.chain_head_sha256
        ):
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                "audit record compact index conflicts",
            )
        return record

    def _require_file_identity(self) -> os.stat_result:
        try:
            value = self._ops.fstat(self._journal_fd)
        except OSError as error:
            raise _audit_error(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "audit journal identity could not be rebound",
            ) from error
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
            or (value.st_dev, value.st_ino) != self._journal_identity
            or value.st_size > MAX_AUDIT_JOURNAL_BYTES
        ):
            raise _audit_error(
                OutcomeCode.CONFLICTING_ID,
                "audit journal identity, mode, link count or bound changed",
            )
        return value

    def _rescan(self, *, permit_torn_tail: bool) -> None:
        file_stat = self._require_file_identity()
        size = file_stat.st_size
        preamble = _pread_exact(self._ops, self._journal_fd, len(AUDIT_JOURNAL_PREAMBLE), 0)
        if preamble != AUDIT_JOURNAL_PREAMBLE:
            raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit journal preamble is invalid")
        entries = _JournalEntryIndex()
        index = _LogicalKeyIndex()
        recovered_sequences = bytearray()
        resident_bytes = _compact_index_resident_bytes(entries, index, recovered_sequences)
        terminal = False
        offset = len(AUDIT_JOURNAL_PREAMBLE)
        torn_offset: int | None = None
        previous_record = EMPTY_RECORD_SHA256
        previous_chain = EMPTY_CHAIN_HEAD_SHA256
        while offset < size:
            frame_start = offset
            raw_header_length = _pread_exact(self._ops, self._journal_fd, 8, offset)
            if len(raw_header_length) < 8:
                torn_offset = frame_start
                break
            header_length = int.from_bytes(raw_header_length, "big")
            if not 1 <= header_length <= MAX_AUDIT_HEADER_BYTES:
                raise _audit_error(
                    OutcomeCode.CONFLICTING_ID, "audit frame header length is invalid"
                )
            offset += 8
            header = _pread_exact(self._ops, self._journal_fd, header_length, offset)
            if len(header) < header_length:
                torn_offset = frame_start
                break
            offset += header_length
            raw_payload_length = _pread_exact(self._ops, self._journal_fd, 8, offset)
            if len(raw_payload_length) < 8:
                torn_offset = frame_start
                break
            payload_length = int.from_bytes(raw_payload_length, "big")
            if not 1 <= payload_length <= MAX_LARGE_AUDIT_PAYLOAD_BYTES:
                raise _audit_error(
                    OutcomeCode.CONFLICTING_ID, "audit frame payload length is invalid"
                )
            offset += 8
            payload = _pread_exact(self._ops, self._journal_fd, payload_length, offset)
            if len(payload) < payload_length:
                torn_offset = frame_start
                break
            offset += payload_length
            checksum = _pread_exact(self._ops, self._journal_fd, 32, offset)
            if len(checksum) < 32:
                torn_offset = frame_start
                break
            offset += 32
            framed = raw_header_length + header + raw_payload_length + payload
            if checksum != sha256(AUDIT_FRAME_DIGEST_DOMAIN + framed).digest():
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit frame checksum is invalid")
            try:
                record = decode_audit_record(
                    binding=self._binding,
                    canonical_header=header,
                    canonical_payload=payload,
                )
            except AuditContractError as error:
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit frame is invalid") from error
            expected_sequence = len(entries) + 1
            if expected_sequence > MAX_AUDIT_RECORDS:
                raise _audit_error(OutcomeCode.OUT_OF_RANGE, "audit record count exceeds v1 bound")
            if (
                record.record_id.owner_sequence != expected_sequence
                or record.previous_record_sha256 != previous_record
                or record.previous_chain_head_sha256 != previous_chain
                or terminal
                or index.get(record.logical_key) is not None
            ):
                raise _audit_error(
                    OutcomeCode.CONFLICTING_ID, "audit record sequence or chain conflicts"
                )
            record_sha256 = audit_record_digest(record)
            chain_head_sha256 = audit_chain_head(record)
            entry = _JournalEntry(
                offset=frame_start,
                frame_length=offset - frame_start,
                payload_length=payload_length,
                payload_sha256=sha256(payload).digest(),
                record_sha256=bytes.fromhex(record_sha256.value),
                chain_head_sha256=bytes.fromhex(chain_head_sha256.value),
            )
            entries.append(entry)
            index.insert(record.logical_key, expected_sequence)
            recovered_sequences.append(1)
            resident_bytes = _compact_index_resident_bytes(entries, index, recovered_sequences)
            _require_index_resident_budget(resident_bytes)
            previous_record = record_sha256
            previous_chain = chain_head_sha256
            terminal = record.record_kind is AuditRecordKind.RUN_TERMINAL
        if torn_offset is not None:
            if not permit_torn_tail:
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                    "audit journal has an incomplete final frame",
                )
            try:
                self._ops.ftruncate(self._journal_fd, torn_offset)
                self._ops.fsync(self._journal_fd)
                self._ops.fsync(self._audit_fd)
            except OSError as error:
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                    "audit torn-tail recovery did not become durable",
                ) from error
            offset = torn_offset
        self._entries = entries
        self._index = index
        self._recovered_sequences = recovered_sequences
        self._index_resident_bytes = resident_bytes
        self._terminal = terminal
        self._verified_eof = offset
        self._needs_rescan = False

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
        spec_set: InstrumentExecutionSpecSet | None = None,
    ) -> AuditAppendAcknowledgement:
        """Append one record; reconciliation outcomes get strict codec
        admission when the writer supplies the bound spec-set (SEC-001)."""
        if not self._lock.acquire(blocking=False):
            raise _audit_error(
                OutcomeCode.CONFLICTING_ID, "audit append is concurrent or reentrant"
            )
        fsync_started = False
        try:
            if self._closed:
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit journal is closed")
            if self._failed:
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit journal is in a monotone failed state",
                )
            logical_key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
            existing_sequence = self._index.get(logical_key)
            if existing_sequence is not None:
                entry = self._entries[existing_sequence - 1]
                if (
                    entry.payload_length != len(canonical_payload)
                    or entry.payload_sha256 != sha256(canonical_payload).digest()
                ):
                    self._failed = True
                    raise _audit_error(
                        OutcomeCode.CONFLICTING_ID,
                        "audit logical retry conflicts with the original payload",
                    )
                if (
                    existing_sequence <= len(self._recovered_sequences)
                    and self._recovered_sequences[existing_sequence - 1]
                ):
                    try:
                        self._ops.fsync(self._journal_fd)
                        record = self._read_entry_record(entry)
                    except (AuditContractError, OSError) as error:
                        self._failed = True
                        raise _audit_error(
                            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                            "recovered audit frame did not revalidate",
                        ) from error
                    self._recovered_sequences[existing_sequence - 1] = 0
                else:
                    record = self._read_entry_record(entry)
                acknowledgement = self._ack_cache.get(existing_sequence)
                if acknowledgement is None:
                    acknowledgement = create_audit_append_acknowledgement(record)
                    self._ack_cache[existing_sequence] = acknowledgement
                return require_audit_acknowledgement(
                    acknowledgement,
                    binding=self._binding,
                    logical_key=logical_key,
                    canonical_payload=canonical_payload,
                )
            if self._terminal:
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "terminal audit journal is closed")
            if self._needs_rescan:
                self._rescan(permit_torn_tail=True)
                existing_sequence = self._index.get(logical_key)
                if existing_sequence is not None:
                    entry = self._entries[existing_sequence - 1]
                    if (
                        entry.payload_length != len(canonical_payload)
                        or entry.payload_sha256 != sha256(canonical_payload).digest()
                    ):
                        self._failed = True
                        raise _audit_error(
                            OutcomeCode.CONFLICTING_ID,
                            "recovered audit retry conflicts with original payload",
                        )
                    try:
                        self._ops.fsync(self._journal_fd)
                        record = self._read_entry_record(entry)
                    except (AuditContractError, OSError) as error:
                        self._failed = True
                        raise _audit_error(
                            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                            "recovered audit frame did not revalidate",
                        ) from error
                    if existing_sequence <= len(self._recovered_sequences):
                        self._recovered_sequences[existing_sequence - 1] = 0
                    acknowledgement = create_audit_append_acknowledgement(record)
                    self._ack_cache[existing_sequence] = acknowledgement
                    return require_audit_acknowledgement(
                        acknowledgement,
                        binding=self._binding,
                        logical_key=logical_key,
                        canonical_payload=canonical_payload,
                    )
            file_stat = self._require_file_identity()
            if file_stat.st_size != self._verified_eof:
                self._failed = True
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit EOF changed unexpectedly")
            previous_record = (
                EMPTY_RECORD_SHA256
                if not self._entries
                else Sha256Digest(self._entries[-1].record_sha256.hex())
            )
            previous_chain = (
                EMPTY_CHAIN_HEAD_SHA256
                if not self._entries
                else Sha256Digest(self._entries[-1].chain_head_sha256.hex())
            )
            record = create_audit_record(
                binding=self._binding,
                owner_sequence=len(self._entries) + 1,
                record_kind=record_kind,
                subject_kind=subject_kind,
                subject_sha256=subject_sha256,
                canonical_payload=canonical_payload,
                previous_record_sha256=previous_record,
                previous_chain_head_sha256=previous_chain,
                spec_set=spec_set,
            )
            frame = _frame_bytes(record)
            if self._verified_eof + len(frame) > MAX_AUDIT_JOURNAL_BYTES:
                raise _audit_error(
                    OutcomeCode.OUT_OF_RANGE, "audit journal hard bound would be crossed"
                )
            start = self._verified_eof
            entry = _JournalEntry(
                offset=start,
                frame_length=len(frame),
                payload_length=len(canonical_payload),
                payload_sha256=sha256(canonical_payload).digest(),
                record_sha256=bytes.fromhex(audit_record_digest(record).value),
                chain_head_sha256=bytes.fromhex(audit_chain_head(record).value),
            )
            self._index.reserve_for_insert()
            self._entries.append(entry)
            projected_resident_bytes = _compact_index_resident_bytes(
                self._entries, self._index, self._recovered_sequences
            )
            self._entries.pop()
            _require_index_resident_budget(projected_resident_bytes)
            try:
                _write_all(self._ops, self._journal_fd, frame, start)
                self._ops.fsync(self._journal_fd)
                fsync_started = True
                read_back = _pread_exact(self._ops, self._journal_fd, len(frame), start)
                if read_back != frame:
                    raise _ObservedAuditMismatch("audit frame read-back mismatch")
                verified = decode_audit_record(
                    binding=self._binding,
                    canonical_header=canonical_audit_record_header_bytes(record),
                    canonical_payload=record.canonical_payload,
                )
                if audit_record_digest(verified) != audit_record_digest(record) or audit_chain_head(
                    verified
                ) != audit_chain_head(record):
                    raise _ObservedAuditMismatch("audit frame reconstruction mismatch")
            except AuditContractError as error:
                self._failed = True
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit frame reconstruction failed",
                ) from error
            except _ObservedAuditMismatch as error:
                self._failed = True
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit frame readback or integrity evidence mismatched",
                ) from error
            except OSError as error:
                self._needs_rescan = True
                code = (
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH
                    if fsync_started
                    else OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
                )
                raise _audit_error(
                    code, "audit append did not produce verified durability"
                ) from error
            acknowledgement = create_audit_append_acknowledgement(record)
            self._ack_cache[record.record_id.owner_sequence] = acknowledgement
            self._entries.append(entry)
            self._index.insert(logical_key, record.record_id.owner_sequence)
            self._index_resident_bytes = _compact_index_resident_bytes(
                self._entries, self._index, self._recovered_sequences
            )
            self._verified_eof += len(frame)
            self._terminal = record_kind is AuditRecordKind.RUN_TERMINAL
            return acknowledgement
        finally:
            self._lock.release()

    def settle_append(
        self,
        *,
        logical_key: AuditLogicalKey,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        """Rescan and independently revalidate one uncertain exact append."""
        if not self._lock.acquire(blocking=False):
            raise _audit_error(
                OutcomeCode.CONFLICTING_ID,
                "audit settlement is concurrent or reentrant",
            )
        try:
            if type(logical_key) is not AuditLogicalKey or type(canonical_payload) is not bytes:
                raise _audit_error(
                    OutcomeCode.INVALID_TYPE,
                    "audit settlement inputs must be exact",
                )
            if self._closed:
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit journal is closed")
            if self._failed:
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit journal is in a monotone failed state",
                )
            if self._needs_rescan:
                try:
                    self._rescan(permit_torn_tail=True)
                except AuditContractError as error:
                    if error.code is not OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED:
                        self._failed = True
                    else:
                        self._needs_rescan = True
                    raise
                except OSError as error:
                    self._needs_rescan = True
                    raise _audit_error(
                        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                        "audit settlement rescan could not complete",
                    ) from error
            sequence = self._index.get(logical_key)
            if sequence is None:
                try:
                    self._ops.fsync(self._journal_fd)
                except OSError as error:
                    self._needs_rescan = True
                    raise _audit_error(
                        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                        "audit absence could not be settled durably",
                    ) from error
                try:
                    file_stat = self._require_file_identity()
                except AuditContractError as error:
                    if error.code is not OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED:
                        self._failed = True
                    else:
                        self._needs_rescan = True
                    raise
                if file_stat.st_size != self._verified_eof:
                    self._needs_rescan = True
                    raise _audit_error(
                        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                        "audit absence changed during settlement",
                    )
                return None
            entry = self._entries[sequence - 1]
            if (
                entry.payload_length != len(canonical_payload)
                or entry.payload_sha256 != sha256(canonical_payload).digest()
            ):
                self._failed = True
                raise _audit_error(
                    OutcomeCode.CONFLICTING_ID,
                    "settled audit retry conflicts with original payload",
                )
            try:
                self._ops.fsync(self._journal_fd)
            except OSError as error:
                self._needs_rescan = True
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "settled audit frame could not be read durably",
                ) from error
            try:
                record = self._read_entry_record(entry)
            except AuditContractError as error:
                self._failed = True
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "settled audit frame integrity mismatched",
                ) from error
            except OSError as error:
                self._needs_rescan = True
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "settled audit frame could not be read durably",
                ) from error
            acknowledgement = create_audit_append_acknowledgement(record)
            try:
                acknowledgement = require_audit_acknowledgement(
                    acknowledgement,
                    binding=self._binding,
                    logical_key=logical_key,
                    canonical_payload=canonical_payload,
                )
            except AuditContractError:
                self._failed = True
                raise
            if sequence <= len(self._recovered_sequences):
                self._recovered_sequences[sequence - 1] = 0
            self._ack_cache[sequence] = acknowledgement
            return acknowledgement
        finally:
            self._lock.release()

    def close(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit close is concurrent or reentrant")
        try:
            if self._closed:
                return
            self._closed = True
            for descriptor in (self._journal_fd, self._audit_fd):
                with suppress(OSError):
                    self._ops.close(descriptor)
        finally:
            self._lock.release()


def create_posix_audit_journal(
    prepared: AuditRunBinding,
    *,
    _ops: _AuditOps | None = None,
) -> PosixAuditJournal:
    """Create, durably initialize and admit one fresh prepared audit journal."""
    if type(prepared) is not AuditRunBinding:
        raise StoreError("audit journal requires an exact AuditRunBinding")
    ops = _ops or _OsAuditOps()
    audit_fd: int | None = None
    journal_fd: int | None = None
    try:
        audit_fd, _ = prepared._store._open_audit_directory(prepared)
        filesystem = ops.fstatvfs(audit_fd)
        if filesystem.f_bavail * filesystem.f_frsize < MIN_AUDIT_FILESYSTEM_FREE_BYTES:
            raise StoreError("audit filesystem has less than the required 13 GiB free")
        journal_fd = ops.create_journal(audit_fd)
        value = ops.fstat(journal_fd)
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
        ):
            raise StoreError("audit journal must be one regular 0600 file")
        _write_all(ops, journal_fd, AUDIT_JOURNAL_PREAMBLE, 0)
        ops.fsync(journal_fd)
        ops.fsync(audit_fd)
        journal = PosixAuditJournal(
            authority=prepared._authority,
            binding=prepared.binding,
            audit_fd=audit_fd,
            journal_fd=journal_fd,
            journal_identity=(value.st_dev, value.st_ino),
            ops=ops,
        )
        journal.append(
            record_kind=AuditRecordKind.RUN_PREPARED,
            subject_kind=AuditSubjectKind.RUN_MANIFEST,
            subject_sha256=prepared.binding.manifest_sha256,
            canonical_payload=canonical_run_prepared_audit_payload(prepared.binding),
        )
        audit_fd = None
        journal_fd = None
        return journal
    except (AuditContractError, StoreError):
        raise
    except (FileExistsError, OSError) as error:
        raise StoreError("audit journal could not be durably initialized") from error
    finally:
        for descriptor in (journal_fd, audit_fd):
            if descriptor is not None:
                with suppress(OSError):
                    ops.close(descriptor)


def reopen_posix_audit_journal(
    prepared: AuditRunBinding,
    *,
    _ops: _AuditOps | None = None,
) -> PosixAuditJournal:
    """Reopen, linearly verify and recover only a mechanically torn final suffix."""
    if type(prepared) is not AuditRunBinding:
        raise StoreError("audit journal reopen requires an exact AuditRunBinding")
    ops = _ops or _OsAuditOps()
    audit_fd: int | None = None
    journal_fd: int | None = None
    try:
        audit_fd, _ = prepared._store._open_audit_directory(prepared)
        filesystem = ops.fstatvfs(audit_fd)
        if filesystem.f_bavail * filesystem.f_frsize < MIN_AUDIT_FILESYSTEM_FREE_BYTES:
            raise StoreError("audit filesystem has less than the required 13 GiB free")
        journal_fd = ops.open_journal(audit_fd)
        value = ops.fstat(journal_fd)
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
        ):
            raise StoreError("reopened audit journal must be one regular 0600 file")
        journal = PosixAuditJournal(
            authority=prepared._authority,
            binding=prepared.binding,
            audit_fd=audit_fd,
            journal_fd=journal_fd,
            journal_identity=(value.st_dev, value.st_ino),
            ops=ops,
        )
        journal._rescan(permit_torn_tail=True)
        prepared_payload = canonical_run_prepared_audit_payload(prepared.binding)
        if journal._entries:
            first_record = journal._read_entry_record(journal._entries[0])
            if (
                first_record.record_kind is not AuditRecordKind.RUN_PREPARED
                or first_record.subject_kind is not AuditSubjectKind.RUN_MANIFEST
                or first_record.subject_sha256 != prepared.binding.manifest_sha256
                or first_record.canonical_payload != prepared_payload
            ):
                journal._failed = True
                raise _audit_error(
                    OutcomeCode.CONFLICTING_ID,
                    "audit journal does not begin with the exact run preparation",
                )
        journal.append(
            record_kind=AuditRecordKind.RUN_PREPARED,
            subject_kind=AuditSubjectKind.RUN_MANIFEST,
            subject_sha256=prepared.binding.manifest_sha256,
            canonical_payload=prepared_payload,
        )
        audit_fd = None
        journal_fd = None
        return journal
    except (AuditContractError, StoreError):
        raise
    except OSError as error:
        raise StoreError("audit journal could not be reopened") from error
    finally:
        for descriptor in (journal_fd, audit_fd):
            if descriptor is not None:
                with suppress(OSError):
                    ops.close(descriptor)
