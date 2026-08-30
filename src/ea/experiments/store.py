"""Atomic local result ownership and durable write-once manifest publication."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID

from ea.core.audit import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditRecord,
    AuditRecordKind,
    AuditRecoveryRecordSource,
    AuditSubjectKind,
    audit_frame_checksum,
    canonical_audit_record_bytes,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
)
from ea.core.run import RunBinding, RunContractError, RunId, RunReference, Sha256Digest
from ea.experiments.manifest import (
    LineageSpec,
    LineageSpecV2,
    RunManifest,
    RunManifestV2,
    build_manifest,
    build_manifest_v2,
    canonical_manifest_bytes,
    read_manifest,
)

_CAPABILITY_SEAL = object()
_BINDING_SEAL = object()
_PREPARED_SEAL = object()
_RECOVERY_SEAL = object()
_RECOVERED_SEAL = object()
_RECOVERED_TERMINAL_SEAL = object()


class StoreError(RuntimeError):
    """Raised when a result attempt cannot be prepared durably."""


class StoreCollisionError(StoreError):
    """Raised when an attempt path already exists and is never adopted."""


class IncompleteAuditRecoveryError(StoreError):
    """Raised when recovery has no durable first run-prepared record."""


class CorruptAuditRecoveryError(StoreError):
    """Raised when recovery's durable first run-prepared record is altered."""


class RunIdProvider(Protocol):
    def __call__(self) -> UUID:
        """Return one fresh operating-system/provider UUID4."""
        ...


class _StoreOps(Protocol):
    def open_root(self, path: Path) -> int: ...

    def mkdir_at(self, parent_fd: int, name: str, mode: int) -> None: ...

    def open_dir_at(self, parent_fd: int, name: str) -> int: ...

    def create_file_at(self, parent_fd: int, name: str, mode: int) -> int: ...

    def open_file_read_at(self, parent_fd: int, name: str) -> int: ...

    def write(self, file_fd: int, data: memoryview) -> int: ...

    def read(self, file_fd: int, size: int) -> bytes: ...

    def fsync(self, file_fd: int) -> None: ...

    def close(self, file_fd: int) -> None: ...

    def fstat(self, file_fd: int) -> os.stat_result: ...


class _RecoveryJournal(Protocol):
    def close(self) -> None: ...


class _OsStoreOps:
    def open_root(self, path: Path) -> int:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def mkdir_at(self, parent_fd: int, name: str, mode: int) -> None:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)

    def open_dir_at(self, parent_fd: int, name: str) -> int:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )

    def create_file_at(self, parent_fd: int, name: str, mode: int) -> int:
        return os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )

    def open_file_read_at(self, parent_fd: int, name: str) -> int:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)

    def write(self, file_fd: int, data: memoryview) -> int:
        return os.write(file_fd, data)

    def read(self, file_fd: int, size: int) -> bytes:
        return os.read(file_fd, size)

    def fsync(self, file_fd: int) -> None:
        os.fsync(file_fd)

    def close(self, file_fd: int) -> None:
        os.close(file_fd)

    def fstat(self, file_fd: int) -> os.stat_result:
        return os.fstat(file_fd)


@dataclass(frozen=True, slots=True)
class _AttemptAuthority:
    """Opaque store/attempt identity shared by that attempt's three capabilities."""

    store_id: object
    attempt_token: object
    binding: RunBinding


@dataclass(frozen=True, slots=True)
class _AttemptRecord:
    authority: _AttemptAuthority
    run_name: str
    root_identity: tuple[int, int]
    run_identity: tuple[int, int]
    manifest_identity: tuple[int, int]
    audit_identity: tuple[int, int]
    output_identity: tuple[int, int]
    writer_lock_identity: tuple[int, int]
    writer_lock_fd: int


class _AttemptCapability:
    """Immutable pathless registry key; concrete subclasses fix the allowed role."""

    __slots__ = ("_authority",)
    _label = "attempt"
    _authority: _AttemptAuthority

    def __init__(
        self,
        seal: object,
        authority: _AttemptAuthority | None = None,
    ) -> None:
        if seal is not _CAPABILITY_SEAL or type(authority) is not _AttemptAuthority:
            raise StoreError(f"{self._label} capabilities can only be issued by LocalResultStore")
        object.__setattr__(self, "_authority", authority)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{self._label} capability is immutable")

    def _matches(self, binding: RunBinding, authority: _AttemptAuthority) -> bool:
        return (
            self._authority is authority
            and authority.binding == binding
            and authority.binding.reference == binding.reference
        )


class AuditCapability(_AttemptCapability):
    """Authority restricted to one exact store-owned attempt's ``audit/`` child."""

    __slots__ = ()
    _label = "audit"


class OutputCapability(_AttemptCapability):
    """Authority restricted to one exact store-owned attempt's ``outputs/`` child."""

    __slots__ = ()
    _label = "output"


class ManifestVerificationCapability(_AttemptCapability):
    """Outer-only authority to re-open and verify one exact durable manifest."""

    __slots__ = ()
    _label = "manifest verification"


class AuditRunBinding:
    """Immutable association between one audit role and its exact attempt authority."""

    __slots__ = ("_authority", "_store", "binding", "capability")
    _authority: _AttemptAuthority
    _store: LocalResultStore
    binding: RunBinding
    capability: AuditCapability

    def __init__(
        self,
        *,
        binding: RunBinding,
        capability: AuditCapability,
        authority: _AttemptAuthority,
        seal: object,
        store: LocalResultStore | None = None,
    ) -> None:
        if (
            seal is not _BINDING_SEAL
            or type(binding) is not RunBinding
            or type(capability) is not AuditCapability
            or type(authority) is not _AttemptAuthority
            or type(store) is not LocalResultStore
            or not capability._matches(binding, authority)
            or authority.store_id is not store._store_id
        ):
            raise StoreError("audit binding requires its exact attempt authority and capability")
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "_authority", authority)
        object.__setattr__(self, "_store", store)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("audit run binding is immutable")


