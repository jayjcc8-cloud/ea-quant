"""Canonical pre-trade risk values from Accepted ADR 0011."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
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
    InstrumentSpecSetId,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    ExecutionPolicyRef,
    RiskDecision,
    RiskDecisionKind,
    execution_approval_digest,
    risk_decision_digest,
)
from ea.core.identity import Instrument
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

RISK_POLICY_SCHEMA_VERSION = 1
RISK_POLICY_CANONICALIZATION = "ea-risk-policy-v1"
RISK_POLICY_DIGEST_DOMAIN = b"ea.risk-policy.v1\0"

RISK_STATE_SCHEMA_VERSION = 1
RISK_STATE_CANONICALIZATION = "ea-risk-state-v1"
RISK_STATE_DIGEST_DOMAIN = b"ea.risk-state.v1\0"

RISK_EVALUATION_EVIDENCE_SCHEMA_VERSION = 1
RISK_EVALUATION_EVIDENCE_CANONICALIZATION = "ea-risk-evaluation-evidence-v1"
RISK_EVALUATION_EVIDENCE_DIGEST_DOMAIN = b"ea.risk-evaluation-evidence.v1\0"

_MAX_UINT64 = (1 << 64) - 1
_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z", flags=re.ASCII)
_RISK_CONTRACT_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.CONFLICTING_ID,
    }
)


class RiskContractError(ValueError):
    """Structured validation error for dependency-neutral risk values."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _RISK_CONTRACT_ERROR_CODES:
            raise TypeError("risk contract errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> RiskContractError:
    return RiskContractError(code, message)


class RiskReasonCode(StrEnum):
    """Closed supplemental risk-authority reason vocabulary."""

    WITHIN_LIMITS = "within_limits"
    RESIZED_ORDER_LIMIT = "resized_order_limit"
    RESIZED_POSITION_LIMIT = "resized_position_limit"
    RESIZED_ORDER_AND_POSITION_LIMITS = "resized_order_and_position_limits"
    HALTED = "halted"
    INSTRUMENT_NOT_CONFIGURED = "instrument_not_configured"
    NO_POSITION_CAPACITY = "no_position_capacity"
    STALE_PORTFOLIO_SNAPSHOT = "stale_portfolio_snapshot"
    LINEAGE_MISMATCH = "lineage_mismatch"
    ARITHMETIC_FAILURE = "arithmetic_failure"
    APPROVAL_SEQUENCE_EXHAUSTED = "approval_sequence_exhausted"


class RiskHaltReason(StrEnum):
    """Closed monotone halt reasons."""

    KILL_SWITCH = "kill_switch"
    INTENT_IDENTITY_CONFLICT = "intent_identity_conflict"
    RISK_EVALUATION_FAILURE = "risk_evaluation_failure"
    EXTERNAL_SAFETY_HALT = "external_safety_halt"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@final
@dataclass(frozen=True, slots=True)
class RiskPolicyId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "risk policy ID must have exact runtime type str",
            )
        if _TOKEN_PATTERN.fullmatch(self.value) is None:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "risk policy ID must match [a-z][a-z0-9._-]{0,127}",
            )


@final
@dataclass(frozen=True, slots=True)
class InstrumentRiskLimit:
    instrument: Instrument
    maximum_order_quantity: CanonicalDecimal
    maximum_absolute_position: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise _fail(OutcomeCode.INVALID_TYPE, "risk limit instrument must be exact")
        try:
            require_positive(
                self.maximum_order_quantity,
                field_name="maximum_order_quantity",
            )
            require_positive(
                self.maximum_absolute_position,
                field_name="maximum_absolute_position",
            )
        except EconomicValidationError as error:
            raise _translate_economic_error(error) from error


@final
@dataclass(frozen=True, slots=True, init=False)
class Phase1RiskPolicy:
    policy_id: RiskPolicyId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    execution_policy: ExecutionPolicyRef
    instrument_limits: tuple[InstrumentRiskLimit, ...]

    def __init__(self) -> None:
        raise TypeError("Phase1RiskPolicy values are created only by create_phase1_risk_policy")


