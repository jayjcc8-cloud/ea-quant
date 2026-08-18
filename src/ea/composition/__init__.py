"""Outer composition roots; inner runtime and policy modules never import this package."""

from ea.composition.frontier import (
    AcknowledgedLifecycleFrontier,
    FrontierError,
    create_acknowledged_lifecycle_frontier,
)
from ea.composition.lifecycle import (
    HistoricalLifecycleOrderVerifier,
    Phase1HistoricalLifecycle,
    Phase1HistoricalLifecycleCoordinatorFacade,
    create_phase1_historical_lifecycle,
    recover_phase1_historical_lifecycle,
    recover_phase1_historical_terminal_evidence,
)

__all__ = [
    "AcknowledgedLifecycleFrontier",
    "FrontierError",
    "HistoricalLifecycleOrderVerifier",
    "Phase1HistoricalLifecycle",
    "Phase1HistoricalLifecycleCoordinatorFacade",
    "create_acknowledged_lifecycle_frontier",
    "create_phase1_historical_lifecycle",
    "recover_phase1_historical_lifecycle",
    "recover_phase1_historical_terminal_evidence",
]
