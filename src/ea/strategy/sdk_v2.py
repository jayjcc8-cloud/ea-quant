"""Bounded action contract; no execution or ledger authority crosses this seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from ea.core.economics import CanonicalDecimal, require_positive
from ea.strategy.registry import ParameterV1
from ea.strategy.sdk_v1 import StrategyBarV1


class PositionState(StrEnum):
    FLAT_INITIAL = "FLAT_INITIAL"
    LONG_OPEN = "LONG_OPEN"
    FLAT_CLOSED = "FLAT_CLOSED"


@dataclass(frozen=True, slots=True)
class PositionViewV2:
    state: PositionState
    quantity: CanonicalDecimal


@dataclass(frozen=True, slots=True)
class StrategyActionV2:
    action: str
    quantity: CanonicalDecimal | None = None


def decode_action(value: object) -> StrategyActionV2:
    if type(value) is not dict:
        raise ValueError("action must be a closed object")
    action = value.get("action")
    if action in ("HOLD", "EXIT_LONG") and set(value) == {"action"}:
        return StrategyActionV2(action)
    if action == "ENTER_LONG" and set(value) == {"action", "quantity"}:
        if type(value["quantity"]) is not str:
            raise ValueError("entry quantity must be canonical decimal")
        quantity = require_positive(CanonicalDecimal(value["quantity"]), field_name="quantity")
        return StrategyActionV2(action, quantity)
    raise ValueError("unsupported or ambiguous action")


@dataclass(frozen=True, slots=True)
class StrategyDescriptorV2:
    schema_version: int = 2
    strategy_id: str = "single-long-hold-roots-v1"
    strategy_version: int = 1
    display_name: str = "Single long hold roots"
    research_visible: bool = True
    action_contract: str = "V2"
    position_lifecycle: str = "single-long-round-trip-v1"
    parameters: tuple[ParameterV1, ...] = (
        ParameterV1("entry_delay", "integer", True, 0, 0, None),
        ParameterV1("hold_root_count", "integer", True, 1, 1, None),
        ParameterV1("target_quantity", "decimal", True, "1", "0", None),
    )

    def document(self) -> dict[str, Any]:
        return asdict(self)


class HoldRootsLogic:
    def __init__(self, parameters: Mapping[str, int | str]) -> None:
        self.parameters = parameters
        self.entry_roots = 0
        self.hold_roots = 0

    def on_bar(self, bar: StrategyBarV1, position: PositionViewV2) -> dict[str, str]:
        if position.state is PositionState.FLAT_INITIAL:
            self.entry_roots += 1
            if self.entry_roots > int(self.parameters["entry_delay"]):
                return {"action": "ENTER_LONG", "quantity": str(self.parameters["target_quantity"])}
        elif position.state is PositionState.LONG_OPEN:
            self.hold_roots += 1
            if self.hold_roots >= int(self.parameters["hold_root_count"]):
                return {"action": "EXIT_LONG"}
        return {"action": "HOLD"}


@dataclass(frozen=True, slots=True)
class StrategyEntryV2:
    descriptor: StrategyDescriptorV2 = StrategyDescriptorV2()

    def normalize(self, parameters: Mapping[str, object]) -> dict[str, int | str]:
        if not isinstance(parameters, Mapping) or set(parameters) != {
            p.name for p in self.descriptor.parameters
        }:
            raise ValueError("complete closed V2 parameters required")
        result = {p.name: p.normalize(parameters[p.name]) for p in self.descriptor.parameters}
        require_positive(
            CanonicalDecimal(str(result["target_quantity"])), field_name="target_quantity"
        )
        return result

    def factory(self, parameters: Mapping[str, int | str]) -> HoldRootsLogic:
        logic_type = (
            RepeatedHoldRootsLogic
            if self.descriptor.position_lifecycle == "bounded-long-round-trips-v1"
            else HoldRootsLogic
        )
        return logic_type(self.normalize(parameters))


ROUND_TRIP_STRATEGY = StrategyEntryV2()


BOUNDED_ROUND_TRIP_STRATEGY = StrategyEntryV2(
    StrategyDescriptorV2(
        strategy_id="bounded-long-hold-roots-v1",
        display_name="Bounded long hold roots",
        position_lifecycle="bounded-long-round-trips-v1",
    )
)


class RepeatedHoldRootsLogic(HoldRootsLogic):
    def on_bar(self, bar: StrategyBarV1, position: PositionViewV2) -> dict[str, str]:
        action = super().on_bar(bar, position)
        if action["action"] == "EXIT_LONG":
            self.entry_roots = self.hold_roots = 0
        return action
