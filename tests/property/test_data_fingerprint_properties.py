from __future__ import annotations

import math
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ea.core import (
    Adjustment,
    Bar,
    Instrument,
    MarketDataEnvelope,
    ReplayWindow,
    SourceId,
    VenueId,
)
from ea.data import select_and_fingerprint_market_data

START = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
END = START + timedelta(minutes=1)
AVAILABLE = END + timedelta(hours=1)
WINDOW = ReplayWindow(
    datetime(2026, 1, 1, tzinfo=UTC),
    datetime(2026, 2, 1, tzinfo=UTC),
)
_MAX_FINITE = float.fromhex("0x1.fffffffffffffp+1023")


def _event() -> MarketDataEnvelope:
    return MarketDataEnvelope(
        payload=Bar(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            interval_start=START,
            interval_end=END,
            adjustment=Adjustment.RAW,
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=10.0,
        ),
        source=SourceId("primary.raw"),
        available_at=AVAILABLE,
        source_sequence=0,
        revision=0,
    )


def _digest(event: MarketDataEnvelope) -> str:
    return select_and_fingerprint_market_data([event], WINDOW).fingerprint.sha256.value


def _event_with_float_pattern(field: str, value: float) -> MarketDataEnvelope:
    event = _event()
    if field == "high":
        payload = replace(
            event.payload,
            open=-_MAX_FINITE,
            high=value,
            low=-_MAX_FINITE,
            close=-_MAX_FINITE,
            volume=0.0,
        )
    elif field == "low":
        payload = replace(
            event.payload,
            open=_MAX_FINITE,
            high=_MAX_FINITE,
            low=value,
            close=_MAX_FINITE,
            volume=0.0,
        )
    elif field == "open":
        payload = replace(
            event.payload,
            open=value,
            high=_MAX_FINITE,
            low=-_MAX_FINITE,
            close=0.0,
            volume=0.0,
        )
    elif field == "close":
        payload = replace(
            event.payload,
            open=0.0,
            high=_MAX_FINITE,
            low=-_MAX_FINITE,
            close=value,
            volume=0.0,
        )
    else:
        payload = replace(
            event.payload,
            open=0.0,
            high=0.0,
            low=0.0,
            close=0.0,
            volume=value,
        )
    return replace(event, payload=payload)


def test_every_mutable_market_data_field_changes_the_fingerprint() -> None:
    baseline = _event()
    payload = baseline.payload
    envelope_variants = [
        replace(baseline, source=SourceId("secondary.raw")),
        replace(baseline, source_sequence=1),
        replace(baseline, revision=1),
        replace(baseline, available_at=AVAILABLE + timedelta(microseconds=1)),
    ]
    payload_variants = [
        replace(payload, interval_start=START - timedelta(minutes=1)),
        replace(payload, interval_end=END + timedelta(minutes=1)),
        replace(
            payload,
            instrument=Instrument(VenueId("XNYS"), payload.instrument.symbol),
        ),
        replace(
            payload,
            instrument=Instrument(payload.instrument.venue, "MSFT"),
        ),
        replace(payload, open=101.0),
        replace(payload, high=111.0),
        replace(payload, low=89.0),
        replace(payload, close=104.0),
        replace(payload, volume=11.0),
    ]
    envelope_variants.extend(replace(baseline, payload=variant) for variant in payload_variants)

    baseline_digest = _digest(baseline)
    assert all(_digest(variant) != baseline_digest for variant in envelope_variants)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_each_ohlcv_binary64_sign_bit_is_lineage_bearing(field: str) -> None:
    baseline = _event()
    zero_payload = replace(
        baseline.payload,
        open=0.0,
        high=0.0,
        low=0.0,
        close=0.0,
        volume=0.0,
    )
    positive = replace(baseline, payload=zero_payload)
    if field == "open":
        negative_payload = replace(zero_payload, open=-0.0)
    elif field == "high":
        negative_payload = replace(zero_payload, high=-0.0)
    elif field == "low":
        negative_payload = replace(zero_payload, low=-0.0)
    elif field == "close":
        negative_payload = replace(zero_payload, close=-0.0)
    else:
        negative_payload = replace(zero_payload, volume=-0.0)
    negative = replace(
        baseline,
        payload=negative_payload,
    )

    assert _digest(positive) != _digest(negative)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@given(bits=st.integers(min_value=1, max_value=(1 << 64) - 1))
def test_each_accepted_finite_binary64_pattern_changes_fingerprint(
    field: str,
    bits: int,
) -> None:
    value = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
    assume(math.isfinite(value))
    assume(field != "volume" or value >= 0.0)

    baseline = _digest(_event_with_float_pattern(field, 0.0))
    mutated = _digest(_event_with_float_pattern(field, value))

    assert mutated != baseline


def _permutation_events() -> tuple[MarketDataEnvelope, ...]:
    baseline = _event()
    return (
        baseline,
        replace(
            baseline,
            payload=replace(
                baseline.payload,
                instrument=Instrument(VenueId("XNAS"), "MSFT"),
            ),
            source_sequence=1,
        ),
        replace(
            baseline,
            payload=replace(
                baseline.payload,
                instrument=Instrument(VenueId("XNYS"), "IBM"),
            ),
            source_sequence=2,
        ),
    )


@given(permutation=st.permutations(_permutation_events()))
def test_fingerprint_is_property_invariant_to_candidate_permutation(
    permutation: list[MarketDataEnvelope],
) -> None:
    expected = select_and_fingerprint_market_data(_permutation_events(), WINDOW)
    actual = select_and_fingerprint_market_data(permutation, WINDOW)

    assert actual == expected
