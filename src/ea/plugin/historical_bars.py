"""Read-only historical Bar adapter over one documented public klines shape.

This module is one *replaceable* provider contract plus one normalizer. It reads
the documented response shape of the fixed public endpoint
``https://data-api.binance.vision/api/v3/klines`` — twelve fields per record, the
opening epoch millisecond at index 0, the four OHLC values and the base volume as
strings at indexes 1..5, the *inclusive* closing epoch millisecond at index 6,
and five further documented fields at indexes 7..11 that are retained but never
interpreted — and turns a bounded, complete set of such records into the one
strict Phase 1 OHLCV capture the rest of this product already reads. Nothing here
re-implements an engine, a decoder or a store: the normalized capture is decoded
in full by the same strict decoder every other entrance uses.

Three boundaries are structural rather than promised, and none of them is a
claim that this host may use the endpoint:

* **This build contains no network transport at all.** There is no HTTP client,
  no socket, no DNS lookup and no retry against another host, mirror, proxy or
  region. ``fetch_historical_bars`` refuses with ``network_disabled`` unless the
  caller injects its own transport object, and every byte it then reads arrived
  from that object. A caller that wants to evaluate the protocol does so offline
  against a synthetic transport; a caller that wants to talk to the real provider
  must supply their own transport *and* establish their own separate, explicit
  right to use that provider's data.
* **Availability remains simulated and unverified.** ``event_time`` is the bar's
  exclusive ``interval_end``; ``available_at`` is ``interval_end`` plus one
  explicitly supplied non-negative latency; ``ingested_at`` is recorded
  separately. No point-in-time verification, vintage, as-of, licence or
  permission is claimed anywhere in the envelope or in the evidence derived from
  it, and no licence is fabricated.
* **The provider's terms are unresolved.** No data-use grant was established for
  this endpoint without accepting a financial agreement, and this host accepts no
  agreement. The envelope and the evidence therefore say so explicitly. Real use
  is blocked for this host; the module exists so that the request/response
  protocol, the bounds and the provenance can be evaluated offline.

Everything a later reader needs is derived again rather than trusted: the
retained raw source is the exact response envelope (page bytes base64-encoded
beside the exact query, provider, normalizer, policy and capture digests), and a
read replays the normalizer over those retained page bytes under the sealed
declaration and requires the capture to be reproduced exactly.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, NoReturn, Protocol, cast

from ea.core.identity import IdentityValidationError, Instrument, VenueId
from ea.core.time import TimeValidationError, require_utc
from ea.data.historical import (
    _FLOAT_PATTERN,
    PHASE1_OHLCV_PROFILE,
    HistoricalMarketDataError,
    _decode_phase1_capture,
)
from ea.data.historical import (
    _HEADER as STRICT_HEADER,
)

# One exact provider and one exact normalizer version. A capture records both, and
# a read refuses an envelope that names anything else rather than reading it as if
# a different provider or a different normalizer had produced it.
PROVIDER_ID = "binance.public-data"
PROVIDER_VERSION = "spot-klines-v3"
PROVIDER_ENDPOINT = "https://data-api.binance.vision/api/v3/klines"
NORMALIZER_ID = "ea.binance-klines-normalizer"
NORMALIZER_VERSION = "1.0.0"

ENVELOPE_SCHEMA = "ea.historical-bars-response-envelope.v1"
PROVIDER_EVIDENCE_SCHEMA = "ea.historical-bars-provider-evidence.v1"

# The two provenances an offline read records. Neither is a network read and
# neither is a claim about the provider: one is one user's own pasted response,
# the other is the host's built-in fixture. They are recorded verbatim on the
# envelope's transport declaration, so a reader can tell them apart and so a
# store can refuse a marking that does not match the bytes.
OFFLINE_RESPONSE_KIND_USER = "user-pasted-offline-response"
OFFLINE_RESPONSE_KIND_SYNTHETIC = "builtin-synthetic-example"
OFFLINE_RESPONSE_KINDS = (OFFLINE_RESPONSE_KIND_USER, OFFLINE_RESPONSE_KIND_SYNTHETIC)
OFFLINE_RESPONSE_MEDIA_TYPE = "application/json"

# One built-in synthetic example: a fixed UTC day of hourly bars in the exact
# documented shape. It exists so that preview, publication and catalog selection
# can be exercised end to end without a provider, a network or any real captured
# bytes. It is a fixture, never an observation, and a store accepts the synthetic
# marking for exactly these bytes and this declaration and for nothing else.
SYNTHETIC_EXAMPLE_SCHEMA = "ea.offline-history-example.v1"
SYNTHETIC_EXAMPLE_ID = "ea.offline-history.synthetic-klines-1h.v1"
SYNTHETIC_EXAMPLE_VENUE = "BINANCE"
SYNTHETIC_EXAMPLE_SYMBOL = "BTCUSDT"
SYNTHETIC_EXAMPLE_INTERVAL = "1h"
SYNTHETIC_EXAMPLE_START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
SYNTHETIC_EXAMPLE_RECORD_COUNT = 24
SYNTHETIC_EXAMPLE_LATENCY_SECONDS = 0

# The documented response shape this adapter reads, field by field. It is a
# declaration of what was read, never a claim that a live request established it.
DOCUMENTED_KLINES_FIELDS: tuple[str, ...] = (
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "quote_asset_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)
DOCUMENTED_KLINES_FIELD_COUNT = len(DOCUMENTED_KLINES_FIELDS)

# The canonical source namespace a normalized row is stamped with.
SOURCE_CODE = "binance.public-data.klines"

# Only fixed UTC minute and hour intervals are accepted. Day, week and month
# intervals are refused with their own code rather than approximated.
ONE_MINUTE_MS = 60_000
ONE_HOUR_MS = 60 * ONE_MINUTE_MS
INTERVALS: dict[str, int] = {
    "1m": ONE_MINUTE_MS,
    "3m": 3 * ONE_MINUTE_MS,
    "5m": 5 * ONE_MINUTE_MS,
    "15m": 15 * ONE_MINUTE_MS,
    "30m": 30 * ONE_MINUTE_MS,
    "1h": ONE_HOUR_MS,
    "2h": 2 * ONE_HOUR_MS,
    "4h": 4 * ONE_HOUR_MS,
    "6h": 6 * ONE_HOUR_MS,
    "8h": 8 * ONE_HOUR_MS,
    "12h": 12 * ONE_HOUR_MS,
}

# Bounds. Every one of them is enforced before the thing it bounds is allocated,
# and every one of them is recorded on the capture's policy so a reader can see
# exactly which limits produced the bytes it is reading.
PAGE_LIMIT = 1_000
MAX_PAGES = 16
# The record bound is implied by the page bound and the requested page size and is
# re-checked in the normalizer, which is also reached when a version is re-derived
# from its retained envelope, where no page loop bounds the page list.
MAX_RECORDS = MAX_PAGES * PAGE_LIMIT
MAX_PAGE_BYTES = 512 * 1024
MAX_TOTAL_RESPONSE_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_429_ATTEMPTS = 4
MAX_RETRY_AFTER_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
MAX_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_DEADLINE_SECONDS = 120.0
MAX_DEADLINE_SECONDS = 600.0
MAX_LATENCY_SECONDS = 366 * 24 * 3600

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNSIGNED = re.compile(r"(?:0|[1-9][0-9]*)\Z", flags=re.ASCII)
# The representable end of the strict profile's own UTC second range.
MAX_EPOCH_MS = 253_402_300_799_999

Monotonic = Callable[[], float]
Sleeper = Callable[[float], None]

# True of every capture this adapter produces, whichever transport produced it.
_LIMITATIONS: tuple[str, ...] = (
    "Synthetic/offline protocol evaluation only. The response bytes were supplied by a "
    "caller-provided transport object; this build implements no production network transport "
    "and no live request to the provider is claimed or made.",
    "The provider's terms are unresolved. No data-use grant, licence or permission was "
    "established without accepting a financial agreement, and this host accepts no agreement. "
    "Actual use of the documented endpoint is blocked pending a separate explicit grant.",
    "available_at is simulated as interval_end plus the explicitly supplied non-negative "
    "latency. It is not verified point-in-time availability, and no vintage, as-of or "
    "publication-time claim is made. ingested_at is recorded separately.",
    "Only the documented twelve-field klines shape is read. Fields 7..11 are retained in the "
    "page bytes exactly as they arrived and are not interpreted.",
    "No alternative host, mirror, proxy, credential or geographic workaround is ever attempted. "
    "A redirect, authorization or restricted-region answer is refused.",
    "The retained raw source is one canonical JSON response envelope, not the provider's own "
    "bytes alone. This store's dataset identifier carries a fixed '.csv' suffix that is a "
    "catalog identity and not a statement about the retained bytes.",
)

# True of one user's own pasted response read offline. It deliberately does not
# inherit the transport statements above: no caller-supplied transport object was
# involved, the pasted bytes are the user's own, and the user — not a provider
# protocol — is the one asserting where they came from.
USER_OFFLINE_LIMITATIONS: tuple[str, ...] = (
    "Offline import only. The response bytes were pasted by the user and read in this process; "
    "this build contains no network transport, makes no request, holds no provider credential "
    "and resolves none.",
    "The source, the rights/licence and the intended use are the user's own assertion. They are "
    "recorded verbatim and are not a legal verification, and no permission or grant is "
    "established by recording them.",
    "available_at is simulated as interval_end plus the explicitly declared latency. It is not "
    "verified point-in-time availability and no vintage, as-of or publication-time claim is "
    "made; ingested_at is recorded separately.",
    "Only the documented twelve-field klines shape is read. Fields 7..11 are retained in the "
    "pasted bytes exactly as they arrived and are not interpreted.",
    "The retained raw source is one canonical JSON response envelope holding the exact pasted "
    "bytes base64-encoded beside the exact query, adapter version and assumptions. This store's "
    "dataset identifier carries a fixed '.csv' suffix that is a catalog identity and not a "
    "statement about the retained bytes.",
)

# True of the host's built-in synthetic example. It is a fixture, and none of it
# is an uploader assertion or an observation of any market.
SYNTHETIC_OFFLINE_LIMITATIONS: tuple[str, ...] = (
    "Built-in synthetic example. These bytes are a fixture the host ships, not user-supplied "
    "data and not an observation of any market. They exercise preview, publication and catalog "
    "selection and support no research claim.",
    "Offline only: no network request was made, this build contains no network transport, and "
    "no provider permission, licence or point-in-time verification is established or claimed.",
    "available_at is simulated as interval_end plus the declared latency; it is not verified "
    "point-in-time availability.",
    "Only the documented twelve-field klines shape is read; fields 7..11 are retained "
    "uninterpreted.",
)


class HistoricalBarFailureCode(StrEnum):
    """Closed failure vocabulary for the historical Bar adapter."""

    INVALID_QUERY = "invalid_query"
    INVALID_INTERVAL = "invalid_interval"
    INVALID_LATENCY = "invalid_latency"
    NETWORK_DISABLED = "network_disabled"
    TRANSPORT_FAILURE = "transport_failure"
    TRANSPORT_CONTRACT = "transport_contract"
    PROVIDER_STATUS = "provider_status"
    PROVIDER_REDIRECT = "provider_redirect"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"
    PROVIDER_RESTRICTED = "provider_restricted"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    RETRY_AFTER_INVALID = "retry_after_invalid"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PAGE_BUDGET_EXCEEDED = "page_budget_exceeded"
    RECORD_BUDGET_EXCEEDED = "record_budget_exceeded"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"
    MALFORMED_RECORD = "malformed_record"
    UNALIGNED_BAR = "unaligned_bar"
    OUT_OF_RANGE_BAR = "out_of_range_bar"
    UNCLOSED_BAR = "unclosed_bar"
    DUPLICATE_BAR = "duplicate_bar"
    OUT_OF_ORDER_BAR = "out_of_order_bar"
    EMPTY_SELECTION = "empty_selection"
    ENVELOPE_INVALID = "envelope_invalid"
    OFFLINE_RESPONSE_INVALID = "offline_response_invalid"
    OFFLINE_RESPONSE_INCOMPLETE = "offline_response_incomplete"


class HistoricalBarError(ValueError):
    """One adapter failure with a stable code and bounded location evidence."""

    def __init__(
        self,
        code: HistoricalBarFailureCode,
        message: str,
        *,
        page_index: int | None = None,
        record_index: int | None = None,
        field_name: str | None = None,
    ) -> None:
        if type(code) is not HistoricalBarFailureCode:
            raise TypeError("historical bar errors require an exact failure code")
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.page_index = page_index
        self.record_index = record_index
        self.field_name = field_name

    def document(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "page_index": self.page_index,
            "record_index": self.record_index,
            "field_name": self.field_name,
        }


def _fail(
    code: HistoricalBarFailureCode,
    message: str,
    *,
    page_index: int | None = None,
    record_index: int | None = None,
    field_name: str | None = None,
) -> NoReturn:
    raise HistoricalBarError(
        code,
        message,
        page_index=page_index,
        record_index=record_index,
        field_name=field_name,
    )


def _canonical_json(document: object) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


# --- query -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalBarQueryV1:
    """One explicit read-only query: one instrument, one fixed UTC interval.

    ``[start_ms, end_ms)`` is the half-open UTC range. Both bounds must be exact
    multiples of the interval, so the range is composed of whole bars and the
    inclusive ``endTime`` this adapter sends is exactly ``end_ms - 1``.
    ``latency_seconds`` has no default: the caller states how long after a bar's
    close it becomes available, or the query is refused.
    """

    venue: str
    symbol: str
    interval: str
    start_ms: int
    end_ms: int
    latency_seconds: int

    def __post_init__(self) -> None:
        _validated_query(self)

    def document(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "interval": self.interval,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "end_time_param_ms": self.end_ms - 1,
            "latency_seconds": self.latency_seconds,
        }


def _validated_query(query: object) -> HistoricalBarQueryV1:
    """Re-read one query under the closed contract this adapter dispatches on."""
    if type(query) is not HistoricalBarQueryV1:
        _fail(HistoricalBarFailureCode.INVALID_QUERY, "a query must be one HistoricalBarQueryV1")
    try:
        VenueId(query.venue)
        Instrument(venue=VenueId(query.venue), symbol=query.symbol)
    except (IdentityValidationError, TypeError):
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            "venue and symbol must be one canonical venue-local instrument identity",
        )
    if type(query.interval) is not str or query.interval not in INTERVALS:
        _fail(
            HistoricalBarFailureCode.INVALID_INTERVAL,
            f"interval must be one fixed UTC minute or hour interval: {', '.join(INTERVALS)}",
        )
    step = INTERVALS[query.interval]
    for name, value in (("start_ms", query.start_ms), ("end_ms", query.end_ms)):
        if type(value) is not int or isinstance(value, bool) or not 0 <= value <= MAX_EPOCH_MS:
            _fail(
                HistoricalBarFailureCode.INVALID_QUERY,
                f"{name} must be one epoch millisecond value this host can represent",
                field_name=name,
            )
    if query.end_ms <= query.start_ms:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            "the half-open UTC range must not be empty",
            field_name="end_ms",
        )
    if query.start_ms % step or query.end_ms % step:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            f"both bounds must be exact multiples of the {query.interval} interval, so the "
            "range is composed of whole bars",
        )
    latency = query.latency_seconds
    if (
        type(latency) is not int
        or isinstance(latency, bool)
        or not 0 <= latency <= MAX_LATENCY_SECONDS
    ):
        _fail(
            HistoricalBarFailureCode.INVALID_LATENCY,
            "latency_seconds must be one explicit non-negative whole number of seconds; the "
            "host never assumes an availability latency",
        )
    return query


# --- transport contract ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalBarHttpResponseV1:
    """One transport answer: a status, the headers, and the exact body bytes."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        """The first case-insensitive match for one header name, or nothing."""
        lowered = name.lower()
        for key, value in self.headers:
            if type(key) is str and key.lower() == lowered:
                return value if type(value) is str else None
        return None


class HistoricalBarTransportV1(Protocol):
    """The one call this adapter makes. Implementations are supplied by the caller.

    This build ships no implementation: a transport is the caller's own object,
    and every byte this adapter reads came from it. A transport must return the
    provider's status, headers and body unchanged; it must not follow a redirect,
    substitute a host, retry on its own, or resolve a credential.
    """

    def request(
        self,
        *,
        url: str,
        params: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> HistoricalBarHttpResponseV1:
        """Perform exactly one request against exactly ``url``."""
        ...


# --- capture -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalBarCaptureV1:
    """One normalized strict capture plus the exact response envelope behind it.

    ``strict_csv`` is what the engine reads; ``envelope_json`` is the retained raw
    source, one canonical JSON document holding the exact page bytes beside the
    exact query, provider, normalizer, policy and limitations. Both are handed out
    as fresh copies, so editing what these properties return cannot change what
    this capture reports.
    """

    profile: str
    strict_csv: bytes
    envelope_json: str
    evidence_json: str
    capture_sha256: str
    capture_byte_count: int
    data_sha256: str
    record_count: int
    venue: str
    symbol: str

    @property
    def envelope(self) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(self.envelope_json))

    @property
    def provider_evidence(self) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(self.evidence_json))

    def document(self) -> dict[str, Any]:
        return self.envelope


@dataclass(slots=True)
class _Page:
    """One accepted response page, exactly as it arrived."""

    index: int
    url: str
    params: tuple[tuple[str, str], ...]
    timeout_seconds: float
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class _Bar:
    """One validated documented record: the endpoints and the five numeric tokens."""

    open_ms: int
    close_ms: int
    open_token: str
    high_token: str
    low_token: str
    close_token: str
    volume_token: str


@dataclass(frozen=True, slots=True)
class _Normalized:
    """One capture the strict decoder accepted in full."""

    strict_csv: bytes
    data_sha256: str
    record_count: int
    first_open_ms: int
    last_close_ms: int


@dataclass(slots=True)
class _Accumulator:
    """Validated records in arrival order; the cursor moves only when this accepts.

    A page is validated in full before anything here changes, so a rejected page
    leaves the cursor exactly where it was and no partial page is ever accepted.
    """

    query: HistoricalBarQueryV1
    observed_now_ms: int
    bars: list[_Bar] = field(default_factory=list)

    @property
    def cursor_ms(self) -> int:
        if not self.bars:
            return self.query.start_ms
        return self.bars[-1].close_ms + 1

    def accept(self, records: list[Any], page_index: int) -> None:
        step = INTERVALS[self.query.interval]
        accepted: list[_Bar] = []
        for record_index, raw in enumerate(records, start=1):
            bar = _validated_record(
                raw, query=self.query, page_index=page_index, record_index=record_index
            )
            if bar.close_ms + 1 != bar.open_ms + step:
                _fail(
                    HistoricalBarFailureCode.UNALIGNED_BAR,
                    "the inclusive close time must be exactly one interval after the open time",
                    page_index=page_index,
                    record_index=record_index,
                    field_name="close_time_ms",
                )
            if bar.close_ms + 1 > self.observed_now_ms:
                _fail(
                    HistoricalBarFailureCode.UNCLOSED_BAR,
                    "the bar is not closed as of the explicitly observed UTC now",
                    page_index=page_index,
                    record_index=record_index,
                )
            if bar.open_ms < self.query.start_ms or bar.close_ms + 1 > self.query.end_ms:
                _fail(
                    HistoricalBarFailureCode.OUT_OF_RANGE_BAR,
                    "the bar is not contained in the queried half-open UTC range",
                    page_index=page_index,
                    record_index=record_index,
                )
            if self.bars or accepted:
                previous = accepted[-1].open_ms if accepted else self.bars[-1].open_ms
                if bar.open_ms == previous:
                    _fail(
                        HistoricalBarFailureCode.DUPLICATE_BAR,
                        "the page repeats a bar this capture already holds",
                        page_index=page_index,
                        record_index=record_index,
                    )
                if bar.open_ms < previous:
                    _fail(
                        HistoricalBarFailureCode.OUT_OF_ORDER_BAR,
                        "the page returns a bar before the last accepted one",
                        page_index=page_index,
                        record_index=record_index,
                    )
            accepted.append(bar)
        self.bars.extend(accepted)


def _validated_record(
    raw: object, *, query: HistoricalBarQueryV1, page_index: int, record_index: int
) -> _Bar:
    """Read one documented record; values are checked by the strict decoder, not here."""
    if type(raw) is not list or len(raw) != DOCUMENTED_KLINES_FIELD_COUNT:
        _fail(
            HistoricalBarFailureCode.MALFORMED_RECORD,
            f"a record must be the documented {DOCUMENTED_KLINES_FIELD_COUNT}-field list",
            page_index=page_index,
            record_index=record_index,
        )
    record = raw
    open_ms = _epoch_ms(
        record[0], field_name="open_time_ms", page_index=page_index, record_index=record_index
    )
    close_ms = _epoch_ms(
        record[6], field_name="close_time_ms", page_index=page_index, record_index=record_index
    )
    tokens: list[str] = []
    for index in (1, 2, 3, 4, 5):
        token = record[index]
        if type(token) is not str or _FLOAT_PATTERN.fullmatch(token) is None:
            _fail(
                HistoricalBarFailureCode.MALFORMED_RECORD,
                "the documented OHLC and volume fields must be numeric strings the strict "
                "profile can read unchanged",
                page_index=page_index,
                record_index=record_index,
                field_name=DOCUMENTED_KLINES_FIELDS[index],
            )
        tokens.append(token)
    for index in range(7, DOCUMENTED_KLINES_FIELD_COUNT):
        _retained_field(
            record[index], index=index, page_index=page_index, record_index=record_index
        )
    return _Bar(open_ms, close_ms, tokens[0], tokens[1], tokens[2], tokens[3], tokens[4])


def _epoch_ms(value: object, *, field_name: str, page_index: int, record_index: int) -> int:
    if type(value) is not int or isinstance(value, bool) or not 0 <= value <= MAX_EPOCH_MS:
        _fail(
            HistoricalBarFailureCode.MALFORMED_RECORD,
            f"{field_name} must be one epoch millisecond integer",
            page_index=page_index,
            record_index=record_index,
            field_name=field_name,
        )
    return value


def _retained_field(value: object, *, index: int, page_index: int, record_index: int) -> None:
    """Check only that a retained field is a documented scalar; never interpret it."""
    if type(value) is bool or not isinstance(value, (str, int, float)):
        _fail(
            HistoricalBarFailureCode.MALFORMED_RECORD,
            "a retained field must be one documented scalar value",
            page_index=page_index,
            record_index=record_index,
            field_name=DOCUMENTED_KLINES_FIELDS[index],
        )


def _page_records(body: bytes, page_index: int) -> list[Any]:
    """Parse one page body as the documented JSON array, or refuse it."""
    if len(body) > MAX_PAGE_BYTES:
        _fail(
            HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
            f"the response page is larger than the {MAX_PAGE_BYTES}-byte page bound",
            page_index=page_index,
        )
    try:
        payload = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        _fail(
            HistoricalBarFailureCode.MALFORMED_RESPONSE,
            "the response body is not the documented JSON document",
            page_index=page_index,
        )
    if type(payload) is not list:
        _fail(
            HistoricalBarFailureCode.MALFORMED_RESPONSE,
            "the response body is not the documented JSON array of records",
            page_index=page_index,
        )
    for record_index, record in enumerate(payload, start=1):
        if type(record) is not list or len(record) != DOCUMENTED_KLINES_FIELD_COUNT:
            _fail(
                HistoricalBarFailureCode.MALFORMED_RECORD,
                f"a record must be the documented {DOCUMENTED_KLINES_FIELD_COUNT}-field list",
                page_index=page_index,
                record_index=record_index,
            )
    return payload


def _stamp(epoch_ms: int, *, page_index: int | None = None) -> str:
    try:
        value = _EPOCH + timedelta(milliseconds=epoch_ms)
    except OverflowError:
        _fail(
            HistoricalBarFailureCode.OUT_OF_RANGE_BAR,
            "the instant is outside the range the strict profile can represent as UTC",
            page_index=page_index,
        )
    return value.strftime(_STAMP_FORMAT)


def _capture_bytes(query: HistoricalBarQueryV1, bars: list[_Bar]) -> bytes:
    latency_ms = query.latency_seconds * 1000
    rows = [",".join(STRICT_HEADER)]
    for index, bar in enumerate(bars):
        interval_end_ms = bar.close_ms + 1
        available_ms = interval_end_ms + latency_ms
        if available_ms > MAX_EPOCH_MS:
            _fail(
                HistoricalBarFailureCode.INVALID_LATENCY,
                "the declared latency moves available_at past the range the strict profile can "
                "represent as UTC",
            )
        rows.append(
            ",".join(
                (
                    "1",
                    query.venue,
                    query.symbol,
                    _stamp(bar.open_ms),
                    _stamp(interval_end_ms),
                    "raw",
                    bar.open_token,
                    bar.high_token,
                    bar.low_token,
                    bar.close_token,
                    bar.volume_token,
                    SOURCE_CODE,
                    str(index),
                    "0",
                    _stamp(available_ms),
                )
            )
        )
    return ("\n".join(rows) + "\n").encode("utf-8")


def _validated_page_params(
    params: object, query: HistoricalBarQueryV1, cursor_ms: int, page_index: int
) -> None:
    """Re-read one retained request against the cursor the accepted records imply.

    A retained page carries the request that produced it. Re-derivation therefore
    checks that request against the query and against the cursor that the records
    accepted so far produce: a page whose retained metadata claims another symbol,
    interval, range or start cannot be replayed as if it were this version's
    evidence, even when every digest has been re-sealed consistently.
    """
    if type(params) is not list or len(params) != 5:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "a retained request is not the closed five-parameter list",
            page_index=page_index,
        )
    expected = (
        ("symbol", query.symbol),
        ("interval", query.interval),
        ("startTime", str(cursor_ms)),
        ("endTime", str(query.end_ms - 1)),
        ("limit", str(PAGE_LIMIT)),
    )
    seen: list[tuple[object, object]] = []
    for pair in params:
        if type(pair) is not list or len(pair) != 2:
            _fail(
                HistoricalBarFailureCode.ENVELOPE_INVALID,
                "a retained request parameter is not one name/value pair",
                page_index=page_index,
            )
        seen.append((pair[0], pair[1]))
    if tuple(seen) != expected:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained request does not match the query and the cursor the accepted records "
            "produce",
            page_index=page_index,
        )


def _normalize(
    query: HistoricalBarQueryV1,
    page_bodies: list[bytes],
    observed_now_ms: int,
    page_params: list[Any] | None = None,
) -> _Normalized:
    """Turn validated complete pages into one strict capture, or refuse."""
    accumulator = _Accumulator(_validated_query(query), observed_now_ms)
    for page_index, body in enumerate(page_bodies, start=1):
        if page_params is not None:
            _validated_page_params(
                page_params[page_index - 1], query, accumulator.cursor_ms, page_index
            )
        records = _page_records(body, page_index)
        accumulator.accept(records, page_index)
        if len(accumulator.bars) > MAX_RECORDS:
            _fail(
                HistoricalBarFailureCode.RECORD_BUDGET_EXCEEDED,
                f"the capture holds more than the {MAX_RECORDS}-record bound",
                page_index=page_index,
            )
    if not accumulator.bars:
        _fail(
            HistoricalBarFailureCode.EMPTY_SELECTION,
            "the provider returned no record inside the queried half-open UTC range",
        )
    strict_csv = _capture_bytes(query, accumulator.bars)
    try:
        dataset = _decode_phase1_capture(strict_csv, replay_window=None)
    except HistoricalMarketDataError as error:
        raise HistoricalBarError(
            _profile_code(error),
            "the normalized capture was refused by the strict profile decoder "
            f"({error.code.value})",
            record_index=error.record_number,
            field_name=error.field_name,
        ) from None
    return _Normalized(
        strict_csv=strict_csv,
        data_sha256=dataset.selection.fingerprint.sha256.value,
        record_count=dataset.selection.fingerprint.record_count,
        first_open_ms=accumulator.bars[0].open_ms,
        last_close_ms=accumulator.bars[-1].close_ms,
    )


def _profile_code(error: HistoricalMarketDataError) -> HistoricalBarFailureCode:
    """The strict decoder's own refusal, reported as an adapter failure.

    The decoder stays the only authority on whether a capture is valid; this maps
    its verdict onto the adapter's closed vocabulary without re-deciding it.
    """
    if error.code.value in {"invalid_bar", "invalid_envelope"}:
        return HistoricalBarFailureCode.MALFORMED_RECORD
    if error.code.value == "invalid_timestamp":
        return HistoricalBarFailureCode.OUT_OF_RANGE_BAR
    return HistoricalBarFailureCode.MALFORMED_RESPONSE


# --- fetch -------------------------------------------------------------------


def _utc_instant(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc(cast("datetime", value), field=field_name)
    except TimeValidationError:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            f"{field_name} must be one explicit timezone-aware UTC instant",
            field_name=field_name,
        )


def _instant_ms(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(milliseconds=1)


def _bounded_seconds(value: object, *, field_name: str, maximum: float) -> float:
    if type(value) is not float and type(value) is not int:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            f"{field_name} must be an explicit number of seconds",
            field_name=field_name,
        )
    seconds = float(cast("float", value))
    if not 0.0 < seconds <= maximum:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            f"{field_name} must be above zero and at most {maximum} seconds",
            field_name=field_name,
        )
    return seconds


def _retry_after(response: HistoricalBarHttpResponseV1) -> float:
    """One bounded whole-second Retry-After, or a refusal — never a guessed delay."""
    value = response.header("retry-after")
    if value is None:
        _fail(
            HistoricalBarFailureCode.RETRY_AFTER_INVALID,
            "a rate-limited answer without Retry-After is refused rather than retried on a "
            "guessed delay",
        )
    text = value.strip()
    if _UNSIGNED.fullmatch(text) is None:
        _fail(
            HistoricalBarFailureCode.RETRY_AFTER_INVALID,
            "Retry-After must be a non-negative whole number of seconds",
        )
    seconds = int(text)
    if seconds > MAX_RETRY_AFTER_SECONDS:
        _fail(
            HistoricalBarFailureCode.RETRY_AFTER_INVALID,
            f"Retry-After is above this adapter's {MAX_RETRY_AFTER_SECONDS}-second bound",
        )
    return float(seconds)


def _request_page(
    transport: HistoricalBarTransportV1,
    query: HistoricalBarQueryV1,
    cursor_ms: int,
    *,
    timeout_seconds: float,
    monotonic: Monotonic,
    sleeper: Sleeper,
    started: float,
    deadline_seconds: float,
) -> _Page:
    """One bounded request, with a bounded and injected 429 backoff.

    The elapsed clock is read immediately after every request returns, so an
    answer that arrives after the declared deadline is refused even when it is a
    well-formed ``200``: a late page is not evidence about the queried range. Each
    attempt is also handed the smaller of the declared request timeout and the time
    left before the deadline, so a retry can never be given more time than the
    deadline leaves.

    What this cannot do is pre-empt the caller's own code. A supplied transport
    runs in this process and is not forcibly interrupted: the timeout is a value
    passed to it, and a transport that ignores it can still return late. This
    adapter therefore refuses an overdue answer rather than trusting it, and a
    transport that must never overrun has to enforce its own request timeout.
    """
    params = (
        ("symbol", query.symbol),
        ("interval", query.interval),
        ("startTime", str(cursor_ms)),
        ("endTime", str(query.end_ms - 1)),
        ("limit", str(PAGE_LIMIT)),
    )
    attempts = 0
    while True:
        attempts += 1
        remaining = deadline_seconds - (monotonic() - started)
        if remaining <= 0.0:
            _fail(
                HistoricalBarFailureCode.DEADLINE_EXCEEDED,
                f"no time is left before the {deadline_seconds}-second deadline to request a page",
            )
        try:
            response = transport.request(
                url=PROVIDER_ENDPOINT,
                params=params,
                timeout_seconds=min(timeout_seconds, remaining),
            )
        except HistoricalBarError:
            raise
        except Exception as error:  # the transport is caller-supplied code
            _fail(
                HistoricalBarFailureCode.TRANSPORT_FAILURE,
                f"the supplied transport raised {type(error).__name__}; no other host, mirror or "
                "proxy is ever attempted",
            )
        if type(response) is not HistoricalBarHttpResponseV1:
            _fail(
                HistoricalBarFailureCode.TRANSPORT_CONTRACT,
                "the supplied transport must return one HistoricalBarHttpResponseV1",
            )
        if deadline_seconds - (monotonic() - started) <= 0.0:
            _fail(
                HistoricalBarFailureCode.DEADLINE_EXCEEDED,
                f"the response arrived after the {deadline_seconds}-second deadline and is "
                "refused rather than accepted late",
            )
        answer = response
        if answer.status == 200:
            return _Page(
                index=0,
                url=PROVIDER_ENDPOINT,
                params=params,
                timeout_seconds=timeout_seconds,
                status=answer.status,
                headers=_bounded_headers(answer.headers),
                body=answer.body,
            )
        if 300 <= answer.status < 400 or answer.header("location") is not None:
            _fail(
                HistoricalBarFailureCode.PROVIDER_REDIRECT,
                f"the provider answered HTTP {answer.status}; a redirect is refused and no "
                "alternative host is tried",
            )
        if answer.status in {401, 403}:
            _fail(
                HistoricalBarFailureCode.PROVIDER_UNAUTHORIZED,
                f"the provider answered HTTP {answer.status}; this host holds no credential and "
                "resolves none",
            )
        if answer.status in {418, 451}:
            _fail(
                HistoricalBarFailureCode.PROVIDER_RESTRICTED,
                f"the provider answered HTTP {answer.status}; a restricted or blocked answer is "
                "refused and no geographic workaround is attempted",
            )
        if answer.status == 429:
            if attempts >= MAX_429_ATTEMPTS:
                _fail(
                    HistoricalBarFailureCode.PROVIDER_RATE_LIMITED,
                    f"the provider rate-limited this adapter on all {MAX_429_ATTEMPTS} bounded "
                    "attempts for one page",
                )
            delay = _retry_after(answer)
            # The remaining deadline is re-read before the bounded backoff: a delay
            # the deadline cannot hold is refused rather than slept through.
            if deadline_seconds - (monotonic() - started) - delay <= 0.0:
                _fail(
                    HistoricalBarFailureCode.DEADLINE_EXCEEDED,
                    "the bounded rate-limit backoff would not fit inside the declared deadline",
                )
            sleeper(delay)
            continue
        _fail(
            HistoricalBarFailureCode.PROVIDER_STATUS,
            f"the provider answered HTTP {answer.status}, which is not a documented klines answer",
        )


def _bounded_headers(headers: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Retain the returned headers verbatim, bounded; never synthesize one."""
    if type(headers) is not tuple:
        _fail(
            HistoricalBarFailureCode.TRANSPORT_CONTRACT,
            "the supplied transport must report its headers as one tuple of name/value pairs",
        )
    pairs: list[tuple[str, str]] = []
    for item in headers:
        if type(item) is not tuple or len(item) != 2:
            _fail(
                HistoricalBarFailureCode.TRANSPORT_CONTRACT,
                "every reported header must be one name/value pair",
            )
        name, value = item
        if type(name) is not str or type(value) is not str:
            _fail(
                HistoricalBarFailureCode.TRANSPORT_CONTRACT,
                "every reported header name and value must be text",
            )
        pairs.append((name, value))
    if len(_canonical_json(pairs).encode("utf-8")) > MAX_HEADER_BYTES:
        _fail(
            HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
            f"the response headers are larger than the {MAX_HEADER_BYTES}-byte bound this "
            "adapter retains",
        )
    return tuple(pairs)


