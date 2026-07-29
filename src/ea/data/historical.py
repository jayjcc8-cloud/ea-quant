"""Strict Phase 1 OHLCV decoding and bounded historical admission."""

from __future__ import annotations

import csv
import io
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from pathlib import Path
from struct import error as StructError
from types import MappingProxyType
from typing import Any, Literal, NoReturn, cast, final

from ea.core.identity import IdentityValidationError, Instrument, VenueId
from ea.core.market_data import (
    Adjustment,
    AdmissionCursor,
    AdmissionOrderKey,
    Bar,
    MarketDataEnvelope,
    MarketDataLogicalKey,
    MarketDataValidationError,
    SourceId,
    admission_order_key,
    admit_market_data,
    is_visible_as_of,
    validate_market_data_batch,
)
from ea.core.run import DataFingerprint, ReplayWindow, RunContractError, Sha256Digest
from ea.core.time import Clock, TimeValidationError, require_utc
from ea.data.fingerprint import (
    MarketDataSelection,
    canonical_market_data_record_bytes,
    select_and_fingerprint_market_data,
)

PHASE1_OHLCV_PROFILE = "ea-phase1-ohlcv-csv-v1"
PHASE1_OHLCV_MAX_BYTES = 67_108_864
PHASE1_OHLCV_MAX_RECORDS = 1_000_001
PHASE1_OHLCV_MAX_FIELD_LENGTH = 1_024

_MAX_UINT64 = (1 << 64) - 1
_HEADER = (
    "schema_version",
    "venue",
    "symbol",
    "interval_start",
    "interval_end",
    "adjustment",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "source_sequence",
    "revision",
    "available_at",
)
_DATETIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z",
    flags=re.ASCII,
)
_UNSIGNED_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z", flags=re.ASCII)
_FLOAT_PATTERN = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?(?:0|[1-9][0-9]*))?\Z",
    flags=re.ASCII,
)


class HistoricalMarketDataFailureCode(StrEnum):
    """Closed failure vocabulary for ADR 0015."""

    INVALID_TYPE = "invalid_type"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_CHANGED_DURING_READ = "source_changed_during_read"
    EMPTY_SOURCE = "empty_source"
    SOURCE_TOO_LARGE = "source_too_large"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_FILE_CHARACTER = "invalid_file_character"
    MALFORMED_CSV = "malformed_csv"
    INVALID_HEADER = "invalid_header"
    INVALID_RECORD_SHAPE = "invalid_record_shape"
    INVALID_FIELD_CHARACTER = "invalid_field_character"
    INVALID_SCHEMA_VERSION = "invalid_schema_version"
    INVALID_ADJUSTMENT = "invalid_adjustment"
    INVALID_IDENTITY = "invalid_identity"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_INTEGER = "invalid_integer"
    INVALID_FLOAT = "invalid_float"
    INVALID_BAR = "invalid_bar"
    INVALID_ENVELOPE = "invalid_envelope"
    CANONICAL_BATCH_CONFLICT = "canonical_batch_conflict"
    EMPTY_REPLAY_SELECTION = "empty_replay_selection"
    INVALID_DATASET = "invalid_dataset"
    INVALID_CURSOR = "invalid_cursor"
    CLOCK_REGRESSED = "clock_regressed"
    INVALID_LIMIT = "invalid_limit"


class HistoricalMarketDataError(ValueError):
    """Structured strict-source failure with stable location evidence."""

    code: HistoricalMarketDataFailureCode
    record_number: int | None
    field_name: str | None

    def __init__(
        self,
        code: HistoricalMarketDataFailureCode,
        *,
        record_number: int | None = None,
        field_name: str | None = None,
    ) -> None:
        if type(code) is not HistoricalMarketDataFailureCode:
            raise TypeError("historical market-data errors require an exact failure code")
        if record_number is not None and (type(record_number) is not int or record_number < 1):
            raise TypeError("record_number must be a positive exact int or None")
        if field_name is not None and (type(field_name) is not str or field_name not in _HEADER):
            raise TypeError("field_name must be one exact header name or None")
        self.code = code
        self.record_number = record_number
        self.field_name = field_name
        location = ""
        if record_number is not None:
            location += f" record={record_number}"
        if field_name is not None:
            location += f" field={field_name}"
        super().__init__(f"{code.value}{location}")


