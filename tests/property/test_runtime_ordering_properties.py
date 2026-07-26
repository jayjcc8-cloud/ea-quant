from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from ea.core import (
    Adjustment,
    Bar,
    EndOfRunKind,
    EndOfRunRoot,
    Instrument,
    MarketDataEnvelope,
    RuntimeIdentifier,
    RuntimeRoot,
    SafetyKind,
    SafetyRoot,
    SourceId,
    SourceNamespace,
    TimerKind,
    TimerRoot,
    VenueId,
    prepare_bounded_runtime_roots,
    runtime_root_order_key,
)
from ea.core.run import RunId

ROOT_TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")


def _fixed_roots() -> tuple[RuntimeRoot, ...]:
    market = MarketDataEnvelope(
        payload=Bar(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            interval_start=ROOT_TIME - timedelta(minutes=1),
            interval_end=ROOT_TIME,
            adjustment=Adjustment.RAW,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
        ),
        source=SourceId("primary.raw"),
        available_at=ROOT_TIME,
        source_sequence=1,
        revision=0,
    )
    return (
        SafetyRoot(
            ROOT_TIME,
            SafetyKind.HALT,
            SourceNamespace("runtime.safety"),
            1,
        ),
        market,
        TimerRoot(
            ROOT_TIME,
            TimerKind.STRATEGY_TIMER,
            SourceNamespace("runtime.timer"),
            RuntimeIdentifier("strategy.primary"),
            1,
        ),
        EndOfRunRoot(
            ROOT_TIME,
            EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED,
            SourceNamespace("runtime.end"),
            1,
            RUN_ID,
        ),
    )


@given(st.permutations((0, 1, 2, 3)))
def test_bounded_plan_is_permutation_invariant(permutation: list[int]) -> None:
    roots = _fixed_roots()
    candidate = tuple(roots[index] for index in permutation)

    assert prepare_bounded_runtime_roots(candidate) == prepare_bounded_runtime_roots(roots)


@given(
    first=st.integers(min_value=0, max_value=10**200),
    gap=st.integers(min_value=1, max_value=10**30),
)
def test_timer_sequence_order_matches_unbounded_integer_order(first: int, gap: int) -> None:
    earlier = TimerRoot(
        ROOT_TIME,
        TimerKind.MAINTENANCE,
        SourceNamespace("runtime.timer"),
        RuntimeIdentifier("maintenance"),
        first,
    )
    later = TimerRoot(
        ROOT_TIME,
        TimerKind.MAINTENANCE,
        SourceNamespace("runtime.timer"),
        RuntimeIdentifier("maintenance"),
        first + gap,
    )

    assert runtime_root_order_key(earlier) < runtime_root_order_key(later)
    assert prepare_bounded_runtime_roots((later, earlier)).roots == (earlier, later)


@given(
    first_time=st.integers(min_value=0, max_value=1_000_000),
    second_time=st.integers(min_value=0, max_value=1_000_000),
)
def test_available_at_always_precedes_domain_rank(
    first_time: int,
    second_time: int,
) -> None:
    safety = SafetyRoot(
        ROOT_TIME + timedelta(microseconds=first_time),
        SafetyKind.HALT,
        SourceNamespace("runtime.safety"),
        1,
    )
    end = EndOfRunRoot(
        ROOT_TIME + timedelta(microseconds=second_time),
        EndOfRunKind.REQUESTED_END,
        SourceNamespace("runtime.end"),
        1,
        RUN_ID,
    )

    ordered = prepare_bounded_runtime_roots((end, safety)).roots

    if first_time <= second_time:
        assert ordered == (safety, end)
    else:
        assert ordered == (end, safety)