def create_phase1_risk_policy(
    *,
    policy_id: RiskPolicyId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    instrument_limits: Iterable[InstrumentRiskLimit],
) -> Phase1RiskPolicy:
    """Validate, sort, and freeze one Phase 1 deny-by-default risk policy."""
    if type(policy_id) is not RiskPolicyId:
        raise _fail(OutcomeCode.INVALID_TYPE, "policy_id must be an exact RiskPolicyId")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    if type(execution_policy) is not ExecutionPolicyRef:
        raise _fail(OutcomeCode.INVALID_TYPE, "execution_policy must be exact")
    try:
        materialized = tuple(instrument_limits)
    except TypeError as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "instrument_limits must be a finite iterable",
        ) from error
    if any(type(limit) is not InstrumentRiskLimit for limit in materialized):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "instrument_limits must contain exact InstrumentRiskLimit values",
        )
    keys = tuple(limit.instrument.key for limit in materialized)
    if len(keys) != len(set(keys)):
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk policy contains a duplicate instrument")
    ordered = tuple(sorted(materialized, key=lambda limit: limit.instrument.key))
    for limit in ordered:
        try:
            specification = spec_set.require(limit.instrument)
            require_quantized(
                limit.maximum_order_quantity,
                specification.quantity_quantum,
                field_name="maximum_order_quantity",
            )
            require_quantized(
                limit.maximum_absolute_position,
                specification.quantity_quantum,
                field_name="maximum_absolute_position",
            )
        except EconomicValidationError as error:
            raise _translate_economic_error(error) from error

    value = object.__new__(Phase1RiskPolicy)
    object.__setattr__(value, "policy_id", policy_id)
    object.__setattr__(value, "instrument_spec_set_id", spec_set.identifier)
    object.__setattr__(value, "instrument_spec_set_sha256", instrument_spec_set_digest(spec_set))
    object.__setattr__(value, "execution_policy", execution_policy)
    object.__setattr__(value, "instrument_limits", ordered)
    canonical_phase1_risk_policy_bytes(value)
    phase1_risk_policy_digest(value)
    return value


@final
@dataclass(frozen=True, slots=True, init=False)
class RiskStateSnapshot:
    run_id: RunId
    policy_id: RiskPolicyId
    policy_sha256: Sha256Digest
    risk_state_version: int
    halted: bool
    halt_reason: RiskHaltReason | None
    halt_causal_root_available_at: datetime | None
    halt_dispatch_sequence: int | None
    conflict_existing_intent_sha256: Sha256Digest | None
    conflict_submitted_intent_sha256: Sha256Digest | None

    def __init__(self) -> None:
        raise TypeError("RiskStateSnapshot values are created only by the risk authority")


def _create_risk_state_snapshot(
    *,
    run_id: RunId,
    policy_id: RiskPolicyId,
    policy_sha256: Sha256Digest,
    risk_state_version: int,
    halted: bool,
    halt_reason: RiskHaltReason | None,
    halt_causal_root_available_at: datetime | None,
    halt_dispatch_sequence: int | None,
    conflict_existing_intent_sha256: Sha256Digest | None,
    conflict_submitted_intent_sha256: Sha256Digest | None,
) -> RiskStateSnapshot:
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "risk state run_id must be exact")
    if type(policy_id) is not RiskPolicyId or type(policy_sha256) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, "risk state policy binding must be exact")
    if type(risk_state_version) is not int or risk_state_version not in (0, 1):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "Phase 1 risk state version must be zero or one")
    if type(halted) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "halted must be an exact bool")

    causal_time: datetime | None = None
    if halted:
        if type(halt_reason) is not RiskHaltReason:
            raise _fail(OutcomeCode.INVALID_TYPE, "halted state requires an exact halt reason")
        if type(halt_causal_root_available_at) is not datetime:
            raise _fail(OutcomeCode.INVALID_TYPE, "halted state requires an exact causal time")
        try:
            causal_time = require_utc(
                halt_causal_root_available_at,
                field="halt_causal_root_available_at",
            )
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        _require_non_negative(
            halt_dispatch_sequence,
            field_name="halt_dispatch_sequence",
        )
        if risk_state_version != 1:
            raise _fail(OutcomeCode.CONFLICTING_ID, "halted state must have version one")
        is_conflict = halt_reason is RiskHaltReason.INTENT_IDENTITY_CONFLICT
        if is_conflict and (
            type(conflict_existing_intent_sha256) is not Sha256Digest
            or type(conflict_submitted_intent_sha256) is not Sha256Digest
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "identity-conflict halt requires both intent digests",
            )
        if not is_conflict and (
            conflict_existing_intent_sha256 is not None
            or conflict_submitted_intent_sha256 is not None
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "public halt cannot carry intent-conflict digests",
            )
    else:
        if any(
            item is not None
            for item in (
                halt_reason,
                halt_causal_root_available_at,
                halt_dispatch_sequence,
                conflict_existing_intent_sha256,
                conflict_submitted_intent_sha256,
            )
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "initial risk state cannot carry halt fields")
        if risk_state_version != 0:
            raise _fail(OutcomeCode.CONFLICTING_ID, "initial risk state must have version zero")

    value = object.__new__(RiskStateSnapshot)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "policy_id", policy_id)
    object.__setattr__(value, "policy_sha256", policy_sha256)
    object.__setattr__(value, "risk_state_version", risk_state_version)
    object.__setattr__(value, "halted", halted)
    object.__setattr__(value, "halt_reason", halt_reason)
    object.__setattr__(value, "halt_causal_root_available_at", causal_time)
    object.__setattr__(value, "halt_dispatch_sequence", halt_dispatch_sequence)
    object.__setattr__(
        value,
        "conflict_existing_intent_sha256",
        conflict_existing_intent_sha256,
    )
    object.__setattr__(
        value,
        "conflict_submitted_intent_sha256",
        conflict_submitted_intent_sha256,
    )
    return value


