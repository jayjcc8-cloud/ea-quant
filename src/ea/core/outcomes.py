"""Closed machine-readable outcome registry from Accepted ADR 0008."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class OutcomeFamily(StrEnum):
    """Closed v1 outcome families."""

    VALIDATION = "validation"
    PREFLIGHT = "preflight"
    DURABILITY = "durability"
    RISK = "risk"
    SUBMISSION = "submission"
    FACT = "fact"
    ORDER = "order"
    LEDGER = "ledger"
    RECONCILIATION = "reconciliation"


class OutcomeCode(StrEnum):
    """Complete stable v1 outcome-code registry."""

    INVALID_TYPE = "validation.invalid_type"
    NON_FINITE = "validation.non_finite"
    OUT_OF_RANGE = "validation.out_of_range"
    NOT_QUANTIZED = "validation.not_quantized"
    PRICE_DOMAIN = "validation.price_domain"
    ARITHMETIC_OVERFLOW = "validation.arithmetic_overflow"
    CONFLICTING_ID = "validation.conflicting_id"

    PREFLIGHT_UNSUPPORTED_PLATFORM = "preflight.unsupported_platform"
    PREFLIGHT_PROVENANCE_UNVERIFIED = "preflight.provenance_unverified"
    PREFLIGHT_MANIFEST_UNVERIFIED = "preflight.manifest_unverified"

    DURABILITY_AUDIT_APPEND_FAILED = "durability.audit_append_failed"
    DURABILITY_AUDIT_ACK_MISMATCH = "durability.audit_ack_mismatch"
    DURABILITY_RESULT_WRITE_FAILED = "durability.result_write_failed"

    RISK_ALLOWED = "risk.allowed"
    RISK_RESIZED = "risk.resized"
    RISK_REJECTED = "risk.rejected"
    RISK_EVALUATION_FAILED = "risk.evaluation_failed"
    RISK_STALE_APPROVAL = "risk.stale_approval"

    SUBMISSION_SUBMITTED = "submission.submitted"
    SUBMISSION_DEFINITELY_NOT_SUBMITTED = "submission.definitely_not_submitted"
    SUBMISSION_UNCERTAIN = "submission.uncertain"
    SUBMISSION_BLOCKED_BY_HALT = "submission.blocked_by_halt"

    FACT_ACCEPTED = "fact.accepted"
    FACT_DUPLICATE = "fact.duplicate"
    FACT_INVALID = "fact.invalid"
    FACT_INVALID_MISSING_DEDUP_IDENTITY = "fact.invalid.missing_dedup_identity"
    FACT_CONFLICT = "fact.conflict"
    FACT_UNRESOLVED = "fact.unresolved"

    ORDER_ACKNOWLEDGED = "order.acknowledged"
    ORDER_REJECTED = "order.rejected"
    ORDER_PARTIALLY_FILLED = "order.partially_filled"
    ORDER_FILLED = "order.filled"
    ORDER_EXPIRED = "order.expired"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA = "order.expired.no_eligible_market_data"

    LEDGER_APPLIED = "ledger.applied"
    LEDGER_DUPLICATE = "ledger.duplicate"
    LEDGER_CONFLICT = "ledger.conflict"
    LEDGER_UNBALANCED = "ledger.unbalanced"
    ROUNDING_UNREPRESENTABLE = "ledger.rounding_unrepresentable"

    RECONCILIATION_MATCH = "reconciliation.match"
    RECONCILIATION_REMOTE_AHEAD = "reconciliation.remote_ahead"
    RECONCILIATION_LOCAL_AHEAD_STALE = "reconciliation.local_ahead_stale"
    RECONCILIATION_MISMATCH = "reconciliation.mismatch"
    RECONCILIATION_UNRESOLVED_CORRELATION = "reconciliation.unresolved_correlation"
    RECONCILIATION_SUBMISSION_UNKNOWN = "reconciliation.submission_unknown"
    RECONCILIATION_LATE_FACT_AFTER_TERMINAL = "reconciliation.late_fact_after_terminal"
    RECONCILIATION_INVALID = "reconciliation.invalid"
    RECONCILIATION_QUARANTINED = "reconciliation.quarantined"
    RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED = "reconciliation.submission.confirmed_submitted"
    RECONCILIATION_SUBMISSION_CONFIRMED_NOT_SUBMITTED = (
        "reconciliation.submission.confirmed_not_submitted"
    )
    RECONCILIATION_SUBMISSION_CONFIRMED_REJECTED = "reconciliation.submission.confirmed_rejected"
    RECONCILIATION_SUBMISSION_CONFIRMED_FILLED = "reconciliation.submission.confirmed_filled"
    RECONCILIATION_SUBMISSION_STILL_UNKNOWN = "reconciliation.submission.still_unknown"


OUTCOME_REGISTRY: tuple[OutcomeCode, ...] = tuple(OutcomeCode)
OUTCOME_FAMILY_BY_CODE = MappingProxyType(
    {code: OutcomeFamily(code.value.split(".", 1)[0]) for code in OUTCOME_REGISTRY}
)
ECONOMIC_ERROR_CODES: frozenset[OutcomeCode] = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.NON_FINITE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.ARITHMETIC_OVERFLOW,
        OutcomeCode.CONFLICTING_ID,
        OutcomeCode.ROUNDING_UNREPRESENTABLE,
    }
)


def outcome_family(code: OutcomeCode) -> OutcomeFamily:
    """Return exact family membership or reject a non-registry value."""
    if type(code) is not OutcomeCode:
        raise TypeError("outcome code must be an exact OutcomeCode")
    return OUTCOME_FAMILY_BY_CODE[code]
