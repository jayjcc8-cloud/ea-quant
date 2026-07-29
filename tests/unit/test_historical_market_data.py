from __future__ import annotations

import os
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest

from ea.core import (
    AdmissionCursor,
    MarketDataEnvelope,
    ReplayWindow,
    Sha256Digest,
)
from ea.data import (
    PHASE1_OHLCV_PROFILE,
    HistoricalMarketDataError,
    HistoricalMarketDataFailureCode,
    Phase1HistoricalDataset,
    Phase1HistoricalMarketDataSource,
    Phase1HistoricalSourceCursor,
    create_phase1_historical_market_data_source,
    decode_phase1_ohlcv_csv,
    read_phase1_ohlcv_csv,
)
from ea.data import historical as historical_module
from ea.data.fingerprint import canonical_market_data_record_bytes

HEADER = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
WINDOW = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
)
GOLDEN_FIXTURE = Path(__file__).parents[1] / "fixtures" / "phase1_ohlcv_v1.csv"


def _row(
    *,
    start: str = "2026-01-02T09:30:00.000000Z",
    end: str = "2026-01-02T09:31:00.000000Z",
    available: str = "2026-01-02T09:31:00.000000Z",
    sequence: str = "0",
    revision: str = "0",
    venue: str = "XNAS",
    symbol: str = "AAPL",
    adjustment: str = "raw",
    open_: str = "100.0",
    high: str = "101.0",
    low: str = "99.0",
    close: str = "100.5",
    volume: str = "10.0",
    source: str = "fixture.raw",
) -> str:
    return ",".join(
        (
            "1",
            venue,
            symbol,
            start,
            end,
            adjustment,
            open_,
            high,
            low,
            close,
            volume,
            source,
            sequence,
            revision,
            available,
        )
    )


def _content(*rows: str, newline: str = "\n") -> bytes:
    return (newline.join((HEADER, *rows)) + newline).encode()


def _replace_field(row: str, field: str, value: str) -> str:
    fields = row.split(",")
    fields[HEADER.split(",").index(field)] = value
    return ",".join(fields)


def _assert_failure(
    content: object,
    code: HistoricalMarketDataFailureCode,
    *,
    record_number: int | None = None,
    field_name: str | None = None,
) -> None:
    with pytest.raises(HistoricalMarketDataError) as caught:
        decode_phase1_ohlcv_csv(cast(bytes, content), replay_window=WINDOW)
    assert caught.value.code is code
    assert caught.value.record_number == record_number
    assert caught.value.field_name == field_name


def _forged_cursor(
    dataset: Phase1HistoricalDataset,
    admission: object,
) -> Phase1HistoricalSourceCursor:
    cursor = object.__new__(Phase1HistoricalSourceCursor)
    object.__setattr__(cursor, "_replay_window", dataset.replay_window)
    object.__setattr__(cursor, "_data_sha256", dataset.selection.fingerprint.sha256)
    object.__setattr__(cursor, "_record_count", dataset.selection.fingerprint.record_count)
    object.__setattr__(cursor, "_admission_cursor", admission)
    return cursor


def _raw_admission(*, as_of: object, last_event: object) -> AdmissionCursor:
    admission = object.__new__(AdmissionCursor)
    object.__setattr__(admission, "as_of", as_of)
    object.__setattr__(admission, "last_event", last_event)
    return admission


class _Clock:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    def now(self) -> Any:
        self.calls += 1
        return self.value


def test_decoder_issues_exact_dataset_and_canonical_order() -> None:
    later = _row(
        start="2026-01-02T09:31:00.000000Z",
        end="2026-01-02T09:32:00.000000Z",
        available="2026-01-02T09:32:00.000000Z",
        sequence="1",
    )
    earlier = _row()
    content = _content(later, earlier)

    dataset = decode_phase1_ohlcv_csv(content, replay_window=WINDOW)

    assert dataset.profile == PHASE1_OHLCV_PROFILE
    assert dataset.replay_window is WINDOW
    assert dataset.source_byte_count == len(content)
    assert dataset.source_bytes_sha256 == Sha256Digest(sha256(content).hexdigest())
    assert tuple(event.source_sequence for event in dataset.selection.events) == (0, 1)
    assert dataset.selection.fingerprint.record_count == 2


