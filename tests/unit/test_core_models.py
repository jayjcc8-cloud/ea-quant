from ea.core import Bar as PublicBar
from ea.core import Instrument as PublicInstrument
from ea.core.models import Bar, Instrument, OrderSide


def test_models_module_reexports_the_single_canonical_schema() -> None:
    assert Instrument is PublicInstrument
    assert Bar is PublicBar


def test_order_side_values_are_stable() -> None:
    assert OrderSide.BUY.value == "buy"
    assert OrderSide.SELL.value == "sell"
