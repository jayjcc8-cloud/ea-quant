from __future__ import annotations

import ast
import importlib
import tokenize
import typing
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ea"


def test_composition_package_does_not_export_mutable_frontier_capabilities() -> None:
    tree = ast.parse((SOURCE_ROOT / "composition" / "__init__.py").read_text(encoding="utf-8"))
    exports = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    )
    assert not {
        "AcknowledgedLifecycleFrontier",
        "FrontierError",
        "create_acknowledged_lifecycle_frontier",
    }.intersection(exports)
    runtime = ast.parse((SOURCE_ROOT / "runtime" / "__init__.py").read_text(encoding="utf-8"))
    runtime_exports = next(
        ast.literal_eval(node.value) for node in runtime.body if isinstance(node, ast.Assign)
    )
    assert not {name for name in runtime_exports if name.endswith("_phase1_lifecycle_coordinator")}


def test_coordinator_recovery_boundary_is_private_and_compatibly_reexported() -> None:
    coordinator_source = SOURCE_ROOT / "runtime" / "coordinator.py"
    recovery_source = SOURCE_ROOT / "runtime" / "_coordinator_recovery.py"
    assert recovery_source.is_file(), "recovery helpers require one private module"
    assert len(coordinator_source.read_text(encoding="utf-8").splitlines()) <= 2800
    assert len(recovery_source.read_text(encoding="utf-8").splitlines()) <= 1500

    runtime = ast.parse((SOURCE_ROOT / "runtime" / "__init__.py").read_text(encoding="utf-8"))
    exports = next(
        ast.literal_eval(node.value)
        for node in runtime.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    )
    assert "_coordinator_recovery" not in exports
    recovery_tree = ast.parse(recovery_source.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "ea.runtime.coordinator"
        for node in recovery_tree.body
    )

    coordinator = importlib.import_module("ea.runtime.coordinator")
    expected_names = (
        "Phase1HistoricalLifecycleCoordinator",
        "RecoveredTerminalCoordinatorEvidence",
        "create_phase1_lifecycle_coordinator",
        "recover_phase1_lifecycle_coordinator",
        "recover_phase1_terminal_evidence",
        "_require_recovery_records",
        "_recovery_record_count",
        "_recovery_record_at",
        "_recovery_record_prefix",
        "_record_document",
        "_group_recovery_records",
        "_recovered_failed_logical_key",
        "_recover_ledger_frontier",
        "_require_recovery_stage_order",
        "_require_runtime_trace",
        "_recover_dispatch",
        "_recover_authorization_frontier",
        "_recover_failing_transition",
        "_recover_pre_batch_failing_transition",
        "_is_pre_batch_failing_record",
        "_root_digest",
        "_trace_root_order_key_document",
        "_latest_active_chain_head",
        "_recovered_capture_state",
        "_recovered_completed_state",
        "_latest_completion_chain_head",
    )
    assert all(hasattr(coordinator, name) for name in expected_names)


def test_coordinator_reexported_recovery_helpers_resolve_type_hints() -> None:
    coordinator = importlib.import_module("ea.runtime.coordinator")
    moved_helper_names = (
        "_group_recovery_records",
        "_recover_ledger_frontier",
        "_require_recovery_stage_order",
        "_recover_dispatch",
        "_recover_authorization_frontier",
        "_recover_failing_transition",
        "_recover_pre_batch_failing_transition",
        "_latest_active_chain_head",
        "_recovered_capture_state",
        "_recovered_completed_state",
    )

    for helper_name in moved_helper_names:
        assert typing.get_type_hints(getattr(coordinator, helper_name)), helper_name


def test_production_source_has_no_type_ignore_comments() -> None:
    violations: list[str] = []
    for source in sorted(SOURCE_ROOT.rglob("*.py")):
        with source.open("rb") as stream:
            for token in tokenize.tokenize(stream.readline):
                if token.type == tokenize.COMMENT and token.string.startswith("# type: ignore"):
                    relative = source.relative_to(SOURCE_ROOT.parent.parent)
                    violations.append(f"{relative}:{token.start[0]}")

    assert violations == []