def test_golden_fixture_binds_raw_and_semantic_evidence() -> None:
    content = GOLDEN_FIXTURE.read_bytes()

    dataset = decode_phase1_ohlcv_csv(content, replay_window=WINDOW)

    assert (
        dataset.source_bytes_sha256.value
        == "397e299212ae7aac4989a78d8bcdc19951cd8493b0f53b14bad0a1c63e5e90e9"
    )
    assert (
        dataset.selection.fingerprint.sha256.value
        == "ccf5c736cb452545e79d20f06c8e2572d2b1a39a8967ab078639dfbbd4d754b4"
    )
    assert tuple(
        (
            event.payload.interval_start.isoformat(),
            event.payload.close,
            event.source_sequence,
            event.revision,
            event.available_at.isoformat(),
        )
        for event in dataset.selection.events
    ) == (
        ("2026-01-02T09:30:00+00:00", 100.5, 0, 0, "2026-01-02T09:31:00+00:00"),
        ("2026-01-02T09:30:00+00:00", 101.0, 1, 1, "2026-01-02T09:31:30+00:00"),
        ("2026-01-02T09:31:00+00:00", 101.5, 2, 0, "2026-01-02T09:32:00+00:00"),
    )


def test_csv_spelling_changes_raw_digest_but_not_semantic_selection() -> None:
    row = _row()
    lf = _content(row)
    quoted_crlf = _content(row.replace("XNAS", '"XNAS"'), newline="\r\n")

    first = decode_phase1_ohlcv_csv(lf, replay_window=WINDOW)
    second = decode_phase1_ohlcv_csv(quoted_crlf, replay_window=WINDOW)

    assert first.source_bytes_sha256 != second.source_bytes_sha256
    assert first.selection == second.selection


def test_factory_only_values_and_source_surface_are_immutable() -> None:
    dataset = decode_phase1_ohlcv_csv(_content(_row()), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)

    with pytest.raises(TypeError):
        Phase1HistoricalDataset()
    with pytest.raises(TypeError):
        Phase1HistoricalMarketDataSource()
    with pytest.raises(TypeError):
        Phase1HistoricalSourceCursor()
    with pytest.raises(FrozenInstanceError):
        cast(Any, source)._events = ()
    assert source.replay_window == WINDOW
    assert source.fingerprint == dataset.selection.fingerprint
    cursor, _events = source.admit(
        clock=cast(Any, _Clock(datetime(2026, 1, 2, 9, 31, tzinfo=UTC))),
        cursor=None,
    )
    assert cursor.replay_window == WINDOW
    assert cursor.data_sha256 == dataset.selection.fingerprint.sha256
    assert cursor.record_count == 1
    assert "MarketDataEnvelope" not in repr(source)
    assert "AAPL" not in repr(source)
    for forbidden in ("events", "history", "path", "peek", "seek", "reset", "__iter__"):
        assert not hasattr(source, forbidden)


