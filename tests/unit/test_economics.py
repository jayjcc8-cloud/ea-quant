from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, Inexact, Rounded, localcontext
from typing import cast

import pytest

from ea.core import (
    CanonicalDecimal,
    EconomicErrorCode,
    EconomicValidationError,
    require_positive,
    require_quantized,
    settle_product,
)


def _assert_code(
    error: pytest.ExceptionInfo[EconomicValidationError],
    code: EconomicErrorCode,
) -> None:
    assert error.value.code is code


@pytest.mark.parametrize(
    ("text", "coefficient", "scale", "significant", "integer", "fractional"),
    [
        ("0", 0, 0, 1, 1, 0),
        ("1", 1, 0, 1, 1, 0),
        ("100", 100, 0, 3, 3, 0),
        ("-12.34", -1234, 2, 4, 2, 2),
        ("0.001", 1, 3, 1, 1, 3),
        ("-0.000000000000000001", -1, 18, 1, 1, 18),
        (
            "99999999999999999999.999999999999999999",
            99999999999999999999999999999999999999,
            18,
            38,
            20,
            18,
        ),
    ],
)
def test_canonical_decimal_has_one_exact_coefficient_scale_representation(
    text: str,
    coefficient: int,
    scale: int,
    significant: int,
    integer: int,
    fractional: int,
) -> None:
    value = CanonicalDecimal(text)

    assert value.text == text
    assert str(value) == text
    assert value.coefficient == coefficient
    assert value.scale == scale
    assert value.significant_digits == significant
    assert value.integer_digits == integer
    assert value.fractional_digits == fractional
    assert value.canonical_bytes == text.encode("ascii")


class _StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "value",
    [
        1,
        1.0,
        True,
        Decimal("1"),
        b"1",
        _StringSubclass("1"),
    ],
)
def test_canonical_decimal_rejects_every_non_exact_string_type(value: object) -> None:
    with pytest.raises(EconomicValidationError) as error:
        CanonicalDecimal(cast(str, value))

    _assert_code(error, EconomicErrorCode.INVALID_TYPE)


@pytest.mark.parametrize(
    "text",
    ["NaN", "-NaN42", "sNaN", "+Infinity", "-inf", "INF"],
)
def test_canonical_decimal_classifies_non_finite_tokens(text: str) -> None:
    with pytest.raises(EconomicValidationError) as error:
        CanonicalDecimal(text)

    _assert_code(error, EconomicErrorCode.NON_FINITE)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " 1",
        "1 ",
        "+1",
        "-0",
        "0.0",
        "-0.0",
        "01",
        "-01",
        ".1",
        "1.",
        "1.0",
        "1.230",
        "1e2",
        "1_000",
        "１００",
        "100000000000000000000",
        "0.0000000000000000001",
        "99999999999999999999.9999999999999999999",
    ],
)
def test_canonical_decimal_rejects_noncanonical_or_out_of_bounds_text(text: str) -> None:
    with pytest.raises(EconomicValidationError) as error:
        CanonicalDecimal(text)

    _assert_code(error, EconomicErrorCode.OUT_OF_RANGE)


