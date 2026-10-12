"""The read-only historical Bar adapter: bounds, refusals and re-derivation.

Every response byte in this module is synthetic and supplied by a test-owned
transport object. Nothing here is a live end-to-end run against the provider, no
network call is made, and no permission, licence or point-in-time availability is
claimed anywhere. One test publishes such a capture through the existing
controlled upload store and runs the existing engine over it; that run reads the
same synthetic bars and reaches no provider.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest

from ea.data.historical import (
    _HEADER as STRICT_HEADER,
)
from ea.data.historical import (
    PHASE1_OHLCV_PROFILE,
)
from ea.plugin import historical_bars
from ea.plugin.historical_bars import (
    DEFAULT_DEADLINE_SECONDS,
    DOCUMENTED_KLINES_FIELD_COUNT,
    ENVELOPE_SCHEMA,
    INTERVALS,
    MAX_ENVELOPE_BYTES,
    MAX_PAGE_BYTES,
    MAX_PAGES,
    MAX_RECORDS,
    MAX_TOTAL_RESPONSE_BYTES,
    NORMALIZER_ID,
    NORMALIZER_VERSION,
    PAGE_LIMIT,
    PROVIDER_ENDPOINT,
    PROVIDER_EVIDENCE_SCHEMA,
    PROVIDER_ID,
    PROVIDER_VERSION,
    SOURCE_CODE,
    HistoricalBarCaptureV1,
    HistoricalBarError,
    HistoricalBarHttpResponseV1,
    HistoricalBarQueryV1,
    fetch_historical_bars,
    rederive_strict_capture,
)
from ea.web.service import WebService
from ea.web.uploads import (
    AVAILABILITY_SIMULATED,
    PROVIDER_VERSION_SCHEMA,
    UPLOAD_VERSION_SCHEMA,
    ControlledUploadStoreV1,
    UploadError,
    evidence_digest,
    seal_version_manifest,
)
from unit.test_web_mapping_integration import _await_job, _bound_binding, _parameters
from unit.test_web_research_declarative import _declarative_root

# 2024-01-01T00:00:00Z, and a moment well after the ranges this module reads.
START_MS = 1_704_067_200_000
HOUR_MS = 3_600_000
MINUTE_MS = 60_000
OBSERVED_MS = START_MS + 10 * HOUR_MS
INGESTED_MS = OBSERVED_MS + 1_000
# The window the declarative scenario's own fixture places its bars in. The one
# engine run below reads a provider capture over exactly this window.
SCENARIO_START = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
SCENARIO_END = datetime(2026, 1, 2, 9, 34, tzinfo=UTC)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ms(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(milliseconds=1)


def _utc(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


def _stamp(epoch_ms: int) -> str:
    return _utc(epoch_ms).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _query(**overrides: Any) -> HistoricalBarQueryV1:
    values: dict[str, Any] = {
        "venue": "BINANCE",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "start_ms": START_MS,
        "end_ms": START_MS + 3 * HOUR_MS,
        "latency_seconds": 0,
    }
    values.update(overrides)
    return HistoricalBarQueryV1(**values)


def _closed(query: HistoricalBarQueryV1) -> int:
    """An explicit observed-now strictly after the queried range has closed.

    A long synthetic range is closed by its own end, not by this module's default
    observed-now; the closure rule is stated per test rather than weakened.
    """
    return query.end_ms + HOUR_MS


def _record(open_ms: int, *, interval: str = "1h", close: str = "100.5") -> list[Any]:
    """One synthetic record in the documented twelve-field klines shape."""
    return [
        open_ms,
        "100.0",
        "101.0",
        "99.0",
        close,
        "10.0",
        open_ms + INTERVALS[interval] - 1,
        "1000.0",
        7,
        "5.0",
        "500.0",
        "0",
    ]


def _hourly(count: int, *, start_ms: int = START_MS) -> list[Any]:
    return [_record(start_ms + index * HOUR_MS) for index in range(count)]


def _minute_page(start_ms: int) -> list[Any]:
    return [_record(start_ms + index * MINUTE_MS, interval="1m") for index in range(PAGE_LIMIT)]


def _page(
    records: list[Any], *, status: int = 200, headers: tuple[tuple[str, str], ...] = ()
) -> HistoricalBarHttpResponseV1:
    return HistoricalBarHttpResponseV1(
        status=status, headers=headers, body=json.dumps(records).encode("utf-8")
    )


class ScriptedTransport:
    """A test-owned transport: it returns scripted answers and records the calls."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def request(
        self, *, url: str, params: tuple[tuple[str, str], ...], timeout_seconds: float
    ) -> HistoricalBarHttpResponseV1:
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout_seconds})
        if not self.answers:
            raise AssertionError("the transport was called more times than the script allows")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]


class Recorder:
    """An injected clock and sleeper: no test ever really sleeps."""

    def __init__(self, *ticks: float) -> None:
        self.ticks = list(ticks)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        if len(self.ticks) > 1:
            return self.ticks.pop(0)
        return self.ticks[0] if self.ticks else 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _fetch(
    query: HistoricalBarQueryV1,
    transport: object,
    *,
    recorder: Recorder | None = None,
    observed_ms: int = OBSERVED_MS,
    ingested_ms: int = INGESTED_MS,
    **overrides: Any,
) -> HistoricalBarCaptureV1:
    clock = recorder or Recorder(0.0)
    return fetch_historical_bars(
        query,
        observed_now=_utc(observed_ms),
        ingested_at=_utc(ingested_ms),
        transport=transport,  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        **overrides,
    )


