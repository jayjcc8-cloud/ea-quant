"""Canonical OHLCV values, point-in-time visibility, and deterministic ordering."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from math import isfinite

from ea.core.identity import Instrument
from ea.core.time import Clock, require_utc

_SOURCE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z", flags=re.ASCII)


class MarketDataValidationError(ValueError):
    """Raised when canonical market data is invalid, duplicated, or conflicting."""


class MarketDataKind(StrEnum):
    """Stable market-event vocabulary; ranks are fixed by ADR 0004."""

    BAR = "bar"


class Adjustment(StrEnum):
    """Supported market-data adjustment semantics."""

    RAW = "raw"


@dataclass(frozen=True, slots=True)
class SourceId:
    """Stable canonical data-source or dataset namespace."""

    code: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _SOURCE_PATTERN.fullmatch(self.code) is None:
            raise MarketDataValidationError("source code must match [a-z0-9][a-z0-9._-]{0,63}")


MarketDataLogicalKey = tuple[str, str, str, datetime, datetime, str, str]
MarketDataRecordKey = tuple[MarketDataLogicalKey, int]
MarketDataEmissionKey = tuple[str, int]
AdmissionOrderKey = tuple[
    datetime,
    datetime,
    int,
    str,
    int,
    str,
    str,
    datetime,
    datetime,
    str,
    int,
]


def _require_finite_float(value: float, *, field: str) -> None:
    if type(value) is not float:
        raise MarketDataValidationError(f"{field} must have exact runtime type float")
    if not isfinite(value):
        raise MarketDataValidationError(f"{field} must be finite")


def _require_nonnegative_int(value: int, *, field: str) -> None:
    if type(value) is not int or value < 0:
        raise MarketDataValidationError(f"{field} must be a non-negative int, not bool")


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed UTC half-open OHLCV interval ``[start, end)``."""

    instrument: Instrument
    interval_start: datetime
    interval_end: datetime
    adjustment: Adjustment
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise MarketDataValidationError("instrument must be an Instrument")
        if type(self.adjustment) is not Adjustment:
            raise MarketDataValidationError("adjustment must be an Adjustment")

        interval_start = require_utc(self.interval_start, field="interval_start")
        interval_end = require_utc(self.interval_end, field="interval_end")
        object.__setattr__(self, "interval_start", interval_start)
        object.__setattr__(self, "interval_end", interval_end)
        if interval_start >= interval_end:
            raise MarketDataValidationError("interval_start must be earlier than interval_end")

        for field in ("open", "high", "low", "close", "volume"):
            _require_finite_float(getattr(self, field), field=field)
        if not self.low <= self.open <= self.high:
            raise MarketDataValidationError("open must be between low and high")
        if not self.low <= self.close <= self.high:
            raise MarketDataValidationError("close must be between low and high")
        if self.volume < 0.0:
            raise MarketDataValidationError("volume must be non-negative")

    @property
    def kind(self) -> MarketDataKind:
        return MarketDataKind.BAR

    @property
    def event_time(self) -> datetime:
        """The event time of a closed Bar is its exclusive interval end."""
        return self.interval_end


@dataclass(frozen=True, slots=True)
class MarketDataEnvelope:
    """Immutable source, revision, and knowledge-time envelope for one Bar."""

    payload: Bar
    source: SourceId
    available_at: datetime
    source_sequence: int
    revision: int

    def __post_init__(self) -> None:
        if type(self.payload) is not Bar:
            raise MarketDataValidationError("payload must be a Bar")
        if type(self.source) is not SourceId:
            raise MarketDataValidationError("source must be a SourceId")
        available_at = require_utc(self.available_at, field="available_at")
        object.__setattr__(self, "available_at", available_at)
        _require_nonnegative_int(self.source_sequence, field="source_sequence")
        _require_nonnegative_int(self.revision, field="revision")
        if available_at < self.payload.event_time:
            raise MarketDataValidationError("available_at cannot be earlier than event_time")

    @property
    def kind(self) -> MarketDataKind:
        return self.payload.kind

    @property
    def event_time(self) -> datetime:
        return self.payload.event_time

    @property
    def logical_key(self) -> MarketDataLogicalKey:
        payload = self.payload
        return (
            payload.kind.value,
            payload.instrument.venue.code,
            payload.instrument.symbol,
            payload.interval_start,
            payload.interval_end,
            payload.adjustment.value,
            self.source.code,
        )

    @property
    def record_key(self) -> MarketDataRecordKey:
        return (self.logical_key, self.revision)

    @property
    def emission_key(self) -> MarketDataEmissionKey:
        return (self.source.code, self.source_sequence)


@dataclass(frozen=True, slots=True)
class AdmissionCursor:
    """Immutable runtime cursor containing only an admitted event and its clock cutoff."""

    as_of: datetime
    last_event: MarketDataEnvelope | None

    def __post_init__(self) -> None:
        as_of = require_utc(self.as_of, field="cursor.as_of")
        object.__setattr__(self, "as_of", as_of)
        if self.last_event is None:
            return
        if type(self.last_event) is not MarketDataEnvelope:
            raise MarketDataValidationError(
                "cursor.last_event must be a MarketDataEnvelope or None"
            )
        if self.last_event.event_time > as_of or self.last_event.available_at > as_of:
            raise MarketDataValidationError("cursor.last_event must be visible at cursor.as_of")