@pytest.mark.parametrize(
    ("content", "code", "record", "field"),
    [
        (cast(Any, bytearray(b"x")), HistoricalMarketDataFailureCode.INVALID_TYPE, None, None),
        (b"", HistoricalMarketDataFailureCode.EMPTY_SOURCE, None, None),
        (b"\xff", HistoricalMarketDataFailureCode.INVALID_ENCODING, None, None),
        (
            b"\xef\xbb\xbf" + _content(_row()),
            HistoricalMarketDataFailureCode.INVALID_ENCODING,
            None,
            None,
        ),
        (
            _content(_row()).replace(b"XNAS", b"XN\x00AS"),
            HistoricalMarketDataFailureCode.INVALID_FILE_CHARACTER,
            None,
            None,
        ),
        (
            _content(_row()).replace(b"\n", b"\r", 1),
            HistoricalMarketDataFailureCode.INVALID_FILE_CHARACTER,
            None,
            None,
        ),
        (
            b"wrong,header\n" + _row().encode() + b"\n",
            HistoricalMarketDataFailureCode.INVALID_HEADER,
            1,
            None,
        ),
        (
            (HEADER + "\n").encode(),
            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
            2,
            None,
        ),
        (
            (HEADER + "\n\n").encode(),
            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
            2,
            None,
        ),
        (
            (HEADER + "\n# comment\n").encode(),
            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
            2,
            None,
        ),
        (
            _content(_row(source="源")),
            HistoricalMarketDataFailureCode.INVALID_FIELD_CHARACTER,
            2,
            "source",
        ),
        (
            _content(_replace_field(_row(), "schema_version", "2")),
            HistoricalMarketDataFailureCode.INVALID_SCHEMA_VERSION,
            2,
            "schema_version",
        ),
        (
            _content(_row(venue="bad")),
            HistoricalMarketDataFailureCode.INVALID_IDENTITY,
            2,
            "venue",
        ),
        (
            _content(_row(start="2026-01-02T09:30:00Z")),
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            2,
            "interval_start",
        ),
        (
            _content(_row(adjustment="adjusted")),
            HistoricalMarketDataFailureCode.INVALID_ADJUSTMENT,
            2,
            "adjustment",
        ),
        (
            _content(_row(open_="NaN")),
            HistoricalMarketDataFailureCode.INVALID_FLOAT,
            2,
            "open",
        ),
        (
            _content(_row(high="98.0")),
            HistoricalMarketDataFailureCode.INVALID_BAR,
            2,
            "high",
        ),
        (
            _content(_row(sequence="01")),
            HistoricalMarketDataFailureCode.INVALID_INTEGER,
            2,
            "source_sequence",
        ),
        (
            _content(_row(available="2026-01-02T09:30:59.999999Z")),
            HistoricalMarketDataFailureCode.INVALID_ENVELOPE,
            2,
            "available_at",
        ),
    ],
)
def test_decoder_failure_matrix(
    content: object,
    code: HistoricalMarketDataFailureCode,
    record: int | None,
    field: str | None,
) -> None:
    _assert_failure(content, code, record_number=record, field_name=field)


@pytest.mark.parametrize(
    ("field", "value", "code", "evidence_field"),
    [
        ("symbol", "", HistoricalMarketDataFailureCode.INVALID_IDENTITY, "symbol"),
        (
            "interval_start",
            "2026-02-30T09:30:00.000000Z",
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            "interval_start",
        ),
        (
            "interval_end",
            "2026-01-02T09:31:00.00000Z",
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            "interval_end",
        ),
        ("open", "+100.0", HistoricalMarketDataFailureCode.INVALID_FLOAT, "open"),
        ("high", "1e999", HistoricalMarketDataFailureCode.INVALID_FLOAT, "high"),
        ("low", "01.0", HistoricalMarketDataFailureCode.INVALID_FLOAT, "low"),
        ("close", "100.", HistoricalMarketDataFailureCode.INVALID_FLOAT, "close"),
        ("volume", "-1.0", HistoricalMarketDataFailureCode.INVALID_BAR, "volume"),
        ("source", "BAD", HistoricalMarketDataFailureCode.INVALID_IDENTITY, "source"),
        (
            "source_sequence",
            "18446744073709551616",
            HistoricalMarketDataFailureCode.INVALID_INTEGER,
            "source_sequence",
        ),
        ("revision", "-1", HistoricalMarketDataFailureCode.INVALID_INTEGER, "revision"),
        (
            "available_at",
            "2026-01-02T09:31:00.000000+00:00",
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            "available_at",
        ),
    ],
)
def test_decoder_field_failure_evidence(
    field: str,
    value: str,
    code: HistoricalMarketDataFailureCode,
    evidence_field: str,
) -> None:
    _assert_failure(
        _content(_replace_field(_row(), field, value)),
        code,
        record_number=2,
        field_name=evidence_field,
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"end": "2026-01-02T09:30:00.000000Z"}, "interval_end"),
        ({"open_": "102.0"}, "open"),
        ({"close": "98.0"}, "close"),
    ],
)
def test_decoder_locates_remaining_bar_invariants(
    changes: dict[str, str],
    field: str,
) -> None:
    _assert_failure(
        _content(_row(**changes)),
        HistoricalMarketDataFailureCode.INVALID_BAR,
        record_number=2,
        field_name=field,
    )


