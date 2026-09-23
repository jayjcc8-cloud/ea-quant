# ADR 0045: Deterministic Execution Latency V1

- Status: Accepted
- Date: 2026-09-20
- Product Issue: #214
- Supersedes: the zero-latency-only restriction of ADRs 0008 and 0018 for an explicit new policy.

## Decision

Optional `execution.latency` uses policy `deterministic-latency-v1` and strict integer
`latency_ms` in [0, 86400000]. This is a bounded offline assumption. Omission preserves every
existing scenario and execution-policy identity; explicit zero is distinct but economically equal.
The new policy identity binds latency and optional commission/slippage, preserving the difference
between omitted and explicitly zero assumptions. Existing policies remain unchanged.

An Order retains its original submission availability and causal root. A matching initial raw
bar must satisfy all existing instrument/root/revision checks and additionally have event time
strictly later than submission availability plus latency. Equality is ineligible. Matching uses
timestamp subtraction to avoid datetime overflow and does not consult wall time, sleep, or look
ahead. The first eligible bar supplies the close; any configured slippage then changes that price,
and per-Fill commission uses the actual final price. This is bar-based execution with a minimum
delay, not an intra-bar, network or venue latency simulation.

Matcher issuance, observation validation, independent fact reconstruction and matcher-history
reconstruction enforce the same bound. Recovery uses original submission timestamps and deterministic
replay, with the existing funding/dispatch/reconciliation frontiers. Pending orders continue to
suppress strategy callbacks. No new scheduler, order identity, recovery frontier or economic
authority is introduced. Strategy entry delay remains a separate pre-submission choice.

## Expiry and evidence

Source exhaustion retains existing matcher expiry facts. A V1 entry-required route whose delayed
order never fills records a classified `order.expired.no_eligible_market_data` product failure and
retains the audit with no successful result/report. This reachable case no longer falls through
to an unspecified internal error. Bounded V4/V5 routes retain their existing no-trade/open-position
terminal semantics, actual committed economics, and no forced exit. Bounds on total round trips
remain strict: an attempted entry beyond the bound still fails.

Frozen Web inputs and compatible Holdout bind the complete execution configuration. Validation,
run details and comparison show latency in milliseconds. A registered example demonstrates
60 seconds: the first one-minute bar is exactly on the boundary and is skipped; the next bar
sets the Fill price. Historical reports need no migration.

## Acceptance and boundaries

Cover exact boundaries, maximum/invalid values, no-fill expiry, combined costs, fractional V1/V5
recovery, local Action V2/V3 packages, immutable Holdout/reopen and a fresh installed-wheel browser
run through report download/refresh. Liquidity, partial fills, stochastic delays, cancellation,
new order types, broker/Paper/Live and publication remain outside this delivery.
