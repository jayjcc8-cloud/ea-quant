# Quant Trading System Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 0 foundation for a long-running quantitative trading system with architecture docs, Python project skeleton, Git/GitHub workflow, and a testable CLI entrypoint.

**Architecture:** Use a Python-first modular monorepo with a thin internal domain model. Keep third-party quant frameworks as references or adapters, not as the first core dependency. All future trading paths must pass through strategy, portfolio, risk, execution, and broker boundaries.

**Tech Stack:** Python 3.12, Typer, Pydantic Settings, Polars, DuckDB, pytest, ruff, mypy, GitHub Actions.

---

## Execution status

- [x] Architecture, ADR, Git identity, SSH remote, and private GitHub repository established.
- [x] CLI, settings, and initial domain models implemented.
- [x] Unit tests, Ruff, and strict mypy checks pass locally.
- [x] VSCode interpreter, tasks, extensions, and debug configuration added.
- [x] GitHub Actions quality gate and locked Python dependency graph added.
- [x] Local `ea doctor` entrypoint verified with Python 3.12.
- [ ] Draft PR checks pass and Phase 0 is merged to `main`.

The detailed task checkboxes below preserve the original TDD execution plan. Phase 0 delivery is
consolidated into one bootstrap commit instead of the task-by-task commits shown in the plan.

---

## File structure

- Create: `src/ea/__init__.py` package marker.
- Create: `src/ea/cli/app.py` Typer CLI with `doctor`.
- Create: `src/ea/config/settings.py` settings model and environment loading.
- Create: `src/ea/core/models.py` first domain dataclasses.
- Create: `tests/unit/test_cli_doctor.py` CLI smoke test.
- Create: `tests/unit/test_core_models.py` deterministic domain model tests.
- Create: `.github/workflows/ci.yml` lint/typecheck/test workflow.
- Modify: `pyproject.toml` if package dependencies or tool settings need adjustment.
- Modify: `README.md` with actual setup commands once `uv` or another package manager is confirmed.

### Task 1: CLI doctor command

**Files:**
- Create: `src/ea/__init__.py`
- Create: `src/ea/cli/__init__.py`
- Create: `src/ea/cli/app.py`
- Test: `tests/unit/test_cli_doctor.py`

- [ ] **Step 1: Write the failing CLI test**

```python
from typer.testing import CliRunner

from ea.cli.app import app


def test_doctor_reports_project_health() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "EA system doctor" in result.stdout
    assert "python:" in result.stdout
    assert "config:" in result.stdout
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
pytest tests/unit/test_cli_doctor.py -q
```

Expected: import fails because `ea.cli.app` does not exist.

- [ ] **Step 3: Create the minimal CLI implementation**

```python
# src/ea/__init__.py
"""EA quantitative trading system."""

__all__ = ["__version__"]

__version__ = "0.1.0"
```

```python
# src/ea/cli/__init__.py
"""Command line interface package."""
```

```python
# src/ea/cli/app.py
from __future__ import annotations

import platform

import typer

app = typer.Typer(help="EA quantitative trading system CLI.")


@app.command()
def doctor() -> None:
    """Print local project health information."""
    typer.echo("EA system doctor")
    typer.echo(f"python: {platform.python_version()}")
    typer.echo("config: not loaded")
```

- [ ] **Step 4: Run the test and verify it passes**

Run:

```bash
pytest tests/unit/test_cli_doctor.py -q
```

Expected: one passing test.

- [ ] **Step 5: Commit**

```bash
git add src/ea/__init__.py src/ea/cli/__init__.py src/ea/cli/app.py tests/unit/test_cli_doctor.py
git commit -m "feat: add initial ea doctor cli"
```

### Task 2: Core domain models

**Files:**
- Create: `src/ea/core/__init__.py`
- Create: `src/ea/core/models.py`
- Test: `tests/unit/test_core_models.py`

- [ ] **Step 1: Write the failing domain model tests**

```python
from datetime import UTC, datetime

from ea.core.models import Bar, Instrument, OrderSide


def test_instrument_symbol_uses_exchange_namespace() -> None:
    instrument = Instrument(symbol="BTCUSDT", exchange="BINANCE", quote_currency="USDT")

    assert instrument.id == "BINANCE:BTCUSDT"


def test_bar_rejects_negative_volume() -> None:
    instrument = Instrument(symbol="BTCUSDT", exchange="BINANCE", quote_currency="USDT")

    try:
        Bar(
            instrument=instrument,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=-1.0,
        )
    except ValueError as exc:
        assert "volume" in str(exc)
    else:
        raise AssertionError("negative volume should fail")


def test_order_side_values_are_stable() -> None:
    assert OrderSide.BUY.value == "buy"
    assert OrderSide.SELL.value == "sell"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
pytest tests/unit/test_core_models.py -q
```

