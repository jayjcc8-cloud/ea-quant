from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import ea.runtime.historical as historical_runtime
from ea.core import (
    AdmissionCursor,
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
    canonical_market_data_record_bytes,
    prepare_bounded_runtime_roots,
)
from ea.data import (
    HistoricalMarketDataError,
    HistoricalMarketDataFailureCode,
    Phase1HistoricalMarketDataSource,
    Phase1HistoricalMarketSourceBridge,
    Phase1HistoricalSourceCursor,
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.runtime import (
    HISTORICAL_RUNTIME_TRACE_SCHEMA,
    HistoricalMarketCandidate,
    HistoricalMarketPreparedCommit,
    HistoricalMarketSourceBinding,
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


def _revision_row(*, revision: int, sequence: int) -> str:
    return ",".join(
        (
            "1",
            "XNAS",
            "AAPL",
            "2026-01-02T09:30:00.000000Z",
            "2026-01-02T09:31:00.000000Z",
            "raw",
            "100.0",
            "101.0",
            "99.0",
            "100.5",
            "10.0",
            "fixture.revision",
            str(sequence),
            str(revision),
            "2026-01-02T09:31:00.000000Z",
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


@pytest.mark.parametrize("field", ("_replay_window", "_fingerprint"))
def test_bridge_maps_malformed_exact_source_binding_to_invalid_dataset(
    field: str,
) -> None:
    source = _source(_two_rows()[0])
    object.__setattr__(source, field, object())

    with pytest.raises(HistoricalMarketDataError) as caught:
        create_phase1_historical_market_source_bridge(source)

    assert caught.value.code is HistoricalMarketDataFailureCode.INVALID_DATASET


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


def test_bridge_prepare_commit_is_read_only_and_each_capability_is_single_use() -> None:
    scheduled = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
    bridge = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    candidate = bridge.admit_one(clock=_ExactClock(scheduled))

    first = bridge.prepare_commit(candidate)
    second = bridge.prepare_commit(candidate)

    assert first is not second
    assert bridge.next_available_at() == scheduled
    bridge.commit(first)
    with pytest.raises(RuntimeOrderingError) as repeated:
        bridge.commit(second)
    assert repeated.value.code is OutcomeCode.CONFLICTING_ID


class _BrokenAdmissionSource:
    def __init__(
        self,
        source: Phase1HistoricalMarketDataSource,
        mode: str,
    ) -> None:
        self.source = source
        self.mode = mode

    def next_available_at(
        self,
        cursor: Phase1HistoricalSourceCursor | None,
    ) -> datetime | None:
        return self.source.next_available_at(cursor)

    def admit(
        self,
        *,
        clock: Clock,
        cursor: Phase1HistoricalSourceCursor | None,
        limit: int | None = None,
    ) -> tuple[Phase1HistoricalSourceCursor, tuple[MarketDataEnvelope, ...]]:
        requested = 2 if self.mode == "cursor_mismatch" else limit
        candidate, events = self.source.admit(
            clock=clock,
            cursor=cursor,
            limit=requested,
        )
        if self.mode == "empty":
            return candidate, ()
        if self.mode == "multiple":
            return candidate, (events[0], events[0])
        if self.mode == "cursor_mismatch":
            return candidate, (events[0],)
        if self.mode == "cutoff_mismatch":
            object.__setattr__(
                candidate,
                "_admission_cursor",
                AdmissionCursor(
                    as_of=candidate.as_of + timedelta(minutes=1),
                    last_event=events[0],
                ),
            )
            return candidate, events
        raise AssertionError("unknown broken admission mode")


@pytest.mark.parametrize(
    "mode",
    ("empty", "multiple", "cursor_mismatch", "cutoff_mismatch"),
)
def test_bridge_rejects_broken_concrete_admission_without_retaining_candidate(
    mode: str,
) -> None:
    rows = (
        _row(
            start="2026-01-02T09:30:00.000000Z",
            end="2026-01-02T09:31:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=0,
        ),
        _row(
            start="2026-01-02T09:31:00.000000Z",
            end="2026-01-02T09:32:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=1,
        ),
    )
    bridge = create_phase1_historical_market_source_bridge(_source(*rows))
    broken = _BrokenAdmissionSource(bridge._source, mode)
    bridge._source = cast(Phase1HistoricalMarketDataSource, broken)

    with pytest.raises(RuntimeOrderingError) as caught:
        bridge.admit_one(clock=_ExactClock(datetime(2026, 1, 2, 9, 32, tzinfo=UTC)))

    assert caught.value.code is OutcomeCode.CONFLICTING_ID
    assert bridge._state.candidate is None


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


def test_equal_knowledge_time_revisions_drain_in_revision_order() -> None:
    runtime, _bridge = _runtime(
        _revision_row(revision=1, sequence=1),
        _revision_row(revision=0, sequence=0),
    )

    leases = (runtime.pop(),)
    runtime.acknowledge(leases[0])
    second = runtime.pop()

    assert [cast(MarketDataEnvelope, lease.root).revision for lease in (*leases, second)] == [0, 1]


@dataclass(slots=True)
class _PortCandidate:
    event: MarketDataEnvelope
    scheduled_at: datetime
    cursor_as_of: datetime
    canonical_event_bytes: bytes


@dataclass(frozen=True, slots=True)
class _PortPrepared:
    candidate: _PortCandidate

    def _historical_market_prepared_commit_marker(self) -> None:
        return None


class _ScriptedPort:
    def __init__(
        self,
        *,
        binding: HistoricalMarketSourceBinding,
        candidates: tuple[_PortCandidate, ...],
        prepare_failures: int = 0,
        commit_failures: int = 0,
        advertised_at: datetime | None = None,
    ) -> None:
        self._binding = binding
        self.candidates = candidates
        self.index = 0
        self.next_calls = 0
        self.prepare_calls = 0
        self.commit_calls = 0
        self.prepare_failures = prepare_failures
        self.commit_failures = commit_failures
        self.advertised_at = advertised_at

    @property
    def binding(self) -> HistoricalMarketSourceBinding:
        return self._binding

    def next_available_at(self) -> datetime | None:
        self.next_calls += 1
        if self.index == len(self.candidates):
            return None
        return self.advertised_at or self.candidates[self.index].scheduled_at

    def admit_one(self, *, clock: Clock) -> HistoricalMarketCandidate:
        return self.candidates[self.index]

    def prepare_commit(
        self,
        candidate: HistoricalMarketCandidate,
    ) -> HistoricalMarketPreparedCommit:
        self.prepare_calls += 1
        if self.prepare_failures:
            self.prepare_failures -= 1
            raise LookupError("scripted prepare failure")
        current = self.candidates[self.index]
        assert candidate is current
        return _PortPrepared(current)

    def commit(self, prepared: HistoricalMarketPreparedCommit) -> None:
        self.commit_calls += 1
        if self.commit_failures:
            self.commit_failures -= 1
            raise LookupError("scripted commit failure")
        assert type(prepared) is _PortPrepared
        assert prepared.candidate is self.candidates[self.index]
        self.index += 1


def _scripted_port(
    events: tuple[MarketDataEnvelope, ...],
    *,
    prepare_failures: int = 0,
    commit_failures: int = 0,
) -> _ScriptedPort:
    source = _source(*_two_rows())
    binding = create_phase1_historical_market_source_bridge(source).binding
    return _ScriptedPort(
        binding=binding,
        candidates=tuple(
            _PortCandidate(
                event=event,
                scheduled_at=event.available_at,
                cursor_as_of=event.available_at,
                canonical_event_bytes=canonical_market_data_record_bytes(event),
            )
            for event in events
        ),
        prepare_failures=prepare_failures,
        commit_failures=commit_failures,
    )


def _runtime_for_port(port: _ScriptedPort) -> Phase1HistoricalMarketRuntime:
    return create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=port,
    )


def test_runtime_admits_one_hundred_thousand_and_rejects_the_next_record() -> None:
    admitted = _scripted_port(())
    admitted._binding = replace(
        admitted.binding,
        data_fingerprint=replace(
            admitted.binding.data_fingerprint,
            record_count=100_000,
        ),
    )
    assert type(_runtime_for_port(admitted)) is Phase1HistoricalMarketRuntime

    port = _scripted_port(())
    port._binding = replace(
        port.binding,
        data_fingerprint=replace(
            port.binding.data_fingerprint,
            record_count=100_001,
        ),
    )

    with pytest.raises(RuntimeOrderingError) as captured:
        _runtime_for_port(port)

    assert captured.value.code is OutcomeCode.OUT_OF_RANGE
    assert "100,000-record profile admission bound" in str(captured.value)


@pytest.mark.parametrize("mutation", ("scheduled", "cursor", "both"))
def test_acknowledgement_revalidates_mutable_candidate_time_evidence(
    mutation: str,
) -> None:
    event = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, _two_rows()[0])) + "\n").encode(),
        replay_window=WINDOW,
    ).selection.events[0]
    port = _scripted_port((event,))
    runtime = _runtime_for_port(port)
    lease = runtime.pop()
    candidate = port.candidates[0]
    if mutation in {"scheduled", "both"}:
        candidate.scheduled_at += timedelta(minutes=1)
    if mutation in {"cursor", "both"}:
        candidate.cursor_as_of += timedelta(minutes=1)

    with pytest.raises(RuntimeOrderingError) as caught:
        runtime.acknowledge(lease)

    assert caught.value.code is OutcomeCode.CONFLICTING_ID
    assert runtime.active_lease is lease
    assert runtime.committed_event_count == 0
    assert runtime.trace_records == ()
    assert port.prepare_calls == 0
    assert port.index == 0


