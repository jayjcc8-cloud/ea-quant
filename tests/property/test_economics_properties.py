from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ea.core import CanonicalDecimal, require_quantized, settle_product


def _canonical_text(coefficient: int, scale: int) -> str:
    if coefficient == 0:
        return "0"
    while scale > 0 and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return sign + digits
    if len(digits) <= scale:
        digits = ("0" * (scale + 1 - len(digits))) + digits
    split = len(digits) - scale
    return f"{sign}{digits[:split]}.{digits[split:]}"


COEFFICIENTS = st.integers(min_value=-(10**12), max_value=10**12)
SCALES = st.integers(min_value=0, max_value=6)


def _at_scale(value: CanonicalDecimal, scale: int) -> int:
    return value.coefficient * (10 ** (scale - value.scale))


@given(coefficient=COEFFICIENTS, scale=SCALES)
def test_parser_render_is_bijective_and_zero_is_unique(coefficient: int, scale: int) -> None:
    text = _canonical_text(coefficient, scale)
    value = CanonicalDecimal(text)

    assert value.text == text
    assert CanonicalDecimal(value.canonical_bytes.decode("ascii")) == value
    if value.coefficient == 0:
        assert value.text == "0"
        assert value.scale == 0
    elif value.scale > 0:
        assert value.coefficient % 10 != 0


@given(
    quantum_coefficient=st.integers(min_value=1, max_value=100_000),
    quantum_scale=st.integers(min_value=0, max_value=5),
    units=st.integers(min_value=-100_000, max_value=100_000),
)
def test_exact_integer_multiples_are_quantized_across_scales(
    quantum_coefficient: int,
    quantum_scale: int,
    units: int,
) -> None:
    quantum = CanonicalDecimal(_canonical_text(quantum_coefficient, quantum_scale))
    value = CanonicalDecimal(_canonical_text(quantum.coefficient * units, quantum.scale))

    assert require_quantized(value, quantum, field_name="generated") == value


@given(
    coefficient=st.integers(min_value=1, max_value=999_999),
    scale=st.integers(min_value=0, max_value=5),
)
def test_settlement_is_sign_symmetric(coefficient: int, scale: int) -> None:
    positive = CanonicalDecimal(_canonical_text(coefficient, scale))
    negative = CanonicalDecimal(_canonical_text(-coefficient, scale))
    quantity = CanonicalDecimal("1")
    multiplier = CanonicalDecimal("1")
    quantum = CanonicalDecimal("0.01")

    positive_result = settle_product(positive, quantity, multiplier, quantum)
    negative_result = settle_product(negative, quantity, multiplier, quantum)

    assert negative_result.amount.coefficient == -positive_result.amount.coefficient
    assert negative_result.amount.scale == positive_result.amount.scale
    assert (
        negative_result.rounding_residual.coefficient
        == -positive_result.rounding_residual.coefficient
    )
    assert negative_result.rounding_residual.scale == positive_result.rounding_residual.scale


@given(
    price_coefficient=st.integers(min_value=-999_999, max_value=999_999),
    price_scale=st.integers(min_value=0, max_value=5),
    quantity_coefficient=st.integers(min_value=1, max_value=10_000),
    quantity_scale=st.integers(min_value=0, max_value=4),
    multiplier_coefficient=st.integers(min_value=1, max_value=1_000),
    multiplier_scale=st.integers(min_value=0, max_value=3),
    quantum_coefficient=st.integers(min_value=1, max_value=1_000),
    quantum_scale=st.integers(min_value=0, max_value=4),
)
def test_settlement_preserves_exact_value_and_nearest_quantum_distance(
    price_coefficient: int,
    price_scale: int,
    quantity_coefficient: int,
    quantity_scale: int,
    multiplier_coefficient: int,
    multiplier_scale: int,
    quantum_coefficient: int,
    quantum_scale: int,
) -> None:
    price = CanonicalDecimal(_canonical_text(price_coefficient, price_scale))
    quantity = CanonicalDecimal(_canonical_text(quantity_coefficient, quantity_scale))
    multiplier = CanonicalDecimal(_canonical_text(multiplier_coefficient, multiplier_scale))
    quantum = CanonicalDecimal(_canonical_text(quantum_coefficient, quantum_scale))

    result = settle_product(price, quantity, multiplier, quantum)
    exact_coefficient = price.coefficient * quantity.coefficient * multiplier.coefficient
    exact_scale = price.scale + quantity.scale + multiplier.scale
    common_scale = max(
        exact_scale,
        result.amount.scale,
        result.rounding_residual.scale,
        quantum.scale,
    )

    exact_at_scale = exact_coefficient * (10 ** (common_scale - exact_scale))
    assert exact_at_scale == _at_scale(result.amount, common_scale) + _at_scale(
        result.rounding_residual,
        common_scale,
    )
    assert 2 * abs(_at_scale(result.rounding_residual, common_scale)) <= _at_scale(
        quantum,
        common_scale,
    )


@given(
    whole=st.integers(min_value=-100_000, max_value=100_000),
    tie_digit=st.sampled_from((5, 15, 25, 35, 45, 55, 65, 75, 85, 95)),
)
def test_half_cent_ties_always_choose_even_currency_units(whole: int, tie_digit: int) -> None:
    coefficient = whole * 1000 + (tie_digit if whole >= 0 else -tie_digit)
    price = CanonicalDecimal(_canonical_text(coefficient, 3))

    result = settle_product(
        price,
        CanonicalDecimal("1"),
        CanonicalDecimal("1"),
        CanonicalDecimal("0.01"),
    )

    amount_in_cents = result.amount.coefficient * (10 ** (2 - result.amount.scale))
    assert amount_in_cents % 2 == 0
    assert abs(result.rounding_residual.coefficient) == 5
    assert result.rounding_residual.scale == 3