def test_canonical_decimal_equality_hash_and_order_use_exact_value() -> None:
    values = (
        CanonicalDecimal("-10"),
        CanonicalDecimal("-0.1"),
        CanonicalDecimal("0"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1"),
        CanonicalDecimal("10"),
    )

    assert tuple(sorted(reversed(values))) == values
    assert len(set(values)) == len(values)
    assert CanonicalDecimal("1") == CanonicalDecimal("1")


@pytest.mark.parametrize(
    ("value", "quantum"),
    [
        ("0", "0.01"),
        ("1", "0.01"),
        ("1.2", "0.01"),
        ("-1.2", "0.01"),
        ("0.001", "0.001"),
        ("100", "25"),
    ],
)
def test_quantization_aligns_scales_with_exact_integer_divisibility(
    value: str,
    quantum: str,
) -> None:
    parsed = CanonicalDecimal(value)

    assert require_quantized(parsed, CanonicalDecimal(quantum), field_name="value") is parsed


def test_quantization_fails_without_silent_rounding() -> None:
    with pytest.raises(EconomicValidationError) as error:
        require_quantized(
            CanonicalDecimal("1.23"),
            CanonicalDecimal("0.1"),
            field_name="price",
        )

    _assert_code(error, EconomicErrorCode.NOT_QUANTIZED)


@pytest.mark.parametrize("text", ["0", "-1"])
def test_positive_requirement_is_distinct_from_grid_membership(text: str) -> None:
    with pytest.raises(EconomicValidationError) as error:
        require_positive(CanonicalDecimal(text), field_name="quantity")

    _assert_code(error, EconomicErrorCode.OUT_OF_RANGE)


@pytest.mark.parametrize(
    ("price", "amount", "residual"),
    [
        ("1.005", "1", "0.005"),
        ("1.015", "1.02", "-0.005"),
        ("-1.005", "-1", "-0.005"),
        ("-1.015", "-1.02", "0.005"),
    ],
)
def test_settlement_uses_signed_round_half_even_and_explicit_residual(
    price: str,
    amount: str,
    residual: str,
) -> None:
    result = settle_product(
        CanonicalDecimal(price),
        CanonicalDecimal("1"),
        CanonicalDecimal("1"),
        CanonicalDecimal("0.01"),
    )

    assert result.amount == CanonicalDecimal(amount)
    assert result.rounding_residual == CanonicalDecimal(residual)


def test_settlement_supports_exact_non_unit_currency_quantum() -> None:
    result = settle_product(
        CanonicalDecimal("12.5"),
        CanonicalDecimal("3"),
        CanonicalDecimal("2"),
        CanonicalDecimal("0.05"),
    )

    assert result.amount == CanonicalDecimal("75")
    assert result.rounding_residual == CanonicalDecimal("0")
    assert require_quantized(result.amount, CanonicalDecimal("0.05"), field_name="amount")


def test_unbounded_product_reaches_declared_output_overflow_instead_of_rounding() -> None:
    maximum_integer = CanonicalDecimal("99999999999999999999")

    with pytest.raises(EconomicValidationError) as error:
        settle_product(
            maximum_integer,
            maximum_integer,
            maximum_integer,
            CanonicalDecimal("1"),
        )

    _assert_code(error, EconomicErrorCode.ARITHMETIC_OVERFLOW)


def test_unrepresentable_rounding_residual_returns_no_partial_result() -> None:
    with pytest.raises(EconomicValidationError) as error:
        settle_product(
            CanonicalDecimal("0.000000000000000001"),
            CanonicalDecimal("0.000000000000000001"),
            CanonicalDecimal("1"),
            CanonicalDecimal("1"),
        )

    _assert_code(error, EconomicErrorCode.ROUNDING_UNREPRESENTABLE)


def test_ambient_decimal_context_cannot_change_parsing_grid_or_settlement() -> None:
    expected = settle_product(
        CanonicalDecimal("1.015"),
        CanonicalDecimal("3"),
        CanonicalDecimal("0.1"),
        CanonicalDecimal("0.01"),
    )

    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_CEILING
        context.traps[Inexact] = True
        context.flags[Inexact] = True
        context.flags[Rounded] = True
        actual = settle_product(
            CanonicalDecimal("1.015"),
            CanonicalDecimal("3"),
            CanonicalDecimal("0.1"),
            CanonicalDecimal("0.01"),
        )
        require_quantized(CanonicalDecimal("1.2"), CanonicalDecimal("0.01"), field_name="price")

    assert actual == expected


def test_public_decimal_value_exposes_no_float_or_decimal_coercion_api() -> None:
    value = CanonicalDecimal("1")

    assert not hasattr(value, "__float__")
    assert not hasattr(value, "as_decimal")
    assert not hasattr(value, "quantize")