def test_runtime_rejects_future_candidate_at_binding_wake_time() -> None:
    events = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, *_two_rows())) + "\n").encode(),
        replay_window=WINDOW,
    ).selection.events
    port = _scripted_port((events[1],))
    port.candidates[0].scheduled_at = events[0].available_at
    runtime = _runtime_for_port(port)

    with pytest.raises(RuntimeOrderingError) as caught:
        runtime.pop()

    assert caught.value.code is OutcomeCode.CONFLICTING_ID
    assert runtime.clock.now() == events[0].available_at
    assert port.index == 0


def test_runtime_rejects_candidate_delayed_beyond_binding_wake_time() -> None:
    event = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, _two_rows()[1])) + "\n").encode(),
        replay_window=WINDOW,
    ).selection.events[0]
    port = _scripted_port((event,))
    port.advertised_at = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
    runtime = _runtime_for_port(port)

    with pytest.raises(RuntimeOrderingError) as caught:
        runtime.pop()

    assert caught.value.code is OutcomeCode.CONFLICTING_ID
    assert runtime.clock.now() == port.advertised_at
    assert port.index == 0


def test_runtime_rejects_non_monotone_root_from_malicious_port() -> None:
    rows = (
        _row(
            start="2026-01-02T09:30:00.000000Z",
            end="2026-01-02T09:31:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=0,
        ),
        _row(
            start="2026-01-02T09:31:00.000000Z",
            end="2026-01-02T09:32:00.000000Z",
            available="2026-01-02T09:32:00.000000Z",
            sequence=1,
        ),
    )
    events = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, *rows)) + "\n").encode(),
        replay_window=WINDOW,
    ).selection.events
    port = _scripted_port(tuple(reversed(events)))
    runtime = _runtime_for_port(port)
    first = runtime.pop()
    runtime.acknowledge(first)

    with pytest.raises(RuntimeOrderingError) as caught:
        runtime.pop()

    assert caught.value.code is OutcomeCode.CONFLICTING_ID
    assert port.index == 1


