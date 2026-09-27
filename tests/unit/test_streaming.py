from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ea.core.execution import InstrumentExecutionSpecSet, InstrumentSpecSetId
from ea.core.execution_messages import ExecutionFactIngress
from ea.core.identity import Instrument, VenueId
from ea.core.market_data import Adjustment, Bar, MarketDataEnvelope, SourceId
from ea.core.run import RunId
from ea.core.strategy import SignalDirection, StrategyContractError
from ea.execution import ExecutionFactAuthorityError, create_phase1_execution_fact_authority
from ea.execution.order_lifecycle import OrderCommandTracker
from ea.execution.paper_broker import PaperBroker
from ea.product.market_stream import LocalSimulatedMarketSource, run_local_market_stream
from ea.runtime.streaming import (
    StreamingMarketRuntime,
    StreamingRuntimeError,
    StreamPhase,
    StreamStopReason,
)
from ea.strategy import create_strategy_signal_authority
from unit.test_execution_fact_authority import (
    INSTRUMENT as ORDER_INSTRUMENT,
)
from unit.test_execution_fact_authority import (
    RUN_ID as ORDER_RUN_ID,
)
from unit.test_execution_fact_authority import (
    SOURCE as FACT_SOURCE,
)
from unit.test_execution_fact_authority import (
    TIME as ORDER_TIME,
)
from unit.test_execution_fact_authority import (
    _orders,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
RUN_ID = RunId("11111111-1111-4111-8111-111111111111")
SPEC_SET = InstrumentExecutionSpecSet(InstrumentSpecSetId("stream.test"), ())
SOURCE = SourceId("stream.test")
INSTRUMENT = Instrument(VenueId("SIM"), "TEST")


class ControlledClock:
    def __init__(self, start: datetime = START) -> None:
        self.utc = start
        self.mono = 0.0

    def now(self) -> datetime:
        return self.utc

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


def event(sequence: int, seconds: int, *, available_delay: int = 0) -> MarketDataEnvelope:
    end = START + timedelta(seconds=seconds)
    return MarketDataEnvelope(
        Bar(
            INSTRUMENT,
            end - timedelta(seconds=1),
            end,
            Adjustment.RAW,
            100.0,
            100.0,
            100.0,
            100.0,
            1.0,
        ),
        SOURCE,
        end + timedelta(seconds=available_delay),
        sequence,
        0,
    )


def runtime(clock: ControlledClock) -> StreamingMarketRuntime:
    return StreamingMarketRuntime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source_id=SOURCE,
        clock=clock,
        monotonic=clock.monotonic,
        max_market_age_seconds=5.0,
        stall_timeout_seconds=10.0,
    )


def test_future_market_waits_and_active_proof_only_inside_dispatch() -> None:
    clock = ControlledClock()
    stream = runtime(clock)
    authority = create_strategy_signal_authority(run_id=RUN_ID, verifier=stream)
    root = event(1, 2)
    stream.receive_market(root)
    seen: list[int] = []

    def on_market(market: MarketDataEnvelope, sequence: int) -> None:
        signal = authority.issue(market, direction=SignalDirection.LONG, dispatch_sequence=sequence)
        assert signal.dispatch_sequence == sequence
        seen.append(sequence)

    assert stream.poll(on_market=on_market, on_fact=lambda *_: None) is False
    clock.advance(2.0)
    assert stream.poll(on_market=on_market, on_fact=lambda *_: None) is True
    assert seen == [1]
    with pytest.raises(StrategyContractError):
        stream.verify_active_market_dispatch(root, dispatch_sequence=1)
    assert stream.heartbeat().dispatch_sequence == 1


def test_duplicate_conflict_order_and_pending_capacity_rejected() -> None:
    clock = ControlledClock()
    stream = runtime(clock)
    first = event(1, 1)
    stream.receive_market(first)
    with pytest.raises(StreamingRuntimeError, match="pending"):
        stream.receive_market(event(2, 2))
    clock.advance(1.0)
    assert stream.poll(on_market=lambda *_: None, on_fact=lambda *_: None)
    with pytest.raises(StreamingRuntimeError, match="duplicate"):
        stream.receive_market(first)
    with pytest.raises(StreamingRuntimeError, match="conflicting"):
        stream.receive_market(event(1, 2))
    with pytest.raises(StreamingRuntimeError, match="out-of-order"):
        stream.receive_market(event(0, 0))


