from __future__ import annotations

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