@pytest.mark.parametrize(
    ("prepare_failures", "commit_failures", "message"),
    ((1, 0, "scripted prepare failure"), (0, 1, "scripted commit failure")),
)
def test_source_prepare_and_commit_failures_leave_ack_state_retryable(
    prepare_failures: int,
    commit_failures: int,
    message: str,
) -> None:
    event = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, _two_rows()[0])) + "\n").encode(),
        replay_window=WINDOW,
    ).selection.events[0]
    port = _scripted_port(
        (event,),
        prepare_failures=prepare_failures,
        commit_failures=commit_failures,
    )
    runtime = _runtime_for_port(port)
    lease = runtime.pop()

    with pytest.raises(LookupError, match=message):
        runtime.acknowledge(lease)
    assert runtime.active_lease is lease
    assert runtime.committed_event_count == 0
    assert runtime.trace_records == ()
    assert port.index == 0

    runtime.acknowledge(lease)
    assert runtime.active_lease is None
    assert runtime.committed_event_count == 1
    assert len(runtime.trace_records) == 1
    assert port.index == 1


@dataclass(frozen=True, slots=True)
class _FakePrepared:
    sequence: int
    trace_record: bytes


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
    ) -> _FakePrepared:
        assert offer is self.offer
        return _FakePrepared(
            dispatch_sequence,
            f"{self.producer_id.value}:{dispatch_sequence}".encode(),
        )

    def commit(self, prepared: historical_runtime._RuntimePreparedCommit) -> None:
        assert type(prepared) is _FakePrepared
        self.offer = None
        self.cursor += 1


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
        replay_end=WINDOW.end_exclusive,
        terminal_producer_id=beta.producer_id,
    )

    first = dispatcher.pop()
    dispatcher.acknowledge(first)
    first_trace_tail = dispatcher._state.trace_tail
    second = dispatcher.pop()
    dispatcher.acknowledge(second)
    second_trace_tail = dispatcher._state.trace_tail

    assert first.root is safety
    assert second.root is timer
    assert (first.dispatch_sequence, second.dispatch_sequence) == (1, 2)
    assert first_trace_tail is not None
    assert second_trace_tail is not None
    assert second_trace_tail.previous is first_trace_tail
    assert dispatcher.trace_records == (b"beta:1", b"alpha:2")