def _raise(
    code: HistoricalMarketDataFailureCode,
    *,
    record_number: int | None = None,
    field_name: str | None = None,
) -> NoReturn:
    raise HistoricalMarketDataError(
        code,
        record_number=record_number,
        field_name=field_name,
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class Phase1HistoricalDataset:
    """Decoder-issued immutable captured bytes and semantic selection evidence."""

    profile: Literal["ea-phase1-ohlcv-csv-v1"]
    replay_window: ReplayWindow
    selection: MarketDataSelection
    source_bytes_sha256: Sha256Digest
    source_byte_count: int

    def __init__(self) -> None:
        raise TypeError("datasets are issued only by the Phase 1 OHLCV decoder")


@final
@dataclass(frozen=True, slots=True, init=False)
class Phase1HistoricalSourceCursor:
    """Source-issued semantic binding around one ADR 0004 admission cursor."""

    _replay_window: ReplayWindow
    _data_sha256: Sha256Digest
    _record_count: int
    _admission_cursor: AdmissionCursor

    def __init__(self) -> None:
        raise TypeError("historical source cursors are issued only by source.admit")

    @property
    def replay_window(self) -> ReplayWindow:
        return self._replay_window

    @property
    def data_sha256(self) -> Sha256Digest:
        return self._data_sha256

    @property
    def record_count(self) -> int:
        return self._record_count

    @property
    def as_of(self) -> datetime:
        return self._admission_cursor.as_of

    @property
    def last_event(self) -> MarketDataEnvelope | None:
        return self._admission_cursor.last_event


@final
@dataclass(frozen=True, slots=True, init=False, repr=False)
class Phase1HistoricalMarketDataSource:
    """Bounded future-payload-safe historical source."""

    _events: tuple[MarketDataEnvelope, ...]
    _fingerprint: DataFingerprint
    _index_by_key: MappingProxyType[
        AdmissionOrderKey,
        tuple[int, MarketDataEnvelope, bytes],
    ]
    _replay_window: ReplayWindow

    def __init__(self) -> None:
        raise TypeError(
            "historical sources are created only by create_phase1_historical_market_data_source"
        )

    @property
    def replay_window(self) -> ReplayWindow:
        return self._replay_window

    @property
    def fingerprint(self) -> DataFingerprint:
        return self._fingerprint

    def next_available_at(
        self,
        cursor: Phase1HistoricalSourceCursor | None,
    ) -> datetime | None:
        """Return monotone scheduling metadata without revealing future payload."""
        admission = self._validated_admission_cursor(cursor)
        if admission is None or admission.last_event is None:
            next_index = 0
        else:
            next_index = self._index_by_key[admission_order_key(admission.last_event)][0] + 1
        if next_index >= len(self._events):
            return None
        available_at = self._events[next_index].available_at
        return (
            available_at
            if admission is None or available_at >= admission.as_of
            else admission.as_of
        )

    def admit(
        self,
        *,
        clock: Clock,
        cursor: Phase1HistoricalSourceCursor | None,
        limit: int | None = None,
    ) -> tuple[Phase1HistoricalSourceCursor, tuple[MarketDataEnvelope, ...]]:
        """Return one visible candidate batch without committing source progress."""
        try:
            now = cast(Any, clock).now
        except Exception:
            _raise(HistoricalMarketDataFailureCode.INVALID_TYPE)
        if not callable(now):
            _raise(HistoricalMarketDataFailureCode.INVALID_TYPE)
        admission = self._validated_admission_cursor(cursor)
        if limit is not None and (type(limit) is not int or limit <= 0):
            _raise(HistoricalMarketDataFailureCode.INVALID_LIMIT)
        observed = now()
        try:
            cutoff = require_utc(observed, field="clock.now()")
        except TimeValidationError:
            _raise(HistoricalMarketDataFailureCode.INVALID_TYPE)
        if admission is not None and cutoff < admission.as_of:
            _raise(HistoricalMarketDataFailureCode.CLOCK_REGRESSED)
        try:
            next_admission, events = admit_market_data(
                self._events,
                clock=_FixedClock(cutoff),
                cursor=admission,
                limit=limit,
            )
        except MarketDataValidationError as error:
            raise AssertionError("validated source admission unexpectedly failed") from error
        return self._issued_cursor(next_admission), events

    def _validated_admission_cursor(
        self,
        cursor: Phase1HistoricalSourceCursor | None,
    ) -> AdmissionCursor | None:
        if cursor is None:
            return None
        if type(cursor) is not Phase1HistoricalSourceCursor:
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        try:
            replay_window = cursor._replay_window
            data_sha256 = cursor._data_sha256
            record_count = cursor._record_count
            admission = cursor._admission_cursor
        except (AttributeError, TypeError):
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        if (
            type(replay_window) is not ReplayWindow
            or type(data_sha256) is not Sha256Digest
            or type(record_count) is not int
            or record_count <= 0
            or type(admission) is not AdmissionCursor
        ):
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        try:
            binding_matches = (
                replay_window == self._replay_window
                and data_sha256 == self._fingerprint.sha256
                and record_count == self._fingerprint.record_count
            )
        except (AttributeError, TypeError):
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        if not binding_matches:
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        try:
            as_of = admission.as_of
            last_event = admission.last_event
        except (AttributeError, TypeError):
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        if type(as_of) is not datetime or as_of.tzinfo is not UTC:
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        if last_event is None:
            if any(is_visible_as_of(event, as_of) for event in self._events):
                _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
            return AdmissionCursor(as_of=as_of, last_event=None)
        if type(last_event) is not MarketDataEnvelope:
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        try:
            key = admission_order_key(last_event)
            indexed = self._index_by_key.get(key)
            encoded = canonical_market_data_record_bytes(last_event)
        except (
            AttributeError,
            MarketDataValidationError,
            OverflowError,
            RunContractError,
            StructError,
            TypeError,
            ValueError,
        ):
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        if indexed is None or encoded != indexed[2]:
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        source_event = indexed[1]
        if not is_visible_as_of(source_event, as_of):
            _raise(HistoricalMarketDataFailureCode.INVALID_CURSOR)
        return AdmissionCursor(as_of=as_of, last_event=source_event)

    def _issued_cursor(
        self,
        admission: AdmissionCursor,
    ) -> Phase1HistoricalSourceCursor:
        value = object.__new__(Phase1HistoricalSourceCursor)
        object.__setattr__(value, "_replay_window", self._replay_window)
        object.__setattr__(value, "_data_sha256", self._fingerprint.sha256)
        object.__setattr__(value, "_record_count", self._fingerprint.record_count)
        object.__setattr__(value, "_admission_cursor", admission)
        return value


@dataclass(frozen=True, slots=True)
class _FixedClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def decode_phase1_ohlcv_csv(
    content: bytes,
    *,
    replay_window: ReplayWindow,
) -> Phase1HistoricalDataset:
    """Decode one strict bounded CSV capture into canonical semantic evidence."""
    if type(content) is not bytes or type(replay_window) is not ReplayWindow:
        _raise(HistoricalMarketDataFailureCode.INVALID_TYPE)
    if not content:
        _raise(HistoricalMarketDataFailureCode.EMPTY_SOURCE)
    if len(content) > PHASE1_OHLCV_MAX_BYTES:
        _raise(HistoricalMarketDataFailureCode.SOURCE_TOO_LARGE)
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _raise(HistoricalMarketDataFailureCode.INVALID_ENCODING)
    if text.startswith("\ufeff"):
        _raise(HistoricalMarketDataFailureCode.INVALID_ENCODING)
    if "\x00" in text or _contains_bare_carriage_return(text):
        _raise(HistoricalMarketDataFailureCode.INVALID_FILE_CHARACTER)
    _preflight_csv(text)
    rows = _read_csv(text)
    if len(rows) > PHASE1_OHLCV_MAX_RECORDS:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
            record_number=PHASE1_OHLCV_MAX_RECORDS + 1,
        )
    if not rows or tuple(rows[0]) != _HEADER:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_HEADER,
            record_number=1,
        )
    if len(rows) == 1:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
            record_number=2,
        )
    events: list[MarketDataEnvelope] = []
    record_numbers: list[int] = []
    for record_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(_HEADER) or (len(row) == 1 and row[0].startswith("#")):
            _raise(
                HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
                record_number=record_number,
            )
        for index, token in enumerate(row):
            if any(ord(character) < 32 or ord(character) > 126 for character in token):
                _raise(
                    HistoricalMarketDataFailureCode.INVALID_FIELD_CHARACTER,
                    record_number=record_number,
                    field_name=_HEADER[index],
                )
        events.append(_event_from_row(row, record_number=record_number))
        record_numbers.append(record_number)
    materialized = tuple(events)
    _validate_complete_history(materialized, tuple(record_numbers))
    selected = tuple(
        event
        for event in materialized
        if replay_window.start_inclusive <= event.available_at < replay_window.end_exclusive
    )
    if not selected:
        _raise(HistoricalMarketDataFailureCode.EMPTY_REPLAY_SELECTION)
    try:
        selection = select_and_fingerprint_market_data(materialized, replay_window)
    except (MarketDataValidationError, RunContractError) as error:
        raise AssertionError("prevalidated market-data selection unexpectedly failed") from error
    value = object.__new__(Phase1HistoricalDataset)
    object.__setattr__(value, "profile", PHASE1_OHLCV_PROFILE)
    object.__setattr__(value, "replay_window", replay_window)
    object.__setattr__(value, "selection", selection)
    object.__setattr__(
        value,
        "source_bytes_sha256",
        Sha256Digest(sha256(content).hexdigest()),
    )
    object.__setattr__(value, "source_byte_count", len(content))
    return value


