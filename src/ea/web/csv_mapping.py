"""Explicit CSV column mapping into one strict Phase 1 OHLCV capture.

The controlled upload store accepts only the strict profile's own header, units
and timestamps. A real vendor export is the same information under other column
names, another timestamp convention and another zone. This module is the one
explicit bridge between the two: the caller declares, field by field, which
source column carries which strict field, which exact allowlisted format each
timestamp column uses, and which timezone the values are in. Nothing is inferred
from the data, and no value is dropped, filled, resampled or corrected.

It is a pure function of its arguments: no HTTP handler, no workspace, no store,
no durable effect, and no second validator. The normalized capture is re-decoded
in full by the same strict Phase 1 decoder the rest of the product reads
through, so a returned capture is one the engine already accepts, and every data
defect is reported in the decoder's own failure vocabulary.

The raw bytes are never consumed: the caller keeps them, and this result records
their digest and size. Integration must retain those original bytes as the raw
source rather than storing the normalized capture as if it were what arrived.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from hashlib import sha256
from typing import Any, NoReturn, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ea.core.market_data import MarketDataEnvelope
from ea.data.historical import _HEADER as STRICT_HEADER
from ea.data.historical import (
    PHASE1_OHLCV_MAX_FIELD_LENGTH,
    HistoricalMarketDataError,
    _contains_bare_carriage_return,
    _decode_phase1_capture,
)
from ea.web.datasets import _coverage
from ea.web.uploads import (
    AVAILABILITY_SIMULATED,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_RECORDS,
    PREVIEW_SAMPLE_ROWS,
    UploadError,
    _policy_document,
    _strict_failure,
)

CSV_MAPPING_SCHEMA = "ea.local-csv-mapping.v1"

# The exact form the strict profile requires. It is produced, never accepted as
# a caller-supplied pattern.
STRICT_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_SOURCE_COLUMNS = 128

# The source and the normalized capture carry separate quotas. The source is the
# caller's own bound; a capture that expands past the store's bound could never be
# stored, so it is refused instead of being silently oversized. The source header
# count is checked before any per-column indexing, and no process-global csv limit
# is ever adjusted: the stdlib field limit surfaces as a stable refusal instead.
MAX_CAPTURE_BYTES = MAX_UPLOAD_BYTES

# Only these strict fields may be declared as constants: they describe the
# capture rather than carry an observation. Every other field is an economic or
# sequence value and must come from a mapped source column.
_METADATA_FIELDS = ("schema_version", "venue", "symbol", "adjustment", "source", "revision")
_TIMESTAMP_FIELDS = ("interval_start", "interval_end", "available_at")

_UTC_ZULU = "utc-zulu"
_OFFSET = "offset"
_NAIVE = "naive"

_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_CLOCK = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_MICROS = r"\.[0-9]{6}"
_OFFSET_SUFFIX = r"[+-][0-9]{2}:[0-9]{2}"


@dataclass(frozen=True, slots=True)
class _TimestampFormat:
    """One exact allowlisted form: an anchored pattern and its strptime form."""

    pattern: re.Pattern[str]
    strptime: str
    family: str


def _form(pattern: str, strptime: str, family: str) -> _TimestampFormat:
    return _TimestampFormat(re.compile(pattern, flags=re.ASCII), strptime, family)


# The closed allowlist. A partial date, a date-only value and a caller-supplied
# pattern are all absent here, so they are refused rather than guessed at.
_TIME_FORMATS: dict[str, _TimestampFormat] = {
    "utc-microseconds": _form(f"{_DATE}T{_CLOCK}{_MICROS}Z", "%Y-%m-%dT%H:%M:%S.%fZ", _UTC_ZULU),
    "utc-seconds": _form(f"{_DATE}T{_CLOCK}Z", "%Y-%m-%dT%H:%M:%SZ", _UTC_ZULU),
    "offset-microseconds": _form(
        f"{_DATE}T{_CLOCK}{_MICROS}{_OFFSET_SUFFIX}",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        _OFFSET,
    ),
    "offset-seconds": _form(
        f"{_DATE}T{_CLOCK}{_OFFSET_SUFFIX}",
        "%Y-%m-%dT%H:%M:%S%z",
        _OFFSET,
    ),
    "naive-microseconds": _form(f"{_DATE}T{_CLOCK}{_MICROS}", "%Y-%m-%dT%H:%M:%S.%f", _NAIVE),
    "naive-seconds": _form(f"{_DATE}T{_CLOCK}", "%Y-%m-%dT%H:%M:%S", _NAIVE),
}

TIME_FORMAT_IDS = tuple(_TIME_FORMATS)

# The normalized capture is UTC interval data with explicit start and end, which
# is the same time semantics the controlled upload store records for a capture.
_TIME_SEMANTICS = {
    "timestamp_role": "interval",
    "timezone": "UTC",
    "bar_boundary": "explicit-interval-start-end",
}

_LIMITATIONS = (
    "The normalized capture is not the raw source. The caller keeps the original bytes, whose "
    "SHA-256 and byte count this result records, and must retain those bytes as the raw source "
    "rather than storing the normalized capture as if it had arrived that way.",
    "Nothing is dropped, filled, interpolated, resampled or corrected. Every strict field is "
    "either the caller's declared constant or the source token itself; only the declared "
    "timestamps and the declared simulated availability are re-rendered.",
    "Availability is not verified point-in-time evidence. An asserted or simulated value is "
    "never presented as PIT verified.",
    f"The preview samples at most {PREVIEW_SAMPLE_ROWS} normalized rows. Only the full strict "
    "re-decode of the whole capture decides validity; a sample is never evidence of validity.",
    "The declared venue, symbol, source, revision, adjustment and schema version are the "
    "caller's assertion, not host-verified facts.",
    "Only the fixed strict UTC 15-column OHLCV profile is produced. There is no second "
    "validator: the normalized capture is decoded by the same strict Phase 1 decoder.",
)


def _fail(
    code: str,
    message: str,
    *,
    record_number: int | None = None,
    field_name: str | None = None,
) -> NoReturn:
    """Reuse the upload store's one error vocabulary for every rejection."""
    raise UploadError(code, message, record_number=record_number, field_name=field_name)


