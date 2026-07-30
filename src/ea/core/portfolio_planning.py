"""Canonical portfolio planning contracts from Accepted ADR 0017."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import (
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    OrderIntent,
    canonical_order_intent_bytes,
    order_intent_digest,
)
from ea.core.identity import Instrument
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    PortfolioSnapshot,
    canonical_portfolio_snapshot_bytes,
    portfolio_snapshot_digest,
)
from ea.core.run import RunId, Sha256Digest
from ea.core.strategy import (
    AuthorityConflictEvidence,
    AuthorityKind,
    StrategyContractError,
    StrategySignal,
    _conflict_document,
    _validate_conflict,
    canonical_strategy_signal_bytes,
    strategy_signal_digest,
)
from ea.core.time import TimeValidationError, require_utc

PORTFOLIO_POLICY_SCHEMA = "ea.phase1-portfolio-policy.v1"
PORTFOLIO_POLICY_DIGEST_DOMAIN = b"ea.phase1-portfolio-policy.v1\0"
PORTFOLIO_TARGET_SCHEMA = "ea.phase1-portfolio-target.v1"
PORTFOLIO_TARGET_DIGEST_DOMAIN = b"ea.phase1-portfolio-target.v1\0"
PLANNING_OUTCOME_SCHEMA = "ea.phase1-planning-outcome.v1"
PLANNING_OUTCOME_DIGEST_DOMAIN = b"ea.phase1-planning-outcome.v1\0"
PORTFOLIO_PLANNING_RESULT_SCHEMA = "ea.phase1-planning-result.v1"
PORTFOLIO_PLANNING_RESULT_DIGEST_DOMAIN = b"ea.phase1-planning-result.v1\0"
PORTFOLIO_PLANNING_SUBMISSION_DIGEST_DOMAIN = b"ea.phase1-portfolio-planning-submission.v1\0"
PORTFOLIO_PLANNING_AUTHORITY_STATE_SCHEMA = "ea.phase1-portfolio-planning-authority-state.v1"
PORTFOLIO_PLANNING_AUTHORITY_STATE_DIGEST_DOMAIN = (
    b"ea.phase1-portfolio-planning-authority-state.v1\0"
)
PLANNING_CANONICALIZATION = "ea-canonical-json-v1"

_MAX_UINT64 = (1 << 64) - 1
_PORTFOLIO_POLICY_SEAL = object()
_PORTFOLIO_TARGET_SEAL = object()
_PLANNING_OUTCOME_SEAL = object()
_PLANNING_RESULT_SEAL = object()
_PLANNING_AUTHORITY_STATE_SEAL = object()
_POLICY_ID_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z", flags=re.ASCII)
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.CONFLICTING_ID,
    }
)


class PortfolioPlanningError(ValueError):
    """Closed public portfolio-planning failure."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("portfolio planning errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> PortfolioPlanningError:
    return PortfolioPlanningError(code, message)


@final
@dataclass(frozen=True, slots=True)
class PortfolioPolicyId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "portfolio policy ID must be exact str")
        if _POLICY_ID_PATTERN.fullmatch(self.value) is None:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "portfolio policy ID must match [a-z][a-z0-9._-]{0,127}",
            )


@final
@dataclass(frozen=True, slots=True)
class Phase1PortfolioPolicyEntry:
    instrument: Instrument
    target_quantity: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise _fail(OutcomeCode.INVALID_TYPE, "policy instrument must be exact")
        try:
            require_positive(self.target_quantity, field_name="target_quantity")
        except EconomicValidationError as error:
            _raise_economic(error)


@final
@dataclass(frozen=True, slots=True, init=False)
class Phase1PortfolioPolicy:
    policy_id: PortfolioPolicyId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    entries: tuple[Phase1PortfolioPolicyEntry, ...]
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("Phase1PortfolioPolicy values are created only by its factory")


@final
@dataclass(frozen=True, slots=True, init=False)
class PortfolioTarget:
    run_id: RunId
    target_id: EconomicId
    correlation_id: EconomicId
    causation_id: EconomicId
    signal_sha256: Sha256Digest
    instrument: Instrument
    target_position: CanonicalDecimal
    portfolio_policy_id: PortfolioPolicyId
    portfolio_policy_sha256: Sha256Digest
    portfolio_snapshot_version: int
    portfolio_snapshot_sha256: Sha256Digest
    causal_root_available_at: datetime
    dispatch_sequence: int
    instrument_specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("PortfolioTarget values are created only by PortfolioPlanningAuthority")


