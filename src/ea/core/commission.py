"""Deterministic execution assumptions with exact arithmetic and bound identities."""

from __future__ import annotations

from datetime import datetime, timedelta
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
COST_POLICY_PREFIX = "phase2.next-bar-close.cost-v1.slippage-"
LATENCY_POLICY_PREFIX = "phase2.next-bar-close.latency-v1.ms-"


def require_latency_ms(value: int) -> int:
    if type(value) is not int:
        raise EconomicValidationError(OutcomeCode.INVALID_TYPE, "latency ms must be an integer")
    if not 0 <= value <= 86400000:
        raise EconomicValidationError(OutcomeCode.OUT_OF_RANGE, "latency ms outside [0,86400000]")
    return value


def execution_latency_policy_identity(
    latency_ms: int,
    slippage_bps: CanonicalDecimal | None,
    commission_bps: CanonicalDecimal | None,
) -> tuple[str, str]:
    require_latency_ms(latency_ms)
    slip = "none" if slippage_bps is None else require_slippage_bps(slippage_bps).text
    fee = "none" if commission_bps is None else require_commission_bps(commission_bps).text
    identifier = (
        LATENCY_POLICY_PREFIX + str(latency_ms) + ".slippage-" + slip + ".commission-" + fee
    )
    digest = sha256(
        (
            "ea.execution-latency-policy.v1\0"
            + identifier
            + "\0strict-event-time-after-submission-latency\0absolute-close-adverse-nearest-tick"
        ).encode("ascii")
    ).hexdigest()
    return identifier, digest


def _latency_costs(
    identifier: str, digest: str
) -> tuple[int, CanonicalDecimal | None, CanonicalDecimal | None]:
    delay_and_cost = identifier[len(LATENCY_POLICY_PREFIX) :].split(".slippage-")
    if len(delay_and_cost) != 2:
        raise EconomicValidationError(OutcomeCode.CONFLICTING_ID, "latency policy conflicts")
    delay, cost = delay_and_cost
    values = cost.split(".commission-")
    if not 1 <= len(delay) <= 8 or not delay.isascii() or not delay.isdigit() or len(values) != 2:
        raise EconomicValidationError(OutcomeCode.CONFLICTING_ID, "latency policy conflicts")
    milliseconds = require_latency_ms(int(delay))
    slip = None if values[0] == "none" else require_slippage_bps(CanonicalDecimal(values[0]))
    fee = None if values[1] == "none" else require_commission_bps(CanonicalDecimal(values[1]))
    if execution_latency_policy_identity(milliseconds, slip, fee) != (identifier, digest):
        raise EconomicValidationError(OutcomeCode.CONFLICTING_ID, "latency policy digest conflicts")
    return milliseconds, slip, fee


def latency_ms_from_identity(identifier: str, digest: str) -> int:
    if not identifier.startswith(LATENCY_POLICY_PREFIX):
        return 0
    return _latency_costs(identifier, digest)[0]


def execution_latency_allows(
    event_time: datetime, submitted_at: datetime, identifier: str, digest: str
) -> bool:
    # Subtract timestamps rather than adding to a datetime near its maximum year.
    return event_time - submitted_at > timedelta(
        milliseconds=latency_ms_from_identity(identifier, digest)
    )


def require_slippage_bps(value: CanonicalDecimal) -> CanonicalDecimal:
    if type(value) is not CanonicalDecimal:
        raise EconomicValidationError(OutcomeCode.INVALID_TYPE, "slippage bps must be canonical")
    if value.coefficient < 0 or value >= CanonicalDecimal("10000"):
        raise EconomicValidationError(OutcomeCode.OUT_OF_RANGE, "slippage bps outside [0,10000)")
    return value


def execution_cost_policy_identity(
    slippage_bps: CanonicalDecimal, commission_bps: CanonicalDecimal | None
) -> tuple[str, str]:
    require_slippage_bps(slippage_bps)
    fee = "none" if commission_bps is None else require_commission_bps(commission_bps).text
    identifier = COST_POLICY_PREFIX + slippage_bps.text + ".commission-" + fee
    digest = sha256(
        (
            "ea.execution-cost-policy.v1\0" + identifier + "\0absolute-close-adverse-nearest-tick"
        ).encode("ascii")
    ).hexdigest()
    return identifier, digest


def _execution_costs(
    identifier: str, digest: str
) -> tuple[CanonicalDecimal, CanonicalDecimal | None]:
    values = identifier[len(COST_POLICY_PREFIX) :].split(".commission-")
    if len(values) != 2:
        raise EconomicValidationError(OutcomeCode.CONFLICTING_ID, "execution cost policy conflicts")
    slippage = require_slippage_bps(CanonicalDecimal(values[0]))
    commission = (
        None if values[1] == "none" else require_commission_bps(CanonicalDecimal(values[1]))
    )
    if execution_cost_policy_identity(slippage, commission) != (identifier, digest):
        raise EconomicValidationError(OutcomeCode.CONFLICTING_ID, "execution cost digest conflicts")
    return slippage, commission


def slippage_bps_from_identity(identifier: str, digest: str) -> CanonicalDecimal | None:
    if identifier.startswith(LATENCY_POLICY_PREFIX):
        return _latency_costs(identifier, digest)[1]
    if not identifier.startswith(COST_POLICY_PREFIX):
        return None
    return _execution_costs(identifier, digest)[0]


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
    if identifier.startswith(LATENCY_POLICY_PREFIX):
        return _latency_costs(identifier, digest)[2]
    if identifier.startswith(COST_POLICY_PREFIX):
        return _execution_costs(identifier, digest)[1]
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
