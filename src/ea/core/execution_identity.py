"""Canonical execution identities and pure replay classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import final

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest

ECONOMIC_ID_SCHEMA_VERSION = 1
ECONOMIC_ID_CANONICALIZATION = "ea-economic-id-v1"
ECONOMIC_ID_DIGEST_DOMAIN = b"ea.economic-id.v1\0"

EXECUTION_INGRESS_ID_SCHEMA_VERSION = 1
EXECUTION_INGRESS_ID_CANONICALIZATION = "ea-execution-ingress-id-v1"
EXECUTION_INGRESS_ID_DIGEST_DOMAIN = b"ea.execution-ingress-id.v1\0"

FACT_DEDUP_ID_SCHEMA_VERSION = 1
FACT_DEDUP_ID_CANONICALIZATION = "ea-fact-dedup-id-v1"
FACT_DEDUP_ID_DIGEST_DOMAIN = b"ea.fact-dedup-id.v1\0"

FACT_DEDUP_KEY_SCHEMA_VERSION = 1
FACT_DEDUP_KEY_CANONICALIZATION = "ea-fact-dedup-key-v1"
FACT_DEDUP_KEY_DIGEST_DOMAIN = b"ea.fact-dedup-key.v1\0"

_MAX_UINT64 = (1 << 64) - 1
_SOURCE_NAMESPACE_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z", flags=re.ASCII)
_IDENTITY_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class ExecutionIdentityError(ValueError):
    """Structured fail-closed identity validation error."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _IDENTITY_ERROR_CODES:
            raise TypeError("identity errors require an exact identity OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> ExecutionIdentityError:
    return ExecutionIdentityError(code, message)


def _require_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type int",
        )
    if value < 0 or value > _MAX_UINT64:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} must be an unsigned 64-bit integer",
        )
    return value


def _require_non_negative_integer(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type int",
        )
    if value < 0:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} must be non-negative",
        )
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_string(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")


def _non_negative_integer_bytes(value: int) -> bytes:
    if value == 0:
        return b"0"
    chunks: list[int] = []
    remaining = value
    while remaining:
        remaining, chunk = divmod(remaining, 1_000_000_000)
        chunks.append(chunk)
    most_significant = str(chunks.pop()).encode("ascii")
    suffix = b"".join(f"{chunk:09d}".encode("ascii") for chunk in reversed(chunks))
    return most_significant + suffix


def _digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())


class EconomicOwnerKind(StrEnum):
    """Closed owners of run-scoped economic sequences."""

    STRATEGY_SIGNAL = "strategy.signal"
    PORTFOLIO_TARGET = "portfolio.target"
    PORTFOLIO_INTENT = "portfolio.intent"
    RISK_DECISION = "risk.decision"
    RISK_APPROVAL = "risk.approval"
    EXECUTION_ORDER = "execution.order"
    EXECUTION_FILL = "execution.fill"
    RUNTIME_DISPATCH = "runtime.dispatch"
    AUDIT_RECORD = "audit.record"
    LEDGER_ENTRY = "ledger.entry"
    RECONCILIATION_OBSERVATION = "reconciliation.observation"


@final
@dataclass(frozen=True, slots=True)
class EconomicId:
    """One run-scoped owner-created identity without ordering semantics."""

    run_id: RunId
    owner_kind: EconomicOwnerKind
    owner_sequence: int

    def __post_init__(self) -> None:
        if type(self.run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
        if type(self.owner_kind) is not EconomicOwnerKind:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "owner_kind must be an exact EconomicOwnerKind",
            )
        _require_uint64(self.owner_sequence, field_name="owner_sequence")


def require_owner_kind(
    identity: EconomicId,
    expected: EconomicOwnerKind,
) -> EconomicId:
    """Require one exact owner binding without coercion."""
    if type(identity) is not EconomicId or type(expected) is not EconomicOwnerKind:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "identity and expected owner kind must have exact canonical types",
        )
    if identity.owner_kind is not expected:
        raise _fail(OutcomeCode.CONFLICTING_ID, "economic identity owner kind conflicts")
    return identity