class PlanningOutcomeKind(StrEnum):
    INTENT_EMITTED = "intent_emitted"
    ALREADY_AT_TARGET = "already_at_target"
    BLOCKED_UNRESOLVED_FILLS = "blocked_unresolved_fills"


@final
@dataclass(frozen=True, slots=True, init=False)
class PlanningOutcome:
    run_id: RunId
    target_id: EconomicId
    target_sha256: Sha256Digest
    signal_id: EconomicId
    signal_sha256: Sha256Digest
    portfolio_policy_id: PortfolioPolicyId
    portfolio_policy_sha256: Sha256Digest
    portfolio_snapshot_version: int
    portfolio_snapshot_sha256: Sha256Digest
    target_next_before: int
    target_next_after: int | None
    intent_next_before: int | None
    intent_next_after: int | None
    kind: PlanningOutcomeKind
    intent_id: EconomicId | None
    intent_sha256: Sha256Digest | None
    delta: CanonicalDecimal
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("PlanningOutcome values are created only by PortfolioPlanningAuthority")


@final
@dataclass(frozen=True, slots=True, init=False)
class PortfolioPlanningResult:
    signal: StrategySignal
    target: PortfolioTarget
    portfolio_snapshot: PortfolioSnapshot
    outcome: PlanningOutcome
    intent: OrderIntent | None
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError(
            "PortfolioPlanningResult values are created only by PortfolioPlanningAuthority"
        )


@final
@dataclass(frozen=True, slots=True, init=False)
class PortfolioPlanningAuthorityState:
    run_id: RunId
    halted: bool
    target_next: int | None
    intent_next: int | None
    last_new_signal_dispatch_sequence: int | None
    result_count: int
    conflict: AuthorityConflictEvidence | None
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("PortfolioPlanningAuthorityState values are created only by an authority")


def _raise_economic(error: EconomicValidationError) -> None:
    if error.code is OutcomeCode.INVALID_TYPE:
        code = OutcomeCode.INVALID_TYPE
    elif error.code is OutcomeCode.NOT_QUANTIZED:
        code = OutcomeCode.NOT_QUANTIZED
    elif error.code is OutcomeCode.CONFLICTING_ID:
        code = OutcomeCode.CONFLICTING_ID
    else:
        code = OutcomeCode.OUT_OF_RANGE
    raise _fail(code, str(error)) from error


def _require_positive_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact int")
    if value < 1 or value > _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be positive uint64")
    return value


def _require_optional_positive_uint64(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_positive_uint64(value, field_name=field_name)


def _require_non_negative_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact int")
    if value < 0 or value > _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be uint64")
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
            "portfolio planning value cannot be canonically encoded",
        ) from error


def _parse_canonical(payload: bytes, *, field_name: str) -> object:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} canonical bytes must be exact")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} canonical JSON is invalid") from error


def _digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    if type(identity) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "economic ID must be exact")
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _optional_id_document(identity: EconomicId | None) -> dict[str, object] | None:
    return None if identity is None else _economic_id_document(identity)


def _instrument_document(instrument: Instrument) -> dict[str, object]:
    if type(instrument) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument must be exact")
    return {"symbol": instrument.symbol, "venue": instrument.venue.code}


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, "causal time must be exact datetime")
    try:
        return require_utc(value, field="causal_root_available_at").strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "causal time must be canonical UTC") from error


def create_phase1_portfolio_policy(
    *,
    policy_id: PortfolioPolicyId,
    entries: tuple[Phase1PortfolioPolicyEntry, ...],
    spec_set: InstrumentExecutionSpecSet,
) -> Phase1PortfolioPolicy:
    if type(policy_id) is not PortfolioPolicyId:
        raise _fail(OutcomeCode.INVALID_TYPE, "policy_id must be exact")
    if type(entries) is not tuple or not entries:
        raise _fail(OutcomeCode.INVALID_TYPE, "policy entries must be one non-empty exact tuple")
    if any(type(entry) is not Phase1PortfolioPolicyEntry for entry in entries):
        raise _fail(OutcomeCode.INVALID_TYPE, "policy entries must contain exact values")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    keys = tuple(entry.instrument.key for entry in entries)
    if keys != tuple(sorted(keys)):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "policy entries must be in canonical order")
    if len(keys) != len(set(keys)):
        raise _fail(OutcomeCode.CONFLICTING_ID, "policy instruments must be unique")
    for entry in entries:
        try:
            specification = spec_set.require(entry.instrument)
            require_quantized(
                entry.target_quantity,
                specification.quantity_quantum,
                field_name="target_quantity",
            )
        except EconomicValidationError as error:
            _raise_economic(error)
    value = object.__new__(Phase1PortfolioPolicy)
    object.__setattr__(value, "policy_id", policy_id)
    object.__setattr__(value, "instrument_spec_set_id", spec_set.identifier)
    object.__setattr__(value, "instrument_spec_set_sha256", instrument_spec_set_digest(spec_set))
    object.__setattr__(value, "entries", entries)
    object.__setattr__(value, "_seal", _PORTFOLIO_POLICY_SEAL)
    canonical_phase1_portfolio_policy_bytes(value)
    return value


