# EA Quant Trading System

EA is an AI-native quantitative R&D and strategy-promotion system in development.

Current `main` supports deterministic offline research plus **Mac-local continuous simulated Paper**.
The local Paper path is supervised by macOS `launchd`; external Paper providers and Live trading
remain unavailable.

## Current Status

| Capability | Status |
| --- | --- |
| Offline research / backtest | **AVAILABLE** |
| Mac-local simulated Paper | **AVAILABLE** |
| Crash recovery / reconciliation / kill switch | **AVAILABLE** |
| Health, backup / restore, local alerts | **AVAILABLE** |
| macOS LaunchAgent runtime operator | **AVAILABLE** |
| Long-duration M4 / 72h Gate | **NOT YET SATISFIED** |
| External Paper provider | **NOT AVAILABLE** |
| Live trading | **DENIED / NOT AVAILABLE** |
| VPS verification | **Not required for current local-development phase** |

Durable project state: [STATUS](docs/STATUS.md).  
Current local Paper product: [Mac-local Paper V1](docs/local-paper.md).  
macOS runtime operator: [Mac-local Paper runtime](docs/local-paper-runtime.md).

> **Release note:** the published `v0.2.0` prerelease predates the current Mac-local Paper runtime.
> For current Paper testing, use an EA installation built from the reviewed commit that owns the
> accepted Candidate. Do not assume the prerelease wheel contains the current Paper/operator path.

---

## Local Paper Test — Quick Start

For normal local testing on macOS, use the **LaunchAgent runtime operator** rather than manually
backgrounding `ea paper start`. The operator is intentionally thin: `launchd` owns process
lifetime and the clock, while `ea paper start|stop|status|backup` remain authoritative for the
Paper runtime.

### 1. Preconditions

Before starting the supervised runtime, you need:

- macOS and the host connected to AC power for unattended testing;
- an installed `ea` entrypoint built from the reviewed Candidate-compatible commit;
- one **ACCEPTED** Candidate in the research workspace;
- the Candidate's matching Paper scenario and inputs;
- Live left unavailable / denied.

