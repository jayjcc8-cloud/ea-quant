# Captured research OHLCV

`coinbase-btc-usd-2024.json` is a captured public Coinbase Exchange BTC-USD daily-candle
response, retrieved on 2026-09-12 from:

https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400&start=2024-01-01&end=2024-05-01

Each exchange row is `[UTC epoch start, low, high, open, close, volume]`. The acceptance helper
sorts the captured rows by timestamp and copies the OHLCV values into the existing raw-bar CSV
contract. Interval end and availability are the following UTC day. No network access occurs in
tests, no prices are invented, and there is no profitability claim.

The source window uses the first 50 rows; the strictly later holdout uses rows 60 through 109.
Both use the existing stateful MA crossover with fast/slow windows 1/2 and Action V2 callback
timing. Package bytes, parameters, commission, funding, instrument and round-trip bound are
frozen by the ordinary scenario/job evidence. This small capture is a reproducible reference
input, not a data service or new dataset authority.

Capture SHA-256: `eedc519966eaa675429240e23043c467d4be111b8ebaacef0d1d942b6beb3e93`.
The installed acceptance uses explicit whole-unit quantities 1 and 2 with initial cash 1,000,000
USD and the same finite risk limits in both windows. Fractional settlement with both a rounding
residual and nonzero commission can hit a pre-existing Ledger posting limit (also reproducible
on Scenario V4); that route fails closed and is not broadened by this capability.