def test_decoder_rejects_malformed_quote_multiline_and_field_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = _content(_row()).replace(b"XNAS", b'X"NAS')
    _assert_failure(
        malformed,
        HistoricalMarketDataFailureCode.MALFORMED_CSV,
        record_number=2,
    )
    unclosed = _content(_row()).rstrip(b"\n").replace(b"XNAS", b'"XNAS')
    _assert_failure(
        unclosed,
        HistoricalMarketDataFailureCode.MALFORMED_CSV,
        record_number=2,
    )
    multiline = _content(_row()).replace(b"XNAS", b'"XN\nAS"')
    _assert_failure(
        multiline,
        HistoricalMarketDataFailureCode.INVALID_FIELD_CHARACTER,
        record_number=2,
        field_name="venue",
    )
    monkeypatch.setattr(historical_module, "PHASE1_OHLCV_MAX_FIELD_LENGTH", 3)
    _assert_failure(
        _content(_row()),
        HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
        record_number=1,
    )


def test_decoder_handles_doubled_quote_before_identity_and_accepts_underflow() -> None:
    _assert_failure(
        _content(_row(source='"fixture.""raw"""')),
        HistoricalMarketDataFailureCode.INVALID_IDENTITY,
        record_number=2,
        field_name="source",
    )

    dataset = decode_phase1_ohlcv_csv(
        _content(_row(open_="1e-9999", low="-1.0")),
        replay_window=WINDOW,
    )
    assert dataset.selection.events[0].payload.open == 0.0


def test_doubled_quote_counts_toward_decoded_field_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(historical_module, "PHASE1_OHLCV_MAX_FIELD_LENGTH", 27)
    _assert_failure(
        _content(_row(source='"' + "a" * 27 + '"""')),
        HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
        record_number=2,
        field_name="source",
    )


def test_decoder_rejects_byte_and_record_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _content(_row())
    monkeypatch.setattr(historical_module, "PHASE1_OHLCV_MAX_BYTES", len(content) - 1)
    _assert_failure(content, HistoricalMarketDataFailureCode.SOURCE_TOO_LARGE)
    monkeypatch.setattr(historical_module, "PHASE1_OHLCV_MAX_BYTES", len(content))
    monkeypatch.setattr(historical_module, "PHASE1_OHLCV_MAX_RECORDS", 1)
    _assert_failure(
        content,
        HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
        record_number=2,
    )


def test_complete_history_conflict_matrix() -> None:
    duplicate = _row()
    _assert_failure(
        _content(duplicate, duplicate),
        HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
        record_number=3,
    )
    emission_collision = _row(
        start="2026-01-02T09:31:00.000000Z",
        end="2026-01-02T09:32:00.000000Z",
        available="2026-01-02T09:32:00.000000Z",
    )
    _assert_failure(
        _content(_row(), emission_collision),
        HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
        record_number=3,
        field_name="source_sequence",
    )
    revision_regression = _row(
        available="2026-01-02T09:35:00.000000Z",
        sequence="1",
    )
    later_revision = _row(
        available="2026-01-02T09:34:00.000000Z",
        sequence="2",
        revision="1",
    )
    _assert_failure(
        _content(revision_regression, later_revision),
        HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
        record_number=3,
        field_name="available_at",
    )
    non_increasing_sequence = _row(
        available="2026-01-02T09:36:00.000000Z",
        sequence="0",
        revision="1",
    )
    _assert_failure(
        _content(_row(), non_increasing_sequence),
        HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
        record_number=3,
        field_name="source_sequence",
    )