def read_phase1_ohlcv_csv(
    path: Path,
    *,
    replay_window: ReplayWindow,
) -> Phase1HistoricalDataset:
    """Capture one stable local regular file and invoke the strict decoder."""
    if type(path) is not type(Path()) or type(replay_window) is not ReplayWindow:
        _raise(HistoricalMarketDataFailureCode.INVALID_TYPE)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if type(nofollow) is not int or type(nonblock) is not int:
        _raise(HistoricalMarketDataFailureCode.SOURCE_UNREADABLE)
    try:
        before = path.lstat()
    except OSError:
        _raise(HistoricalMarketDataFailureCode.SOURCE_UNREADABLE)
    if not stat.S_ISREG(before.st_mode):
        _raise(HistoricalMarketDataFailureCode.SOURCE_UNREADABLE)
    if before.st_size > PHASE1_OHLCV_MAX_BYTES:
        _raise(HistoricalMarketDataFailureCode.SOURCE_TOO_LARGE)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | nonblock,
        )
    except OSError:
        _raise(_classify_open_failure(path, before))
    try:
        content = _capture_descriptor(descriptor, path=path, before=before)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    os.close(descriptor)
    return decode_phase1_ohlcv_csv(content, replay_window=replay_window)


def create_phase1_historical_market_data_source(
    dataset: Phase1HistoricalDataset,
) -> Phase1HistoricalMarketDataSource:
    """Create the bounded source from exact decoder-issued dataset evidence."""
    if type(dataset) is not Phase1HistoricalDataset:
        _raise(HistoricalMarketDataFailureCode.INVALID_TYPE)
    try:
        if (
            dataset.profile != PHASE1_OHLCV_PROFILE
            or type(dataset.replay_window) is not ReplayWindow
            or type(dataset.selection) is not MarketDataSelection
            or dataset.selection.window != dataset.replay_window
            or type(dataset.source_bytes_sha256) is not Sha256Digest
            or type(dataset.source_byte_count) is not int
            or not 1 <= dataset.source_byte_count <= PHASE1_OHLCV_MAX_BYTES
        ):
            _raise(HistoricalMarketDataFailureCode.INVALID_DATASET)
        recomputed = select_and_fingerprint_market_data(
            dataset.selection.events,
            dataset.replay_window,
        )
        if recomputed != dataset.selection:
            _raise(HistoricalMarketDataFailureCode.INVALID_DATASET)
    except HistoricalMarketDataError:
        raise
    except (MarketDataValidationError, RunContractError, TypeError, AttributeError):
        _raise(HistoricalMarketDataFailureCode.INVALID_DATASET)
    source = object.__new__(Phase1HistoricalMarketDataSource)
    events = tuple(dataset.selection.events)
    object.__setattr__(source, "_events", events)
    object.__setattr__(source, "_replay_window", dataset.replay_window)
    object.__setattr__(source, "_fingerprint", dataset.selection.fingerprint)
    object.__setattr__(
        source,
        "_index_by_key",
        MappingProxyType(
            {
                admission_order_key(event): (
                    index,
                    event,
                    canonical_market_data_record_bytes(event),
                )
                for index, event in enumerate(events)
            }
        ),
    )
    return source