class OutputRunBinding:
    """Immutable association between one output role and its exact attempt authority."""

    __slots__ = ("_authority", "binding", "capability")
    _authority: _AttemptAuthority
    binding: RunBinding
    capability: OutputCapability

    def __init__(
        self,
        *,
        binding: RunBinding,
        capability: OutputCapability,
        authority: _AttemptAuthority,
        seal: object,
    ) -> None:
        if (
            seal is not _BINDING_SEAL
            or type(binding) is not RunBinding
            or type(capability) is not OutputCapability
            or type(authority) is not _AttemptAuthority
            or not capability._matches(binding, authority)
        ):
            raise StoreError("output binding requires its exact attempt authority and capability")
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "_authority", authority)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("output run binding is immutable")


class PreparedRun:
    """Durably published attempt context; no run-root or manifest path escapes."""

    __slots__ = (
        "_authority",
        "audit",
        "manifest_sha256",
        "manifest_verification",
        "output",
        "reference",
    )
    _authority: _AttemptAuthority
    audit: AuditRunBinding
    manifest_sha256: Sha256Digest
    manifest_verification: ManifestVerificationCapability
    output: OutputRunBinding
    reference: RunReference

    def __init__(
        self,
        *,
        reference: RunReference,
        manifest_sha256: Sha256Digest,
        manifest_verification: ManifestVerificationCapability,
        audit: AuditRunBinding,
        output: OutputRunBinding,
        authority: _AttemptAuthority,
        seal: object,
    ) -> None:
        if (
            seal is not _PREPARED_SEAL
            or type(reference) is not RunReference
            or type(manifest_sha256) is not Sha256Digest
            or type(manifest_verification) is not ManifestVerificationCapability
            or type(audit) is not AuditRunBinding
            or type(output) is not OutputRunBinding
            or type(authority) is not _AttemptAuthority
        ):
            raise StoreError("prepared context can only be issued by LocalResultStore")
        expected = RunBinding(reference=reference, manifest_sha256=manifest_sha256)
        if (
            authority.binding != expected
            or audit.binding != expected
            or output.binding != expected
            or audit._authority is not authority
            or output._authority is not authority
            or not manifest_verification._matches(expected, authority)
        ):
            raise StoreError("prepared capabilities do not share the durable attempt authority")
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "manifest_verification", manifest_verification)
        object.__setattr__(self, "audit", audit)
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "_authority", authority)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("prepared run is immutable")


class _RecoveryClassificationState:
    __slots__ = ("_consumption_lock", "_consumption_state", "_consumption_token")
    _consumption_lock: Lock
    _consumption_state: str
    _consumption_token: object | None

    def _initialize_consumption(self) -> None:
        object.__setattr__(self, "_consumption_lock", Lock())
        object.__setattr__(self, "_consumption_state", "available")
        object.__setattr__(self, "_consumption_token", None)

    def _reserve_consumption(self) -> object:
        token = object()
        with self._consumption_lock:
            if self._consumption_state != "available":
                raise StoreError("recovery classification is stale or foreign")
            object.__setattr__(self, "_consumption_state", "consuming")
            object.__setattr__(self, "_consumption_token", token)
        return token

    def _commit_consumption(self, token: object) -> None:
        with self._consumption_lock:
            if self._consumption_state != "consuming" or self._consumption_token is not token:
                object.__setattr__(self, "_consumption_state", "failed")
                object.__setattr__(self, "_consumption_token", None)
                raise StoreError("recovery classification reservation changed")
            object.__setattr__(self, "_consumption_state", "consumed")
            object.__setattr__(self, "_consumption_token", None)

    def _abort_consumption(self, token: object, *, retryable: bool = True) -> None:
        with self._consumption_lock:
            if self._consumption_state != "consuming" or self._consumption_token is not token:
                object.__setattr__(self, "_consumption_state", "failed")
                object.__setattr__(self, "_consumption_token", None)
                raise StoreError("recovery classification reservation changed")
            object.__setattr__(
                self,
                "_consumption_state",
                "available" if retryable else "failed",
            )
            object.__setattr__(self, "_consumption_token", None)


class VerifiedIncompleteRecoveryBinding(_RecoveryClassificationState):
    """One-use store-local classification for an incomplete existing attempt."""

    __slots__ = ("_authority", "_store", "binding", "record_count")
    _authority: _AttemptAuthority
    _store: LocalResultStore
    binding: RunBinding
    record_count: int

    def __init__(
        self,
        seal: object,
        *,
        store: LocalResultStore,
        authority: _AttemptAuthority,
        record_count: int,
    ) -> None:
        if seal is not _RECOVERY_SEAL:
            raise StoreError("recovery classifications are store-issued")
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_authority", authority)
        object.__setattr__(self, "binding", authority.binding)
        object.__setattr__(self, "record_count", record_count)
        self._initialize_consumption()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("recovery classification is immutable")


