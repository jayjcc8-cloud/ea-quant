from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from ea.core import (
    Adjustment,
    AdmissionCursor,
    Bar,
    IdentityValidationError,
    Instrument,
    MarketDataEnvelope,
    MarketDataValidationError,
    SourceId,
    TimeValidationError,
    VenueId,
    admission_order_key,
    admit_market_data,
    is_visible_as_of,
    latest_as_of,
    order_market_data,
    require_utc,
    validate_market_data_batch,
)

START = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
END = START + timedelta(minutes=1)


def _instrument(*, venue: str = "XNAS", symbol: str = "AAPL") -> Instrument:
    return Instrument(venue=VenueId(venue), symbol=symbol)


def _bar(**overrides: object) -> Bar:
    values: dict[str, object] = {
        "instrument": _instrument(),
        "interval_start": START,
        "interval_end": END,
        "adjustment": Adjustment.RAW,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }
    values.update(overrides)
    return Bar(
        instrument=cast(Instrument, values["instrument"]),
        interval_start=cast(datetime, values["interval_start"]),
        interval_end=cast(datetime, values["interval_end"]),
        adjustment=cast(Adjustment, values["adjustment"]),
        open=cast(float, values["open"]),
        high=cast(float, values["high"]),
        low=cast(float, values["low"]),
        close=cast(float, values["close"]),
        volume=cast(float, values["volume"]),
    )


def _event(**overrides: object) -> MarketDataEnvelope:
    values: dict[str, object] = {
        "payload": _bar(),
        "source": SourceId("primary.raw"),
        "available_at": END,
        "source_sequence": 0,
        "revision": 0,
    }
    values.update(overrides)
    return MarketDataEnvelope(
        payload=cast(Bar, values["payload"]),
        source=cast(SourceId, values["source"]),
        available_at=cast(datetime, values["available_at"]),
        source_sequence=cast(int, values["source_sequence"]),
        revision=cast(int, values["revision"]),
    )


@pytest.mark.parametrize(
    "code",
    ["", " ", "xnas", "XN:AS", "XN AS", "XNAS\n", "交易所", "X" * 33],
)
def test_venue_rejects_noncanonical_tokens(code: str) -> None:
    with pytest.raises(IdentityValidationError, match="venue"):
        VenueId(code)


@pytest.mark.parametrize(
    "symbol",
    ["", " ", " AAPL", "AAPL ", "AA:PL", "AA PL", "AAPL\n", "苹果", "A" * 65],
)
def test_instrument_rejects_ambiguous_symbols(symbol: str) -> None:
    with pytest.raises(IdentityValidationError, match="symbol"):
        Instrument(venue=VenueId("XNAS"), symbol=symbol)


def test_instrument_identity_is_structured_case_preserving_and_hashable() -> None:
    upper = _instrument(symbol="ABC")
    lower = _instrument(symbol="abc")
    other_venue = _instrument(venue="XNYS", symbol="ABC")

    assert upper.key == ("XNAS", "ABC")
    assert upper.id == "XNAS:ABC"
    assert len({upper, lower, other_venue}) == 3
    assert hash(_instrument(symbol="ABC")) == hash(upper)
    with pytest.raises(FrozenInstanceError):
        upper.symbol = "CHANGED"  # type: ignore[misc]


def test_instrument_requires_a_venue_value_object() -> None:
    with pytest.raises(IdentityValidationError, match="VenueId"):
        Instrument(venue=cast(VenueId, "XNAS"), symbol="AAPL")


def test_require_utc_accepts_and_canonicalizes_zero_offset() -> None:
    named_zero_offset = timezone(timedelta(0), name="zero")
    value = datetime(2026, 1, 2, tzinfo=named_zero_offset)

    canonical = require_utc(value, field="value")

    assert canonical == value
    assert canonical.tzinfo is UTC


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 2),
        datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_require_utc_rejects_naive_and_nonzero_offsets(value: datetime) -> None:
    with pytest.raises(TimeValidationError, match="UTC"):
        require_utc(value, field="value")


def test_require_utc_rejects_non_datetime_values() -> None:
    with pytest.raises(TimeValidationError, match="datetime"):
        require_utc(cast(datetime, "2026-01-02T00:00:00Z"), field="value")


def test_require_utc_rejects_datetime_subclasses_from_adapter_libraries() -> None:
    class DerivedDateTime(datetime):
        pass

    value = DerivedDateTime(2026, 1, 2, tzinfo=UTC)

    with pytest.raises(TimeValidationError, match="datetime"):
        require_utc(value, field="value")


def test_bar_is_a_closed_half_open_interval() -> None:
    bar = _bar()

    assert bar.interval_start == START
    assert bar.interval_end == END
    assert bar.event_time == END


@pytest.mark.parametrize(
    ("interval_start", "interval_end"),
    [(START, START), (END, START)],
)
def test_bar_rejects_empty_or_reversed_intervals(
    interval_start: datetime, interval_end: datetime
) -> None:
    with pytest.raises(MarketDataValidationError, match="interval_start"):
        _bar(interval_start=interval_start, interval_end=interval_end)


