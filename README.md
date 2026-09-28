# EA Quant Trading System
EA is an AI-native quantitative R&D and strategy-promotion system in development. Current `main`
supports deterministic offline research plus **Mac-local continuous simulated Paper** under macOS
`launchd`. External Paper providers and Live trading remain unavailable.

## Current Status
| Capability | Status |
| --- | --- |
| Offline research / backtest | **AVAILABLE** |
| Mac-local simulated Paper + crash/recovery/backup/alerts | **AVAILABLE** |
| macOS LaunchAgent runtime operator | **AVAILABLE** |
| M4 long-duration / 72h Gate | **NOT YET SATISFIED** |
| External Paper provider | **NOT AVAILABLE** |
| Live trading | **DENIED / NOT AVAILABLE** |

Durable state: [STATUS](docs/STATUS.md) · Delivery workflow: [WORKFLOW](docs/governance/WORKFLOW.md) ·
Paper product: [local-paper.md](docs/local-paper.md) · macOS operator: [local-paper-runtime.md](docs/local-paper-runtime.md)

> The published `v0.2.0` prerelease predates the current Mac-local Paper runtime. For Paper testing,
> use the reviewed Candidate-compatible EA installation.

## Local Paper Test — Quick Start
Normal unattended local testing should use `~/EA/supervisor/ea-runtime`. `launchd` owns process
lifetime; `ea paper start|stop|status|backup` remain authoritative for Paper state and evidence.

### 1. Preconditions
Use macOS, keep the machine on AC for unattended tests, and prepare one **ACCEPTED** Candidate with
its matching installed `ea`, workspace and scenario. If needed, first follow
[Install and prepare one Candidate](docs/local-paper.md#install-and-prepare-one-candidate).

### 2. One-time host setup
From the repository checkout:
```bash
mkdir -p ~/EA/{runtime,workspace,inputs,logs,evidence,backups,supervisor}
cp ops/launchd/run-config.example ~/EA/supervisor/run-config.env
${EDITOR:-vi} ~/EA/supervisor/run-config.env
```
Verify `EA_BIN`, `WORKSPACE`, `CANDIDATE_ID`, `SCENARIO`, `RUNTIME_ROOT`, `BACKUP_ROOT`,
`PRICES`, `INTERVAL`, backup cadence and restart throttle, then install:
```bash
sh ops/launchd/ea-runtime install --config ~/EA/supervisor/run-config.env
```
`install` loads `com.ea.paper` and `com.ea.paper-backup`. Paper uses `RunAtLoad`, so the first
attempt may already be running; `start` is safe and does not create a second writer.

### 3. Start, observe and stop
```bash
EA_RUNTIME=~/EA/supervisor/ea-runtime
"$EA_RUNTIME" start
"$EA_RUNTIME" status
"$EA_RUNTIME" log paper
```
Useful while running:
```bash
"$EA_RUNTIME" log backup
"$EA_RUNTIME" backup
cat ~/EA/supervisor/state/current-run
```
Stop cooperatively:
```bash
"$EA_RUNTIME" stop
"$EA_RUNTIME" status
```
Restart later with `"$EA_RUNTIME" start`.

Runtime semantics:
- start on an already-running job is a no-op; there is no second writer;
- crash / SIGKILL creates a **fresh attempt with a new run ID**;
- cooperative stop exits successfully and stays stopped while the agent remains loaded;
- status/logs are observations, not a second source of trading truth;
- login/reboot can start Paper again because the LaunchAgent uses `RunAtLoad`.

To remain stopped across reboot:
```bash
launchctl disable gui/$(id -u)/com.ea.paper
# before intentionally running again:
launchctl enable gui/$(id -u)/com.ea.paper
"$EA_RUNTIME" start
```

### 4. Keep the Mac awake for short tests
The operator does not change power settings. On AC, use a temporary assertion when needed:
```bash
caffeinate -s
```
Do not treat this as 72h Gate configuration. Persistent AC sleep settings belong to Gate
preparation, where the previous value is recorded and restored afterwards; ordinary startup does
not require changing `pmset`.

### 5. Real-host acceptance
```bash
uv run --no-project --python 3.12 python scripts/accept_local_paper_runtime.py
```
This exercises the real LaunchAgents, installed `ea`, crash replacement, M1 recovery /
reconciliation, scheduled backup and intentional stop/start behavior. Evidence is written below
`~/EA/evidence/`. Unless `--keep-installed` is used, the script cleans up its agents.

## Other Entry Points
- Direct Paper debugging: [local-paper.md](docs/local-paper.md#start-observe-and-stop)
- Candidate lifecycle: [candidate-lifecycle.md](docs/candidate-lifecycle.md)
- Strategy / backtest CLI: [local-strategy-package-v1.md](docs/local-strategy-package-v1.md)
- Local Web research: [apps/web/README.md](apps/web/README.md)
- Runtime/failure details: [local-paper-runtime.md](docs/local-paper-runtime.md)

## Contributor Setup
```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

## Product Boundary
Mac-local Paper is simulated and uses an in-process feed/broker; it does not connect to an external
account. Live remains unavailable and denied. Long-duration 72h/7-day stability is not yet
established. VPS verification is not required for the current local-development phase.
`.eastrategy` is trusted executable Python, not a sandbox. AI interpretation is not runtime
trading authority. See [STATUS](docs/STATUS.md) for the durable current boundary.