@final
@dataclass(frozen=True, slots=True, init=False)
class RiskEvaluationEvidence:
    run_id: RunId
    intent_id: EconomicId
    intent_sha256: Sha256Digest
    portfolio_snapshot_version: int
    portfolio_snapshot_sha256: Sha256Digest
    policy_id: RiskPolicyId
    policy_sha256: Sha256Digest
    risk_state_version: int
    decision_id: EconomicId
    decision_sha256: Sha256Digest
    approval_sha256: Sha256Digest | None
    reason_code: RiskReasonCode
    decision_next_before: int
    decision_next_after: int | None
    approval_next_before: int | None
    approval_next_after: int | None

    def __init__(self) -> None:
        raise TypeError("RiskEvaluationEvidence values are created only by the risk authority")


def _create_risk_evaluation_evidence(
    *,
    decision: RiskDecision,
    intent_sha256: Sha256Digest,
    portfolio_snapshot_version: int,
    portfolio_snapshot_sha256: Sha256Digest,
    policy: Phase1RiskPolicy,
    risk_state_version: int,
    reason_code: RiskReasonCode,
    decision_next_before: int,
    decision_next_after: int | None,
    approval_next_before: int | None,
    approval_next_after: int | None,
) -> RiskEvaluationEvidence:
    if type(decision) is not RiskDecision:
        raise _fail(OutcomeCode.INVALID_TYPE, "decision must be an exact RiskDecision")
    if type(portfolio_snapshot_version) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, "portfolio snapshot version must be an exact int")
    if portfolio_snapshot_version < 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "portfolio snapshot version must be non-negative")
    if (
        type(intent_sha256) is not Sha256Digest
        or type(portfolio_snapshot_sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "evaluation input digests must be exact")
    if type(policy) is not Phase1RiskPolicy:
        raise _fail(OutcomeCode.INVALID_TYPE, "policy must be an exact Phase1RiskPolicy")
    if type(reason_code) is not RiskReasonCode:
        raise _fail(OutcomeCode.INVALID_TYPE, "reason_code must be exact")
    if type(risk_state_version) is not int or risk_state_version < 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "risk_state_version must be non-negative")
    if decision.risk_state_version != risk_state_version:
        raise _fail(OutcomeCode.CONFLICTING_ID, "decision risk-state version conflicts")
    if decision.intent_sha256 != intent_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "decision intent digest conflicts")
    _require_id(
        decision.decision_id,
        owner=EconomicOwnerKind.RISK_DECISION,
        run_id=decision.run_id,
        field_name="decision_id",
    )
    _require_allocated_transition(
        before=decision_next_before,
        after=decision_next_after,
        allocated_id=decision.decision_id,
        field_name="decision_next",
    )
    if reason_code not in _REASONS_BY_KIND[decision.kind]:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reason code conflicts with decision kind")

    approval_sha256: Sha256Digest | None = None
    if decision.kind in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE):
        if decision.approval is None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "executable decision lacks approval")
        _require_allocated_transition(
            before=approval_next_before,
            after=approval_next_after,
            allocated_id=decision.approval.approval_id,
            field_name="approval_next",
        )
        approval_sha256 = execution_approval_digest(decision.approval)
    else:
        _require_unchanged_transition(
            before=approval_next_before,
            after=approval_next_after,
            field_name="approval_next",
        )
        if decision.approval is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "non-executable decision carries approval")

    value = object.__new__(RiskEvaluationEvidence)
    object.__setattr__(value, "run_id", decision.run_id)
    object.__setattr__(value, "intent_id", decision.intent_id)
    object.__setattr__(value, "intent_sha256", intent_sha256)
    object.__setattr__(
        value,
        "portfolio_snapshot_version",
        portfolio_snapshot_version,
    )
    object.__setattr__(value, "portfolio_snapshot_sha256", portfolio_snapshot_sha256)
    object.__setattr__(value, "policy_id", policy.policy_id)
    object.__setattr__(value, "policy_sha256", phase1_risk_policy_digest(policy))
    object.__setattr__(value, "risk_state_version", risk_state_version)
    object.__setattr__(value, "decision_id", decision.decision_id)
    object.__setattr__(value, "decision_sha256", risk_decision_digest(decision))
    object.__setattr__(value, "approval_sha256", approval_sha256)
    object.__setattr__(value, "reason_code", reason_code)
    object.__setattr__(value, "decision_next_before", decision_next_before)
    object.__setattr__(value, "decision_next_after", decision_next_after)
    object.__setattr__(value, "approval_next_before", approval_next_before)
    object.__setattr__(value, "approval_next_after", approval_next_after)
    return value


