# ADR 0044: Deterministic Adverse Slippage V1

- Status: Accepted
- Date: 2026-09-20
- Product Issue: #212
- Supersedes: the zero-slippage-only restriction of ADRs 0008 and 0018 for an explicit new policy.

## Decision

Add optional `execution.slippage` with policy `deterministic-slippage-v1` and canonical decimal
`slippage_bps` in [0, 10000). Omission preserves all historical normalized bytes and execution
identities. Explicit zero is a distinct assumption with equal prices. Existing optional
commission remains independently distinguishable from explicit zero commission.

The new execution-policy identity binds both slippage and optional commission, including the
price rule. It is used by existing Order, matcher, fact reconstruction, audit, snapshots and
resume authorities. There is no second execution engine or new recovery frontier.

For the exact binary64 close `p/q`, apply `close +/- abs(close) * bps / 10000` using integer
rational arithmetic: plus for a buy, minus for a sell. Then use the existing nearest-price-tick
rule, resolving a half-tick tie adversely (up for buy, down for sell). Small slippage may round
to the same tick. Price-domain violations and bounded decimal overflow fail closed. No price
is clipped to a candle high/low: this is an explicit cost assumption, not evidence of venue
liquidity or an intra-bar trading path. It does not change next-eligible-bar timing.

The helper retains the existing signed-price domain semantics through `abs(close)`; the public
offline scenario still admits only positive-price instruments and data. A sell that rounds
to zero for a positive-price instrument fails rather than silently changing the policy.

Commission is calculated from the final Fill price with the existing per-Fill currency rounding.
Settlement, cash sufficiency, positions, ledger balance, reconciliation and report valuation
retain their current owners. A slipped Fill that cannot settle successfully cannot publish
a successful report. Order-time estimates do not guarantee later cash sufficiency.

## Research and compatibility

Scenario versions 1 through 5 admit the additive explicit field. Built-ins and local Action
V2/V3 packages traverse the same price and verification path. Web summaries, persisted run
details and comparisons show the slippage assumption. Immutable inputs and compatible Holdout
reuse bind the complete execution configuration. Historical reports need no migration.

Acceptance includes exact buy/sell/tie/domain arithmetic, policy conflicts, fractional settlement,
V1/V5 supported resume, local package paths, frozen Holdout/reopen, and a fresh installed-wheel
browser run through a downloaded formal report. Existing no-slippage identities remain pinned.

## Boundaries

Latency, participation limits, partial fills, stochastic impact, order-type expansion and
calendars remain later bounded deliveries. No broker, Paper/Live, release or deployment is enabled.