@pytest.mark.parametrize(
    "field",
    ["interval_start", "interval_end"],
)
@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 2, 9, 30),
        datetime(2026, 1, 2, 9, 30, tzinfo=timezone(timedelta(hours=-5))),
    ],
)
def test_bar_rejects_noncanonical_times(field: str, value: datetime) -> None:
    with pytest.raises(TimeValidationError, match="UTC"):
        _bar(**{field: value})


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), True, 1, "1.0", None],
    ids=["nan", "positive-infinity", "negative-infinity", "bool", "int", "str", "none"],
)
def test_bar_rejects_noncanonical_numeric_values(field: str, value: object) -> None:
    with pytest.raises(MarketDataValidationError, match=field):
        _bar(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"open": 98.0},
        {"open": 102.0},
        {"close": 98.0},
        {"close": 102.0},
        {"low": 102.0},
        {"high": 98.0},
        {"volume": -0.1},
    ],
)
def test_bar_rejects_invalid_ohlcv_envelopes(overrides: dict[str, float]) -> None:
    with pytest.raises(MarketDataValidationError):
        _bar(**overrides)


def test_bar_allows_finite_negative_prices_equal_prices_and_zero_volume() -> None:
    negative = _bar(open=-2.0, high=-1.0, low=-3.0, close=-1.5, volume=0.0)
    equal = _bar(open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0)

    assert negative.low < negative.close < negative.high
    assert equal.open == equal.high == equal.low == equal.close == equal.volume == 0.0


def test_adjustment_vocabulary_is_exhaustively_phase1_raw_only() -> None:
    assert tuple(Adjustment) == (Adjustment.RAW,)
    assert Adjustment.RAW.value == "raw"


def test_bar_requires_explicit_supported_adjustment() -> None:
    with pytest.raises(MarketDataValidationError, match="Adjustment"):
        _bar(adjustment=cast(Adjustment, "adjusted"))


@pytest.mark.parametrize(
    "code",
    ["", "Primary", "PRIMARY", "primary/raw", "primary raw", "primary:raw", "源", "s" * 65],
)
def test_source_rejects_noncanonical_tokens(code: str) -> None:
    with pytest.raises(MarketDataValidationError, match="source"):
        SourceId(code)


@pytest.mark.parametrize("field", ["source_sequence", "revision"])
@pytest.mark.parametrize("value", [-1, True, 1.0])
def test_envelope_rejects_invalid_integer_fields(field: str, value: object) -> None:
    with pytest.raises(MarketDataValidationError, match=field):
        _event(**{field: value})


def test_envelope_rejects_availability_before_event_time() -> None:
    with pytest.raises(MarketDataValidationError, match="available_at"):
        _event(available_at=END - timedelta(microseconds=1))


def test_envelope_requires_canonical_payload_and_source_types() -> None:
    with pytest.raises(MarketDataValidationError, match="Bar"):
        _event(payload=cast(Bar, "bar"))
    with pytest.raises(MarketDataValidationError, match="SourceId"):
        _event(source=cast(SourceId, "primary.raw"))


def test_envelope_is_immutable_hashable_and_exposes_structured_identities() -> None:
    event = _event()

    assert event.logical_key[-1] == "primary.raw"
    assert event.record_key == (event.logical_key, 0)
    assert event.emission_key == ("primary.raw", 0)
    assert hash(_event()) == hash(event)
    assert len({event, _event()}) == 1
    with pytest.raises(FrozenInstanceError):
        event.revision = 2  # type: ignore[misc]


def test_batch_rejects_exact_duplicate_record_versions() -> None:
    event = _event()

    with pytest.raises(MarketDataValidationError, match="duplicate record version"):
        validate_market_data_batch([event, event])


def test_batch_rejects_conflicting_record_versions() -> None:
    first = _event(source_sequence=0)
    conflicting = _event(source_sequence=1, payload=_bar(close=100.75))

    with pytest.raises(MarketDataValidationError, match="conflicting record version"):
        validate_market_data_batch([first, conflicting])


def test_batch_rejects_source_sequence_collisions_across_records() -> None:
    first = _event(source_sequence=5)
    second = _event(
        source_sequence=5,
        payload=_bar(
            interval_start=END,
            interval_end=END + timedelta(minutes=1),
        ),
        available_at=END + timedelta(minutes=1),
    )

    with pytest.raises(MarketDataValidationError, match="source emission"):
        validate_market_data_batch([first, second])


def test_batch_rejects_revision_availability_regression() -> None:
    initial = _event(available_at=END + timedelta(minutes=2), source_sequence=1, revision=0)
    correction = _event(
        available_at=END + timedelta(minutes=1),
        source_sequence=2,
        revision=1,
        payload=_bar(close=100.75),
    )

    with pytest.raises(MarketDataValidationError, match="availability regressed"):
        validate_market_data_batch([initial, correction])


