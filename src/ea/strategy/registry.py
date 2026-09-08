"""Versioned descriptors and closed, distribution-owned strategy registration."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal

from ea.core.economics import CanonicalDecimal, require_positive, require_quantized
from ea.core.market_data import Adjustment, MarketDataEnvelope

ParameterValue = int | str


@dataclass(frozen=True, slots=True)
class ParameterV1:
    name: str
    type: Literal["integer", "decimal"]
    required: bool
    default: ParameterValue
    static_minimum: ParameterValue | None
    static_maximum: ParameterValue | None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError("invalid parameter name")
        if self.type not in {"integer", "decimal"} or type(self.required) is not bool:
            raise ValueError("unsupported parameter definition")
        for value in (self.default, self.static_minimum, self.static_maximum):
            if value is not None:
                self._typed(value)
        self.normalize(self.default)
        if (
            self.static_minimum is not None
            and self.static_maximum is not None
            and Decimal(self.static_minimum) > Decimal(self.static_maximum)
        ):
            raise ValueError("invalid parameter bounds")

    def _typed(self, value: object) -> ParameterValue:
        if self.type == "integer":
            if type(value) is not int:
                raise ValueError(f"{self.name} must be an integer")
            return value
        if type(value) is not str:
            raise ValueError(f"{self.name} must be an ea-decimal-v1 string")
        return CanonicalDecimal(value).text

    def normalize(self, value: object) -> ParameterValue:
        normalized = self._typed(value)
        if self.static_minimum is not None and Decimal(normalized) < Decimal(self.static_minimum):
            raise ValueError(f"{self.name} is below minimum")
        if self.static_maximum is not None and Decimal(normalized) > Decimal(self.static_maximum):
            raise ValueError(f"{self.name} is above maximum")
        return normalized


@dataclass(frozen=True, slots=True)
class StrategyDescriptorV1:
    schema_version: int
    strategy_id: str
    strategy_version: int
    display_name: str
    research_visible: bool
    parameters: tuple[ParameterV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported descriptor schema")
        if type(self.strategy_version) is not int or self.strategy_version < 1:
            raise ValueError("invalid strategy version")
        if not self.strategy_id or not self.display_name or type(self.research_visible) is not bool:
            raise ValueError("invalid descriptor")
        if len({p.name for p in self.parameters}) != len(self.parameters):
            raise ValueError("duplicate parameter name")

    def document(self) -> dict[str, Any]:
        return asdict(self)

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.document(), sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True)
class ResolvedStrategyParameterV1:
    parameter: ParameterV1
    minimum: ParameterValue | None
    maximum: ParameterValue | None

    def document(self, current: ParameterValue, default: ParameterValue) -> dict[str, Any]:
        return {
            **asdict(self.parameter),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "current_value": current,
            "default": default,
        }


@dataclass(frozen=True, slots=True)
class StrategyEntryV1:
    descriptor: StrategyDescriptorV1
    validate: Callable[[Mapping[str, ParameterValue]], None]
    factory: Callable[[Mapping[str, ParameterValue]], EntryLogic]
    validate_outcome: Callable[[int, int], None]

    def normalize(self, parameters: Mapping[str, object]) -> dict[str, ParameterValue]:
        if not isinstance(parameters, Mapping):
            raise ValueError("parameters must be a mapping")
        declared = {p.name: p for p in self.descriptor.parameters}
        if parameters.keys() - declared.keys():
            raise ValueError("unknown strategy parameter")
        result = {}
        for name, parameter in sorted(declared.items()):
            if name not in parameters and parameter.required:
                raise ValueError(f"missing strategy parameter {name}")
            result[name] = parameter.normalize(parameters.get(name, parameter.default))
        self.validate(result)
        return result


class StrategyRegistryV1:
    """Explicit constructor entries only; no import discovery or user package loading."""

    def __init__(self, entries: tuple[StrategyEntryV1, ...]) -> None:
        if len({e.descriptor.strategy_id for e in entries}) != len(entries):
            raise ValueError("duplicate strategy ID")
        for entry in entries:
            entry.normalize({p.name: p.default for p in entry.descriptor.parameters})
        self._entries = {e.descriptor.strategy_id: e for e in entries}

    def get(self, strategy_id: str, version: int) -> StrategyEntryV1:
        entry = self._entries.get(strategy_id)
        if (
            entry is None
            or type(version) is not int
            or version != entry.descriptor.strategy_version
        ):
            raise ValueError("unknown strategy ID/version")
        return entry

    def descriptors(self, *, research_only: bool = False) -> tuple[StrategyDescriptorV1, ...]:
        return tuple(
            e.descriptor
            for e in self._entries.values()
            if not research_only or e.descriptor.research_visible
        )


class EntryLogic:
    """One-run deterministic state; called only after active market verification."""

    def __init__(self, parameters: Mapping[str, ParameterValue], *, mode: str) -> None:
        self.parameters = parameters
        self.mode = mode
        self.count = 0
        self.closes: list[Fraction] = []
        self.entered = False

    def on_event(self, event: MarketDataEnvelope) -> CanonicalDecimal | None:
        if self.mode == "average" and (
            event.revision != 0 or event.payload.adjustment is not Adjustment.RAW
        ):
            return None
        return self.on_market(event.payload.close)

    def on_market(self, close: object) -> CanonicalDecimal | None:
        if self.entered or self.mode == "flat":
            return None
        self.count += 1
        if self.mode == "delay":
            trigger = self.count > int(self.parameters["entry_delay_bars"])
        else:
            slow = int(self.parameters["slow_window"])
            fast = int(self.parameters["fast_window"])
            self.closes.append(Fraction(str(close)))
            self.closes = self.closes[-slow:]
            trigger = len(self.closes) == slow and (
                sum(self.closes[-fast:]) * slow > sum(self.closes) * fast
            )
        if not trigger:
            return None
        self.entered = True
        return CanonicalDecimal(str(self.parameters["target_quantity"]))


def _flat_factory(parameters: Mapping[str, ParameterValue]) -> EntryLogic:
    return EntryLogic(parameters, mode="flat")


def _delay_factory(parameters: Mapping[str, ParameterValue]) -> EntryLogic:
    return EntryLogic(parameters, mode="delay")


def _average_factory(parameters: Mapping[str, ParameterValue]) -> EntryLogic:
    return EntryLogic(parameters, mode="average")


def _positive_quantity(parameters: Mapping[str, ParameterValue]) -> None:
    require_positive(
        CanonicalDecimal(str(parameters["target_quantity"])), field_name="target_quantity"
    )


def _moving_average(parameters: Mapping[str, ParameterValue]) -> None:
    _positive_quantity(parameters)
    if int(parameters["fast_window"]) >= int(parameters["slow_window"]):
        raise ValueError("fast_window must be less than slow_window")


def _flat(parameters: Mapping[str, ParameterValue]) -> None:
    pass


def _requires_entry(orders: int, fills: int) -> None:
    if (orders, fills) != (1, 1):
        raise ValueError("entry strategy requires one order and Fill")


def _requires_flat(orders: int, fills: int) -> None:
    if (orders, fills) != (0, 0):
        raise ValueError("flat strategy forbids orders and Fills")


def _optional_entry(orders: int, fills: int) -> None:
    if (orders, fills) not in {(0, 0), (1, 1)}:
        raise ValueError("conditional entry requires zero or one completed entry")


_QUANTITY = ParameterV1("target_quantity", "decimal", True, "1", "0", None)
BUILTIN_STRATEGIES = StrategyRegistryV1(
    (
        StrategyEntryV1(
            StrategyDescriptorV1(1, "always-flat-v1", 1, "Always flat", False, ()),
            _flat,
            _flat_factory,
            _requires_flat,
        ),
        StrategyEntryV1(
            StrategyDescriptorV1(
                1,
                "bounded-long-v1",
                1,
                "Bounded long",
                True,
                (
                    _QUANTITY,
                    ParameterV1("entry_delay_bars", "integer", True, 0, 0, None),
                ),
            ),
            _positive_quantity,
            _delay_factory,
            _requires_entry,
        ),
        StrategyEntryV1(
            StrategyDescriptorV1(
                1,
                "moving-average-entry-v1",
                1,
                "Moving average entry",
                True,
                (
                    ParameterV1("fast_window", "integer", True, 5, 1, None),
                    ParameterV1("slow_window", "integer", True, 20, 2, None),
                    _QUANTITY,
                ),
            ),
            _moving_average,
            _average_factory,
            _optional_entry,
        ),
    )
)


def project_parameters(strategy: Mapping[str, Any]) -> dict[str, ParameterValue]:
    """Read-time projection; never rewrites historical V1 evidence."""
    entry = BUILTIN_STRATEGIES.get(strategy["id"], strategy.get("version", 1))
    if "parameters" in strategy:
        return entry.normalize(strategy["parameters"])
    values = {p.name: strategy.get(p.name, p.default) for p in entry.descriptor.parameters}
    return entry.normalize(values)


def resolve_parameters(
    strategy_id: str,
    version: int,
    parameters: Mapping[str, object],
    *,
    quantity_quantum: CanonicalDecimal,
    last_entry_index: int | None,
    history_bars: int | None = None,
) -> tuple[ResolvedStrategyParameterV1, ...]:
    entry = BUILTIN_STRATEGIES.get(strategy_id, version)
    normalized = entry.normalize(parameters)
    resolved = []
    for parameter in entry.descriptor.parameters:
        minimum, maximum = parameter.static_minimum, parameter.static_maximum
        if parameter.name == "target_quantity":
            minimum = quantity_quantum.text
            require_quantized(
                CanonicalDecimal(str(normalized[parameter.name])),
                quantity_quantum,
                field_name=parameter.name,
            )
        if parameter.name == "entry_delay_bars":
            if last_entry_index is None:
                raise ValueError("strategy requires a market bar with an executable next bar")
            maximum = last_entry_index
        if parameter.name in {"fast_window", "slow_window"}:
            if last_entry_index is None:
                raise ValueError("strategy requires history and a later executable bar")
            maximum = history_bars if history_bars is not None else last_entry_index + 1
        value = Decimal(normalized[parameter.name])
        if minimum is not None and value < Decimal(minimum):
            raise ValueError(f"{parameter.name} is below resolved minimum")
        if maximum is not None and value > Decimal(maximum):
            raise ValueError(f"{parameter.name} exceeds dataset maximum {maximum}")
        resolved.append(ResolvedStrategyParameterV1(parameter, minimum, maximum))
    return tuple(resolved)
