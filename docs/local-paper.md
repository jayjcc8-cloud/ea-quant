# Mac-local Paper V1

`ea paper start` is an explicit local simulation operation. It requires an ACCEPTED Candidate
bound to the installed EA implementation, then uses the shared Strategy, portfolio planner,
Risk, Order, execution-fact, Fill, ledger, reconciliation and audit owners. The price cycle is a
generated local feed, and the broker is in-process. No account credentials or network provider
are used. Live is unavailable. See [ADR 0049](adr/0049-explicit-local-paper-profile.md).

## Install and prepare one Candidate

Use the wheel built from the desired reviewed commit and Python 3.12. The published v0.2.0
prerelease does not contain this entry. Run outside the source checkout:

```sh
WHEEL=/absolute/path/to/ea_quant-0.2.0-py3-none-any.whl
uv venv --python 3.12 paper-env
uv pip install --python paper-env/bin/python "$WHEEL"
EA="$PWD/paper-env/bin/ea"
"$EA" --version
```

Prepare research with this same installation before starting Paper. Copy the four files from
[`examples/paper`](../examples/paper/) into an external input directory. They contain synthetic
USD/AAPL prices, initial cash 1000, quantity 2, three bounded long round trips, commission 10 bps
and slippage 100 bps. `later.yaml` selects a separate, later synthetic Holdout interval. These
fixtures demonstrate plumbing and deliberately lose money; they are not an investment strategy.

The current research UI is supplied separately from the Python wheel. Use the built `apps/web/dist`
from that same source snapshot and install its declared Web dependencies:

```sh
uv pip install --python paper-env/bin/python "${WHEEL}[web]"
INPUTS=/absolute/path/to/copied/paper-inputs
WORKSPACE="$PWD/research-workspace"
UI=/absolute/path/to/built/web/dist
"$EA" web serve --scenario-root "$INPUTS" --workspace "$WORKSPACE" --ui-dir "$UI" --port 8765
```

In the local Web UI, validate and run `source.yaml`, create its chronological Holdout using
`later.yaml`, create a Candidate, inspect it and explicitly ACCEPT with a reason. Copy its ID.
An EVALUATED or rejected Candidate cannot start Paper. Stop the research server when finished.
An EA code or distribution upgrade requires new research evidence; do not edit old identities.

## Start, observe and stop

Use an absolute workspace and the accepted scenario's unchanged strategy, normalized parameters,
instrument, funding, risk and execution settings. Supported profiles are Action V2 single/bounded
long-only V4/V5, including retained local packages. Other profiles reject before package execution.

```sh
CANDIDATE=accepted-candidate-uuid
RUN_ID="$(paper-env/bin/python -c 'from uuid import uuid4; print(uuid4())')"
RUN_DIR="$PWD/paper-runs/$RUN_ID"
"$EA" paper start --workspace "$WORKSPACE" --candidate-id "$CANDIDATE" \
  --scenario "$INPUTS/source.yaml" --output-root "$PWD/paper-runs" \
  --run-id "$RUN_ID" --prices 100 --interval 0.1 > "$PWD/paper-console.jsonl" 2>&1 &
PAPER_PID=$!
```

Wait for `paper_started` in `paper-console.jsonl`, then:

```sh
"$EA" paper status --run-dir "$RUN_DIR"
tail -n 12 "$RUN_DIR/operational.jsonl"
"$EA" paper stop --run-dir "$RUN_DIR" --timeout 10
wait "$PAPER_PID"
"$EA" paper status --run-dir "$RUN_DIR"
```

Start stays in its foreground process unless the shell backgrounds it. Ctrl-C/SIGTERM uses the
same cooperative cutoff. Status reports acknowledged cash, equity, position, valuation price/time,
market/fact counts, observed versus committed Fills, pending work and writer-lease presence. The
stop command waits for terminal status **and** release of the existing audit writer lease. A timeout
keeps the stop request and returns failure; it does not kill an unrelated PID or pretend to finish.

