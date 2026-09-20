### Trusted local strategy artifacts (V1)

`.eastrategy` contains executable Python and should only be loaded from sources the user trusts.
This is **not a Python sandbox**. Configure a dedicated absolute local artifact root; EA does not
accept Web uploads, URLs, remote registries, Python entry points or strategy dependencies.

Create a directory outside the EA checkout containing exactly `manifest.json` and `strategy.py`.
The manifest must be canonical JSON (ASCII-escaped, sorted keys, compact separators, no trailing
newline). Its `schema_version` is `1`, `package_id` is a stable local name, and `strategy` contains
`id`, positive integer `version`, `display_name`, `parameters` and `outcome_mode`. Parameters use
the existing ParameterV1 fields: `name`, `type` (`integer` or `decimal`), `required`, `default`,
`static_minimum`, `static_maximum`. Decimal values are canonical decimal strings.

```python
from decimal import Decimal
from ea.strategy.sdk_v1 import StrategyDecisionV1


def validate_parameters(parameters, context):
    if Decimal(parameters["target_quantity"]) <= 0:
        raise ValueError("quantity must be positive")


class Logic:
    def __init__(self, parameters):
        self.parameters = parameters

    def on_bar(self, bar):
        if bar.close > Decimal(self.parameters["threshold_price"]):
            return StrategyDecisionV1(self.parameters["target_quantity"])
        return None


def create_logic(parameters):
    return Logic(parameters)
```

The public SDK supplies immutable admitted bars (`event_time`, `available_at`, exact Decimal
OHLCV) and validation facts (`history_bar_count`, `last_entry_index`, `quantity_quantum`). It
provides no future data, portfolio or execution handles. A strategy can request at most one long
entry through existing risk and execution controls. Supported outcome modes are
`optional_single_long_entry`, `required_single_long_entry`, and `no_entry`.

```sh
ea strategy pack --source /absolute/my-strategy --output /absolute/strategies/my.eastrategy
ea strategy validate --artifact /absolute/strategies/my.eastrategy
ea strategy inspect --artifact /absolute/strategies/my.eastrategy
```

All three print canonical JSON describing the artifact identity and descriptor. Pack requires a
new output file and preserves input bytes; identical inputs produce identical ZIP bytes. Inspect
and validate check the code contract by loading trusted Python, without running a backtest.

Use Scenario `schema_version: 3` with the existing data/instrument/funding/risk/execution fields
and the following strategy mapping, substituting the SHA returned by pack:

```yaml
strategy:
  source:
    kind: local-package
    package_id: example.threshold
    artifact_sha256: <64-lowercase-hex-artifact-SHA>
  id: local-close-threshold-entry-v1
  version: 1
  parameters:
    threshold_price: '100'
    target_quantity: '2'
```

```sh
ea backtest validate --scenario /absolute/scenarios/local.yaml --strategy-root /absolute/strategies
ea backtest run --scenario /absolute/scenarios/local.yaml --strategy-root /absolute/strategies --output-root /absolute/runs
ea web serve --scenario-root /absolute/scenarios --strategy-root /absolute/strategies --workspace /absolute/workspace --ui-dir /absolute/ui
```

Only immediate `*.eastrategy` files in the configured root are considered. No root means built-ins
only. Duplicate package or strategy IDs, built-in shadowing, digest mismatch, noncanonical ZIP or
manifest, extra members, symlinks and oversized files reject. No `strategy install` is needed.
New strategies require neither EA source edits nor a rebuild of the installed EA wheel.

Web uses the existing generic parameter controls, history, 2–10 member batches and comparison.
History reuse retains the source digest and rejects implementation replacement; comparison labels
different digests as “Strategy implementation changed”. Jobs and attempts retain exact read-only
package bytes. Completed reports and history survive external package deletion. Chronological
Holdout reuses source frozen bytes and parameters with a compatible later Scenario V3 pointing to
that same digest. It never substitutes the currently configured package or target defaults.

## Bounded repeated long round trips

Scenario V5 adds the explicit `bounded-long-round-trips-v1` lifecycle and identity-bound
`strategy.max_round_trips` in `1..256`. Local strategies use Package V3 while keeping the existing
Action V2 `HOLD`, `ENTER_LONG(quantity)` and `EXIT_LONG` contract. The reference source is
[`moving-average-crossover-v3`](../examples/local-strategies/moving-average-crossover-v3); pack,
validate and inspect it with the existing `ea strategy` commands. Preserve the manifest's exact
canonical bytes. The prior V2 example and all legacy scenarios remain supported.

Each acknowledged full exit permits another entry until the bound. Entry while long, exit while
flat, a new action during pending execution, partial/resized exits and entry beyond the bound
fail closed. Replay end never forces an exit. Report V3 lists ordered complete trades and an
optional open position with per-Fill fees; Path V3 uses the actual acknowledged balances at each
market root. Web Job V5 displays these formal values and reuses ordinary Batch, comparison and
chronological Holdout. The bound is frozen with source parameters and package bytes for Holdout.
Persisted evidence remains readable after restart and removal of original source/CSV files.

The installed browser acceptance uses [captured real daily OHLCV](../examples/research-data/README.md)
with the existing MA crossover, including at least three completed round trips. This capability
is offline research only; it does not enable Candidate, optimization, Paper, Live or broker access.

Fractional settlement with both a rounding residual and commission is supported by Issue #203.
Explicit funding/risk bounds still apply. Optional scenario `execution.slippage` also applies to
local packages through the existing matcher; see [ADR 0044](adr/0044-deterministic-adverse-slippage-v1.md).
The [slippage example](../examples/web-scenarios/bounded-long-slippage.yaml) specifies 100 bps of
adverse price movement and 100 bps commission. Both assumptions persist with the run. They are
deterministic research assumptions, not a measured liquidity or venue-execution model.
