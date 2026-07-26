from __future__ import annotations

import ast
from pathlib import Path

import ea.portfolio
from ea.portfolio import PortfolioLedger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PROJECT_ROOT / "src" / "ea" / "portfolio" / "ledger.py"


def test_portfolio_public_api_has_no_arbitrary_mutation_surface() -> None:
    assert ea.portfolio.__all__ == ["PortfolioLedger", "create_portfolio_ledger"]
    public = {name for name in dir(PortfolioLedger) if not name.startswith("_")}
    assert public == {"apply_fill", "snapshot", "transactions"}
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
        name == "__future__" or name == "typing" or name == "ea.core" or name.startswith("ea.core.")
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