def require_same_run(first: EconomicId, second: EconomicId) -> None:
    """Require two exact economic identities to share their run scope."""
    if type(first) is not EconomicId or type(second) is not EconomicId:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "same-run operands must be exact EconomicId values",
        )
    if first.run_id != second.run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "economic identities have conflicting runs")


def canonical_economic_id_bytes(identity: EconomicId) -> bytes:
    """Return the closed canonical JSON for one economic identity."""
    if type(identity) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "identity must be an exact EconomicId")
    return _canonical_json_bytes(
        {
            "canonicalization": ECONOMIC_ID_CANONICALIZATION,
            "owner_kind": identity.owner_kind.value,
            "owner_sequence": identity.owner_sequence,
            "run_id": identity.run_id.value,
            "schema_version": ECONOMIC_ID_SCHEMA_VERSION,
        }
    )


def economic_id_digest(identity: EconomicId) -> Sha256Digest:
    """Derive the internally owned economic-identity digest."""
    return _digest(ECONOMIC_ID_DIGEST_DOMAIN, canonical_economic_id_bytes(identity))


@final
@dataclass(frozen=True, slots=True)
class SourceNamespace:
    """Canonical execution-fact source namespace."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "source namespace must have exact runtime type str",
            )
        if _SOURCE_NAMESPACE_PATTERN.fullmatch(self.value) is None:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "source namespace must match [a-z][a-z0-9._-]{0,127}",
            )


@final
@dataclass(frozen=True, slots=True)
class ExternalFactId:
    """Stable visible-ASCII source-provided fact identity."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "external fact ID must have exact runtime type str",
            )
        if not 1 <= len(self.value) <= 128 or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in self.value
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "external fact ID must contain 1..128 visible ASCII characters",
            )


@final
@dataclass(frozen=True, slots=True)
class SourceNativeSequence:
    """Stable exact non-negative source sequence without an artificial maximum."""

    value: int

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.value, field_name="source native sequence")


FactDedupIdentity = ExternalFactId | SourceNativeSequence


def _fact_dedup_mapping(identity: FactDedupIdentity) -> dict[str, object]:
    if type(identity) is ExternalFactId:
        return {
            "canonicalization": FACT_DEDUP_ID_CANONICALIZATION,
            "kind": "external_id",
            "schema_version": FACT_DEDUP_ID_SCHEMA_VERSION,
            "value": identity.value,
        }
    if type(identity) is SourceNativeSequence:
        return {
            "canonicalization": FACT_DEDUP_ID_CANONICALIZATION,
            "kind": "source_native_sequence",
            "schema_version": FACT_DEDUP_ID_SCHEMA_VERSION,
            "value": identity.value,
        }
    raise _fail(
        OutcomeCode.INVALID_TYPE,
        "fact dedup identity must be an exact tagged variant",
    )


def canonical_fact_dedup_identity_bytes(identity: FactDedupIdentity) -> bytes:
    """Return canonical bytes for one exact tagged fact identity."""
    mapping = _fact_dedup_mapping(identity)
    if type(identity) is ExternalFactId:
        return _canonical_json_bytes(mapping)
    return (
        b'{"canonicalization":"ea-fact-dedup-id-v1",'
        b'"kind":"source_native_sequence","schema_version":1,"value":'
        + _non_negative_integer_bytes(identity.value)
        + b"}"
    )


def fact_dedup_identity_digest(identity: FactDedupIdentity) -> Sha256Digest:
    """Derive the tagged fact-identity digest."""
    return _digest(
        FACT_DEDUP_ID_DIGEST_DOMAIN,
        canonical_fact_dedup_identity_bytes(identity),
    )


