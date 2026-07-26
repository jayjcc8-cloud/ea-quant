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
