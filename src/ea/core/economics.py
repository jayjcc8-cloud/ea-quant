"""Canonical decimal values and exact settlement arithmetic."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import (
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)
from enum import StrEnum
from functools import total_ordering
from typing import final

EA_DECIMAL_CANONICALIZATION = "ea-decimal-v1"
MAX_SIGNIFICANT_DIGITS = 38
MAX_INTEGER_DIGITS = 20
MAX_FRACTIONAL_DIGITS = 18

_DECIMAL_PATTERN = re.compile(
    r"0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9])\Z",
    flags=re.ASCII,
)
_NON_FINITE_PATTERN = re.compile(
    r"[+-]?(?:inf(?:inity)?|s?nan[0-9]*)\Z",
    flags=re.ASCII | re.IGNORECASE,
)
_PARSE_TRAPS: tuple[type[DecimalException], ...] = (
    Clamped,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
)


class EconomicErrorCode(StrEnum):
    """ADR 0008 outcome codes used by the exact economic-value slice."""

    INVALID_TYPE = "validation.invalid_type"
    NON_FINITE = "validation.non_finite"
    OUT_OF_RANGE = "validation.out_of_range"
    NOT_QUANTIZED = "validation.not_quantized"
    PRICE_DOMAIN = "validation.price_domain"
    ARITHMETIC_OVERFLOW = "validation.arithmetic_overflow"
    CONFLICTING_ID = "validation.conflicting_id"
    ROUNDING_UNREPRESENTABLE = "ledger.rounding_unrepresentable"


class EconomicValidationError(ValueError):
    """Structured fail-closed economic validation error."""

    code: EconomicErrorCode

    def __init__(self, code: EconomicErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def _fail(code: EconomicErrorCode, message: str) -> EconomicValidationError:
    return EconomicValidationError(code, message)


@final
@total_ordering
@dataclass(frozen=True, slots=True)
class CanonicalDecimal:
    """One bounded ``ea-decimal-v1`` value constructed from exact text."""

    text: str
    _coefficient: int = field(init=False, repr=False)
    _scale: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "canonical decimal input must have exact runtime type str",
            )
        if _NON_FINITE_PATTERN.fullmatch(self.text) is not None:
            raise _fail(EconomicErrorCode.NON_FINITE, "non-finite decimal input is forbidden")
        if _DECIMAL_PATTERN.fullmatch(self.text) is None:
            raise _fail(
                EconomicErrorCode.OUT_OF_RANGE,
                "decimal text does not match ea-decimal-v1",
            )

        unsigned_text = self.text.removeprefix("-")
        integer_text, separator, fractional_text = unsigned_text.partition(".")
        coefficient_digits = (integer_text + fractional_text).lstrip("0") or "0"
        significant_digits = len(coefficient_digits)
        integer_digits = len(integer_text)
        scale = len(fractional_text) if separator else 0
        if (
            significant_digits > MAX_SIGNIFICANT_DIGITS
            or integer_digits > MAX_INTEGER_DIGITS
            or scale > MAX_FRACTIONAL_DIGITS
        ):
            raise _fail(
                EconomicErrorCode.OUT_OF_RANGE,
                "decimal value exceeds the 38/20/18 digit limits",
            )

        parse_context = Context(
            prec=MAX_SIGNIFICANT_DIGITS,
            rounding=ROUND_HALF_EVEN,
            Emin=-MAX_FRACTIONAL_DIGITS,
            Emax=MAX_INTEGER_DIGITS - 1,
        )
        for signal in _PARSE_TRAPS:
            parse_context.traps[signal] = True
        parse_context.clear_flags()
        with localcontext(parse_context) as context:
            decimal_tuple = context.create_decimal(self.text).as_tuple()
        if not isinstance(decimal_tuple.exponent, int):
            raise _fail(EconomicErrorCode.NON_FINITE, "non-finite decimal input is forbidden")
        coefficient = int("".join(str(digit) for digit in decimal_tuple.digits))
        if decimal_tuple.sign:
            coefficient = -coefficient
        parsed_scale = -decimal_tuple.exponent

        if coefficient == 0:
            coefficient = 0
            parsed_scale = 0
        if parsed_scale != scale:
            raise AssertionError("explicit decimal parse changed the canonical scale")
        if parsed_scale > 0 and coefficient % 10 == 0:
            raise _fail(
                EconomicErrorCode.OUT_OF_RANGE,
                "fractional decimal coefficient must not contain trailing zeroes",
            )

        object.__setattr__(self, "_coefficient", coefficient)
        object.__setattr__(self, "_scale", parsed_scale)

    @property
    def coefficient(self) -> int:
        return self._coefficient

    @property
    def scale(self) -> int:
        return self._scale

    @property
    def significant_digits(self) -> int:
        return len(str(abs(self._coefficient))) if self._coefficient else 1

    @property
    def integer_digits(self) -> int:
        return max(self.significant_digits - self._scale, 1)

    @property
    def fractional_digits(self) -> int:
        return self._scale

    @property
    def canonical_bytes(self) -> bytes:
        return self.text.encode("ascii")

    def __str__(self) -> str:
        return self.text

    def __lt__(self, other: object) -> bool:
        if type(other) is not CanonicalDecimal:
            return NotImplemented
        scale = max(self._scale, other._scale)
        return self._at_scale(scale) < other._at_scale(scale)

    def _at_scale(self, scale: int) -> int:
        if scale < self._scale:
            raise AssertionError("aligned scale cannot be smaller than the value scale")
        factor = 1
        for _ in range(scale - self._scale):
            factor *= 10
        return self._coefficient * factor


@final
@dataclass(frozen=True, slots=True)
class ScalarSettlement:
    """One atomic bounded settlement result without domain identity."""

    amount: CanonicalDecimal
    rounding_residual: CanonicalDecimal

    def __post_init__(self) -> None:
        if (
            type(self.amount) is not CanonicalDecimal
            or type(self.rounding_residual) is not CanonicalDecimal
        ):
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "settlement values must be exact CanonicalDecimal instances",
            )


def require_positive(value: CanonicalDecimal, *, field_name: str) -> CanonicalDecimal:
    """Return one exact positive value or fail with the closed range code."""
    if type(value) is not CanonicalDecimal:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            f"{field_name} must be an exact CanonicalDecimal",
        )
    if value.coefficient <= 0:
        raise _fail(EconomicErrorCode.OUT_OF_RANGE, f"{field_name} must be strictly positive")
    return value


def require_quantized(
    value: CanonicalDecimal,
    quantum: CanonicalDecimal,
    *,
    field_name: str,
) -> CanonicalDecimal:
    """Require exact divisibility after aligning coefficient scales."""
    if type(value) is not CanonicalDecimal or type(quantum) is not CanonicalDecimal:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            f"{field_name} and its quantum must be exact CanonicalDecimal values",
        )
    require_positive(quantum, field_name=f"{field_name}_quantum")
    scale = max(value.scale, quantum.scale)
    value_coefficient = value.coefficient * (10 ** (scale - value.scale))
    quantum_coefficient = quantum.coefficient * (10 ** (scale - quantum.scale))
    if value_coefficient % quantum_coefficient != 0:
        raise _fail(
            EconomicErrorCode.NOT_QUANTIZED,
            f"{field_name} is not an exact multiple of its quantum",
        )
    return value


def _normalize_scaled(coefficient: int, scale: int) -> tuple[int, int]:
    if coefficient == 0:
        return (0, 0)
    while scale > 0 and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    return (coefficient, scale)


def _scaled_text(coefficient: int, scale: int) -> str:
    coefficient, scale = _normalize_scaled(coefficient, scale)
    if coefficient == 0:
        return "0"
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return sign + digits
    if len(digits) <= scale:
        digits = ("0" * (scale + 1 - len(digits))) + digits
    split = len(digits) - scale
    return f"{sign}{digits[:split]}.{digits[split:]}"


def _bounded_scaled(
    coefficient: int,
    scale: int,
    *,
    failure_code: EconomicErrorCode,
) -> CanonicalDecimal:
    try:
        return CanonicalDecimal(_scaled_text(coefficient, scale))
    except EconomicValidationError as error:
        if error.code is not EconomicErrorCode.OUT_OF_RANGE:
            raise
        raise _fail(failure_code, "exact settlement output exceeds canonical bounds") from None


def settle_product(
    price: CanonicalDecimal,
    quantity: CanonicalDecimal,
    contract_multiplier: CanonicalDecimal,
    currency_quantum: CanonicalDecimal,
) -> ScalarSettlement:
    """Settle one exact product at the sole ROUND_HALF_EVEN boundary."""
    if any(
        type(value) is not CanonicalDecimal
        for value in (price, quantity, contract_multiplier, currency_quantum)
    ):
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "settlement inputs must be exact CanonicalDecimal values",
        )
    require_positive(quantity, field_name="quantity")
    require_positive(contract_multiplier, field_name="contract_multiplier")
    require_positive(currency_quantum, field_name="currency_quantum")

    exact_coefficient = price.coefficient * quantity.coefficient * contract_multiplier.coefficient
    exact_scale = price.scale + quantity.scale + contract_multiplier.scale

    numerator = abs(exact_coefficient) * (10**currency_quantum.scale)
    denominator = currency_quantum.coefficient * (10**exact_scale)
    quotient, remainder = divmod(numerator, denominator)
    doubled_remainder = remainder * 2
    if doubled_remainder > denominator or (doubled_remainder == denominator and quotient % 2 == 1):
        quotient += 1
    units = -quotient if exact_coefficient < 0 else quotient

    settled_coefficient = units * currency_quantum.coefficient
    settled_scale = currency_quantum.scale
    amount = _bounded_scaled(
        settled_coefficient,
        settled_scale,
        failure_code=EconomicErrorCode.ARITHMETIC_OVERFLOW,
    )

    residual_scale = max(exact_scale, settled_scale)
    residual_coefficient = exact_coefficient * (
        10 ** (residual_scale - exact_scale)
    ) - settled_coefficient * (10 ** (residual_scale - settled_scale))
    residual = _bounded_scaled(
        residual_coefficient,
        residual_scale,
        failure_code=EconomicErrorCode.ROUNDING_UNREPRESENTABLE,
    )
    return ScalarSettlement(amount=amount, rounding_residual=residual)