The example completes three round trips, then keeps receiving fresh generated market events and
heartbeats until stopped. Independent arithmetic is: buy at 101, sell at 99, quantity 2, fee 0.20
per Fill; six Fills lose 13.20, leaving cash/equity 986.80, flat, ledger sequence 7. Each new Fill
is queried once through the Paper broker; the redelivered trade is retained as duplicate evidence
without a second ledger effect. No quantity or Fill is hardcoded into the runner.

## Failures and retained evidence

Adding `--event-limit 2` makes the simulated source exhaust after two market events. This returns
exit code 3 with `source_exhausted`, stops new decisions, drains issued facts and may retain an open
position. Normal user stop returns 0. Stop never implies liquidation. Fresh starts always receive
a new run ID; existing, interrupted or unknown attempts cannot resume or resend orders.

Before transport, the runtime verifies active market identity, age, stop state, current portfolio
and risk versions, accepted quantity limits, current cash and notional including worst configured
buy slippage and fees. It verifies the exact durable authorization acknowledgement and rechecks
stop/freshness before submission. Source-issued facts and processing outcomes must be acknowledged
before ledger application; ledger and refresh acknowledgements must precede publication. An audit
failure can leave an observed Fill or an internal ledger advance without a published balance;
`incomplete`, `observed_fills` and `internal_ledger_sequence` retain that distinction.

`manifest.json` binds the explicit Paper operation, Candidate identity/configuration, simulation
price cycle and initial funding. `funding.json`, the existing POSIX audit journal and
`operational.jsonl` retain financial and causal evidence. Operational events include Candidate,
account, Order/client and Fill IDs with portfolio changes. Status files are observations, not
execution or recovery authority. Missing writer lease on a running snapshot becomes `interrupted`.
Local reconciliation compares funding plus retained Fills with the acknowledged ledger; it is not
an external broker account confirmation.

Global `--config`, `--environment`, `EA_CONFIG_PATH`, `EA_ENVIRONMENT` and any explicit run mode
other than `paper` are rejected without loading configuration. Feed buffering, strategy/order
history and current refresh retention are bounded; the audit journal's existing resource caps fail
closed. Local crash recovery is delivered by M1 (`ea paper resume`, journal replay, reconciliation
and kill switch); M3 adds the read-only health projection, quiesced backup/restore and the local
alert stream described below. This path still does not establish long-duration stability, 72-hour
soak, real-provider semantics or crash recovery against an external provider, VPS operation, or
Live safety.

## Self-operation: health, backup, restore and alerts

`ea paper status` returns one read-only health projection alongside acknowledged money. It
separates three questions that must never be collapsed: `process_alive` (LIVENESS, from the
writer lease), `runtime_ready` (READINESS, dependencies and authoritative state consistent) and
`trade_permitted` (TRADE_PERMISSION, PPV-15's own verdict, with `trade_blocking_guard` and
`trade_reason`). `reason_codes` names every condition behind a not-ready or not-permitted answer.
An operator halt appears as a halt, never as a process failure.

`ea paper backup --run-dir DIR --backup-root ROOT [--keep N]` captures only authoritative state
(the journal, manifest, funding, kill switch, operational-safety limits, strategy) while the
writer lease is released, and refuses a held lease or an existing target. Derived, regenerable,
ephemeral and secret state is never captured. `ea paper backup inspect --backup DIR` re-verifies
the capture against its own manifest; `ea paper restore --backup DIR --run-dir NEWDIR` verifies
first, then materialises a new isolated attempt that reopens through the existing recovery path.
The source backup is immutable and a live workspace is never overwritten in place.

`ea paper alerts evaluate --run-dir DIR` observes nine signals and updates one durable local alert
stream. Alerts carry `alert_id`, `type`, `severity`, `state` (ACTIVE/RESOLVED), `first_seen`,
`last_seen`, `run_id`/`account_id`, `reason` and a required `operator_action`. A repeated
observation updates the existing alert rather than creating another. A signal that cannot be
observed is published as `unavailable`, never as healthy, so an unreadable projection is never
mistaken for a good one. Alerting is observation only: it repairs no ledger, lifts no kill switch,
modifies no reconciliation, restores no trading and resends no order. Alert delivery is the local
stream and CLI inspection; no external paging, chat or metrics service is integrated.
