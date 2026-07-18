"""Compatibility exports plus trading primitives not yet split into domain modules."""

from enum import StrEnum

from ea.core.identity import Instrument
from ea.core.market_data import Bar

__all__ = ["Bar", "Instrument", "OrderSide"]


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"
