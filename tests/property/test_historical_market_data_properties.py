from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from hypothesis import given
from hypothesis import strategies as st

from ea.core import MarketDataEnvelope, ReplayWindow
from ea.data import (
    create_phase1_historical_market_data_source,
    decode_phase1_ohlcv_csv,
)

HEADER = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
ROWS = (
    (
        "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,"
        "2026-01-02T09:31:00.000000Z,raw,100.0,101.0,99.0,100.5,10.0,"
        "property.raw,0,0,2026-01-02T09:31:00.000000Z"
    ),
    (
        "1,XNAS,MSFT,2026-01-02T09:30:00.000000Z,"
        "2026-01-02T09:31:00.000000Z,raw,200.0,202.0,199.0,201.0,20.0,"
        "property.raw,1,0,2026-01-02T09:31:00.000000Z"
    ),
    (
        "1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,"
        "2026-01-02T09:32:00.000000Z,raw,100.5,102.0,100.0,101.5,12.0,"
        "property.raw,2,0,2026-01-02T09:32:00.000000Z"
    ),
)
WINDOW = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
)
CUTOFF = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)


def _content(rows: list[str] | tuple[str, ...], *, newline: str, quoted: bool) -> bytes:
    if quoted:
        rows = [
            row.replace("XNAS", '"XNAS"').replace("property.raw", '"property.raw"') for row in rows
        ]
    return (newline.join((HEADER, *rows)) + newline).encode()


class _Clock:
    def now(self) -> datetime:
        return CUTOFF


@given(
    permutation=st.permutations(ROWS),
    newline=st.sampled_from(("\n", "\r\n")),
    quoted=st.booleans(),
)
def test_decode_is_invariant_to_row_order_newline_and_equivalent_quoting(
    permutation: list[str],
    newline: str,
    quoted: bool,
) -> None:
    expected = decode_phase1_ohlcv_csv(
        _content(ROWS, newline="\n", quoted=False),
        replay_window=WINDOW,
    )

    actual = decode_phase1_ohlcv_csv(
        _content(permutation, newline=newline, quoted=quoted),
        replay_window=WINDOW,
    )

    assert actual.selection == expected.selection


@given(limit=st.integers(min_value=1, max_value=8))
def test_arbitrary_positive_limit_drains_exact_canonical_history(limit: int) -> None:
    dataset = decode_phase1_ohlcv_csv(
        _content(ROWS, newline="\n", quoted=False),
        replay_window=WINDOW,
    )
    source = create_phase1_historical_market_data_source(dataset)
    cursor = None
    admitted: list[MarketDataEnvelope] = []

    while source.next_available_at(cursor) is not None:
        candidate, batch = source.admit(
            clock=cast(Any, _Clock()),
            cursor=cursor,
            limit=limit,
        )
        assert candidate.as_of == CUTOFF
        admitted.extend(batch)
        cursor = candidate

    assert tuple(admitted) == dataset.selection.events