def test_empty_replay_selection_is_explicit() -> None:
    outside = ReplayWindow(
        datetime(2026, 1, 3, 9, 0, tzinfo=UTC),
        datetime(2026, 1, 3, 10, 0, tzinfo=UTC),
    )
    with pytest.raises(HistoricalMarketDataError) as caught:
        decode_phase1_ohlcv_csv(_content(_row()), replay_window=outside)
    assert caught.value.code is HistoricalMarketDataFailureCode.EMPTY_REPLAY_SELECTION


def test_source_early_cursor_replay_and_monotone_limited_drain() -> None:
    second = _row(
        start="2026-01-02T09:31:00.000000Z",
        end="2026-01-02T09:32:00.000000Z",
        available="2026-01-02T09:32:00.000000Z",
        sequence="1",
    )
    dataset = decode_phase1_ohlcv_csv(_content(_row(), second), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)
    first_time = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
    early_clock = _Clock(datetime(2026, 1, 2, 9, 30, tzinfo=UTC))

    initial, empty = source.admit(clock=cast(Any, early_clock), cursor=None)

    assert empty == ()
    assert initial.last_event is None
    assert early_clock.calls == 1
    assert source.next_available_at(initial) == first_time

    cutoff = datetime(2026, 1, 2, 9, 33, tzinfo=UTC)
    clock = _Clock(cutoff)
    first_cursor, first_batch = source.admit(
        clock=cast(Any, clock),
        cursor=initial,
        limit=1,
    )
    assert tuple(event.source_sequence for event in first_batch) == (0,)
    assert source.next_available_at(first_cursor) == cutoff
    second_cursor, second_batch = source.admit(
        clock=cast(Any, clock),
        cursor=first_cursor,
        limit=1,
    )
    assert tuple(event.source_sequence for event in second_batch) == (1,)
    assert source.next_available_at(second_cursor) is None
    replay_cursor, replay_batch = source.admit(
        clock=cast(Any, clock),
        cursor=first_cursor,
        limit=1,
    )
    assert replay_batch == second_batch
    assert replay_cursor == second_cursor


def test_source_rejects_forged_binding_initial_skip_and_signed_zero_alias() -> None:
    row = _row(open_="-0.0", low="-1.0", high="1.0", close="0.0")
    dataset = decode_phase1_ohlcv_csv(_content(row), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)
    cutoff = datetime(2026, 1, 2, 9, 40, tzinfo=UTC)
    cursor, events = source.admit(clock=cast(Any, _Clock(cutoff)), cursor=None)
    assert len(events) == 1

    object.__setattr__(cursor, "_data_sha256", Sha256Digest("f" * 64))
    with pytest.raises(HistoricalMarketDataError) as binding:
        source.next_available_at(cursor)
    assert binding.value.code is HistoricalMarketDataFailureCode.INVALID_CURSOR

    valid_cursor, _events = source.admit(clock=cast(Any, _Clock(cutoff)), cursor=None)
    event = cast(MarketDataEnvelope, valid_cursor.last_event)
    changed_bar = replace(event.payload, open=0.0)
    changed_event = replace(event, payload=changed_bar)
    forged = _forged_cursor(
        dataset,
        AdmissionCursor(as_of=cutoff, last_event=changed_event),
    )
    assert event == changed_event
    assert canonical_market_data_record_bytes(event) != canonical_market_data_record_bytes(
        changed_event
    )
    with pytest.raises(HistoricalMarketDataError) as signed_zero:
        source.next_available_at(forged)
    assert signed_zero.value.code is HistoricalMarketDataFailureCode.INVALID_CURSOR

    initial_skip = _forged_cursor(
        dataset,
        AdmissionCursor(as_of=cutoff, last_event=None),
    )
    with pytest.raises(HistoricalMarketDataError) as skipped:
        source.next_available_at(initial_skip)
    assert skipped.value.code is HistoricalMarketDataFailureCode.INVALID_CURSOR


