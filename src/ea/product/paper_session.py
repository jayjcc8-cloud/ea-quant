"""Observational local Paper status and cooperative stop over a store attempt.

The existing LocalResultStore owns the writer lease. This module only observes
that lease and publishes bounded files in the already-created outputs directory.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import uuid4

from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest

_MANIFEST_SCHEMA = "ea.local-paper-attempt.v1"
_STATUS_SCHEMA = "ea.local-paper-status.v1"
_STOP_SCHEMA = "ea.local-paper-stop.v1"
_MAX_FILE_BYTES = 65_536
_STATES = frozenset({"starting", "running", "stopping", "stopped", "failed"})
_TERMINAL = frozenset({"stopped", "failed"})
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class PaperSessionError(RuntimeError):
    """Untrusted, missing, or conflicting local Paper session evidence."""


class PaperSessionTimeout(PaperSessionError):
    """Stop remains requested but a terminal status and released lease were not seen."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _require_directory(fd: int, *, name: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise PaperSessionError(f"{name} must be one real 0700 directory")
    return info


@contextmanager
def _attempt(run_dir: Path) -> Iterator[tuple[Path, int]]:
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise PaperSessionError("run_dir must be an absolute Path")
    descriptor: int | None = None
    try:
        if run_dir.is_symlink():
            raise PaperSessionError("attempt final path cannot be a symlink")
        canonical = run_dir.resolve(strict=True)
        descriptor = os.open(canonical, _DIR_FLAGS)
        _require_directory(descriptor, name="attempt")
    except PaperSessionError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PaperSessionError("attempt path is unavailable or unsafe") from error
    assert descriptor is not None
    try:
        yield canonical, descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _child_directory(parent_fd: int, name: str) -> Iterator[int]:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        _require_directory(descriptor, name=name)
    except PaperSessionError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PaperSessionError(f"{name} directory is unavailable or unsafe") from error
    assert descriptor is not None
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read_file(parent_fd: int, name: str, *, optional: bool = False) -> bytes | None:
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if optional:
                return None
            raise
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 1 <= info.st_size <= _MAX_FILE_BYTES
        ):
            raise PaperSessionError(f"{name} must be one bounded regular 0600 file")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != info.st_size
            or after.st_size != info.st_size
            or _identity(after) != _identity(info)
        ):
            raise PaperSessionError(f"{name} changed while being read")
        return payload
    except PaperSessionError:
        raise
    except OSError as error:
        raise PaperSessionError(f"{name} is unavailable or unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _decode_object(payload: bytes, *, name: str) -> dict[str, Any]:
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (RecursionError, UnicodeError, ValueError) as error:
        raise PaperSessionError(f"{name} is malformed JSON") from error
    if type(document) is not dict:
        raise PaperSessionError(f"{name} must be one JSON object")
    return document


def _encode_object(document: dict[str, Any], *, name: str) -> bytes:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError, UnicodeError) as error:
        raise PaperSessionError(f"{name} is not JSON serializable") from error
    if not 1 <= len(payload) <= _MAX_FILE_BYTES:
        raise PaperSessionError(f"{name} exceeds the bounded file size")
    return payload