@dataclass(frozen=True, slots=True)
class _TimePlan:
    """One validated time declaration: which field uses which exact form, in which zone."""

    ids: tuple[tuple[str, str], ...]
    formats: dict[str, _TimestampFormat]
    timezone: str
    zone: tzinfo
    localized: bool


@dataclass(frozen=True, slots=True)
class _Plan:
    """One validated declaration resolved against one concrete source header."""

    header: tuple[str, ...]
    columns: dict[str, str]
    constants: dict[str, str]
    index_by_name: dict[str, int]
    time: _TimePlan


@dataclass(frozen=True, slots=True)
class MappedCsvCaptureV1:
    """One fully validated strict capture plus the declarations that produced it.

    ``strict_csv`` is the bytes the engine reads. ``source_bytes_sha256`` and
    ``source_byte_count`` describe the caller's original bytes, which this module
    never consumed or stored; ``capture_sha256`` is the digest of the normalized
    bytes and ``data_sha256`` is the strict decoder's own semantic fingerprint.

    The evidence dictionaries are kept as canonical JSON text and handed out as
    fresh copies, so this result is immutable in practice: editing anything it
    returns cannot change what it subsequently reports.
    """

    strict_csv: bytes
    profile: str
    source_byte_count: int
    source_bytes_sha256: str
    source_record_count: int
    capture_byte_count: int
    capture_sha256: str
    data_sha256: str
    record_count: int
    venue: str
    symbol: str
    mapping_columns: tuple[tuple[str, str], ...]
    mapping_constants: tuple[tuple[str, str], ...]
    time_formats: tuple[tuple[str, str], ...]
    timezone: str
    policy_json: str
    quality_json: str
    preview_json: str

    @property
    def availability_policy(self) -> dict[str, Any]:
        """A fresh copy of the availability policy: editing it changes nothing here."""
        return cast("dict[str, Any]", json.loads(self.policy_json))

    @property
    def quality(self) -> dict[str, Any]:
        """A fresh copy of the coverage evidence: editing it changes nothing here."""
        return cast("dict[str, Any]", json.loads(self.quality_json))

    @property
    def preview(self) -> dict[str, Any]:
        """A fresh copy of the bounded preview: editing it changes nothing here."""
        return cast("dict[str, Any]", json.loads(self.preview_json))

    def document(self) -> dict[str, Any]:
        return {
            "schema": CSV_MAPPING_SCHEMA,
            "profile": self.profile,
            "strict_header": list(STRICT_HEADER),
            "strict_time_format": STRICT_DATETIME_FORMAT,
            "source": {
                "byte_count": self.source_byte_count,
                "sha256": self.source_bytes_sha256,
                "record_count": self.source_record_count,
                # This result stored nothing: the caller holds the raw bytes and
                # must keep holding them, because the capture is not the source.
                "retention_required": True,
                "stored_here": False,
                "retention_owner": "caller",
            },
            "capture": {
                "byte_count": self.capture_byte_count,
                "sha256": self.capture_sha256,
                "data_sha256": self.data_sha256,
                "record_count": self.record_count,
            },
            "instrument": {"venue": self.venue, "symbol": self.symbol},
            "mapping": {
                "columns": dict(self.mapping_columns),
                "constants": dict(self.mapping_constants),
            },
            "time": {
                "timezone": self.timezone,
                "formats": dict(self.time_formats),
                "target_format": STRICT_DATETIME_FORMAT,
                "time_semantics": dict(_TIME_SEMANTICS),
            },
            "availability_policy": self.availability_policy,
            "quality": self.quality,
            "preview": self.preview,
            "limitations": list(_LIMITATIONS),
        }