def test_binding_market_promise_beats_intervening_later_domain_at_same_time() -> None:
    bridge = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    port = _CountingPort(bridge)
    clock = historical_runtime._create_virtual_clock(WINDOW.start_inclusive)
    market = historical_runtime._create_historical_market_producer(
        run_id=RUN_ID,
        source=port,
        binding=port.binding,
        clock=clock,
    )
    due = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
    timer_root = TimerRoot(
        available_at=due,
        kind=TimerKind.STRATEGY_TIMER,
        timer_namespace=SourceNamespace("timer.intervening"),
        timer_id=RuntimeIdentifier("timer-at-market-time"),
        producer_sequence=0,
    )
    timer = _FakeRootProducer("z-timer", (timer_root,))
    dispatcher = historical_runtime._create_run_wide_dispatcher(
        clock=clock,
        producers=cast(
            tuple[historical_runtime._RuntimeRootProducer, ...],
            (market, timer),
        ),
        replay_end=WINDOW.end_exclusive,
        terminal_producer_id=market.producer_id,
    )

    lease = dispatcher.pop()

    assert type(lease.root) is MarketDataEnvelope
    assert lease.root.available_at == due
    assert port.next_calls == 1
    assert port.admit_calls == 1


def test_dispatcher_rejects_noncanonical_registration_and_replay_bounds() -> None:
    due = datetime(2026, 1, 2, 9, 1, tzinfo=UTC)
    root = TimerRoot(
        available_at=due,
        kind=TimerKind.STRATEGY_TIMER,
        timer_namespace=SourceNamespace("timer.registration"),
        timer_id=RuntimeIdentifier("timer-registration"),
        producer_sequence=0,
    )
    alpha = _FakeRootProducer("alpha", (root,))
    beta = _FakeRootProducer("beta", ())
    clock = historical_runtime._create_virtual_clock(WINDOW.start_inclusive)

    with pytest.raises(RuntimeOrderingError) as unsorted:
        historical_runtime._create_run_wide_dispatcher(
            clock=clock,
            producers=cast(
                tuple[historical_runtime._RuntimeRootProducer, ...],
                (beta, alpha),
            ),
            replay_end=WINDOW.end_exclusive,
            terminal_producer_id=beta.producer_id,
        )
    assert unsorted.value.code is OutcomeCode.CONFLICTING_ID
    with pytest.raises(RuntimeOrderingError) as duplicate:
        historical_runtime._create_run_wide_dispatcher(
            clock=clock,
            producers=cast(
                tuple[historical_runtime._RuntimeRootProducer, ...],
                (alpha, alpha),
            ),
            replay_end=WINDOW.end_exclusive,
            terminal_producer_id=alpha.producer_id,
        )
    assert duplicate.value.code is OutcomeCode.CONFLICTING_ID

    at_end = TimerRoot(
        available_at=WINDOW.end_exclusive,
        kind=TimerKind.STRATEGY_TIMER,
        timer_namespace=SourceNamespace("timer.boundary"),
        timer_id=RuntimeIdentifier("timer-at-end"),
        producer_sequence=0,
    )
    boundary = _FakeRootProducer("alpha", (at_end,))
    terminal = _FakeRootProducer("z-terminal", ())
    bounded = historical_runtime._create_run_wide_dispatcher(
        clock=clock,
        producers=cast(
            tuple[historical_runtime._RuntimeRootProducer, ...],
            (boundary, terminal),
        ),
        replay_end=WINDOW.end_exclusive,
        terminal_producer_id=terminal.producer_id,
    )
    with pytest.raises(RuntimeOrderingError) as outside:
        bounded.pop()
    assert outside.value.code is OutcomeCode.OUT_OF_RANGE
    assert clock.now() == WINDOW.start_inclusive


def test_terminal_offer_is_retained_once_and_source_is_never_polled_after_ack() -> None:
    bridge = create_phase1_historical_market_source_bridge(_source(_two_rows()[0]))
    port = _CountingPort(bridge)
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=port,
    )
    market = runtime.pop()
    runtime.acknowledge(market)

    first_terminal = runtime.peek()
    second_terminal = runtime.peek()
    terminal_lease = runtime.pop()

    assert first_terminal is second_terminal is terminal_lease.root
    assert port.next_calls == 2
    runtime.acknowledge(terminal_lease)
    with pytest.raises(RuntimeOrderingError):
        runtime.pop()
    assert port.next_calls == 2


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
        replay_end=WINDOW.end_exclusive,
        terminal_producer_id=producer.producer_id,
    )
    dispatcher._state = replace(dispatcher._state, next_sequence=(1 << 64) - 1)

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
