"""Concrete POSIX v1 append-only audit journal from Accepted ADR 0020."""

from __future__ import annotations

import os
import stat
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
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, Sha256Digest
from ea.experiments.store import AuditRunBinding, StoreError

AUDIT_JOURNAL_PREAMBLE = b"EA-AUDIT-V1\n"
AUDIT_JOURNAL_NAME = "audit-v1.journal"
MAX_AUDIT_JOURNAL_BYTES = 6 * 1024 * 1024 * 1024
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


def _compact_logical_key(value: AuditLogicalKey) -> bytes:
    return (
        value.record_kind.value.encode("ascii")
        + b"\0"
        + value.subject_kind.value.encode("ascii")
        + b"\0"
        + bytes.fromhex(value.subject_sha256.value)
    )


def _entry_resident_bytes(entry: _JournalEntry) -> int:
    return (
        getsizeof(entry)
        + getsizeof(entry.offset)
        + getsizeof(entry.frame_length)
        + getsizeof(entry.payload_length)
        + getsizeof(entry.payload_sha256)
        + getsizeof(entry.record_sha256)
        + getsizeof(entry.chain_head_sha256)
    )


def _require_index_resident_budget(resident_bytes: int) -> None:
    if resident_bytes > MAX_AUDIT_INDEX_RESIDENT_BYTES:
        raise _audit_error(
            OutcomeCode.OUT_OF_RANGE,
            "audit reopen index exceeds the 256 MiB resident-memory bound",
        )


@final
class PosixAuditJournal:
    """One serialized journal owner bound to an opaque prepared attempt."""

    __slots__ = (
        "_audit_fd",
        "_ack_cache",
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
        binding: RunBinding,
        audit_fd: int,
        journal_fd: int,
        journal_identity: tuple[int, int],
        ops: _AuditOps,
    ) -> None:
        self._binding = binding
        self._ack_cache: WeakValueDictionary[int, AuditAppendAcknowledgement] = (
            WeakValueDictionary()
        )
        self._audit_fd = audit_fd
        self._journal_fd = journal_fd
        self._journal_identity = journal_identity
        self._ops = ops
        self._lock = Lock()
        self._entries: list[_JournalEntry] = []
        self._index: dict[bytes, int] = {}
        self._recovered_sequences = bytearray()
        self._index_resident_bytes = (
            getsizeof(self._entries) + getsizeof(self._index) + getsizeof(self._recovered_sequences)
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
        entries: list[_JournalEntry] = []
        index: dict[bytes, int] = {}
        resident_bytes = getsizeof(entries) + getsizeof(index) + getsizeof(bytearray())
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
            compact_key = _compact_logical_key(record.logical_key)
            if (
                record.record_id.owner_sequence != expected_sequence
                or record.previous_record_sha256 != previous_record
                or record.previous_chain_head_sha256 != previous_chain
                or terminal
                or compact_key in index
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
            previous_container_bytes = getsizeof(entries) + getsizeof(index)
            entries.append(entry)
            index[compact_key] = expected_sequence
            resident_bytes += (
                getsizeof(entries)
                + getsizeof(index)
                - previous_container_bytes
                + _entry_resident_bytes(entry)
                + getsizeof(compact_key)
                + getsizeof(expected_sequence)
                + 1
            )
            _require_index_resident_budget(resident_bytes)
            previous_record = record_sha256
            previous_chain = chain_head_sha256
            terminal = record.record_kind is AuditRecordKind.RUN_TERMINAL
            if len(entries) > MAX_AUDIT_RECORDS:
                raise _audit_error(OutcomeCode.OUT_OF_RANGE, "audit record count exceeds v1 bound")
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
        recovered_sequences = bytearray(b"\x01") * len(entries)
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
    ) -> AuditAppendAcknowledgement:
        if not self._lock.acquire(blocking=False):
            raise _audit_error(
                OutcomeCode.CONFLICTING_ID, "audit append is concurrent or reentrant"
            )
        fsync_started = False
        try:
            if self._closed:
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "audit journal is closed")
            logical_key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
            compact_key = _compact_logical_key(logical_key)
            existing_sequence = self._index.get(compact_key)
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
            if self._failed:
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit journal is in a monotone failed state",
                )
            if self._terminal:
                raise _audit_error(OutcomeCode.CONFLICTING_ID, "terminal audit journal is closed")
            if self._needs_rescan:
                self._rescan(permit_torn_tail=True)
                existing_sequence = self._index.get(compact_key)
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
            previous_container_bytes = getsizeof(self._entries) + getsizeof(self._index)
            self._entries.append(entry)
            self._index[compact_key] = record.record_id.owner_sequence
            expanded_container_bytes = getsizeof(self._entries) + getsizeof(self._index)
            self._entries.pop()
            del self._index[compact_key]
            retained_container_bytes = getsizeof(self._entries) + getsizeof(self._index)
            self._index_resident_bytes += retained_container_bytes - previous_container_bytes
            projected_resident_bytes = (
                self._index_resident_bytes
                + expanded_container_bytes
                - retained_container_bytes
                + _entry_resident_bytes(entry)
                + getsizeof(compact_key)
                + getsizeof(record.record_id.owner_sequence)
            )
            _require_index_resident_budget(projected_resident_bytes)
            try:
                _write_all(self._ops, self._journal_fd, frame, start)
                self._ops.fsync(self._journal_fd)
                fsync_started = True
                read_back = _pread_exact(self._ops, self._journal_fd, len(frame), start)
                if read_back != frame:
                    raise OSError("audit frame read-back mismatch")
                verified = decode_audit_record(
                    binding=self._binding,
                    canonical_header=canonical_audit_record_header_bytes(record),
                    canonical_payload=record.canonical_payload,
                )
                if audit_record_digest(verified) != audit_record_digest(record) or audit_chain_head(
                    verified
                ) != audit_chain_head(record):
                    raise OSError("audit frame reconstruction mismatch")
            except AuditContractError as error:
                self._failed = True
                raise _audit_error(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit frame reconstruction failed",
                ) from error
            except OSError as error:
                if fsync_started:
                    self._failed = True
                    code = OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH
                else:
                    self._needs_rescan = True
                    code = OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
                raise _audit_error(
                    code, "audit append did not produce verified durability"
                ) from error
            acknowledgement = create_audit_append_acknowledgement(record)
            self._ack_cache[record.record_id.owner_sequence] = acknowledgement
            self._entries.append(entry)
            self._index[compact_key] = record.record_id.owner_sequence
            self._index_resident_bytes = projected_resident_bytes
            self._verified_eof += len(frame)
            self._terminal = record_kind is AuditRecordKind.RUN_TERMINAL
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
            raise StoreError("audit filesystem has less than the required 6 GiB free")
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
        journal_fd = ops.open_journal(audit_fd)
        value = ops.fstat(journal_fd)
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
        ):
            raise StoreError("reopened audit journal must be one regular 0600 file")
        journal = PosixAuditJournal(
            binding=prepared.binding,
            audit_fd=audit_fd,
            journal_fd=journal_fd,
            journal_identity=(value.st_dev, value.st_ino),
            ops=ops,
        )
        journal._rescan(permit_torn_tail=True)
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
