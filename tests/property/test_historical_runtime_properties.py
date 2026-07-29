from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from ea.core import (
    CanonicalDecimal,
    Clock,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    MarketDataEnvelope,
    PriceDomain,
    ReplayWindow,
    RunId,
    SettlementCurrency,
    VenueId,
    build_instrument_spec_set,
)
from ea.data import (
    Phase1HistoricalMarketSourceBridge,
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.runtime import (
    HistoricalMarketCandidate,
    HistoricalMarketPreparedCommit,
    HistoricalMarketSourceBinding,
    Phase1HistoricalMarketRuntime,
    create_phase1_historical_market_runtime,
)

HEADER = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
WINDOW = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
)
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("historical-runtime-property-v1"),
    (
        InstrumentExecutionSpec(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            specification_id=InstrumentSpecId("xnas-aapl-property-v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        ),
    ),
)
RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")


def _text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _runtime(
    delays: list[int],
) -> tuple[Phase1HistoricalMarketRuntime, tuple[MarketDataEnvelope, ...]]:
    rows: list[str] = []
    for sequence, delay in enumerate(delays):
        start = WINDOW.start_inclusive + timedelta(minutes=sequence)
        end = start + timedelta(minutes=1)
        available = end + timedelta(minutes=delay)
        rows.append(
            ",".join(
                (
                    "1",
                    "XNAS",
                    "AAPL",
                    _text(start),
                    _text(end),
                    "raw",
                    "100.0",
                    "101.0",
                    "99.0",
                    "100.5",
                    "10.0",
                    "property.raw",
                    str(sequence),
                    "0",
                    _text(available),
                )
            )
        )
    dataset = decode_phase1_ohlcv_csv(
        ("\n".join((HEADER, *rows)) + "\n").encode(),
        replay_window=WINDOW,
    )
    bridge = create_phase1_historical_market_source_bridge(
        create_phase1_historical_market_data_source(dataset)
    )
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=bridge,
    )
    return runtime, dataset.selection.events


@given(
    delays=st.lists(
        st.integers(min_value=0, max_value=4),
        min_size=1,
        max_size=8,
    )
)
def test_runtime_drains_generated_history_in_canonical_order(delays: list[int]) -> None:
    runtime, expected = _runtime(delays)
    observed: list[MarketDataEnvelope] = []
    sequences: list[int] = []
    clock_values: list[datetime] = []

    for _event in expected:
        lease = runtime.pop()
        assert type(lease.root) is MarketDataEnvelope
        observed.append(lease.root)
        sequences.append(lease.dispatch_sequence)
        clock_values.append(runtime.clock.now())
        runtime.acknowledge(lease)

    terminal = runtime.pop()
    sequences.append(terminal.dispatch_sequence)
    clock_values.append(runtime.clock.now())
    runtime.acknowledge(terminal)

    assert tuple(observed) == expected
    assert sequences == list(range(1, len(expected) + 2))
    assert clock_values == sorted(clock_values)
    assert runtime.committed_event_count == len(expected)
    assert runtime.terminal_acknowledged
    assert len(runtime.trace_records) == len(expected) + 1


class _RetryingPort:
    def __init__(
        self,
        bridge: Phase1HistoricalMarketSourceBridge,
        failures: int,
    ) -> None:
        self.bridge = bridge
        self.remaining_failures = failures
        self.next_calls = 0

    @property
    def binding(self) -> HistoricalMarketSourceBinding:
        return self.bridge.binding

    def next_available_at(self) -> datetime | None:
        self.next_calls += 1
        return self.bridge.next_available_at()

    def admit_one(self, *, clock: Clock) -> HistoricalMarketCandidate:
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise LookupError("retryable property failure")
        return self.bridge.admit_one(clock=clock)

    def prepare_commit(
        self,
        candidate: HistoricalMarketCandidate,
    ) -> HistoricalMarketPreparedCommit:
        return self.bridge.prepare_commit(candidate)

    def commit(self, prepared: HistoricalMarketPreparedCommit) -> None:
        self.bridge.commit(prepared)


@given(failures=st.integers(min_value=0, max_value=5))
def test_admission_retries_never_revise_the_binding_time_promise(failures: int) -> None:
    content = (
        HEADER
        + "\n"
        + ",".join(
            (
                "1",
                "XNAS",
                "AAPL",
                "2026-01-02T09:00:00.000000Z",
                "2026-01-02T09:01:00.000000Z",
                "raw",
                "100.0",
                "101.0",
                "99.0",
                "100.5",
                "10.0",
                "property.raw",
                "0",
                "0",
                "2026-01-02T09:01:00.000000Z",
            )
        )
        + "\n"
    )
    dataset = decode_phase1_ohlcv_csv(content.encode(), replay_window=WINDOW)
    bridge = create_phase1_historical_market_source_bridge(
        create_phase1_historical_market_data_source(dataset)
    )
    port = _RetryingPort(bridge, failures)
    runtime = create_phase1_historical_market_runtime(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        source=port,
    )

    for _attempt in range(failures):
        try:
            runtime.pop()
        except LookupError:
            pass
        else:
            raise AssertionError("configured admission failure did not propagate")

    lease = runtime.pop()

    assert lease.root is dataset.selection.events[0]
    assert port.next_calls == 1
