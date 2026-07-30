"""Canonical strategy signals and authority evidence from Accepted ADR 0017."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, final

from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.identity import Instrument
from ea.core.market_data import MarketDataEnvelope, MarketDataKind
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

if TYPE_CHECKING:
    from ea.core.runtime import ActiveMarketDispatchProof

STRATEGY_SIGNAL_SCHEMA = "ea.phase1-strategy-signal.v1"
STRATEGY_SIGNAL_CANONICALIZATION = "ea-canonical-json-v1"
STRATEGY_SIGNAL_DIGEST_DOMAIN = b"ea.phase1-strategy-signal.v1\0"
STRATEGY_CAUSAL_MARKET_DIGEST_DOMAIN = b"ea.phase1-strategy-causal-market.v1\0"
STRATEGY_SIGNAL_SUBMISSION_DIGEST_DOMAIN = b"ea.phase1-strategy-signal-submission.v1\0"
AUTHORITY_CONFLICT_SCHEMA = "ea.phase1-authority-conflict.v1"
AUTHORITY_CONFLICT_DIGEST_DOMAIN = b"ea.phase1-authority-conflict.v1\0"
STRATEGY_SIGNAL_AUTHORITY_STATE_SCHEMA = "ea.phase1-strategy-signal-authority-state.v1"
STRATEGY_SIGNAL_AUTHORITY_STATE_DIGEST_DOMAIN = b"ea.phase1-strategy-signal-authority-state.v1\0"

_MAX_UINT64 = (1 << 64) - 1
_STRATEGY_SIGNAL_SEAL = object()
_AUTHORITY_CONFLICT_SEAL = object()
_STRATEGY_AUTHORITY_STATE_SEAL = object()
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class StrategyContractError(ValueError):
    """Closed public strategy/proof/authority failure."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("strategy errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> StrategyContractError:
    return StrategyContractError(code, message)


class SignalDirection(StrEnum):
    LONG = "long"
    FLAT = "flat"
    SHORT = "short"


class AuthorityKind(StrEnum):
    STRATEGY_SIGNAL = "strategy_signal"
    PORTFOLIO_PLANNING = "portfolio_planning"


class AuthorityConflictKind(StrEnum):
    IDENTITY_REUSE = "identity_reuse"
    NON_MONOTONE_DISPATCH = "non_monotone_dispatch"


class ActiveMarketDispatchVerifierPort(Protocol):
    """Consumer-owned read-only proof port."""

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> ActiveMarketDispatchProof: ...


@final
@dataclass(frozen=True, slots=True, init=False)
class StrategySignal:
    run_id: RunId
    signal_id: EconomicId
    instrument: Instrument
    direction: SignalDirection
    causal_market_sha256: Sha256Digest
    causal_root_available_at: datetime
    dispatch_sequence: int
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("StrategySignal values are created only by StrategySignalAuthority")


@final
@dataclass(frozen=True, slots=True, init=False)
class AuthorityConflictEvidence:
    authority_kind: AuthorityKind
    conflict_kind: AuthorityConflictKind
    run_id: RunId
    occupied_owner_id: EconomicId | None
    dispatch_sequence: int
    last_new_dispatch_sequence: int | None
    existing_sha256: Sha256Digest | None
    submitted_sha256: Sha256Digest
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("AuthorityConflictEvidence values are created only by an authority")


@final
@dataclass(frozen=True, slots=True, init=False)
class StrategySignalAuthorityState:
    run_id: RunId
    halted: bool
    signal_next: int | None
    last_new_dispatch_sequence: int | None
    issuance_count: int
    conflict: AuthorityConflictEvidence | None
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("StrategySignalAuthorityState values are created only by an authority")


def _require_positive_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must have exact runtime type int")
    if value < 1 or value > _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be in 1..2**64-1")
    return value


def _require_optional_positive_uint64(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_positive_uint64(value, field_name=field_name)


def _require_non_negative_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must have exact runtime type int")
    if value < 0 or value > _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be an unsigned 64-bit integer")
    return value


def _encode_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "strategy value cannot be canonically encoded",
        ) from error


def _digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())