def _policy_document(policy: Phase1PortfolioPolicy) -> dict[str, object]:
    if type(policy) is not Phase1PortfolioPolicy:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio policy must be exact")
    if (
        type(policy.policy_id) is not PortfolioPolicyId
        or type(policy.instrument_spec_set_id) is not InstrumentSpecSetId
        or type(policy.instrument_spec_set_sha256) is not Sha256Digest
        or type(policy.entries) is not tuple
        or not policy.entries
        or any(type(entry) is not Phase1PortfolioPolicyEntry for entry in policy.entries)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio policy carriers are invalid")
    if policy._seal is not _PORTFOLIO_POLICY_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio policy is not factory-issued")
    for entry in policy.entries:
        try:
            require_positive(entry.target_quantity, field_name="target_quantity")
        except EconomicValidationError as error:
            _raise_economic(error)
    keys = tuple(entry.instrument.key for entry in policy.entries)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise _fail(OutcomeCode.CONFLICTING_ID, "portfolio policy entry order conflicts")
    return {
        "canonicalization": PLANNING_CANONICALIZATION,
        "entries": [
            {
                "instrument": _instrument_document(entry.instrument),
                "target_quantity": entry.target_quantity.text,
            }
            for entry in policy.entries
        ],
        "instrument_spec_set_id": policy.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": policy.instrument_spec_set_sha256.value,
        "policy_id": policy.policy_id.value,
        "schema": PORTFOLIO_POLICY_SCHEMA,
    }


def canonical_phase1_portfolio_policy_bytes(policy: Phase1PortfolioPolicy) -> bytes:
    try:
        return _encode_json(_policy_document(policy))
    except PortfolioPlanningError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "portfolio policy cannot be canonically encoded",
        ) from error


def phase1_portfolio_policy_digest(policy: Phase1PortfolioPolicy) -> Sha256Digest:
    return _digest(PORTFOLIO_POLICY_DIGEST_DOMAIN, canonical_phase1_portfolio_policy_bytes(policy))


def _create_portfolio_target(
    *,
    run_id: RunId,
    target_id: EconomicId,
    signal: StrategySignal,
    signal_sha256: Sha256Digest,
    instrument: Instrument,
    target_position: CanonicalDecimal,
    policy: Phase1PortfolioPolicy,
    policy_sha256: Sha256Digest,
    portfolio_snapshot_version: int,
    portfolio_snapshot_sha256: Sha256Digest,
    instrument_specification_id: InstrumentSpecId,
) -> PortfolioTarget:
    value = object.__new__(PortfolioTarget)
    for name, item in (
        ("run_id", run_id),
        ("target_id", target_id),
        ("correlation_id", signal.signal_id),
        ("causation_id", signal.signal_id),
        ("signal_sha256", signal_sha256),
        ("instrument", instrument),
        ("target_position", target_position),
        ("portfolio_policy_id", policy.policy_id),
        ("portfolio_policy_sha256", policy_sha256),
        ("portfolio_snapshot_version", portfolio_snapshot_version),
        ("portfolio_snapshot_sha256", portfolio_snapshot_sha256),
        ("causal_root_available_at", signal.causal_root_available_at),
        ("dispatch_sequence", signal.dispatch_sequence),
        ("instrument_specification_id", instrument_specification_id),
        ("instrument_spec_set_id", policy.instrument_spec_set_id),
        ("instrument_spec_set_sha256", policy.instrument_spec_set_sha256),
    ):
        object.__setattr__(value, name, item)
    object.__setattr__(value, "_seal", _PORTFOLIO_TARGET_SEAL)
    canonical_portfolio_target_bytes(value)
    return value