class VerifiedTerminalRecoveryBinding(_RecoveryClassificationState):
    """One-use store-local classification retaining the exact terminal evidence."""

    __slots__ = (
        "_authority",
        "_store",
        "_terminal_payload",
        "binding",
        "record_count",
        "terminal_acknowledgement",
        "terminal_record",
    )
    _authority: _AttemptAuthority
    _store: LocalResultStore
    _terminal_payload: bytes
    binding: RunBinding
    record_count: int
    terminal_acknowledgement: AuditAppendAcknowledgement
    terminal_record: AuditRecord

    def __init__(
        self,
        seal: object,
        *,
        store: LocalResultStore,
        authority: _AttemptAuthority,
        record_count: int,
        terminal_record: AuditRecord,
    ) -> None:
        if (
            seal is not _RECOVERY_SEAL
            or type(record_count) is not int
            or record_count < 2
            or type(terminal_record) is not AuditRecord
            or terminal_record.record_id.owner_sequence != record_count
        ):
            raise StoreError("terminal recovery classifications are store-issued")
        if terminal_record.record_kind is not AuditRecordKind.RUN_TERMINAL:
            raise StoreError("terminal recovery requires one final terminal record")
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_authority", authority)
        object.__setattr__(self, "binding", authority.binding)
        object.__setattr__(self, "record_count", record_count)
        object.__setattr__(self, "terminal_record", terminal_record)
        object.__setattr__(
            self,
            "terminal_acknowledgement",
            create_audit_append_acknowledgement(terminal_record),
        )
        object.__setattr__(self, "_terminal_payload", terminal_record.canonical_payload)
        self._initialize_consumption()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("recovery classification is immutable")


class RecoveredRun:
    """Reissued process-local capabilities for the same incomplete durable attempt."""

    __slots__ = (
        "_admission_lock",
        "_admission_state",
        "_admission_token",
        "_authority",
        "audit",
        "manifest_sha256",
        "manifest_verification",
        "output",
        "record_count",
        "reference",
    )
    _admission_lock: Lock
    _admission_state: str
    _admission_token: object | None
    _authority: _AttemptAuthority
    audit: AuditRunBinding
    manifest_sha256: Sha256Digest
    manifest_verification: ManifestVerificationCapability
    output: OutputRunBinding
    record_count: int
    reference: RunReference

    def __init__(
        self,
        seal: object,
        *,
        authority: _AttemptAuthority,
        audit: AuditRunBinding,
        output: OutputRunBinding,
        manifest_verification: ManifestVerificationCapability,
        record_count: int,
    ) -> None:
        if seal is not _RECOVERED_SEAL or type(record_count) is not int or record_count < 1:
            raise StoreError("recovered runs are store-issued")
        object.__setattr__(self, "_admission_lock", Lock())
        object.__setattr__(self, "_admission_state", "available")
        object.__setattr__(self, "_admission_token", None)
        object.__setattr__(self, "_authority", authority)
        object.__setattr__(self, "reference", authority.binding.reference)
        object.__setattr__(self, "manifest_sha256", authority.binding.manifest_sha256)
        object.__setattr__(self, "manifest_verification", manifest_verification)
        object.__setattr__(self, "audit", audit)
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "record_count", record_count)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("recovered run is immutable")

    def _reserve_for_admission(self) -> object:
        token = object()
        with self._admission_lock:
            if self._admission_state != "available":
                raise StoreError("recovered run was already admitted")
            object.__setattr__(self, "_admission_state", "admitting")
            object.__setattr__(self, "_admission_token", token)
        return token

    def _commit_admission(self, token: object) -> None:
        with self._admission_lock:
            if self._admission_state != "admitting" or self._admission_token is not token:
                raise StoreError("recovered run admission reservation changed")
            object.__setattr__(self, "_admission_state", "admitted")
            object.__setattr__(self, "_admission_token", None)

    def _abort_admission(self, token: object, *, retryable: bool) -> None:
        with self._admission_lock:
            if self._admission_state != "admitting" or self._admission_token is not token:
                raise StoreError("recovered run admission reservation changed")
            object.__setattr__(
                self,
                "_admission_state",
                "available" if retryable else "failed",
            )
            object.__setattr__(self, "_admission_token", None)


class RecoveredTerminalRun:
    """Read-only terminal recovery evidence; no mutation capability is present."""

    __slots__ = (
        "_consumed",
        "_journal",
        "_records",
        "binding",
        "record_count",
        "terminal_acknowledgement",
        "terminal_record",
    )
    binding: RunBinding
    _consumed: bool
    _journal: _RecoveryJournal | None
    _records: AuditRecoveryRecordSource
    record_count: int
    terminal_record: AuditRecord
    terminal_acknowledgement: AuditAppendAcknowledgement

    def __init__(
        self,
        seal: object,
        *,
        binding: RunBinding,
        records: AuditRecoveryRecordSource,
        journal: _RecoveryJournal,
        terminal_record: AuditRecord,
        terminal_acknowledgement: AuditAppendAcknowledgement,
    ) -> None:
        if (
            seal is not _RECOVERED_TERMINAL_SEAL
            or records.record_count < 2
            or records.binding != binding
            or records.record_at(records.record_count - 1) != terminal_record
        ):
            raise StoreError("terminal recovery evidence is store-issued")
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_records", records)
        object.__setattr__(self, "_journal", journal)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "record_count", records.record_count)
        object.__setattr__(self, "terminal_record", terminal_record)
        object.__setattr__(self, "terminal_acknowledgement", terminal_acknowledgement)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("terminal recovery evidence is immutable")

    def _consume(self) -> tuple[RunBinding, AuditRecoveryRecordSource]:
        if self._consumed:
            raise StoreError("terminal recovery evidence was already consumed")
        object.__setattr__(self, "_consumed", True)
        return self.binding, self._records

    def _finish(self) -> None:
        journal = self._journal
        if journal is not None:
            object.__setattr__(self, "_journal", None)
            journal.close()


class LocalResultStore:
    """Own one trusted existing result root and never reuse attempt directories."""

    __slots__ = ("_attempts", "_ops", "_registry_lock", "_root", "_store_id")

    def __init__(self, root: Path, *, _ops: _StoreOps | None = None) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise StoreError("result root must be an absolute Path")
        try:
            if root.is_symlink():
                raise StoreError("result root final entry cannot be a symlink")
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise StoreError("result root cannot be resolved") from exc
        if not resolved.is_dir():
            raise StoreError("result root must already be a real directory")
        self._root = resolved
        self._ops = _ops or _OsStoreOps()
        self._store_id = object()
        self._attempts: dict[object, _AttemptRecord] = {}
        self._registry_lock = Lock()

    def _close(self, descriptor: int | None) -> None:
        if descriptor is not None:
            self._ops.close(descriptor)

    def _write_all(self, descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = self._ops.write(descriptor, remaining)
            if type(written) is not int or written <= 0 or written > len(remaining):
                raise StoreError("manifest write made no valid forward progress")
            remaining = remaining[written:]

    def _read_all(self, descriptor: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = self._ops.read(descriptor, 64 * 1024)
            if type(chunk) is not bytes:
                raise StoreError("manifest read returned a non-bytes value")
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    @staticmethod
    def _identity(value: os.stat_result) -> tuple[int, int]:
        return value.st_dev, value.st_ino

    @staticmethod
    def _first_audit_frame(binding: RunBinding) -> bytes:
        payload = canonical_run_prepared_audit_payload(binding)
        record = create_audit_record(
            binding=binding,
            owner_sequence=1,
            record_kind=AuditRecordKind.RUN_PREPARED,
            subject_kind=AuditSubjectKind.RUN_MANIFEST,
            subject_sha256=binding.manifest_sha256,
            canonical_payload=payload,
            previous_record_sha256=EMPTY_RECORD_SHA256,
            previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
        )
        return canonical_audit_record_bytes(record) + bytes.fromhex(
            audit_frame_checksum(record).value
        )

    @staticmethod
    def _require_durable_first_audit_frame(audit_fd: int, binding: RunBinding) -> None:
        from ea.experiments.audit import AUDIT_JOURNAL_NAME, AUDIT_JOURNAL_PREAMBLE

        descriptor: int | None = None
        try:
            descriptor = os.open(
                AUDIT_JOURNAL_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=audit_fd,
            )
            frame = LocalResultStore._first_audit_frame(binding)
            stat_result = os.fstat(descriptor)
            minimum_size = len(AUDIT_JOURNAL_PREAMBLE) + len(frame)
            if not stat.S_ISREG(stat_result.st_mode):
                raise CorruptAuditRecoveryError("recovery audit journal is not a regular file")
            if stat_result.st_size < minimum_size:
                raise IncompleteAuditRecoveryError(
                    "recovery audit lacks a durable run-prepared frame"
                )
            prefix = os.pread(descriptor, minimum_size, 0)
            if prefix[: len(AUDIT_JOURNAL_PREAMBLE)] != AUDIT_JOURNAL_PREAMBLE:
                raise CorruptAuditRecoveryError("recovery audit preamble is invalid")
            if prefix[len(AUDIT_JOURNAL_PREAMBLE) :] != frame:
                raise CorruptAuditRecoveryError("recovery audit first frame is invalid")
        except IncompleteAuditRecoveryError:
            raise
        except FileNotFoundError as error:
            raise IncompleteAuditRecoveryError(
                "recovery audit lacks a durable run-prepared frame"
            ) from error
        except OSError as error:
            raise StoreError("recovery audit first frame cannot be verified") from error
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def _record_for(self, authority: _AttemptAuthority) -> _AttemptRecord:
        if type(authority) is not _AttemptAuthority or authority.store_id is not self._store_id:
            raise StoreError("capability belongs to another result store")
        with self._registry_lock:
            record = self._attempts.get(authority.attempt_token)
        if record is None or record.authority is not authority:
            raise StoreError("capability does not identify a registered attempt")
        return record

    def _resolve_child(
        self,
        capability: AuditCapability | OutputCapability,
    ) -> tuple[_AttemptRecord, str]:
        """Resolve a pathless capability to its one registered attempt and fixed role."""
        if type(capability) is AuditCapability:
            child = "audit"
        elif type(capability) is OutputCapability:
            child = "outputs"
        else:
            raise StoreError("child capability has an invalid role")
        record = self._record_for(capability._authority)
        if not capability._matches(record.authority.binding, record.authority):
            raise StoreError("child capability does not match its registered attempt")
        return record, child

    def _open_audit_directory(self, prepared: AuditRunBinding) -> tuple[int, _AttemptRecord]:
        """Open and rebind the fixed audit directory for its opaque prepared binding."""
        if type(prepared) is not AuditRunBinding or prepared._store is not self:
            raise StoreError("audit binding belongs to another result store")
        record = self._record_for(prepared._authority)
        if not prepared.capability._matches(record.authority.binding, record.authority):
            raise StoreError("audit binding does not match its registered attempt")
        root_fd: int | None = None
        run_fd: int | None = None
        audit_fd: int | None = None
        named_lock_fd: int | None = None
        try:
            root_fd = self._ops.open_root(self._root)
            root_stat = self._ops.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or self._identity(root_stat) != record.root_identity
            ):
                raise StoreError("result root identity changed after preparation")
            run_fd = self._ops.open_dir_at(root_fd, record.run_name)
            run_stat = self._ops.fstat(run_fd)
            if (
                not stat.S_ISDIR(run_stat.st_mode)
                or stat.S_IMODE(run_stat.st_mode) != 0o700
                or self._identity(run_stat) != record.run_identity
            ):
                raise StoreError("attempt directory identity changed after preparation")
            audit_fd = self._ops.open_dir_at(run_fd, "audit")
            audit_stat = self._ops.fstat(audit_fd)
            lock_stat = os.fstat(record.writer_lock_fd)
            named_lock_fd = os.open(
                "writer-v1.lock",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=audit_fd,
            )
            named_lock_stat = os.fstat(named_lock_fd)
            if (
                not stat.S_ISDIR(audit_stat.st_mode)
                or stat.S_IMODE(audit_stat.st_mode) != 0o700
                or self._identity(audit_stat) != record.audit_identity
                or not stat.S_ISREG(lock_stat.st_mode)
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
                or lock_stat.st_nlink != 1
                or self._identity(lock_stat) != record.writer_lock_identity
                or not stat.S_ISREG(named_lock_stat.st_mode)
                or stat.S_IMODE(named_lock_stat.st_mode) != 0o600
                or named_lock_stat.st_nlink != 1
                or self._identity(named_lock_stat) != self._identity(lock_stat)
            ):
                raise StoreError("audit directory or writer-lock identity changed")
            returned = audit_fd
            audit_fd = None
            return returned, record
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError("audit directory could not be opened without following links") from exc
        finally:
            for descriptor in (named_lock_fd, audit_fd, run_fd, root_fd):
                if descriptor is not None:
                    with suppress(OSError):
                        self._ops.close(descriptor)

    def verify_manifest(
        self,
        capability: ManifestVerificationCapability,
    ) -> RunManifest | RunManifestV2:
        """No-follow re-open and verify the original manifest bytes and file identity."""
        if type(capability) is not ManifestVerificationCapability:
            raise StoreError("manifest verification requires its exact capability")
        record = self._record_for(capability._authority)
        if not capability._matches(record.authority.binding, record.authority):
            raise StoreError("manifest capability does not match its registered attempt")

        root_fd: int | None = None
        run_fd: int | None = None
        manifest_fd: int | None = None
        try:
            if self._root.is_symlink() or self._root.resolve(strict=True) != self._root:
                raise StoreError("result root changed or became a symlink")
            root_fd = self._ops.open_root(self._root)
            root_stat = self._ops.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or self._identity(root_stat) != record.root_identity
            ):
                raise StoreError("result root identity changed after preparation")

            run_fd = self._ops.open_dir_at(root_fd, record.run_name)
            run_stat = self._ops.fstat(run_fd)
            if (
                not stat.S_ISDIR(run_stat.st_mode)
                or stat.S_IMODE(run_stat.st_mode) != 0o700
                or self._identity(run_stat) != record.run_identity
            ):
                raise StoreError("attempt directory identity changed after preparation")

            manifest_fd = self._ops.open_file_read_at(run_fd, "manifest.json")
            manifest_stat = self._ops.fstat(manifest_fd)
            if (
                not stat.S_ISREG(manifest_stat.st_mode)
                or stat.S_IMODE(manifest_stat.st_mode) != 0o600
                or self._identity(manifest_stat) != record.manifest_identity
            ):
                raise StoreError("manifest file identity changed after preparation")
            payload = self._read_all(manifest_fd)
            if sha256(payload).hexdigest() != record.authority.binding.manifest_sha256.value:
                raise StoreError("manifest bytes no longer match the prepared digest")
            try:
                manifest = read_manifest(payload)
            except RunContractError as exc:
                raise StoreError("manifest bytes no longer satisfy the canonical schema") from exc
            if manifest.reference != record.authority.binding.reference:
                raise StoreError("manifest reference no longer matches the prepared attempt")
            return manifest
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError("manifest could not be re-opened without following links") from exc
        finally:
            for descriptor in (manifest_fd, run_fd, root_fd):
                if descriptor is not None:
                    with suppress(OSError):
                        self._ops.close(descriptor)

    def verify_recovery_attempt(
        self,
        expected_manifest: RunManifest | RunManifestV2,
    ) -> VerifiedIncompleteRecoveryBinding | VerifiedTerminalRecoveryBinding:
        """Lock, rebind, scan and classify one exact existing attempt."""
        if type(expected_manifest) not in {RunManifest, RunManifestV2}:
            raise StoreError("recovery requires one exact expected RunManifest")
        expected_payload = canonical_manifest_bytes(expected_manifest)
        run_name = expected_manifest.run_id.value
        root_fd: int | None = None
        run_fd: int | None = None
        audit_fd: int | None = None
        output_fd: int | None = None
        manifest_fd: int | None = None
        writer_lock_fd: int | None = None
        authority: _AttemptAuthority | None = None
        classified = False
        try:
            root_fd = self._ops.open_root(self._root)
            root_stat = self._ops.fstat(root_fd)
            run_fd = self._ops.open_dir_at(root_fd, run_name)
            run_stat = self._ops.fstat(run_fd)
            audit_fd = self._ops.open_dir_at(run_fd, "audit")
            audit_stat = self._ops.fstat(audit_fd)
            output_fd = self._ops.open_dir_at(run_fd, "outputs")
            output_stat = self._ops.fstat(output_fd)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or not stat.S_ISDIR(run_stat.st_mode)
                or stat.S_IMODE(run_stat.st_mode) != 0o700
                or not stat.S_ISDIR(audit_stat.st_mode)
                or stat.S_IMODE(audit_stat.st_mode) != 0o700
                or not stat.S_ISDIR(output_stat.st_mode)
                or stat.S_IMODE(output_stat.st_mode) != 0o700
            ):
                raise StoreError("recovery directory identity or mode is invalid")
            writer_lock_fd = os.open(
                "writer-v1.lock",
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=audit_fd,
            )
            lock_stat = os.fstat(writer_lock_fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
                or lock_stat.st_nlink != 1
            ):
                raise StoreError("recovery writer lock identity is invalid")
            try:
                fcntl.flock(writer_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StoreError("writer lock already has an active owner") from error
            rebound_lock_fd = os.open(
                "writer-v1.lock",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=audit_fd,
            )
            try:
                if self._identity(os.fstat(rebound_lock_fd)) != self._identity(lock_stat):
                    raise StoreError("writer lock name changed after lease acquisition")
            finally:
                os.close(rebound_lock_fd)
            manifest_fd = self._ops.open_file_read_at(run_fd, "manifest.json")
            manifest_stat = self._ops.fstat(manifest_fd)
            manifest_payload = self._read_all(manifest_fd)
            if (
                not stat.S_ISREG(manifest_stat.st_mode)
                or stat.S_IMODE(manifest_stat.st_mode) != 0o600
                or manifest_stat.st_nlink != 1
                or manifest_payload != expected_payload
                or read_manifest(manifest_payload) != expected_manifest
            ):
                raise StoreError("recovery manifest differs from exact expected evidence")
            binding = RunBinding(
                reference=expected_manifest.reference,
                manifest_sha256=Sha256Digest(sha256(manifest_payload).hexdigest()),
            )
            if type(expected_manifest) is RunManifestV2:
                self._require_durable_first_audit_frame(audit_fd, binding)
            authority = _AttemptAuthority(self._store_id, object(), binding)
            record = _AttemptRecord(
                authority=authority,
                run_name=run_name,
                root_identity=self._identity(root_stat),
                run_identity=self._identity(run_stat),
                manifest_identity=self._identity(manifest_stat),
                audit_identity=self._identity(audit_stat),
                output_identity=self._identity(output_stat),
                writer_lock_identity=self._identity(lock_stat),
                writer_lock_fd=writer_lock_fd,
            )
            with self._registry_lock:
                self._attempts[authority.attempt_token] = record
            writer_lock_fd = None
            capability = AuditCapability(_CAPABILITY_SEAL, authority)
            audit_binding = AuditRunBinding(
                binding=binding,
                capability=capability,
                authority=authority,
                seal=_BINDING_SEAL,
                store=self,
            )
            from ea.experiments.audit import reopen_posix_audit_journal

            journal = reopen_posix_audit_journal(audit_binding)
            try:
                records = journal.recovery_records
                terminal_record = records.record_at(records.record_count - 1)
            finally:
                journal.close()
            if terminal_record.record_kind is AuditRecordKind.RUN_TERMINAL:
                result: VerifiedIncompleteRecoveryBinding | VerifiedTerminalRecoveryBinding = (
                    VerifiedTerminalRecoveryBinding(
                        _RECOVERY_SEAL,
                        store=self,
                        authority=authority,
                        record_count=records.record_count,
                        terminal_record=terminal_record,
                    )
                )
            else:
                result = VerifiedIncompleteRecoveryBinding(
                    _RECOVERY_SEAL,
                    store=self,
                    authority=authority,
                    record_count=records.record_count,
                )
            classified = True
            return result
        except StoreError:
            raise
        except (OSError, RunContractError) as error:
            raise StoreError("existing attempt could not be verified for recovery") from error
        finally:
            if authority is not None and not classified:
                with self._registry_lock:
                    registered = self._attempts.pop(authority.attempt_token, None)
                if registered is not None:
                    with suppress(OSError):
                        os.close(registered.writer_lock_fd)
            for descriptor in (
                writer_lock_fd,
                manifest_fd,
                output_fd,
                audit_fd,
                run_fd,
                root_fd,
            ):
                if descriptor is not None:
                    with suppress(OSError):
                        if descriptor is writer_lock_fd:
                            os.close(descriptor)
                        else:
                            self._ops.close(descriptor)

    def recover_incomplete_attempt(
        self,
        verified: VerifiedIncompleteRecoveryBinding,
    ) -> RecoveredRun:
        """Consume one incomplete classification and reissue process-local capabilities."""
        if type(verified) is not VerifiedIncompleteRecoveryBinding or verified._store is not self:
            raise StoreError("incomplete recovery classification is stale or foreign")
        try:
            reservation = verified._reserve_consumption()
        except StoreError as error:
            raise StoreError("incomplete recovery classification is stale or foreign") from error
        journal = None
        cleanup_failed = False
        try:
            try:
                record = self._record_for(verified._authority)
            except BaseException:
                verified._abort_consumption(reservation, retryable=False)
                raise
            authority = record.authority
            audit_binding = AuditRunBinding(
                binding=authority.binding,
                capability=AuditCapability(_CAPABILITY_SEAL, authority),
                authority=authority,
                seal=_BINDING_SEAL,
                store=self,
            )
            from ea.experiments.audit import reopen_posix_audit_journal

            journal = reopen_posix_audit_journal(audit_binding)
            try:
                record_count = journal.recovery_records.record_count
            finally:
                try:
                    journal.close()
                except BaseException:
                    cleanup_failed = True
                    raise
                else:
                    journal = None
            if record_count != verified.record_count:
                verified._abort_consumption(reservation, retryable=False)
                raise StoreError("incomplete recovery record prefix changed")
            recovered = RecoveredRun(
                _RECOVERED_SEAL,
                authority=authority,
                manifest_verification=ManifestVerificationCapability(_CAPABILITY_SEAL, authority),
                audit=audit_binding,
                output=OutputRunBinding(
                    binding=authority.binding,
                    capability=OutputCapability(_CAPABILITY_SEAL, authority),
                    authority=authority,
                    seal=_BINDING_SEAL,
                ),
                record_count=record_count,
            )
            verified._commit_consumption(reservation)
            return recovered
        except BaseException:
            if journal is not None:
                try:
                    journal.close()
                except BaseException:
                    cleanup_failed = True
            if verified._consumption_state == "consuming":
                verified._abort_consumption(reservation, retryable=not cleanup_failed)
            raise

    def recover_terminal_attempt(
        self,
        verified: VerifiedTerminalRecoveryBinding,
    ) -> RecoveredTerminalRun:
        """Consume one terminal classification after a fresh fsync and exact read-back."""
        if type(verified) is not VerifiedTerminalRecoveryBinding or verified._store is not self:
            raise StoreError("terminal recovery classification is stale or foreign")
        try:
            reservation = verified._reserve_consumption()
        except StoreError as error:
            raise StoreError("terminal recovery classification is stale or foreign") from error
        try:
            record = self._record_for(verified._authority)
        except BaseException:
            verified._abort_consumption(reservation, retryable=False)
            raise
        authority = record.authority
        capability = AuditCapability(_CAPABILITY_SEAL, authority)
        audit_binding = AuditRunBinding(
            binding=authority.binding,
            capability=capability,
            authority=authority,
            seal=_BINDING_SEAL,
            store=self,
        )
        from ea.experiments.audit import reopen_posix_audit_journal

        journal = None
        cleanup_failed = False
        retirement_started = False
        try:
            journal = reopen_posix_audit_journal(audit_binding)
            terminal = verified.terminal_record
            acknowledgement = journal.append(
                record_kind=terminal.record_kind,
                subject_kind=terminal.subject_kind,
                subject_sha256=terminal.subject_sha256,
                canonical_payload=verified._terminal_payload,
            )
            if acknowledgement != verified.terminal_acknowledgement:
                raise StoreError("terminal recovery acknowledgement changed")
            records = journal.recovery_records
            if (
                records.record_count != verified.record_count
                or records.record_at(records.record_count - 1) != terminal
            ):
                raise StoreError("terminal recovery record prefix changed")
            result = RecoveredTerminalRun(
                _RECOVERED_TERMINAL_SEAL,
                binding=authority.binding,
                records=records,
                journal=journal,
                terminal_record=verified.terminal_record,
                terminal_acknowledgement=acknowledgement,
            )
            with self._registry_lock:
                if self._attempts.get(authority.attempt_token) is not record:
                    verified._abort_consumption(reservation, retryable=False)
                    raise StoreError("terminal recovery authority changed")
                retirement_started = True
                os.close(record.writer_lock_fd)
                self._attempts.pop(authority.attempt_token)
            verified._commit_consumption(reservation)
            return result
        except BaseException:
            if journal is not None:
                try:
                    journal.close()
                except BaseException:
                    cleanup_failed = True
            if verified._consumption_state == "consuming":
                verified._abort_consumption(
                    reservation,
                    retryable=not retirement_started and not cleanup_failed,
                )
            raise

    def _retire_product_attempt(self, audit: AuditRunBinding) -> None:
        """Release a failed product prelude without deleting durable evidence."""
        if type(audit) is not AuditRunBinding or audit._store is not self:
            raise StoreError("product retirement requires its exact store-owned audit binding")
        authority = audit._authority
        with self._registry_lock:
            record = self._attempts.pop(authority.attempt_token, None)
        if record is None or record.authority is not authority:
            raise StoreError("product retirement capability is stale or foreign")
        try:
            os.close(record.writer_lock_fd)
        except OSError as error:
            raise StoreError("product retirement could not release the writer lease") from error

    def _retire_verified_product_recovery(
        self, verified: VerifiedIncompleteRecoveryBinding
    ) -> None:
        """Release an unmaterialized V2 product-recovery classification."""
        if type(verified) is not VerifiedIncompleteRecoveryBinding or verified._store is not self:
            raise StoreError("product recovery retirement requires its exact classification")
        authority = verified._authority
        with self._registry_lock:
            record = self._attempts.pop(authority.attempt_token, None)
        if record is None or record.authority is not authority:
            raise StoreError("product recovery classification is stale or foreign")
        try:
            os.close(record.writer_lock_fd)
        except OSError as error:
            raise StoreError(
                "product recovery retirement could not release the writer lease"
            ) from error

    def prepare_product(self, spec: LineageSpecV2, run_id_provider: RunIdProvider) -> PreparedRun:
        if type(spec) is not LineageSpecV2:
            raise StoreError("product preparation requires an exact LineageSpecV2")
        return self.prepare(spec, run_id_provider)

    def prepare(
        self,
        spec: LineageSpec | LineageSpecV2,
        run_id_provider: RunIdProvider,
    ) -> PreparedRun:
        """Reserve, publish, read back, and durably acknowledge one attempt."""
        if type(spec) not in {LineageSpec, LineageSpecV2}:
            raise StoreError("spec must be an exact lineage specification")
        if not callable(run_id_provider):
            raise StoreError("run_id_provider must be callable")
        try:
            provided = run_id_provider()
            if type(provided) is not UUID:
                raise StoreError("run_id_provider must return an exact UUID")
            run_id = RunId(str(provided))
        except RunContractError as exc:
            raise StoreError("run_id_provider did not return a canonical UUID4") from exc

        if type(spec) is LineageSpec:
            manifest: RunManifest | RunManifestV2 = build_manifest(spec, run_id)
        elif type(spec) is LineageSpecV2:
            manifest = build_manifest_v2(spec, run_id)
        else:
            raise AssertionError("lineage specification type check was not exhaustive")
        payload = canonical_manifest_bytes(manifest)
        run_name = run_id.value
        root_fd: int | None = None
        run_fd: int | None = None
        manifest_fd: int | None = None
        read_fd: int | None = None
        root_identity: tuple[int, int] | None = None
        run_identity: tuple[int, int] | None = None
        manifest_identity: tuple[int, int] | None = None
        audit_identity: tuple[int, int] | None = None
        output_identity: tuple[int, int] | None = None
        writer_lock_identity: tuple[int, int] | None = None
        audit_fd: int | None = None
        output_fd: int | None = None
        writer_lock_fd: int | None = None
        reserved = False
        try:
            if self._root.is_symlink() or self._root.resolve(strict=True) != self._root:
                raise StoreError("result root changed or became a symlink")
            root_fd = self._ops.open_root(self._root)
            root_stat = self._ops.fstat(root_fd)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise StoreError("opened result root is not a directory")
            root_identity = self._identity(root_stat)
            try:
                self._ops.mkdir_at(root_fd, run_name, 0o700)
            except FileExistsError as exc:
                raise StoreCollisionError("run attempt path already exists") from exc
            reserved = True
            run_fd = self._ops.open_dir_at(root_fd, run_name)
            run_stat = self._ops.fstat(run_fd)
            if not stat.S_ISDIR(run_stat.st_mode):
                raise StoreError("reserved attempt entry is not a directory")
            if stat.S_IMODE(run_stat.st_mode) != 0o700:
                raise StoreError("reserved attempt directory mode is not 0700")
            run_identity = self._identity(run_stat)

            self._ops.mkdir_at(run_fd, "audit", 0o700)
            self._ops.mkdir_at(run_fd, "outputs", 0o700)
            audit_fd = self._ops.open_dir_at(run_fd, "audit")
            audit_stat = self._ops.fstat(audit_fd)
            if not stat.S_ISDIR(audit_stat.st_mode) or stat.S_IMODE(audit_stat.st_mode) != 0o700:
                raise StoreError("audit directory must be a real 0700 directory")
            audit_identity = self._identity(audit_stat)
            output_fd = self._ops.open_dir_at(run_fd, "outputs")
            output_stat = self._ops.fstat(output_fd)
            if not stat.S_ISDIR(output_stat.st_mode) or stat.S_IMODE(output_stat.st_mode) != 0o700:
                raise StoreError("output directory must be a real 0700 directory")
            output_identity = self._identity(output_stat)
            self._close(output_fd)
            output_fd = None
            writer_lock_fd = os.open(
                "writer-v1.lock",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=audit_fd,
            )
            writer_lock_stat = os.fstat(writer_lock_fd)
            if (
                not stat.S_ISREG(writer_lock_stat.st_mode)
                or stat.S_IMODE(writer_lock_stat.st_mode) != 0o600
                or writer_lock_stat.st_nlink != 1
            ):
                raise StoreError("writer lock must be one regular 0600 file")
            writer_lock_identity = self._identity(writer_lock_stat)
            try:
                fcntl.flock(writer_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StoreError("writer lock already has an active owner") from exc
            os.fsync(writer_lock_fd)
            os.fsync(audit_fd)
            self._close(audit_fd)
            audit_fd = None
            manifest_fd = self._ops.create_file_at(run_fd, "manifest.json", 0o600)
            manifest_stat = self._ops.fstat(manifest_fd)
            if (
                not stat.S_ISREG(manifest_stat.st_mode)
                or stat.S_IMODE(manifest_stat.st_mode) != 0o600
            ):
                raise StoreError("manifest must be a regular 0600 file")
            self._write_all(manifest_fd, payload)
            self._ops.fsync(manifest_fd)
            self._close(manifest_fd)
            manifest_fd = None

            read_fd = self._ops.open_file_read_at(run_fd, "manifest.json")
            read_stat = self._ops.fstat(read_fd)
            if not stat.S_ISREG(read_stat.st_mode):
                raise StoreError("published manifest is not a regular file")
            manifest_identity = self._identity(read_stat)
            read_back = self._read_all(read_fd)
            self._close(read_fd)
            read_fd = None
            if read_back != payload:
                raise StoreError("published manifest did not read back byte-for-byte")
            manifest_digest = Sha256Digest(sha256(read_back).hexdigest())

            self._ops.fsync(run_fd)
            self._ops.fsync(root_fd)
            self._close(run_fd)
            run_fd = None
            self._close(root_fd)
            root_fd = None

            binding = RunBinding(
                reference=manifest.reference,
                manifest_sha256=manifest_digest,
            )
            if (
                root_identity is None
                or run_identity is None
                or manifest_identity is None
                or audit_identity is None
                or writer_lock_identity is None
                or output_identity is None
                or writer_lock_fd is None
            ):
                raise StoreError("durable attempt identities were not captured")
            authority = _AttemptAuthority(
                store_id=self._store_id,
                attempt_token=object(),
                binding=binding,
            )
            audit_capability = AuditCapability(_CAPABILITY_SEAL, authority)
            output_capability = OutputCapability(_CAPABILITY_SEAL, authority)
            manifest_capability = ManifestVerificationCapability(
                _CAPABILITY_SEAL,
                authority,
            )
            prepared = PreparedRun(
                reference=manifest.reference,
                manifest_sha256=manifest_digest,
                manifest_verification=manifest_capability,
                audit=AuditRunBinding(
                    binding=binding,
                    capability=audit_capability,
                    authority=authority,
                    seal=_BINDING_SEAL,
                    store=self,
                ),
                output=OutputRunBinding(
                    binding=binding,
                    capability=output_capability,
                    authority=authority,
                    seal=_BINDING_SEAL,
                ),
                authority=authority,
                seal=_PREPARED_SEAL,
            )
            record = _AttemptRecord(
                authority=authority,
                run_name=run_name,
                root_identity=root_identity,
                run_identity=run_identity,
                manifest_identity=manifest_identity,
                audit_identity=audit_identity,
                output_identity=output_identity,
                writer_lock_identity=writer_lock_identity,
                writer_lock_fd=writer_lock_fd,
            )
            with self._registry_lock:
                self._attempts[authority.attempt_token] = record
            writer_lock_fd = None
            return prepared
        except StoreError:
            raise
        except FileExistsError as exc:
            if reserved:
                raise StoreError("reserved attempt child collision retained as poison") from exc
            raise StoreCollisionError("run attempt path already exists") from exc
        except OSError as exc:
            message = (
                "attempt preparation failed after reservation; poisoned directory retained"
                if reserved
                else "result root or attempt reservation failed"
            )
            raise StoreError(message) from exc
        finally:
            # Cleanup is descriptor-only. Files and directories are deliberately never removed.
            for descriptor in (
                writer_lock_fd,
                output_fd,
                audit_fd,
                read_fd,
                manifest_fd,
                run_fd,
                root_fd,
            ):
                if descriptor is not None:
                    with suppress(OSError):
                        if descriptor is writer_lock_fd:
                            os.close(descriptor)
                        else:
                            self._ops.close(descriptor)