def _refused(call: Any) -> HistoricalBarError:
    with pytest.raises(HistoricalBarError) as caught:
        call()
    return caught.value


def _capture(records: int = 3, **overrides: Any) -> HistoricalBarCaptureV1:
    return _fetch(_query(**overrides), ScriptedTransport(_page(_hourly(records))))


# --- the transport boundary --------------------------------------------------


def test_without_an_injected_transport_the_adapter_fails_closed() -> None:
    """No transport means no attempt: this build contains no network transport."""
    calls: list[str] = []

    class Unreachable:
        def request(self, **_: object) -> HistoricalBarHttpResponseV1:
            calls.append("called")
            raise AssertionError("an unreachable transport was called")

    error = _refused(
        lambda: fetch_historical_bars(
            _query(), observed_now=_utc(OBSERVED_MS), ingested_at=_utc(INGESTED_MS)
        )
    )
    assert error.code.value == "network_disabled"
    assert "no network transport" in error.message
    assert calls == []


def test_the_adapter_asks_only_the_one_fixed_endpoint() -> None:
    transport = ScriptedTransport(_page(_hourly(3)))
    _fetch(_query(), transport)
    assert [call["url"] for call in transport.calls] == [PROVIDER_ENDPOINT]
    assert transport.calls[0]["params"] == {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "startTime": str(START_MS),
        "endTime": str(START_MS + 3 * HOUR_MS - 1),
        "limit": str(PAGE_LIMIT),
    }


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (
            _page([], status=302, headers=(("location", "https://elsewhere.example"),)),
            "provider_redirect",
        ),
        (_page([], status=403), "provider_unauthorized"),
        (_page([], status=451), "provider_restricted"),
        (_page([], status=500), "provider_status"),
    ],
)
def test_redirects_authorization_and_region_answers_are_refused_without_retry(
    answer: HistoricalBarHttpResponseV1, code: str
) -> None:
    transport = ScriptedTransport(answer)
    error = _refused(lambda: _fetch(_query(), transport))
    assert error.code.value == code
    # One call, to the one fixed host: no alternative host, mirror or proxy.
    assert len(transport.calls) == 1


def test_a_transport_that_raises_is_reported_rather_than_retried() -> None:
    transport = ScriptedTransport(OSError("connection refused"))
    error = _refused(lambda: _fetch(_query(), transport))
    assert error.code.value == "transport_failure"
    assert "OSError" in error.message
    assert len(transport.calls) == 1


def test_a_transport_that_breaks_the_contract_is_refused() -> None:
    class Broken:
        def request(self, **_: object) -> object:
            return {"status": 200}

    error = _refused(lambda: _fetch(_query(), Broken()))
    assert error.code.value == "transport_contract"


# --- rate limiting -----------------------------------------------------------


def test_a_bounded_retry_after_is_honoured_through_an_injected_sleeper() -> None:
    clock = Recorder(0.0)
    transport = ScriptedTransport(
        HistoricalBarHttpResponseV1(429, (("Retry-After", "7"),), b""),
        _page(_hourly(3)),
    )
    capture = _fetch(_query(), transport, recorder=clock)
    assert clock.slept == [7.0]
    assert len(transport.calls) == 2
    assert capture.record_count == 3


@pytest.mark.parametrize(
    "headers",
    [(("Retry-After", "3600"),), (("Retry-After", "soon"),), (("Retry-After", "-1"),), ()],
)
def test_an_unbounded_or_absent_retry_after_is_refused(
    headers: tuple[tuple[str, str], ...],
) -> None:
    transport = ScriptedTransport(HistoricalBarHttpResponseV1(429, headers, b""))
    error = _refused(lambda: _fetch(_query(), transport))
    assert error.code.value == "retry_after_invalid"
    assert len(transport.calls) == 1


def test_repeated_rate_limiting_exhausts_a_bounded_number_of_attempts() -> None:
    clock = Recorder(0.0)
    transport = ScriptedTransport(
        *[HistoricalBarHttpResponseV1(429, (("Retry-After", "1"),), b"") for _ in range(4)]
    )
    error = _refused(lambda: _fetch(_query(), transport, recorder=clock))
    assert error.code.value == "provider_rate_limited"
    assert len(transport.calls) == 4
    assert clock.slept == [1.0, 1.0, 1.0]


# --- pagination and bounds ---------------------------------------------------


def test_the_cursor_advances_only_after_a_complete_validated_page() -> None:
    minute_query = _query(
        interval="1m",
        start_ms=START_MS,
        end_ms=START_MS + (PAGE_LIMIT + 2) * MINUTE_MS,
        latency_seconds=5,
    )
    transport = ScriptedTransport(
        _page(_minute_page(START_MS)),
        _page(_minute_page(START_MS + PAGE_LIMIT * MINUTE_MS)[:2]),
    )
    capture = _fetch(minute_query, transport, observed_ms=_closed(minute_query))
    assert capture.record_count == PAGE_LIMIT + 2
    # The second request resumes exactly after the last accepted bar's close.
    assert transport.calls[1]["params"]["startTime"] == str(START_MS + PAGE_LIMIT * MINUTE_MS)
    assert transport.calls[1]["params"]["endTime"] == str(minute_query.end_ms - 1)
    assert capture.data_sha256 and capture.capture_sha256