def test_batch_rejects_revision_source_sequence_regression() -> None:
    initial = _event(source_sequence=2, revision=0)
    correction = _event(
        available_at=END + timedelta(minutes=1),
        source_sequence=1,
        revision=1,
        payload=_bar(close=100.75),
    )

    with pytest.raises(MarketDataValidationError, match="source_sequence"):
        validate_market_data_batch([initial, correction])


def test_admission_order_uses_stable_source_sequence_and_business_fields() -> None:
    available_at = END + timedelta(minutes=5)
    later_sequence = _event(
        source=SourceId("alpha"),
        source_sequence=2,
        available_at=available_at,
    )
    earlier_sequence = _event(
        source=SourceId("alpha"),
        source_sequence=1,
        available_at=available_at,
        payload=_bar(instrument=_instrument(symbol="MSFT")),
    )
    other_source = _event(
        source=SourceId("beta"),
        source_sequence=0,
        available_at=available_at,
    )

    ordered = order_market_data([other_source, later_sequence, earlier_sequence])

    assert ordered == (earlier_sequence, later_sequence, other_source)
    assert admission_order_key(earlier_sequence) < admission_order_key(later_sequence)
    assert order_market_data(ordered) == ordered


def test_visibility_boundary_is_inclusive_and_uses_knowledge_time() -> None:
    late = _event(available_at=END + timedelta(minutes=5))

    assert not is_visible_as_of(late, END + timedelta(minutes=5, microseconds=-1))
    assert is_visible_as_of(late, END + timedelta(minutes=5))
    assert is_visible_as_of(late, END + timedelta(minutes=6))


def test_latest_as_of_selects_only_the_highest_visible_revision() -> None:
    initial = _event(source_sequence=10, revision=0)
    correction_time = END + timedelta(minutes=5)
    correction = _event(
        payload=_bar(close=100.75),
        available_at=correction_time,
        source_sequence=11,
        revision=2,
    )

    before = latest_as_of([correction, initial], correction_time - timedelta(microseconds=1))
    at = latest_as_of([initial, correction], correction_time)

    assert before == (initial,)
    assert at == (correction,)
    assert latest_as_of([initial], correction_time - timedelta(microseconds=1)) == before


@dataclass
class FakeClock:
    current: datetime
    reads: int = 0

    def now(self) -> datetime:
        self.reads += 1
        return self.current


def test_clock_admission_reads_once_and_never_returns_future_payloads() -> None:
    current = _event(source_sequence=0)
    future = _event(
        payload=_bar(
            interval_start=END,
            interval_end=END + timedelta(minutes=1),
        ),
        available_at=END + timedelta(minutes=10),
        source_sequence=1,
    )
    clock = FakeClock(END)

    cursor, admitted = admit_market_data([future, current], clock=clock)

    assert cursor.as_of == END
    assert cursor.last_event == current
    assert admitted == (current,)
    assert future not in admitted
    assert clock.reads == 1


def test_clock_admission_resumes_from_a_total_order_cursor() -> None:
    first = _event(source_sequence=0)
    second = _event(
        payload=_bar(instrument=_instrument(symbol="MSFT")),
        available_at=END,
        source_sequence=1,
    )
    clock = FakeClock(END)

    history = [second, first]
    first_cursor, first_batch = admit_market_data(history, clock=clock, limit=1)
    second_cursor, second_batch = admit_market_data(
        history, clock=clock, cursor=first_cursor, limit=1
    )

    assert first_batch == (first,)
    assert second_batch == (second,)
    assert second_cursor.as_of == END
    assert second_cursor.last_event == second


def test_clock_admission_rejects_delta_history_that_omits_the_cursor_event() -> None:
    first = _event(source_sequence=0)
    second = _event(
        payload=_bar(instrument=_instrument(symbol="MSFT")),
        available_at=END + timedelta(minutes=1),
        source_sequence=1,
    )
    cursor, _ = admit_market_data([first], clock=FakeClock(END))

    with pytest.raises(MarketDataValidationError, match="complete and cumulative"):
        admit_market_data(
            [second],
            clock=FakeClock(END + timedelta(minutes=1)),
            cursor=cursor,
        )


@pytest.mark.parametrize("limit", [0, -1, True, 1.0])
def test_clock_admission_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(MarketDataValidationError, match="limit"):
        admit_market_data([_event()], clock=FakeClock(END), limit=cast(int, limit))


def test_admission_cursor_rejects_a_future_last_event() -> None:
    future = _event(available_at=END + timedelta(minutes=1))

    with pytest.raises(MarketDataValidationError, match="visible"):
        AdmissionCursor(as_of=END, last_event=future)


def test_clock_admission_rejects_clock_regression() -> None:
    cursor, _ = admit_market_data(
        [_event()],
        clock=FakeClock(END + timedelta(microseconds=1)),
    )

    with pytest.raises(MarketDataValidationError, match="regressed"):
        admit_market_data(
            [_event()],
            clock=FakeClock(END),
            cursor=cursor,
        )
