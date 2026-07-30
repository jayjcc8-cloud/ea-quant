from __future__ import annotations

import ast
import tokenize
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ea"


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
            "ea.core.economics",
            "ea.core.execution",
            "ea.core.execution_identity",
            "ea.core.execution_messages",
            "ea.core.market_data",
            "ea.core.market_data_codec",
            "ea.core.outcomes",
            "ea.core.run",
            "ea.core.runtime",
            "ea.core.time",
            "ea.runtime.historical",
            "ea.runtime.ingress",
            "ea.runtime.queue",
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
            "ea.core.outcomes",
            "ea.core.risk",
            "ea.core.run",
            "ea.core.time",
            "ea.execution.authority",
            "ea.execution.fact_authority",
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
