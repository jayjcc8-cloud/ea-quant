"""Outer bridge from the strict Phase 1 source to the runtime-owned port."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast, final

from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.runtime import RuntimeOrderingError
from ea.core.time import Clock, TimeValidationError, require_utc
from ea.data.historical import (
    PHASE1_OHLCV_PROFILE,
    HistoricalMarketDataError,
    HistoricalMarketDataFailureCode,
    Phase1HistoricalMarketDataSource,
    Phase1HistoricalSourceCursor,
)
from ea.runtime.historical import (
    HistoricalMarketCandidate,
    HistoricalMarketPreparedCommit,
    HistoricalMarketSourceBinding,
)

_CANDIDATE_SEAL = object()
_PREPARED_SEAL = object()


def _conflict(message: str) -> RuntimeOrderingError:
    return RuntimeOrderingError(OutcomeCode.CONFLICTING_ID, message)


@final
class _Phase1HistoricalMarketCandidate:
    __slots__ = (
        "_canonical_event_bytes",
        "_cursor",
        "_cursor_as_of",
        "_event",
        "_scheduled_at",
        "_seal",
    )

    _canonical_event_bytes: bytes
    _cursor: Phase1HistoricalSourceCursor
    _cursor_as_of: datetime
    _event: MarketDataEnvelope
    _scheduled_at: datetime
    _seal: object

    def __init__(self) -> None:
        raise TypeError("historical market candidates are issued only by the source bridge")

    @property
    def event(self) -> MarketDataEnvelope:
        return self._event

    @property
    def scheduled_at(self) -> datetime:
        return self._scheduled_at

    @property
    def cursor_as_of(self) -> datetime:
        return self._cursor_as_of

    @property
    def canonical_event_bytes(self) -> bytes:
        return self._canonical_event_bytes


@final
class _Phase1HistoricalPreparedCommit:
    __slots__ = ("_candidate", "_cursor", "_seal")

    _candidate: _Phase1HistoricalMarketCandidate
    _cursor: Phase1HistoricalSourceCursor
    _seal: object

    def __init__(self) -> None:
        raise TypeError("historical source commits are issued only by prepare_commit")

    def _historical_market_prepared_commit_marker(self) -> None:
        return None


@final
class Phase1HistoricalMarketSourceBridge:
    """Factory-only bridge retaining concrete cursor state outside ``ea.runtime``."""

    __slots__ = ("_binding", "_candidate", "_committed_cursor", "_prepared", "_source")

    _binding: HistoricalMarketSourceBinding
    _candidate: _Phase1HistoricalMarketCandidate | None
    _committed_cursor: Phase1HistoricalSourceCursor | None
    _prepared: _Phase1HistoricalPreparedCommit | None
    _source: Phase1HistoricalMarketDataSource

    def __init__(self) -> None:
        raise TypeError(
            "historical source bridges are created only by "
            "create_phase1_historical_market_source_bridge"
        )

    @property
    def binding(self) -> HistoricalMarketSourceBinding:
        return self._binding

    def next_available_at(self) -> datetime | None:
        candidate = self._candidate
        if candidate is not None:
            return candidate.scheduled_at
        return self._source.next_available_at(self._committed_cursor)

    def admit_one(self, *, clock: Clock) -> HistoricalMarketCandidate:
        candidate = self._candidate
        if candidate is not None:
            observed = _read_clock(clock)
            if observed != candidate.scheduled_at:
                raise _conflict("retained candidate clock does not match its schedule")
            return candidate
        cursor, events = self._source.admit(
            clock=clock,
            cursor=self._committed_cursor,
            limit=1,
        )
        if type(cursor) is not Phase1HistoricalSourceCursor or len(events) != 1:
            raise _conflict("one-event source admission did not return one exact candidate")
        event = events[0]
        if type(event) is not MarketDataEnvelope:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "one-event source admission returned a non-canonical event",
            )
        if cursor.last_event is None:
            raise _conflict("source candidate cursor lost its admitted event")
        event_bytes = canonical_market_data_record_bytes(event)
        cursor_bytes = canonical_market_data_record_bytes(cursor.last_event)
        if (
            cursor.as_of != event.available_at
            or cursor.last_event is not event
            or cursor_bytes != event_bytes
        ):
            raise _conflict("source candidate cursor/event/canonical bytes conflict")
        issued = object.__new__(_Phase1HistoricalMarketCandidate)
        issued._canonical_event_bytes = event_bytes
        issued._cursor = cursor
        issued._cursor_as_of = cursor.as_of
        issued._event = event
        issued._scheduled_at = event.available_at
        issued._seal = _CANDIDATE_SEAL
        self._candidate = issued
        return issued

    def prepare_commit(
        self,
        candidate: HistoricalMarketCandidate,
    ) -> HistoricalMarketPreparedCommit:
        if type(candidate) is not _Phase1HistoricalMarketCandidate:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "source prepare_commit requires an exact bridge candidate",
            )
        current = self._candidate
        if current is None or candidate is not current or candidate._seal is not _CANDIDATE_SEAL:
            raise _conflict("source prepare_commit candidate is stale or foreign")
        _validate_candidate_bytes(current)
        prepared = self._prepared
        if prepared is not None:
            if prepared._candidate is not current:
                raise _conflict("source retained a conflicting prepared commit")
            return prepared
        prepared = object.__new__(_Phase1HistoricalPreparedCommit)
        prepared._candidate = current
        prepared._cursor = current._cursor
        prepared._seal = _PREPARED_SEAL
        self._prepared = prepared
        return prepared

    def commit(self, prepared: HistoricalMarketPreparedCommit) -> None:
        if type(prepared) is not _Phase1HistoricalPreparedCommit:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "source commit requires an exact prepared bridge capability",
            )
        current = self._candidate
        if (
            prepared is not self._prepared
            or prepared._seal is not _PREPARED_SEAL
            or current is None
            or prepared._candidate is not current
            or prepared._cursor is not current._cursor
        ):
            raise _conflict("source prepared commit is stale or foreign")
        _validate_candidate_bytes(current)
        self._committed_cursor = prepared._cursor
        self._candidate = None
        self._prepared = None


def _read_clock(clock: Clock) -> datetime:
    candidate = cast(Any, clock)
    try:
        now = candidate.now
    except (AttributeError, TypeError) as error:
        raise HistoricalMarketDataError(HistoricalMarketDataFailureCode.INVALID_TYPE) from error
    if not callable(now):
        raise HistoricalMarketDataError(HistoricalMarketDataFailureCode.INVALID_TYPE)
    observed = now()
    try:
        return require_utc(observed, field="clock.now()")
    except TimeValidationError as error:
        raise HistoricalMarketDataError(HistoricalMarketDataFailureCode.INVALID_TYPE) from error


def _validate_candidate_bytes(candidate: _Phase1HistoricalMarketCandidate) -> None:
    if candidate._seal is not _CANDIDATE_SEAL:
        raise _conflict("source candidate seal conflicts")
    cursor_event = candidate._cursor.last_event
    if cursor_event is None:
        raise _conflict("source candidate cursor lost its last event")
    if not (
        candidate._cursor.as_of
        == candidate._cursor_as_of
        == candidate._scheduled_at
        == candidate._event.available_at
    ):
        raise _conflict("source candidate time binding conflicts")
    if not (
        canonical_market_data_record_bytes(candidate._event)
        == canonical_market_data_record_bytes(cursor_event)
        == candidate._canonical_event_bytes
    ):
        raise _conflict("source candidate canonical bytes conflict")


def create_phase1_historical_market_source_bridge(
    source: Phase1HistoricalMarketDataSource,
) -> Phase1HistoricalMarketSourceBridge:
    """Wrap one exact strict source behind the runtime-owned structural port."""
    if type(source) is not Phase1HistoricalMarketDataSource:
        raise HistoricalMarketDataError(HistoricalMarketDataFailureCode.INVALID_TYPE)
    binding = HistoricalMarketSourceBinding(
        profile=PHASE1_OHLCV_PROFILE,
        replay_window=source.replay_window,
        data_fingerprint=source.fingerprint,
    )
    bridge = object.__new__(Phase1HistoricalMarketSourceBridge)
    bridge._binding = binding
    bridge._candidate = None
    bridge._committed_cursor = None
    bridge._prepared = None
    bridge._source = source
    return bridge