@final
@dataclass(frozen=True, slots=True, init=False)
class RiskEvaluationResult:
    decision: RiskDecision
    evidence: RiskEvaluationEvidence

    def __init__(self) -> None:
        raise TypeError("RiskEvaluationResult values are created only by the risk authority")


def _create_risk_evaluation_result(
    decision: RiskDecision,
    evidence: RiskEvaluationEvidence,
) -> RiskEvaluationResult:
    if type(decision) is not RiskDecision or type(evidence) is not RiskEvaluationEvidence:
        raise _fail(OutcomeCode.INVALID_TYPE, "risk result values must be exact")
    if (
        evidence.run_id != decision.run_id
        or evidence.intent_id != decision.intent_id
        or evidence.decision_id != decision.decision_id
        or evidence.decision_sha256 != risk_decision_digest(decision)
        or evidence.risk_state_version != decision.risk_state_version
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk result decision/evidence binding conflicts")
    expected_approval = (
        None if decision.approval is None else execution_approval_digest(decision.approval)
    )
    if evidence.approval_sha256 != expected_approval:
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk result approval binding conflicts")
    value = object.__new__(RiskEvaluationResult)
    object.__setattr__(value, "decision", decision)
    object.__setattr__(value, "evidence", evidence)
    return value


def canonical_phase1_risk_policy_bytes(policy: Phase1RiskPolicy) -> bytes:
    if type(policy) is not Phase1RiskPolicy:
        raise _fail(OutcomeCode.INVALID_TYPE, "policy must be an exact Phase1RiskPolicy")
    return _encode_json(
        {
            "canonicalization": RISK_POLICY_CANONICALIZATION,
            "default_action": "deny",
            "execution_policy": _execution_policy_document(policy.execution_policy),
            "instrument_limits": [
                {
                    "instrument": _instrument_document(limit.instrument),
                    "maximum_absolute_position": limit.maximum_absolute_position.text,
                    "maximum_order_quantity": limit.maximum_order_quantity.text,
                }
                for limit in policy.instrument_limits
            ],
            "instrument_spec_set_id": policy.instrument_spec_set_id.value,
            "instrument_spec_set_sha256": policy.instrument_spec_set_sha256.value,
            "message_type": "phase1_risk_policy",
            "policy_id": policy.policy_id.value,
            "schema_version": RISK_POLICY_SCHEMA_VERSION,
        }
    )


def phase1_risk_policy_digest(policy: Phase1RiskPolicy) -> Sha256Digest:
    return _digest(RISK_POLICY_DIGEST_DOMAIN, canonical_phase1_risk_policy_bytes(policy))


def canonical_risk_state_snapshot_bytes(state: RiskStateSnapshot) -> bytes:
    if type(state) is not RiskStateSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "state must be an exact RiskStateSnapshot")
    return _encode_json(
        {
            "canonicalization": RISK_STATE_CANONICALIZATION,
            "conflict_existing_intent_sha256": (
                None
                if state.conflict_existing_intent_sha256 is None
                else state.conflict_existing_intent_sha256.value
            ),
            "conflict_submitted_intent_sha256": (
                None
                if state.conflict_submitted_intent_sha256 is None
                else state.conflict_submitted_intent_sha256.value
            ),
            "halt_causal_root_available_at": (
                None
                if state.halt_causal_root_available_at is None
                else _utc_text(state.halt_causal_root_available_at)
            ),
            "halt_dispatch_sequence": state.halt_dispatch_sequence,
            "halt_reason": None if state.halt_reason is None else state.halt_reason.value,
            "halted": state.halted,
            "message_type": "risk_state_snapshot",
            "policy_id": state.policy_id.value,
            "policy_sha256": state.policy_sha256.value,
            "risk_state_version": state.risk_state_version,
            "run_id": state.run_id.value,
            "schema_version": RISK_STATE_SCHEMA_VERSION,
        }
    )


