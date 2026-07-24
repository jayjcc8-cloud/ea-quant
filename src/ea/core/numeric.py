"""Narrow deterministic binary64 operations for the economic causal cone."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

DETERMINISTIC_FLOAT64_POLICY = "deterministic-ordered-float64-v1"


class NumericPolicyError(ValueError):
    """Raised when an operation cannot satisfy the declared numeric policy."""


def _require_float(value: object, *, field: str) -> float:
    if type(value) is not float or not isfinite(value):
        raise NumericPolicyError(f"{field} must be an exact finite binary64 float")
    return value


def _require_result(value: float) -> float:
    if not isfinite(value):
        raise NumericPolicyError("binary64 operation produced a non-finite result")
    return value


@dataclass(frozen=True, slots=True)
class OrderedFloat64Policy:
    """Serial, explicitly ordered Python-float operations allowed by ADR 0006."""

    identifier: str = DETERMINISTIC_FLOAT64_POLICY

    def __post_init__(self) -> None:
        if type(self.identifier) is not str or self.identifier != DETERMINISTIC_FLOAT64_POLICY:
            raise NumericPolicyError("numeric policy identifier is unsupported")

    def add(self, left: float, right: float) -> float:
        return _require_result(
            _require_float(left, field="left") + _require_float(right, field="right")
        )

    def subtract(self, left: float, right: float) -> float:
        return _require_result(
            _require_float(left, field="left") - _require_float(right, field="right")
        )

    def multiply(self, left: float, right: float) -> float:
        return _require_result(
            _require_float(left, field="left") * _require_float(right, field="right")
        )

    def divide(self, left: float, right: float) -> float:
        numerator = _require_float(left, field="left")
        denominator = _require_float(right, field="right")
        try:
            return _require_result(numerator / denominator)
        except ZeroDivisionError as exc:
            raise NumericPolicyError("binary64 division by zero is forbidden") from exc

    def sum_ordered(self, values: tuple[float, ...]) -> float:
        """Fold one immutable tuple from left to right without a backend reduction."""
        if type(values) is not tuple:
            raise NumericPolicyError("ordered aggregation requires an exact immutable tuple")
        total = 0.0
        for index, value in enumerate(values):
            total = _require_result(total + _require_float(value, field=f"values[{index}]"))
        return total