def test_a_rejected_page_is_refused_whole_and_the_cursor_does_not_move() -> None:
    minute_query = _query(
        interval="1m", start_ms=START_MS, end_ms=START_MS + (PAGE_LIMIT + 2) * MINUTE_MS
    )
    # The second page repeats a bar the first page already contributed.
    transport = ScriptedTransport(
        _page(_minute_page(START_MS)),
        _page([_record(START_MS + (PAGE_LIMIT - 1) * MINUTE_MS, interval="1m")]),
    )
    error = _refused(lambda: _fetch(minute_query, transport, observed_ms=_closed(minute_query)))
    assert error.code.value == "duplicate_bar"
    assert (error.page_index, error.record_index) == (2, 1)
    assert len(transport.calls) == 2


def test_a_range_inside_the_page_bound_is_read_whole() -> None:
    query = _query(
        interval="1m",
        start_ms=START_MS,
        end_ms=START_MS + (5 * PAGE_LIMIT + 500) * MINUTE_MS,
    )
    transport = ScriptedTransport(
        *[_page(_minute_page(START_MS + index * PAGE_LIMIT * MINUTE_MS)) for index in range(5)],
        _page(_minute_page(START_MS + 5 * PAGE_LIMIT * MINUTE_MS)[:500]),
    )
    capture = _fetch(query, transport, observed_ms=_closed(query))
    assert capture.record_count == 5 * PAGE_LIMIT + 500
    assert len(transport.calls) == 6


def test_the_page_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # The total-response byte cap has its own test and its own limit; raising the
    # cap only for this test isolates the page bound from that smaller, separate
    # bound so this range reaches the page limit rather than the byte limit.
    monkeypatch.setattr(historical_bars, "MAX_TOTAL_RESPONSE_BYTES", 4 * 1024 * 1024)
    # The declared record bound is implied by the page bound and page size.
    assert MAX_RECORDS == MAX_PAGES * PAGE_LIMIT
    query = _query(
        interval="1m",
        start_ms=START_MS,
        end_ms=START_MS + (MAX_PAGES + 1) * PAGE_LIMIT * MINUTE_MS,
    )
    transport = ScriptedTransport(
        *[
            _page(_minute_page(START_MS + index * PAGE_LIMIT * MINUTE_MS))
            for index in range(MAX_PAGES)
        ]
    )
    error = _refused(lambda: _fetch(query, transport, observed_ms=_closed(query)))
    assert error.code.value == "page_budget_exceeded"
    assert len(transport.calls) == MAX_PAGES


def test_the_deadline_is_bounded() -> None:
    # The clock is read at the top of the fetch loop and again before the request:
    # a read with no time left never reaches the transport at all.
    clock = Recorder(0.0, 0.0, DEFAULT_DEADLINE_SECONDS + 1.0)
    query = _query(interval="1m", start_ms=START_MS, end_ms=START_MS + (PAGE_LIMIT + 1) * MINUTE_MS)
    transport = ScriptedTransport(_page(_minute_page(START_MS)))
    error = _refused(lambda: _fetch(query, transport, recorder=clock, observed_ms=_closed(query)))
    assert error.code.value == "deadline_exceeded"
    assert "no time is left" in error.message
    assert transport.calls == []


def test_a_page_that_arrives_after_the_deadline_is_refused_even_as_a_200() -> None:
    """A late answer is not evidence about the queried range, whatever it says."""
    # One complete first page, then a second page whose clock is read immediately
    # after it returns and is already past the declared deadline.
    late = DEFAULT_DEADLINE_SECONDS + 1.0
    clock = Recorder(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, late)
    query = _query(interval="1m", start_ms=START_MS, end_ms=START_MS + (PAGE_LIMIT + 1) * MINUTE_MS)
    transport = ScriptedTransport(
        _page(_minute_page(START_MS)),
        _page(_minute_page(START_MS + PAGE_LIMIT * MINUTE_MS)[:1]),
    )
    error = _refused(lambda: _fetch(query, transport, recorder=clock, observed_ms=_closed(query)))
    assert error.code.value == "deadline_exceeded"
    assert "refused rather than accepted late" in error.message
    assert len(transport.calls) == 2


def test_a_rate_limited_retry_is_given_only_the_time_the_deadline_leaves() -> None:
    # The declared request timeout is 10s and the deadline is 5s: the first attempt
    # is shrunk to the deadline, and the retry, three seconds later and after a
    # one-second backoff, is shrunk again to what is actually left.
    clock = Recorder(0.0, 0.0, 0.0, 0.0, 3.0, 3.0, 3.0)
    transport = ScriptedTransport(
        HistoricalBarHttpResponseV1(429, (("Retry-After", "1"),), b""),
        _page(_hourly(3)),
    )
    capture = _fetch(
        _query(),
        transport,
        recorder=clock,
        deadline_seconds=5.0,
        request_timeout_seconds=10.0,
    )
    assert [call["timeout"] for call in transport.calls] == [5.0, 2.0]
    assert clock.slept == [1.0]
    assert capture.record_count == 3


def test_a_backoff_the_deadline_cannot_hold_is_refused_without_sleeping() -> None:
    clock = Recorder(0.0, 0.0, 0.0, 0.0, 4.5)
    transport = ScriptedTransport(HistoricalBarHttpResponseV1(429, (("Retry-After", "1"),), b""))
    error = _refused(lambda: _fetch(_query(), transport, recorder=clock, deadline_seconds=5.0))
    assert error.code.value == "deadline_exceeded"
    assert clock.slept == []
    assert len(transport.calls) == 1


@pytest.mark.parametrize("value", [0.0, -1.0, 10_000.0])
def test_an_unusable_deadline_is_refused(value: float) -> None:
    error = _refused(
        lambda: _fetch(_query(), ScriptedTransport(_page(_hourly(3))), deadline_seconds=value)
    )
    assert error.code.value == "invalid_query"


def test_an_oversized_page_body_is_refused_before_it_is_parsed() -> None:
    body = b"[" + b"0" * (MAX_PAGE_BYTES + 1) + b"]"
    transport = ScriptedTransport(HistoricalBarHttpResponseV1(status=200, headers=(), body=body))
    error = _refused(lambda: _fetch(_query(), transport))
    assert error.code.value == "response_too_large"


def _unaligned(open_ms: int) -> list[Any]:
    record = _record(open_ms)
    record[6] = open_ms + HOUR_MS + 1  # no longer exactly one interval after the open
    return record


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (HistoricalBarHttpResponseV1(200, (), b"not json"), "malformed_response"),
        (HistoricalBarHttpResponseV1(200, (), b'{"code":-1121}'), "malformed_response"),
        (_page([_record(START_MS)[:11]]), "malformed_record"),
        (
            _page([[*_record(START_MS)[:2], 2, *_record(START_MS)[3:]]]),
            "malformed_record",
        ),
        (_page([_unaligned(START_MS)]), "unaligned_bar"),
        (_page([_record(START_MS - HOUR_MS)]), "out_of_range_bar"),
        (_page([_record(START_MS + 3 * HOUR_MS)]), "out_of_range_bar"),
        (_page([]), "empty_selection"),
    ],
)
def test_documented_shape_violations_are_refused_with_a_stable_code(
    answer: HistoricalBarHttpResponseV1, code: str
) -> None:
    error = _refused(lambda: _fetch(_query(), ScriptedTransport(answer)))
    assert error.code.value == code
    assert error.document()["code"] == code


def test_a_range_not_closed_as_of_the_observed_now_is_refused_before_any_call() -> None:
    transport = ScriptedTransport(_page(_hourly(3)))
    error = _refused(lambda: _fetch(_query(), transport, observed_ms=START_MS + 2 * HOUR_MS))
    assert error.code.value == "invalid_query"
    assert transport.calls == []


def test_an_unclosed_bar_is_refused_when_a_version_is_re_derived() -> None:
    """The closure rule is re-applied to the sealed observed-now on every read."""
    capture = _capture()
    envelope = capture.envelope
    evidence = capture.provider_evidence
    earlier = _stamp(START_MS + HOUR_MS)
    envelope["observed_now_utc"] = earlier
    evidence["declaration"]["observed_now_utc"] = earlier
    error = _refused(
        lambda: rederive_strict_capture(json.dumps(envelope).encode("utf-8"), evidence)
    )
    assert error.code.value == "unclosed_bar"
    assert (error.page_index, error.record_index) == (1, 2)


def test_an_out_of_order_page_is_refused() -> None:
    records = _hourly(3)
    transport = ScriptedTransport(_page([records[2], records[0]]))
    error = _refused(lambda: _fetch(_query(), transport))
    assert error.code.value == "out_of_order_bar"


# --- the query contract ------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"interval": "1d"}, "invalid_interval"),
        ({"interval": "1w"}, "invalid_interval"),
        ({"start_ms": START_MS + 1}, "invalid_query"),
        ({"end_ms": START_MS}, "invalid_query"),
        ({"end_ms": START_MS + HOUR_MS + 1}, "invalid_query"),
        ({"latency_seconds": -1}, "invalid_latency"),
        ({"latency_seconds": None}, "invalid_latency"),
        ({"symbol": "not a symbol"}, "invalid_query"),
        ({"venue": ""}, "invalid_query"),
    ],
)
def test_only_whole_aligned_utc_minute_and_hour_ranges_are_accepted(
    overrides: dict[str, Any], code: str
) -> None:
    error = _refused(lambda: _query(**overrides))
    assert error.code.value == code


def test_a_query_that_is_not_the_closed_contract_is_refused() -> None:
    error = _refused(
        lambda: fetch_historical_bars(
            {"venue": "BINANCE"},  # type: ignore[arg-type]
            observed_now=_utc(OBSERVED_MS),
            ingested_at=_utc(INGESTED_MS),
            transport=ScriptedTransport(_page(_hourly(3))),
        )
    )
    assert error.code.value == "invalid_query"


# --- provenance --------------------------------------------------------------


def test_availability_is_the_declared_latency_and_is_never_called_verified() -> None:
    capture = _fetch(_query(latency_seconds=30), ScriptedTransport(_page(_hourly(3))))
    rows = capture.strict_csv.decode("utf-8").splitlines()
    assert rows[0].split(",") == list(STRICT_HEADER)
    first = rows[1].split(",")
    assert first[3] == "2024-01-01T00:00:00.000000Z"
    assert first[4] == "2024-01-01T01:00:00.000000Z"
    assert first[11] == SOURCE_CODE
    assert first[14] == "2024-01-01T01:00:30.000000Z"
    assert capture.profile == PHASE1_OHLCV_PROFILE

    envelope = capture.envelope
    assert envelope["schema"] == ENVELOPE_SCHEMA
    assert envelope["policy"]["pit_verified"] is False
    assert envelope["policy"]["availability"] == "simulated: interval_end + latency_seconds"
    assert envelope["policy"]["vintage"] is None
    assert envelope["policy"]["real_network"] is False
    assert envelope["policy"]["provider_terms"] == "blocked"
    assert envelope["policy"]["max_pages"] == MAX_PAGES
    assert envelope["policy"]["max_records"] == MAX_RECORDS
    assert envelope["transport"]["real_network"] is False
    assert envelope["transport"]["provider_terms"] == "blocked"
    assert envelope["provider"]["id"] == PROVIDER_ID
    assert envelope["provider"]["version"] == PROVIDER_VERSION
    assert envelope["provider"]["endpoint"] == PROVIDER_ENDPOINT
    assert envelope["provider"]["documented_field_count"] == DOCUMENTED_KLINES_FIELD_COUNT
    assert envelope["provider"]["documented_fields"][0] == "open_time_ms"
    assert envelope["provider"]["documented_fields"][6] == "close_time_ms"
    assert envelope["normalizer"] == {"id": NORMALIZER_ID, "version": NORMALIZER_VERSION}
    assert envelope["observed_now_utc"] == "2024-01-01T10:00:00.000000Z"
    assert envelope["ingested_at_utc"] == "2024-01-01T10:00:01.000000Z"
    assert envelope["capture"]["profile"] == PHASE1_OHLCV_PROFILE
    assert envelope["capture"]["record_count"] == 3
    assert envelope["records"] == {
        "count": 3,
        "first_open_ms": START_MS,
        "last_close_ms": START_MS + 3 * HOUR_MS - 1,
    }
    limitations = " ".join(envelope["limitations"])
    assert "caller-provided transport" in limitations
    assert "no live request" in limitations
    assert "terms are unresolved" in limitations
    assert "not verified point-in-time availability" in limitations
    # No licence is fabricated anywhere in what this host records.
    assert "license_reference" not in json.dumps(capture.provider_evidence)


def test_the_exact_page_bytes_are_retained_and_digested() -> None:
    body = json.dumps(_hourly(3)).encode("utf-8")
    capture = _fetch(
        _query(),
        ScriptedTransport(
            HistoricalBarHttpResponseV1(
                status=200, headers=(("Content-Type", "application/json"),), body=body
            )
        ),
    )
    page = capture.envelope["pages"][0]
    assert page["request_url"] == PROVIDER_ENDPOINT
    assert page["status"] == 200
    assert page["byte_count"] == len(body)
    assert page["sha256"] == sha256(body).hexdigest()
    assert base64.b64decode(page["body_base64"]) == body
    assert ["Content-Type", "application/json"] in page["headers"]
    assert capture.provider_evidence["schema"] == PROVIDER_EVIDENCE_SCHEMA
    assert capture.provider_evidence["result"]["pages"] == [
        {
            "index": 1,
            "status": 200,
            "byte_count": len(body),
            "sha256": sha256(body).hexdigest(),
        }
    ]


# --- re-derivation -----------------------------------------------------------


def test_a_capture_is_reproduced_from_its_own_envelope_and_evidence() -> None:
    capture = _capture()
    rederived = rederive_strict_capture(
        capture.envelope_json.encode("utf-8"), capture.provider_evidence
    )
    assert rederived == capture.strict_csv


@pytest.mark.parametrize("field", ["observed_now_utc", "ingested_at_utc"])
def test_an_edited_declaration_no_longer_reproduces_the_capture(field: str) -> None:
    capture = _capture()
    evidence = capture.provider_evidence
    evidence["declaration"][field] = "2024-01-02T00:00:00.000000Z"
    error = _refused(
        lambda: rederive_strict_capture(capture.envelope_json.encode("utf-8"), evidence)
    )
    assert error.code.value == "envelope_invalid"


def test_an_edited_page_body_no_longer_reproduces_the_capture() -> None:
    capture = _capture()
    envelope = capture.envelope
    body = _hourly(3)
    body[0][6] = body[0][6] - 1
    envelope["pages"][0]["body_base64"] = base64.b64encode(json.dumps(body).encode("utf-8")).decode(
        "ascii"
    )
    error = _refused(
        lambda: rederive_strict_capture(
            json.dumps(envelope).encode("utf-8"), capture.provider_evidence
        )
    )
    assert error.code.value == "envelope_invalid"


def test_evidence_from_another_provider_is_refused() -> None:
    capture = _capture()
    evidence = capture.provider_evidence
    evidence["provider"] = {**evidence["provider"], "version": "some-other-shape"}
    error = _refused(
        lambda: rederive_strict_capture(capture.envelope_json.encode("utf-8"), evidence)
    )
    assert error.code.value == "envelope_invalid"
    assert "another provider" in error.message


def test_a_retained_envelope_larger_than_the_bound_is_refused_before_it_is_decoded() -> None:
    capture = _capture()
    error = _refused(
        lambda: rederive_strict_capture(b"x" * (MAX_ENVELOPE_BYTES + 1), capture.provider_evidence)
    )
    assert error.code.value == "response_too_large"


def test_a_retained_envelope_beyond_the_page_bound_is_refused() -> None:
    capture = _capture()
    envelope = capture.envelope
    evidence = capture.provider_evidence
    repeated = MAX_PAGES + 1
    envelope["pages"] = envelope["pages"] * repeated
    evidence["result"]["pages"] = evidence["result"]["pages"] * repeated
    error = _refused(
        lambda: rederive_strict_capture(json.dumps(envelope).encode("utf-8"), evidence)
    )
    assert error.code.value == "page_budget_exceeded"


def test_a_retained_envelope_beyond_the_total_byte_bound_is_refused() -> None:
    capture = _capture()
    envelope = capture.envelope
    evidence = capture.provider_evidence
    # Two page bodies that each sit inside the per-page bound but together exceed
    # the total one, with every digest re-sealed consistently for each of them.
    blob = b"0" * (MAX_TOTAL_RESPONSE_BYTES // 2 + 1)
    pages: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for index in range(1, 3):
        pages.append(
            {
                "index": index,
                "request_url": PROVIDER_ENDPOINT,
                "request_params": [
                    ["symbol", "BTCUSDT"],
                    ["interval", "1h"],
                    ["startTime", str(START_MS)],
                    ["endTime", str(START_MS + 3 * HOUR_MS - 1)],
                    ["limit", str(PAGE_LIMIT)],
                ],
                "request_timeout_seconds": 10.0,
                "status": 200,
                "headers": [],
                "byte_count": len(blob),
                "sha256": sha256(blob).hexdigest(),
                "body_base64": base64.b64encode(blob).decode("ascii"),
            }
        )
        summaries.append(
            {
                "index": index,
                "status": 200,
                "byte_count": len(blob),
                "sha256": sha256(blob).hexdigest(),
            }
        )
    envelope["pages"] = pages
    evidence["result"]["pages"] = summaries
    error = _refused(
        lambda: rederive_strict_capture(json.dumps(envelope).encode("utf-8"), evidence)
    )
    assert error.code.value == "response_too_large"


@pytest.mark.parametrize("page_field", ["request_params", "index", "status"])
def test_a_retained_page_that_no_longer_matches_the_sealed_query_is_refused(
    page_field: str,
) -> None:
    """A tampered page's own metadata cannot masquerade as this version's evidence."""
    capture = _capture()
    envelope = capture.envelope
    evidence = capture.provider_evidence
    page = cast("dict[str, Any]", envelope["pages"][0])
    summary = cast("dict[str, Any]", evidence["result"]["pages"][0])
    if page_field == "request_params":
        # A retained request for another instrument, every digest re-sealed.
        page["request_params"] = [["symbol", "ETHUSDT"], *page["request_params"][1:]]
    elif page_field == "index":
        page["index"] = 2
        summary["index"] = 2
    else:
        page["status"] = 201
        summary["status"] = 201
    error = _refused(
        lambda: rederive_strict_capture(json.dumps(envelope).encode("utf-8"), evidence)
    )
    assert error.code.value == "envelope_invalid"


# --- publication through the existing store ----------------------------------


def _store(tmp_path: Path) -> ControlledUploadStoreV1:
    return ControlledUploadStoreV1(tmp_path / "uploads")


def _reseal(document: dict[str, Any]) -> None:
    """A consistent hostile editor: it recomputes every digest it can reach."""
    if "provider_evidence" in document:
        document["provider_evidence_sha256"] = evidence_digest(document["provider_evidence"])
    if "mapping_evidence" in document:
        document["mapping_evidence_sha256"] = evidence_digest(document["mapping_evidence"])
    document.update(seal_version_manifest(document))


def _write_version(
    store: ControlledUploadStoreV1, dataset_id: str, document: dict[str, Any]
) -> None:
    (store.root / "versions" / f"{dataset_id}.json").write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def test_a_published_provider_version_keeps_the_envelope_as_its_raw_source(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        capture = _capture(latency_seconds=15)
        manifest = store.publish_historical_bars(capture, file_name="klines-envelope.json")
        assert manifest["schema"] == PROVIDER_VERSION_SCHEMA
        assert store.manifest(manifest["dataset_id"]) == manifest
        raw = (store.root / "raw" / manifest["dataset_id"]).read_bytes()
        assert raw == capture.envelope_json.encode("utf-8")
        assert manifest["source_byte_count"] == len(raw)
        assert manifest["source"]["declared_by"] == "caller"
        assert manifest["source"]["asserted"] is False
        assert manifest["source"]["license_reference"] is None
        assert manifest["availability_policy"]["id"] == AVAILABILITY_SIMULATED
        assert manifest["availability_policy"]["latency_seconds"] == 15
        assert manifest["availability_policy"]["pit_verified"] is False

        path, dataset = store.load(manifest["dataset_id"])
        assert path.read_bytes() == capture.strict_csv
        assert dataset.profile == PHASE1_OHLCV_PROFILE
        assert dataset.selection.fingerprint.sha256.value == manifest["data_sha256"]

        evidence = store.version_evidence(manifest["dataset_id"])
        assert evidence["origin"] == "upload"
        assert evidence["source"]["declared_by"] == "caller"
        assert evidence["provider"]["provider"]["id"] == PROVIDER_ID
        assert evidence["provider"]["normalizer"]["id"] == NORMALIZER_ID
        assert evidence["provider"]["query"]["interval"] == "1h"
        assert evidence["availability_policy"]["latency_seconds"] == 15
        assert evidence["capture_sha256"] == manifest["capture_sha256"]
        assert store.version_evidence_digest(manifest["dataset_id"]) == evidence_digest(evidence)
    finally:
        store.close()


def test_a_capture_its_own_envelope_does_not_reproduce_is_not_published(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        capture = _capture()
        forged = HistoricalBarCaptureV1(
            profile=capture.profile,
            strict_csv=capture.strict_csv.replace(b"100.5", b"100.4"),
            envelope_json=capture.envelope_json,
            evidence_json=capture.evidence_json,
            capture_sha256=capture.capture_sha256,
            capture_byte_count=capture.capture_byte_count,
            data_sha256=capture.data_sha256,
            record_count=capture.record_count,
            venue=capture.venue,
            symbol=capture.symbol,
        )
        with pytest.raises(UploadError) as caught:
            store.publish_historical_bars(forged)
        assert caught.value.code == "provider_capture"
        assert store.list() == []
    finally:
        store.close()


def test_a_read_re_normalizes_the_retained_pages_and_refuses_a_tampered_version(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.publish_historical_bars(_capture())
        dataset_id = manifest["dataset_id"]

        # A hostile editor edits the retained page and then re-seals everything it
        # can reach, including the page digest and the manifest digests. The
        # normalizer is run again over the retained bytes and still refuses to
        # reproduce the published capture.
        envelope = json.loads((store.root / "raw" / dataset_id).read_bytes())
        body = _hourly(3)
        body[0][6] = body[0][6] - 1
        encoded = json.dumps(body).encode("utf-8")
        page = envelope["pages"][0]
        page["body_base64"] = base64.b64encode(encoded).decode("ascii")
        page["byte_count"] = len(encoded)
        page["sha256"] = sha256(encoded).hexdigest()
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        (store.root / "raw" / dataset_id).write_bytes(raw)
        document = store.manifest(dataset_id)
        assert document is not None
        document["source_byte_count"] = len(raw)
        document["source_bytes_sha256"] = sha256(raw).hexdigest()
        document["provider_evidence"]["result"]["pages"][0] = {
            "index": 1,
            "status": 200,
            "byte_count": len(encoded),
            "sha256": sha256(encoded).hexdigest(),
        }
        _reseal(document)
        _write_version(store, dataset_id, document)
        assert store.manifest(dataset_id) is not None
        with pytest.raises(ValueError) as caught:
            store.load(dataset_id)
        assert "unaligned_bar" in str(caught.value)
    finally:
        store.close()


def test_a_capture_that_is_not_what_the_envelope_normalizes_to_is_refused(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.publish_historical_bars(_capture())
        (store.root / "captures" / manifest["dataset_id"]).write_bytes(
            _capture().strict_csv.replace(b"100.5", b"100.4")
        )
        with pytest.raises(ValueError):
            store.load(manifest["dataset_id"])
    finally:
        store.close()


def test_a_published_provider_version_survives_a_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "uploads"
    first = ControlledUploadStoreV1(workspace)
    try:
        manifest = first.publish_historical_bars(_capture(latency_seconds=3))
        digest = first.version_evidence_digest(manifest["dataset_id"])
    finally:
        first.close()
    second = ControlledUploadStoreV1(workspace)
    try:
        assert second.manifest(manifest["dataset_id"]) == manifest
        _, dataset = second.load(manifest["dataset_id"])
        assert dataset.selection.fingerprint.sha256.value == manifest["data_sha256"]
        assert second.version_evidence_digest(manifest["dataset_id"]) == digest
        assert [entry["dataset_id"] for entry in second.list()] == [manifest["dataset_id"]]
        assert second.inspect(manifest["dataset_id"])["provider"]["provider"]["id"] == PROVIDER_ID
    finally:
        second.close()


# --- one bound provider version through the real engine ----------------------


def _scenario_query() -> HistoricalBarQueryV1:
    """A synthetic query over exactly the window the declarative fixture uses."""
    return _query(
        venue="XNAS",
        symbol="AAPL",
        interval="1m",
        start_ms=_ms(SCENARIO_START),
        end_ms=_ms(SCENARIO_END),
        latency_seconds=0,
    )


def _scenario_records() -> list[Any]:
    return [_record(_ms(SCENARIO_START) + index * MINUTE_MS, interval="1m") for index in range(3)]


def test_a_bound_provider_version_runs_the_engine_and_keeps_its_evidence(
    tmp_path: Path,
) -> None:
    """Synthetic provider bytes in, one real engine run, the same evidence after a restart.

    The bars are this test's own synthetic bytes, built over the window the
    declarative scenario's fixture uses and read through a test-owned transport.
    Everything after the adapter is the existing engine, its own report, the
    existing controlled upload store and the existing bound version evidence.
    """
    # One scenario root: a restart reads the same scenarios and the same workspace.
    root = _declarative_root(tmp_path)
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        query = _scenario_query()
        capture = _fetch(
            query,
            ScriptedTransport(_page(_scenario_records())),
            observed_ms=_closed(query),
            ingested_ms=_closed(query) + 1_000,
        )
        manifest = service.uploads.publish_historical_bars(
            capture, file_name="klines-envelope.json", source_reference="synthetic fixture"
        )
        dataset_id = manifest["dataset_id"]
        assert manifest["schema"] == PROVIDER_VERSION_SCHEMA
        assert manifest["instrument"] == {"venue": "XNAS", "symbol": "AAPL"}
        assert manifest["availability_policy"]["id"] == AVAILABILITY_SIMULATED
        assert manifest["availability_policy"]["pit_verified"] is False
        binding = _bound_binding(service, dataset_id)
        digest = service.uploads.version_evidence_digest(dataset_id)
        assert binding.component("data").identity["version_evidence_sha256"] == digest

        validated = service.validate_scenario(
            "rule-entry.yaml", dataset_id=dataset_id, parameters=_parameters()
        )
        job, created = service.create_job(
            scenario_id="rule-entry.yaml",
            input_identity=cast("dict[str, object]", validated["input_identity"]),
            parameters=_parameters(),
            request_id="provider-run",
            plugin_binding=binding.document(),
        )
        assert created is True
        record = _await_job(service, job.job_id)
        assert record.status == "succeeded", record.message

        # The provider's query, time semantics and evidence travel with the job.
        version = cast("dict[str, Any]", record.dataset_version)
        assert version["schema"] == PROVIDER_VERSION_SCHEMA
        assert version["provider_evidence_sha256"] == manifest["provider_evidence_sha256"]
        assert version["provider_evidence"] == capture.provider_evidence
        declaration = cast(
            "dict[str, Any]", cast("dict[str, Any]", version["provider_evidence"])["declaration"]
        )
        sealed_query = cast("dict[str, Any]", declaration["query"])
        assert sealed_query["venue"] == "XNAS" and sealed_query["symbol"] == "AAPL"
        assert sealed_query["interval"] == "1m"
        assert sealed_query["end_time_param_ms"] == _ms(SCENARIO_END) - 1
        assert declaration["observed_now_utc"] == capture.envelope["observed_now_utc"]
        assert declaration["policy"]["pit_verified"] is False

        report = json.loads(service.report(job.job_id))
        assert report["source"]["data"]["fingerprint"]["sha256"] == manifest["data_sha256"]
        assert record.input_identity["source_sha256"] == manifest["capture_sha256"]
        # The retained raw source is the envelope, not the strict capture.
        assert manifest["raw_retained"] is True
        assert manifest["source_bytes_sha256"] != manifest["capture_sha256"]
    finally:
        service.stop()

    # A restart re-reads the same version from its retained envelope and derives
    # the same evidence, and the finished job still carries that same evidence.
    reopened = WebService(root, tmp_path / "workspace")
    reopened.start()
    try:
        assert reopened.uploads.version_evidence_digest(dataset_id) == digest
        again = reopened.get_job(job.job_id)
        assert again.status == "succeeded"
        assert again.dataset_version == record.dataset_version
        inspection = reopened.uploads.inspect(dataset_id)
        assert inspection["provider"]["query"]["symbol"] == "AAPL"
        assert inspection["provider"]["evidence_sha256"] == manifest["provider_evidence_sha256"]
    finally:
        reopened.stop()


# --- the existing strict entrance is unchanged -------------------------------


def _strict_content(rows: int = 2) -> bytes:
    lines = [",".join(STRICT_HEADER)]
    for index in range(rows):
        start = START_MS + index * HOUR_MS
        lines.append(
            ",".join(
                (
                    "1",
                    "XNAS",
                    "AAPL",
                    _stamp(start),
                    _stamp(start + HOUR_MS),
                    "raw",
                    "100.0",
                    "101.0",
                    "99.0",
                    "100.5",
                    "10.0",
                    "fixture",
                    str(index),
                    "0",
                    _stamp(start + HOUR_MS),
                )
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_the_existing_strict_upload_entrance_is_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        content = _strict_content()
        manifest = store.publish(
            content, availability_policy="ea.availability.provided-in-capture.v1"
        )
        assert manifest["schema"] == UPLOAD_VERSION_SCHEMA
        assert manifest["source"]["declared_by"] == "user"
        path, dataset = store.load(manifest["dataset_id"])
        assert path.read_bytes() == content
        assert dataset.selection.fingerprint.record_count == 2
        evidence = store.version_evidence(manifest["dataset_id"])
        assert evidence["source"] == {
            "declared_by": "user",
            "asserted": True,
            "reference": None,
            "file_name": None,
            "license_reference": None,
        }
        assert "provider" not in evidence
        assert "mapping" not in evidence
    finally:
        store.close()
