# EA Quant Trading System

EA is a long-running quantitative-trading infrastructure project. It builds deterministic,
auditable, restart-equivalent research and execution contracts before enabling any live trading.

## Current Phase and Health

- Phase: **Phase 1 — Backtest MVP (Incomplete)**
- Merged baseline: **main healthy**
- Live trading: **unavailable**
- Active gate: [Phase 1 project status](docs/STATUS.md)

Do not infer current capability from old Issues, comments, or this short entry page. STATUS is the
human-readable present; merged code/tests/CI remain evidence of implemented behavior.

## Quick Start

Requirements: Python 3.12 and the exact uv version declared in `pyproject.toml`.

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
```

Run the supported quality or full verification profiles:

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

Validate and run a strict local `BacktestScenario v1`:

```bash
venv/bin/ea backtest validate --scenario /absolute/path/to/scenario.yaml
venv/bin/ea backtest run \
  --scenario /absolute/path/to/scenario.yaml \
  --output-root /absolute/path/to/runs
```

The scenario is the product configuration authority. It binds a local OHLCV path and fingerprint,
an explicit UTC replay window, one instrument specification, positive initial cash, order/position/
cash/notional limits, the deterministic next-bar-close execution policy, and either
`always-flat-v1` or `bounded-long-v1`. Every run creates a fresh attempt directory and emits
funding, audit, and result evidence. The fixed RESET demo remains available by omitting
`--scenario`.

This is an offline deterministic single-run product. Resume, generic crash recovery,
BacktestReportV1 analytics, paper/live profiles, broker writes, credentials, and release
publication are unavailable.

## Project Navigation

- [Current status](docs/STATUS.md)
- [Phase roadmap](docs/ROADMAP.md)
- [Technical architecture](docs/architecture.md)
- [Accepted architecture decisions](docs/adr/)
- [Governance workflow](docs/governance/WORKFLOW.md)
- [Contributor entry point](CONTRIBUTING.md)

History lives in Git, Issues, pull requests, and CI. Decisions live in ADRs. Current state lives in
STATUS. Future Phase boundaries live in ROADMAP.
