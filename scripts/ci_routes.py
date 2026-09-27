#!/usr/bin/env python3
"""Classify changed repository paths for the existing CI verification jobs."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Routes:
    docs_only: bool = True
    governance: bool = False
    python: bool = False
    web: bool = False
    web_e2e: bool = False
    ci: bool = False


def classify(paths: list[str], *, candidate: bool = False, release: bool = False) -> Routes:
    """Keep unknown paths on the conservative route, including new build inputs."""
    if not paths:
        return Routes(False, True, True, True, True, True)

    docs_only = True
    governance = False
    python = False
    web = False
    web_e2e = False
    ci = False
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
        elif path.startswith(("src/", "scripts/")):
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read()
    if raw and not raw.endswith(b"\0"):
        parser.error("expected NUL-delimited git diff paths")
    try:
        paths = [part.decode("utf-8") for part in raw.split(b"\0") if part]
    except UnicodeDecodeError as error:
        parser.error(f"invalid UTF-8 path: {error}")
    if any(
        PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts for path in paths
    ):
        parser.error("changed paths must be repository relative")
    routes = classify(paths, candidate=args.candidate, release=args.release)
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