def test_execution_value_modules_keep_the_frozen_import_boundary() -> None:
    expected = {
        "outcomes.py": frozenset(),
        "execution_identity.py": frozenset(
            {
                "ea.core.outcomes",
                "ea.core.run",
            }
        ),
        "economics.py": frozenset({"ea.core.outcomes"}),
        "execution.py": frozenset(
            {
                "ea.core.economics",
                "ea.core.identity",
                "ea.core.run",
            }
        ),
        "execution_messages.py": frozenset(
            {
                "ea.core.economics",
                "ea.core.execution",
                "ea.core.execution_identity",
                "ea.core.identity",
                "ea.core.outcomes",
                "ea.core.run",
                "ea.core.time",
            }
        ),
        "execution_state.py": frozenset(
            {
                "ea.core.economics",
                "ea.core.execution",
                "ea.core.execution_identity",
                "ea.core.execution_messages",
                "ea.core.outcomes",
                "ea.core.run",
            }
        ),
        "runtime.py": frozenset(
            {
                "ea.core.execution_identity",
                "ea.core.execution_messages",
                "ea.core.market_data",
                "ea.core.outcomes",
                "ea.core.run",
                "ea.core.time",
            }
        ),
        "risk.py": frozenset(
            {
                "ea.core.economics",
                "ea.core.execution",
                "ea.core.execution_identity",
                "ea.core.execution_messages",
                "ea.core.identity",
                "ea.core.outcomes",
                "ea.core.run",
                "ea.core.time",
            }
        ),
        "strategy.py": frozenset(
            {
                "ea.core.execution_identity",
                "ea.core.identity",
                "ea.core.market_data",
                "ea.core.market_data_codec",
                "ea.core.outcomes",
                "ea.core.run",
                "ea.core.runtime",
                "ea.core.time",
            }
        ),
        "portfolio_planning.py": frozenset(
            {
                "ea.core.economics",
                "ea.core.execution",
                "ea.core.execution_identity",
                "ea.core.execution_messages",
                "ea.core.identity",
                "ea.core.outcomes",
                "ea.core.portfolio",
                "ea.core.run",
                "ea.core.strategy",
                "ea.core.time",
            }
        ),
    }

    for filename, allowed in expected.items():
        tree = ast.parse((SOURCE_ROOT / "core" / filename).read_text(encoding="utf-8"))
        imported_ea_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("ea.")
        }
        assert imported_ea_modules == allowed


def test_inner_runtime_package_depends_only_on_core_and_itself() -> None:
    allowed = frozenset(
        {
            "ea.core.audit",
            "ea.core.economics",
            "ea.core.execution",
            "ea.core.execution_identity",
            "ea.core.execution_messages",
            "ea.core.execution_state",
            "ea.core.historical_matching",
            "ea.core.ledger_integration",
            "ea.core.lifecycle",
            "ea.core.market_data",
            "ea.core.market_data_codec",
            "ea.core.outcomes",
            "ea.core.portfolio",
            "ea.core.risk",
            "ea.core.run",
            "ea.core.runtime",
            "ea.core.strategy",
            "ea.core.time",
            "ea.runtime.coordinator",
            "ea.runtime._coordinator_recovery",
            "ea.runtime.historical",
            "ea.runtime.ingress",
            "ea.runtime.matcher",
            "ea.runtime.queue",
            "ea.runtime.strategy",
        }
    )
    imported_ea_modules: set[str] = set()
    for source in sorted((SOURCE_ROOT / "runtime").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported_ea_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("ea.")
        )

    assert imported_ea_modules == allowed


def test_inner_strategy_package_depends_only_on_core_and_itself() -> None:
    allowed = frozenset(
        {
            "ea.core.execution_identity",
            "ea.core.market_data",
            "ea.core.market_data_codec",
            "ea.core.outcomes",
            "ea.core.run",
            "ea.core.runtime",
            "ea.core.strategy",
            "ea.strategy.authority",
        }
    )
    imported_ea_modules: set[str] = set()
    for source in sorted((SOURCE_ROOT / "strategy").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported_ea_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("ea.")
        )

    assert imported_ea_modules == allowed


def test_inner_risk_package_depends_only_on_core_and_itself() -> None:
    allowed = frozenset(
        {
            "ea.core.economics",
            "ea.core.execution",
            "ea.core.execution_identity",
            "ea.core.execution_messages",
            "ea.core.identity",
            "ea.core.outcomes",
            "ea.core.portfolio",
            "ea.core.risk",
            "ea.core.run",
            "ea.core.time",
            "ea.risk.authority",
        }
    )
    imported_ea_modules: set[str] = set()
    for source in sorted((SOURCE_ROOT / "risk").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported_ea_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("ea.")
        )

    assert imported_ea_modules == allowed


def test_inner_execution_package_depends_only_on_core_and_itself() -> None:
    allowed = frozenset(
        {
            "ea.core.economics",
            "ea.core.execution",
            "ea.core.execution_identity",
            "ea.core.execution_messages",
            "ea.core.execution_state",
            "ea.core.historical_matching",
            "ea.core.identity",
            "ea.core.market_data",
            "ea.core.market_data_codec",
            "ea.core.outcomes",
            "ea.core.risk",
            "ea.core.run",
            "ea.core.runtime",
            "ea.core.strategy",
            "ea.core.time",
            "ea.execution.authority",
            "ea.execution.fact_authority",
            "ea.execution.matcher",
        }
    )
    imported_ea_modules: set[str] = set()
    for source in sorted((SOURCE_ROOT / "execution").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported_ea_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("ea.")
        )

    assert imported_ea_modules == allowed