def test_source_rejects_wrong_cursor_type_shape_time_key_and_visibility() -> None:
    dataset = decode_phase1_ohlcv_csv(_content(_row()), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)
    event = dataset.selection.events[0]
    early = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)

    invalid_candidates = (
        cast(Any, AdmissionCursor(as_of=early, last_event=None)),
        _forged_cursor(dataset, object()),
        _forged_cursor(
            dataset,
            _raw_admission(
                as_of=datetime(2026, 1, 2, 9, 30),
                last_event=None,
            ),
        ),
        _forged_cursor(
            dataset,
            _raw_admission(
                as_of=early,
                last_event=replace(event, source_sequence=999),
            ),
        ),
        _forged_cursor(
            dataset,
            _raw_admission(as_of=early, last_event=event),
        ),
    )

    for candidate in invalid_candidates:
        with pytest.raises(HistoricalMarketDataError) as caught:
            source.next_available_at(candidate)
        assert caught.value.code is HistoricalMarketDataFailureCode.INVALID_CURSOR


def test_source_validates_cursor_and_limit_before_reading_clock() -> None:
    dataset = decode_phase1_ohlcv_csv(_content(_row()), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)
    clock = _Clock(WINDOW.start_inclusive)

    with pytest.raises(HistoricalMarketDataError) as cursor_error:
        source.admit(clock=cast(Any, clock), cursor=cast(Any, object()))
    assert cursor_error.value.code is HistoricalMarketDataFailureCode.INVALID_CURSOR
    assert clock.calls == 0

    with pytest.raises(HistoricalMarketDataError) as limit_error:
        source.admit(clock=cast(Any, clock), cursor=None, limit=True)
    assert limit_error.value.code is HistoricalMarketDataFailureCode.INVALID_LIMIT
    assert clock.calls == 0


def test_source_clock_limit_and_unexpected_clock_error_matrix() -> None:
    dataset = decode_phase1_ohlcv_csv(_content(_row()), replay_window=WINDOW)
    source = create_phase1_historical_market_data_source(dataset)
    with pytest.raises(HistoricalMarketDataError) as shape:
        source.admit(clock=cast(Any, object()), cursor=None)
    assert shape.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE
    non_callable_clock = type("_NonCallableClock", (), {"now": 0})()
    with pytest.raises(HistoricalMarketDataError) as non_callable:
        source.admit(clock=cast(Any, non_callable_clock), cursor=None)
    assert non_callable.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE

    class _BrokenNowProperty:
        @property
        def now(self) -> datetime:
            raise RuntimeError("property failed")

    with pytest.raises(HistoricalMarketDataError) as property_error:
        source.admit(clock=cast(Any, _BrokenNowProperty()), cursor=None)
    assert property_error.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE
    with pytest.raises(HistoricalMarketDataError) as limit:
        source.admit(clock=cast(Any, _Clock(WINDOW.start_inclusive)), cursor=None, limit=0)
    assert limit.value.code is HistoricalMarketDataFailureCode.INVALID_LIMIT
    with pytest.raises(HistoricalMarketDataError) as naive:
        source.admit(clock=cast(Any, _Clock(datetime(2026, 1, 2))), cursor=None)
    assert naive.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE

    class _BrokenClock:
        def now(self) -> datetime:
            raise RuntimeError("clock failed")

    with pytest.raises(RuntimeError, match="clock failed"):
        source.admit(clock=_BrokenClock(), cursor=None)

    late, _ = source.admit(
        clock=cast(Any, _Clock(datetime(2026, 1, 2, 9, 30, tzinfo=UTC))),
        cursor=None,
    )
    with pytest.raises(HistoricalMarketDataError) as regressed:
        source.admit(
            clock=cast(Any, _Clock(datetime(2026, 1, 2, 9, 29, tzinfo=UTC))),
            cursor=late,
        )
    assert regressed.value.code is HistoricalMarketDataFailureCode.CLOCK_REGRESSED