def fetch_historical_bars(
    query: HistoricalBarQueryV1,
    *,
    observed_now: datetime,
    ingested_at: datetime,
    transport: HistoricalBarTransportV1 | None = None,
    monotonic: Monotonic = time.monotonic,
    sleeper: Sleeper = time.sleep,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> HistoricalBarCaptureV1:
    """Read one closed half-open UTC range through a caller-supplied transport.

    ``observed_now`` is the explicit UTC instant this adapter treats as "now": a
    bar whose exclusive end is later than it is not closed and is refused.
    ``ingested_at`` is recorded separately and is never used as an availability
    claim. ``transport`` has no default, and there is no built-in one: without it
    this function fails closed with ``network_disabled``.
    """
    if transport is None:
        _fail(
            HistoricalBarFailureCode.NETWORK_DISABLED,
            "no transport was injected. This build implements no network transport and never "
            "opens a connection on its own; supply your own transport, and establish your own "
            "separate right to use the provider's data before doing so.",
        )
    query = _validated_query(query)
    now = _utc_instant(observed_now, field_name="observed_now")
    ingested = _utc_instant(ingested_at, field_name="ingested_at")
    observed_now_ms = _instant_ms(now)
    if query.end_ms > observed_now_ms:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            "the queried range is not closed as of the explicitly observed UTC now",
            field_name="end_ms",
        )
    deadline = _bounded_seconds(
        deadline_seconds, field_name="deadline_seconds", maximum=MAX_DEADLINE_SECONDS
    )
    timeout = _bounded_seconds(
        request_timeout_seconds,
        field_name="request_timeout_seconds",
        maximum=MAX_REQUEST_TIMEOUT_SECONDS,
    )
    started = monotonic()
    pages: list[_Page] = []
    accumulator = _Accumulator(query, observed_now_ms)
    total_bytes = 0
    while True:
        if len(pages) >= MAX_PAGES:
            _fail(
                HistoricalBarFailureCode.PAGE_BUDGET_EXCEEDED,
                f"the range needs more than the {MAX_PAGES}-page bound",
            )
        elapsed = monotonic() - started
        if elapsed > deadline:
            _fail(
                HistoricalBarFailureCode.DEADLINE_EXCEEDED,
                f"the read exceeded the {deadline}-second deadline",
            )
        page = _request_page(
            transport,
            query,
            accumulator.cursor_ms,
            timeout_seconds=min(timeout, deadline - elapsed),
            monotonic=monotonic,
            sleeper=sleeper,
            started=started,
            deadline_seconds=deadline,
        )
        total_bytes += len(page.body)
        if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
            _fail(
                HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
                f"the response bodies exceed the {MAX_TOTAL_RESPONSE_BYTES}-byte bound",
            )
        # The cursor advances only once the whole page has been validated.
        records = _page_records(page.body, len(pages) + 1)
        accumulator.accept(records, len(pages) + 1)
        page.index = len(pages) + 1
        pages.append(page)
        if len(records) < PAGE_LIMIT:
            break
    normalized = _normalize(query, [page.body for page in pages], observed_now_ms)
    return _capture(query, pages, normalized, now, ingested)


