# EA Quant Web

This is the Dark Professional frontend for the real local offline backtest and bounded research
loop defined by ADRs 0030-0033. The production build talks only to the same-origin `/api`
served by `ea web serve`; it does not fall back to mock business data when the service is
unavailable. Component tests inject an explicit fake adapter, while the Playwright suite uses an
installed candidate wheel, actual loopback HTTP, the existing engine, and formal reports.

The V1 research controls are intentionally narrow: `initial_cash` plus the backend-published
`bounded-long-v1` parameters `target_quantity` and `entry_delay_bars` are editable; `symbol` is
read-only and comes from the selected registered, verified scenario/data combination. The backend
derives the delay maximum from the loaded canonical market-bar sequence and next-bar execution
semantics. Saved runs retain their normalized input snapshot. “Use parameters” creates a new
validation/run, and comparison shows parameter changes beside exact report deltas without storing
a new comparison artifact or making causal claims.

`/batches/new` creates one explicit 2-10 member parameter set for a single scenario. The backend
validates the complete set before creating normal jobs, executes them serially, and persists only
the batch-to-job relationship. `/batches/{batch_id}` derives member states, canonical parameters,
equity, net P&L, and total return after refresh or restart. Its ephemeral controls filter by state
and sort exact decimal values with stable missing-last behavior; any two members reuse the existing
currency-safe comparison. The UI never names a best or recommended run.

Run the supported frontend checks from this directory:

```bash
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npm run test:e2e
```

`npm run test:e2e` builds the wheel and UI, creates a repository-outside Python environment,
installs the wheel non-editably with its Web extra, and runs Chromium against the installed
service. Supported entries are `/backtests` and `/batches/new`; job, batch, and
`/backtests/compare/{left_job_id}/{right_job_id}` URLs are refreshable.

For direct use, build with `npm run build`, then follow the root README command using separate
scenario, workspace, and `dist` roots:

```bash
WHEEL=/absolute/path/to/the-candidate-wheel.whl
WEB_DIST=/absolute/path/to/apps/web/dist
SCENARIOS=/absolute/path/to/examples/web-scenarios
WORKSPACE=/absolute/path/to/an-empty-web-workspace
uv venv --python 3.12 web-env
uv pip install --python web-env/bin/python "$WHEEL[web]"
web-env/bin/ea web serve \
  --scenario-root "$SCENARIOS" \
  --workspace "$WORKSPACE" \
  --ui-dir "$WEB_DIST" \
  --port 8765
```

Open `http://127.0.0.1:8765/backtests`; stop the service with `Ctrl-C`. There is no host option and
Vite development mode is not the delivery entrypoint.

`bounded-long-commission.yaml` is a positive-fee sibling of the legacy scenario. Its registered
execution policy charges 100 bps per actual Fill, half-even rounded to the settlement currency
quantum. The UI displays the fixed assumption and verified report fees; equity, P&L and return
include ledger-applied commission. There is no cost editor or second fee calculator in Web.