def _validate_economic_id(
    identity: EconomicId,
    *,
    field_name: str,
    expected_owner: EconomicOwnerKind | None = None,
    expected_run: RunId | None = None,
) -> None:
    if type(identity) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an exact EconomicId")
    if type(identity.run_id) is not RunId or type(identity.owner_kind) is not EconomicOwnerKind:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} nested identity carriers must be exact",
        )
    _require_positive_uint64(identity.owner_sequence, field_name=f"{field_name}.owner_sequence")
    if expected_owner is not None and identity.owner_kind is not expected_owner:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} owner kind conflicts")
    if expected_run is not None and identity.run_id != expected_run:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} run conflicts")


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    _validate_economic_id(identity, field_name="economic ID")
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _optional_economic_id_document(identity: EconomicId | None) -> dict[str, object] | None:
    return None if identity is None else _economic_id_document(identity)


def _instrument_document(instrument: Instrument) -> dict[str, object]:
    if type(instrument) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument must be exact")
    return {"symbol": instrument.symbol, "venue": instrument.venue.code}


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, "causal time must have exact runtime type datetime")
    try:
        return require_utc(value, field="causal_root_available_at").strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "causal time must be canonical UTC") from error


def causal_market_digest(market_root: MarketDataEnvelope) -> Sha256Digest:
    """Digest one exact canonical admitted market record with its ADR 0017 domain."""
    if type(market_root) is not MarketDataEnvelope:
        raise _fail(OutcomeCode.INVALID_TYPE, "market root must be an exact MarketDataEnvelope")
    try:
        payload = canonical_market_data_record_bytes(market_root)
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "market root cannot be canonically encoded",
        ) from error
    return _causal_market_digest_from_canonical_bytes(payload)


def _causal_market_digest_from_canonical_bytes(payload: bytes) -> Sha256Digest:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical market bytes must be exact")
    return _digest(
        STRATEGY_CAUSAL_MARKET_DIGEST_DOMAIN,
        len(payload).to_bytes(8, "big") + payload,
    )


def strategy_signal_submission_digest(
    market_root: MarketDataEnvelope,
    *,
    dispatch_sequence: int,
    direction: SignalDirection,
) -> Sha256Digest:
    """Digest the minimal valid submitted signal input before state lookup."""
    if type(market_root) is not MarketDataEnvelope:
        raise _fail(OutcomeCode.INVALID_TYPE, "market root must be an exact MarketDataEnvelope")
    if type(direction) is not SignalDirection:
        raise _fail(OutcomeCode.INVALID_TYPE, "direction must be an exact SignalDirection")
    sequence = _require_positive_uint64(dispatch_sequence, field_name="dispatch_sequence")
    try:
        market_bytes = canonical_market_data_record_bytes(market_root)
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "market root cannot be canonically encoded",
        ) from error
    direction_bytes = direction.value.encode("ascii")
    preimage = (
        len(market_bytes).to_bytes(8, "big")
        + market_bytes
        + sequence.to_bytes(8, "big")
        + len(direction_bytes).to_bytes(8, "big")
        + direction_bytes
    )
    return _digest(STRATEGY_SIGNAL_SUBMISSION_DIGEST_DOMAIN, preimage)


def _validate_signal(signal: StrategySignal) -> None:
    if type(signal) is not StrategySignal:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal must be an exact StrategySignal")
    if type(signal.run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal run_id must be exact")
    if signal._seal is not _STRATEGY_SIGNAL_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal is not factory-issued")
    _validate_economic_id(
        signal.signal_id,
        field_name="signal ID",
        expected_owner=EconomicOwnerKind.STRATEGY_SIGNAL,
        expected_run=signal.run_id,
    )
    if type(signal.instrument) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal instrument must be exact")
    if type(signal.direction) is not SignalDirection:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal direction must be exact")
    if type(signal.causal_market_sha256) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal causal digest must be exact")
    _utc_text(signal.causal_root_available_at)
    _require_positive_uint64(signal.dispatch_sequence, field_name="dispatch_sequence")


def _signal_document(signal: StrategySignal) -> dict[str, object]:
    _validate_signal(signal)
    return {
        "canonicalization": STRATEGY_SIGNAL_CANONICALIZATION,
        "causal_market_sha256": signal.causal_market_sha256.value,
        "causal_root_available_at": _utc_text(signal.causal_root_available_at),
        "direction": signal.direction.value,
        "dispatch_sequence": signal.dispatch_sequence,
        "instrument": _instrument_document(signal.instrument),
        "run_id": signal.run_id.value,
        "schema": STRATEGY_SIGNAL_SCHEMA,
        "signal_id": _economic_id_document(signal.signal_id),
    }


def canonical_strategy_signal_bytes(signal: StrategySignal) -> bytes:
    try:
        return _encode_json(_signal_document(signal))
    except StrategyContractError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "signal cannot be canonically encoded",
        ) from error


