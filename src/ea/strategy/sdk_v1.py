"""Public trusted-local strategy SDK. No execution or portfolio authority is exposed."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

ParameterValue = int | str


@dataclass(frozen=True, slots=True)
class StrategyBarV1:
    event_time: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class StrategyDecisionV1:
    target_quantity: str


@dataclass(frozen=True, slots=True)
class StrategyValidationContextV1:
    history_bar_count: int
    last_entry_index: int | None
    quantity_quantum: str


class StrategyLogicV1(Protocol):
    def on_bar(self, bar: StrategyBarV1) -> StrategyDecisionV1 | None: ...


class StrategyFactoryV1(Protocol):
    def __call__(self, parameters: Mapping[str, ParameterValue]) -> StrategyLogicV1: ...
