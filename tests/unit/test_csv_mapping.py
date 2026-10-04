"""Explicit CSV column mapping: declared renames and declared time, strict output.

The fixtures are vendor-shaped on purpose: the same observations under other
column names, another timestamp convention and another zone. The point of every
acceptance case is that the mapped capture is byte-identical to the strict
capture the existing Phase 1 decoder issues for the same data, and that every
refusal speaks the existing vocabulary rather than a second one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from ea.data.historical import _HEADER as STRICT_HEADER
from ea.data.historical import PHASE1_OHLCV_MAX_FIELD_LENGTH, read_full_capture_phase1_ohlcv_csv
from ea.web import csv_mapping as csv_mapping_module
from ea.web.csv_mapping import MappedCsvCaptureV1, map_csv_capture
from ea.web.datasets import _coverage
from ea.web.uploads import (
    AVAILABILITY_PROVIDED,
    AVAILABILITY_SIMULATED,
    MAX_UPLOAD_BYTES,
    PREVIEW_SAMPLE_ROWS,
    UploadError,
)

SHANGHAI = "Asia/Shanghai"
NEW_YORK = "America/New_York"
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"
SECONDS_ZULU = "%Y-%m-%dT%H:%M:%SZ"
LOCAL = "%Y-%m-%dT%H:%M:%S"
START = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
_OFFSET_ZONE = timezone(timedelta(hours=5, minutes=30))

VENDOR_FIELDS = (
    "symbol",
    "venue",
    "bar_start",
    "bar_end",
    "o",
    "h",
    "l",
    "c",
    "v",
    "sequence",
    "rev",
    "available",
)

MAPPING: dict[str, Any] = {
    "columns": {
        "venue": "venue",
        "symbol": "symbol",
        "interval_start": "bar_start",
        "interval_end": "bar_end",
        "open": "o",
        "high": "h",
        "low": "l",
        "close": "c",
        "volume": "v",
        "source_sequence": "sequence",
        "revision": "rev",
        "available_at": "available",
    },
    "constants": {"schema_version": "1", "adjustment": "raw", "source": "vendor.a"},
}

TIME: dict[str, Any] = {
    "timezone": SHANGHAI,
    "formats": {
        "interval_start": "naive-seconds",
        "interval_end": "naive-seconds",
        "available_at": "naive-seconds",
    },
}

TIME_NEW_YORK: dict[str, Any] = {
    "timezone": NEW_YORK,
    "formats": dict(TIME["formats"]),
}

TIME_OFFSET: dict[str, Any] = {
    "timezone": "UTC",
    "formats": {
        "interval_start": "offset-seconds",
        "interval_end": "offset-seconds",
        "available_at": "offset-seconds",
    },
}

# Under the simulated policy available_at is derived, so only the two bar stamps
# are declared here.
TIME_ZULU_SECONDS: dict[str, Any] = {
    "timezone": "UTC",
    "formats": {"interval_start": "utc-seconds", "interval_end": "utc-seconds"},
}


def _skip_without_zone(name: str) -> None:
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        pytest.skip(f"this host has no tz database entry for {name}")


def _local(value: datetime) -> str:
    return value.astimezone(ZoneInfo(SHANGHAI)).strftime(LOCAL)


def _naive(value: datetime) -> str:
    return value.strftime(LOCAL)


def _zulu_seconds(value: datetime) -> str:
    return value.strftime(SECONDS_ZULU)


def _offset(value: datetime) -> str:
    """One exact ISO offset form; the offset itself is part of every value."""
    return value.astimezone(_OFFSET_ZONE).strftime(LOCAL) + "+05:30"


def _vendor_row(index: int = 0, *, stamp: Any = None, **overrides: str) -> dict[str, str]:
    """One vendor row: an hourly bar whose availability is 45s after its end."""
    stamp = _local if stamp is None else stamp
    start = START + timedelta(hours=index)
    end = start + timedelta(hours=1)
    values = {
        "symbol": "AAPL",
        "venue": "XNAS",
        "bar_start": stamp(start),
        "bar_end": stamp(end),
        "o": "100.0",
        "h": "101.0",
        "l": "99.0",
        "c": "100.5",
        "v": "10.0",
        "sequence": str(index),
        "rev": "0",
        "available": stamp(end + timedelta(seconds=45)),
    }
    values.update(overrides)
    return values


def _vendor(*rows: dict[str, str], drop: tuple[str, ...] = ()) -> bytes:
    fields = tuple(name for name in VENDOR_FIELDS if name not in drop)
    body = "".join(",".join(row[name] for name in fields) + "\n" for row in rows)
    return (",".join(fields) + "\n" + body).encode()


def _vendor_with_extra(name: str, value: str) -> bytes:
    """One row carrying a column no declaration uses; it is still source data."""
    row = _vendor_row(0, **{name: value})
    header = (*VENDOR_FIELDS, name)
    body = ",".join(row[field] for field in header)
    return (",".join(header) + "\n" + body + "\n").encode()


def _strict_row(
    index: int = 0,
    *,
    symbol: str = "AAPL",
    sequence: str | None = None,
    volume: str = "10.0",
    latency: int = 45,
) -> list[str]:
    start = START + timedelta(hours=index)
    end = start + timedelta(hours=1)
    return [
        "1",
        "XNAS",
        symbol,
        start.strftime(STAMP),
        end.strftime(STAMP),
        "raw",
        "100.0",
        "101.0",
        "99.0",
        "100.5",
        volume,
        "vendor.a",
        str(index) if sequence is None else sequence,
        "0",
        (end + timedelta(seconds=latency)).strftime(STAMP),
    ]


def _strict_capture(*rows: list[str]) -> bytes:
    body = "".join(",".join(row) + "\n" for row in rows)
    return (",".join(STRICT_HEADER) + "\n" + body).encode()


def _map(content: object, **overrides: Any) -> MappedCsvCaptureV1:
    arguments: dict[str, Any] = {
        "mapping": MAPPING,
        "time_declaration": TIME,
        "availability_policy": AVAILABILITY_PROVIDED,
    }
    arguments.update(overrides)
    return map_csv_capture(content, **arguments)


def _reject(content: object, **overrides: Any) -> UploadError:
    with pytest.raises(UploadError) as failure:
        _map(content, **overrides)
    return failure.value


def _without(field: str) -> dict[str, Any]:
    return {
        "columns": {k: v for k, v in MAPPING["columns"].items() if k != field},
        "constants": {k: v for k, v in MAPPING["constants"].items() if k != field},
    }


def _with_constant(field: str, value: str) -> dict[str, Any]:
    mapping = _without(field)
    mapping["constants"] = {**mapping["constants"], field: value}
    return mapping


# -- the accepted path ------------------------------------------------------


def test_renamed_columns_and_a_declared_zone_reach_the_codec_identity(tmp_path: Any) -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0), _vendor_row(1), _vendor_row(2))
    # The source really is another convention and another zone, so the equality
    # below is a conversion rather than a coincidence.
    assert "2026-01-05T22:30:00" in content.decode()
    result = _map(content)

    expected = _strict_capture(_strict_row(0), _strict_row(1), _strict_row(2))
    assert result.strict_csv == expected
    path = tmp_path / "expected.csv"
    path.write_bytes(expected)
    dataset = read_full_capture_phase1_ohlcv_csv(path)
    assert result.data_sha256 == dataset.selection.fingerprint.sha256.value
    assert result.record_count == dataset.selection.fingerprint.record_count == 3
    assert result.capture_sha256 == sha256(expected).hexdigest()
    assert result.capture_byte_count == len(expected)
    assert result.quality == _coverage(dataset)
    assert result.profile == "ea-phase1-ohlcv-csv-v1"
    assert (result.venue, result.symbol) == ("XNAS", "AAPL")

    # The original bytes are described, never consumed or re-served as raw.
    assert result.strict_csv != content
    assert result.source_bytes_sha256 == sha256(content).hexdigest()
    assert result.source_byte_count == len(content)
    assert result.source_record_count == 3
    document = result.document()
    assert document["schema"] == "ea.local-csv-mapping.v1"
    # The result stores no bytes and claims none: it records that the caller must
    # keep the original raw source, which the capture is not.
    assert document["source"]["retention_required"] is True
    assert document["source"]["stored_here"] is False
    assert document["source"]["retention_owner"] == "caller"
    assert document["time"]["timezone"] == SHANGHAI
    assert document["time"]["formats"] == TIME["formats"]
    assert document["time"]["target_format"] == STAMP
    assert document["mapping"]["columns"]["close"] == "c"
    assert document["mapping"]["constants"]["source"] == "vendor.a"
    assert document["availability_policy"]["pit_verified"] is False
    assert document["limitations"]
    preview = document["preview"]
    assert preview["full_validation"] == "passed"
    assert preview["valid"] is True
    assert preview["truncated"] is False
    assert preview["rows"][0]["interval_start"] == "2026-01-05T14:30:00.000000Z"
    assert preview["rows"][0]["available_at"] == "2026-01-05T15:30:45.000000Z"


def test_an_explicit_offset_form_is_normalized_to_utc() -> None:
    content = _vendor(*[_vendor_row(index, stamp=_offset) for index in range(2)])
    result = _map(content, time_declaration=TIME_OFFSET)
    assert result.strict_csv == _strict_capture(_strict_row(0), _strict_row(1))
    # 14:30Z is 20:00 at the value's own declared +05:30 offset.
    assert _offset(START) == "2026-01-05T20:00:00+05:30"
    assert result.timezone == "UTC"


def test_naive_values_may_declare_utc_explicitly() -> None:
    content = _vendor(*[_vendor_row(index, stamp=_naive) for index in range(2)])
    time_declaration = {
        "timezone": "UTC",
        "formats": {
            "interval_start": "naive-seconds",
            "interval_end": "naive-seconds",
            "available_at": "naive-seconds",
        },
    }
    result = _map(content, time_declaration=time_declaration)
    assert result.strict_csv == _strict_capture(_strict_row(0), _strict_row(1))


def test_provided_availability_is_mapped_and_stays_an_assertion() -> None:
    _skip_without_zone(SHANGHAI)
    result = _map(_vendor(_vendor_row(0)))
    assert result.availability_policy == {
        "id": AVAILABILITY_PROVIDED,
        "version": 1,
        "latency_seconds": None,
        "pit_verified": False,
        "evidence": "asserted-by-uploader",
    }
    assert result.preview["rows"][0]["available_at"] == "2026-01-05T15:30:45.000000Z"
    assert result.strict_csv == _strict_capture(_strict_row(0))


def test_simulated_availability_uses_the_declared_latency_from_bar_end() -> None:
    content = _vendor(
        _vendor_row(0, stamp=_zulu_seconds),
        _vendor_row(1, stamp=_zulu_seconds),
        drop=("available",),
    )
    mapping = _without("available_at")
    result = _map(
        content,
        mapping=mapping,
        time_declaration=TIME_ZULU_SECONDS,
        availability_policy=AVAILABILITY_SIMULATED,
        latency_seconds=3600,
    )
    expected = _strict_capture(_strict_row(0, latency=3600), _strict_row(1, latency=3600))
    assert result.strict_csv == expected
    assert result.availability_policy == {
        "id": AVAILABILITY_SIMULATED,
        "version": 1,
        "latency_seconds": 3600,
        "pit_verified": False,
        "evidence": "host-simulated: interval_end + 3600s",
    }
    # A declared latency of zero is explicit and legal: availability is exactly
    # the bar's own end, which is still not a point-in-time verification.
    immediate = _map(
        content,
        mapping=mapping,
        time_declaration=TIME_ZULU_SECONDS,
        availability_policy=AVAILABILITY_SIMULATED,
        latency_seconds=0,
    )
    zero = _strict_capture(_strict_row(0, latency=0), _strict_row(1, latency=0))
    assert immediate.strict_csv == zero
    assert immediate.availability_policy["pit_verified"] is False


# -- declaration refusals ---------------------------------------------------


def test_the_availability_policy_is_never_defaulted() -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0))
    simulated_mapping = _without("available_at")
    assert _reject(content, availability_policy=None).code == "upload_policy"
    assert _reject(content, availability_policy="whatever").code == "upload_policy"
    no_latency = _reject(
        content, mapping=simulated_mapping, availability_policy=AVAILABILITY_SIMULATED
    )
    assert no_latency.code == "upload_policy"
    negative = _reject(
        content,
        mapping=simulated_mapping,
        availability_policy=AVAILABILITY_SIMULATED,
        latency_seconds=-1,
    )
    assert negative.code == "upload_policy"
    # Declaring an available_at column under a derived policy would silently
    # ignore it, so the contradiction is refused instead.
    ignored = _reject(content, availability_policy=AVAILABILITY_SIMULATED, latency_seconds=60)
    assert ignored.code == "mapping_declaration"


def test_every_strict_field_must_be_declared_exactly_once() -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0))
    assert _reject(content, mapping=_without("close")).code == "mapping_declaration"
    assert _reject(content, mapping=_without("source_sequence")).code == "mapping_declaration"
    assert _reject(content, mapping=_without("available_at")).code == "mapping_declaration"
    overlap = {
        "columns": MAPPING["columns"],
        "constants": {**MAPPING["constants"], "venue": "XNAS"},
    }
    assert _reject(content, mapping=overlap).code == "mapping_declaration"
    assert _reject(content, mapping={}).code == "mapping_declaration"
    # A declaration with an unknown section: each value is an empty object, so the
    # mapping type is named explicitly rather than inferred from empty literals.
    extra: dict[str, dict[str, str]] = {"columns": {}, "constants": {}, "extra": {}}
    assert _reject(content, mapping=extra).code == "mapping_declaration"


def test_economic_and_sequence_values_are_never_constants() -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0))
    for field, constant in (
        ("close", "100.5"),
        ("open", "100.0"),
        ("volume", "10.0"),
        ("source_sequence", "0"),
        ("available_at", "2026-01-05T15:30:45.000000Z"),
    ):
        failure = _reject(content, mapping=_with_constant(field, constant))
        assert failure.code == "mapping_declaration", field


def test_unknown_fields_and_absent_columns_are_refused() -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0))
    unknown = {
        "columns": {**MAPPING["columns"], "ticker": "symbol"},
        "constants": MAPPING["constants"],
    }
    assert _reject(content, mapping=unknown).code == "mapping_declaration"
    absent = {
        "columns": {**MAPPING["columns"], "close": "last_px"},
        "constants": MAPPING["constants"],
    }
    failure = _reject(content, mapping=absent)
    assert failure.code == "mapping_column_missing"
    assert failure.field_name == "last_px"


def test_the_time_declaration_admits_no_guessing_and_no_contradiction() -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0))
    formats = TIME["formats"]
    assert _reject(content, time_declaration={"formats": formats}).code == "mapping_declaration"
    assert _reject(content, time_declaration={"timezone": SHANGHAI}).code == "mapping_declaration"
    empty = {"timezone": SHANGHAI, "formats": {}}
    assert _reject(content, time_declaration=empty).code == "mapping_declaration"
    partial = {"timezone": SHANGHAI, "formats": {"interval_start": "naive-seconds"}}
    assert _reject(content, time_declaration=partial).code == "mapping_declaration"
    for declared in ("date-only", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        # A partial or date-only form is absent from the closed allowlist, so it
        # can never be declared; only the six exact forms exist.
        unsupported = {"timezone": SHANGHAI, "formats": {**formats, "available_at": declared}}
        failure = _reject(content, time_declaration=unsupported)
        assert failure.code == "invalid_time_format", declared
        assert failure.field_name == "available_at"
    blank = _reject(content, time_declaration={"timezone": "", "formats": formats})
    assert blank.code == "invalid_timezone"
    unknown = {"timezone": "Mars/Olympus", "formats": formats}
    assert _reject(content, time_declaration=unknown).code == "invalid_timezone"
    zulu = {name: "utc-seconds" for name in formats}
    contradiction = {"timezone": SHANGHAI, "formats": zulu}
    assert _reject(content, time_declaration=contradiction).code == "invalid_timezone"
    offsets = {name: "offset-seconds" for name in formats}
    claimed_zone = {"timezone": NEW_YORK, "formats": offsets}
    assert _reject(content, time_declaration=claimed_zone).code == "invalid_timezone"
    # A declared format for a field this policy does not use is a contradiction.
    assert _reject(
        content,
        mapping=_without("available_at"),
        time_declaration=TIME,
        availability_policy=AVAILABILITY_SIMULATED,
        latency_seconds=60,
    ).code == "mapping_declaration"


# -- value refusals ---------------------------------------------------------


def test_a_value_that_does_not_have_the_declared_form_is_refused() -> None:
    _skip_without_zone(SHANGHAI)
    for token in (
        "2026-01-05",  # date only
        "2026-1-5T22:30:00",  # partial, unpadded
        "2026-01-05T22:30:00.000000",  # microseconds under a seconds declaration
        "2026-01-05T22:30:00Z",  # Zulu under a naive declaration
        "05/01/2026 22:30:00",  # another convention entirely
    ):
        failure = _reject(_vendor(_vendor_row(0, bar_start=token)))
        assert failure.code == "invalid_timestamp", token
        assert failure.field_name == "interval_start"
        assert failure.record_number == 2
    # An offset must be the declared colon form; the compact form is not guessed.
    compact = _vendor(_vendor_row(0, stamp=_offset, available="2026-01-05T21:00:45+0530"))
    failure = _reject(compact, time_declaration=TIME_OFFSET)
    assert failure.code == "invalid_timestamp"
    assert failure.field_name == "available_at"


def test_a_declared_iana_zone_is_applied_to_naive_values() -> None:
    _skip_without_zone(NEW_YORK)
    spring = _vendor(
        _vendor_row(
            0,
            bar_start="2026-03-08T03:30:00",
            bar_end="2026-03-08T04:30:00",
            available="2026-03-08T04:30:00",
        )
    )
    result = _map(spring, time_declaration=TIME_NEW_YORK)
    row = result.preview["rows"][0]
    # 03:30 on that date is EDT (-04:00), so the instant is 07:30Z.
    assert row["interval_start"] == "2026-03-08T07:30:00.000000Z"
    assert row["available_at"] == "2026-03-08T08:30:00.000000Z"


def test_a_daylight_saving_gap_is_refused() -> None:
    _skip_without_zone(NEW_YORK)
    gap = _vendor(
        _vendor_row(
            0,
            bar_start="2026-03-08T02:30:00",
            bar_end="2026-03-08T03:30:00",
            available="2026-03-08T03:30:00",
        )
    )
    failure = _reject(gap, time_declaration=TIME_NEW_YORK)
    assert failure.code == "invalid_timestamp"
    assert failure.field_name == "interval_start"
    assert failure.record_number == 2


def test_a_daylight_saving_fold_is_refused() -> None:
    _skip_without_zone(NEW_YORK)
    fold = _vendor(
        _vendor_row(
            0,
            bar_start="2026-11-01T01:30:00",
            bar_end="2026-11-01T02:30:00",
            available="2026-11-01T02:30:00",
        )
    )
    failure = _reject(fold, time_declaration=TIME_NEW_YORK)
    assert failure.code == "invalid_timestamp"
    assert failure.field_name == "interval_start"


# -- codec-owned refusals ---------------------------------------------------


def test_strict_economic_failures_stay_owned_by_the_decoder() -> None:
    _skip_without_zone(SHANGHAI)
    negative_volume = _reject(_vendor(_vendor_row(0, v="-1.0")))
    assert negative_volume.code == "invalid_bar"
    assert negative_volume.field_name == "volume"
    outside_range = _reject(_vendor(_vendor_row(0, o="200.0")))
    assert outside_range.code == "invalid_bar"
    assert outside_range.field_name == "open"
    before_end = _reject(_vendor(_vendor_row(0, available="2026-01-05T23:00:00")))
    assert before_end.code == "invalid_envelope"
    assert before_end.field_name == "available_at"
    split_adjustment = _with_constant("adjustment", "split")
    wrong_adjustment = _reject(_vendor(_vendor_row(0)), mapping=split_adjustment)
    assert wrong_adjustment.code == "invalid_adjustment"
    other_version = _with_constant("schema_version", "2")
    wrong_version = _reject(_vendor(_vendor_row(0)), mapping=other_version)
    assert wrong_version.code == "invalid_schema_version"


def test_ordering_and_revision_failures_stay_owned_by_the_decoder() -> None:
    _skip_without_zone(SHANGHAI)
    repeated = _reject(_vendor(_vendor_row(0), _vendor_row(1, sequence="0")))
    assert repeated.code == "canonical_batch_conflict"
    assert repeated.field_name == "source_sequence"
    assert repeated.record_number == 3


def test_a_mixed_instrument_source_is_refused() -> None:
    _skip_without_zone(SHANGHAI)
    mixed = _reject(_vendor(_vendor_row(0), _vendor_row(1, symbol="MSFT")))
    assert mixed.code == "invalid_identity"


def test_content_is_data_and_is_never_executed() -> None:
    _skip_without_zone(SHANGHAI)
    call = _reject(_vendor(_vendor_row(0, symbol="os.system('rm -rf /')")))
    assert call.code == "invalid_identity"
    assert call.field_name == "symbol"
    formula = _reject(_vendor(_vendor_row(0, c="=1+1")))
    assert formula.code == "invalid_float"
    assert formula.field_name == "close"


# -- bounds and structure ---------------------------------------------------


def test_the_upload_bounds_and_structure_rules_are_reused(monkeypatch: Any) -> None:
    _skip_without_zone(SHANGHAI)
    base = _vendor(_vendor_row(0), _vendor_row(1))
    assert MAX_UPLOAD_BYTES == 4 * 1024 * 1024
    assert _reject("not bytes").code == "upload_invalid"
    assert _reject(b"").code == "empty_source"
    assert _reject(b"\xff\xfe").code == "invalid_encoding"
    assert _reject(b"\xef\xbb\xbf" + base).code == "invalid_encoding"
    assert _reject(base.replace(b"AAPL", b"A\x00PL", 1)).code == "invalid_file_character"
    assert _reject(base.replace(b"\n", b"\r", 1)).code == "invalid_file_character"
    assert _reject(base + b'"unterminated\n').code == "malformed_csv"
    assert _reject(base + b"AAPL,XNAS\n").code == "invalid_record_shape"
    header_only = (",".join(VENDOR_FIELDS) + "\n").encode()
    failure = _reject(header_only)
    assert failure.code == "invalid_record_shape"
    assert failure.record_number == 2
    # Windows line endings are the same records, not a defect.
    windows = _map(base.replace(b"\n", b"\r\n"))
    assert windows.strict_csv == _strict_capture(_strict_row(0), _strict_row(1))
    monkeypatch.setattr(csv_mapping_module, "MAX_UPLOAD_BYTES", 32)
    assert _reject(base).code == "source_too_large"
    monkeypatch.undo()
    monkeypatch.setattr(csv_mapping_module, "MAX_UPLOAD_RECORDS", 1)
    assert _reject(base).code == "too_many_records"
    monkeypatch.undo()
    monkeypatch.setattr(csv_mapping_module, "MAX_SOURCE_COLUMNS", 4)
    assert _reject(base).code == "invalid_header"
    monkeypatch.undo()
    monkeypatch.setattr(csv_mapping_module, "PHASE1_OHLCV_MAX_FIELD_LENGTH", 3)
    assert _reject(base).code == "invalid_record_shape"


def test_a_repeated_source_column_is_refused() -> None:
    _skip_without_zone(SHANGHAI)
    header = [*VENDOR_FIELDS, "available"]
    row = ["AAPL", "XNAS", "2026-01-05T22:30:00", "2026-01-05T23:30:00"]
    row += ["100.0", "101.0", "99.0", "100.5", "10.0", "0", "0"]
    row += ["2026-01-05T23:30:45", "2026-01-05T23:30:45"]
    duplicated = (",".join(header) + "\n" + ",".join(row) + "\n").encode()
    failure = _reject(duplicated)
    assert failure.code == "invalid_header"
    assert failure.record_number == 1


def test_the_preview_is_bounded_while_the_validation_is_whole() -> None:
    _skip_without_zone(SHANGHAI)
    result = _map(_vendor(*[_vendor_row(index) for index in range(25)]))
    assert result.record_count == 25
    assert result.quality["bar_count"] == 25
    assert result.quality["bar_durations_seconds"] == ["3600"]
    assert result.preview["row_count_sampled"] == PREVIEW_SAMPLE_ROWS
    assert result.preview["rows"][0]["record_number"] == 2
    assert result.preview["truncated"] is True
    # The verdict is the whole-file decode, not the sample that is displayed.
    assert result.preview["full_validation"] == "passed"
    assert result.preview["valid"] is True
    assert result.strict_csv.count(b"\n") == 26


# -- repair regressions -----------------------------------------------------


def test_returned_evidence_cannot_be_mutated_into_a_different_claim() -> None:
    _skip_without_zone(SHANGHAI)
    result = _map(_vendor(_vendor_row(0)))
    policy = result.availability_policy
    policy["pit_verified"] = True
    policy["id"] = "anything"
    quality = result.quality
    quality["bar_count"] = 0
    preview = result.preview
    preview["valid"] = False
    preview["rows"].clear()
    assert result.availability_policy["pit_verified"] is False
    assert result.availability_policy["id"] == AVAILABILITY_PROVIDED
    assert result.quality["bar_count"] == 1
    assert result.preview["valid"] is True
    assert len(result.preview["rows"]) == 1
    # document() hands out copies too, one level down as well.
    document = result.document()
    document["availability_policy"]["pit_verified"] = True
    document["quality"]["bar_count"] = 0
    document["preview"]["rows"][0]["close"] = "0.0"
    document["preview"]["rows"].clear()
    assert result.document()["availability_policy"]["pit_verified"] is False
    assert result.document()["quality"]["bar_count"] == 1
    assert result.document()["preview"]["rows"][0]["close"] == "100.5"


def test_a_normalized_capture_past_the_store_bound_is_refused(monkeypatch: Any) -> None:
    _skip_without_zone(SHANGHAI)
    content = _vendor(_vendor_row(0), _vendor_row(1))
    expanded = _map(content).strict_csv
    # The strict form is longer than the source: stamps grow and rows re-quote, so
    # the two quotas really are different quotas.
    assert len(expanded) > len(content)
    monkeypatch.setattr(csv_mapping_module, "MAX_CAPTURE_BYTES", len(content))
    failure = _reject(content)
    assert failure.code == "capture_too_large"
    assert failure.record_number == 2
    monkeypatch.undo()
    # The bound is inclusive, and the source quota is untouched by it.
    monkeypatch.setattr(csv_mapping_module, "MAX_CAPTURE_BYTES", len(expanded))
    assert _map(content).strict_csv == expanded


def test_source_fields_and_declared_constants_share_the_strict_field_bound() -> None:
    _skip_without_zone(SHANGHAI)
    long_token = "9" * (PHASE1_OHLCV_MAX_FIELD_LENGTH + 1)
    # A column no declaration uses is still source data, so it is still bounded.
    ignored = _reject(_vendor_with_extra("note", long_token))
    assert ignored.code == "invalid_record_shape"
    assert ignored.field_name == "note"
    assert ignored.record_number == 2
    # A declared constant is bounded as well, in the declaration's own vocabulary.
    constant = _reject(_vendor(_vendor_row(0)), mapping=_with_constant("source", long_token))
    assert constant.code == "mapping_declaration"
    assert constant.field_name == "source"
    # The stdlib field limit is never adjusted process-wide; it surfaces as a
    # stable refusal rather than an exception escaping the module.
    oversized = _reject(_vendor_with_extra("note", "x" * 200_000))
    assert oversized.code == "malformed_csv"


def test_a_declared_latency_that_leaves_the_calendar_is_refused() -> None:
    content = _vendor(
        _vendor_row(
            0,
            stamp=_zulu_seconds,
            bar_start="9999-12-31T22:30:00Z",
            bar_end="9999-12-31T23:30:00Z",
        ),
        drop=("available",),
    )
    failure = _reject(
        content,
        mapping=_without("available_at"),
        time_declaration=TIME_ZULU_SECONDS,
        availability_policy=AVAILABILITY_SIMULATED,
        latency_seconds=3600,
    )
    assert failure.code == "invalid_timestamp"
    assert failure.field_name == "available_at"
    assert failure.record_number == 2