def admission_order_key(event: MarketDataEnvelope) -> AdmissionOrderKey:
    """Return the ADR 0004 deterministic market admission key."""
    if type(event) is not MarketDataEnvelope:
        raise MarketDataValidationError("event must be a MarketDataEnvelope")
    payload = event.payload
    return (
        event.available_at,
        event.event_time,
        0,
        event.source.code,
        event.source_sequence,
        payload.instrument.venue.code,
        payload.instrument.symbol,
        payload.interval_start,
        payload.interval_end,
        payload.adjustment.value,
        event.revision,
    )


def _validated_events(events: Iterable[MarketDataEnvelope]) -> tuple[MarketDataEnvelope, ...]:
    try:
        materialized = tuple(events)
    except TypeError as exc:
        raise MarketDataValidationError("events must be an iterable") from exc

    records: dict[MarketDataRecordKey, MarketDataEnvelope] = {}
    emissions: dict[MarketDataEmissionKey, MarketDataEnvelope] = {}
    order_keys: dict[AdmissionOrderKey, MarketDataEnvelope] = {}
    histories: dict[MarketDataLogicalKey, list[MarketDataEnvelope]] = {}

    for index, event in enumerate(materialized):
        if type(event) is not MarketDataEnvelope:
            raise MarketDataValidationError(f"events[{index}] must be a MarketDataEnvelope")

        prior_record = records.get(event.record_key)
        if prior_record is not None:
            label = "duplicate" if prior_record == event else "conflicting"
            raise MarketDataValidationError(f"{label} record version: {event.record_key!r}")
        records[event.record_key] = event

        prior_emission = emissions.get(event.emission_key)
        if prior_emission is not None:
            raise MarketDataValidationError(f"duplicate source emission: {event.emission_key!r}")
        emissions[event.emission_key] = event

        order_key = admission_order_key(event)
        prior_order_key = order_keys.get(order_key)
        if prior_order_key is not None:
            raise MarketDataValidationError(f"duplicate admission ordering key: {order_key!r}")
        order_keys[order_key] = event
        histories.setdefault(event.logical_key, []).append(event)

    for logical_key, history in histories.items():
        by_revision = sorted(history, key=lambda event: event.revision)
        for earlier, later in pairwise(by_revision):
            if later.available_at < earlier.available_at:
                raise MarketDataValidationError(
                    f"revision availability regressed for logical record: {logical_key!r}"
                )
            if later.source_sequence <= earlier.source_sequence:
                raise MarketDataValidationError(
                    f"revision source_sequence did not increase for logical record: {logical_key!r}"
                )

    return materialized


def validate_market_data_batch(events: Iterable[MarketDataEnvelope]) -> None:
    """Fail fast on duplicate, conflicting, or non-monotonic market data."""
    _validated_events(events)


def order_market_data(events: Iterable[MarketDataEnvelope]) -> tuple[MarketDataEnvelope, ...]:
    """Validate and return events in deterministic admission order."""
    return tuple(sorted(_validated_events(events), key=admission_order_key))


def is_visible_as_of(event: MarketDataEnvelope, as_of: datetime) -> bool:
    """Return whether an event was complete and knowable at an inclusive UTC cutoff."""
    if type(event) is not MarketDataEnvelope:
        raise MarketDataValidationError("event must be a MarketDataEnvelope")
    cutoff = require_utc(as_of, field="as_of")
    return event.event_time <= cutoff and event.available_at <= cutoff


def latest_as_of(
    events: Iterable[MarketDataEnvelope], as_of: datetime
) -> tuple[MarketDataEnvelope, ...]:
    """Return the highest visible revision for every source-scoped logical Bar."""
    cutoff = require_utc(as_of, field="as_of")
    selected: dict[MarketDataLogicalKey, MarketDataEnvelope] = {}
    for event in order_market_data(events):
        if not is_visible_as_of(event, cutoff):
            continue
        prior = selected.get(event.logical_key)
        if prior is None or event.revision > prior.revision:
            selected[event.logical_key] = event
    return tuple(sorted(selected.values(), key=admission_order_key))


def admit_market_data(
    history: Iterable[MarketDataEnvelope],
    *,
    clock: Clock,
    cursor: AdmissionCursor | None = None,
    limit: int | None = None,
) -> tuple[AdmissionCursor, tuple[MarketDataEnvelope, ...]]:
    """Admit a bounded batch from complete cumulative history after a cursor."""
    as_of = require_utc(clock.now(), field="clock.now()")
    if cursor is not None and type(cursor) is not AdmissionCursor:
        raise MarketDataValidationError("cursor must be an AdmissionCursor or None")
    if cursor is not None and as_of < cursor.as_of:
        raise MarketDataValidationError("injected clock regressed behind the previous cutoff")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise MarketDataValidationError("limit must be a positive int, not bool")

    ordered_history = order_market_data(history)
    if (
        cursor is not None
        and cursor.last_event is not None
        and cursor.last_event not in ordered_history
    ):
        raise MarketDataValidationError(
            "history must be complete and cumulative, including cursor.last_event"
        )

    previous_key = (
        admission_order_key(cursor.last_event)
        if cursor is not None and cursor.last_event is not None
        else None
    )
    candidates = tuple(
        event
        for event in ordered_history
        if is_visible_as_of(event, as_of)
        and (previous_key is None or admission_order_key(event) > previous_key)
    )
    admitted = candidates if limit is None else candidates[:limit]
    last_event = admitted[-1] if admitted else cursor.last_event if cursor is not None else None
    return AdmissionCursor(as_of=as_of, last_event=last_event), admitted
