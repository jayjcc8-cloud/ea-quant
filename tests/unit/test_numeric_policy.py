from __future__ import annotations

import numpy as np
import pytest

from ea.core import NumericPolicyError, OrderedFloat64Policy


def test_ordered_float64_policy_uses_explicit_serial_left_fold() -> None:
    policy = OrderedFloat64Policy()

    assert policy.sum_ordered((1.0e16, -1.0e16, 1.0)) == 1.0
    assert policy.sum_ordered((1.0e16, 1.0, -1.0e16)) == 0.0
    assert policy.add(1.0, 2.0) == 3.0
    assert policy.subtract(3.0, 2.0) == 1.0
    assert policy.multiply(3.0, 2.0) == 6.0
    assert policy.divide(6.0, 2.0) == 3.0


@pytest.mark.parametrize(
    "values",
    [
        [1.0, 2.0],
        {1.0, 2.0},
        np.array([1.0, 2.0]),
    ],
)
def test_ordered_aggregation_rejects_mutable_unordered_or_backend_values(
    values: object,
) -> None:
    with pytest.raises(NumericPolicyError, match="exact immutable tuple"):
        OrderedFloat64Policy().sum_ordered(values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("add", (1, 2.0)),
        ("add", (float("nan"), 2.0)),
        ("multiply", (1.0e308, 1.0e308)),
        ("divide", (1.0, 0.0)),
    ],
)
def test_numeric_policy_fails_closed_on_non_binary64_or_nonfinite_results(
    operation: str,
    arguments: tuple[object, object],
) -> None:
    method = getattr(OrderedFloat64Policy(), operation)
    with pytest.raises(NumericPolicyError):
        method(*arguments)


def test_numeric_capability_exposes_no_parallel_or_backend_reduction_api() -> None:
    policy = OrderedFloat64Policy()

    assert not hasattr(policy, "parallel")
    assert not hasattr(policy, "dot")
    assert not hasattr(policy, "array")
    assert not hasattr(policy, "map")