def _contains_bare_carriage_return(text: str) -> bool:
    index = text.find("\r")
    while index >= 0:
        if index + 1 >= len(text) or text[index + 1] != "\n":
            return True
        index = text.find("\r", index + 2)
    return False


def _field_name(record_number: int, field_index: int) -> str | None:
    if record_number == 1 or field_index >= len(_HEADER):
        return None
    return _HEADER[field_index]


def _preflight_csv(text: str) -> None:
    record_number = 1
    field_index = 0
    field_length = 0
    in_quotes = False
    after_quote = False
    at_field_start = True
    index = 0
    while index < len(text):
        character = text[index]
        if in_quotes:
            if character in "\r\n":
                _raise(
                    HistoricalMarketDataFailureCode.INVALID_FIELD_CHARACTER,
                    record_number=record_number,
                    field_name=_field_name(record_number, field_index),
                )
            if character == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    field_length += 1
                    if field_length > PHASE1_OHLCV_MAX_FIELD_LENGTH:
                        _raise(
                            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
                            record_number=record_number,
                            field_name=_field_name(record_number, field_index),
                        )
                    index += 2
                    continue
                in_quotes = False
                after_quote = True
                index += 1
                continue
            field_length += 1
            if field_length > PHASE1_OHLCV_MAX_FIELD_LENGTH:
                _raise(
                    HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
                    record_number=record_number,
                    field_name=_field_name(record_number, field_index),
                )
            index += 1
            continue
        if after_quote and character not in ",\r\n":
            _raise(
                HistoricalMarketDataFailureCode.MALFORMED_CSV,
                record_number=record_number,
            )
        if character == '"' and at_field_start:
            in_quotes = True
            at_field_start = False
            index += 1
            continue
        if character == '"':
            _raise(
                HistoricalMarketDataFailureCode.MALFORMED_CSV,
                record_number=record_number,
            )
        if character == ",":
            field_index += 1
            field_length = 0
            at_field_start = True
            after_quote = False
            index += 1
            continue
        if character in "\r\n":
            if character == "\r":
                index += 1
            record_number += 1
            if record_number > PHASE1_OHLCV_MAX_RECORDS + 1:
                _raise(
                    HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
                    record_number=PHASE1_OHLCV_MAX_RECORDS + 1,
                )
            field_index = 0
            field_length = 0
            at_field_start = True
            after_quote = False
            index += 1
            continue
        if after_quote:
            raise AssertionError("after-quote state should have failed")
        field_length += 1
        if field_length > PHASE1_OHLCV_MAX_FIELD_LENGTH:
            _raise(
                HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
                record_number=record_number,
                field_name=_field_name(record_number, field_index),
            )
        at_field_start = False
        index += 1
    if in_quotes:
        _raise(
            HistoricalMarketDataFailureCode.MALFORMED_CSV,
            record_number=record_number,
        )
    logical_records = record_number - 1 if text.endswith("\n") else record_number
    if logical_records > PHASE1_OHLCV_MAX_RECORDS:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_RECORD_SHAPE,
            record_number=PHASE1_OHLCV_MAX_RECORDS + 1,
        )


