from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from ea.core import CanonicalDecimal
from ea.strategy.registry import (
    BUILTIN_STRATEGIES,
    ParameterV1,
    StrategyDescriptorV1,
    StrategyRegistryV1,
)


def test_descriptors_are_canonical_and_research_visibility_is_explicit() -> None:
    registry = BUILTIN_STRATEGIES
    assert [d.strategy_id for d in registry.descriptors(research_only=True)] == [
        "bounded-long-v1",
        "moving-average-entry-v1",
    ]
    descriptor = registry.get("moving-average-entry-v1", 1).descriptor
    assert descriptor.canonical_bytes() == descriptor.canonical_bytes()
    assert descriptor.schema_version == 1
    assert [p.name for p in descriptor.parameters] == [
        "fast_window",
        "slow_window",
        "target_quantity",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"type": "boolean"},
        {"default": True},
        {"static_minimum": 3},
        {"static_maximum": 0},
        {"static_minimum": "1"},
        {"name": ""},
    ],
)
def test_invalid_parameter_definition_rejects(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ParameterV1(
            **{
                "name": "window",
                "type": "integer",
                "required": True,
                "default": 1,
                "static_minimum": 1,
                "static_maximum": 10,
                **change,
            }
        )


def test_duplicate_parameters_and_registry_ids_reject() -> None:
    entry = BUILTIN_STRATEGIES.get("bounded-long-v1", 1)
    with pytest.raises(ValueError):
        replace(entry.descriptor, parameters=entry.descriptor.parameters * 2)
    with pytest.raises(ValueError):
        StrategyRegistryV1((entry, entry))


@pytest.mark.parametrize(
    "parameters",
    [
        {"fast_window": True, "slow_window": 3, "target_quantity": "2"},
        {"fast_window": "1", "slow_window": 3, "target_quantity": "2"},
        {"fast_window": 1, "slow_window": 3, "target_quantity": 2},
        {"fast_window": 1, "slow_window": 3, "target_quantity": "2.0"},
        {"fast_window": 1, "slow_window": 3, "target_quantity": "0"},
        {"fast_window": 3, "slow_window": 3, "target_quantity": "2"},
        {"fast_window": 1, "slow_window": 3},
        {"fast_window": 1, "slow_window": 3, "target_quantity": "2", "extra": 1},
    ],
)
def test_closed_normalized_parameters_reject_invalid_values(parameters: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BUILTIN_STRATEGIES.get("moving-average-entry-v1", 1).normalize(parameters)


def test_normalization_has_sorted_keys_and_bound_version() -> None:
    entry = BUILTIN_STRATEGIES.get("moving-average-entry-v1", 1)
    assert list(entry.normalize({"target_quantity": "2", "slow_window": 3, "fast_window": 1})) == [
        "fast_window",
        "slow_window",
        "target_quantity",
    ]
    with pytest.raises(ValueError):
        BUILTIN_STRATEGIES.get("moving-average-entry-v1", 2)


def test_descriptor_rejects_duplicate_parameter_names() -> None:
    parameter = ParameterV1("n", "integer", True, 1, 1, None)
    with pytest.raises(ValueError):
        StrategyDescriptorV1(1, "test", 1, "Test", True, (parameter, parameter))


def test_moving_average_state_is_deterministic_and_requires_history() -> None:
    entry = BUILTIN_STRATEGIES.get("moving-average-entry-v1", 1)
    parameters = entry.normalize({"fast_window": 1, "slow_window": 3, "target_quantity": "2"})
    for _ in range(2):
        logic = entry.factory(parameters)
        assert [logic.on_market(value) for value in ("3", "2", "1", "2")] == [
            None,
            None,
            None,
            CanonicalDecimal("2"),
        ]
        assert logic.on_market("100") is None


def test_registry_rejects_cross_field_invalid_defaults() -> None:
    entry = BUILTIN_STRATEGIES.get("moving-average-entry-v1", 1)
    changed = replace(entry.descriptor.parameters[0], default=21)
    with pytest.raises(ValueError):
        StrategyRegistryV1(
            (
                replace(
                    entry,
                    descriptor=replace(
                        entry.descriptor, parameters=(changed, *entry.descriptor.parameters[1:])
                    ),
                ),
            )
        )


def test_legacy_strategy_outcome_invariants_remain_strict() -> None:
    with pytest.raises(ValueError):
        BUILTIN_STRATEGIES.get("bounded-long-v1", 1).validate_outcome(0, 0)
    with pytest.raises(ValueError):
        BUILTIN_STRATEGIES.get("always-flat-v1", 1).validate_outcome(1, 1)
    BUILTIN_STRATEGIES.get("moving-average-entry-v1", 1).validate_outcome(0, 0)
