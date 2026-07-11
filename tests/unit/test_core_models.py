from datetime import UTC, datetime

from ea.core.models import Bar, Instrument, OrderSide


def test_instrument_symbol_uses_exchange_namespace() -> None:
    instrument = Instrument(symbol="BTCUSDT", exchange="BINANCE", quote_currency="USDT")

    assert instrument.id == "BINANCE:BTCUSDT"


def test_bar_rejects_negative_volume() -> None:
    instrument = Instrument(symbol="BTCUSDT", exchange="BINANCE", quote_currency="USDT")

    try:
        Bar(
            instrument=instrument,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=-1.0,
        )
    except ValueError as exc:
        assert "volume" in str(exc)
    else:
        raise AssertionError("negative volume should fail")


def test_order_side_values_are_stable() -> None:
    assert OrderSide.BUY.value == "buy"
    assert OrderSide.SELL.value == "sell"
