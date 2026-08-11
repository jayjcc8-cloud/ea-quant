"""Outer composition roots; inner runtime and policy modules never import this package."""

from ea.composition.lifecycle import (
    HistoricalLifecycleOrderVerifier,
    Phase1HistoricalLifecycle,
    Phase1HistoricalLifecycleCoordinatorFacade,
    create_phase1_historical_lifecycle,
    recover_phase1_historical_lifecycle,
    recover_phase1_historical_terminal_evidence,
)

__all__ = [
    "HistoricalLifecycleOrderVerifier",
    "Phase1HistoricalLifecycle",
    "Phase1HistoricalLifecycleCoordinatorFacade",
    "create_phase1_historical_lifecycle",
    "recover_phase1_historical_lifecycle",
    "recover_phase1_historical_terminal_evidence",
]
