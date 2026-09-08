# Local Web deterministic examples

These fixtures demonstrate the supported local offline Web loop. They are deterministic test and
demo inputs, not historical research results. `flat.yaml` creates no orders or fills;
`bounded-long.yaml` targets two units using the same fixed OHLCV input.

`bounded-long-commission.yaml` keeps the original data/strategy and explicitly selects
`execution.commission: {policy: deterministic-commission-v1, commission_bps: '100'}`.
Legacy files remain zero-fee and retain their canonical identities. Fees use actual Fill
price × quantity × multiplier × bps / 10000, rounded per Fill to the currency quantum
with half-even ties. This is a configured offline assumption, not broker-specific pricing.
