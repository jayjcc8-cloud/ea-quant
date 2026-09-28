"""Quiesced consistent backup and isolated restore for local Paper attempts.

V1 is deliberately not an online snapshot. The writer lease must be released
before a single authoritative byte is read, so the captured set is settled by
construction rather than by copy-on-write or incremental machinery: wait for the
writer release, verify the attempt settled, then capture. Nothing here persists
Paper state and nothing here decides whether an attempt may trade again.

The authoritative set is the attempt manifest, the audit journal -- the only
source of Orders, Fills, Ledger, refresh, reconciliation and recovery evidence --
and the small durable side records. Status, stop requests, the operational log
and the writer-lease carrier are derived or ephemeral, so they are never
captured; a restored attempt gets a *fresh empty* lease carrier instead, because
the M1 recovery path flocks that path and cannot adopt a copied one.

Restore materialises a new isolated attempt through the store's own attempt
preparation, so the M1 recovery path -- not this module -- remains the only
authority on whether a restored attempt is usable.

Integrity here means *internal consistency*, not authenticity. The boundary
digest and the recorded identity prove that a backup is exactly the set of bytes
it claims to be and that those bytes agree with each other; they are not a
signature and are not anchored anywhere outside the backup. ``inspect`` reporting
``verified: true`` therefore means "internally consistent", and someone who can
already write to both the backup root and the attempt root is explicitly outside
this threat model.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast, final
from uuid import uuid4

from ea import __version__
from ea.core.audit import (
    AUDIT_FRAME_DIGEST_DOMAIN,
    MAX_AUDIT_HEADER_BYTES,
    MAX_LARGE_AUDIT_PAYLOAD_BYTES,
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
    canonical_run_prepared_audit_payload,
    decode_audit_record,
)
from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest
from ea.experiments._manifest_wire import canonical_json_bytes, digest
from ea.experiments.audit import (
    AUDIT_JOURNAL_NAME,
    AUDIT_JOURNAL_PREAMBLE,
    MAX_AUDIT_JOURNAL_BYTES,
)
from ea.experiments.store import (
    CanonicalAttemptManifest,
    LocalResultStore,
    StoreCollisionError,
    StoreError,
)
from ea.product.backtest import BacktestRunError, _safe_output_root
from ea.product.paper_session import PaperSessionError, _attempt, _lease_held, _manifest
from ea.strategy.package import MAX_ARTIFACT_BYTES

_BACKUP_SCHEMA = "ea.paper-backup.v1"
_RESTORE_SCHEMA = "ea.paper-restore.v1"
_BACKUP_DIGEST_DOMAIN = b"ea.paper-backup.v1\0"
_BACKUP_ROOT_PREFIX = "backup-"
_BACKUP_MANIFEST_NAME = "backup.json"
_FILES_DIRECTORY = "files"
_MANIFEST_NAME = "manifest.json"
_JOURNAL_PATH = f"audit/{AUDIT_JOURNAL_NAME}"
_MAX_DOCUMENT_BYTES = 65_536
_MAX_VERSION_BYTES = 64
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_CHUNK_BYTES = 1 << 20
_DEFAULT_KEEP = 5
# The store and the audit journal ops create the manifest, the journal and the
# lease carrier 0600, and the recovery path revalidates exactly that. The side
# records and the strategy artifact are published by the product through
# ``_write_once``, which honours the process umask (0644 under umask 022), so
# they are held only to "private from group and world writes"; they still sit
# inside a 0700 attempt directory that only the owner can traverse.
_STRICT_PRIVATE = frozenset({_MANIFEST_NAME, _JOURNAL_PATH})
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_UNSHARED_WRITE_BITS = 0o022
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


class PaperBackupError(RuntimeError):
    """A Paper backup or restore could not be completed as requested."""


class PaperBackupInputError(PaperBackupError):
    """The requested attempt, backup or retention input is malformed or unavailable."""


class PaperBackupRefused(PaperBackupError):
    """The operation failed closed; no state was created or published."""


@dataclass(frozen=True, slots=True)
class _AuthoritativeFile:
    """One captured path, its size bound and whether a real attempt must have it."""

    path: str
    maximum_bytes: int
    optional: bool


# The minimal authoritative set, in canonical order. That order is also the order
# the boundary digest commits to, so it is part of the published contract.
_AUTHORITATIVE: tuple[_AuthoritativeFile, ...] = (
    _AuthoritativeFile(_MANIFEST_NAME, _MAX_DOCUMENT_BYTES, False),
    _AuthoritativeFile(_JOURNAL_PATH, MAX_AUDIT_JOURNAL_BYTES, False),
    _AuthoritativeFile("funding.json", _MAX_DOCUMENT_BYTES, False),
    _AuthoritativeFile("kill-switch.json", _MAX_DOCUMENT_BYTES, False),
    _AuthoritativeFile("operational-safety.json", _MAX_DOCUMENT_BYTES, False),
    _AuthoritativeFile("strategy.eastrategy", MAX_ARTIFACT_BYTES, True),
)
_SPEC_BY_PATH = {spec.path: spec for spec in _AUTHORITATIVE}
_AUTHORITATIVE_RANK = {spec.path: rank for rank, spec in enumerate(_AUTHORITATIVE)}


def _rank(path: str) -> int:
    return _AUTHORITATIVE_RANK[path]


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _leaf(path: str) -> str:
    return path.rpartition("/")[2]


def _is_digest_text(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_stamp(value: str) -> bool:
    try:
        datetime.strptime(value, _STAMP_FORMAT)
    except ValueError:
        return False
    return True


def _boundary_sha256(entries: tuple[PaperBackupEntry, ...]) -> Sha256Digest:
    """One digest over the canonical ordered (path, byte length, sha256) list."""
    rows = [[entry.path, entry.size_bytes, entry.sha256.value] for entry in entries]
    return digest(_BACKUP_DIGEST_DOMAIN, canonical_json_bytes(rows))


def _require_entries(entries: object, files_sha256: object) -> None:
    if type(entries) is not tuple or any(
        type(entry) is not PaperBackupEntry for entry in cast(tuple[object, ...], entries)
    ):
        raise PaperBackupError("backup entries must be one tuple of exact entries")
    ordered = cast("tuple[PaperBackupEntry, ...]", entries)
    if any(entry.path not in _SPEC_BY_PATH for entry in ordered):
        raise PaperBackupError("backup entry path is not authoritative")
    paths = tuple(entry.path for entry in ordered)
    if paths != tuple(sorted(paths, key=_rank)):
        raise PaperBackupError("backup entries are not in canonical order")
    if not {spec.path for spec in _AUTHORITATIVE if not spec.optional} <= set(paths):
        raise PaperBackupError("backup is missing an authoritative file")
    if type(files_sha256) is not Sha256Digest or files_sha256 != _boundary_sha256(ordered):
        raise PaperBackupError("backup boundary digest does not cover its entries")


@final
@dataclass(frozen=True, slots=True)
class PaperBackupEntry:
    """One authoritative file captured in a backup, with its recorded integrity."""

    path: str
    size_bytes: int
    sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.path) is not str or self.path not in _SPEC_BY_PATH:
            raise PaperBackupError("backup entry path must be one authoritative relative path")
        if type(self.size_bytes) is not int or self.size_bytes < 1:
            raise PaperBackupError("backup entry size must be a positive integer")
        if type(self.sha256) is not Sha256Digest:
            raise PaperBackupError("backup entry digest must be an exact Sha256Digest")

    def document(self) -> dict[str, Any]:
        return {"path": self.path, "size_bytes": self.size_bytes, "sha256": self.sha256.value}


@final
@dataclass(frozen=True, slots=True)
class PaperBackupManifest:
    """The published identity and integrity record of one backup."""

    created_at: str
    ea_version: str
    run_id: RunId
    lineage_sha256: Sha256Digest
    attempt_manifest_sha256: Sha256Digest
    candidate: dict[str, Any] | None
    candidate_id: str | None
    artifact_sha256: str | None
    configuration_sha256: str | None
    entries: tuple[PaperBackupEntry, ...]
    files_sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.created_at) is not str or not _is_stamp(self.created_at):
            raise PaperBackupError("backup creation time must be one UTC microsecond stamp")
        if type(self.ea_version) is not str or not 1 <= len(self.ea_version) <= _MAX_VERSION_BYTES:
            raise PaperBackupError("backup EA version must be bounded text")
        if type(self.run_id) is not RunId:
            raise PaperBackupError("backup requires one exact attempt run id")
        if type(self.lineage_sha256) is not Sha256Digest:
            raise PaperBackupError("backup requires one exact attempt lineage digest")
        if type(self.attempt_manifest_sha256) is not Sha256Digest:
            raise PaperBackupError("backup requires one exact attempt manifest digest")
        if self.candidate is not None and (
            type(self.candidate) is not dict
            or not self.candidate
            or any(type(key) is not str for key in self.candidate)
        ):
            raise PaperBackupError("backup candidate identity must be one object or null")
        if self.candidate_id is not None and type(self.candidate_id) is not str:
            raise PaperBackupError("backup candidate id must be text or null")
        for name, value in (
            ("artifact_sha256", self.artifact_sha256),
            ("configuration_sha256", self.configuration_sha256),
        ):
            if value is not None and not _is_digest_text(value):
                raise PaperBackupError(f"backup {name} must be lowercase hex or null")
        _require_entries(self.entries, self.files_sha256)

    def document(self) -> dict[str, Any]:
        return {
            "schema": _BACKUP_SCHEMA,
            "created_at": self.created_at,
            "ea_version": self.ea_version,
            "run": {
                "run_id": self.run_id.value,
                "lineage_sha256": self.lineage_sha256.value,
                "manifest_sha256": self.attempt_manifest_sha256.value,
            },
            "candidate": self.candidate,
            "candidate_id": self.candidate_id,
            "artifact_sha256": self.artifact_sha256,
            "configuration_sha256": self.configuration_sha256,
            "files": [entry.document() for entry in self.entries],
            "files_sha256": self.files_sha256.value,
        }


@final
@dataclass(frozen=True, slots=True)
class PaperBackupResult:
    """One published backup and the retention decision that followed it."""

    backup_dir: Path
    manifest: PaperBackupManifest
    pruned: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.backup_dir, Path):
            raise PaperBackupError("backup directory must be one Path")
        if type(self.manifest) is not PaperBackupManifest:
            raise PaperBackupError("backup result requires one exact manifest")
        if type(self.pruned) is not tuple or any(type(name) is not str for name in self.pruned):
            raise PaperBackupError("pruned backup names must be one tuple of text")

    def document(self) -> dict[str, Any]:
        return {
            **self.manifest.document(),
            "backup_dir": str(self.backup_dir),
            "pruned": list(self.pruned),
        }


@final
@dataclass(frozen=True, slots=True)
class PaperRestoreResult:
    """One attempt restored into a previously empty target path."""

    backup_dir: Path
    run_dir: Path
    run_id: RunId
    lineage_sha256: Sha256Digest
    attempt_manifest_sha256: Sha256Digest
    entries: tuple[PaperBackupEntry, ...]
    files_sha256: Sha256Digest

    def __post_init__(self) -> None:
        if not isinstance(self.backup_dir, Path) or not isinstance(self.run_dir, Path):
            raise PaperBackupError("restore result paths must be Path values")
        if type(self.run_id) is not RunId or self.run_dir.name != self.run_id.value:
            raise PaperBackupError("restored attempt must be named by its run id")
        if type(self.lineage_sha256) is not Sha256Digest:
            raise PaperBackupError("restore result requires one exact lineage digest")
        if type(self.attempt_manifest_sha256) is not Sha256Digest:
            raise PaperBackupError("restore result requires one exact manifest digest")
        _require_entries(self.entries, self.files_sha256)

    def document(self) -> dict[str, Any]:
        return {
            "schema": _RESTORE_SCHEMA,
            "backup_dir": str(self.backup_dir),
            "run_dir": str(self.run_dir),
            "run_id": self.run_id.value,
            "lineage_sha256": self.lineage_sha256.value,
            "manifest_sha256": self.attempt_manifest_sha256.value,
            "files": [entry.document() for entry in self.entries],
            "files_sha256": self.files_sha256.value,
        }


def _canonical_document(document: dict[str, Any], *, name: str) -> bytes:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError, UnicodeError) as error:
        raise PaperBackupRefused(f"{name} is not serializable canonical evidence") from error
    if not 1 <= len(payload) <= _MAX_DOCUMENT_BYTES:
        raise PaperBackupRefused(f"{name} exceeds the bounded document size")
    return payload


def _safe_root(path: Path, *, name: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PaperBackupInputError(f"{name} must be an absolute Path")
    try:
        return _safe_output_root(path.expanduser().absolute())
    except BacktestRunError as error:
        raise PaperBackupInputError(f"{name} could not be created or resolved") from error


def _existing_directory(path: Path, *, name: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PaperBackupInputError(f"{name} must be an absolute Path")
    try:
        if path.is_symlink():
            raise PaperBackupInputError(f"{name} cannot be a symlink")
        resolved = path.resolve(strict=True)
    except PaperBackupInputError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise PaperBackupInputError(f"{name} is unavailable or unsafe") from error
    if not resolved.is_dir():
        raise PaperBackupInputError(f"{name} must be one real directory")
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _DIR_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_directory(path: Path) -> None:
    """Best-effort removal of a directory this module created; never raises."""
    try:
        if path.is_symlink() or not path.is_dir():
            return
        shutil.rmtree(path)
    except OSError:
        return


def _discard_empty_directory(path: Path) -> None:
    """Best-effort removal of a root this call created; never removes content."""
    try:
        os.rmdir(path)
    except OSError:
        return


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise PaperBackupRefused("backup write made no progress")
        remaining = remaining[written:]


def _create_directory(root: Path, name: str) -> Path:
    """Create one directory this module owns, refusing anything already there."""
    path = root / name
    if path.is_symlink() or path.exists():
        raise PaperBackupRefused(f"{name} already exists in the target root")
    try:
        path.mkdir(mode=_DIRECTORY_MODE)
    except OSError as error:
        raise PaperBackupRefused(f"{name} could not be created") from error
    return path


@contextmanager
def _entry_parent(root_fd: int, path: str) -> Iterator[int]:
    """Yield the directory descriptor that holds one authoritative relative path."""
    if "/" not in path:
        yield root_fd
        return
    directory = path.rpartition("/")[0]
    if "/" in directory:
        raise PaperBackupRefused("authoritative paths are at most one directory deep")
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, _DIR_FLAGS, dir_fd=root_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
            raise PaperBackupRefused(f"{directory} must be one real 0700 directory")
    except PaperBackupRefused:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PaperBackupRefused(f"{directory} is unavailable or unsafe") from error
    assert descriptor is not None
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _require_private(info: os.stat_result, spec: _AuthoritativeFile) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if mode & _UNSHARED_WRITE_BITS:
        raise PaperBackupRefused(f"{spec.path} must not be group- or world-writable")
    if spec.path in _STRICT_PRIVATE and mode != _FILE_MODE:
        raise PaperBackupRefused(f"{spec.path} must be one private 0600 file")


@contextmanager
def _opened_entry(parent_fd: int, spec: _AuthoritativeFile) -> Iterator[tuple[int, os.stat_result]]:
    """Open one bounded authoritative file and require it to be unchanged after use."""
    descriptor: int | None = None
    try:
        descriptor = os.open(_leaf(spec.path), _READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        # Callers distinguish an absent optional file from an unsafe one.
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PaperBackupRefused(f"{spec.path} is unavailable or unsafe") from error
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= spec.maximum_bytes
    ):
        os.close(descriptor)
        raise PaperBackupRefused(f"{spec.path} must be one bounded regular single-link file")
    try:
        _require_private(before, spec)
    except PaperBackupRefused:
        os.close(descriptor)
        raise
    try:
        yield descriptor, before
    except BaseException:
        raise
    else:
        after = os.fstat(descriptor)
        if (
            _identity(after) != _identity(before)
            or after.st_size != before.st_size
            or after.st_mode != before.st_mode
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise PaperBackupRefused(f"{spec.path} changed while being captured")
    finally:
        os.close(descriptor)


def _capture_entry(
    parent_fd: int, spec: _AuthoritativeFile, sink_fd: int | None
) -> tuple[int, Sha256Digest] | None:
    """Stream one authoritative file, hashing it and optionally copying it.

    A missing optional file yields ``None``; a missing required file fails closed.
    """
    hasher = sha256()
    try:
        with _opened_entry(parent_fd, spec) as (descriptor, before):
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, _CHUNK_BYTES))
                if not chunk:
                    raise PaperBackupRefused(f"{spec.path} ended before its recorded size")
                hasher.update(chunk)
                if sink_fd is not None:
                    _write_all(sink_fd, chunk)
                remaining -= len(chunk)
            return before.st_size, Sha256Digest(hasher.hexdigest())
    except FileNotFoundError:
        if spec.optional:
            return None
        raise PaperBackupRefused(f"{spec.path} is missing from the attempt") from None
    except PaperBackupRefused:
        raise
    except OSError as error:
        raise PaperBackupRefused(f"{spec.path} could not be captured") from error


def _read_entry_payload(parent_fd: int, spec: _AuthoritativeFile) -> bytes:
    try:
        with _opened_entry(parent_fd, spec) as (descriptor, before):
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    raise PaperBackupRefused(f"{spec.path} ended before its recorded size")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
    except OSError as error:
        raise PaperBackupRefused(f"{spec.path} could not be read") from error


def _verify_entries(root_fd: int, entries: tuple[PaperBackupEntry, ...]) -> None:
    """Re-read one materialised tree and require it to match its recorded integrity."""
    for entry in entries:
        with _entry_parent(root_fd, entry.path) as parent_fd:
            captured = _capture_entry(parent_fd, _SPEC_BY_PATH[entry.path], None)
        if captured is None:
            raise PaperBackupRefused(f"{entry.path} is missing from the materialised attempt")
        size, checksum = captured
        if size != entry.size_bytes or checksum != entry.sha256:
            raise PaperBackupRefused(f"{entry.path} does not match its recorded integrity")


def _read_first_journal_frame(files_fd: int) -> tuple[bytes, bytes]:
    """Read the first captured journal frame's header and payload, read-only.

    ``reopen_posix_audit_journal`` appends a RUN_PREPARED record, so it can never
    be pointed at a captured copy: it would mutate the very bytes being verified.
    Only the frame boundary is located here, and every field is then validated by
    the audit module's own decoder against the captured binding.
    """
    spec = _SPEC_BY_PATH[_JOURNAL_PATH]

    def exact(descriptor: int, size: int, offset: int) -> bytes:
        chunk = os.pread(descriptor, size, offset)
        if len(chunk) != size:
            raise PaperBackupRefused("captured journal is truncated before its first frame")
        return chunk

    with (
        _entry_parent(files_fd, _JOURNAL_PATH) as parent_fd,
        _opened_entry(parent_fd, spec) as (descriptor, _),
    ):
        offset = len(AUDIT_JOURNAL_PREAMBLE)
        if exact(descriptor, offset, 0) != AUDIT_JOURNAL_PREAMBLE:
            raise PaperBackupRefused("captured journal preamble is invalid")
        raw_header_length = exact(descriptor, 8, offset)
        header_length = int.from_bytes(raw_header_length, "big")
        if not 1 <= header_length <= MAX_AUDIT_HEADER_BYTES:
            raise PaperBackupRefused("captured journal header length is outside the v1 bound")
        offset += 8
        header = exact(descriptor, header_length, offset)
        offset += header_length
        raw_payload_length = exact(descriptor, 8, offset)
        payload_length = int.from_bytes(raw_payload_length, "big")
        if not 1 <= payload_length <= MAX_LARGE_AUDIT_PAYLOAD_BYTES:
            raise PaperBackupRefused("captured journal payload length is outside the v1 bound")
        offset += 8
        payload = exact(descriptor, payload_length, offset)
        offset += payload_length
        checksum = exact(descriptor, 32, offset)
    framed = raw_header_length + header + raw_payload_length + payload
    if checksum != sha256(AUDIT_FRAME_DIGEST_DOMAIN + framed).digest():
        raise PaperBackupRefused("captured journal first frame checksum is invalid")
    return header, payload


def _require_journal_binding(files_fd: int, binding: RunBinding) -> None:
    """Cross-check the captured journal against the captured attempt identity.

    The journal's own header carries run id, lineage and manifest digest, so a
    journal captured from a different attempt is refused here rather than later
    as a failed M1 reopen. This is read-only: no record is ever appended.
    """
    header, payload = _read_first_journal_frame(files_fd)
    try:
        record = decode_audit_record(
            binding=binding, canonical_header=header, canonical_payload=payload
        )
    except AuditContractError as error:
        raise PaperBackupRefused(
            "captured journal does not bind to the captured attempt"
        ) from error
    if (
        record.record_kind is not AuditRecordKind.RUN_PREPARED
        or record.subject_kind is not AuditSubjectKind.RUN_MANIFEST
        or record.subject_sha256 != binding.manifest_sha256
        or record.canonical_payload != canonical_run_prepared_audit_payload(binding)
    ):
        raise PaperBackupRefused(
            "captured journal does not begin with the captured attempt preparation"
        )


def _require_recorded_identity(files_fd: int, manifest: PaperBackupManifest) -> bytes:
    """Bind every recorded identity field to the captured attempt manifest bytes.

    Without this the recorded run id, lineage, digest and candidate block would be
    free-floating claims: a tampered manifest could report one identity while the
    captured bytes carry another, and a tampered run id would even decide the
    directory a restore writes to.
    """
    with _entry_parent(files_fd, _MANIFEST_NAME) as parent_fd:
        payload = _read_entry_payload(parent_fd, _SPEC_BY_PATH[_MANIFEST_NAME])
    if sha256(payload).hexdigest() != manifest.attempt_manifest_sha256.value:
        raise PaperBackupRefused(
            "captured attempt manifest does not match the recorded manifest digest"
        )
    try:
        document = json.loads(payload)
    except (RecursionError, UnicodeError, ValueError) as error:
        raise PaperBackupRefused("captured attempt manifest is malformed JSON") from error
    if type(document) is not dict:
        raise PaperBackupRefused("captured attempt manifest must be one JSON object")
    try:
        run_id = RunId(document["run_id"])
        lineage_sha256 = Sha256Digest(document["lineage_sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise PaperBackupRefused("captured attempt manifest identity is malformed") from error
    if run_id != manifest.run_id or lineage_sha256 != manifest.lineage_sha256:
        raise PaperBackupRefused("backup run identity conflicts with the captured attempt")
    raw = document.get("candidate")
    candidate = dict(raw) if type(raw) is dict and raw else None
    if candidate != manifest.candidate:
        raise PaperBackupRefused("backup candidate identity conflicts with the captured attempt")
    candidate_id = None if candidate is None else candidate.get("candidate_id")
    if (
        manifest.candidate_id != (candidate_id if type(candidate_id) is str else None)
        or manifest.artifact_sha256 != _optional_digest(candidate, "artifact_sha256")
        or manifest.configuration_sha256 != _optional_digest(candidate, "configuration_sha256")
    ):
        raise PaperBackupRefused("backup candidate fields conflict with the captured attempt")
    return payload


def _copy_entry(
    destination_fd: int, spec: _AuthoritativeFile, source_parent_fd: int
) -> PaperBackupEntry | None:
    """Copy one authoritative file into a fresh 0600 file while hashing it."""
    descriptor: int | None = None
    try:
        with _entry_parent(destination_fd, spec.path) as parent_fd:
            try:
                descriptor = os.open(_leaf(spec.path), _CREATE_FLAGS, _FILE_MODE, dir_fd=parent_fd)
            except OSError as error:
                raise PaperBackupRefused(f"{spec.path} could not be written") from error
            try:
                captured = _capture_entry(source_parent_fd, spec, descriptor)
                if captured is None:
                    return None
                size, checksum = captured
                os.fsync(descriptor)
                return PaperBackupEntry(spec.path, size, checksum)
            finally:
                os.close(descriptor)
    except PaperBackupRefused:
        raise
    except OSError as error:
        raise PaperBackupRefused(f"{spec.path} could not be written") from error


def _stage_authoritative(staging: Path, run_fd: int) -> tuple[PaperBackupEntry, ...]:
    """Capture the whole authoritative set into a fresh staging tree."""
    files = _create_directory(staging, _FILES_DIRECTORY)
    files_fd = os.open(files, _DIR_FLAGS)
    try:
        staged: list[PaperBackupEntry] = []
        for spec in _AUTHORITATIVE:
            if "/" in spec.path:
                try:
                    os.mkdir(spec.path.rpartition("/")[0], _DIRECTORY_MODE, dir_fd=files_fd)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise PaperBackupRefused(f"{spec.path} could not be staged") from error
            with _entry_parent(run_fd, spec.path) as source_fd:
                entry = _copy_entry(files_fd, spec, source_fd)
            if entry is not None:
                staged.append(entry)
        _fsync_directory(files)
        return tuple(staged)
    finally:
        os.close(files_fd)


def _write_backup_manifest(staging: Path, payload: bytes) -> None:
    path = staging / _BACKUP_MANIFEST_NAME
    if path.is_symlink() or path.exists():
        raise PaperBackupRefused("backup manifest already exists in the staging tree")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _CREATE_FLAGS, _FILE_MODE)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except PaperBackupRefused:
        raise
    except OSError as error:
        raise PaperBackupRefused("backup manifest could not be published") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _publish_directory(staging: Path, final: Path) -> None:
    """Rename a fully fsynced staging tree into place, then fsync its parent."""
    if final.is_symlink() or final.exists():
        raise PaperBackupRefused("backup target already exists")
    try:
        os.rename(staging, final)
    except OSError as error:
        raise PaperBackupRefused("backup could not be published") from error
    _fsync_directory(final.parent)


def _backup_name(created_at: datetime, run_id: RunId) -> str:
    return f"{_BACKUP_ROOT_PREFIX}{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{run_id.value}"


def _owned_backups(root: Path, run_id: RunId) -> tuple[str, ...]:
    """Names of published backups this module can positively identify as its own.

    A directory is only ever prunable when it holds a canonical ``backup.json``
    whose manifest names *this* run. A name prefix is not ownership: the backup
    root is an operator directory that may hold hand-made archives or other
    runs' backups, and retention must never delete what it cannot verify.
    """
    owned: list[str] = []
    for entry in os.scandir(root):
        if not entry.name.startswith(_BACKUP_ROOT_PREFIX):
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            if _read_backup_manifest(Path(entry.path)).run_id == run_id:
                owned.append(entry.name)
        except (PaperBackupError, OSError):
            continue
    return tuple(sorted(owned))


def _apply_retention(root: Path, run_id: RunId, published: str, keep: int) -> tuple[str, ...]:
    """Deterministically keep only the newest ``keep`` backups of one run.

    Names sort chronologically because the stamp prefix is fixed-width UTC. The
    backup just published is never a pruning candidate, so a backwards clock
    cannot make it delete itself after its path has been reported. A directory
    that cannot be removed is left for the next backup rather than failing one
    that is already published.
    """
    try:
        owned = _owned_backups(root, run_id)
    except OSError as error:
        raise PaperBackupRefused("backup root could not be listed for retention") from error
    stale = tuple(name for name in (owned[: max(0, len(owned) - keep)]) if name != published)
    for name in stale:
        _discard_directory(root / name)
    if stale:
        _fsync_directory(root)
    if not (root / published).is_dir():
        raise PaperBackupRefused("the published backup did not survive retention")
    return stale


def _optional_digest(candidate: dict[str, Any] | None, key: str) -> str | None:
    if candidate is None:
        return None
    value = candidate.get(key)
    return value if _is_digest_text(value) else None


def _build_manifest(
    *,
    created_at: datetime,
    run_id: RunId,
    lineage_sha256: Sha256Digest,
    attempt_manifest_sha256: Sha256Digest,
    source_document: dict[str, Any],
    entries: tuple[PaperBackupEntry, ...],
) -> PaperBackupManifest:
    raw = source_document.get("candidate")
    candidate = dict(raw) if type(raw) is dict and raw else None
    candidate_id = None if candidate is None else candidate.get("candidate_id")
    return PaperBackupManifest(
        created_at=created_at.strftime(_STAMP_FORMAT),
        ea_version=__version__,
        run_id=run_id,
        lineage_sha256=lineage_sha256,
        attempt_manifest_sha256=attempt_manifest_sha256,
        candidate=candidate,
        candidate_id=candidate_id if type(candidate_id) is str else None,
        artifact_sha256=_optional_digest(candidate, "artifact_sha256"),
        configuration_sha256=_optional_digest(candidate, "configuration_sha256"),
        entries=entries,
        files_sha256=_boundary_sha256(entries),
    )


def create_paper_backup(
    run_dir: Path,
    backup_root: Path,
    *,
    keep: int = _DEFAULT_KEEP,
) -> PaperBackupResult:
    """Capture one quiesced attempt into a new immutable backup directory.

    The attempt must have released the writer lease, so the authoritative set is
    settled before any byte is read. A held lease, an existing target, or any
    authoritative file that changes mid-capture fails closed without publishing.
    """
    if type(keep) is not int or keep < 1:
        raise PaperBackupInputError("backup retention must keep at least one backup")
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise PaperBackupInputError("run_dir must be an absolute Path")
    created_at = datetime.now(UTC)
    try:
        with _attempt(run_dir) as (canonical, run_fd):
            binding, manifest_bytes = _manifest(run_fd, canonical.name)
            if _lease_held(run_fd):
                raise PaperBackupRefused(
                    "writer lease is held; quiesce the attempt before capturing it"
                )
            # The backup root is only created once the capture is known to be
            # permitted, so a refusal leaves the operator's filesystem untouched.
            root = _safe_root(backup_root, name="backup root")
            name = _backup_name(created_at, binding.reference.run_id)
            staging = _create_directory(root, f".{name}.{uuid4().hex}.tmp")
            try:
                entries = _stage_authoritative(staging, run_fd)
                manifest = _build_manifest(
                    created_at=created_at,
                    run_id=binding.reference.run_id,
                    lineage_sha256=binding.reference.lineage_sha256,
                    attempt_manifest_sha256=binding.manifest_sha256,
                    source_document=json.loads(manifest_bytes),
                    entries=entries,
                )
                _write_backup_manifest(
                    staging,
                    _canonical_document(manifest.document(), name="backup manifest"),
                )
                _fsync_directory(staging)
                _publish_directory(staging, root / name)
            except BaseException:
                _discard_directory(staging)
                raise
            if _lease_held(run_fd):
                # A writer acquired the attempt while it was being captured, so
                # the capture may interleave with new evidence: publish nothing.
                _discard_directory(root / name)
                raise PaperBackupRefused("writer lease was acquired while the attempt was captured")
            return PaperBackupResult(
                root / name,
                manifest,
                _apply_retention(root, binding.reference.run_id, name, keep),
            )
    except PaperBackupRefused:
        raise
    except PaperSessionError as error:
        raise PaperBackupInputError(str(error)) from error
    except OSError as error:
        raise PaperBackupInputError("attempt could not be read for backup") from error
    except (ValueError, KeyError, TypeError) as error:
        raise PaperBackupRefused("attempt manifest is not usable backup evidence") from error


def inspect_paper_backup(backup_dir: Path) -> PaperBackupManifest:
    """Re-verify one backup's internal consistency and return its manifest.

    ``verified`` means internally consistent, not authentic: the recorded identity
    is bound to the captured bytes, and the captured journal is bound to the
    captured attempt, but nothing here anchors either to a trusted third party.
    """
    directory = _existing_directory(backup_dir, name="backup")
    manifest = _read_backup_manifest(directory)
    files_fd = os.open(directory / _FILES_DIRECTORY, _DIR_FLAGS)
    try:
        # Cheap identity checks first, so a tampered claim is refused without
        # hashing the whole authoritative set.
        _require_recorded_identity(files_fd, manifest)
        _require_journal_binding(
            files_fd,
            RunBinding(
                RunReference(manifest.run_id, manifest.lineage_sha256),
                manifest.attempt_manifest_sha256,
            ),
        )
        _verify_entries(files_fd, manifest.entries)
    except PaperBackupRefused:
        raise
    except OSError as error:
        raise PaperBackupRefused("backup contents could not be verified") from error
    finally:
        os.close(files_fd)
    return manifest


@final
@dataclass(frozen=True, slots=True)
class VerifiedPaperBackup:
    """One published backup whose recorded integrity re-verified, read-only."""

    backup_dir: Path
    manifest: PaperBackupManifest
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.backup_dir, Path):
            raise PaperBackupError("a verified backup requires one Path")
        if type(self.manifest) is not PaperBackupManifest:
            raise PaperBackupError("a verified backup requires one exact manifest")
        if type(self.created_at) is not datetime or self.created_at.utcoffset() != timedelta(0):
            raise PaperBackupError("a verified backup requires one UTC creation instant")

    def document(self) -> dict[str, Any]:
        return {
            "backup_dir": str(self.backup_dir),
            "created_at": self.created_at.isoformat(),
            "verified": True,
            **self.manifest.document(),
        }


def latest_verified_paper_backup(
    backup_root: Path, *, run_id: RunId | None = None
) -> VerifiedPaperBackup | None:
    """Return the newest backup whose contents still re-verify, or ``None``.

    This is the read-only observation PPV-06 needs to age a backup, and it owns
    no second reading of the backup format: every candidate is judged by
    :func:`inspect_paper_backup`, which is the same verification an operator
    runs by hand. A candidate that fails verification is skipped rather than
    reported, because the question is "how old is the newest backup that is
    actually usable", not "how old is the newest directory that looks like one".

    ``run_id`` narrows the question to one attempt; without it the newest
    verified backup on the host answers, which is the host-level fact PPV-06
    alerts on. Nothing here mutates the root or any backup.
    """
    if not isinstance(backup_root, Path) or not backup_root.is_absolute():
        raise PaperBackupInputError("backup root must be an absolute Path")
    if run_id is not None and type(run_id) is not RunId:
        raise PaperBackupInputError("run_id must be an exact RunId or absent")
    directory = _existing_directory(backup_root, name="backup root")
    try:
        names = tuple(
            entry.name
            for entry in os.scandir(directory)
            if entry.name.startswith(_BACKUP_ROOT_PREFIX) and entry.is_dir(follow_symlinks=False)
        )
    except OSError as error:
        raise PaperBackupInputError("backup root could not be listed") from error

    # Order by the recorded creation instant, which is the manifest's own claim
    # and is re-verified below; the directory name is only a tie-break.
    dated: list[tuple[str, str]] = []
    for name in names:
        try:
            header = _read_backup_manifest(directory / name)
        except (PaperBackupError, OSError):
            continue
        if run_id is not None and header.run_id != run_id:
            continue
        dated.append((header.created_at, name))
    for _, name in sorted(dated, reverse=True):
        candidate = directory / name
        try:
            manifest = inspect_paper_backup(candidate)
        except (PaperBackupError, OSError):
            continue
        return VerifiedPaperBackup(
            backup_dir=candidate,
            manifest=manifest,
            created_at=datetime.strptime(manifest.created_at, _STAMP_FORMAT).replace(tzinfo=UTC),
        )
    return None


def _read_backup_manifest(directory: Path) -> PaperBackupManifest:
    spec = _AuthoritativeFile(_BACKUP_MANIFEST_NAME, _MAX_DOCUMENT_BYTES, False)
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, _DIR_FLAGS)
        payload = _read_entry_payload(descriptor, spec)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        document = json.loads(payload)
    except (RecursionError, UnicodeError, ValueError) as error:
        raise PaperBackupRefused("backup manifest is malformed JSON") from error
    if type(document) is not dict:
        raise PaperBackupRefused("backup manifest must be one canonical JSON object")
    if _canonical_document(document, name="backup manifest") != payload:
        raise PaperBackupRefused("backup manifest is not canonical evidence")
    try:
        run = document["run"]
        raw_entries = document["files"]
        if type(run) is not dict or type(raw_entries) is not list:
            raise ValueError("backup manifest structure conflicts")
        entries = tuple(
            PaperBackupEntry(
                path=item["path"],
                size_bytes=item["size_bytes"],
                sha256=Sha256Digest(item["sha256"]),
            )
            for item in raw_entries
        )
        return PaperBackupManifest(
            created_at=document["created_at"],
            ea_version=document["ea_version"],
            run_id=RunId(run["run_id"]),
            lineage_sha256=Sha256Digest(run["lineage_sha256"]),
            attempt_manifest_sha256=Sha256Digest(run["manifest_sha256"]),
            candidate=document["candidate"],
            candidate_id=document["candidate_id"],
            artifact_sha256=document["artifact_sha256"],
            configuration_sha256=document["configuration_sha256"],
            entries=entries,
            files_sha256=Sha256Digest(document["files_sha256"]),
        )
    except PaperBackupRefused:
        raise
    except (PaperBackupError, KeyError, TypeError, ValueError) as error:
        raise PaperBackupRefused("backup manifest identity conflicts") from error


def _materialise(target: Path, files_fd: int, manifest: PaperBackupManifest) -> None:
    """Write every authoritative file the store did not already publish."""
    with _attempt(target) as (_, run_fd):
        for entry in manifest.entries:
            if entry.path == _MANIFEST_NAME:
                # The store published this attempt manifest as part of preparation.
                continue
            with _entry_parent(files_fd, entry.path) as source_fd:
                written = _copy_entry(run_fd, _SPEC_BY_PATH[entry.path], source_fd)
            if written != entry:
                raise PaperBackupRefused(f"{entry.path} could not be restored")
        _verify_entries(run_fd, manifest.entries)
        try:
            os.fsync(run_fd)
        except OSError as error:
            raise PaperBackupRefused("restored attempt could not be made durable") from error


def restore_paper_backup(backup_dir: Path, run_dir: Path) -> PaperRestoreResult:
    """Restore one verified backup into a new isolated attempt directory.

    The backup is fully verified first, so an integrity failure is always
    reported before any state is created at the target. The target attempt
    directory is never an existing one, and the source backup is never modified.
    A refused or failed restore leaves nothing behind: the reservation the store
    makes is this call's to remove, because the store deliberately keeps a
    poisoned directory that would otherwise block every later retry.
    """
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise PaperBackupInputError("run_dir must be an absolute Path")
    manifest = inspect_paper_backup(backup_dir)
    directory = _existing_directory(backup_dir, name="backup")
    if run_dir.name != manifest.run_id.value:
        raise PaperBackupInputError("run_dir must be named by the backup run id")
    # Refuse an existing target before creating its parent, so a refusal is
    # side-effect free on the operator's filesystem.
    requested = run_dir.parent / run_dir.name
    if requested.is_symlink() or requested.exists():
        raise PaperBackupRefused("restore target already exists; it is never overwritten")
    created_root = not run_dir.parent.exists()
    target_root = _safe_root(run_dir.parent, name="restore root")
    target = target_root / run_dir.name
    files_fd: int | None = None
    store: LocalResultStore | None = None
    reserved = False
    try:
        files_fd = os.open(directory / _FILES_DIRECTORY, _DIR_FLAGS)
        with _entry_parent(files_fd, _MANIFEST_NAME) as parent_fd:
            manifest_payload = _read_entry_payload(parent_fd, _SPEC_BY_PATH[_MANIFEST_NAME])
        store = LocalResultStore(target_root)
        # Preparation reserves the attempt durably, publishes the exact manifest
        # bytes and creates the fresh empty writer-lease carrier the M1 recovery
        # path flocks; a copied carrier could never take that lease.
        try:
            store.prepare_canonical_attempt(
                CanonicalAttemptManifest(
                    RunReference(manifest.run_id, manifest.lineage_sha256), manifest_payload
                )
            )
        except StoreCollisionError:
            # The target appeared between the check above and the reservation, so
            # it belongs to someone else and is never discarded.
            raise
        except BaseException:
            reserved = True
            raise
        reserved = True
        _materialise(target, files_fd, manifest)
        store.close()
        store = None
    except BaseException as error:
        if reserved:
            _discard_directory(target)
            if created_root:
                _discard_empty_directory(target_root)
        if isinstance(error, PaperBackupError):
            raise
        if isinstance(error, (StoreError, OSError, ValueError, KeyError, TypeError)):
            raise PaperBackupRefused("restore could not be completed") from error
        raise
    finally:
        if store is not None:
            store.close()
        if files_fd is not None:
            os.close(files_fd)
    return PaperRestoreResult(
        backup_dir=directory,
        run_dir=target,
        run_id=manifest.run_id,
        lineage_sha256=manifest.lineage_sha256,
        attempt_manifest_sha256=manifest.attempt_manifest_sha256,
        entries=manifest.entries,
        files_sha256=manifest.files_sha256,
    )
