"""Read and verify a fixed Paper journal without acquiring or repairing it."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

from ea.core.audit import (
    AUDIT_FRAME_DIGEST_DOMAIN,
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    MAX_AUDIT_HEADER_BYTES,
    MAX_AUDIT_RECORDS,
    MAX_LARGE_AUDIT_PAYLOAD_BYTES,
    AuditContractError,
    AuditRecord,
    AuditRecordKind,
    AuditSubjectKind,
    audit_chain_head,
    audit_record_digest,
    decode_audit_record,
)
from ea.core.run import RunBinding, Sha256Digest
from ea.experiments.audit import AUDIT_JOURNAL_PREAMBLE

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class CapturedJournalError(ValueError):
    """Captured frames conflict with their recorded bytes or binding."""


def _read_exact(descriptor: int, size: int, offset: int) -> bytes:
    """Read one exact byte range, or refuse: captured evidence has no torn tail."""
    chunk = os.pread(descriptor, size, offset)
    if len(chunk) != size:
        raise CapturedJournalError("captured journal is truncated")
    return chunk


def verified_journal_records(
    path: Path,
    binding: RunBinding,
    *,
    size_bytes: int,
    expected_sha256: Sha256Digest,
    expected_head: Sha256Digest | None = None,
) -> Iterator[AuditRecord]:
    """Consume to EOF to verify every frame, the file digest and optional running boundary."""
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size_bytes:
            raise CapturedJournalError("captured journal size conflicts")
        size = info.st_size
        hasher = sha256()
        preamble = _read_exact(descriptor, len(AUDIT_JOURNAL_PREAMBLE), 0)
        if preamble != AUDIT_JOURNAL_PREAMBLE:
            raise CapturedJournalError("captured journal preamble is invalid")
        hasher.update(preamble)
        offset = len(AUDIT_JOURNAL_PREAMBLE)
        count = 0
        previous_record = EMPTY_RECORD_SHA256
        previous_chain = EMPTY_CHAIN_HEAD_SHA256
        terminal_seen = False
        while offset < size:
            raw_header_length = _read_exact(descriptor, 8, offset)
            header_length = int.from_bytes(raw_header_length, "big")
            if not 1 <= header_length <= MAX_AUDIT_HEADER_BYTES:
                raise CapturedJournalError("captured journal header bound conflicts")
            offset += 8
            header = _read_exact(descriptor, header_length, offset)
            offset += header_length
            raw_payload_length = _read_exact(descriptor, 8, offset)
            payload_length = int.from_bytes(raw_payload_length, "big")
            if not 1 <= payload_length <= MAX_LARGE_AUDIT_PAYLOAD_BYTES:
                raise CapturedJournalError("captured journal payload bound conflicts")
            offset += 8
            payload = _read_exact(descriptor, payload_length, offset)
            offset += payload_length
            checksum = _read_exact(descriptor, 32, offset)
            offset += 32
            framed = raw_header_length + header + raw_payload_length + payload
            if checksum != sha256(AUDIT_FRAME_DIGEST_DOMAIN + framed).digest():
                raise CapturedJournalError("captured journal checksum conflicts")
            try:
                record = decode_audit_record(
                    binding=binding, canonical_header=header, canonical_payload=payload
                )
            except AuditContractError as error:
                raise CapturedJournalError("captured frame conflicts") from error
            count += 1
            if (
                record.record_id.owner_sequence != count
                or count > MAX_AUDIT_RECORDS
                or record.previous_record_sha256 != previous_record
                or record.previous_chain_head_sha256 != previous_chain
                or terminal_seen
            ):
                raise CapturedJournalError("captured journal chain conflicts")
            if count == 1 and (
                record.record_kind is not AuditRecordKind.RUN_PREPARED
                or record.subject_kind is not AuditSubjectKind.RUN_MANIFEST
                or record.subject_sha256 != binding.manifest_sha256
            ):
                raise CapturedJournalError("captured journal does not begin with preparation")
            previous_record = audit_record_digest(record)
            previous_chain = audit_chain_head(record)
            terminal_seen = record.record_kind is AuditRecordKind.RUN_TERMINAL
            yield record
            hasher.update(framed)
            hasher.update(checksum)
        if offset != size or count == 0:
            raise CapturedJournalError("captured journal is not a complete journal")
        if Sha256Digest(hasher.hexdigest()) != expected_sha256:
            raise CapturedJournalError("captured journal does not match its digest")
        if expected_head is not None and (terminal_seen or previous_chain != expected_head):
            raise CapturedJournalError("journal does not match the running writer checkpoint")
    finally:
        if descriptor is not None:
            os.close(descriptor)
