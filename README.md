# EA Quant Trading System

EA is an AI-native quantitative R&D and strategy-promotion system in development. This candidate
delivers one **local research loop** on a single machine: an idea, a registered and frozen
strict spec, one explicit real backtest, its formal report, one bounded evidence conclusion, and a
durable refresh. The deterministic engine runs entirely offline on `127.0.0.1` against local files;
the two model-backed steps call the external provider you configure. External Paper providers and
Live trading remain unavailable.

Simulated Paper under macOS `launchd` exists and is documented separately. It is a distinct runtime
that you install and activate explicitly; it is **not** part of the Quick Start below.

## Current Status

| Capability | Status |
| --- | --- |
| Local offline research / backtest | **AVAILABLE** |
| Loopback Web research loop | **AVAILABLE** |
| Model-backed spec proposal and conclusion | **AVAILABLE WHEN A MODEL PROVIDER IS CONFIGURED** (unconfigured by default) |
| Mac-local simulated Paper + crash/recovery/backup/alerts | **AVAILABLE** (separate runtime) |
| macOS LaunchAgent runtime operator | **AVAILABLE** (separate runtime) |
| M4 long-duration / 72-hour / 7-day Gate | **NO 72-HOUR / 7-DAY PASS RECORDED** |
| External Paper provider | **NOT AVAILABLE** |
| Live trading | **DENIED / NOT AVAILABLE** |

Durable state: [STATUS](docs/STATUS.md) · Delivery workflow: [WORKFLOW](docs/governance/WORKFLOW.md) ·
Research first release: [research-first-release.md](docs/research-first-release.md) ·
Web app: [apps/web/README.md](apps/web/README.md) ·
Simulated Paper: [local-paper.md](docs/local-paper.md) · macOS operator: [local-paper-runtime.md](docs/local-paper-runtime.md)

## Quick Start — the local research loop

This uses the repository's own bootstrap, the locked frontend build, and the existing `ea web serve`
command on loopback. It needs no broker, no external network service, and no key to run backtests
and reports; only the two model-backed steps call the configured provider (see below).

```bash
# 1. Repository-local Python environment (locked).
python3 scripts/bootstrap_local.py

# 2. Add the Web extra to that same environment (locked).
#    The default bootstrap sync installs the dev extra only, so the server
#    dependencies (FastAPI/uvicorn) are added here from uv.lock.
UV_PROJECT_ENVIRONMENT=venv uv sync --locked --extra dev --extra web

# 3. Build the frontend from the lockfile.
(cd apps/web && npm ci && npm run build)

# 4. Serve the real UI on loopback with explicit local roots.
mkdir -p "$HOME/ea-web-workspace"
venv/bin/ea web serve \
  --scenario-root examples/web-scenarios \
  --workspace "$HOME/ea-web-workspace" \
  --ui-dir apps/web/dist \
  --port 8765
```

Open `http://127.0.0.1:8765/research` (or `/backtests` for the plain backtest list) and stop the
server with `Ctrl-C`. The listener host is fixed to `127.0.0.1`; there is no host option. The
scenario, workspace, and UI roots must be separate, non-overlapping directories, which is why the
workspace is created outside the checkout. Optional roots: `--strategy-root` for local
`.eastrategy` packages, `--data-root` for registered local OHLCV CSVs, and `--runtime-root` for the
read-only simulated-Paper views.

## The local research workflow

The Web research surface is at `/research`; each task's durable record is at `/research/{task_id}`.
The path is always the same, and each step is explicit:

1. **Idea.** You submit one idea (bounded text). The task record is durable before any work starts.
2. **Registered strict spec.** The host asks the configured model to propose a spec through exactly
   two tools, `propose_spec` and `declare_unsupported`. The proposal is validated in code against
   the registered scenario/strategy catalog, not by prompt: known strategy, registered
   scenario/dataset, source identity, decimal initial cash, and the canonical parameter map. An
   idea the product cannot run is kept with its original text and a declared gap instead of being
   forced into a scenario.
3. **Frozen spec.** You edit the unconfirmed draft and confirm it. Confirming versions the spec and
   freezes it, then starts **one real backtest** through the existing single-active-job service.
   The same request identity is reused on retry, so a retry never duplicates a job.
4. **Explicit real backtest.** The run executes through the existing deterministic offline engine —
   funding, risk limits, orders, fills, ledger, reconciliation, audit, and report authorities are
   unchanged. Nothing is simulated for display.
5. **Formal report.** A verified report is published for the completed run. The conclusion reads
   only that verified report; the numbers shown to you come from the report, not from the model.
6. **One bounded evidence conclusion.** On a succeeded run, **Interpret this run** asks the host to
   read the verified report and publish exactly one bounded conclusion against the criterion that
   was frozen before the run. It spends at most one model call, starts no new backtest, and never
   changes the result above.
7. **Refresh.** Task status is re-derived from the real started jobs on every read, so a refresh,
   a back navigation, or a reopened page shows the persisted truth and never re-confirms, re-runs,
   or submits anything.

### What the UI does not fake

- **The result comes first.** A published conclusion is shown with the verified run behind it,
  not instead of it.
- **Backtest status and conclusion status are separate axes.** A task can be `succeeded` with a
  published conclusion, with a recorded interpretation failure, or with the conclusion closed by
  your cancellation. A succeeded run without a conclusion is shown plainly as
  “Backtest succeeded / conclusion not published” — an empty slot, never an invented conclusion.
- **Pending and unproven boundaries stay neutral.** When a submission left the process and its
  outcome cannot be proven, the task is not silently retried or marked failed: the slot stays
  charged, the message names the real gap, and the only offered new attempt is an explicit cancel
  plus a new task. **Retry is offered only when the failure provably spent nothing** or reached a
  terminal provider answer. There is no fake retry button.