Expected: import fails because `ea.core.models` does not exist.

- [ ] **Step 3: Create the domain models**

```python
# src/ea/core/__init__.py
"""Core domain models and trading primitives."""
```

```python
# src/ea/core/models.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str
    quote_currency: str

    @property
    def id(self) -> str:
        return f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class Bar:
    instrument: Instrument
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.low > self.high:
            raise ValueError("low must be less than or equal to high")
        for name in ("open", "high", "low", "close"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
```

- [ ] **Step 4: Run the tests**

Run:

```bash
pytest tests/unit/test_core_models.py -q
```

Expected: three passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/ea/core/__init__.py src/ea/core/models.py tests/unit/test_core_models.py
git commit -m "feat: define initial trading domain models"
```

### Task 3: Configuration settings

**Files:**
- Create: `src/ea/config/__init__.py`
- Create: `src/ea/config/settings.py`
- Test: `tests/unit/test_settings.py`

- [ ] **Step 1: Write the failing settings tests**

```python
from ea.config.settings import Settings


def test_default_environment_is_development() -> None:
    settings = Settings()

    assert settings.env == "development"
    assert settings.paper_trading_enabled is True
    assert settings.live_trading_enabled is False


def test_live_trading_requires_explicit_flag() -> None:
    settings = Settings(live_trading_enabled=True)

    assert settings.live_trading_enabled is True
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
pytest tests/unit/test_settings.py -q
```

Expected: import fails because settings module does not exist.

- [ ] **Step 3: Implement settings**

```python
# src/ea/config/__init__.py
"""Configuration loading and validation."""
```

```python
# src/ea/config/settings.py
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    env: str = Field(default="development")
    paper_trading_enabled: bool = Field(default=True)
    live_trading_enabled: bool = Field(default=False)
```

- [ ] **Step 4: Run the tests**

Run:

```bash
pytest tests/unit/test_settings.py -q
```

Expected: two passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/ea/config/__init__.py src/ea/config/settings.py tests/unit/test_settings.py
git commit -m "feat: add validated project settings"
```

### Task 4: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Add CI workflow**

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Set up Python
        run: uv python install 3.12
      - name: Install dependencies
        run: uv sync --extra dev
      - name: Lint
        run: uv run ruff check .
      - name: Typecheck
        run: uv run mypy
      - name: Test
        run: uv run pytest -q
```

- [ ] **Step 2: Validate locally**

Run:

```bash
ruff check .
mypy
pytest -q
```

Expected: all checks pass after Tasks 1-3 are implemented.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add python quality gate"
```

### Task 5: GitHub remote connection

**Files:**
- Modify: local Git config and remote only.

- [ ] **Step 1: Configure repo-local Git identity**

Run with user-confirmed values:

```bash
git config user.name "USER_CONFIRMED_NAME"
git config user.email "USER_CONFIRMED_EMAIL"
```

Expected:

```bash
git config --get user.name
git config --get user.email
```

prints the confirmed values.

- [ ] **Step 2: Re-authenticate GitHub CLI**

Run:

```bash
gh auth login --hostname github.com --web
```

Expected:

```bash
gh auth status --hostname github.com
```

reports a valid authenticated account.

- [ ] **Step 3: Create or connect remote**

If creating a new private repo:

```bash
gh repo create OWNER/REPO --private --source . --remote origin --push
```

If using an existing repo:

```bash
git remote add origin git@github.com:OWNER/REPO.git
git push -u origin main
```

Expected:

```bash
git remote -v
```

shows `origin`, and GitHub contains the `main` branch.

## Self-review

- Spec coverage: architecture-first workflow, mature open-source references, Git/GitHub requirement, and phased implementation are covered.
- Placeholder scan: the only placeholder-like strings are `USER_CONFIRMED_NAME`, `USER_CONFIRMED_EMAIL`, `OWNER`, and `REPO`, which require user-provided identity and repository choices before external GitHub writes.
- Type consistency: tests and implementation use the same names: `Instrument`, `Bar`, `OrderSide`, `Settings`, and `doctor`.