def _read_csv(text: str) -> list[list[str]]:
    stream = io.StringIO(text, newline="")
    reader = csv.reader(
        stream,
        dialect="excel",
        delimiter=",",
        quotechar='"',
        doublequote=True,
        escapechar=None,
        skipinitialspace=False,
        strict=True,
        quoting=csv.QUOTE_MINIMAL,
    )
    rows: list[list[str]] = []
    try:
        for row in reader:
            rows.append(row)
    except csv.Error:
        _raise(
            HistoricalMarketDataFailureCode.MALFORMED_CSV,
            record_number=len(rows) + 1,
        )
    return rows


def _parse_datetime(token: str, *, record_number: int, field_name: str) -> datetime:
    if _DATETIME_PATTERN.fullmatch(token) is None:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            record_number=record_number,
            field_name=field_name,
        )
    try:
        value = datetime.strptime(token, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            record_number=record_number,
            field_name=field_name,
        )
    if value.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != token:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_TIMESTAMP,
            record_number=record_number,
            field_name=field_name,
        )
    return value


def _parse_unsigned(token: str, *, record_number: int, field_name: str) -> int:
    if _UNSIGNED_PATTERN.fullmatch(token) is None:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_INTEGER,
            record_number=record_number,
            field_name=field_name,
        )
    value = int(token)
    if value > _MAX_UINT64:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_INTEGER,
            record_number=record_number,
            field_name=field_name,
        )
    return value


