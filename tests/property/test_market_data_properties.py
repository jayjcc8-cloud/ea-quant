from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ea.core import (
    Adjustment,
    Bar,
    Instrument,
    MarketDataEnvelope,
    MarketDataValidationError,
    SourceId,
    VenueId,
    admit_market_data,
    latest_as_of,
    order_market_data,
)

START = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
END = START + timedelta(minutes=1)
INSTRUMENT = Instrument(venue=VenueId("XNAS"), symbol="AAPL")


def _bar(
    *,
    interval_start: datetime = START,
    interval_end: datetime = END,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 10.0,
    instrument: Instrument = INSTRUMENT,
) -> Bar:
    return Bar(
        instrument=instrument,
        interval_start=interval_start,
        interval_end=interval_end,
        adjustment=Adjustment.RAW,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _event(
    *,
    payload: Bar,
    source: str,
    source_sequence: int,
    available_at: datetime,
    revision: int = 0,
) -> MarketDataEnvelope:
    return MarketDataEnvelope(
        payload=payload,
        source=SourceId(source),
        available_at=available_at,
        source_sequence=source_sequence,
        revision=revision,
    )


FINITE_PRICE = st.floats(
    min_value=-1_000_000.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)
FINITE_VOLUME = st.floats(
    min_value=0.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)


@given(first=FINITE_PRICE, second=FINITE_PRICE, volume=FINITE_VOLUME)
def test_all_generated_finite_ohlcv_envelopes_construct(
    first: float, second: float, volume: float
) -> None:
    low = min(first, second)
    high = max(first, second)

    bar = _bar(open_=first, high=high, low=low, close=second, volume=volume)

    assert bar.low <= bar.open <= bar.high
    assert bar.low <= bar.close <= bar.high
    assert bar.volume >= 0.0


@given(
    high=st.floats(
        min_value=-1_000_000.0,
        max_value=1_000_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    delta=st.floats(
        min_value=0.000001,
        max_value=1_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_generated_open_above_high_always_fails(high: float, delta: float) -> None:
    with pytest.raises(MarketDataValidationError):
        _bar(open_=high + delta, high=high, low=high - delta, close=high)


def _ordered_examples() -> tuple[MarketDataEnvelope, ...]:
    available_at = END + timedelta(minutes=10)
    return (
        _event(
            payload=_bar(instrument=Instrument(VenueId("XNAS"), "AAPL")),
            source="alpha",
            source_sequence=2,
            available_at=available_at,
        ),
        _event(
            payload=_bar(instrument=Instrument(VenueId("XNAS"), "MSFT")),
            source="alpha",
            source_sequence=1,
            available_at=available_at,
        ),
        _event(
            payload=_bar(instrument=Instrument(VenueId("XNYS"), "IBM")),
            source="beta",
            source_sequence=0,
            available_at=available_at,
        ),
        _event(
            payload=_bar(
                interval_start=END,
                interval_end=END + timedelta(minutes=1),
                instrument=Instrument(VenueId("XNAS"), "AAPL"),
            ),
            source="alpha",
            source_sequence=3,
            available_at=available_at + timedelta(minutes=1),
        ),
    )


@given(permutation=st.permutations(_ordered_examples()))
def test_order_is_permutation_invariant_and_idempotent(
    permutation: list[MarketDataEnvelope],
) -> None:
    expected = order_market_data(_ordered_examples())

    actual = order_market_data(permutation)

    assert actual == expected
    assert order_market_data(actual) == expected


@dataclass
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


def _visibility_examples() -> tuple[MarketDataEnvelope, ...]:
    return tuple(
        _event(
            payload=_bar(
                interval_start=START + timedelta(minutes=index),
                interval_end=END + timedelta(minutes=index),
            ),
            source="history",
            source_sequence=index,
            available_at=END + timedelta(minutes=index),
        )
        for index in range(5)
    )


@given(first=st.integers(min_value=-1, max_value=6), second=st.integers(min_value=-1, max_value=6))
def test_visible_history_is_monotonic_and_contains_no_future_events(
    first: int, second: int
) -> None:
    earlier, later = sorted((first, second))
    events = _visibility_examples()
    earlier_cursor, earlier_events = admit_market_data(
        events,
        clock=FakeClock(END + timedelta(minutes=earlier)),
    )
    later_cursor, later_events = admit_market_data(
        events,
        clock=FakeClock(END + timedelta(minutes=later)),
    )

    assert set(earlier_events).issubset(later_events)
    assert all(event.event_time <= earlier_cursor.as_of for event in earlier_events)
    assert all(event.available_at <= earlier_cursor.as_of for event in earlier_events)
    assert all(event.event_time <= later_cursor.as_of for event in later_events)
    assert all(event.available_at <= later_cursor.as_of for event in later_events)


@given(
    delay_minutes=st.integers(min_value=1, max_value=10_000),
    corrected_close=st.floats(
        min_value=99.0,
        max_value=101.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_future_revision_never_changes_an_earlier_snapshot(
    delay_minutes: int, corrected_close: float
) -> None:
    initial = _event(
        payload=_bar(),
        source="history",
        source_sequence=0,
        available_at=END,
        revision=0,
    )
    correction_time = END + timedelta(minutes=delay_minutes)
    correction = _event(
        payload=_bar(close=corrected_close),
        source="history",
        source_sequence=1,
        available_at=correction_time,
        revision=1,
    )
    cutoff = correction_time - timedelta(microseconds=1)

    without_future = latest_as_of([initial], cutoff)
    with_future = latest_as_of([correction, initial], cutoff)

    assert with_future == without_future == (initial,)