- **Cancellation is not a rollback.** Cancelling stops future orchestration. It does not kill or
  undo a backtest that already started; started runs keep their own terminal states. Cancelling
  after a run succeeded closes only the conclusion axis, and the completed run and its verified
  report are left exactly as they are.
- **The conclusion axis is final once closed.** No new model-call slot may be reserved for it and
  no late result or failure may overwrite it.

### What “continuous research” means here

Continuous research means **repeated independent, explicitly confirmed experiments**: you run one
idea → spec → confirm → backtest → conclusion cycle, then start another. It is **not** autonomous
background continuous optimization and **not** autonomous trading. Nothing is created, confirmed,
run, or concluded without a user action.

## Model provider configuration

Model-backed steps (spec proposal and the single conclusion) are **off until configured**. The
store defaults to absent, and absent means “not configured” — the product shows that honestly and
never presents a working provider. Every model-backed step then fails with `model_not_configured`,
while deterministic facts, evidence, backtests, reports, and simulated Paper stay available.

The backend-owned settings file defaults to `~/.config/radian/settings.json`; set
`RADIAN_SETTINGS_FILE` to point elsewhere. Its shape and the supported providers are documented in
[RADIAN model provider configuration](docs/radian-local-product.md#model-provider-configuration).
Supported provider values are `anthropic`, `deepseek`, and `openai-chat-completions-compatible`.

Two credential shapes are supported, both backend-side only. The recommended one is the
environment reference:

- `api_key_env` — the **name** of an environment variable that the backend process reads at
  startup. This is the recommended shape: the settings file names the variable and holds no secret,
  and you provide its value to the `ea web serve` process yourself. Never commit a key, and do not
  write it into a file tracked by Git.
- `api_key` — the key itself, inside the settings file. This is supported, but it stores the secret
  on disk, so prefer `api_key_env` unless you have a specific reason to do otherwise.

The key never travels to the UI, the logs, or research artifacts. `/api/settings/status` and the
`/settings` page report only whether a provider is configured, plus the protocol, endpoint,
capabilities, and the settings file path — never the key. This repository ships no credential-entry
UI and no separate credential launcher; the shipped surface reads the settings file and reports
status.

### Screenshot

![Historical single example: a research task whose idea, frozen spec, real backtest, verified report, and one bounded conclusion were completed by a model on commit 088d4ab. The image is an illustration of one past run, not a current capability or performance claim.](docs/site/assets/research-conclusion.png)

The screenshot is a historical single example: the model completed that conclusion on commit
`088d4ab`. An isolated verification of the current UI was completed against a copied, safe
historical report, with no new provider calls and no new backtests; it showed no overflow at
desktop or mobile widths and a stable state across GET refresh. Full exact-head release
verification is not yet passed: it was attempted and the formatter gate blocked it, so the release
remains pending.

## Simulated Paper (separate runtime)

Mac-local simulated Paper is delivered and documented, but it is a **separate, explicitly installed
runtime** and is not started by the Quick Start above. It uses an in-process feed and broker on your
own machine; it does not connect to an external account. Read the boundary and the operational
details in [local-paper.md](docs/local-paper.md) and
[local-paper-runtime.md](docs/local-paper-runtime.md) before activating it. Long-duration 72-hour
and 7-day stability is not yet established, and no Paper Gate PASS is recorded — see
[research-first-release.md](docs/research-first-release.md).

## What this candidate does not claim

- No verified real-time, all-market, robustness, or profit claim. The examples are deterministic
  demo and test inputs, and a formal backtest report is one run over one registered input.
- No external Paper provider and no Live trading; Live remains denied.
- No autonomous optimization, no autonomous trading, and no AI authority over orders, risk, or
  paper/live execution. AI assistance is bounded to the spec proposal and the single conclusion.
- A strategy artifact (`.eastrategy`) is trusted local executable Python, not a sandbox.
- No mobile or remote access: the Web app is a single-user loopback service. It is not a hosted
  browser app and cannot be reached from another device.
- No package-registry publication and no deployment.

## Version and release status

The public package version stays **0.2.0** until the owner decides otherwise. The published `v0.2.0`
GitHub prerelease predates this candidate, so it does not contain the research loop described here.
A next tag, `v0.3.0-rc.1`, is a **proposal only** — no version edit, tag, or publication has been
made. The candidate is not a completed release; see
[research-first-release.md](docs/research-first-release.md) for what is implemented and tested
versus what still gates publication.

## Other entry points

- Backtest CLI: `ea backtest validate|run|report|resume`, `ea data inspect` —
  [local-strategy-package-v1.md](docs/local-strategy-package-v1.md)
- Strategy packages: `ea strategy pack|validate|inspect` —
  [local-strategy-package-v2.md](docs/local-strategy-package-v2.md)
- Candidate lifecycle: [candidate-lifecycle.md](docs/candidate-lifecycle.md)
- AI-native local product surface: [radian-local-product.md](docs/radian-local-product.md)
- Web app details and supported checks: [apps/web/README.md](apps/web/README.md)
- Static project page: [docs/site/index.html](docs/site/index.html)

### Contributor setup

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

## Product boundary

The research loop is deterministic: local scenario files, local OHLCV, a simulated matcher, ledger,
reconciliation, audit, and formal reports all run offline. Only the two model-backed steps leave the
machine, and only to the provider you configure. Simulated Paper is local and simulated;
Live is unavailable and denied. VPS host verification is not required for the current
local-development phase and would verify host attributes only, never product logic. AI output is
not runtime trading authority. [STATUS](docs/STATUS.md) remains the durable current boundary.
