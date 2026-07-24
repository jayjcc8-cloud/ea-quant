from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ea.core import (
    Adjustment,
    Bar,
    DataFingerprint,
    Instrument,
    MarketDataEnvelope,
    MarketDataKind,
    ReplayWindow,
    RunContractError,
    Sha256Digest,
    SourceId,
    VenueId,
)
from ea.data import (
    MarketDataSelection,
    select_and_fingerprint_market_data,
)
from ea.data.fingerprint import canonical_market_data_record_bytes

START = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
END = START + timedelta(minutes=1)
GOLDEN_RECORD = (
    b'{"adjustment":"raw","available_at":"2026-01-02T09:31:00.000000Z",'
    b'"close_bits":"4059200000000000","high_bits":"4059400000000000",'
    b'"interval_end":"2026-01-02T09:31:00.000000Z",'
    b'"interval_start":"2026-01-02T09:30:00.000000Z","kind":"bar",'
    b'"low_bits":"4058c00000000000","open_bits":"4059000000000000",'
    b'"revision":0,"source":"primary.raw","source_sequence":0,"symbol":"AAPL",'
    b'"venue":"XNAS","volume_bits":"4024000000000000"}'
)


def _event(
    *,
    available_at: datetime = END,
    revision: int = 0,
    source_sequence: int = 0,
    close: float = 100.5,
    volume: float = 10.0,
) -> MarketDataEnvelope:
    return MarketDataEnvelope(
        payload=Bar(
            instrument=Instrument(venue=VenueId("XNAS"), symbol="AAPL"),
            interval_start=START,
            interval_end=END,
            adjustment=Adjustment.RAW,
            open=100.0,
            high=101.0,
            low=99.0,
            close=close,
            volume=volume,
        ),
        source=SourceId("primary.raw"),
        available_at=available_at,
        source_sequence=source_sequence,
        revision=revision,
    )


def _window(
    *,
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    end: datetime = datetime(2026, 2, 1, tzinfo=UTC),
) -> ReplayWindow:
    return ReplayWindow(start_inclusive=start, end_exclusive=end)


def test_canonical_record_and_data_digest_match_literal_golden() -> None:
    event = _event()

    assert canonical_market_data_record_bytes(event) == GOLDEN_RECORD
    assert len(GOLDEN_RECORD) == 414

    prepared = select_and_fingerprint_market_data([event], _window())

    assert prepared.events == (event,)
    assert prepared.fingerprint.record_count == 1
    assert (
        prepared.fingerprint.sha256.value
        == "ef22439b2e2fa38e22cea9f544b0c29f1908827d62487815b486163c3c0a1624"
    )


def test_fixed_adjustment_and_kind_are_explicit_fingerprint_schema_fields() -> None:
    record = json.loads(canonical_market_data_record_bytes(_event()))

    assert record["adjustment"] == Adjustment.RAW.value == "raw"
    assert record["kind"] == MarketDataKind.BAR.value == "bar"
    assert set(record) == {
        "adjustment",
        "available_at",
        "close_bits",
        "high_bits",
        "interval_end",
        "interval_start",
        "kind",
        "low_bits",
        "open_bits",
        "revision",
        "source",
        "source_sequence",
        "symbol",
        "venue",
        "volume_bits",
    }


def test_selection_is_available_at_half_open_and_replays_exact_tuple() -> None:
    before = _event(available_at=END, source_sequence=0)
    at_end = _event(
        available_at=END + timedelta(minutes=1),
        revision=1,
        source_sequence=1,
        close=100.75,
    )
    window = _window(start=END, end=END + timedelta(minutes=1))

    prepared = select_and_fingerprint_market_data([at_end, before], window)

    assert prepared.events == (before,)


def test_fingerprint_is_permutation_invariant_after_admission_ordering() -> None:
    first = _event()
    correction = _event(
        available_at=END + timedelta(seconds=1),
        revision=1,
        source_sequence=1,
        close=100.75,
    )

    forward = select_and_fingerprint_market_data([first, correction], _window())
    reverse = select_and_fingerprint_market_data([correction, first], _window())

    assert forward == reverse
    assert forward.events == (first, correction)


def test_lineage_bearing_float_bits_change_fingerprint() -> None:
    original = select_and_fingerprint_market_data([_event(volume=0.0)], _window())
    negative_zero = select_and_fingerprint_market_data([_event(volume=-0.0)], _window())

    assert original.fingerprint != negative_zero.fingerprint


def test_candidate_iterable_is_materialized_exactly_once() -> None:
    class OneShot:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> Iterator[MarketDataEnvelope]:
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("candidate input was reopened")
            yield _event()

    candidate = OneShot()

    prepared = select_and_fingerprint_market_data(candidate, _window())

    assert candidate.iterations == 1
    assert len(prepared.events) == 1


def test_selection_recomputes_digest_and_binds_the_exact_window() -> None:
    event = _event()
    window = _window()

    with pytest.raises(RunContractError, match="does not match"):
        MarketDataSelection(
            window=window,
            events=(event,),
            fingerprint=DataFingerprint(Sha256Digest("0" * 64), 1),
        )
    with pytest.raises(RunContractError, match="outside its window"):
        MarketDataSelection(
            window=_window(
                start=END + timedelta(seconds=1),
                end=END + timedelta(seconds=2),
            ),
            events=(event,),
            fingerprint=select_and_fingerprint_market_data([event], window).fingerprint,
        )


def test_empty_knowledge_window_cannot_claim_phase1_backtest() -> None:
    event = _event()

    with pytest.raises(RunContractError, match="requires selected market data"):
        select_and_fingerprint_market_data(
            [event],
            _window(start=END + timedelta(seconds=1), end=END + timedelta(seconds=2)),
        )
