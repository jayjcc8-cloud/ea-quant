# ADR 0038: Equity Path and Maximum Drawdown V1

Date: 2026-09-12

## Status

Accepted — Issue #189

## Decision

`ea.backtest-equity-path.v1` is a separate canonical `equity-path.json` artifact with
`DERIVED_PATH_EVIDENCE` role. The closed BacktestReportV1 contract and terminal economic
truth remain unchanged. The artifact binds run, scenario, canonical data fingerprint/count,
formal report SHA-256, semantic outcome SHA-256, currency and valuation rule. Repeated
analysis of the same verified completed attempt produces identical canonical JSON bytes.

The bounded single-entry path begins at index 0 with initial funding, timestamped at
`replay_window.start_inclusive`. Each admitted market root contributes one observation after
its existing runtime dispatch completion, including revisions and adjustments in admitted
order. The exact bound CSV row is identified by instrument, interval, adjustment, source,
source sequence, revision and availability time, as in `last-admitted-close-v1`.

The existing reporter verifies funding, Fill, fees, terminal balances and reconciliation.
The committed Fill's ledger handoff dispatch locates the cash/position transition. Before
it, cash equals funding and quantity is zero. At and after it, use verified ending cash
(which already subtracts Fill notional and commission) plus quantity times admitted close
times contract multiplier, using the reporter's canonical decimal arithmetic and currency
quantization. Commission is included exactly once on the committing root. No execution or
ledger is replayed. Final exact path equity must equal formal report equity; otherwise
analysis fails closed.

Maximum drawdown uses every logical observation, including funding. Running peak changes
only for a strictly greater equity; equal peaks retain the earliest peak. Drawdown amount
is running peak minus current equity, and ratio uses the existing 18-decimal-place,
half-even canonical ratio rule. The maximum canonical ratio wins; equal ratios retain the
earliest trough. Amount and peak/trough indices, times and equities describe that selected
pair. A zero-drawdown run uses amount/ratio zero and the initial anchor for both endpoints.
There is no NaN, Infinity or missing-value arithmetic.

Display selection is `uniform-index-extrema-v1`, capped at 2048 points. If the exact
point count is at most 2048, retain all observations. Otherwise reserve the distinct
indices `{0, last, maximum-drawdown peak, maximum-drawdown trough}`. Let `N` be the number
of non-reserved indices and `K = 2048 - reserved_count`. In the ascending sequence of
non-reserved indices, select ranks `floor(i * (N - 1) / (K - 1))` for `i = 0..K-1`.
Union with reserved indices and persist in ascending path order. Selection uses integer
arithmetic. Two passes compute exact extrema then materialize only the bounded display
series. Sampling never defines the risk metric and requires no raw-data persistence.

New Web jobs use `ea.local-web-job.v3` and bind `equity_path_sha256`. Generate and fsync
report, summary and path in a hidden staging directory; rename the complete directory
before atomically storing succeeded job evidence. Analysis failure uses the existing
failed-job semantics; unfinished jobs use existing interrupted semantics after restart.
No analysis recovery frontier or historical migration is added. v1/v2 jobs and reports
remain readable without path evidence, regeneration or rewriting. Completed v3 artifacts
remain readable after external CSV removal/replacement or data-root changes.

Run detail retains terminal metrics and adds an SVG equity curve, exact maximum drawdown
and its peak-to-trough period. Sampled curves are labeled. Browser numeric coordinates
are display only; Python owns the metric. Batch adds exact canonical ratio sorting with
stable missing-last values in either direction. Pair comparison adds drawdown amount
with existing currency compatibility, and directly comparable ratios. Holdout displays
both independent returns and drawdowns without an acceptance/promotion conclusion.

## Boundaries and validation

No strategy, Order, Fill, Ledger, Risk, Matcher, reconciliation or recovery authority
changes. Single-entry, long-only and existing order/fill limits remain. No Candidate,
additional analytics, optimizer, multi-trade, raw-data store, Paper/Live or #125 activation.

Deterministic tests cover flat and rising paths, known OHLCV peaks/troughs, ties, commission,
final equality, repeatability, bounded display/extrema, digest integrity, legacy reads,
publication failure and external data deletion/restart. Existing scenario/strategy/data,
commission and Web compatibility tests remain. Installed-wheel Chromium extends the
full research input flow through all four path evidence surfaces and restart.
