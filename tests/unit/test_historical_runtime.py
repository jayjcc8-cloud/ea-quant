from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import ea.runtime.historical as historical_runtime
from ea.core import (
    CanonicalDecimal,
    Clock,
    EndOfRunKind,
    EndOfRunRoot,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    MarketDataEnvelope,
    OutcomeCode,
    PriceDomain,
    ReplayWindow,
    RunId,
    RuntimeIdentifier,
    RuntimeOrderingError,
    SafetyKind,
    SafetyRoot,
    SettlementCurrency,
    SourceNamespace,
    TimerKind,
    TimerRoot,
    VenueId,
    build_instrument_spec_set,
    prepare_bounded_runtime_roots,
)
from ea.data import (
    HistoricalMarketDataError,
    HistoricalMarketDataFailureCode,
    Phase1HistoricalMarketDataSource,
    Phase1HistoricalMarketSourceBridge,
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.runtime import (
    HISTORICAL_RUNTIME_TRACE_SCHEMA,
    Phase1HistoricalMarketRuntime,
    Phase1VirtualClock,
    RuntimeDispatchLease,
    create_phase1_historical_market_runtime,
    historical_runtime_trace_digest,
)

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
WINDOW = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
)
HEADER = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("historical-runtime-test-v1"),
    (
        InstrumentExecutionSpec(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            specification_id=InstrumentSpecId("xnas-aapl-v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        ),
    ),
)


def _row(
    *,
    start: str,
    end: str,
    available: str,
    sequence: int,
) -> str:
    return ",".join(
        (
            "1",
            "XNAS",
            "AAPL",
            start,
            end,
            "raw",
            "100.0",
            "101.0",
            "99.0",
            "100.5",
            "10.0",
            "fixture.raw",
            str(sequence),
            "0",
            available,
        )
    )


def _source(*rows: str) -> Phase1HistoricalMarketDataSource:
    content = ("\n".join((HEADER, *rows)) + "\n").encode()
    dataset = decode_phase1_ohlcv_csv(content, replay_window=WINDOW)
    return create_phase1_historical_market_data_source(dataset)


def _runtime(
    *rows: str,
) -> tuple[Phase1HistoricalMarketRuntime, Phase1HistoricalMarketSourceBridge]:
    bridge = create_phase1_historical_market_source_bridge(_source(*rows))
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=bridge,
    )
    return runtime, bridge


def _two_rows() -> tuple[str, str]:
    return (
        _row(
            start="2026-01-02T09:30:00.000000Z",
            end="2026-01-02T09:31:00.000000Z",
            available="2026-01-02T09:31:00.000000Z",
            sequence=0,
        ),
        _row(
            start="2026-01-02T09:31:00.000000Z",
            end="2026-01-02T09:32:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=1,
        ),
    )


def test_historical_runtime_dispatches_market_roots_then_one_terminal() -> None:
    runtime, bridge = _runtime(*_two_rows())

    assert runtime.clock.now() == WINDOW.start_inclusive
    first = runtime.peek()
    assert type(first) is MarketDataEnvelope
    assert runtime.clock.now() == first.available_at
    assert bridge.next_available_at() == first.available_at

    first_lease = runtime.pop()
    assert first_lease.root is first
    assert first_lease.dispatch_sequence == 1
    assert runtime.next_dispatch_sequence == 2
    assert runtime.committed_event_count == 0
    with pytest.raises(RuntimeOrderingError) as active:
        runtime.peek()
    assert active.value.code is OutcomeCode.CONFLICTING_ID

    runtime.acknowledge(first_lease)
    assert runtime.committed_event_count == 1
    assert bridge.next_available_at() == datetime(2026, 1, 2, 9, 32, tzinfo=UTC)

    second_lease = runtime.pop()
    assert type(second_lease.root) is MarketDataEnvelope
    assert second_lease.dispatch_sequence == 2
    runtime.acknowledge(second_lease)

    terminal_lease = runtime.pop()
    terminal = cast(EndOfRunRoot, terminal_lease.root)
    assert type(terminal) is EndOfRunRoot
    assert terminal_lease.dispatch_sequence == 3
    assert terminal == EndOfRunRoot(
        available_at=WINDOW.end_exclusive,
        kind=EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED,
        producer_namespace=terminal.producer_namespace,
        producer_sequence=0,
        run_id=RUN_ID,
    )
    assert runtime.clock.now() == WINDOW.end_exclusive
    runtime.acknowledge(terminal_lease)

    assert runtime.terminal_acknowledged
    assert runtime.committed_event_count == 2
    assert len(runtime.trace_records) == 3
    assert runtime.trace_digest == historical_runtime_trace_digest(runtime.trace_records)
    with pytest.raises(RuntimeOrderingError) as exhausted:
        runtime.pop()
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE


def test_trace_records_use_the_exact_market_and_terminal_schema() -> None:
    runtime, _bridge = _runtime(_two_rows()[0])
    market_lease = runtime.pop()
    runtime.acknowledge(market_lease)
    terminal_lease = runtime.pop()
    runtime.acknowledge(terminal_lease)

    market = json.loads(runtime.trace_records[0])
    terminal = json.loads(runtime.trace_records[1])
    assert market["schema"] == HISTORICAL_RUNTIME_TRACE_SCHEMA
    assert market["dispatch_sequence"] == 1
    assert market["committed_event_count"] == 1
    assert market["committed_cursor_as_of"] == "2026-01-02T09:31:00.000000Z"
    assert market["root"]["close_bits"] == "4059200000000000"
    assert market["root_order_key"][:4] == [
        "2026-01-02T09:31:00.000000Z",
        30,
        "2026-01-02T09:31:00.000000Z",
        0,
    ]
    assert terminal["dispatch_sequence"] == 2
    assert terminal["terminal_acknowledged"] is True
    assert terminal["committed_cursor_as_of"] is None
    assert terminal["root"] == {
        "available_at": "2026-01-02T10:00:00.000000Z",
        "kind": "bounded_source_exhausted",
        "producer_namespace": "runtime.phase1.historical",
        "producer_sequence": 0,
        "run_id": RUN_ID.value,
        "type": "end_of_run",
    }
    assert terminal["root_order_key"] == [
        "2026-01-02T10:00:00.000000Z",
        50,
        0,
        "runtime.phase1.historical",
        0,
        RUN_ID.value,
    ]


def test_source_cursor_commits_only_after_exact_live_acknowledgement() -> None:
    runtime, bridge = _runtime(*_two_rows())
    lease = runtime.pop()
    scheduled = cast(MarketDataEnvelope, lease.root).available_at

    assert bridge.next_available_at() == scheduled
    with pytest.raises(RuntimeOrderingError) as wrong:
        runtime.acknowledge(cast(RuntimeDispatchLease, object()))
    assert wrong.value.code is OutcomeCode.INVALID_TYPE
    assert bridge.next_available_at() == scheduled
    assert runtime.committed_event_count == 0

    runtime.acknowledge(lease)
    assert bridge.next_available_at() != scheduled
    with pytest.raises(RuntimeOrderingError) as repeated:
        runtime.acknowledge(lease)
    assert repeated.value.code is OutcomeCode.CONFLICTING_ID


def test_payload_change_outside_root_key_fails_before_source_commit() -> None:
    runtime, bridge = _runtime(_two_rows()[0])
    lease = runtime.pop()
    market = cast(MarketDataEnvelope, lease.root)
    object.__setattr__(market.payload, "close", 100.25)

    with pytest.raises(RuntimeOrderingError) as caught:
        runtime.acknowledge(lease)
    assert caught.value.code is OutcomeCode.CONFLICTING_ID
    assert runtime.active_lease is lease
    assert runtime.committed_event_count == 0
    assert bridge.next_available_at() == market.available_at


def test_direct_construction_and_wrong_bridge_source_fail_closed() -> None:
    with pytest.raises(TypeError):
        Phase1VirtualClock()
    with pytest.raises(TypeError):
        Phase1HistoricalMarketRuntime()
    with pytest.raises(TypeError):
        Phase1HistoricalMarketSourceBridge()
    with pytest.raises(HistoricalMarketDataError) as caught:
        create_phase1_historical_market_source_bridge(
            cast(Phase1HistoricalMarketDataSource, object())
        )
    assert caught.value.code is HistoricalMarketDataFailureCode.INVALID_TYPE


def test_virtual_clock_is_publicly_read_only() -> None:
    runtime, _bridge = _runtime(_two_rows()[0])

    with pytest.raises(FrozenInstanceError):
        cast(Any, runtime.clock)._current = WINDOW.end_exclusive

    assert runtime.clock.now() == WINDOW.start_inclusive


