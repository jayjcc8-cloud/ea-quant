"""Dependency-neutral run identity and reproducibility values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import RFC_4122, UUID

from ea.core.time import TimeValidationError, require_utc

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)
_LABEL_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z", flags=re.ASCII)
_MAX_UINT64 = (1 << 64) - 1


class RunContractError(ValueError):
    """Raised when a run identity or reproducibility value is invalid."""


class RunBindingMismatchError(RunContractError):
    """Raised before an audit or result write uses the wrong run binding."""


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    """Canonical lowercase SHA-256 digest."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or _DIGEST_PATTERN.fullmatch(self.value) is None:
            raise RunContractError("SHA-256 digest must be 64 lowercase hexadecimal characters")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RunId:
    """Canonical lowercase RFC 4122 UUID4 attempt identifier."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or len(self.value) != 36:
            raise RunContractError("run_id must be a canonical 36-character UUID4")
        try:
            parsed = UUID(self.value)
        except (AttributeError, ValueError) as exc:
            raise RunContractError("run_id must be a canonical UUID4") from exc
        if (
            parsed.version != 4
            or parsed.variant != RFC_4122
            or str(parsed) != self.value
            or parsed.int == 0
        ):
            raise RunContractError("run_id must be a canonical lowercase RFC 4122 UUID4")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ReplayWindow:
    """Exact UTC knowledge-time selection window ``[start, end)``."""

    start_inclusive: datetime
    end_exclusive: datetime

    def __post_init__(self) -> None:
        try:
            start = require_utc(self.start_inclusive, field="start_inclusive")
            end = require_utc(self.end_exclusive, field="end_exclusive")
        except TimeValidationError as exc:
            raise RunContractError(str(exc)) from exc
        if start >= end:
            raise RunContractError("start_inclusive must be earlier than end_exclusive")
        object.__setattr__(self, "start_inclusive", start)
        object.__setattr__(self, "end_exclusive", end)


@dataclass(frozen=True, slots=True)
class DataFingerprint:
    """Digest and non-empty record count for an immutable replay tuple."""

    sha256: Sha256Digest
    record_count: int

    def __post_init__(self) -> None:
        if type(self.sha256) is not Sha256Digest:
            raise RunContractError("data fingerprint sha256 must be a Sha256Digest")
        if (
            type(self.record_count) is not int
            or self.record_count < 1
            or self.record_count > _MAX_UINT64
        ):
            raise RunContractError("data fingerprint record_count must be in 1..2**64-1")


@dataclass(frozen=True, slots=True)
class RunReference:
    """Narrow identity shared by runtime, audit, and result boundaries."""

    run_id: RunId
    lineage_sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.run_id) is not RunId:
            raise RunContractError("run reference run_id must be a RunId")
        if type(self.lineage_sha256) is not Sha256Digest:
            raise RunContractError("run reference lineage_sha256 must be a Sha256Digest")


@dataclass(frozen=True, slots=True)
class RunBinding:
    """Attempt identity plus the digest of its durable manifest bytes."""

    reference: RunReference
    manifest_sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.reference) is not RunReference:
            raise RunContractError("run binding reference must be a RunReference")
        if type(self.manifest_sha256) is not Sha256Digest:
            raise RunContractError("run binding manifest_sha256 must be a Sha256Digest")


def require_run_binding(expected: RunBinding, actual: RunBinding) -> None:
    """Reject a mismatched audit/result binding before the boundary is called."""
    if type(expected) is not RunBinding or type(actual) is not RunBinding or actual != expected:
        raise RunBindingMismatchError("run reference or manifest digest does not match")


def validate_stream_label(label: str) -> str:
    """Return a canonical component RNG label or fail without coercion."""
    if type(label) is not str or _LABEL_PATTERN.fullmatch(label) is None:
        raise RunContractError("stream label must match [a-z][a-z0-9_.-]{0,127}")
    return label


def derive_component_seed(master_seed: int, label: str) -> int:
    """Derive the ADR 0006 unsigned 128-bit PCG64 component seed."""
    if type(master_seed) is not int or master_seed < 0 or master_seed > _MAX_UINT64:
        raise RunContractError("master_seed must be an unsigned 64-bit integer")
    canonical_label = validate_stream_label(label)
    encoded_label = canonical_label.encode("ascii")
    payload = (
        b"ea.rng-stream.v1\0"
        + master_seed.to_bytes(8, "big")
        + len(encoded_label).to_bytes(2, "big")
        + encoded_label
    )
    return int.from_bytes(sha256(payload).digest()[:16], "big")
