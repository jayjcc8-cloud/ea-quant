"""Runnable offline product compositions."""

from ea.product.identity import (
    BacktestIdentityError,
    BacktestLineageInputs,
    BacktestRandomness,
    RandomnessProfile,
    build_backtest_lineage,
    canonical_backtest_lineage_bytes,
    semantic_outcome_sha256,
)
from ea.product.offline_demo import (
    DemoMode,
    OfflineDemoFailure,
    OfflineDemoInputError,
    OfflineDemoResult,
    run_offline_demo,
)

__all__ = [
    "BacktestIdentityError",
    "BacktestLineageInputs",
    "BacktestRandomness",
    "DemoMode",
    "OfflineDemoFailure",
    "OfflineDemoInputError",
    "OfflineDemoResult",
    "RandomnessProfile",
    "build_backtest_lineage",
    "canonical_backtest_lineage_bytes",
    "run_offline_demo",
    "semantic_outcome_sha256",
]
