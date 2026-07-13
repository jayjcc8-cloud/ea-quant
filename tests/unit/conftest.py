from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_ea_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove caller configuration and run default-value tests outside the repository."""
    for name in tuple(os.environ):
        if name.upper().startswith("EA_"):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.chdir(tmp_path)
