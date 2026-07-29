"""Deterministic historical scheduling, global dispatch, and cursor commit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol, cast, final

from ea.core.execution import (
    InstrumentExecutionSpecSet,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import SourceNamespace
from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import DataFingerprint, ReplayWindow, RunId, Sha256Digest
from ea.core.runtime import (
    BoundedRuntimeRootPlan,
    EndOfRunKind,
    EndOfRunRoot,
    RuntimeIdentifier,
    RuntimeOrderingError,
    RuntimeRoot,
    RuntimeRootOrderKey,
    prepare_bounded_runtime_roots,
    runtime_root_order_key,
)
from ea.core.time import Clock, TimeValidationError, require_utc
from ea.runtime.queue import RuntimeDispatchLease

PHASE1_HISTORICAL_MARKET_PROFILE = "ea-phase1-ohlcv-csv-v1"
HISTORICAL_RUNTIME_TRACE_SCHEMA = "ea.phase1-historical-runtime-trace.v1"
HISTORICAL_RUNTIME_TRACE_DIGEST_DOMAIN = b"ea.phase1-historical-runtime-trace.v1\0"
HISTORICAL_RUNTIME_PRODUCER_NAMESPACE = "runtime.phase1.historical"

_MAX_UINT64 = (1 << 64) - 1
_CLOCK_SEAL = object()
_OFFER_SEAL = object()
_PREPARED_SEAL = object()


def _fail(code: OutcomeCode, message: str) -> RuntimeOrderingError:
    return RuntimeOrderingError(code, message)


def _utc(value: object, *, field: str) -> datetime:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must have exact runtime type datetime")
    try:
        return require_utc(value, field=field)
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} must be canonical UTC") from error


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@final
@dataclass(frozen=True, slots=True)
class HistoricalMarketSourceBinding:
    """Runtime-owned immutable binding exposed by a historical source port."""

    profile: str
    replay_window: ReplayWindow
    data_fingerprint: DataFingerprint

    def __post_init__(self) -> None:
        if (
            type(self.profile) is not str
            or type(self.replay_window) is not ReplayWindow
            or type(self.data_fingerprint) is not DataFingerprint
        ):
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "historical source binding fields require exact canonical types",
            )
        if self.profile != PHASE1_HISTORICAL_MARKET_PROFILE:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "historical source binding profile conflicts with the Phase 1 profile",
            )


class HistoricalMarketCandidate(Protocol):
    """Opaque source-issued candidate visible to runtime only through scalar evidence."""

    @property
    def event(self) -> MarketDataEnvelope: ...

    @property
    def scheduled_at(self) -> datetime: ...

    @property
    def cursor_as_of(self) -> datetime: ...

    @property
    def canonical_event_bytes(self) -> bytes: ...


class HistoricalMarketPreparedCommit(Protocol):
    """Opaque source-issued prepared commit marker."""

    def _historical_market_prepared_commit_marker(self) -> None: ...


class HistoricalMarketSourcePort(Protocol):
    """Consumer-owned port implemented by an outer historical data bridge."""

    @property
    def binding(self) -> HistoricalMarketSourceBinding: ...

    def next_available_at(self) -> datetime | None: ...

    def admit_one(self, *, clock: Clock) -> HistoricalMarketCandidate: ...

    def prepare_commit(
        self,
        candidate: HistoricalMarketCandidate,
    ) -> HistoricalMarketPreparedCommit: ...

    def commit(self, prepared: HistoricalMarketPreparedCommit) -> None: ...


@final
@dataclass(frozen=True, slots=True, init=False)
class Phase1VirtualClock:
    """Factory-only read-only virtual clock capability."""

    _current: datetime
    _seal: object

    def __init__(self) -> None:
        raise TypeError("virtual clocks are created only by the historical runtime factory")

    def now(self) -> datetime:
        return self._current


def _create_virtual_clock(initial: datetime) -> Phase1VirtualClock:
    clock = object.__new__(Phase1VirtualClock)
    object.__setattr__(clock, "_seal", _CLOCK_SEAL)
    object.__setattr__(clock, "_current", _utc(initial, field="initial"))
    return clock


def _advance_virtual_clock(clock: Phase1VirtualClock, target: datetime) -> None:
    if type(clock) is not Phase1VirtualClock or getattr(clock, "_seal", None) is not _CLOCK_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "virtual clock is not factory-issued")
    advanced = _utc(target, field="target")
    if advanced < clock._current:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "virtual clock cannot regress")
    object.__setattr__(clock, "_current", advanced)


@final
@dataclass(frozen=True, slots=True, init=False)
class _RuntimeRootOffer:
    _seal: object
    producer_id: RuntimeIdentifier
    plan: BoundedRuntimeRootPlan
    root: RuntimeRoot
    order_key: RuntimeRootOrderKey
    canonical_root_bytes: bytes
    candidate: object | None
    terminal: bool

    def __init__(self) -> None:
        raise TypeError("runtime root offers are created only by their factory")


def _create_offer(
    *,
    producer_id: RuntimeIdentifier,
    plan: BoundedRuntimeRootPlan,
    root: RuntimeRoot,
    canonical_root_bytes: bytes,
    candidate: object | None,
    terminal: bool,
) -> _RuntimeRootOffer:
    if (
        type(producer_id) is not RuntimeIdentifier
        or type(plan) is not BoundedRuntimeRootPlan
        or type(canonical_root_bytes) is not bytes
        or type(terminal) is not bool
        or len(plan.roots) != 1
        or plan.roots[0] is not root
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime root offer has invalid exact carriers")
    offer = object.__new__(_RuntimeRootOffer)
    object.__setattr__(offer, "_seal", _OFFER_SEAL)
    object.__setattr__(offer, "producer_id", producer_id)
    object.__setattr__(offer, "plan", plan)
    object.__setattr__(offer, "root", root)
    object.__setattr__(offer, "order_key", runtime_root_order_key(root))
    object.__setattr__(offer, "canonical_root_bytes", canonical_root_bytes)
    object.__setattr__(offer, "candidate", candidate)
    object.__setattr__(offer, "terminal", terminal)
    return offer


class _ProducerResponseKind(StrEnum):
    OFFER = "offer"
    LOWER_BOUND = "lower_bound"
    EXHAUSTED = "exhausted"


@final
@dataclass(frozen=True, slots=True)
class _ProducerResponse:
    kind: _ProducerResponseKind
    offer: _RuntimeRootOffer | None
    lower_bound: datetime | None


def _offer_response(offer: _RuntimeRootOffer) -> _ProducerResponse:
    if type(offer) is not _RuntimeRootOffer or offer._seal is not _OFFER_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "producer offer is not factory-issued")
    return _ProducerResponse(_ProducerResponseKind.OFFER, offer, None)


def _lower_bound_response(lower_bound: datetime) -> _ProducerResponse:
    return _ProducerResponse(_ProducerResponseKind.LOWER_BOUND, None, lower_bound)


def _exhausted_response() -> _ProducerResponse:
    return _ProducerResponse(_ProducerResponseKind.EXHAUSTED, None, None)


class _RuntimeRootProducer(Protocol):
    @property
    def producer_id(self) -> RuntimeIdentifier: ...

    def poll(self, *, clock: Clock) -> _ProducerResponse: ...

    def prepare_commit(
        self,
        offer: _RuntimeRootOffer,
        *,
        dispatch_sequence: int,
    ) -> object: ...

    def commit(self, prepared: object) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _CandidateView:
    candidate: HistoricalMarketCandidate
    event: MarketDataEnvelope
    scheduled_at: datetime
    cursor_as_of: datetime
    canonical_event_bytes: bytes


def _candidate_view(candidate_object: object) -> _CandidateView:
    candidate = cast(Any, candidate_object)
    try:
        event = candidate.event
        scheduled_at = candidate.scheduled_at
        cursor_as_of = candidate.cursor_as_of
        canonical_bytes = candidate.canonical_event_bytes
    except (AttributeError, TypeError) as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "historical candidate has an incomplete operation contract",
        ) from error
    if type(event) is not MarketDataEnvelope or type(canonical_bytes) is not bytes:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "historical candidate values require exact canonical types",
        )
    return _CandidateView(
        candidate=cast(HistoricalMarketCandidate, candidate_object),
        event=event,
        scheduled_at=_utc(scheduled_at, field="candidate.scheduled_at"),
        cursor_as_of=_utc(cursor_as_of, field="candidate.cursor_as_of"),
        canonical_event_bytes=canonical_bytes,
    )


def _terminal_root_bytes(root: EndOfRunRoot) -> bytes:
    document = {
        "available_at": _utc_text(root.available_at),
        "kind": root.kind.value,
        "producer_namespace": root.producer_namespace.value,
        "producer_sequence": root.producer_sequence,
        "run_id": root.run_id.value,
        "type": "end_of_run",
    }
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_order_key(key: RuntimeRootOrderKey) -> list[str | int]:
    values: list[str | int] = []
    for value in key.as_tuple():
        if type(value) is datetime:
            values.append(_utc_text(value))
        elif type(value) is int or type(value) is str:
            values.append(value)
        else:
            raise AssertionError("runtime root order key contains an unsupported canonical value")
    return values


def _trace_record_bytes(
    *,
    run_id: RunId,
    fingerprint: DataFingerprint,
    clock_now: datetime,
    dispatch_sequence: int,
    offer: _RuntimeRootOffer,
    committed_event_count: int,
    committed_cursor_as_of: datetime | None,
    terminal_acknowledged: bool,
) -> bytes:
    root_document = json.loads(offer.canonical_root_bytes.decode("utf-8"))
    if type(root_document) is not dict:
        raise AssertionError("canonical runtime root bytes must decode to one object")
    document = {
        "clock_now": _utc_text(clock_now),
        "committed_cursor_as_of": (
            None if committed_cursor_as_of is None else _utc_text(committed_cursor_as_of)
        ),
        "committed_event_count": committed_event_count,
        "data_sha256": fingerprint.sha256.value,
        "dispatch_sequence": dispatch_sequence,
        "root": root_document,
        "root_order_key": _json_order_key(offer.order_key),
        "run_id": run_id.value,
        "schema": HISTORICAL_RUNTIME_TRACE_SCHEMA,
        "terminal_acknowledged": terminal_acknowledged,
    }
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def historical_runtime_trace_digest(records: tuple[bytes, ...]) -> Sha256Digest:
    """Digest exact canonical historical runtime trace records."""
    if type(records) is not tuple or any(type(record) is not bytes for record in records):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "historical runtime trace records must be one exact tuple of bytes",
        )
    digest = sha256(HISTORICAL_RUNTIME_TRACE_DIGEST_DOMAIN)
    for record in records:
        if len(record) > _MAX_UINT64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "historical runtime trace record is too large")
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    digest.update(len(records).to_bytes(8, "big"))
    return Sha256Digest(digest.hexdigest())


@final
@dataclass(frozen=True, slots=True, init=False)
class _PreparedHistoricalCommit:
    _seal: object
    offer: _RuntimeRootOffer
    source_prepared: HistoricalMarketPreparedCommit | None
    next_committed_count: int
    next_last_key: RuntimeRootOrderKey
    trace_record: bytes
    terminal: bool

    def __init__(self) -> None:
        raise TypeError("historical producer commits are created only by prepare_commit")


@final
class _HistoricalMarketProducer:
    __slots__ = (
        "_binding",
        "_clock",
        "_committed_count",
        "_current_offer",
        "_last_market_key",
        "_market_promise",
        "_producer_id",
        "_run_id",
        "_source",
        "_terminal_acknowledged",
        "_terminal_promise",
    )

    _binding: HistoricalMarketSourceBinding
    _clock: Phase1VirtualClock
    _committed_count: int
    _current_offer: _RuntimeRootOffer | None
    _last_market_key: RuntimeRootOrderKey | None
    _market_promise: datetime | None
    _producer_id: RuntimeIdentifier
    _run_id: RunId
    _source: HistoricalMarketSourcePort
    _terminal_acknowledged: bool
    _terminal_promise: bool

    def __init__(self) -> None:
        raise TypeError("historical producers are created only by the runtime factory")

    @property
    def producer_id(self) -> RuntimeIdentifier:
        return self._producer_id

    @property
    def committed_event_count(self) -> int:
        return self._committed_count

    @property
    def terminal_acknowledged(self) -> bool:
        return self._terminal_acknowledged

    def poll(self, *, clock: Clock) -> _ProducerResponse:
        if clock is not self._clock:
            raise _fail(OutcomeCode.CONFLICTING_ID, "historical producer clock binding conflicts")
        if self._terminal_acknowledged:
            return _exhausted_response()
        if self._current_offer is not None:
            return _offer_response(self._current_offer)
        now = self._clock.now()
        if self._terminal_promise:
            if now < self._binding.replay_window.end_exclusive:
                return _lower_bound_response(self._binding.replay_window.end_exclusive)
            if now > self._binding.replay_window.end_exclusive:
                raise _fail(OutcomeCode.OUT_OF_RANGE, "clock advanced beyond replay end")
            return _offer_response(self._create_terminal_offer())

        scheduled = self._market_promise
        if scheduled is None:
            scheduled_value = self._source.next_available_at()
            if scheduled_value is None:
                if self._committed_count == 0:
                    raise _fail(
                        OutcomeCode.CONFLICTING_ID,
                        "historical source exhausted before any committed event",
                    )
                self._terminal_promise = True
                if now < self._binding.replay_window.end_exclusive:
                    return _lower_bound_response(self._binding.replay_window.end_exclusive)
                if now > self._binding.replay_window.end_exclusive:
                    raise _fail(OutcomeCode.OUT_OF_RANGE, "clock advanced beyond replay end")
                return _offer_response(self._create_terminal_offer())
            scheduled = _utc(scheduled_value, field="source.next_available_at")
            if scheduled < now or scheduled >= self._binding.replay_window.end_exclusive:
                raise _fail(
                    OutcomeCode.OUT_OF_RANGE,
                    "historical source schedule is outside the active clock/window frontier",
                )
            self._market_promise = scheduled

        if now < scheduled:
            return _lower_bound_response(scheduled)
        if now > scheduled:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "clock advanced beyond the binding historical market promise",
            )
        candidate_object = self._source.admit_one(clock=self._clock)
        view = _candidate_view(candidate_object)
        if not (
            view.scheduled_at == view.event.available_at == view.cursor_as_of == scheduled == now
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "historical candidate schedule/event/cursor/clock binding conflicts",
            )
        encoded = canonical_market_data_record_bytes(view.event)
        if encoded != view.canonical_event_bytes:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "historical candidate canonical bytes conflict",
            )
        plan = prepare_bounded_runtime_roots((view.event,))
        key = runtime_root_order_key(view.event)
        if self._last_market_key is not None and key <= self._last_market_key:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "historical market root did not advance the canonical key",
            )
        offer = _create_offer(
            producer_id=self._producer_id,
            plan=plan,
            root=view.event,
            canonical_root_bytes=encoded,
            candidate=view.candidate,
            terminal=False,
        )
        self._current_offer = offer
        return _offer_response(offer)

    def _create_terminal_offer(self) -> _RuntimeRootOffer:
        end = self._binding.replay_window.end_exclusive
        root = EndOfRunRoot(
            available_at=end,
            kind=EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED,
            producer_namespace=SourceNamespace(HISTORICAL_RUNTIME_PRODUCER_NAMESPACE),
            producer_sequence=0,
            run_id=self._run_id,
        )
        plan = prepare_bounded_runtime_roots((root,))
        offer = _create_offer(
            producer_id=self._producer_id,
            plan=plan,
            root=root,
            canonical_root_bytes=_terminal_root_bytes(root),
            candidate=None,
            terminal=True,
        )
        if self._last_market_key is None or offer.order_key <= self._last_market_key:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "terminal root does not follow the final market root",
            )
        self._current_offer = offer
        return offer

    def prepare_commit(
        self,
        offer: _RuntimeRootOffer,
        *,
        dispatch_sequence: int,
    ) -> object:
        if type(offer) is not _RuntimeRootOffer or offer._seal is not _OFFER_SEAL:
            raise _fail(OutcomeCode.INVALID_TYPE, "commit requires a factory-issued offer")
        if offer is not self._current_offer:
            raise _fail(OutcomeCode.CONFLICTING_ID, "commit offer is stale or foreign")
        if type(dispatch_sequence) is not int or not 1 <= dispatch_sequence <= _MAX_UINT64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence is outside uint64")

        source_prepared: HistoricalMarketPreparedCommit | None = None
        next_count = self._committed_count
        cursor_as_of: datetime | None = None
        if offer.terminal:
            if (
                type(offer.root) is not EndOfRunRoot
                or _terminal_root_bytes(offer.root) != offer.canonical_root_bytes
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "terminal offer bytes conflict")
        else:
            if type(offer.root) is not MarketDataEnvelope or offer.candidate is None:
                raise _fail(OutcomeCode.INVALID_TYPE, "market offer carriers are invalid")
            view = _candidate_view(offer.candidate)
            live_bytes = canonical_market_data_record_bytes(offer.root)
            candidate_bytes = canonical_market_data_record_bytes(view.event)
            if not (
                offer.root is view.event
                and live_bytes
                == candidate_bytes
                == view.canonical_event_bytes
                == offer.canonical_root_bytes
            ):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "live market/candidate/offer canonical bytes conflict",
                )
            source_prepared = self._source.prepare_commit(view.candidate)
            next_count += 1
            cursor_as_of = view.cursor_as_of

        trace = _trace_record_bytes(
            run_id=self._run_id,
            fingerprint=self._binding.data_fingerprint,
            clock_now=self._clock.now(),
            dispatch_sequence=dispatch_sequence,
            offer=offer,
            committed_event_count=next_count,
            committed_cursor_as_of=cursor_as_of,
            terminal_acknowledged=offer.terminal,
        )
        prepared = object.__new__(_PreparedHistoricalCommit)
        object.__setattr__(prepared, "_seal", _PREPARED_SEAL)
        object.__setattr__(prepared, "offer", offer)
        object.__setattr__(prepared, "source_prepared", source_prepared)
        object.__setattr__(prepared, "next_committed_count", next_count)
        object.__setattr__(prepared, "next_last_key", offer.order_key)
        object.__setattr__(prepared, "trace_record", trace)
        object.__setattr__(prepared, "terminal", offer.terminal)
        return prepared

    def commit(self, prepared_object: object) -> bytes:
        if (
            type(prepared_object) is not _PreparedHistoricalCommit
            or prepared_object._seal is not _PREPARED_SEAL
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "producer commit is not factory-prepared")
        prepared = prepared_object
        if prepared.offer is not self._current_offer:
            raise _fail(OutcomeCode.CONFLICTING_ID, "producer commit is stale or foreign")
        if prepared.terminal:
            if prepared.source_prepared is not None:
                raise AssertionError("terminal commit unexpectedly contains source progress")
            self._terminal_acknowledged = True
        else:
            if prepared.source_prepared is None:
                raise AssertionError("market commit lost source prepared state")
            self._source.commit(prepared.source_prepared)
            self._market_promise = None
            self._committed_count = prepared.next_committed_count
            self._last_market_key = prepared.next_last_key
        self._current_offer = None
        return prepared.trace_record


@dataclass(frozen=True, slots=True)
class _ActiveDispatch:
    producer: _RuntimeRootProducer
    offer: _RuntimeRootOffer
    lease: RuntimeDispatchLease
    dispatch_sequence: int


@final
class _RunWideDispatcher:
    __slots__ = (
        "_active",
        "_clock",
        "_exhausted",
        "_next_sequence",
        "_producers",
        "_responses",
        "_trace_records",
    )

    _active: _ActiveDispatch | None
    _clock: Phase1VirtualClock
    _exhausted: bool
    _next_sequence: int | None
    _producers: tuple[_RuntimeRootProducer, ...]
    _responses: dict[RuntimeIdentifier, _ProducerResponse]
    _trace_records: tuple[bytes, ...]

    def __init__(self) -> None:
        raise TypeError("run-wide dispatchers are created only by their factory")

    @property
    def next_dispatch_sequence(self) -> int | None:
        return self._next_sequence

    @property
    def active_lease(self) -> RuntimeDispatchLease | None:
        return None if self._active is None else self._active.lease

    @property
    def trace_records(self) -> tuple[bytes, ...]:
        return self._trace_records

    def peek(self) -> RuntimeRoot:
        if self._active is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "one runtime dispatch remains active")
        return self._select_offer().root

    def pop(self) -> RuntimeDispatchLease:
        if self._active is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "one runtime dispatch remains active")
        sequence = self._next_sequence
        if sequence is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "runtime dispatch sequence is exhausted")
        offer = self._select_offer()
        producer = self._producer_for(offer.producer_id)
        lease = object.__new__(RuntimeDispatchLease)
        object.__setattr__(lease, "_root", offer.root)
        object.__setattr__(lease, "_dispatch_sequence", sequence)
        self._next_sequence = None if sequence == _MAX_UINT64 else sequence + 1
        self._active = _ActiveDispatch(producer, offer, lease, sequence)
        return lease

    def acknowledge(self, lease: RuntimeDispatchLease) -> None:
        if type(lease) is not RuntimeDispatchLease:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "acknowledgement requires an exact RuntimeDispatchLease",
            )
        active = self._active
        if active is None or active.lease is not lease:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "acknowledgement does not match the exact live dispatch",
            )
        if (
            lease.root is not active.offer.root
            or lease.dispatch_sequence != active.dispatch_sequence
            or runtime_root_order_key(lease.root) != active.offer.order_key
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "live dispatch evidence conflicts")
        prepared = active.producer.prepare_commit(
            active.offer,
            dispatch_sequence=active.dispatch_sequence,
        )
        trace = active.producer.commit(prepared)
        if type(trace) is not bytes:
            raise AssertionError("producer commit must return exact canonical trace bytes")
        self._responses.pop(active.offer.producer_id)
        self._trace_records = (*self._trace_records, trace)
        self._active = None

    def _producer_for(self, producer_id: RuntimeIdentifier) -> _RuntimeRootProducer:
        for producer in self._producers:
            if producer.producer_id == producer_id:
                return producer
        raise AssertionError("selected runtime producer is not registered")

    def _select_offer(self) -> _RuntimeRootOffer:
        if self._exhausted:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "run-wide dispatcher is exhausted")
        while True:
            self._poll_missing()
            responses = tuple(self._responses[producer.producer_id] for producer in self._producers)
            offers = tuple(
                response.offer
                for response in responses
                if response.kind is _ProducerResponseKind.OFFER and response.offer is not None
            )
            if offers:
                return min(offers, key=lambda offer: offer.order_key)
            if all(response.kind is _ProducerResponseKind.EXHAUSTED for response in responses):
                self._exhausted = True
                raise _fail(OutcomeCode.OUT_OF_RANGE, "run-wide dispatcher is exhausted")
            bounds = tuple(
                response.lower_bound
                for response in responses
                if response.kind is _ProducerResponseKind.LOWER_BOUND
                and response.lower_bound is not None
            )
            if not bounds:
                raise AssertionError("producer response set has no offer, bound, or exhaustion")
            selected = min(bounds)
            _advance_virtual_clock(self._clock, selected)
            due = tuple(
                producer.producer_id
                for producer in self._producers
                if (
                    self._responses[producer.producer_id].kind is _ProducerResponseKind.LOWER_BOUND
                    and self._responses[producer.producer_id].lower_bound == selected
                )
            )
            for producer_id in due:
                self._responses.pop(producer_id)

    def _poll_missing(self) -> None:
        now = self._clock.now()
        for producer in self._producers:
            if producer.producer_id in self._responses:
                continue
            response = producer.poll(clock=self._clock)
            self._responses[producer.producer_id] = _validate_response(
                response,
                producer_id=producer.producer_id,
                clock_now=now,
            )


def _validate_response(
    response: object,
    *,
    producer_id: RuntimeIdentifier,
    clock_now: datetime,
) -> _ProducerResponse:
    if type(response) is not _ProducerResponse:
        raise _fail(OutcomeCode.INVALID_TYPE, "producer response has the wrong exact carrier")
    if response.kind is _ProducerResponseKind.OFFER:
        offer = response.offer
        if (
            type(offer) is not _RuntimeRootOffer
            or offer._seal is not _OFFER_SEAL
            or offer.producer_id != producer_id
            or response.lower_bound is not None
            or offer.order_key.available_at != clock_now
            or runtime_root_order_key(offer.root) != offer.order_key
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "producer offer conflicts")
        return response
    if response.kind is _ProducerResponseKind.LOWER_BOUND:
        if response.offer is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "lower-bound response contains an offer")
        bound = _utc(response.lower_bound, field="producer.lower_bound")
        if bound <= clock_now:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "producer lower bound must be strictly later than the current clock",
            )
        return _ProducerResponse(response.kind, None, bound)
    if response.kind is _ProducerResponseKind.EXHAUSTED:
        if response.offer is not None or response.lower_bound is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "exhausted response contains extra state")
        return response
    raise _fail(OutcomeCode.INVALID_TYPE, "producer response kind is invalid")


def _create_run_wide_dispatcher(
    *,
    clock: Phase1VirtualClock,
    producers: tuple[_RuntimeRootProducer, ...],
) -> _RunWideDispatcher:
    if type(clock) is not Phase1VirtualClock or clock._seal is not _CLOCK_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatcher clock is not factory-issued")
    if type(producers) is not tuple or not producers:
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatcher producers must be a non-empty tuple")
    identifiers: list[RuntimeIdentifier] = []
    for producer in producers:
        candidate = cast(Any, producer)
        try:
            producer_id = candidate.producer_id
            poll = candidate.poll
            prepare_commit = candidate.prepare_commit
            commit = candidate.commit
        except (AttributeError, TypeError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE, "runtime producer contract is incomplete"
            ) from error
        if (
            type(producer_id) is not RuntimeIdentifier
            or not callable(poll)
            or not callable(prepare_commit)
            or not callable(commit)
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "runtime producer contract has invalid members")
        identifiers.append(producer_id)
    values = tuple(identifier.value for identifier in identifiers)
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "runtime producers must have unique canonical-order identifiers",
        )
    dispatcher = object.__new__(_RunWideDispatcher)
    dispatcher._active = None
    dispatcher._clock = clock
    dispatcher._exhausted = False
    dispatcher._next_sequence = 1
    dispatcher._producers = producers
    dispatcher._responses = {}
    dispatcher._trace_records = ()
    return dispatcher


@final
class Phase1HistoricalMarketRuntime:
    """Single-use Phase 1 historical market runtime facade."""

    __slots__ = ("_clock", "_dispatcher", "_producer", "_run_id", "_spec_set")

    _clock: Phase1VirtualClock
    _dispatcher: _RunWideDispatcher
    _producer: _HistoricalMarketProducer
    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet

    def __init__(self) -> None:
        raise TypeError(
            "historical market runtimes are created only by create_phase1_historical_market_runtime"
        )

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def committed_event_count(self) -> int:
        return self._producer.committed_event_count

    @property
    def next_dispatch_sequence(self) -> int | None:
        return self._dispatcher.next_dispatch_sequence

    @property
    def active_lease(self) -> RuntimeDispatchLease | None:
        return self._dispatcher.active_lease

    @property
    def terminal_acknowledged(self) -> bool:
        return self._producer.terminal_acknowledged

    @property
    def trace_records(self) -> tuple[bytes, ...]:
        return self._dispatcher.trace_records

    @property
    def trace_digest(self) -> Sha256Digest:
        return historical_runtime_trace_digest(self.trace_records)

    def peek(self) -> RuntimeRoot:
        return self._dispatcher.peek()

    def pop(self) -> RuntimeDispatchLease:
        return self._dispatcher.pop()

    def acknowledge(self, lease: RuntimeDispatchLease) -> None:
        self._dispatcher.acknowledge(lease)


def _require_source_port(
    source: object,
) -> tuple[HistoricalMarketSourcePort, HistoricalMarketSourceBinding]:
    candidate = cast(Any, source)
    try:
        binding = candidate.binding
        next_available_at = candidate.next_available_at
        admit_one = candidate.admit_one
        prepare_commit = candidate.prepare_commit
        commit = candidate.commit
    except (AttributeError, TypeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "historical source port is incomplete") from error
    if (
        type(binding) is not HistoricalMarketSourceBinding
        or not callable(next_available_at)
        or not callable(admit_one)
        or not callable(prepare_commit)
        or not callable(commit)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "historical source port has invalid members")
    return cast(HistoricalMarketSourcePort, source), binding


def create_phase1_historical_market_runtime(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    source: HistoricalMarketSourcePort,
) -> Phase1HistoricalMarketRuntime:
    """Create one run-wide historical market runtime over a consumer-owned source port."""
    if type(run_id) is not RunId or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "historical runtime run/spec bindings require exact canonical types",
        )
    port, binding = _require_source_port(source)
    if binding.profile != PHASE1_HISTORICAL_MARKET_PROFILE:
        raise _fail(OutcomeCode.CONFLICTING_ID, "historical source profile conflicts")
    if binding.data_fingerprint.record_count + 1 > _MAX_UINT64:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "historical source cannot reserve dispatch identity for the terminal root",
        )
    instrument_spec_set_digest(spec_set)
    clock = _create_virtual_clock(binding.replay_window.start_inclusive)
    producer = object.__new__(_HistoricalMarketProducer)
    producer._binding = binding
    producer._clock = clock
    producer._committed_count = 0
    producer._current_offer = None
    producer._last_market_key = None
    producer._market_promise = None
    producer._producer_id = RuntimeIdentifier(HISTORICAL_RUNTIME_PRODUCER_NAMESPACE)
    producer._run_id = run_id
    producer._source = port
    producer._terminal_acknowledged = False
    producer._terminal_promise = False
    dispatcher = _create_run_wide_dispatcher(clock=clock, producers=(producer,))
    runtime = object.__new__(Phase1HistoricalMarketRuntime)
    runtime._clock = clock
    runtime._dispatcher = dispatcher
    runtime._producer = producer
    runtime._run_id = run_id
    runtime._spec_set = spec_set
    return runtime