def _parse_float(token: str, *, record_number: int, field_name: str) -> float:
    if _FLOAT_PATTERN.fullmatch(token) is None:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_FLOAT,
            record_number=record_number,
            field_name=field_name,
        )
    try:
        value = float(token)
    except ValueError:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_FLOAT,
            record_number=record_number,
            field_name=field_name,
        )
    if not isfinite(value):
        _raise(
            HistoricalMarketDataFailureCode.INVALID_FLOAT,
            record_number=record_number,
            field_name=field_name,
        )
    return value


def _event_from_row(row: list[str], *, record_number: int) -> MarketDataEnvelope:
    if row[0] != "1":
        _raise(
            HistoricalMarketDataFailureCode.INVALID_SCHEMA_VERSION,
            record_number=record_number,
            field_name="schema_version",
        )
    try:
        venue = VenueId(row[1])
    except IdentityValidationError:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_IDENTITY,
            record_number=record_number,
            field_name="venue",
        )
    try:
        instrument = Instrument(venue=venue, symbol=row[2])
    except IdentityValidationError:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_IDENTITY,
            record_number=record_number,
            field_name="symbol",
        )
    interval_start = _parse_datetime(
        row[3],
        record_number=record_number,
        field_name="interval_start",
    )
    interval_end = _parse_datetime(
        row[4],
        record_number=record_number,
        field_name="interval_end",
    )
    if row[5] != Adjustment.RAW.value:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_ADJUSTMENT,
            record_number=record_number,
            field_name="adjustment",
        )
    values = {
        name: _parse_float(
            row[index],
            record_number=record_number,
            field_name=name,
        )
        for index, name in enumerate(("open", "high", "low", "close", "volume"), start=6)
    }
    if interval_start >= interval_end:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_BAR,
            record_number=record_number,
            field_name="interval_end",
        )
    if values["low"] > values["high"]:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_BAR,
            record_number=record_number,
            field_name="high",
        )
    if not values["low"] <= values["open"] <= values["high"]:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_BAR,
            record_number=record_number,
            field_name="open",
        )
    if not values["low"] <= values["close"] <= values["high"]:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_BAR,
            record_number=record_number,
            field_name="close",
        )
    if values["volume"] < 0.0:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_BAR,
            record_number=record_number,
            field_name="volume",
        )
    try:
        bar = Bar(
            instrument=instrument,
            interval_start=interval_start,
            interval_end=interval_end,
            adjustment=Adjustment.RAW,
            open=values["open"],
            high=values["high"],
            low=values["low"],
            close=values["close"],
            volume=values["volume"],
        )
    except MarketDataValidationError as error:
        raise AssertionError("prevalidated Bar construction unexpectedly failed") from error
    try:
        source = SourceId(row[11])
    except MarketDataValidationError:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_IDENTITY,
            record_number=record_number,
            field_name="source",
        )
    source_sequence = _parse_unsigned(
        row[12],
        record_number=record_number,
        field_name="source_sequence",
    )
    revision = _parse_unsigned(
        row[13],
        record_number=record_number,
        field_name="revision",
    )
    available_at = _parse_datetime(
        row[14],
        record_number=record_number,
        field_name="available_at",
    )
    if available_at < interval_end:
        _raise(
            HistoricalMarketDataFailureCode.INVALID_ENVELOPE,
            record_number=record_number,
            field_name="available_at",
        )
    try:
        return MarketDataEnvelope(
            payload=bar,
            source=source,
            available_at=available_at,
            source_sequence=source_sequence,
            revision=revision,
        )
    except MarketDataValidationError as error:
        raise AssertionError("prevalidated envelope construction unexpectedly failed") from error


