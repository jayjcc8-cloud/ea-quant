"""Resume-feed integration: reconstruct a crashed run and continue it exactly once."""

from __future__ import annotations

from pathlib import Path

from ea.product.paper import restore_paper_trading_session
from unit.test_paper_runtime import drive, session
from unit.test_streaming import SOURCE


def test_resume_feed_continues_without_duplicate_effects(tmp_path: Path) -> None:
    # Reference: a full uninterrupted run settles six Fills to 986.8 cash.
    reference, reference_clock = session(tmp_path)
    drive(reference, reference_clock)
    assert reference.status()["cash"] == "986.8"
    assert reference.status()["fills"] == 6

    # Crash a partial run part-way: some Fills retained, one Order still open.
    engine, clock = session(tmp_path)
    drive(engine, clock, count=6)
    records = tuple(engine.audit.records)
    partial_fills = len(engine.committed)
    assert 0 < partial_fills < 6

    # Reconstruct a fresh session from the journal, adopting the acknowledged
    # economic and outbound state without replaying a single Fill twice.
    restored = restore_paper_trading_session(
        engine.scenario,
        binding=engine.binding,
        audit=engine.audit,
        records=records,
        clock=clock,
        monotonic=clock.monotonic,
        source_id=SOURCE,
        prices=(100.0,),
        stop_requested=lambda: False,
    )
    assert len(restored.committed) == partial_fills
    assert len(restored.facts.fills) == partial_fills

    # Continue the feed: the open Order fills once, the remaining round trips
    # issue fresh Signals/Intents, and no already-filled Order re-matches.
    drive(restored, clock)

    status = restored.status()
    assert status["fills"] == 6
    assert status["cash"] == "986.8"
    assert status["equity"] == "986.8"
    assert status["position"] == "0"
    assert restored.reconcile() == "match"