def test_source_factory_rejects_low_level_corrupted_dataset() -> None:
    dataset = decode_phase1_ohlcv_csv(_content(_row()), replay_window=WINDOW)
    object.__setattr__(dataset, "profile", "wrong")
    with pytest.raises(HistoricalMarketDataError) as caught:
        create_phase1_historical_market_data_source(dataset)
    assert caught.value.code is HistoricalMarketDataFailureCode.INVALID_DATASET


def test_source_factory_rejects_non_dataset_and_forged_fingerprint() -> None:
    with pytest.raises(HistoricalMarketDataError) as wrong_type:
        create_phase1_historical_market_data_source(cast(Any, object()))
    assert wrong_type.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE

    dataset = decode_phase1_ohlcv_csv(_content(_row()), replay_window=WINDOW)
    object.__setattr__(
        dataset.selection,
        "fingerprint",
        replace(dataset.selection.fingerprint, record_count=2),
    )
    with pytest.raises(HistoricalMarketDataError) as forged:
        create_phase1_historical_market_data_source(dataset)
    assert forged.value.code is HistoricalMarketDataFailureCode.INVALID_DATASET


def test_local_file_reader_accepts_stable_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    content = _content(_row())
    path.write_bytes(content)

    dataset = read_phase1_ohlcv_csv(path, replay_window=WINDOW)

    assert dataset.source_bytes_sha256 == Sha256Digest(sha256(content).hexdigest())


def test_local_file_reader_rejects_symlink_directory_fifo_and_missing(tmp_path: Path) -> None:
    regular = tmp_path / "bars.csv"
    regular.write_bytes(_content(_row()))
    symlink = tmp_path / "link.csv"
    symlink.symlink_to(regular)
    fifo = tmp_path / "bars.fifo"
    os.mkfifo(fifo)
    for path in (symlink, tmp_path, fifo, tmp_path / "missing.csv"):
        with pytest.raises(HistoricalMarketDataError) as caught:
            read_phase1_ohlcv_csv(path, replay_window=WINDOW)
        assert caught.value.code is HistoricalMarketDataFailureCode.SOURCE_UNREADABLE


def test_local_file_reader_rejects_wrong_types_missing_flags_and_initial_oversize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bars.csv"
    path.write_bytes(_content(_row()))
    with pytest.raises(HistoricalMarketDataError) as wrong_type:
        read_phase1_ohlcv_csv(cast(Any, str(path)), replay_window=WINDOW)
    assert wrong_type.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE

    monkeypatch.delattr("ea.data.historical.os.O_NOFOLLOW")
    with pytest.raises(HistoricalMarketDataError) as missing_flag:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert missing_flag.value.code is HistoricalMarketDataFailureCode.SOURCE_UNREADABLE

    monkeypatch.undo()
    monkeypatch.setattr(historical_module, "PHASE1_OHLCV_MAX_BYTES", path.stat().st_size - 1)
    with pytest.raises(HistoricalMarketDataError) as oversized:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert oversized.value.code is HistoricalMarketDataFailureCode.SOURCE_TOO_LARGE