def strategy_signal_digest(signal: StrategySignal) -> Sha256Digest:
    return _digest(STRATEGY_SIGNAL_DIGEST_DOMAIN, canonical_strategy_signal_bytes(signal))


def _create_strategy_signal(
    *,
    run_id: RunId,
    signal_id: EconomicId,
    market_root: MarketDataEnvelope,
    direction: SignalDirection,
    causal_market_sha256: Sha256Digest,
    dispatch_sequence: int,
) -> StrategySignal:
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
    if type(market_root) is not MarketDataEnvelope or market_root.kind is not MarketDataKind.BAR:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal cause must be an exact market Bar envelope")
    value = object.__new__(StrategySignal)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "signal_id", signal_id)
    object.__setattr__(value, "instrument", market_root.payload.instrument)
    object.__setattr__(value, "direction", direction)
    object.__setattr__(value, "causal_market_sha256", causal_market_sha256)
    object.__setattr__(value, "causal_root_available_at", market_root.available_at)
    object.__setattr__(value, "dispatch_sequence", dispatch_sequence)
    object.__setattr__(value, "_seal", _STRATEGY_SIGNAL_SEAL)
    canonical_strategy_signal_bytes(value)
    return value


def _create_authority_conflict_evidence(
    *,
    authority_kind: AuthorityKind,
    conflict_kind: AuthorityConflictKind,
    run_id: RunId,
    occupied_owner_id: EconomicId | None,
    dispatch_sequence: int,
    last_new_dispatch_sequence: int | None,
    existing_sha256: Sha256Digest | None,
    submitted_sha256: Sha256Digest,
) -> AuthorityConflictEvidence:
    value = object.__new__(AuthorityConflictEvidence)
    object.__setattr__(value, "authority_kind", authority_kind)
    object.__setattr__(value, "conflict_kind", conflict_kind)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "occupied_owner_id", occupied_owner_id)
    object.__setattr__(value, "dispatch_sequence", dispatch_sequence)
    object.__setattr__(value, "last_new_dispatch_sequence", last_new_dispatch_sequence)
    object.__setattr__(value, "existing_sha256", existing_sha256)
    object.__setattr__(value, "submitted_sha256", submitted_sha256)
    object.__setattr__(value, "_seal", _AUTHORITY_CONFLICT_SEAL)
    canonical_authority_conflict_evidence_bytes(value)
    return value


def _validate_conflict(value: AuthorityConflictEvidence) -> None:
    if type(value) is not AuthorityConflictEvidence:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict evidence must be exact")
    if type(value.authority_kind) is not AuthorityKind:
        raise _fail(OutcomeCode.INVALID_TYPE, "authority kind must be exact")
    if value._seal is not _AUTHORITY_CONFLICT_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict evidence is not factory-issued")
    if type(value.conflict_kind) is not AuthorityConflictKind:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict kind must be exact")
    if type(value.run_id) is not RunId or type(value.submitted_sha256) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict run/digest carriers must be exact")
    _require_positive_uint64(value.dispatch_sequence, field_name="dispatch_sequence")
    _require_optional_positive_uint64(
        value.last_new_dispatch_sequence,
        field_name="last_new_dispatch_sequence",
    )
    if value.occupied_owner_id is not None:
        _validate_economic_id(
            value.occupied_owner_id,
            field_name="occupied owner ID",
            expected_owner=EconomicOwnerKind.STRATEGY_SIGNAL,
            expected_run=value.run_id,
        )
    if value.existing_sha256 is not None and type(value.existing_sha256) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, "existing digest must be exact or None")
    occupied = value.occupied_owner_id is not None
    existing = value.existing_sha256 is not None
    if occupied is not existing:
        raise _fail(OutcomeCode.CONFLICTING_ID, "occupied identity and digest must coexist")
    if value.conflict_kind is AuthorityConflictKind.IDENTITY_REUSE:
        if (
            not occupied
            or value.occupied_owner_id is None
            or value.occupied_owner_id.owner_kind is not EconomicOwnerKind.STRATEGY_SIGNAL
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "identity-reuse evidence must identify an occupied signal",
            )
    elif occupied:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "non-monotone evidence cannot identify an occupied signal",
        )


