# EA Quant Trading System

EA is an AI-native quantitative R&D and strategy promotion system in development.
Its delivered offline Research Validation reduces Idea → Evidence-ready Candidate work; live is unavailable.

## Current Phase and Health
- Phase: **Research Validation — bounded repeated long round trips**
- GitHub prerelease: **v0.2.0 published**
- Merged baseline: **main healthy**
- Live trading: **unavailable**
- Durable state: [STATUS](docs/STATUS.md); contribution rules: [WORKFLOW](docs/governance/WORKFLOW.md)

The published v0.2.0 wheel does not provide `ea --version`. This README describes current `main`
and a candidate wheel built from it; the next version and any later release require a separate
Product Owner decision.

## Installed Wheel: First Strict Report

Use the `uv` version declared in `pyproject.toml` to provision Python 3.12, plus the candidate wheel
supplied to you. Start in an empty directory; only `WHEEL` needs to be changed. This path does not
require a source checkout or editable install.

```bash
WHEEL=/absolute/path/to/the-candidate-wheel.whl
uv venv --python 3.12 user-env
uv pip install --python user-env/bin/python "$WHEEL"
EA="$PWD/user-env/bin/ea"
"$EA" --version
mkdir input runs reports
```

Create `input/prices.csv` with these exact bytes:

<!-- first-use-prices.csv:start -->
```csv
schema_version,venue,symbol,interval_start,interval_end,adjustment,open,high,low,close,volume,source,source_sequence,revision,available_at
1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,2026-01-02T09:32:00.000000Z,raw,100.5,102.0,100.0,101.5,12.0,user.local,2,0,2026-01-02T09:32:00.000000Z
1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,raw,100.0,101.0,99.0,100.5,10.0,user.local,0,0,2026-01-02T09:31:00.000000Z
1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,raw,100.0,101.5,99.0,101.0,11.0,user.local,1,1,2026-01-02T09:31:30.000000Z
1,XNAS,AAPL,2026-01-02T09:32:00.000000Z,2026-01-02T09:33:00.000000Z,raw,108.0,111.0,107.0,110.0,9.0,user.local,3,0,2026-01-02T09:33:00.000000Z
```
<!-- first-use-prices.csv:end -->

Create `input/scenario.yaml`:

<!-- first-use-scenario.yaml:start -->
```yaml
schema_version: 1
data:
  path: prices.csv
  start_utc: '2026-01-02T09:31:00.000000Z'
  end_utc: '2026-01-02T09:34:00.000000Z'
  fingerprint:
    sha256: c95c6182ba68d8c03726336172b5ce089c464fdd87ba28cb4444460fcdbae2fb
    record_count: 4
instrument:
  venue: XNAS
  symbol: AAPL
  specification_id: xnas.aapl.v1
  specification_set_id: scenario.xnas.aapl.v1
  settlement_currency: USD
  price_quantum: '0.01'
  quantity_quantum: '1'
  currency_quantum: '0.01'
  contract_multiplier: '1'
strategy:
  id: always-flat-v1
funding: {currency: USD, initial_cash: '10000'}
risk: {max_order_quantity: '5', max_position_quantity: '5', max_notional: '1000'}
execution: {policy: phase1.next-bar-close.v1}
randomness_profile: none
```
<!-- first-use-scenario.yaml:end -->

Validate, create one fresh attempt, verify completed-resume behavior, and publish its report:

```bash
"$EA" backtest validate --scenario "$PWD/input/scenario.yaml"
"$EA" backtest run --scenario "$PWD/input/scenario.yaml" --output-root "$PWD/runs"
ATTEMPT_DIR="$(find "$PWD/runs" -mindepth 1 -maxdepth 1 -type d -print -quit)"
test -n "$ATTEMPT_DIR"
"$EA" backtest resume --run-dir "$ATTEMPT_DIR"
"$EA" backtest report --run-dir "$ATTEMPT_DIR" --output-dir "$PWD/reports/first"
sed -n '1,20p' "$PWD/reports/first/summary.txt"
```

`--output-root` is the parent. A strict fresh run creates the actual UUID attempt directory below
it and prints that attempt's `result.json`; `resume` and `report` require the UUID directory.

### RESET Compatibility Demo

Omitting `--scenario` runs the fixed compatibility demo at `demo-runs/phase1-demo-v1`:

```bash
"$EA" backtest run --output-root "$PWD/demo-runs"
```

The RESET demo is run-only and does not support `resume` or `report`; those commands fail closed
because the demo intentionally does not create a complete strict-attempt evidence set.
## Installed Wheel: Local Web Research Loop

The bounded research loop uses a versioned schema-driven Strategy Contract, verified with
`bounded-long-v1` and `moving-average-entry-v1`. Generic parameters flow through history/reuse,
2-10 member batches, comparison and Chronological Holdout; see the [Web README](apps/web/README.md).
## Contributor Setup

From a source checkout, use the exact `uv` version declared in `pyproject.toml`:

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

## Product Boundary
Built-ins and trusted local strategies run offline. See [local strategy tools and SDK](docs/local-strategy-package-v1.md).
`.eastrategy` contains executable Python and should only be loaded from sources the user trusts.
This is not a Python sandbox. AI, datasets, optimization, paper/live and deployment remain unavailable.
Bounded repeated trades: [Package V3 and lifecycle](docs/local-strategy-package-v1.md#bounded-repeated-long-round-trips).