def _validate_target(target: PortfolioTarget) -> None:
    if type(target) is not PortfolioTarget:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio target must be exact")
    if type(target.run_id) is not RunId or type(target.target_id) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "target identity carriers must be exact")
    if target._seal is not _PORTFOLIO_TARGET_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio target is not factory-issued")
    if (
        target.target_id.owner_kind is not EconomicOwnerKind.PORTFOLIO_TARGET
        or target.target_id.run_id != target.run_id
        or target.target_id.owner_sequence < 1
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "target identity lineage conflicts")
    if (
        type(target.correlation_id) is not EconomicId
        or type(target.causation_id) is not EconomicId
        or target.correlation_id != target.causation_id
        or target.correlation_id.owner_kind is not EconomicOwnerKind.STRATEGY_SIGNAL
        or target.correlation_id.run_id != target.run_id
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "target signal lineage conflicts")
    if any(
        type(value) is not Sha256Digest
        for value in (
            target.signal_sha256,
            target.portfolio_policy_sha256,
            target.portfolio_snapshot_sha256,
            target.instrument_spec_set_sha256,
        )
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "target digests must be exact")
    if (
        type(target.instrument) is not Instrument
        or type(target.target_position) is not CanonicalDecimal
        or type(target.portfolio_policy_id) is not PortfolioPolicyId
        or type(target.instrument_specification_id) is not InstrumentSpecId
        or type(target.instrument_spec_set_id) is not InstrumentSpecSetId
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "target domain carriers must be exact")
    _require_non_negative_uint64(
        target.portfolio_snapshot_version,
        field_name="portfolio_snapshot_version",
    )
    _require_positive_uint64(target.dispatch_sequence, field_name="dispatch_sequence")
    _utc_text(target.causal_root_available_at)


def _target_document(target: PortfolioTarget) -> dict[str, object]:
    _validate_target(target)
    return {
        "canonicalization": PLANNING_CANONICALIZATION,
        "causal_root_available_at": _utc_text(target.causal_root_available_at),
        "causation_id": _economic_id_document(target.causation_id),
        "correlation_id": _economic_id_document(target.correlation_id),
        "dispatch_sequence": target.dispatch_sequence,
        "instrument": _instrument_document(target.instrument),
        "instrument_spec_set_id": target.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": target.instrument_spec_set_sha256.value,
        "instrument_specification_id": target.instrument_specification_id.value,
        "portfolio_policy_id": target.portfolio_policy_id.value,
        "portfolio_policy_sha256": target.portfolio_policy_sha256.value,
        "portfolio_snapshot_sha256": target.portfolio_snapshot_sha256.value,
        "portfolio_snapshot_version": target.portfolio_snapshot_version,
        "run_id": target.run_id.value,
        "schema": PORTFOLIO_TARGET_SCHEMA,
        "signal_sha256": target.signal_sha256.value,
        "target_id": _economic_id_document(target.target_id),
        "target_position": target.target_position.text,
    }


def canonical_portfolio_target_bytes(target: PortfolioTarget) -> bytes:
    try:
        return _encode_json(_target_document(target))
    except PortfolioPlanningError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "portfolio target cannot be canonically encoded",
        ) from error


def portfolio_target_digest(target: PortfolioTarget) -> Sha256Digest:
    return _digest(PORTFOLIO_TARGET_DIGEST_DOMAIN, canonical_portfolio_target_bytes(target))


def _create_planning_outcome(
    *,
    run_id: RunId,
    target: PortfolioTarget,
    target_sha256: Sha256Digest,
    signal: StrategySignal,
    policy: Phase1PortfolioPolicy,
    policy_sha256: Sha256Digest,
    snapshot_sha256: Sha256Digest,
    target_next_before: int,
    target_next_after: int | None,
    intent_next_before: int | None,
    intent_next_after: int | None,
    kind: PlanningOutcomeKind,
    intent: OrderIntent | None,
    intent_sha256: Sha256Digest | None,
    delta: CanonicalDecimal,
) -> PlanningOutcome:
    value = object.__new__(PlanningOutcome)
    fields = {
        "run_id": run_id,
        "target_id": target.target_id,
        "target_sha256": target_sha256,
        "signal_id": signal.signal_id,
        "signal_sha256": strategy_signal_digest(signal),
        "portfolio_policy_id": policy.policy_id,
        "portfolio_policy_sha256": policy_sha256,
        "portfolio_snapshot_version": target.portfolio_snapshot_version,
        "portfolio_snapshot_sha256": snapshot_sha256,
        "target_next_before": target_next_before,
        "target_next_after": target_next_after,
        "intent_next_before": intent_next_before,
        "intent_next_after": intent_next_after,
        "kind": kind,
        "intent_id": None if intent is None else intent.intent_id,
        "intent_sha256": intent_sha256,
        "delta": delta,
    }
    for name, item in fields.items():
        object.__setattr__(value, name, item)
    object.__setattr__(value, "_seal", _PLANNING_OUTCOME_SEAL)
    canonical_planning_outcome_bytes(value)
    return value


