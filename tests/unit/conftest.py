from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def worker_hook(tmp_path: Path) -> Callable[[str, str], str]:
    """Write one source text as an importable hook module and name it.

    The research worker runs as an isolated child process, so a test cannot
    monkeypatch inside it. ``WebService._test_hook`` names a file the worker
    loads as ``<path>:hook`` immediately before the engine reads market data;
    this helper writes that file and returns the reference.
    """

    def write(name: str, source: str) -> str:
        path = tmp_path / f"{name}-hook.py"
        path.write_text(source, encoding="utf-8")
        return f"{path}:hook"

    return write


@pytest.fixture
def isolated_ea_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove caller configuration and run default-value tests outside the repository."""
    for name in tuple(os.environ):
        if name.upper().startswith("EA_"):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.chdir(tmp_path)
