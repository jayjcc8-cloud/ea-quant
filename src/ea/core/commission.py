"""One deterministic commission rule; exact integer arithmetic, no ambient context."""

from __future__ import annotations

from hashlib import sha256

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    _bounded_scaled,
    require_positive,
)
from ea.core.outcomes import OutcomeCode

COMMISSION_POLICY = "deterministic-commission-v1"
POLICY_PREFIX = "phase2.next-bar-close.commission-v1.bps-"


def require_commission_bps(value: CanonicalDecimal) -> CanonicalDecimal:
    if type(value) is not CanonicalDecimal:
        raise EconomicValidationError(OutcomeCode.INVALID_TYPE, "commission bps must be canonical")
    if value.coefficient < 0 or value > CanonicalDecimal("10000"):
        raise EconomicValidationError(OutcomeCode.OUT_OF_RANGE, "commission bps outside [0,10000]")
    return value


def commission_policy_identity(bps: CanonicalDecimal) -> tuple[str, str]:
    require_commission_bps(bps)
    identifier = POLICY_PREFIX + bps.text
    digest = sha256(
        ("ea.commission-policy.v1\0" + identifier + "\0per-fill-half-even-currency-quantum").encode(
            "ascii"
        )
    ).hexdigest()
    return identifier, digest


def commission_bps_from_identity(identifier: str, digest: str) -> CanonicalDecimal | None:
    if not identifier.startswith(POLICY_PREFIX):
        return None
    bps = require_commission_bps(CanonicalDecimal(identifier[len(POLICY_PREFIX) :]))
    if commission_policy_identity(bps) != (identifier, digest):
        raise EconomicValidationError(
            OutcomeCode.CONFLICTING_ID, "commission policy digest conflicts"
        )
    return bps


def commission_amount(
    price: CanonicalDecimal,
    quantity: CanonicalDecimal,
    multiplier: CanonicalDecimal,
    bps: CanonicalDecimal,
    quantum: CanonicalDecimal,
) -> CanonicalDecimal:
    require_commission_bps(bps)
    require_positive(quantity, field_name="quantity")
    require_positive(multiplier, field_name="multiplier")
    require_positive(quantum, field_name="currency_quantum")
    if type(price) is not CanonicalDecimal or price.coefficient < 0:
        raise EconomicValidationError(
            OutcomeCode.OUT_OF_RANGE, "commission price must be nonnegative"
        )
    numerator = price.coefficient * quantity.coefficient * multiplier.coefficient * bps.coefficient
    scale = price.scale + quantity.scale + multiplier.scale + bps.scale + 4
    numerator *= 10**quantum.scale
    denominator = quantum.coefficient * 10**scale
    units, remainder = divmod(numerator, denominator)
    if remainder * 2 > denominator or (remainder * 2 == denominator and units % 2):
        units += 1
    return _bounded_scaled(
        units * quantum.coefficient, quantum.scale, failure_code=OutcomeCode.ARITHMETIC_OVERFLOW
    )
