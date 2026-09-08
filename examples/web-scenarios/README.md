# Local Web deterministic examples

These fixtures demonstrate the supported local offline Web loop. They are deterministic test and
demo inputs, not historical research results. `flat.yaml` creates no orders or fills;
`bounded-long.yaml` targets two units using the same fixed OHLCV input.

`bounded-long-commission.yaml` keeps the original data/strategy and explicitly selects
`execution.commission: {policy: deterministic-commission-v1, commission_bps: '100'}`.
Legacy files remain zero-fee and retain their canonical identities. Fees use actual Fill
price × quantity × multiplier × bps / 10000, rounded per Fill to the currency quantum
with half-even ties. This is a configured offline assumption, not broker-specific pricing.

`moving-average-entry.yaml` and `moving-average-holdout.yaml` use strict Scenario V2 with
`strategy: {id, version, parameters}` and separate six-bar later windows. Their integer
fast/slow windows and canonical decimal quantity are described by StrategyDescriptorV1.
They demonstrate a real MA long-entry path and generic research/holdout parameter handling.
`flat.yaml` remains a CLI fixture and is hidden from the main research selector.