def _validate_outcome(outcome: PlanningOutcome) -> None:
    if type(outcome) is not PlanningOutcome:
        raise _fail(OutcomeCode.INVALID_TYPE, "planning outcome must be exact")
    if (
        type(outcome.run_id) is not RunId
        or type(outcome.target_id) is not EconomicId
        or type(outcome.signal_id) is not EconomicId
        or type(outcome.portfolio_policy_id) is not PortfolioPolicyId
        or type(outcome.kind) is not PlanningOutcomeKind
        or type(outcome.delta) is not CanonicalDecimal
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "planning outcome carriers must be exact")
    if outcome._seal is not _PLANNING_OUTCOME_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "planning outcome is not factory-issued")
    if (
        outcome.target_id.owner_kind is not EconomicOwnerKind.PORTFOLIO_TARGET
        or outcome.signal_id.owner_kind is not EconomicOwnerKind.STRATEGY_SIGNAL
        or outcome.target_id.run_id != outcome.run_id
        or outcome.signal_id.run_id != outcome.run_id
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "planning outcome identity lineage conflicts")
    if any(
        type(value) is not Sha256Digest
        for value in (
            outcome.target_sha256,
            outcome.signal_sha256,
            outcome.portfolio_policy_sha256,
            outcome.portfolio_snapshot_sha256,
        )
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "planning outcome digests must be exact")
    _require_non_negative_uint64(
        outcome.portfolio_snapshot_version,
        field_name="portfolio_snapshot_version",
    )
    _require_positive_uint64(outcome.target_next_before, field_name="target_next_before")
    _require_optional_positive_uint64(outcome.target_next_after, field_name="target_next_after")
    _require_optional_positive_uint64(outcome.intent_next_before, field_name="intent_next_before")
    _require_optional_positive_uint64(outcome.intent_next_after, field_name="intent_next_after")
    has_intent = outcome.intent_id is not None or outcome.intent_sha256 is not None
    if has_intent and (
        type(outcome.intent_id) is not EconomicId
        or outcome.intent_id.owner_kind is not EconomicOwnerKind.PORTFOLIO_INTENT
        or outcome.intent_id.run_id != outcome.run_id
        or type(outcome.intent_sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "planning intent evidence conflicts")
    if (outcome.intent_id is None) is not (outcome.intent_sha256 is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "planning intent fields must coexist")
    if outcome.kind is PlanningOutcomeKind.INTENT_EMITTED:
        if not has_intent or outcome.delta.coefficient == 0:
            raise _fail(OutcomeCode.CONFLICTING_ID, "emitted outcome coupling conflicts")
        expected_intent_after = (
            None
            if outcome.intent_next_before == _MAX_UINT64
            else (None if outcome.intent_next_before is None else outcome.intent_next_before + 1)
        )
        if outcome.intent_next_before is None or outcome.intent_next_after != expected_intent_after:
            raise _fail(OutcomeCode.CONFLICTING_ID, "intent sequence transition conflicts")
    elif has_intent:
        raise _fail(OutcomeCode.CONFLICTING_ID, "non-emitted outcome cannot carry intent")
    elif outcome.intent_next_after != outcome.intent_next_before:
        raise _fail(OutcomeCode.CONFLICTING_ID, "no-intent sequence must remain unchanged")
    if outcome.kind is PlanningOutcomeKind.ALREADY_AT_TARGET and outcome.delta.coefficient != 0:
        raise _fail(OutcomeCode.CONFLICTING_ID, "already-at-target delta must be zero")
    expected_target_after = (
        None if outcome.target_next_before == _MAX_UINT64 else outcome.target_next_before + 1
    )
    if outcome.target_next_after != expected_target_after:
        raise _fail(OutcomeCode.CONFLICTING_ID, "target sequence transition conflicts")


def _outcome_document(outcome: PlanningOutcome) -> dict[str, object]:
    _validate_outcome(outcome)
    return {
        "canonicalization": PLANNING_CANONICALIZATION,
        "delta": outcome.delta.text,
        "intent_id": _optional_id_document(outcome.intent_id),
        "intent_next_after": outcome.intent_next_after,
        "intent_next_before": outcome.intent_next_before,
        "intent_sha256": None if outcome.intent_sha256 is None else outcome.intent_sha256.value,
        "kind": outcome.kind.value,
        "portfolio_policy_id": outcome.portfolio_policy_id.value,
        "portfolio_policy_sha256": outcome.portfolio_policy_sha256.value,
        "portfolio_snapshot_sha256": outcome.portfolio_snapshot_sha256.value,
        "portfolio_snapshot_version": outcome.portfolio_snapshot_version,
        "run_id": outcome.run_id.value,
        "schema": PLANNING_OUTCOME_SCHEMA,
        "signal_id": _economic_id_document(outcome.signal_id),
        "signal_sha256": outcome.signal_sha256.value,
        "target_id": _economic_id_document(outcome.target_id),
        "target_next_after": outcome.target_next_after,
        "target_next_before": outcome.target_next_before,
        "target_sha256": outcome.target_sha256.value,
    }


def canonical_planning_outcome_bytes(outcome: PlanningOutcome) -> bytes:
    try:
        return _encode_json(_outcome_document(outcome))
    except PortfolioPlanningError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "planning outcome cannot be canonically encoded",
        ) from error


