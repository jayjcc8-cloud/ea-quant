from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from ea.core import execution_messages, historical_matching, market_data_codec

TIME_ENCODERS = [
    execution_messages._utc_text,
    historical_matching._utc_text,
    market_data_codec._utc_text,
]


@pytest.mark.parametrize("encode", TIME_ENCODERS)
@pytest.mark.parametrize("year", [1, 999, 1000, 2000, 9999])
@pytest.mark.parametrize("microsecond", [0, 1, 999999])
def test_canonical_time_preserves_strftime_bytes(
    encode: Callable[[datetime], str], year: int, microsecond: int
) -> None:
    value = datetime(year, 12, 31, 23, 59, 59, microsecond, tzinfo=UTC, fold=1)
    # Early-year padding is platform-dependent and must retain the original libc behavior.
    assert encode(value) == value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.mark.parametrize("encode", TIME_ENCODERS)
def test_canonical_time_utc_golden_bytes(encode: Callable[[datetime], str]) -> None:
    assert encode(datetime(2000, 2, 29, 1, 2, 3, 4005, tzinfo=UTC)) == (
        "2000-02-29T01:02:03.004005Z"
    )


class _ObservedZone(tzinfo):
    def __init__(self, *, fail_dst: bool) -> None:
        self.calls: list[str] = []
        self.fail_dst = fail_dst

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self.calls.append("utcoffset")
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        self.calls.append("dst")
        if self.fail_dst:
            raise ValueError("custom dst failure")
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        self.calls.append("tzname")
        return "observed-zero"


@pytest.mark.parametrize("encode", TIME_ENCODERS)
@pytest.mark.parametrize("fail_dst", [False, True])
def test_canonical_time_preserves_custom_timezone_callbacks(
    encode: Callable[[datetime], str], fail_dst: bool
) -> None:
    zone = _ObservedZone(fail_dst=fail_dst)
    value = datetime(2026, 1, 2, tzinfo=zone)

    def original() -> str:
        if encode is historical_matching._utc_text:
            assert value.utcoffset() == timedelta(0)
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    if fail_dst:
        with pytest.raises(ValueError, match="custom dst failure"):
            original()
        expected = ""
    else:
        expected = original()
    expected_calls = zone.calls.copy()
    zone.calls.clear()
    if fail_dst:
        with pytest.raises(ValueError, match="custom dst failure"):
            encode(value)
    else:
        assert encode(value) == expected
    assert zone.calls == expected_calls


@pytest.mark.parametrize("encode", TIME_ENCODERS)
def test_canonical_time_preserves_named_zero_offset(
    encode: Callable[[datetime], str],
) -> None:
    value = datetime(2026, 1, 2, tzinfo=timezone(timedelta(0), "named-zero"))
    assert encode(value) == value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _DateSubclass(datetime):
    def strftime(self, fmt: str) -> str:
        return "subclass-override"


@pytest.mark.parametrize("encode", [execution_messages._utc_text, market_data_codec._utc_text])
def test_canonical_time_preserves_subclass_override(
    encode: Callable[[datetime], str],
) -> None:
    assert encode(_DateSubclass(2026, 1, 2, tzinfo=UTC)) == "subclass-override"


@pytest.mark.parametrize(
    "value",
    [
        _DateSubclass(2026, 1, 2, tzinfo=UTC),
        None,
        "2026-01-02",
        datetime(2026, 1, 2),
        datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_historical_canonical_time_still_rejects_invalid_values(value: object) -> None:
    with pytest.raises(historical_matching.HistoricalMatcherError):
        historical_matching._utc_text(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        '"\\\b\f\n\r\t',
        "".join(chr(index) for index in range(256)),
        "中文😀\u2028\u2029",
        "\ud800\udfff",
        {"z": ["中文", None, -17], "a": {"\ud800": "😀"}},
    ],
)
def test_canonical_json_preserves_ascii_string_and_container_bytes(
    value: object,
) -> None:
    expected = json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    assert execution_messages._encode_json(value) == expected


class _StringSubclass(str):
    pass


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (True, "unsupported canonical JSON value: bool"),
        (False, "unsupported canonical JSON value: bool"),
        (0.1, "unsupported canonical JSON value: float"),
        (float("nan"), "unsupported canonical JSON value: float"),
        ((), "unsupported canonical JSON value: tuple"),
        (b"s", "unsupported canonical JSON value: bytes"),
        (_StringSubclass("s"), "unsupported canonical JSON value: _StringSubclass"),
        ({1: "x"}, "canonical JSON object keys must be exact str"),
        ({_StringSubclass("s"): "x"}, "canonical JSON object keys must be exact str"),
        ({"x": False}, "unsupported canonical JSON value: bool"),
    ],
)
def test_canonical_json_preserves_rejections(value: object, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        execution_messages._encode_json(value)


def test_canonical_json_preserves_large_integers_and_reads_mutated_input() -> None:
    expected = b"1" + b"0" * 5000
    assert execution_messages._encode_json([10**5000, -(10**5000)]) == (
        b"[" + expected + b",-" + expected + b"]"
    )
    values = ["before"]
    document = {"x": values}
    assert execution_messages._encode_json(document) == b'{"x":["before"]}'
    values[0] = "after"
    assert execution_messages._encode_json(document) == b'{"x":["after"]}'
