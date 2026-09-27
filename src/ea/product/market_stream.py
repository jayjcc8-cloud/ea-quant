"""Finite local simulated market source and process lifetime for streaming."""

from __future__ import annotations

import argparse
import json
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import final
from uuid import uuid4

from ea.core.execution import InstrumentExecutionSpecSet, InstrumentSpecSetId
from ea.core.execution_messages import ExecutionFactIngress
from ea.core.identity import Instrument, VenueId
from ea.core.market_data import Adjustment, Bar, MarketDataEnvelope, SourceId
from ea.core.run import RunId
from ea.core.time import Clock, require_utc
from ea.runtime.streaming import (
    StreamingMarketRuntime,
    StreamingRuntimeError,
    StreamPhase,
    StreamStatus,
)


@final
class RealUtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@final
class LocalSimulatedMarketSource:
    """Yield one current closed synthetic Bar per poll, then signal exhaustion."""

    def __init__(
        self,
        *,
        instrument: Instrument,
        source_id: SourceId,
        prices: tuple[float, ...],
        repeats: int | None,
        bar_seconds: float,
        event_limit: int | None = None,
    ) -> None:
        if type(instrument) is not Instrument or type(source_id) is not SourceId:
            raise ValueError("local source identities must be exact")
        if (
            type(prices) is not tuple
            or not prices
            or any(
                type(price) is not float or not isfinite(price) or price <= 0 for price in prices
            )
        ):
            raise ValueError("prices must be a nonempty tuple of positive finite floats")
        if repeats is not None and (type(repeats) is not int or repeats < 1):
            raise ValueError("repeats must be a positive int or None")
        if type(bar_seconds) is not float or not isfinite(bar_seconds) or bar_seconds <= 0:
            raise ValueError("bar_seconds must be a positive finite float")
        if event_limit is not None and (type(event_limit) is not int or event_limit < 1):
            raise ValueError("event_limit must be a positive int")
        self.instrument = instrument
        self.source_id = source_id
        self._prices = prices
        self._limit = None if repeats is None else len(prices) * repeats
        if event_limit is not None:
            self._limit = event_limit if self._limit is None else min(event_limit, self._limit)
        self._bar_seconds = bar_seconds
        self._issued = 0
        self._last_time: datetime | None = None
        self.closed = False

    def next_event(self, now: datetime) -> MarketDataEnvelope | None:
        if self.closed:
            raise ValueError("local source is closed")
        if self._limit is not None and self._issued >= self._limit:
            return None
        end = require_utc(now, field="local source now")
        if self._last_time is not None and end <= self._last_time:
            raise ValueError("local source poll clock must advance")
        price = self._prices[self._issued % len(self._prices)]
        event = MarketDataEnvelope(
            payload=Bar(
                instrument=self.instrument,
                interval_start=end - timedelta(seconds=self._bar_seconds),
                interval_end=end,
                adjustment=Adjustment.RAW,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1.0,
            ),
            source=self.source_id,
            available_at=end,
            source_sequence=self._issued + 1,
            revision=0,
        )
        self._issued += 1
        self._last_time = end
        return event

    def close(self) -> None:
        self.closed = True


def run_local_market_stream(
    *,
    source: LocalSimulatedMarketSource,
    runtime: StreamingMarketRuntime,
    clock: Clock,
    sleep: Callable[[float], None],
    poll_interval_seconds: float,
    on_market: Callable[[MarketDataEnvelope, int], None],
    on_fact: Callable[[ExecutionFactIngress, int], None],
    on_heartbeat: Callable[[StreamStatus], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    max_drain_polls: int = 100,
) -> StreamStatus:
    """Run one finite source to exhaustion/stop, always releasing it."""
    if source.source_id != runtime.source_id:
        raise ValueError("source and runtime identities conflict")
    if (
        type(poll_interval_seconds) is not float
        or not isfinite(poll_interval_seconds)
        or poll_interval_seconds <= 0
    ):
        raise ValueError("poll interval must be a positive finite float")
    if type(max_drain_polls) is not int or max_drain_polls < 1:
        raise ValueError("max_drain_polls must be a positive int")

    drain_polls = 0

    def drain_facts() -> None:
        nonlocal drain_polls
        while runtime.status().pending_facts:
            if drain_polls >= max_drain_polls:
                raise StreamingRuntimeError("fact drain deadline exceeded")
            drain_polls += 1
            if not runtime.poll(on_market=on_market, on_fact=on_fact):
                sleep(poll_interval_seconds)

    try:
        while runtime.phase is StreamPhase.RUNNING:
            if stop_requested is not None and stop_requested():
                runtime.request_stop()
                break
            state = runtime.heartbeat()
            if on_heartbeat is not None:
                try:
                    on_heartbeat(state)
                except Exception:
                    runtime.fail_heartbeat_callback()
                    raise
            if runtime.phase is not StreamPhase.RUNNING:
                break
            try:
                event = source.next_event(clock.now())
            except Exception:
                runtime.fail_source()
                raise
            if event is None:
                runtime.mark_source_exhausted()
                break
            runtime.receive_market(event)
            while runtime.poll(on_market=on_market, on_fact=on_fact):
                pass
            sleep(poll_interval_seconds)
        drain_facts()
        runtime.close()
        return runtime.status()
    except BaseException:
        if runtime.phase is StreamPhase.RUNNING:
            runtime.fail_source()
        if runtime.status().failed_dispatch_kind != "fact":
            drain_facts()
            if not runtime.status().pending_facts:
                runtime.close()
        raise
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finite local simulated market stream")
    parser.add_argument("--events", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args(argv)
    if args.events < 1 or args.interval <= 0:
        parser.error("events and interval must be positive")
    clock = RealUtcClock()
    source_id = SourceId("local.simulated")
    source = LocalSimulatedMarketSource(
        instrument=Instrument(VenueId("SIM"), "PAPER"),
        source_id=source_id,
        prices=(100.0, 101.0),
        repeats=(args.events + 1) // 2,
        bar_seconds=args.interval,
        event_limit=args.events,
    )
    runtime = StreamingMarketRuntime(
        run_id=RunId(str(uuid4())),
        spec_set=InstrumentExecutionSpecSet(InstrumentSpecSetId("local.stream"), ()),
        source_id=source_id,
        clock=clock,
        monotonic=time.monotonic,
        max_market_age_seconds=5.0,
        stall_timeout_seconds=5.0,
    )
    stopping = False

    def handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    prior_int = signal.signal(signal.SIGINT, handle_stop)
    prior_term = signal.signal(signal.SIGTERM, handle_stop)
    try:
        state = run_local_market_stream(
            source=source,
            runtime=runtime,
            clock=clock,
            sleep=time.sleep,
            poll_interval_seconds=args.interval,
            on_market=lambda event, sequence: print(
                json.dumps(
                    {
                        "kind": "market",
                        "sequence": sequence,
                        "source_sequence": event.source_sequence,
                    }
                ),
                flush=True,
            ),
            on_fact=lambda _ingress, _sequence: None,
            on_heartbeat=lambda status: print(
                json.dumps(
                    {
                        "kind": "heartbeat",
                        "count": status.heartbeat_count,
                    }
                ),
                flush=True,
            ),
            stop_requested=lambda: stopping,
        )
    finally:
        signal.signal(signal.SIGINT, prior_int)
        signal.signal(signal.SIGTERM, prior_term)
    print(
        json.dumps(
            {
                "kind": "exit",
                "phase": state.phase.value,
                "reason": None if state.reason is None else state.reason.value,
                "markets": state.market_dispatches,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