def _conflict_document(value: AuthorityConflictEvidence) -> dict[str, object]:
    _validate_conflict(value)
    return {
        "authority_kind": value.authority_kind.value,
        "canonicalization": STRATEGY_SIGNAL_CANONICALIZATION,
        "conflict_kind": value.conflict_kind.value,
        "dispatch_sequence": value.dispatch_sequence,
        "existing_sha256": None if value.existing_sha256 is None else value.existing_sha256.value,
        "last_new_dispatch_sequence": value.last_new_dispatch_sequence,
        "occupied_owner_id": _optional_economic_id_document(value.occupied_owner_id),
        "run_id": value.run_id.value,
        "schema": AUTHORITY_CONFLICT_SCHEMA,
        "submitted_sha256": value.submitted_sha256.value,
    }


def canonical_authority_conflict_evidence_bytes(value: AuthorityConflictEvidence) -> bytes:
    try:
        return _encode_json(_conflict_document(value))
    except StrategyContractError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "conflict evidence cannot be canonically encoded",
        ) from error


def authority_conflict_evidence_digest(value: AuthorityConflictEvidence) -> Sha256Digest:
    return _digest(
        AUTHORITY_CONFLICT_DIGEST_DOMAIN,
        canonical_authority_conflict_evidence_bytes(value),
    )


def _create_strategy_signal_authority_state(
    *,
    run_id: RunId,
    halted: bool,
    signal_next: int | None,
    last_new_dispatch_sequence: int | None,
    issuance_count: int,
    conflict: AuthorityConflictEvidence | None,
) -> StrategySignalAuthorityState:
    value = object.__new__(StrategySignalAuthorityState)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "halted", halted)
    object.__setattr__(value, "signal_next", signal_next)
    object.__setattr__(value, "last_new_dispatch_sequence", last_new_dispatch_sequence)
    object.__setattr__(value, "issuance_count", issuance_count)
    object.__setattr__(value, "conflict", conflict)
    object.__setattr__(value, "_seal", _STRATEGY_AUTHORITY_STATE_SEAL)
    canonical_strategy_signal_authority_state_bytes(value)
    return value


def _strategy_signal_authority_state_document(
    value: StrategySignalAuthorityState,
) -> dict[str, object]:
    if type(value) is not StrategySignalAuthorityState:
        raise _fail(OutcomeCode.INVALID_TYPE, "strategy authority state must be exact")
    if type(value.run_id) is not RunId or type(value.halted) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "strategy authority state carriers must be exact")
    if value._seal is not _STRATEGY_AUTHORITY_STATE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "strategy authority state is not factory-issued")
    _require_optional_positive_uint64(value.signal_next, field_name="signal_next")
    _require_optional_positive_uint64(
        value.last_new_dispatch_sequence,
        field_name="last_new_dispatch_sequence",
    )
    _require_non_negative_uint64(value.issuance_count, field_name="issuance_count")
    if value.conflict is not None:
        _validate_conflict(value.conflict)
        if (
            value.conflict.authority_kind is not AuthorityKind.STRATEGY_SIGNAL
            or value.conflict.run_id != value.run_id
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "strategy conflict state lineage conflicts")
    if value.halted is (value.conflict is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "halt and conflict evidence must agree")
    return {
        "canonicalization": STRATEGY_SIGNAL_CANONICALIZATION,
        "conflict": None if value.conflict is None else _conflict_document(value.conflict),
        "halted": value.halted,
        "issuance_count": value.issuance_count,
        "last_new_dispatch_sequence": value.last_new_dispatch_sequence,
        "run_id": value.run_id.value,
        "schema": STRATEGY_SIGNAL_AUTHORITY_STATE_SCHEMA,
        "signal_next": value.signal_next,
    }


def canonical_strategy_signal_authority_state_bytes(
    value: StrategySignalAuthorityState,
) -> bytes:
    try:
        return _encode_json(_strategy_signal_authority_state_document(value))
    except StrategyContractError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "strategy authority state cannot be canonically encoded",
        ) from error


def strategy_signal_authority_state_digest(
    value: StrategySignalAuthorityState,
) -> Sha256Digest:
    return _digest(
        STRATEGY_SIGNAL_AUTHORITY_STATE_DIGEST_DOMAIN,
        canonical_strategy_signal_authority_state_bytes(value),
    )