def _validate_complete_history(
    events: tuple[MarketDataEnvelope, ...],
    record_numbers: tuple[int, ...],
) -> None:
    record_keys: dict[object, int] = {}
    emission_keys: dict[object, int] = {}
    order_keys: dict[object, int] = {}
    histories: dict[MarketDataLogicalKey, list[tuple[int, MarketDataEnvelope]]] = {}
    for record_number, event in zip(record_numbers, events, strict=True):
        if event.record_key in record_keys:
            _raise(
                HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
                record_number=record_number,
            )
        record_keys[event.record_key] = record_number
        if event.emission_key in emission_keys:
            _raise(
                HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
                record_number=record_number,
                field_name="source_sequence",
            )
        emission_keys[event.emission_key] = record_number
        key = admission_order_key(event)
        if key in order_keys:
            _raise(
                HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
                record_number=record_number,
            )
        order_keys[key] = record_number
        histories.setdefault(event.logical_key, []).append((record_number, event))
    for logical_key in sorted(histories):
        history = sorted(histories[logical_key], key=lambda item: item[1].revision)
        for index in range(1, len(history)):
            _, earlier = history[index - 1]
            later_record, later = history[index]
            if later.available_at < earlier.available_at:
                _raise(
                    HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
                    record_number=later_record,
                    field_name="available_at",
                )
            if later.source_sequence <= earlier.source_sequence:
                _raise(
                    HistoricalMarketDataFailureCode.CANONICAL_BATCH_CONFLICT,
                    record_number=later_record,
                    field_name="source_sequence",
                )
    try:
        validate_market_data_batch(events)
    except MarketDataValidationError as error:
        raise AssertionError("prevalidated complete history unexpectedly failed") from error


def _same_capture_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _classify_open_failure(
    path: Path,
    before: os.stat_result,
) -> HistoricalMarketDataFailureCode:
    try:
        after = path.lstat()
    except FileNotFoundError:
        return HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ
    except OSError:
        return HistoricalMarketDataFailureCode.SOURCE_UNREADABLE
    return (
        HistoricalMarketDataFailureCode.SOURCE_UNREADABLE
        if _same_capture_stat(before, after)
        else HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ
    )


def _capture_descriptor(
    descriptor: int,
    *,
    path: Path,
    before: os.stat_result,
) -> bytes:
    try:
        descriptor_before = os.fstat(descriptor)
    except OSError:
        _raise(HistoricalMarketDataFailureCode.SOURCE_UNREADABLE)
    if (
        not stat.S_ISREG(descriptor_before.st_mode)
        or descriptor_before.st_dev != before.st_dev
        or descriptor_before.st_ino != before.st_ino
        or descriptor_before.st_size != before.st_size
        or descriptor_before.st_mtime_ns != before.st_mtime_ns
    ):
        _raise(HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ)
    chunks: list[bytes] = []
    remaining = PHASE1_OHLCV_MAX_BYTES + 1
    try:
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        _raise(HistoricalMarketDataFailureCode.SOURCE_UNREADABLE)
    content = b"".join(chunks)
    if len(content) > PHASE1_OHLCV_MAX_BYTES:
        _raise(HistoricalMarketDataFailureCode.SOURCE_TOO_LARGE)
    try:
        descriptor_after = os.fstat(descriptor)
        path_after = path.lstat()
    except OSError:
        _raise(HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ)
    if (
        not _same_capture_stat(descriptor_before, descriptor_after)
        or not _same_capture_stat(descriptor_after, path_after)
        or len(content) != descriptor_after.st_size
    ):
        _raise(HistoricalMarketDataFailureCode.SOURCE_CHANGED_DURING_READ)
    return content