def test_non_adjacent_record_replay_and_old_revision_reject_without_history() -> None:
    clock = ControlledClock()
    stream = runtime(clock)
    first = event(1, 1)
    second = event(2, 2)
    seen: list[MarketDataEnvelope] = []
    for root in (first, second):
        clock.advance(1.0)
        stream.receive_market(root)
        assert stream.poll(on_market=lambda item, _: seen.append(item), on_fact=lambda *_: None)
    replay = replace(first, source_sequence=3, available_at=START + timedelta(seconds=3))
    with pytest.raises(StreamingRuntimeError, match="event time"):
        stream.receive_market(replay)
    revision = replace(replay, revision=1)
    with pytest.raises(StreamingRuntimeError, match="event time"):
        stream.receive_market(revision)
    assert seen == [first, second]
    assert not stream.status().pending_market


def test_stall_and_stale_market_fail_closed_without_heartbeat_refresh() -> None:
    clock = ControlledClock()
    stream = runtime(clock)
    clock.advance(11.0)
    assert stream.heartbeat().reason is StreamStopReason.SOURCE_STALLED
    assert stream.phase is StreamPhase.DRAINING
    with pytest.raises(StreamingRuntimeError):
        stream.receive_market(event(1, 11))
    stream.close()
    assert stream.status().phase is StreamPhase.CLOSED

    other_clock = ControlledClock()
    stale = runtime(other_clock)
    stale.receive_market(event(1, 1))
    other_clock.advance(7.0)
    assert not stale.poll(
        on_market=lambda *_: pytest.fail("stale event dispatched"),
        on_fact=lambda *_: None,
    )
    assert stale.reason is StreamStopReason.STALE_MARKET

    delayed_clock = ControlledClock()
    delayed = runtime(delayed_clock)
    delayed.receive_market(event(1, 1, available_delay=9))
    delayed_clock.advance(10.0)
    assert not delayed.poll(
        on_market=lambda *_: pytest.fail("old Bar dispatched"), on_fact=lambda *_: None
    )
    assert delayed.reason is StreamStopReason.STALE_MARKET


def test_stop_exhaustion_and_callback_failure_cut_off_market() -> None:
    clock = ControlledClock()
    stream = runtime(clock)
    stream.receive_market(event(1, 1))
    stream.request_stop()
    clock.advance(1.0)
    assert not stream.poll(
        on_market=lambda *_: pytest.fail("market dispatched"),
        on_fact=lambda *_: None,
    )
    assert stream.reason is StreamStopReason.STOP_REQUESTED
    stream.close()
    assert not stream.poll(on_market=lambda *_: None, on_fact=lambda *_: None)

    failed = runtime(ControlledClock())
    failed.receive_market(event(1, 0))

    def callback_failure(_root: MarketDataEnvelope, _sequence: int) -> None:
        raise ZeroDivisionError("callback failed")

    with pytest.raises(ZeroDivisionError):
        failed.poll(on_market=callback_failure, on_fact=lambda *_: None)
    assert failed.reason is StreamStopReason.CALLBACK_ERROR
    failed.mark_source_exhausted()
    assert failed.reason is StreamStopReason.CALLBACK_ERROR


def test_issued_fact_drains_after_stop_with_global_sequence() -> None:
    clock = ControlledClock(ORDER_TIME)
    spec_set, issued, (order,) = _orders()
    broker = PaperBroker(issued, source_namespace=FACT_SOURCE)
    tracker = OrderCommandTracker(issued)
    command = tracker.begin_submit(order)
    assert command is not None
    stream = StreamingMarketRuntime(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        source_id=SOURCE,
        clock=clock,
        monotonic=clock.monotonic,
        max_market_age_seconds=5.0,
        stall_timeout_seconds=10.0,
        fact_source=broker,
        max_pending_facts=1,
    )
    authority = create_phase1_execution_fact_authority(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        order_verifier=issued,
        dispatch_verifier=stream,
    )
    market = MarketDataEnvelope(
        Bar(
            ORDER_INSTRUMENT,
            ORDER_TIME - timedelta(seconds=1),
            ORDER_TIME,
            Adjustment.RAW,
            100.0,
            100.0,
            100.0,
            100.0,
            1.0,
        ),
        SOURCE,
        ORDER_TIME,
        1,
        0,
    )
    sequences: list[int] = []

    def on_market(root: MarketDataEnvelope, sequence: int) -> None:
        assert (
            stream.verify_active_market_dispatch(root, dispatch_sequence=sequence).dispatch_sequence
            == 1
        )
        result = broker.submit(command, submitted_at=ORDER_TIME)
        stream.enqueue_fact(result.ingresses[0])
        stream.request_stop()
        sequences.append(sequence)

    def on_fact(ingress: ExecutionFactIngress, sequence: int) -> None:
        assert (
            stream.resolve_active_issued_fact_dispatch(
                ingress_identity=ingress.identity,
                canonical_ingress_bytes=b"forged",
                canonical_fact_bytes=b"forged",
            )
            is None
        )
        authority.process_ingress(ingress)
        sequences.append(sequence)

    stream.receive_market(market)
    assert stream.poll(on_market=on_market, on_fact=on_fact)
    assert stream.phase is StreamPhase.DRAINING
    assert stream.poll(on_market=on_market, on_fact=on_fact)
    assert sequences == [1, 2]
    assert stream.status().fact_dispatches == 1
    stream.close()


