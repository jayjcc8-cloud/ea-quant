# EA Quant Web

This is the Dark Professional frontend for the real local offline backtest and bounded research
loop defined by ADRs 0030 and 0031. The production build talks only to the same-origin `/api`
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
service. The supported product entry is `/backtests`; `/backtests/{job_id}` and
`/backtests/compare/{left_job_id}/{right_job_id}` are refreshable.

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
