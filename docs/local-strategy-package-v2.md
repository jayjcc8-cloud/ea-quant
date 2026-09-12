# Local Action V2 strategy packages

Create an entry/exit strategy without editing EA. Copy the two files in
[`examples/local-strategies/moving-average-crossover-v2`](../examples/local-strategies/moving-average-crossover-v2/)
into a directory outside the repository. Edit `strategy.py` and keep `manifest.json` canonical
(sorted keys, compact separators, no trailing newline). The manifest uses schema version 2,
Action Contract V2 and `single-long-round-trip-v1`; it has no `outcome_mode`.

```sh
ea strategy pack --source /absolute/my-strategy --output /absolute/strategies/ma.eastrategy
ea strategy validate --artifact /absolute/strategies/ma.eastrategy
ea strategy inspect --artifact /absolute/strategies/ma.eastrategy
```

The source directory must contain exactly `manifest.json` and `strategy.py`. Inspection returns
strategy metadata and the SHA-256 of the entire deterministic artifact. Pack to a new output path;
existing outputs are not overwritten. V1 continues using the same commands.

Use a registered Scenario V4 with your existing verified instrument/data/funding/risk/execution
configuration. Its strategy section is:

```yaml
strategy:
  id: moving-average-crossover-round-trip-v1
  version: 1
  action_contract: V2
  position_lifecycle: single-long-round-trip-v1
  source:
    kind: local-package
    package_id: example.ma
    artifact_sha256: <SHA-256 returned by inspect>
  parameters:
    fast_window: 1
    slow_window: 2
    quantity: '2'
```

Supply `--strategy-root /absolute/strategies` to `ea backtest validate`, `ea backtest run`, or
`ea web serve`. The existing Web selector discovers the registered scenario and generates its
parameter controls. Select registered local data as usual. Local V2 uses Scenario V4, Report V2,
and Path V2; Batch, comparison, and chronological Holdout use the same immutable inputs.

`validate_parameters(parameters, context)` validates relationships between normalized parameters.
`create_logic(parameters)` creates fresh state for each run. Its `on_bar(bar, position)` returns:

```python
{"action": "HOLD"}
{"action": "ENTER_LONG", "quantity": "2"}
{"action": "EXIT_LONG"}
```

Bar prices are Decimal values. The position is the existing immutable `PositionViewV2` with
FLAT_INITIAL, LONG_OPEN or FLAT_CLOSED and acknowledged quantity. ENTER_LONG is permitted once;
EXIT_LONG closes the full acknowledged position. After closure return HOLD. No strategy callback
occurs while an Order is pending, including its Fill root. The MA example therefore computes
averages over callback bars. EA owns fill prices, risk limits, fees and settlement. At replay end,
no entry is valid; an open position remains OPEN_AT_END without a forced exit.

Only place trusted Python in the configured strategy root. Code is not sandboxed. Dependencies
are not bundled or installed. Changing source code requires a new artifact and SHA-bound scenario;
existing jobs retain the original bytes. Holdout retains those exact bytes, strategy version,
parameters and lifecycle on strictly later compatible data even if external code changes or is
removed. Stored reports and curves reopen after external source removal; rerunning still requires
the original admitted data/runtime dependencies.