def _manifest(run_fd: int, run_name: str) -> tuple[RunBinding, bytes]:
    payload = _read_file(run_fd, "manifest.json")
    assert payload is not None
    document = _decode_object(payload, name="manifest")
    if document.get("schema") != _MANIFEST_SCHEMA:
        raise PaperSessionError("manifest schema is not local Paper")
    try:
        run_id = RunId(document["run_id"])
        lineage = Sha256Digest(document["lineage_sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise PaperSessionError("manifest identity is malformed") from error
    if run_id.value != run_name or _encode_object(document, name="manifest") != payload:
        raise PaperSessionError("manifest identity or canonical bytes conflict")
    return (
        RunBinding(
            RunReference(run_id, lineage),
            Sha256Digest(sha256(payload).hexdigest()),
        ),
        payload,
    )


def _status_document(payload: bytes, binding: RunBinding) -> dict[str, Any]:
    document = _decode_object(payload, name="paper status")
    if (
        document.get("schema") != _STATUS_SCHEMA
        or document.get("run_id") != binding.reference.run_id.value
        or document.get("manifest_sha256") != binding.manifest_sha256.value
        or type(document.get("state")) is not str
        or document["state"] not in _STATES
        or "lease_held" in document
        or (
            "reason" in document
            and document["reason"] is not None
            and type(document["reason"]) is not str
        )
        or _encode_object(document, name="paper status") != payload
    ):
        raise PaperSessionError("paper status identity or schema conflicts")
    return document


def _lease_held(run_fd: int) -> bool:
    with _child_directory(run_fd, "audit") as audit_fd:
        descriptor: int | None = None
        try:
            descriptor = os.open("writer-v1.lock", _READ_FLAGS, dir_fd=audit_fd)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise PaperSessionError("writer lease identity is invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        except PaperSessionError:
            raise
        except OSError as error:
            raise PaperSessionError("writer lease is unavailable or unsafe") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _observed_status(run_fd: int, outputs_fd: int, binding: RunBinding) -> dict[str, Any]:
    payload = _read_file(outputs_fd, "paper-status.json")
    assert payload is not None
    document = _status_document(payload, binding)
    held = _lease_held(run_fd)
    result = dict(document)
    result["lease_held"] = held
    if document["state"] not in _TERMINAL and not held:
        result["state"] = "interrupted"
        result["reason"] = "writer_lease_missing"
    return result


def _stop_payload(binding: RunBinding) -> bytes:
    return _encode_object(
        {
            "schema": _STOP_SCHEMA,
            "run_id": binding.reference.run_id.value,
            "manifest_sha256": binding.manifest_sha256.value,
        },
        name="paper stop request",
    )


def _stop_requested(outputs_fd: int, binding: RunBinding) -> bool:
    payload = _read_file(outputs_fd, "paper-stop.json", optional=True)
    if payload is None:
        return False
    if payload != _stop_payload(binding):
        raise PaperSessionError("paper stop request identity or schema conflicts")
    return True


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise PaperSessionError("paper output write made no progress")
        remaining = remaining[written:]


def _write_stop_once(outputs_fd: int, binding: RunBinding) -> None:
    if _stop_requested(outputs_fd, binding):
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(
            "paper-stop.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=outputs_fd,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, _stop_payload(binding))
        os.fsync(descriptor)
        os.fsync(outputs_fd)
    except FileExistsError:
        if not _stop_requested(outputs_fd, binding):
            raise PaperSessionError("paper stop request appeared without valid bytes") from None
    except OSError as error:
        raise PaperSessionError("paper stop request could not be written durably") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


class PaperSessionWriter:
    """One manifest-bound publisher; the store keeps ownership of the lease."""

    def __init__(self, run_dir: Path, binding: RunBinding) -> None:
        if type(binding) is not RunBinding:
            raise PaperSessionError("writer requires an exact RunBinding")
        with _attempt(run_dir) as (canonical, run_fd):
            actual, manifest_bytes = _manifest(run_fd, canonical.name)
            if actual != binding:
                raise PaperSessionError("manifest binding conflicts with writer")
            with _child_directory(run_fd, "outputs") as outputs_fd:
                self._outputs_identity = _identity(os.fstat(outputs_fd))
            self._run_identity = _identity(os.fstat(run_fd))
            self._run_dir = canonical
            self._binding = binding
            self._manifest_bytes = manifest_bytes

    @contextmanager
    def _open_bound(self) -> Iterator[tuple[int, int]]:
        with _attempt(self._run_dir) as (_, run_fd):
            if _identity(os.fstat(run_fd)) != self._run_identity:
                raise PaperSessionError("attempt directory identity changed")
            with _child_directory(run_fd, "outputs") as outputs_fd:
                if _identity(os.fstat(outputs_fd)) != self._outputs_identity:
                    raise PaperSessionError("outputs directory identity changed")
                yield run_fd, outputs_fd

    def publish(self, document: dict[str, Any]) -> None:
        if type(document) is not dict or any(type(key) is not str for key in document):
            raise PaperSessionError("paper status must be one string-keyed object")
        expected = {
            "schema": _STATUS_SCHEMA,
            "run_id": self._binding.reference.run_id.value,
            "manifest_sha256": self._binding.manifest_sha256.value,
        }
        if any(key in document and document[key] != value for key, value in expected.items()):
            raise PaperSessionError("paper status supplied identity conflicts")
        if "lease_held" in document:
            raise PaperSessionError("paper status cannot supply observed lease state")
        assembled = {**document, **expected}
        if type(assembled.get("state")) is not str or assembled["state"] not in _STATES:
            raise PaperSessionError("paper status state is invalid")
        if (
            "reason" in assembled
            and assembled["reason"] is not None
            and type(assembled["reason"]) is not str
        ):
            raise PaperSessionError("paper status reason must be text or null")
        payload = _encode_object(assembled, name="paper status")
        with self._open_bound() as (run_fd, outputs_fd):
            if not _lease_held(run_fd):
                raise PaperSessionError("writer lease is no longer held")
            old = _read_file(outputs_fd, "paper-status.json", optional=True)
            if old is not None:
                _status_document(old, self._binding)
            temp_name = f".paper-status.{uuid4().hex}.tmp"
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=outputs_fd,
                )
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, payload)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(
                    temp_name,
                    "paper-status.json",
                    src_dir_fd=outputs_fd,
                    dst_dir_fd=outputs_fd,
                )
                os.fsync(outputs_fd)
            except OSError as error:
                raise PaperSessionError("paper status could not be published atomically") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temp_name, dir_fd=outputs_fd)

    def stop_requested(self) -> bool:
        with self._open_bound() as (_, outputs_fd):
            return _stop_requested(outputs_fd, self._binding)


def read_paper_status(run_dir: Path) -> dict[str, Any]:
    """Read one bound status and overlay the actual writer lease observation."""
    with _attempt(run_dir) as (canonical, run_fd):
        binding, _ = _manifest(run_fd, canonical.name)
        with _child_directory(run_fd, "outputs") as outputs_fd:
            return _observed_status(run_fd, outputs_fd, binding)


def request_paper_stop(run_dir: Path, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Durably request cooperative stop and wait for terminal plus lease release."""
    if type(timeout_seconds) is not float or not isfinite(timeout_seconds) or timeout_seconds < 0:
        raise PaperSessionError("stop timeout must be a nonnegative finite float")
    with _attempt(run_dir) as (canonical, run_fd):
        binding, _ = _manifest(run_fd, canonical.name)
        with _child_directory(run_fd, "outputs") as outputs_fd:
            _observed_status(run_fd, outputs_fd, binding)
            _write_stop_once(outputs_fd, binding)
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = read_paper_status(run_dir)
        if status["state"] in _TERMINAL and status["lease_held"] is False:
            return status
        if time.monotonic() >= deadline:
            raise PaperSessionTimeout(
                "paper stop remains requested; terminal release was not observed"
            )
        time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
