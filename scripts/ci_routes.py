#!/usr/bin/env python3
"""Classify changed repository paths for the existing CI verification jobs."""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Routes:
    docs_only: bool = True
    governance: bool = False
    python: bool = False
    web: bool = False
    web_e2e: bool = False
    ci: bool = False


def installed_python_modules(source_root: Path) -> set[str] | None:
    """Find modules imported by the installed CLI and Web entrypoints; None means uncertain."""
    stack = ["ea.cli.app", "ea.web.server"]
    visited: set[str] = set()

    def module_path(module: str) -> Path | None:
        relative = Path(*module.split("."))
        candidates = (
            source_root / relative.with_suffix(".py"),
            source_root / relative / "__init__.py",
        )
        found = [path for path in candidates if path.is_file()]
        if len(found) > 1:
            raise ValueError(f"ambiguous module path: {module}")
        return found[0] if found else None

    def add(module: str) -> bool:
        parts = module.split(".")
        for length in range(1, len(parts) + 1):
            name = ".".join(parts[:length])
            if module_path(name) is None:
                return False
            stack.append(name)
        return True

    try:
        while stack:
            module = stack.pop()
            if module in visited:
                continue
            path = module_path(module)
            if path is None:
                return None
            visited.add(module)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if (alias.name == "ea" or alias.name.startswith("ea.")) and not add(
                            alias.name
                        ):
                            return None
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        parts = package.split(".")
                        if node.level > len(parts):
                            return None
                        base = ".".join(parts[: len(parts) - node.level + 1])
                        if node.module:
                            base += "." + node.module
                    else:
                        base = node.module or ""
                    if base == "ea" or base.startswith("ea."):
                        if not add(base):
                            return None
                        for alias in node.names:
                            child = base + "." + alias.name
                            if module_path(child) is not None and not add(child):
                                return None
                elif isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"__import__", "import_module"}
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    if not node.args or not isinstance(node.args[0], ast.Constant):
                        return None
                    target = node.args[0].value
                    if not isinstance(target, str):
                        return None
                    if (target == "ea" or target.startswith("ea.")) and not add(target):
                        return None
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return None
    return visited


def classify(
    paths: list[str],
    *,
    added_paths: frozenset[str] = frozenset(),
    source_root: Path = PROJECT_ROOT / "src",
    candidate: bool = False,
    release: bool = False,
) -> Routes:
    """Keep unknown paths on the conservative route, including new build inputs."""
    if not paths:
        return Routes(False, True, True, True, True, True)

    docs_only = True
    governance = False
    python = False
    web = False
    web_e2e = False
    ci = False
    installed_modules: set[str] | None = None
    for path in paths:
        if path.startswith(".github/workflows/") or path == "scripts/ci_routes.py":
            docs_only = False
            governance = python = web = web_e2e = ci = True
        elif path.startswith("apps/web/") or path in {
            "package.json",
            "package-lock.json",
            "npm-shrinkwrap.json",
        }:
            docs_only = False
            web = web_e2e = True
        elif path.startswith("src/"):
            docs_only = False
            python = True
            source_path = source_root / path.removeprefix("src/")
            if (
                path in added_paths
                and path.startswith("src/ea/")
                and path.endswith(".py")
                and source_path.is_file()
                and not source_path.is_symlink()
            ):
                if installed_modules is None:
                    installed_modules = installed_python_modules(source_root)
                module = "ea." + path.removeprefix("src/ea/").removesuffix(".py").replace("/", ".")
                if module.endswith(".__init__"):
                    module = module.removesuffix(".__init__")
                web_e2e |= installed_modules is None or module in installed_modules
            else:
                web_e2e = True
        elif path.startswith("scripts/"):
            docs_only = False
            python = web_e2e = True
        elif path.startswith("tests/"):
            docs_only = False
            python = True
            if path.startswith("tests/unit/test_web") or path.startswith("tests/unit/test_cli"):
                web_e2e = True
        elif path.startswith("examples/") or path in {
            "pyproject.toml",
            "uv.lock",
            "build-constraints.txt",
            "MANIFEST.in",
            "setup.cfg",
        }:
            docs_only = False
            python = web_e2e = True
        elif path.startswith("docs/fixtures/"):
            docs_only = False
            governance = python = True
        elif (
            path.startswith(("docs/", ".governance/", ".github/ISSUE_TEMPLATE/"))
            or path in {"README.md", "AGENTS.md", "CONTRIBUTING.md"}
            or path == ".github/PULL_REQUEST_TEMPLATE.md"
        ):
            governance = True
        else:
            docs_only = False
            governance = python = web = web_e2e = True

    if candidate:
        # The full profile already owns Python checks. Candidate frontend remains
        # path-aware; release candidates always build the frontend as a release gate.
        return Routes(False, False, False, web or release, web_e2e, False)
    return Routes(docs_only, governance, python, web, web_e2e, ci)


def parse_change_statuses(raw: bytes) -> dict[str, str]:
    """Read `git diff --no-renames --name-status -z` without guessing file status."""
    if raw and not raw.endswith(b"\0"):
        raise ValueError("expected NUL-delimited git diff statuses and paths")
    fields = [part.decode("utf-8") for part in raw.split(b"\0") if part]
    if len(fields) % 2:
        raise ValueError("expected one path per git diff status")
    return dict(zip(fields[1::2], fields[0::2], strict=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read()
    try:
        statuses = parse_change_statuses(raw)
    except (UnicodeDecodeError, ValueError) as error:
        parser.error(str(error))
    paths = list(statuses)
    if any(
        PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts for path in paths
    ):
        parser.error("changed paths must be repository relative")
    if any(status not in {"A", "M", "D"} for status in statuses.values()):
        routes = Routes(False, True, True, True, True, True)
    else:
        added_paths = frozenset(path for path, status in statuses.items() if status == "A")
        routes = classify(
            paths, added_paths=added_paths, candidate=args.candidate, release=args.release
        )
    output = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"{field}={str(getattr(routes, field)).lower()}\n" for field in Routes.__dataclass_fields__
    ]
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.writelines(lines)
    else:
        sys.stdout.writelines(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
