"""Runnable offline product compositions."""

from ea.product.backtest import (
    BacktestResumeFailure,
    BacktestRunError,
    BacktestRunFailure,
    BacktestRunResult,
    resume_backtest_attempt,
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
from ea.product.reporting import (
    BACKTEST_REPORT_FIELD_SOURCES,
    BacktestReportError,
    BacktestReportResult,
    BacktestReportV1,
    generate_backtest_report,
)
from ea.product.scenario import (
    BacktestScenarioError,
    BacktestStrategyId,
    LoadedBacktestScenario,
    load_backtest_scenario,
    parameterize_backtest_scenario,
)

__all__ = [
    "BACKTEST_REPORT_FIELD_SOURCES",
    "BacktestIdentityError",
    "BacktestLineageInputs",
    "BacktestRandomness",
    "BacktestResumeFailure",
    "BacktestReportError",
    "BacktestReportResult",
    "BacktestReportV1",
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
    "generate_backtest_report",
    "load_backtest_scenario",
    "parameterize_backtest_scenario",
    "resume_backtest_attempt",
    "run_offline_demo",
    "run_backtest_scenario",
    "semantic_outcome_sha256",
]
