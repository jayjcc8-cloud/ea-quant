# RADIAN Local Product

RADIAN is the AI-native research-and-promotion surface of the installed EA product. It is a
single-user, loopback-only Web application served by `ea web serve` on top of the existing
backtest, Candidate, evidence and Mac-local simulated Paper authorities. RADIAN adds no account,
funds, order, or risk authority of its own.

Running mode: `LOCAL_SIMULATED`. Broker Paper and Live are not implemented and are not reachable
from any RADIAN surface.

## Workspace read surface (RAD-01)

When `ea web serve` is started with `--runtime-root`, the following read-only endpoints are
available. Absence is explicit (`available: false` plus a reason); no endpoint fabricates zero,
empty success, or sample data.

- `GET /api/workspace/overview` — recent work (backtests, batches, holdouts, candidates), needs
  attention (paper health reasons, failed runs, active alerts), the latest supervised Paper run
  observation, and the model configuration status.
- `GET /api/paper/overview?run_id=` — one observed run from `read_paper_status`: bound manifest
  identity, live writer lease, re-observed health projection (liveness, readiness, trade
  permission are separate facts and may disagree).
- `GET /api/paper/events?run_id=&limit=` — tail of the run's correlated operational log.
- `GET /api/search?q=` — minimal search over scenarios, jobs, batches, holdouts, candidates.
- `GET /api/settings/status` — model provider configuration status without any secret material.

The supervisor alert stream can be attached with `--alerts-stream <file>`; active alerts then
appear in the workspace attention list.

## Research tasks (RAD-02)

`POST /api/research/tasks {idea}` creates a durable research task and immediately runs the spec
proposal step. A task keeps the raw idea, spec draft and confirmed versions, a frozen budget,
recorded usage, and the real backtest jobs it started. Steps fail honestly and resume in place:

- `GET /api/research/tasks` / `GET /api/research/tasks/{id}` — list/detail; detail re-derives
  status from the real started jobs.
- `PATCH /api/research/tasks/{id}/spec` — edit the unconfirmed draft; validated against the
  registered strategy catalog in code, not by prompt.
- `POST /api/research/tasks/{id}/confirm {auto_run}` — confirm the draft (versioned) and start a
  real backtest through the existing single-active-job service. The same `request_id` is reused
  on retry, so retries never duplicate a job.
- `POST /api/research/tasks/{id}/retry` — resume the failed step (`propose` or `start_backtest`)
  without creating a new task.
- `POST /api/research/tasks/{id}/cancel` — stop further orchestration; already started backtests
  are not killable and keep their own terminal states.

The orchestrator's only tools are `propose_spec` and `declare_unsupported`. Object identity and
parameters are validated against the registered scenarios; unsupported ideas keep the original
text and the declared gap. Model-call and backtest budgets are frozen per task from settings.
Duplicate submissions of an active (or retryable failed) task are rejected with the existing
task id.

## Model provider configuration

Keys live only in the backend-owned settings store and never travel to the UI, logs, or research
artifacts. The store defaults to `~/.config/radian/settings.json`; set `RADIAN_SETTINGS_FILE` to
override the location. The file is created and owned by the backend (mode 0600) and has this
shape:

```json
{
  "schema": "radian.settings.v1",
  "model": {
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20251001",
    "api_key": "sk-…",
    "api_key_env": null,
    "base_url": null
  },
  "research": {
    "max_model_calls": 10,
    "max_backtests": 5
  }
}
```

`api_key` holds the key itself; `api_key_env` may instead name an environment variable the
backend process reads. `base_url` overrides the Anthropic default. Without a usable key every
model-backed step fails with `model_not_configured`; deterministic facts, evidence and Paper
operation remain available, and the UI must show the real error rather than invented analysis.
The product never assumes a development subscription provides API quota.

## Strategy generation (RAD-03)

Not yet delivered: RADIAN does not claim safe automatic strategy engineering. Generated strategy
code will execute only in a sandboxed strategy host process; the trusted process never imports or
executes generated code. This document will record the isolation boundary and its actual
coverage when RAD-03 lands.
