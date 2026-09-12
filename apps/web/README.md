# EA Quant Web

This is the Dark Professional frontend for the real local offline backtest and bounded research
loop defined by ADRs 0030-0035. The production build talks only to the same-origin `/api`
served by `ea web serve`; it does not fall back to mock business data when the service is
unavailable. Component tests inject an explicit fake adapter, while the Playwright suite uses an
installed candidate wheel, actual loopback HTTP, the existing engine, and formal reports.

StrategyDescriptorV1 drives generic integer/decimal controls for both built-in research strategies:
`bounded-long-v1` and `moving-average-entry-v1`. Initial cash is editable; symbol remains bound to
registered data. The backend resolves every dynamic bound, including MA history and next-bar
eligibility. The entire normalized parameter map passes through immutable history/reuse, explicit
batches, and same-strategy/version parameter deltas. Different strategies have no parameter delta.
V1 scenarios and historical relationships remain readable without migration; Scenario V2 uses an
independent identity domain. Always-flat remains available to CLI but hidden from research selection.

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

## Chronological Holdout

Open a successful research-strategy run (including an explicitly selected batch member), choose
**Evaluate chronological holdout**, and select a compatible later registered scenario. Parameters
are frozen from the source snapshot. The backend rejects equal/overlapping windows, configuration
mismatches, invalid target dynamic bounds, and unavailable source reports before creating a job.

The bundled `bounded-long-commission.yaml` and `chronological-holdout.yaml` provide an earlier/later
positive-commission example with different target defaults to demonstrate source parameter authority.
The new run uses the normal single execution slot and formal reporter. The Chronological Holdout
sidebar reopens saved relationships and both independent reports after restart. Unfinished jobs
remain interrupted under the existing restart behavior. This evaluation makes no statistical claim.

The bundled `moving-average-entry.yaml` and `moving-average-holdout.yaml` are Scenario V2
examples with separate six-bar chronological windows. MA enters once when fast SMA exceeds slow
SMA using admitted raw revision-0 closes; revisions do not count as additional history. The whole
map is frozen for holdout and validated against target data, without clamping or target defaults.
No AI generation, external plugin loading, data editor, optimizer or live capability is included.


## Registered local research data

Start the installed server with optional `--data-root /absolute/research-data` alongside the
existing scenario, strategy, workspace and UI roots. Keep these directories separate. Place
strict Phase-1 OHLCV CSV files directly in that directory; choose **Research dataset** before
validation or batch creation. `ea data inspect /absolute/research-data/research.csv` prints raw
provenance, canonical fingerprint, instrument, record count and the derived full-capture window.
No source changes or manual fingerprint calculation are needed for another compatible dataset.

The registered scenario still authorizes the exact instrument and economic configuration.
Invalid or incompatible CSVs reject; there is no upload, path field, cleaning, date-range editor
or automatic splitting. For chronological Holdout, provide a separate strictly later compatible
CSV and explicitly select it from the Holdout choices. Source strategy bytes and parameters stay
frozen. Dataset filenames identify catalog entries, not economic semantics.

History reuse retains the selected dataset and expected raw SHA. If its contents changed,
validation reports a conflict; explicitly selecting current input starts a new validation.
Completed Web reports, comparisons and Holdout remain readable after external CSV deletion or
replacement. New runs and CLI report regeneration/resume may require the original source file.

### Single long round trip

Select `single-long-round-trip.yaml` for Scenario V4 / Action V2. `entry_delay` counts eligible
pre-entry roots; `hold_root_count` counts eligible roots after entry; `target_quantity` is the
requested entry quantity. Each run permits one entry and one full exit, with independent fees.
Run Detail shows entry/exit facts and `FLAT_INITIAL`, `OPEN_AT_END`, or `CLOSED`. An absent exit
is shown as `—`; an open position has zero realized P&L and gross unrealized P&L before its entry
fee. Net P&L includes fees. No forced closing occurs at the end of a window.

Report V2 and Path V2 are persisted under Web Job V4. The curve updates cash and position at each
acknowledged fill and retains every root for drawdown. Batch, comparison and chronological
holdout reuse these verified artifacts; holdout freezes the strategy parameters and lifecycle.
Old scenarios, reports and jobs retain their original formats and behavior.

Web V4 integer controls reject values outside ±9007199254740991 to preserve exact JSON/browser
input identity. The CLI retains its original integer contract; no value is silently clamped.
