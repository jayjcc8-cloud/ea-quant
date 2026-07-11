from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str
    quote_currency: str

    @property
    def id(self) -> str:
        return f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class Bar:
    instrument: Instrument
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.low > self.high:
            raise ValueError("low must be less than or equal to high")
        for name in ("open", "high", "low", "close"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