If you do not yet have an accepted Candidate, follow
[Mac-local Paper V1 — Install and prepare one Candidate](docs/local-paper.md#install-and-prepare-one-candidate)
first.

The normal runtime boundary is:

```text
~/EA/
  runtime/      supervised Paper attempts
  workspace/    accepted Candidate workspace
  inputs/       Paper scenario / fixtures
  logs/         launchd stdout / stderr
  evidence/     host / Gate evidence
  backups/      settled attempt backups
  supervisor/   ea-runtime, pinned config and state
```

### 2. One-time host setup

From the repository checkout:

```bash
mkdir -p ~/EA/{runtime,workspace,inputs,logs,evidence,backups,supervisor}

cp ops/launchd/run-config.example ~/EA/supervisor/run-config.env
${EDITOR:-vi} ~/EA/supervisor/run-config.env
```

At minimum, verify the pinned values in `run-config.env`:

- `EA_BIN` — the Candidate-compatible installed `ea` executable;
- `WORKSPACE` — the workspace containing the ACCEPTED Candidate;
- `CANDIDATE_ID` — the ACCEPTED Candidate UUID;
- `SCENARIO` — the matching Paper scenario;
- `RUNTIME_ROOT` — normally `~/EA/runtime`;
- `BACKUP_ROOT` — normally `~/EA/backups`;
- `PRICES`, `INTERVAL`, backup cadence and restart throttle.

Then install and load the two per-user LaunchAgents:

```bash
sh ops/launchd/ea-runtime install --config ~/EA/supervisor/run-config.env
```

`install` renders and bootstraps:

- `com.ea.paper` — the supervised foreground Paper process;
- `com.ea.paper-backup` — scheduled settled-attempt backup.

Because the Paper LaunchAgent uses `RunAtLoad`, installation may start the supervised Paper attempt
immediately. Check status before issuing another start; `ea-runtime start` is intentionally a no-op
when one supervised writer is already live.

### 3. Start and observe a local test

Use the installed operator:

```bash
EA_RUNTIME=~/EA/supervisor/ea-runtime

"$EA_RUNTIME" start
"$EA_RUNTIME" status
"$EA_RUNTIME" log paper
```

Useful inspection commands while the test is running:

```bash
"$EA_RUNTIME" status
"$EA_RUNTIME" log paper
"$EA_RUNTIME" log backup
cat ~/EA/supervisor/state/current-run
```

Run one backup tick manually when needed:

```bash
"$EA_RUNTIME" backup
```

Normal scheduled backup cadence is controlled by `BACKUP_INTERVAL_SECONDS` in
`~/EA/supervisor/run-config.env` (the current example defaults to 300 seconds).

### 4. Stop the local test

Stop through the operator rather than killing the process:

```bash
"$EA_RUNTIME" stop
"$EA_RUNTIME" status
```

A normal stop is cooperative: EA writes the stop request, reaches terminal state and releases the
writer lease. A successful exit stays stopped under the current `KeepAlive{SuccessfulExit:false}`
policy.

Do **not** use SIGKILL as the normal stop path. SIGKILL is a crash drill; `launchd` treats it as an
unsuccessful exit and starts a **fresh attempt with a new run ID**.

### 5. Restart after a clean stop

```bash
"$EA_RUNTIME" start
"$EA_RUNTIME" status
```

Important runtime semantics:

- starting an already-running supervised job does not create a second writer;
- a crash / SIGKILL is replaced by a fresh attempt;
- an intentional cooperative stop stays stopped while the current LaunchAgent remains loaded;
- every supervised launch receives a fresh run ID;
- status and log projections are observations, not a second source of trading truth.

### 6. Keep the Mac awake during short local tests

The runtime operator does **not** change macOS power-management settings.

For a short validation run while connected to AC, you may temporarily keep the Mac awake with:

```bash
caffeinate -s
```

The assertion disappears when `caffeinate` exits.

Do not treat this as the production-like 72h Gate configuration. Persistent host power settings
(such as an explicit AC sleep policy) belong to **Gate preparation**, where the previous host value
should be recorded and restored after the Gate. Ordinary local testing does not require README setup
to modify `pmset`.

### 7. Reboot / login behavior

The Paper LaunchAgent uses `RunAtLoad`. A new login or reboot can therefore start Paper again even
after a previous clean stop.

If this Mac must remain stopped across a reboot:

```bash
launchctl disable gui/$(id -u)/com.ea.paper
```

Before intentionally starting Paper again:

```bash
launchctl enable gui/$(id -u)/com.ea.paper
"$EA_RUNTIME" start
```

### 8. Real-host acceptance

The focused macOS host acceptance is:

```bash
uv run --no-project --python 3.12 python scripts/accept_local_paper_runtime.py
```

It exercises the real LaunchAgents, installed `ea`, crash replacement, M1 recovery /
reconciliation, scheduled backup and intentional stop/start behavior, and writes machine-readable
evidence below `~/EA/evidence/`.

Unless `--keep-installed` is explicitly used, the acceptance script cleans up the LaunchAgents
after its drills. See [Mac-local Paper runtime — Host acceptance](docs/local-paper-runtime.md#host-acceptance).

---

## Direct Paper CLI

For focused debugging, `ea paper start` can still be run directly in the foreground or
backgrounded by the shell. That lower-level path is documented in
[Mac-local Paper V1 — Start, observe and stop](docs/local-paper.md#start-observe-and-stop).

For normal unattended local testing, prefer `~/EA/supervisor/ea-runtime` so crash semantics,
single-writer behavior, logs and scheduled backup use the same host path already accepted on macOS.

---

## Installed Wheel: First Strict Backtest Report

The offline strict-report path remains available independently of Paper.

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

---

## Local Web Research Loop

The bounded research loop uses a versioned schema-driven Strategy Contract, verified with
`bounded-long-v1` and `moving-average-entry-v1`. Generic parameters flow through history/reuse,
2-10 member batches, comparison and Chronological Holdout.

See the [Web README](apps/web/README.md).

---

## Contributor Setup

From a source checkout, use the exact `uv` version declared in `pyproject.toml`:

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

---

## Product Boundary

- Built-ins and trusted local strategies run offline.
- Mac-local simulated Paper is available only through the explicit accepted-Candidate path.
- The local Paper feed and broker are simulated and in-process; no external broker/account is used.
- External Paper-provider connectivity is unavailable.
- Live trading is unavailable and denied.
- Long-duration 72h/7-day production-candidate stability is not yet established.
- VPS operation is not required for the current local-development phase.
- `.eastrategy` contains executable Python and should only be loaded from sources the user trusts;
  it is not a Python sandbox.
- AI interpretation / Claude Observer is not trading authority and is not part of the deterministic
  runtime safety layer.

See [local strategy tools and SDK](docs/local-strategy-package-v1.md) and
[Project Status](docs/STATUS.md) for the current durable boundary.