def test_local_file_reader_distinguishes_stable_open_failure_and_raced_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bars.csv"
    path.write_bytes(_content(_row()))
    real_open = os.open

    def denied(_path: object, _flags: int) -> int:
        raise PermissionError

    monkeypatch.setattr("ea.data.historical.os.open", denied)
    with pytest.raises(HistoricalMarketDataError) as denied_error:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert denied_error.value.code is HistoricalMarketDataFailureCode.SOURCE_UNREADABLE

    monkeypatch.setattr("ea.data.historical.os.open", real_open)
    path.write_bytes(_content(_row()))

    def raced_fifo(target: object, flags: int) -> int:
        Path(cast(Any, target)).unlink()
        os.mkfifo(Path(cast(Any, target)))
        return real_open(cast(Any, target), flags)

    monkeypatch.setattr("ea.data.historical.os.open", raced_fifo)
    with pytest.raises(HistoricalMarketDataError) as race:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert race.value.code is HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ


def test_local_file_reader_classifies_changed_and_indeterminate_open_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bars.csv"
    path.write_bytes(_content(_row()))

    def disappear(target: object, _flags: int) -> int:
        Path(cast(Any, target)).unlink()
        raise PermissionError

    monkeypatch.setattr("ea.data.historical.os.open", disappear)
    with pytest.raises(HistoricalMarketDataError) as changed:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert changed.value.code is HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ

    monkeypatch.undo()
    path.write_bytes(_content(_row()))
    real_lstat = Path.lstat
    calls = 0

    def indeterminate(self: Path) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_lstat(self)
        raise PermissionError

    def denied(_path: object, _flags: int) -> int:
        raise PermissionError

    monkeypatch.setattr(Path, "lstat", indeterminate)
    monkeypatch.setattr("ea.data.historical.os.open", denied)
    with pytest.raises(HistoricalMarketDataError) as unreadable:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert unreadable.value.code is HistoricalMarketDataFailureCode.SOURCE_UNREADABLE


def test_local_file_reader_rejects_raced_device_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bars.csv"
    path.write_bytes(_content(_row()))
    real_open = os.open

    def raced_device(_target: object, flags: int) -> int:
        return real_open("/dev/null", flags)

    monkeypatch.setattr("ea.data.historical.os.open", raced_device)
    with pytest.raises(HistoricalMarketDataError) as changed:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert changed.value.code is HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ


def test_local_file_reader_detects_fstat_read_and_post_read_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bars.csv"
    path.write_bytes(_content(_row()))
    real_fstat = os.fstat

    def failed_fstat(_descriptor: int) -> os.stat_result:
        raise OSError

    monkeypatch.setattr("ea.data.historical.os.fstat", failed_fstat)
    with pytest.raises(HistoricalMarketDataError) as fstat_error:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert fstat_error.value.code is HistoricalMarketDataFailureCode.SOURCE_UNREADABLE

    monkeypatch.setattr("ea.data.historical.os.fstat", real_fstat)
    real_read = os.read
    changed = False

    def changing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b"\n")
        return chunk

    monkeypatch.setattr("ea.data.historical.os.read", changing_read)
    with pytest.raises(HistoricalMarketDataError) as changed_error:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert changed_error.value.code is HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ


def test_local_file_reader_classifies_verified_read_and_post_stat_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bars.csv"
    path.write_bytes(_content(_row()))

    def failed_read(_descriptor: int, _size: int) -> bytes:
        raise OSError

    monkeypatch.setattr("ea.data.historical.os.read", failed_read)
    with pytest.raises(HistoricalMarketDataError) as read_error:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert read_error.value.code is HistoricalMarketDataFailureCode.SOURCE_UNREADABLE

    monkeypatch.undo()
    real_fstat = os.fstat
    calls = 0

    def failed_second_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_fstat(descriptor)
        raise OSError

    monkeypatch.setattr("ea.data.historical.os.fstat", failed_second_fstat)
    with pytest.raises(HistoricalMarketDataError) as post_error:
        read_phase1_ohlcv_csv(path, replay_window=WINDOW)
    assert post_error.value.code is HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ
