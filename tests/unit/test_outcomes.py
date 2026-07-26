from __future__ import annotations

from types import MappingProxyType
from typing import cast

import pytest

from ea.core import (
    ECONOMIC_ERROR_CODES,
    OUTCOME_FAMILY_BY_CODE,
    OUTCOME_REGISTRY,
    EconomicErrorCode,
    EconomicValidationError,
    OutcomeCode,
    OutcomeFamily,
    outcome_family,
)

EXPECTED_CODES = (
    "validation.invalid_type",
    "validation.non_finite",
    "validation.out_of_range",
    "validation.not_quantized",
    "validation.price_domain",
    "validation.arithmetic_overflow",
    "validation.conflicting_id",
    "preflight.unsupported_platform",
    "preflight.provenance_unverified",
    "preflight.manifest_unverified",
    "durability.audit_append_failed",
    "durability.audit_ack_mismatch",
    "durability.result_write_failed",
    "risk.allowed",
    "risk.resized",
    "risk.rejected",
    "risk.evaluation_failed",
    "risk.stale_approval",
    "submission.submitted",
    "submission.definitely_not_submitted",
    "submission.uncertain",
    "submission.blocked_by_halt",
    "fact.accepted",
    "fact.duplicate",
    "fact.invalid",
    "fact.invalid.missing_dedup_identity",
    "fact.conflict",
    "fact.unresolved",
    "order.acknowledged",
    "order.rejected",
    "order.partially_filled",
    "order.filled",
    "order.expired",
    "order.cancelled",
    "order.expired.no_eligible_market_data",
    "ledger.applied",
    "ledger.duplicate",
    "ledger.conflict",
    "ledger.unbalanced",
    "ledger.rounding_unrepresentable",
    "reconciliation.match",
    "reconciliation.remote_ahead",
    "reconciliation.local_ahead_stale",
    "reconciliation.mismatch",
    "reconciliation.unresolved_correlation",
    "reconciliation.submission_unknown",
    "reconciliation.late_fact_after_terminal",
    "reconciliation.invalid",
    "reconciliation.quarantined",
    "reconciliation.submission.confirmed_submitted",
    "reconciliation.submission.confirmed_not_submitted",
    "reconciliation.submission.confirmed_rejected",
    "reconciliation.submission.confirmed_filled",
    "reconciliation.submission.still_unknown",
)


def test_outcome_registry_is_the_exact_closed_adr_0008_sequence() -> None:
    assert tuple(code.value for code in OUTCOME_REGISTRY) == EXPECTED_CODES
    assert tuple(OutcomeCode) == OUTCOME_REGISTRY
    assert len(set(OUTCOME_REGISTRY)) == len(EXPECTED_CODES)
    assert isinstance(OUTCOME_FAMILY_BY_CODE, MappingProxyType)


def test_every_outcome_has_exact_family_membership() -> None:
    for code in OUTCOME_REGISTRY:
        expected = OutcomeFamily(code.value.split(".", 1)[0])
        assert outcome_family(code) is expected
        assert OUTCOME_FAMILY_BY_CODE[code] is expected

    with pytest.raises(TypeError):
        outcome_family(cast(OutcomeCode, "risk.allowed"))
    with pytest.raises(ValueError):
        OutcomeCode("risk.not_reserved")


def test_economic_error_compatibility_alias_is_restricted_to_original_codes() -> None:
    assert EconomicErrorCode is OutcomeCode
    assert (
        frozenset(
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
        == ECONOMIC_ERROR_CODES
    )

    error = EconomicValidationError(OutcomeCode.INVALID_TYPE, "invalid")
    assert error.code is OutcomeCode.INVALID_TYPE

    with pytest.raises(TypeError):
        EconomicValidationError(OutcomeCode.RISK_REJECTED, "not economic")
    with pytest.raises(TypeError):
        EconomicValidationError(cast(OutcomeCode, "validation.invalid_type"), "raw string")