def test_local_source_exhausts_and_closes_under_controlled_clock() -> None:
    clock = ControlledClock()
    source = LocalSimulatedMarketSource(
        instrument=INSTRUMENT,
        source_id=SOURCE,
        prices=(100.0, 101.0),
        repeats=1,
        bar_seconds=1.0,
    )
    stream = runtime(clock)
    seen: list[float] = []

    def advance(seconds: float) -> None:
        clock.advance(seconds)

    status = run_local_market_stream(
        source=source,
        runtime=stream,
        clock=clock,
        sleep=advance,
        poll_interval_seconds=1.0,
        on_market=lambda root, _sequence: seen.append(root.payload.close),
        on_fact=lambda *_: None,
    )
    assert seen == [100.0, 101.0]
    assert status.phase is StreamPhase.CLOSED
    assert status.reason is StreamStopReason.SOURCE_EXHAUSTED
    assert status.heartbeat_count >= 3
    assert source.closed


def test_unbounded_local_cycle_stops_and_releases_source() -> None:
    clock = ControlledClock()
    source = LocalSimulatedMarketSource(
        instrument=INSTRUMENT,
        source_id=SOURCE,
        prices=(100.0, 101.0),
        repeats=None,
        bar_seconds=1.0,
    )
    stream = runtime(clock)
    seen: list[int] = []
    status = run_local_market_stream(
        source=source,
        runtime=stream,
        clock=clock,
        sleep=clock.advance,
        poll_interval_seconds=1.0,
        on_market=lambda _root, sequence: seen.append(sequence),
        on_fact=lambda *_: None,
        stop_requested=lambda: len(seen) == 3,
    )
    assert seen == [1, 2, 3]
    assert status.reason is StreamStopReason.STOP_REQUESTED
    assert source.closed


def test_market_callback_error_drains_already_issued_fact_then_closes_source() -> None:
    clock = ControlledClock(ORDER_TIME)
    spec_set, issued, (order,) = _orders()
    broker = PaperBroker(issued, source_namespace=FACT_SOURCE)
    command = OrderCommandTracker(issued).begin_submit(order)
    assert command is not None
    source = LocalSimulatedMarketSource(
        instrument=ORDER_INSTRUMENT,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=1,
        bar_seconds=1.0,
    )
    stream = StreamingMarketRuntime(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        source_id=SOURCE,
        clock=clock,
        monotonic=clock.monotonic,
        max_market_age_seconds=5.0,
        stall_timeout_seconds=10.0,
        fact_source=broker,
    )
    authority = create_phase1_execution_fact_authority(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        order_verifier=issued,
        dispatch_verifier=stream,
    )
    fact_sequences: list[int] = []

    def break_after_issuance(_root: MarketDataEnvelope, _sequence: int) -> None:
        result = broker.submit(command, submitted_at=ORDER_TIME)
        stream.enqueue_fact(result.ingresses[0])
        raise RuntimeError("strategy callback failed")

    def on_fact(ingress: ExecutionFactIngress, sequence: int) -> None:
        authority.process_ingress(ingress)
        fact_sequences.append(sequence)

    with pytest.raises(RuntimeError, match="strategy callback failed"):
        run_local_market_stream(
            source=source,
            runtime=stream,
            clock=clock,
            sleep=clock.advance,
            poll_interval_seconds=1.0,
            on_market=break_after_issuance,
            on_fact=on_fact,
        )
    assert fact_sequences == [2]
    assert stream.status().pending_facts == 0
    assert stream.status().failed_dispatch_kind == "market"
    assert stream.reason is StreamStopReason.CALLBACK_ERROR
    assert stream.phase is StreamPhase.CLOSED
    assert source.closed


