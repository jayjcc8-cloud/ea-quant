"""Outer composition roots; inner runtime and policy modules never import this package."""

from ea.composition.lifecycle import (
    HistoricalLifecycleOrderVerifier,
    Phase1HistoricalLifecycle,
    create_phase1_historical_lifecycle,
)

__all__ = [
    "HistoricalLifecycleOrderVerifier",
    "Phase1HistoricalLifecycle",
    "create_phase1_historical_lifecycle",
]