def map_csv_capture(
    content: object,
    *,
    mapping: object,
    time_declaration: object,
    availability_policy: object = None,
    latency_seconds: object = None,
) -> MappedCsvCaptureV1:
    """Map one explicitly declared source CSV into one strict Phase 1 capture.

    ``content`` is the caller's original UTF-8 bytes, bounded exactly as an
    upload is. ``mapping`` declares every strict field as either a source column
    or, for the metadata fields only, a constant. ``time_declaration`` declares
    the timezone and the exact format of every timestamp field in use.
    ``availability_policy`` has no default: the caller states how the capture
    became available, or the mapping is refused.

    Every rejection raises :class:`UploadError` with a stable code, so a returned
    result is always a capture the strict decoder accepted in full.
    """
    payload = _check_source(content)
    policy = _policy_document(availability_policy, latency_seconds)
    simulated = policy["id"] == AVAILABILITY_SIMULATED
    latency = timedelta(seconds=policy["latency_seconds"]) if simulated else None

    rows = _read_source_rows(payload)
    header = _source_header(rows)
    columns, constants = _declaration(mapping, header, simulated=simulated)
    time_plan = _time_declaration(time_declaration, simulated=simulated)
    plan = _Plan(
        header=header,
        columns=columns,
        constants=constants,
        index_by_name={name: index for index, name in enumerate(header)},
        time=time_plan,
    )

    capture, sampled = _normalized_capture(rows[1:], plan, latency=latency)
    try:
        dataset = _decode_phase1_capture(capture, replay_window=None)
    except HistoricalMarketDataError as error:
        raise _strict_failure(error) from None
    instruments = {_instrument_key(event) for event in dataset.selection.events}
    if len(instruments) != 1:
        _fail("invalid_identity", "a mapped capture requires exactly one instrument")
    venue, symbol = next(iter(instruments))
    quality = _coverage(dataset)
    return MappedCsvCaptureV1(
        strict_csv=capture,
        profile=dataset.profile,
        source_byte_count=len(payload),
        source_bytes_sha256=sha256(payload).hexdigest(),
        source_record_count=len(rows) - 1,
        capture_byte_count=len(capture),
        capture_sha256=sha256(capture).hexdigest(),
        data_sha256=dataset.selection.fingerprint.sha256.value,
        record_count=dataset.selection.fingerprint.record_count,
        venue=venue,
        symbol=symbol,
        mapping_columns=tuple(sorted(columns.items())),
        mapping_constants=tuple(sorted(constants.items())),
        time_formats=time_plan.ids,
        timezone=time_plan.timezone,
        policy_json=_canonical_json(policy),
        quality_json=_canonical_json(quality),
        preview_json=_canonical_json(_preview(header, sampled, len(rows) - 1, quality)),
    )


def _canonical_json(value: object) -> str:
    """Canonical JSON text: one stable spelling, and a copy on every read."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _instrument_key(event: MarketDataEnvelope) -> tuple[str, str]:
    instrument = event.payload.instrument
    return (instrument.venue.code, instrument.symbol)


def _preview(
    header: tuple[str, ...],
    sampled: tuple[dict[str, Any], ...],
    record_count: int,
    quality: dict[str, Any],
) -> dict[str, Any]:
    """A bounded sample of the normalized rows beside the full validation verdict."""
    return {
        "source_header": list(header),
        "rows": list(sampled),
        "row_count_sampled": len(sampled),
        "record_count": record_count,
        "truncated": record_count > len(sampled),
        "full_validation": "passed",
        "valid": True,
        "quality": quality,
        "limitations": list(_LIMITATIONS),
    }


def _check_source(content: object) -> bytes:
    """The upload store's own request bounds, before anything is decoded."""
    if type(content) is not bytes:
        _fail("upload_invalid", "a mapped CSV must be raw bytes")
    if not content:
        _fail("empty_source", "the source CSV is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        message = f"the source CSV exceeds the {MAX_UPLOAD_BYTES}-byte limit"
        _fail("source_too_large", message)
    return content


def _read_source_rows(payload: bytes) -> list[list[str]]:
    """Bounded structural parse of the source, in the strict decoder's own dialect."""
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("invalid_encoding", "the source CSV is not strict UTF-8")
    if text.startswith("﻿"):
        _fail("invalid_encoding", "the source CSV starts with a byte-order mark")
    if "\x00" in text or _contains_bare_carriage_return(text):
        _fail("invalid_file_character", "the source CSV contains a control character")
    reader = csv.reader(
        io.StringIO(text, newline=""),
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
            if len(rows) > MAX_UPLOAD_RECORDS:
                message = f"the source CSV holds more than {MAX_UPLOAD_RECORDS} records"
                _fail("too_many_records", message, record_number=len(rows) + 1)
            rows.append(row)
    except csv.Error:
        _fail(
            "malformed_csv",
            "the source rows are not well-formed CSV",
            record_number=len(rows) + 1,
        )
    return rows


def _source_header(rows: list[list[str]]) -> tuple[str, ...]:
    """One unambiguous source header, or a refusal to map against it."""
    header = tuple(rows[0]) if rows else ()
    if not header:
        _fail("invalid_header", "the source CSV has no header row", record_number=1)
    if len(header) > MAX_SOURCE_COLUMNS:
        message = f"the header has {len(header)} columns, above the {MAX_SOURCE_COLUMNS} limit"
        _fail("invalid_header", message, record_number=1)
    for name in header:
        if len(name) > PHASE1_OHLCV_MAX_FIELD_LENGTH:
            _fail(
                "invalid_record_shape",
                "a header field is longer than the strict field limit",
                record_number=1,
                field_name=name[:64],
            )
        if not name or name.strip() != name:
            _fail(
                "invalid_header",
                "every header name must be one non-empty name without outer whitespace",
                record_number=1,
                field_name=name[:64],
            )
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        message = f"the header repeats {', '.join(duplicates)}; a repeated column cannot be mapped"
        _fail("invalid_header", message, record_number=1)
    if len(rows) == 1:
        _fail(
            "invalid_record_shape",
            "the source CSV has a header and no data records",
            record_number=2,
        )
    return header


def _declaration(
    mapping: object, header: tuple[str, ...], *, simulated: bool
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve one complete field-by-field declaration against the real header."""
    if type(mapping) is not dict or set(mapping) != {"columns", "constants"}:
        _fail("mapping_declaration", "mapping must declare exactly 'columns' and 'constants'")
    columns = mapping["columns"]
    constants = mapping["constants"]
    if type(columns) is not dict or type(constants) is not dict:
        _fail("mapping_declaration", "'columns' and 'constants' must each be an object")
    declared_columns: dict[str, str] = {}
    declared_constants: dict[str, str] = {}
    for name, source in columns.items():
        declared_columns[_destination_field(name, "columns")] = source
    for name, value in constants.items():
        declared_constants[_destination_field(name, "constants")] = value
    for name in declared_constants:
        if name not in _METADATA_FIELDS:
            _fail(
                "mapping_declaration",
                f"{name} carries an observation, not metadata; it must be mapped from a source "
                "column and never fixed as a constant",
            )
    for name, value in declared_constants.items():
        if type(value) is not str or not value:
            _fail(
                "mapping_declaration",
                f"the constant declared for {name} must be one non-empty text value",
                field_name=name,
            )
        if len(value) > PHASE1_OHLCV_MAX_FIELD_LENGTH:
            _fail(
                "mapping_declaration",
                f"the constant declared for {name} is longer than the strict profile's field "
                "limit, so it could never be written as a strict field",
                field_name=name,
            )
    overlap = sorted(set(declared_columns) & set(declared_constants))
    if overlap:
        message = f"{', '.join(overlap)} is declared twice; declare each field exactly once"
        _fail("mapping_declaration", message)
    if simulated and "available_at" in declared_columns:
        _fail(
            "mapping_declaration",
            "the simulated availability policy derives available_at from interval_end plus the "
            "declared latency, so a mapped available_at column must not be declared",
        )
    if not simulated and "available_at" not in declared_columns:
        _fail(
            "mapping_declaration",
            "the provided availability policy requires available_at to be mapped from a source "
            "column and declared with its own exact format",
        )
    # Under the simulated policy available_at is derived from interval_end plus the
    # declared latency, so it is the one strict field that is deliberately not
    # declared here; under the provided policy every field must be declared.
    required = [name for name in STRICT_HEADER if not (simulated and name == "available_at")]
    declared = set(declared_columns) | set(declared_constants)
    missing = [name for name in required if name not in declared]
    if missing:
        message = f"every strict field must be declared explicitly; missing {', '.join(missing)}"
        _fail("mapping_declaration", message)
    for name, source in declared_columns.items():
        if type(source) is not str:
            _fail(
                "mapping_declaration",
                f"the source column declared for {name} must be one header name",
                field_name=name,
            )
        if source not in header:
            _fail(
                "mapping_column_missing",
                f"the mapping sends {name} to a source column this header does not have",
                field_name=source,
            )
    return declared_columns, declared_constants


def _destination_field(name: object, section: str) -> str:
    """One declared destination must be an exact strict field name."""
    if type(name) is not str or name not in STRICT_HEADER:
        _fail(
            "mapping_declaration",
            f"{name!r} in '{section}' is not one of the strict fields "
            f"{', '.join(STRICT_HEADER)}",
        )
    return name


def _time_declaration(declaration: object, *, simulated: bool) -> _TimePlan:
    """One explicit timezone and one exact allowlisted form per timestamp field."""
    if type(declaration) is not dict or set(declaration) != {"timezone", "formats"}:
        _fail(
            "mapping_declaration",
            "time_declaration must declare exactly 'timezone' and 'formats'",
        )
    zone_name = declaration["timezone"]
    declared = declaration["formats"]
    if type(declared) is not dict:
        _fail("mapping_declaration", "'formats' must map each timestamp field to one exact format")
    for name in declared:
        if type(name) is not str or name not in _TIMESTAMP_FIELDS:
            _fail(
                "mapping_declaration",
                f"{name!r} is not one of the strict timestamp fields "
                f"{', '.join(_TIMESTAMP_FIELDS)}",
            )
    needed = ["interval_start", "interval_end"] if simulated else list(_TIMESTAMP_FIELDS)
    missing = [name for name in needed if name not in declared]
    if missing:
        _fail(
            "mapping_declaration",
            "every timestamp field in use must declare its exact format; "
            f"missing {', '.join(missing)}",
        )
    unused = sorted(set(declared) - set(needed))
    if unused:
        _fail(
            "mapping_declaration",
            f"{', '.join(unused)} is not a timestamp field this policy uses; a declared but "
            "unused format is a contradiction, not a hint",
        )
    if type(zone_name) is not str or not zone_name:
        _fail(
            "invalid_timezone",
            "timezone must be 'UTC' or one IANA zone name; a naive timestamp names no instant "
            "until its timezone is declared",
        )
    ids: list[tuple[str, str]] = []
    formats: dict[str, _TimestampFormat] = {}
    for name in needed:
        identifier = declared[name]
        if type(identifier) is not str or identifier not in _TIME_FORMATS:
            _fail(
                "invalid_time_format",
                f"the format declared for {name} must be one of the exact supported forms "
                f"{', '.join(TIME_FORMAT_IDS)}; a pattern is never guessed from the data",
                field_name=name,
            )
        ids.append((name, identifier))
        formats[name] = _TIME_FORMATS[identifier]
    localized = False
    for name, _identifier in ids:
        family = formats[name].family
        if family == _NAIVE:
            localized = localized or zone_name != "UTC"
            continue
        if zone_name != "UTC":
            carried = "an explicit numeric offset" if family == _OFFSET else "the literal Z"
            _fail(
                "invalid_timezone",
                f"a {name} value in that form already carries {carried}; declaring timezone "
                f"{zone_name!r} alongside it contradicts the value",
                field_name=name,
            )
    zone: tzinfo = UTC
    if localized:
        zone = _resolve_zone(zone_name)
    return _TimePlan(
        ids=tuple(ids),
        formats=formats,
        timezone=zone_name,
        zone=zone,
        localized=localized,
    )


def _resolve_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        _fail(
            "invalid_timezone",
            "timezone must be 'UTC' or one IANA zone name this host can resolve",
        )


def _normalized_capture(
    rows: list[list[str]], plan: _Plan, *, latency: timedelta | None
) -> tuple[bytes, tuple[dict[str, Any], ...]]:
    """Write the strict capture record by record, keeping a bounded sample.

    The capture is materialized incrementally and its exact UTF-8 size is checked
    as it grows, so an expansion past the store's own bound is refused rather than
    discovered after the whole capture exists.
    """
    column_count = len(plan.header)
    buffered = io.StringIO(newline="")
    writer = csv.writer(buffered, dialect="excel", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    chunks = [_row_bytes(writer, buffered, STRICT_HEADER)]
    written = len(chunks[0])
    sampled: list[dict[str, Any]] = []
    for record_number, row in enumerate(rows, start=2):
        if len(row) != column_count:
            _fail(
                "invalid_record_shape",
                f"the row does not have the header's {column_count} columns",
                record_number=record_number,
            )
        # Every source field is bounded, including columns no declaration uses:
        # an ignored column is still source data and is still held to the limit.
        for position, token in enumerate(row):
            if len(token) > PHASE1_OHLCV_MAX_FIELD_LENGTH:
                _fail(
                    "invalid_record_shape",
                    "a source field is longer than the strict profile's field limit",
                    record_number=record_number,
                    field_name=plan.header[position],
                )
        tokens = _strict_tokens(row, record_number=record_number, plan=plan, latency=latency)
        chunk = _row_bytes(writer, buffered, [tokens[name] for name in STRICT_HEADER])
        written += len(chunk)
        if written > MAX_CAPTURE_BYTES:
            _fail(
                "capture_too_large",
                f"the normalized capture would exceed the {MAX_CAPTURE_BYTES}-byte store limit; "
                "the source is inside its own quota, but its strict form is not storable",
                record_number=record_number,
            )
        chunks.append(chunk)
        if len(sampled) < PREVIEW_SAMPLE_ROWS:
            sampled.append({"record_number": record_number, **tokens})
    return b"".join(chunks), tuple(sampled)


def _row_bytes(writer: Any, buffered: io.StringIO, values: Iterable[str]) -> bytes:
    """One CSV record as its exact UTF-8 bytes, written and then buffered away."""
    buffered.seek(0)
    buffered.truncate(0)
    writer.writerow(values)
    return buffered.getvalue().encode("utf-8")


def _strict_tokens(
    row: list[str], *, record_number: int, plan: _Plan, latency: timedelta | None
) -> dict[str, str]:
    """One strict row: declared constants, mapped tokens, declared time conversions."""
    tokens = dict(plan.constants)
    parsed: dict[str, datetime] = {}
    for name, source in plan.columns.items():
        token = row[plan.index_by_name[source]]
        if name not in _TIMESTAMP_FIELDS:
            tokens[name] = token
            continue
        try:
            value = _parse_timestamp(
                token,
                record_number=record_number,
                field=name,
                fmt=plan.time.formats[name],
                time=plan.time,
            )
            stamp = _strict_stamp(value)
        except OverflowError:
            _fail(
                "invalid_timestamp",
                "the value is outside the range the strict profile can represent as UTC",
                record_number=record_number,
                field_name=name,
            )
        parsed[name] = value
        tokens[name] = stamp
    if latency is not None:
        try:
            derived = parsed["interval_end"] + latency
            tokens["available_at"] = _strict_stamp(derived)
        except OverflowError:
            _fail(
                "invalid_timestamp",
                "the declared latency moves available_at past the range the strict profile can "
                "represent as UTC",
                record_number=record_number,
                field_name="available_at",
            )
    return tokens


def _strict_stamp(value: datetime) -> str:
    """The one form the strict profile accepts, always in UTC."""
    return value.astimezone(UTC).strftime(STRICT_DATETIME_FORMAT)


def _parse_timestamp(
    token: str, *, record_number: int, field: str, fmt: _TimestampFormat, time: _TimePlan
) -> datetime:
    """Parse one declared timestamp, or refuse it. Never guesses and never defaults."""
    if fmt.pattern.fullmatch(token) is None:
        _fail(
            "invalid_timestamp",
            f"the value does not have the exact declared {fmt.strptime!r} form",
            record_number=record_number,
            field_name=field,
        )
    try:
        value = datetime.strptime(token, fmt.strptime)
    except ValueError:
        _fail(
            "invalid_timestamp",
            f"the value is not a valid {fmt.strptime!r} date and time",
            record_number=record_number,
            field_name=field,
        )
    if fmt.family == _UTC_ZULU:
        return value.replace(tzinfo=UTC)
    if fmt.family == _OFFSET:
        return value.astimezone(UTC)
    if not time.localized:
        return value.replace(tzinfo=UTC)
    return _localize(value, record_number=record_number, field=field, time=time)


def _localize(naive: datetime, *, record_number: int, field: str, time: _TimePlan) -> datetime:
    """Interpret one naive value in its declared zone, refusing gaps and folds."""
    first = naive.replace(tzinfo=time.zone, fold=0)
    second = naive.replace(tzinfo=time.zone, fold=1)
    if first.astimezone(UTC).astimezone(time.zone).replace(tzinfo=None) != naive:
        _fail(
            "invalid_timestamp",
            f"the value names a local time that its declared timezone {time.timezone} skips, "
            "so it is not one instant",
            record_number=record_number,
            field_name=field,
        )
    if first.utcoffset() != second.utcoffset():
        _fail(
            "invalid_timestamp",
            f"the value is ambiguous in its declared timezone {time.timezone}; it names two "
            "instants and no offset is declared to choose between them",
            record_number=record_number,
            field_name=field,
        )
    return first.astimezone(UTC)