def _fetch_policy() -> dict[str, Any]:
    """The policy one caller-supplied-transport read records."""
    return {
        "network_transport": "not-implemented-in-this-build",
        "transport": "caller-supplied",
        "real_network": False,
        "provider_terms": "blocked",
        "page_limit": PAGE_LIMIT,
        "max_pages": MAX_PAGES,
        "max_records": MAX_RECORDS,
        "max_page_bytes": MAX_PAGE_BYTES,
        "max_total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
        "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
        "max_429_attempts": MAX_429_ATTEMPTS,
        "availability": "simulated: interval_end + latency_seconds",
        "pit_verified": False,
        "vintage": None,
    }


def _fetch_transport_provenance() -> dict[str, Any]:
    """What is true of bytes a caller-supplied transport returned."""
    return {
        "kind": "synthetic-or-caller-supplied",
        "real_network": False,
        "provider_terms": "blocked",
        "note": (
            "Every page byte below was supplied by a caller-provided transport object. This "
            "host made no live request, holds no permission and records no licence, and accepts "
            "no provider agreement. Real use of the documented endpoint is blocked for this "
            "host pending a separate explicit data-use grant."
        ),
    }


def _offline_policy() -> dict[str, Any]:
    """The policy one offline paste read records: one page, no transport."""
    return {
        "network_transport": "not-implemented-in-this-build",
        "transport": "pasted-offline-response",
        "real_network": False,
        "provider_terms": "unresolved",
        "page_limit": PAGE_LIMIT,
        "max_pages": 1,
        "max_records": PAGE_LIMIT,
        "max_page_bytes": MAX_PAGE_BYTES,
        "max_total_response_bytes": MAX_PAGE_BYTES,
        "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
        "max_429_attempts": 0,
        "availability": "simulated: interval_end + latency_seconds",
        "pit_verified": False,
        "vintage": None,
    }


def _offline_transport_provenance(kind: str) -> dict[str, Any]:
    """What is true of one offline read, stated for exactly its own provenance."""
    if kind == OFFLINE_RESPONSE_KIND_SYNTHETIC:
        note = (
            "Every byte below is the host's own built-in synthetic example, read offline. It is "
            "a fixture and not user data; no network request was made and no provider, "
            "permission, licence or point-in-time verification is involved."
        )
    else:
        note = (
            "Every byte below was pasted by the user and read offline in this process. This host "
            "made no network request, holds no provider credential and resolves none. The "
            "source, rights/licence and intended use are the user's own assertion and are "
            "recorded without legal verification."
        )
    return {
        "kind": kind,
        "real_network": False,
        "provider_terms": "unresolved",
        "media_type": OFFLINE_RESPONSE_MEDIA_TYPE,
        "record_field_count": DOCUMENTED_KLINES_FIELD_COUNT,
        "adapter": _normalizer_document(),
        "note": note,
    }


def adapter_identity_document() -> dict[str, Any]:
    """The exact provider and adapter identity a capture records.

    A caller that seals its own evidence over a capture's bytes names this
    document rather than restating a version string of its own, so one adapter
    version is stated in exactly one place.
    """
    return {"provider": _provider_document(), "normalizer": _normalizer_document()}


def _capture(
    query: HistoricalBarQueryV1,
    pages: list[_Page],
    normalized: _Normalized,
    observed_now: datetime,
    ingested_at: datetime,
    *,
    policy: dict[str, Any] | None = None,
    transport: dict[str, Any] | None = None,
    limitations: tuple[str, ...] | None = None,
) -> HistoricalBarCaptureV1:
    """One capture: the strict bytes plus the exact envelope that produced them."""
    instrument = Instrument(venue=VenueId(query.venue), symbol=query.symbol)
    provider = _provider_document()
    normalizer = _normalizer_document()
    declared_policy = _fetch_policy() if policy is None else policy
    transport_provenance = _fetch_transport_provenance() if transport is None else transport
    declared_limitations = _LIMITATIONS if limitations is None else limitations
    observed_stamp = observed_now.strftime(_STAMP_FORMAT)
    ingested_stamp = ingested_at.strftime(_STAMP_FORMAT)
    page_documents = [
        {
            "index": page.index,
            "request_url": page.url,
            "request_params": [[name, value] for name, value in page.params],
            "request_timeout_seconds": page.timeout_seconds,
            "status": page.status,
            "headers": [[name, value] for name, value in page.headers],
            "byte_count": len(page.body),
            "sha256": _digest(page.body),
            "body_base64": base64.b64encode(page.body).decode("ascii"),
        }
        for page in pages
    ]
    records = {
        "count": normalized.record_count,
        "first_open_ms": normalized.first_open_ms,
        "last_close_ms": normalized.last_close_ms,
    }
    capture_digests: dict[str, Any] = {
        "profile": PHASE1_OHLCV_PROFILE,
        "sha256": _digest(normalized.strict_csv),
        "byte_count": len(normalized.strict_csv),
        "data_sha256": normalized.data_sha256,
        "record_count": normalized.record_count,
    }
    evidence = {
        "schema": PROVIDER_EVIDENCE_SCHEMA,
        "version": 1,
        "provider": provider,
        "normalizer": normalizer,
        "declaration": {
            "query": query.document(),
            "policy": declared_policy,
            "transport": transport_provenance,
            "observed_now_utc": observed_stamp,
            "ingested_at_utc": ingested_stamp,
        },
        "result": {
            "pages": [
                {
                    "index": page["index"],
                    "status": page["status"],
                    "byte_count": page["byte_count"],
                    "sha256": page["sha256"],
                }
                for page in page_documents
            ],
            "records": records,
            "capture": capture_digests,
            "instrument": {"venue": instrument.venue.code, "symbol": instrument.symbol},
            "limitations": list(declared_limitations),
        },
    }
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "provider": provider,
        "normalizer": normalizer,
        "query": query.document(),
        "policy": declared_policy,
        "transport": transport_provenance,
        "observed_now_utc": observed_stamp,
        "ingested_at_utc": ingested_stamp,
        "pages": page_documents,
        "records": records,
        "capture": capture_digests,
        "limitations": list(declared_limitations),
    }
    envelope_json = _canonical_json(envelope)
    if len(envelope_json.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        _fail(
            HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
            f"the response envelope exceeds the {MAX_ENVELOPE_BYTES}-byte bound",
        )
    return HistoricalBarCaptureV1(
        profile=PHASE1_OHLCV_PROFILE,
        strict_csv=normalized.strict_csv,
        envelope_json=envelope_json,
        evidence_json=_canonical_json(evidence),
        capture_sha256=capture_digests["sha256"],
        capture_byte_count=capture_digests["byte_count"],
        data_sha256=normalized.data_sha256,
        record_count=normalized.record_count,
        venue=instrument.venue.code,
        symbol=instrument.symbol,
    )


# --- offline paste -----------------------------------------------------------


def _synthetic_example_records() -> list[list[Any]]:
    """A fixed UTC day of synthetic hourly bars in the documented shape."""
    step = INTERVALS[SYNTHETIC_EXAMPLE_INTERVAL]
    records: list[list[Any]] = []
    for index in range(SYNTHETIC_EXAMPLE_RECORD_COUNT):
        open_ms = SYNTHETIC_EXAMPLE_START_MS + index * step
        base = 100 + index
        records.append(
            [
                open_ms,
                f"{base}.0",
                f"{base + 1}.0",
                f"{base - 1}.0",
                f"{base}.5",
                "10.0",
                open_ms + step - 1,
                "1000.0",
                index + 1,
                "5.0",
                "500.0",
                "0",
            ]
        )
    return records


SYNTHETIC_EXAMPLE_END_MS = (
    SYNTHETIC_EXAMPLE_START_MS
    + SYNTHETIC_EXAMPLE_RECORD_COUNT * INTERVALS[SYNTHETIC_EXAMPLE_INTERVAL]
)
SYNTHETIC_EXAMPLE_BODY: bytes = _canonical_json(_synthetic_example_records()).encode("utf-8")
SYNTHETIC_EXAMPLE_BODY_SHA256 = _digest(SYNTHETIC_EXAMPLE_BODY)


def synthetic_example_query() -> HistoricalBarQueryV1:
    """The one declaration the built-in example's bytes correspond to."""
    return HistoricalBarQueryV1(
        venue=SYNTHETIC_EXAMPLE_VENUE,
        symbol=SYNTHETIC_EXAMPLE_SYMBOL,
        interval=SYNTHETIC_EXAMPLE_INTERVAL,
        start_ms=SYNTHETIC_EXAMPLE_START_MS,
        end_ms=SYNTHETIC_EXAMPLE_END_MS,
        latency_seconds=SYNTHETIC_EXAMPLE_LATENCY_SECONDS,
    )


def is_synthetic_example(body: object) -> bool:
    """Whether these bytes are exactly the host's own built-in example."""
    return type(body) is bytes and body == SYNTHETIC_EXAMPLE_BODY


def synthetic_example_document() -> dict[str, Any]:
    """The built-in example whole: its bytes, its declaration and its marking."""
    return {
        "schema": SYNTHETIC_EXAMPLE_SCHEMA,
        "example_id": SYNTHETIC_EXAMPLE_ID,
        "synthetic": True,
        "label": "Built-in synthetic example — not market data",
        "note": (
            "These bytes are a fixture this host ships so that offline preview, publication and "
            "catalog selection can be exercised without a provider or a network. They are not "
            "user-supplied data and not an observation of any market, and loading them is what "
            "marks an import synthetic."
        ),
        "adapter": adapter_identity_document(),
        "media_type": OFFLINE_RESPONSE_MEDIA_TYPE,
        "record_field_count": DOCUMENTED_KLINES_FIELD_COUNT,
        "documented_fields": list(DOCUMENTED_KLINES_FIELDS),
        "venue": SYNTHETIC_EXAMPLE_VENUE,
        "symbol": SYNTHETIC_EXAMPLE_SYMBOL,
        "interval": SYNTHETIC_EXAMPLE_INTERVAL,
        "start_ms": SYNTHETIC_EXAMPLE_START_MS,
        "end_ms": SYNTHETIC_EXAMPLE_END_MS,
        "latency_seconds": SYNTHETIC_EXAMPLE_LATENCY_SECONDS,
        "record_count": SYNTHETIC_EXAMPLE_RECORD_COUNT,
        "response_body": SYNTHETIC_EXAMPLE_BODY.decode("utf-8"),
        "response_sha256": SYNTHETIC_EXAMPLE_BODY_SHA256,
        "limitations": list(SYNTHETIC_OFFLINE_LIMITATIONS),
    }


def capture_offline_response(
    body: object,
    *,
    query: HistoricalBarQueryV1,
    observed_now: datetime,
    ingested_at: datetime,
    synthetic: bool = False,
) -> HistoricalBarCaptureV1:
    """Read one pasted documented response offline: one page and no transport at all.

    The pasted bytes become the single retained page, so the envelope keeps them
    exactly and a read re-derives the capture from them. ``synthetic`` selects
    which provenance the envelope records, and it is accepted for exactly the
    host's own built-in example bytes and declaration — never for whatever bytes
    a caller chose to label synthetic.
    """
    if type(body) is not bytes or not body:
        _fail(
            HistoricalBarFailureCode.OFFLINE_RESPONSE_INVALID,
            "the pasted response must be the response body's own non-empty bytes",
        )
    if len(body) > MAX_PAGE_BYTES:
        _fail(
            HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
            f"the pasted response is larger than the {MAX_PAGE_BYTES}-byte single-response bound",
        )
    if type(synthetic) is not bool:
        _fail(
            HistoricalBarFailureCode.OFFLINE_RESPONSE_INVALID,
            "the synthetic marking must be one boolean",
        )
    validated = _validated_query(query)
    if synthetic and (body != SYNTHETIC_EXAMPLE_BODY or validated != synthetic_example_query()):
        _fail(
            HistoricalBarFailureCode.OFFLINE_RESPONSE_INVALID,
            "the synthetic marking is accepted only for the host's own built-in example bytes "
            "and declaration; pasted input is never recorded as synthetic on request",
        )
    now = _utc_instant(observed_now, field_name="observed_now")
    ingested = _utc_instant(ingested_at, field_name="ingested_at")
    observed_now_ms = _instant_ms(now)
    if validated.end_ms > observed_now_ms:
        _fail(
            HistoricalBarFailureCode.INVALID_QUERY,
            "the declared range is not closed as of the host's observed UTC now",
            field_name="end_ms",
        )
    params = (
        ("symbol", validated.symbol),
        ("interval", validated.interval),
        ("startTime", str(validated.start_ms)),
        ("endTime", str(validated.end_ms - 1)),
        ("limit", str(PAGE_LIMIT)),
    )
    records = _page_records(body, 1)
    accumulator = _Accumulator(validated, observed_now_ms)
    accumulator.accept(records, 1)
    if len(accumulator.bars) == PAGE_LIMIT and accumulator.cursor_ms < validated.end_ms:
        _fail(
            HistoricalBarFailureCode.OFFLINE_RESPONSE_INCOMPLETE,
            "the pasted response is one full page whose last bar does not reach the declared "
            "range end; a single pasted response must be the whole answer",
        )
    normalized = _normalize(
        validated,
        [body],
        observed_now_ms,
        # One entry per page, each the closed five-parameter request that page
        # answers: the same shape a fetched multi-page capture seals.
        [[[name, value] for name, value in params]],
    )
    page = _Page(
        index=1,
        url=PROVIDER_ENDPOINT,
        params=params,
        timeout_seconds=0.0,
        status=200,
        headers=(),
        body=body,
    )
    kind = OFFLINE_RESPONSE_KIND_SYNTHETIC if synthetic else OFFLINE_RESPONSE_KIND_USER
    return _capture(
        validated,
        [page],
        normalized,
        now,
        ingested,
        policy=_offline_policy(),
        transport=_offline_transport_provenance(kind),
        limitations=SYNTHETIC_OFFLINE_LIMITATIONS if synthetic else USER_OFFLINE_LIMITATIONS,
    )


# --- re-derivation -----------------------------------------------------------


def _checked_evidence(provider_evidence: object) -> dict[str, Any]:
    """Re-read one sealed provider evidence document under its closed schema."""
    if type(provider_evidence) is not dict:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, "provider evidence is not one document")
    evidence = cast("dict[str, Any]", provider_evidence)
    if (
        set(evidence) != {"schema", "version", "provider", "normalizer", "declaration", "result"}
        or evidence["schema"] != PROVIDER_EVIDENCE_SCHEMA
        or evidence["version"] != 1
        or type(evidence["declaration"]) is not dict
        or type(evidence["result"]) is not dict
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "provider evidence is not the closed provider-evidence schema",
        )
    if (
        evidence["provider"] != _provider_document()
        or evidence["normalizer"] != _normalizer_document()
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "this normalizer refuses evidence another provider or normalizer version produced",
        )
    return evidence


def _provider_document() -> dict[str, Any]:
    return {
        "id": PROVIDER_ID,
        "version": PROVIDER_VERSION,
        "endpoint": PROVIDER_ENDPOINT,
        "documented_fields": list(DOCUMENTED_KLINES_FIELDS),
        "documented_field_count": DOCUMENTED_KLINES_FIELD_COUNT,
    }


def _normalizer_document() -> dict[str, Any]:
    return {"id": NORMALIZER_ID, "version": NORMALIZER_VERSION}


def _query_from_document(document: object) -> HistoricalBarQueryV1:
    if type(document) is not dict or set(cast("dict[str, Any]", document)) != {
        "venue",
        "symbol",
        "interval",
        "start_ms",
        "end_ms",
        "end_time_param_ms",
        "latency_seconds",
    }:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID, "the sealed query is not the closed schema"
        )
    values = cast("dict[str, Any]", document)
    if values["end_time_param_ms"] != cast("int", values["end_ms"]) - 1:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the sealed inclusive endTime is not the half-open end minus one millisecond",
        )
    return HistoricalBarQueryV1(
        venue=cast("str", values["venue"]),
        symbol=cast("str", values["symbol"]),
        interval=cast("str", values["interval"]),
        start_ms=cast("int", values["start_ms"]),
        end_ms=cast("int", values["end_ms"]),
        latency_seconds=cast("int", values["latency_seconds"]),
    )


def _sealed_instant(document: object, *, field_name: str) -> datetime:
    if type(document) is not str:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, f"{field_name} is not one UTC stamp")
    try:
        value = datetime.strptime(document, _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, f"{field_name} is not one UTC stamp")
    if value.strftime(_STAMP_FORMAT) != document:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, f"{field_name} is not one UTC stamp")
    return value


def _page_body(document: object, summary: object, position: int) -> bytes:
    """One retained page body, checked against the digest the evidence sealed."""
    if type(document) is not dict or type(summary) is not dict:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, "a retained page is not one document")
    page = cast("dict[str, Any]", document)
    recorded = cast("dict[str, Any]", summary)
    if set(page) != {
        "index",
        "request_url",
        "request_params",
        "request_timeout_seconds",
        "status",
        "headers",
        "byte_count",
        "sha256",
        "body_base64",
    }:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, "a retained page is not the closed schema")
    if set(recorded) != {"index", "status", "byte_count", "sha256"}:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "a sealed page summary is not the closed schema",
        )
    if page["request_url"] != PROVIDER_ENDPOINT:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained page names a host other than the one fixed endpoint",
        )
    if page["index"] != position or page["status"] != 200:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "a retained page is not a documented 200 answer in the recorded page order",
            page_index=position,
        )
    if (
        page["index"] != recorded["index"]
        or page["status"] != recorded["status"]
        or page["byte_count"] != recorded["byte_count"]
        or page["sha256"] != recorded["sha256"]
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained page no longer matches the sealed page evidence",
        )
    encoded = page["body_base64"]
    if type(encoded) is not str:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained page body is not base64 text",
        )
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained page body is not base64 text",
        )
    if len(body) != page["byte_count"] or _digest(body) != page["sha256"]:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained page body does not reproduce its sealed digest",
        )
    return body


