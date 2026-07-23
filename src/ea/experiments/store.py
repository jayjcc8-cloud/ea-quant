"""Atomic local result ownership and durable write-once manifest publication."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID

from ea.core.run import RunBinding, RunContractError, RunId, RunReference, Sha256Digest
from ea.experiments.manifest import (
    LineageSpec,
    RunManifest,
    build_manifest,
    canonical_manifest_bytes,
    read_manifest,
)

_CAPABILITY_SEAL = object()
_BINDING_SEAL = object()
_PREPARED_SEAL = object()


class StoreError(RuntimeError):
    """Raised when a result attempt cannot be prepared durably."""


class StoreCollisionError(StoreError):
    """Raised when an attempt path already exists and is never adopted."""


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

    __slots__ = ("_authority", "binding", "capability")
    _authority: _AttemptAuthority
    binding: RunBinding
    capability: AuditCapability

    def __init__(
        self,
        *,
        binding: RunBinding,
        capability: AuditCapability,
        authority: _AttemptAuthority,
        seal: object,
    ) -> None:
        if (
            seal is not _BINDING_SEAL
            or type(binding) is not RunBinding
            or type(capability) is not AuditCapability
            or type(authority) is not _AttemptAuthority
            or not capability._matches(binding, authority)
        ):
            raise StoreError("audit binding requires its exact attempt authority and capability")
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "_authority", authority)

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

    def verify_manifest(
        self,
        capability: ManifestVerificationCapability,
    ) -> RunManifest:
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

    def prepare(
        self,
        spec: LineageSpec,
        run_id_provider: RunIdProvider,
    ) -> PreparedRun:
        """Reserve, publish, read back, and durably acknowledge one attempt."""
        if type(spec) is not LineageSpec:
            raise StoreError("spec must be a LineageSpec")
        if not callable(run_id_provider):
            raise StoreError("run_id_provider must be callable")
        try:
            provided = run_id_provider()
            if type(provided) is not UUID:
                raise StoreError("run_id_provider must return an exact UUID")
            run_id = RunId(str(provided))
        except RunContractError as exc:
            raise StoreError("run_id_provider did not return a canonical UUID4") from exc

        manifest = build_manifest(spec, run_id)
        payload = canonical_manifest_bytes(manifest)
        run_name = run_id.value
        root_fd: int | None = None
        run_fd: int | None = None
        manifest_fd: int | None = None
        read_fd: int | None = None
        root_identity: tuple[int, int] | None = None
        run_identity: tuple[int, int] | None = None
        manifest_identity: tuple[int, int] | None = None
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
            if root_identity is None or run_identity is None or manifest_identity is None:
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
            )
            with self._registry_lock:
                self._attempts[authority.attempt_token] = record
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
            for descriptor in (read_fd, manifest_fd, run_fd, root_fd):
                if descriptor is not None:
                    with suppress(OSError):
                        self._ops.close(descriptor)