def risk_state_snapshot_digest(state: RiskStateSnapshot) -> Sha256Digest:
    return _digest(RISK_STATE_DIGEST_DOMAIN, canonical_risk_state_snapshot_bytes(state))


def canonical_risk_evaluation_evidence_bytes(evidence: RiskEvaluationEvidence) -> bytes:
    if type(evidence) is not RiskEvaluationEvidence:
        raise _fail(OutcomeCode.INVALID_TYPE, "evidence must be exact")
    return _encode_json(
        {
            "approval_next_after": evidence.approval_next_after,
            "approval_next_before": evidence.approval_next_before,
            "approval_sha256": (
                None if evidence.approval_sha256 is None else evidence.approval_sha256.value
            ),
            "canonicalization": RISK_EVALUATION_EVIDENCE_CANONICALIZATION,
            "decision_id": _economic_id_document(evidence.decision_id),
            "decision_next_after": evidence.decision_next_after,
            "decision_next_before": evidence.decision_next_before,
            "decision_sha256": evidence.decision_sha256.value,
            "intent_id": _economic_id_document(evidence.intent_id),
            "intent_sha256": evidence.intent_sha256.value,
            "message_type": "risk_evaluation_evidence",
            "policy_id": evidence.policy_id.value,
            "policy_sha256": evidence.policy_sha256.value,
            "portfolio_snapshot_sha256": evidence.portfolio_snapshot_sha256.value,
            "portfolio_snapshot_version": evidence.portfolio_snapshot_version,
            "reason_code": evidence.reason_code.value,
            "risk_state_version": evidence.risk_state_version,
            "run_id": evidence.run_id.value,
            "schema_version": RISK_EVALUATION_EVIDENCE_SCHEMA_VERSION,
        }
    )


def risk_evaluation_evidence_digest(evidence: RiskEvaluationEvidence) -> Sha256Digest:
    return _digest(
        RISK_EVALUATION_EVIDENCE_DIGEST_DOMAIN,
        canonical_risk_evaluation_evidence_bytes(evidence),
    )