class _CountingPort:
    def __init__(
        self,
        bridge: Phase1HistoricalMarketSourceBridge,
        *,
        fail_first_admission: bool = False,
    ) -> None:
        self.bridge = bridge
        self.next_calls = 0
        self.admit_calls = 0
        self.fail_first_admission = fail_first_admission

    @property
    def binding(self) -> historical_runtime.HistoricalMarketSourceBinding:
        return self.bridge.binding

    def next_available_at(self) -> datetime | None:
        self.next_calls += 1
        scheduled = self.bridge.next_available_at()
        if self.next_calls == 1:
            return scheduled
        if scheduled is None:
            return None
        return scheduled + timedelta(minutes=1)

    def admit_one(
        self,
        *,
        clock: Clock,
    ) -> historical_runtime.HistoricalMarketCandidate:
        self.admit_calls += 1
        if self.fail_first_admission and self.admit_calls == 1:
            raise LookupError("declared test admission failure")
        return self.bridge.admit_one(clock=clock)

    def prepare_commit(
        self,
        candidate: historical_runtime.HistoricalMarketCandidate,
    ) -> historical_runtime.HistoricalMarketPreparedCommit:
        return self.bridge.prepare_commit(candidate)

    def commit(
        self,
        prepared: historical_runtime.HistoricalMarketPreparedCommit,
    ) -> None:
        self.bridge.commit(prepared)


def test_due_market_promise_admits_without_asking_source_to_revise_it() -> None:
    bridge = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    port = _CountingPort(bridge)
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=port,
    )

    lease = runtime.pop()

    assert cast(MarketDataEnvelope, lease.root).available_at == datetime(
        2026, 1, 2, 9, 31, tzinfo=UTC
    )
    assert port.next_calls == 1
    assert port.admit_calls == 1


def test_failed_due_admission_retries_same_promise_without_source_revision() -> None:
    bridge = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    port = _CountingPort(bridge, fail_first_admission=True)
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=port,
    )

    with pytest.raises(LookupError, match="declared test admission failure"):
        runtime.pop()
    assert runtime.clock.now() == datetime(2026, 1, 2, 9, 31, tzinfo=UTC)

    lease = runtime.pop()

    assert cast(MarketDataEnvelope, lease.root).available_at == runtime.clock.now()
    assert port.next_calls == 1
    assert port.admit_calls == 2


class _ExactClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


def test_bridge_rejects_foreign_stale_and_repeated_commit_capabilities() -> None:
    scheduled = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
    first = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    second = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    first_candidate = first.admit_one(clock=_ExactClock(scheduled))
    second_candidate = second.admit_one(clock=_ExactClock(scheduled))

    with pytest.raises(RuntimeOrderingError) as foreign_candidate:
        first.prepare_commit(second_candidate)
    assert foreign_candidate.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(RuntimeOrderingError) as wrong_prepared:
        first.commit(cast(historical_runtime.HistoricalMarketPreparedCommit, object()))
    assert wrong_prepared.value.code is OutcomeCode.INVALID_TYPE

    prepared = first.prepare_commit(first_candidate)
    first.commit(prepared)

    with pytest.raises(RuntimeOrderingError) as repeated_prepared:
        first.commit(prepared)
    assert repeated_prepared.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(RuntimeOrderingError) as stale_candidate:
        first.prepare_commit(first_candidate)
    assert stale_candidate.value.code is OutcomeCode.CONFLICTING_ID


def test_equal_availability_market_events_drain_in_complete_key_order() -> None:
    rows = (
        _row(
            start="2026-01-02T09:31:00.000000Z",
            end="2026-01-02T09:32:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=1,
        ),
        _row(
            start="2026-01-02T09:30:00.000000Z",
            end="2026-01-02T09:31:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=0,
        ),
    )
    runtime, _bridge = _runtime(*rows)

    first = runtime.pop()
    runtime.acknowledge(first)
    second = runtime.pop()
    runtime.acknowledge(second)

    first_event = cast(MarketDataEnvelope, first.root)
    second_event = cast(MarketDataEnvelope, second.root)
    assert first_event.available_at == second_event.available_at
    assert first_event.event_time < second_event.event_time
    assert first.dispatch_sequence == 1
    assert second.dispatch_sequence == 2


