"""Runnable offline product compositions."""

from ea.product.backtest import (
    BacktestRunError,
    BacktestRunFailure,
    BacktestRunResult,
    run_backtest_scenario,
)
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
from ea.product.scenario import (
    BacktestScenarioError,
    BacktestStrategyId,
    LoadedBacktestScenario,
    load_backtest_scenario,
)

__all__ = [
    "BacktestIdentityError",
    "BacktestLineageInputs",
    "BacktestRandomness",
    "BacktestRunError",
    "BacktestRunFailure",
    "BacktestRunResult",
    "BacktestScenarioError",
    "BacktestStrategyId",
    "DemoMode",
    "LoadedBacktestScenario",
    "OfflineDemoFailure",
    "OfflineDemoInputError",
    "OfflineDemoResult",
    "RandomnessProfile",
    "build_backtest_lineage",
    "canonical_backtest_lineage_bytes",
    "load_backtest_scenario",
    "run_offline_demo",
    "run_backtest_scenario",
    "semantic_outcome_sha256",
]