@final
@dataclass(frozen=True, slots=True)
class IngressIdentity:
    """One delivery occurrence within an execution source namespace."""

    source_namespace: SourceNamespace
    ingress_sequence: int

    def __post_init__(self) -> None:
        if type(self.source_namespace) is not SourceNamespace:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "source_namespace must be an exact SourceNamespace",
            )
        _require_non_negative_integer(self.ingress_sequence, field_name="ingress_sequence")


def canonical_ingress_identity_bytes(identity: IngressIdentity) -> bytes:
    """Return canonical bytes for an ingress occurrence identity."""
    if type(identity) is not IngressIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "identity must be an exact IngressIdentity")
    return (
        b'{"canonicalization":"ea-execution-ingress-id-v1","ingress_sequence":'
        + _non_negative_integer_bytes(identity.ingress_sequence)
        + b',"schema_version":1,"source_namespace":'
        + _json_string(identity.source_namespace.value)
        + b"}"
    )


def ingress_identity_digest(identity: IngressIdentity) -> Sha256Digest:
    """Derive the ingress-identity digest."""
    return _digest(
        EXECUTION_INGRESS_ID_DIGEST_DOMAIN,
        canonical_ingress_identity_bytes(identity),
    )


@final
@dataclass(frozen=True, slots=True)
class FactDedupKey:
    """Source-scoped fact identity used for replay/conflict classification."""

    source_namespace: SourceNamespace
    identity: FactDedupIdentity

    def __post_init__(self) -> None:
        if type(self.source_namespace) is not SourceNamespace:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "source_namespace must be an exact SourceNamespace",
            )
        if type(self.identity) not in (ExternalFactId, SourceNativeSequence):
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "identity must be an exact fact dedup variant",
            )


def canonical_fact_dedup_key_bytes(key: FactDedupKey) -> bytes:
    """Return canonical bytes for the source-scoped fact key."""
    if type(key) is not FactDedupKey:
        raise _fail(OutcomeCode.INVALID_TYPE, "key must be an exact FactDedupKey")
    identity_mapping = _fact_dedup_mapping(key.identity)
    value_bytes = (
        _json_string(key.identity.value)
        if type(key.identity) is ExternalFactId
        else _non_negative_integer_bytes(key.identity.value)
    )
    return (
        b'{"canonicalization":"ea-fact-dedup-key-v1","identity":{"kind":'
        + _json_string(str(identity_mapping["kind"]))
        + b',"value":'
        + value_bytes
        + b'},"schema_version":1,"source_namespace":'
        + _json_string(key.source_namespace.value)
        + b"}"
    )


def fact_dedup_key_digest(key: FactDedupKey) -> Sha256Digest:
    """Derive the source-scoped fact-key digest."""
    return _digest(FACT_DEDUP_KEY_DIGEST_DOMAIN, canonical_fact_dedup_key_bytes(key))


class IdentityDisposition(StrEnum):
    """Pure same-identity replay classification."""

    NEW = "new"
    EXACT_REPLAY = "exact_replay"
    CONFLICT = "conflict"


ReplayIdentity = EconomicId | IngressIdentity | FactDedupKey
_REPLAY_IDENTITY_TYPES = (EconomicId, IngressIdentity, FactDedupKey)


def classify_identity_replay(
    existing_identity: ReplayIdentity,
    existing_payload: bytes,
    candidate_identity: ReplayIdentity,
    candidate_payload: bytes,
) -> IdentityDisposition:
    """Classify exact identity/payload equality without owning any state."""
    if (
        type(existing_identity) not in _REPLAY_IDENTITY_TYPES
        or type(candidate_identity) not in _REPLAY_IDENTITY_TYPES
        or type(existing_payload) is not bytes
        or type(candidate_payload) is not bytes
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "replay classification requires exact supported identities and bytes",
        )
    if type(existing_identity) is not type(candidate_identity):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "replay identities must have the same exact concrete type",
        )
    if existing_identity != candidate_identity:
        return IdentityDisposition.NEW
    if existing_payload == candidate_payload:
        return IdentityDisposition.EXACT_REPLAY
    return IdentityDisposition.CONFLICT