class _FakeRootProducer:
    def __init__(
        self,
        producer_id: str,
        roots: tuple[SafetyRoot | TimerRoot, ...],
    ) -> None:
        self._producer_id = RuntimeIdentifier(producer_id)
        self.roots = roots
        self.cursor = 0
        self.offer: historical_runtime._RuntimeRootOffer | None = None

    @property
    def producer_id(self) -> RuntimeIdentifier:
        return self._producer_id

    def poll(
        self,
        *,
        clock: Clock,
    ) -> historical_runtime._ProducerResponse:
        if self.offer is not None:
            return historical_runtime._offer_response(self.offer)
        if self.cursor == len(self.roots):
            return historical_runtime._exhausted_response()
        root = self.roots[self.cursor]
        if root.available_at > clock.now():
            return historical_runtime._lower_bound_response(root.available_at)
        assert root.available_at == clock.now()
        self.offer = historical_runtime._create_offer(
            producer_id=self.producer_id,
            plan=prepare_bounded_runtime_roots((root,)),
            root=root,
            canonical_root_bytes=b"{}",
            candidate=None,
            terminal=False,
        )
        return historical_runtime._offer_response(self.offer)

    def prepare_commit(
        self,
        offer: historical_runtime._RuntimeRootOffer,
        *,
        dispatch_sequence: int,
    ) -> object:
        assert offer is self.offer
        return dispatch_sequence

    def commit(self, prepared: object) -> bytes:
        assert type(prepared) is int
        self.offer = None
        self.cursor += 1
        return f"{self.producer_id.value}:{prepared}".encode()


def test_run_wide_dispatcher_arbitrates_all_due_producers_by_complete_root_key() -> None:
    due = datetime(2026, 1, 2, 9, 1, tzinfo=UTC)
    timer = TimerRoot(
        available_at=due,
        kind=TimerKind.STRATEGY_TIMER,
        timer_namespace=SourceNamespace("timer.test"),
        timer_id=RuntimeIdentifier("timer-1"),
        producer_sequence=0,
    )
    safety = SafetyRoot(
        available_at=due,
        kind=SafetyKind.HALT,
        producer_namespace=SourceNamespace("safety.test"),
        producer_sequence=0,
    )
    alpha = _FakeRootProducer("alpha", (timer,))
    beta = _FakeRootProducer("beta", (safety,))
    clock = historical_runtime._create_virtual_clock(WINDOW.start_inclusive)
    dispatcher = historical_runtime._create_run_wide_dispatcher(
        clock=clock,
        producers=cast(
            tuple[historical_runtime._RuntimeRootProducer, ...],
            (alpha, beta),
        ),
    )

    first = dispatcher.pop()
    dispatcher.acknowledge(first)
    second = dispatcher.pop()
    dispatcher.acknowledge(second)

    assert first.root is safety
    assert second.root is timer
    assert (first.dispatch_sequence, second.dispatch_sequence) == (1, 2)
    assert dispatcher.trace_records == (b"beta:1", b"alpha:2")


def test_run_wide_dispatch_sequence_closes_at_uint64_boundary() -> None:
    due = WINDOW.start_inclusive
    timer = TimerRoot(
        available_at=due,
        kind=TimerKind.STRATEGY_TIMER,
        timer_namespace=SourceNamespace("timer.boundary"),
        timer_id=RuntimeIdentifier("timer-max"),
        producer_sequence=0,
    )
    producer = _FakeRootProducer("boundary", (timer,))
    clock = historical_runtime._create_virtual_clock(due)
    dispatcher = historical_runtime._create_run_wide_dispatcher(
        clock=clock,
        producers=cast(
            tuple[historical_runtime._RuntimeRootProducer, ...],
            (producer,),
        ),
    )
    dispatcher._next_sequence = (1 << 64) - 1

    lease = dispatcher.pop()

    assert lease.dispatch_sequence == (1 << 64) - 1
    assert dispatcher.next_dispatch_sequence is None
    dispatcher.acknowledge(lease)
    with pytest.raises(RuntimeOrderingError) as exhausted:
        dispatcher.pop()
    assert exhausted.value.code is OutcomeCode.OUT_OF_RANGE


def test_trace_digest_rejects_non_exact_record_tuple() -> None:
    with pytest.raises(RuntimeOrderingError) as caught:
        historical_runtime_trace_digest(cast(tuple[bytes, ...], [b"not-a-tuple"]))
    assert caught.value.code is OutcomeCode.INVALID_TYPE
