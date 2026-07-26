"""Compatibility exports plus trading primitives not yet split into domain modules."""

from ea.core.execution_messages import OrderSide
from ea.core.identity import Instrument
from ea.core.market_data import Bar

__all__ = ["Bar", "Instrument", "OrderSide"]