@pytest.mark.parametrize("error_type", [RuntimeError, SystemExit, KeyboardInterrupt])
def test_fact_callback_error_is_not_retried_or_reported_clean(
    error_type: type[BaseException],
) -> None:
    clock = ControlledClock(ORDER_TIME)
    spec_set, issued, (order,) = _orders()
    broker = PaperBroker(issued, source_namespace=FACT_SOURCE)
    command = OrderCommandTracker(issued).begin_submit(order)
    assert command is not None
    source = LocalSimulatedMarketSource(
        instrument=ORDER_INSTRUMENT,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=1,
        bar_seconds=1.0,
    )
    stream = StreamingMarketRuntime(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        source_id=SOURCE,
        clock=clock,
        monotonic=clock.monotonic,
        max_market_age_seconds=5.0,
        stall_timeout_seconds=10.0,
        fact_source=broker,
    )
    attempts: list[int] = []

    def on_market(_root: MarketDataEnvelope, _sequence: int) -> None:
        result = broker.submit(command, submitted_at=ORDER_TIME)
        stream.enqueue_fact(result.ingresses[0])

    def on_fact(_ingress: ExecutionFactIngress, sequence: int) -> None:
        attempts.append(sequence)
        if len(attempts) == 1:
            raise error_type("fact consumer failed")

    with pytest.raises(error_type, match="fact consumer failed"):
        run_local_market_stream(
            source=source,
            runtime=stream,
            clock=clock,
            sleep=clock.advance,
            poll_interval_seconds=1.0,
            on_market=on_market,
            on_fact=on_fact,
        )
    assert attempts == [2]
    assert stream.phase is StreamPhase.DRAINING
    assert stream.status().pending_facts == 1
    assert stream.status().failed_dispatch_kind == "fact"
    assert source.closed


def test_future_issued_fact_waits_for_visibility_and_active_dispatch() -> None:
    clock = ControlledClock(ORDER_TIME)
    spec_set, issued, (order,) = _orders()
    broker = PaperBroker(issued, source_namespace=FACT_SOURCE)
    command = OrderCommandTracker(issued).begin_submit(order)
    assert command is not None
    stream = StreamingMarketRuntime(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        source_id=SOURCE,
        clock=clock,
        monotonic=clock.monotonic,
        max_market_age_seconds=5.0,
        stall_timeout_seconds=10.0,
        fact_source=broker,
    )
    authority = create_phase1_execution_fact_authority(
        run_id=ORDER_RUN_ID,
        spec_set=spec_set,
        order_verifier=issued,
        dispatch_verifier=stream,
    )
    ingress = broker.submit(command, submitted_at=ORDER_TIME + timedelta(seconds=2)).ingresses[0]
    stream.enqueue_fact(ingress)
    with pytest.raises(ExecutionFactAuthorityError):
        authority.process_ingress(ingress)
    assert not stream.poll(on_market=lambda *_: None, on_fact=lambda *_: None)
    clock.advance(2.0)
    seen: list[int] = []

    def consume_fact(root: ExecutionFactIngress, sequence: int) -> None:
        authority.process_ingress(root)
        seen.append(sequence)

    assert stream.poll(
        on_market=lambda *_: pytest.fail("market dispatch occurred"),
        on_fact=consume_fact,
    )
    assert seen == [1]
    assert (
        stream.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=b"forged",
            canonical_fact_bytes=b"forged",
        )
        is None
    )


def test_outer_heartbeat_failure_keeps_reason_and_releases_source() -> None:
    clock = ControlledClock()
    source = LocalSimulatedMarketSource(
        instrument=INSTRUMENT,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=1,
        bar_seconds=1.0,
    )
    stream = runtime(clock)

    def fail_heartbeat(_status: object) -> None:
        raise RuntimeError("heartbeat observer failed")

    with pytest.raises(RuntimeError, match="heartbeat observer failed"):
        run_local_market_stream(
            source=source,
            runtime=stream,
            clock=clock,
            sleep=clock.advance,
            poll_interval_seconds=1.0,
            on_market=lambda *_: pytest.fail("market dispatched"),
            on_fact=lambda *_: None,
            on_heartbeat=fail_heartbeat,
        )
    assert stream.reason is StreamStopReason.CALLBACK_ERROR
    assert stream.status().failed_dispatch_kind == "heartbeat"
    assert source.closed


def test_outer_source_failure_keeps_reason_and_releases_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ControlledClock()
    source = LocalSimulatedMarketSource(
        instrument=INSTRUMENT,
        source_id=SOURCE,
        prices=(100.0,),
        repeats=1,
        bar_seconds=1.0,
    )
    stream = runtime(clock)

    def fail_source(_now: datetime) -> MarketDataEnvelope | None:
        raise RuntimeError("market source failed")

    monkeypatch.setattr(source, "next_event", fail_source)
    with pytest.raises(RuntimeError, match="market source failed"):
        run_local_market_stream(
            source=source,
            runtime=stream,
            clock=clock,
            sleep=clock.advance,
            poll_interval_seconds=1.0,
            on_market=lambda *_: pytest.fail("market dispatched"),
            on_fact=lambda *_: None,
        )
    assert stream.reason is StreamStopReason.SOURCE_ERROR
    assert source.closed