def planning_outcome_digest(outcome: PlanningOutcome) -> Sha256Digest:
    return _digest(PLANNING_OUTCOME_DIGEST_DOMAIN, canonical_planning_outcome_bytes(outcome))


def _create_portfolio_planning_result(
    *,
    signal: StrategySignal,
    target: PortfolioTarget,
    portfolio_snapshot: PortfolioSnapshot,
    outcome: PlanningOutcome,
    intent: OrderIntent | None,
) -> PortfolioPlanningResult:
    value = object.__new__(PortfolioPlanningResult)
    object.__setattr__(value, "signal", signal)
    object.__setattr__(value, "target", target)
    object.__setattr__(value, "portfolio_snapshot", portfolio_snapshot)
    object.__setattr__(value, "outcome", outcome)
    object.__setattr__(value, "intent", intent)
    object.__setattr__(value, "_seal", _PLANNING_RESULT_SEAL)
    canonical_portfolio_planning_result_bytes(value)
    return value


def _result_document(result: PortfolioPlanningResult) -> dict[str, object]:
    if type(result) is not PortfolioPlanningResult:
        raise _fail(OutcomeCode.INVALID_TYPE, "planning result must be exact")
    if (
        type(result.signal) is not StrategySignal
        or type(result.target) is not PortfolioTarget
        or type(result.portfolio_snapshot) is not PortfolioSnapshot
        or type(result.outcome) is not PlanningOutcome
        or (result.intent is not None and type(result.intent) is not OrderIntent)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "planning result carriers must be exact")
    if result._seal is not _PLANNING_RESULT_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "planning result is not factory-issued")
    signal_bytes = canonical_strategy_signal_bytes(result.signal)
    target_bytes = canonical_portfolio_target_bytes(result.target)
    snapshot_bytes = canonical_portfolio_snapshot_bytes(result.portfolio_snapshot)
    outcome_bytes = canonical_planning_outcome_bytes(result.outcome)
    current_position = CanonicalDecimal("0")
    for balance in result.portfolio_snapshot.position_balances:
        if balance.instrument == result.signal.instrument:
            current_position = balance.quantity
            break
    delta_scale = max(
        result.target.target_position.scale,
        current_position.scale,
        result.outcome.delta.scale,
    )
    expected_delta = result.target.target_position.coefficient * (
        10 ** (delta_scale - result.target.target_position.scale)
    ) - current_position.coefficient * (10 ** (delta_scale - current_position.scale))
    actual_delta = result.outcome.delta.coefficient * (
        10 ** (delta_scale - result.outcome.delta.scale)
    )
    if (
        result.target.run_id != result.signal.run_id
        or result.outcome.run_id != result.signal.run_id
        or result.portfolio_snapshot.run_id != result.signal.run_id
        or result.target.correlation_id != result.signal.signal_id
        or result.target.causation_id != result.signal.signal_id
        or result.target.signal_sha256 != strategy_signal_digest(result.signal)
        or result.target.instrument != result.signal.instrument
        or result.target.causal_root_available_at != result.signal.causal_root_available_at
        or result.target.dispatch_sequence != result.signal.dispatch_sequence
        or result.outcome.signal_id != result.signal.signal_id
        or result.outcome.signal_sha256 != strategy_signal_digest(result.signal)
        or result.outcome.target_id != result.target.target_id
        or result.outcome.target_sha256 != portfolio_target_digest(result.target)
        or actual_delta != expected_delta
        or result.target.portfolio_snapshot_version != result.portfolio_snapshot.snapshot_version
        or result.target.portfolio_snapshot_sha256
        != portfolio_snapshot_digest(result.portfolio_snapshot)
        or result.target.instrument_spec_set_id != result.portfolio_snapshot.instrument_spec_set_id
        or result.target.instrument_spec_set_sha256
        != result.portfolio_snapshot.instrument_spec_set_sha256
        or result.outcome.portfolio_snapshot_version != result.portfolio_snapshot.snapshot_version
        or result.outcome.portfolio_snapshot_sha256
        != portfolio_snapshot_digest(result.portfolio_snapshot)
        or result.outcome.portfolio_policy_id != result.target.portfolio_policy_id
        or result.outcome.portfolio_policy_sha256 != result.target.portfolio_policy_sha256
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "planning result lineage conflicts")
    if result.portfolio_snapshot.unresolved_fills:
        expected_kind = PlanningOutcomeKind.BLOCKED_UNRESOLVED_FILLS
    elif result.outcome.delta.coefficient == 0:
        expected_kind = PlanningOutcomeKind.ALREADY_AT_TARGET
    else:
        expected_kind = PlanningOutcomeKind.INTENT_EMITTED
    if result.outcome.kind is not expected_kind:
        raise _fail(OutcomeCode.CONFLICTING_ID, "planning result outcome classification conflicts")
    if result.intent is None:
        if result.outcome.kind is PlanningOutcomeKind.INTENT_EMITTED:
            raise _fail(OutcomeCode.CONFLICTING_ID, "emitted result requires intent")
        intent_document = None
    else:
        if (
            result.outcome.kind is not PlanningOutcomeKind.INTENT_EMITTED
            or result.outcome.intent_id != result.intent.intent_id
            or result.outcome.intent_sha256 != order_intent_digest(result.intent)
            or result.intent.run_id != result.signal.run_id
            or result.intent.correlation_id != result.signal.signal_id
            or result.intent.causation_id != result.target.target_id
            or result.intent.instrument != result.target.instrument
            or result.intent.causal_root_available_at != result.signal.causal_root_available_at
            or result.intent.dispatch_sequence != result.signal.dispatch_sequence
            or result.intent.target_lineage.target_id != result.target.target_id
            or result.intent.target_lineage.target_sha256 != portfolio_target_digest(result.target)
            or result.intent.portfolio_snapshot_version
            != result.portfolio_snapshot.snapshot_version
            or result.intent.instrument_specification_id
            != result.target.instrument_specification_id
            or result.intent.instrument_spec_set_id != result.target.instrument_spec_set_id
            or result.intent.instrument_spec_set_sha256 != result.target.instrument_spec_set_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "planning result intent coupling conflicts")
        intent_document = _parse_canonical(
            canonical_order_intent_bytes(result.intent),
            field_name="intent",
        )
    return {
        "canonicalization": PLANNING_CANONICALIZATION,
        "intent": intent_document,
        "outcome": _parse_canonical(outcome_bytes, field_name="outcome"),
        "portfolio_snapshot": _parse_canonical(snapshot_bytes, field_name="portfolio_snapshot"),
        "schema": PORTFOLIO_PLANNING_RESULT_SCHEMA,
        "signal": _parse_canonical(signal_bytes, field_name="signal"),
        "target": _parse_canonical(target_bytes, field_name="target"),
    }


