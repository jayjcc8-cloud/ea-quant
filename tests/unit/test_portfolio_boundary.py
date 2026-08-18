from __future__ import annotations

import ast
from pathlib import Path

import ea.portfolio
from ea.portfolio import PortfolioLedger, PortfolioPlanningAuthority

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PROJECT_ROOT / "src" / "ea" / "portfolio" / "ledger.py"


def test_portfolio_public_api_has_no_arbitrary_mutation_surface() -> None:
    assert ea.portfolio.__all__ == [
        "Phase1LedgerHandoffAuthority",
        "Phase1PortfolioRiskRefreshAuthority",
        "PortfolioLedger",
        "PortfolioPlanningAuthority",
        "create_phase1_ledger_handoff_authority",
        "create_phase1_portfolio_risk_refresh_authority",
        "create_portfolio_ledger",
        "create_portfolio_planning_authority",
    ]
    public = {name for name in dir(PortfolioLedger) if not name.startswith("_")}
    assert public == {
        "apply_fill",
        "apply_ledger_application_command",
        "apply_reconciliation_adjustment",
        "snapshot",
        "transactions",
    }
    planner_public = {name for name in dir(PortfolioPlanningAuthority) if not name.startswith("_")}
    assert planner_public == {"lookup_by_signal_id", "plan", "state"}
    assert (
        not {
            "append",
            "append_posting",
            "set_balance",
            "restore",
            "import_state",
            "reconcile",
            "fund",
        }
        & public
    )


def test_stateful_ledger_imports_only_stdlib_and_core_contracts() -> None:
    tree = ast.parse(LEDGER_PATH.read_text(encoding="utf-8"))
    imports: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)

    assert all(
        name in {"__future__", "collections.abc", "dataclasses", "types", "typing"}
        or name == "ea.core"
        or name.startswith("ea.core.")
        for name in imports
    )
    assert (
        not {
            "open",
            "connect",
            "send",
            "write",
            "dump",
            "dumps",
            "run",
            "Popen",
            "create_task",
        }
        & calls
    )


def test_portfolio_planning_imports_only_core_and_same_package_ledger() -> None:
    planning_path = PROJECT_ROOT / "src" / "ea" / "portfolio" / "planning.py"
    tree = ast.parse(planning_path.read_text(encoding="utf-8"))
    imported_ea_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("ea.")
    }
    assert imported_ea_modules == {
        "ea.core.economics",
        "ea.core.execution",
        "ea.core.execution_identity",
        "ea.core.execution_messages",
        "ea.core.identity",
        "ea.core.outcomes",
        "ea.core.portfolio",
        "ea.core.portfolio_planning",
        "ea.core.run",
        "ea.core.strategy",
        "ea.portfolio.ledger",
    }