def rederive_strict_capture(raw_source: object, provider_evidence: object) -> bytes:
    """Re-derive one normalized capture from the retained envelope and its evidence.

    This is what a read of a published version runs: the exact page bytes are read
    back out of the retained raw source, checked against the page digests the
    evidence sealed, and normalized again under the sealed query, provider,
    normalizer and policy. A declaration, envelope or capture that no longer
    reproduces is refused rather than read as if it were intact.
    """
    if type(raw_source) is not bytes:
        _fail(HistoricalBarFailureCode.ENVELOPE_INVALID, "the retained raw source must be bytes")
    if len(raw_source) > MAX_ENVELOPE_BYTES:
        # The retained source is bounded here too: a read must not be the place
        # where an unbounded document is decoded.
        _fail(
            HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
            f"the retained raw source is larger than the {MAX_ENVELOPE_BYTES}-byte bound",
        )
    try:
        envelope = json.loads(raw_source.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained raw source is not the recorded response envelope",
        )
    if type(envelope) is not dict:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained raw source is not the recorded response envelope",
        )
    document = cast("dict[str, Any]", envelope)
    if (
        set(document)
        != {
            "schema",
            "provider",
            "normalizer",
            "query",
            "policy",
            "transport",
            "observed_now_utc",
            "ingested_at_utc",
            "pages",
            "records",
            "capture",
            "limitations",
        }
        or document["schema"] != ENVELOPE_SCHEMA
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained raw source is not the closed response envelope schema",
        )
    evidence = _checked_evidence(provider_evidence)
    declaration = cast("dict[str, Any]", evidence["declaration"])
    if set(declaration) != {
        "query",
        "policy",
        "transport",
        "observed_now_utc",
        "ingested_at_utc",
    }:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the sealed declaration is not the closed schema",
        )
    if (
        document["provider"] != evidence["provider"]
        or document["normalizer"] != evidence["normalizer"]
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained envelope names another provider or normalizer than the sealed evidence",
        )
    if (
        document["query"] != declaration["query"]
        or document["policy"] != declaration["policy"]
        or document["transport"] != declaration["transport"]
        or document["observed_now_utc"] != declaration["observed_now_utc"]
        or document["ingested_at_utc"] != declaration["ingested_at_utc"]
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained envelope no longer matches the sealed declaration",
        )
    query = _query_from_document(declaration["query"])
    observed_now_ms = _instant_ms(
        _sealed_instant(declaration["observed_now_utc"], field_name="observed_now_utc")
    )
    _sealed_instant(declaration["ingested_at_utc"], field_name="ingested_at_utc")
    pages = document["pages"]
    result = cast("dict[str, Any]", evidence["result"])
    summaries = result["pages"]
    if type(pages) is not list or type(summaries) is not list or len(pages) != len(summaries):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained envelope and the sealed evidence disagree on the page count",
        )
    if not pages:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained envelope records no page at all",
        )
    if len(pages) > MAX_PAGES:
        _fail(
            HistoricalBarFailureCode.PAGE_BUDGET_EXCEEDED,
            f"the retained envelope records more than the {MAX_PAGES}-page bound",
        )
    bodies: list[bytes] = []
    params: list[Any] = []
    total_bytes = 0
    for position, (page, summary) in enumerate(zip(pages, summaries, strict=True), start=1):
        body = _page_body(page, summary, position)
        total_bytes += len(body)
        if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
            _fail(
                HistoricalBarFailureCode.RESPONSE_TOO_LARGE,
                f"the retained page bodies exceed the {MAX_TOTAL_RESPONSE_BYTES}-byte bound",
            )
        bodies.append(body)
        params.append(page["request_params"])
    normalized = _normalize(query, bodies, observed_now_ms, params)
    recorded = cast("dict[str, Any]", result["capture"])
    if (
        _digest(normalized.strict_csv) != recorded["sha256"]
        or len(normalized.strict_csv) != recorded["byte_count"]
        or normalized.data_sha256 != recorded["data_sha256"]
        or normalized.record_count != recorded["record_count"]
    ):
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained pages no longer normalize to the capture the evidence sealed",
        )
    if document["capture"] != recorded or document["records"] != result["records"]:
        _fail(
            HistoricalBarFailureCode.ENVELOPE_INVALID,
            "the retained envelope no longer matches the sealed capture evidence",
        )
    return normalized.strict_csv