def canonical_portfolio_planning_result_bytes(result: PortfolioPlanningResult) -> bytes:
    try:
        return _encode_json(_result_document(result))
    except PortfolioPlanningError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "planning result cannot be canonically encoded",
        ) from error


def portfolio_planning_result_digest(result: PortfolioPlanningResult) -> Sha256Digest:
    return _digest(
        PORTFOLIO_PLANNING_RESULT_DIGEST_DOMAIN,
        canonical_portfolio_planning_result_bytes(result),
    )


def validate_portfolio_planning_risk_handoff(
    result: PortfolioPlanningResult,
    *,
    intent: OrderIntent,
    portfolio_snapshot: PortfolioSnapshot,
) -> None:
    """Require the exact retained emitted-intent/snapshot pair before a Risk call."""
    if type(result) is not PortfolioPlanningResult:
        raise _fail(OutcomeCode.INVALID_TYPE, "planning result must be exact")
    if type(intent) is not OrderIntent or type(portfolio_snapshot) is not PortfolioSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "risk handoff carriers must be exact")
    canonical_portfolio_planning_result_bytes(result)
    if result.intent is not intent or result.portfolio_snapshot is not portfolio_snapshot:
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk handoff object identity conflicts")
    if (
        result.outcome.kind is not PlanningOutcomeKind.INTENT_EMITTED
        or result.outcome.intent_id != intent.intent_id
        or result.outcome.intent_sha256 != order_intent_digest(intent)
        or result.outcome.portfolio_snapshot_version != portfolio_snapshot.snapshot_version
        or result.outcome.portfolio_snapshot_sha256 != portfolio_snapshot_digest(portfolio_snapshot)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk handoff canonical evidence conflicts")


def portfolio_planning_submission_digest(signal: StrategySignal) -> Sha256Digest:
    try:
        signal_bytes = canonical_strategy_signal_bytes(signal)
    except StrategyContractError as error:
        code = (
            error.code
            if error.code in {OutcomeCode.INVALID_TYPE, OutcomeCode.CONFLICTING_ID}
            else OutcomeCode.OUT_OF_RANGE
        )
        raise _fail(code, str(error)) from error
    return _digest(
        PORTFOLIO_PLANNING_SUBMISSION_DIGEST_DOMAIN,
        len(signal_bytes).to_bytes(8, "big") + signal_bytes,
    )


def _create_portfolio_planning_authority_state(
    *,
    run_id: RunId,
    halted: bool,
    target_next: int | None,
    intent_next: int | None,
    last_new_signal_dispatch_sequence: int | None,
    result_count: int,
    conflict: AuthorityConflictEvidence | None,
) -> PortfolioPlanningAuthorityState:
    value = object.__new__(PortfolioPlanningAuthorityState)
    for name, item in (
        ("run_id", run_id),
        ("halted", halted),
        ("target_next", target_next),
        ("intent_next", intent_next),
        ("last_new_signal_dispatch_sequence", last_new_signal_dispatch_sequence),
        ("result_count", result_count),
        ("conflict", conflict),
    ):
        object.__setattr__(value, name, item)
    object.__setattr__(value, "_seal", _PLANNING_AUTHORITY_STATE_SEAL)
    canonical_portfolio_planning_authority_state_bytes(value)
    return value


def _planning_state_document(value: PortfolioPlanningAuthorityState) -> dict[str, object]:
    if type(value) is not PortfolioPlanningAuthorityState:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio planning state must be exact")
    if type(value.run_id) is not RunId or type(value.halted) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio planning state carriers must be exact")
    if value._seal is not _PLANNING_AUTHORITY_STATE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio planning state is not factory-issued")
    _require_optional_positive_uint64(value.target_next, field_name="target_next")
    _require_optional_positive_uint64(value.intent_next, field_name="intent_next")
    _require_optional_positive_uint64(
        value.last_new_signal_dispatch_sequence,
        field_name="last_new_signal_dispatch_sequence",
    )
    _require_non_negative_uint64(value.result_count, field_name="result_count")
    if value.conflict is not None:
        _validate_conflict(value.conflict)
        if (
            value.conflict.authority_kind is not AuthorityKind.PORTFOLIO_PLANNING
            or value.conflict.run_id != value.run_id
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "portfolio conflict state lineage conflicts")
    if value.halted is (value.conflict is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "halt and conflict evidence must agree")
    return {
        "canonicalization": PLANNING_CANONICALIZATION,
        "conflict": None if value.conflict is None else _conflict_document(value.conflict),
        "halted": value.halted,
        "intent_next": value.intent_next,
        "last_new_signal_dispatch_sequence": value.last_new_signal_dispatch_sequence,
        "result_count": value.result_count,
        "run_id": value.run_id.value,
        "schema": PORTFOLIO_PLANNING_AUTHORITY_STATE_SCHEMA,
        "target_next": value.target_next,
    }


def canonical_portfolio_planning_authority_state_bytes(
    value: PortfolioPlanningAuthorityState,
) -> bytes:
    try:
        return _encode_json(_planning_state_document(value))
    except PortfolioPlanningError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "portfolio planning state cannot be canonically encoded",
        ) from error


def portfolio_planning_authority_state_digest(
    value: PortfolioPlanningAuthorityState,
) -> Sha256Digest:
    return _digest(
        PORTFOLIO_PLANNING_AUTHORITY_STATE_DIGEST_DOMAIN,
        canonical_portfolio_planning_authority_state_bytes(value),
    )