_REASONS_BY_KIND = {
    RiskDecisionKind.ALLOW: frozenset({RiskReasonCode.WITHIN_LIMITS}),
    RiskDecisionKind.RESIZE: frozenset(
        {
            RiskReasonCode.RESIZED_ORDER_LIMIT,
            RiskReasonCode.RESIZED_POSITION_LIMIT,
            RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS,
        }
    ),
    RiskDecisionKind.REJECT: frozenset(
        {
            RiskReasonCode.HALTED,
            RiskReasonCode.INSTRUMENT_NOT_CONFIGURED,
            RiskReasonCode.NO_POSITION_CAPACITY,
        }
    ),
    RiskDecisionKind.EVALUATION_FAILED: frozenset(
        {
            RiskReasonCode.STALE_PORTFOLIO_SNAPSHOT,
            RiskReasonCode.LINEAGE_MISMATCH,
            RiskReasonCode.ARITHMETIC_FAILURE,
            RiskReasonCode.APPROVAL_SEQUENCE_EXHAUSTED,
        }
    ),
}


def _require_id(
    identity: object,
    *,
    owner: EconomicOwnerKind,
    run_id: RunId,
    field_name: str,
) -> EconomicId:
    if type(identity) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an exact EconomicId")
    if identity.owner_kind is not owner or identity.run_id != run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} binding conflicts")
    return identity


def _require_non_negative(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an exact int")
    if value < 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be non-negative")
    return value


def _require_next(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an exact int or None")
    if value < 1 or value > _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be in uint64 allocation range")
    return value


def _require_allocated_transition(
    *,
    before: object,
    after: object,
    allocated_id: EconomicId,
    field_name: str,
) -> None:
    before_value = _require_next(before, field_name=f"{field_name}_before")
    after_value = _require_next(after, field_name=f"{field_name}_after")
    if before_value is None or allocated_id.owner_sequence != before_value:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} allocation binding conflicts")
    expected = None if before_value == _MAX_UINT64 else before_value + 1
    if after_value != expected:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} transition conflicts")


def _require_unchanged_transition(
    *,
    before: object,
    after: object,
    field_name: str,
) -> None:
    before_value = _require_next(before, field_name=f"{field_name}_before")
    after_value = _require_next(after, field_name=f"{field_name}_after")
    if before_value != after_value:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} must remain unchanged")


def _translate_economic_error(error: EconomicValidationError) -> RiskContractError:
    if error.code is OutcomeCode.INVALID_TYPE:
        code = OutcomeCode.INVALID_TYPE
    elif error.code is OutcomeCode.NOT_QUANTIZED:
        code = OutcomeCode.NOT_QUANTIZED
    elif error.code is OutcomeCode.CONFLICTING_ID:
        code = OutcomeCode.CONFLICTING_ID
    elif error.code in {
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NON_FINITE,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.ARITHMETIC_OVERFLOW,
    }:
        code = OutcomeCode.OUT_OF_RANGE
    else:
        raise AssertionError("unexpected economic validation outcome at risk boundary")
    return _fail(code, str(error))


def _instrument_document(instrument: Instrument) -> dict[str, object]:
    return {"symbol": instrument.symbol, "venue": instrument.venue.code}


def _execution_policy_document(policy: ExecutionPolicyRef) -> dict[str, object]:
    return {
        "execution_policy_id": policy.identifier.value,
        "execution_policy_sha256": policy.sha256.value,
    }


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _utc_text(value: datetime) -> str:
    try:
        canonical = require_utc(value, field="canonical timestamp")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    return canonical.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _integer_bytes(value: int) -> bytes:
    if type(value) is not int:
        raise TypeError("canonical integer encoder requires exact int")
    if value == 0:
        return b"0"
    sign = b""
    remaining = value
    if remaining < 0:
        sign = b"-"
        remaining = -remaining
    chunks: list[int] = []
    while remaining:
        remaining, chunk = divmod(remaining, 1_000_000_000)
        chunks.append(chunk)
    head = str(chunks.pop()).encode("ascii")
    tail = b"".join(f"{chunk:09d}".encode("ascii") for chunk in reversed(chunks))
    return sign + head + tail


def _encode_json(value: object) -> bytes:
    if value is None:
        return b"null"
    if type(value) is bool:
        return b"true" if value else b"false"
    if type(value) is str:
        return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    if type(value) is int:
        return _integer_bytes(value)
    if type(value) is list:
        return b"[" + b",".join(_encode_json(item) for item in value) + b"]"
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("canonical JSON object keys must be exact str")
        return (
            b"{"
            + b",".join(
                _encode_json(key) + b":" + _encode_json(value[key]) for key in sorted(value)
            )
            + b"}"
        )
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())
