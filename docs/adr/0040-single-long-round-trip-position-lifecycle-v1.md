# ADR 0040: Single Long Round Trip Position Lifecycle V1

Date: 2026-09-12

## Status

Proposed — Issue #192. This is the Phase A architectural proposal, not runtime support.
Candidate implementation (ADR 0039) remains deferred. Existing Accepted ADRs are unchanged.

## Context and alternatives

The product executor stops calling strategy logic after entry. Its result/report carry singular
order/Fill evidence, and ADR 0038 explicitly defines a single-entry path. Meanwhile existing
Portfolio planning supports a FLAT target, Matcher supports SELL, and Ledger supports balanced
BUY/SELL settlement. A bounded exit can reuse those owners rather than create a trading engine.

Reject widening every V1 validator from one to two: that silently changes old evidence domains
and misses the call-after-entry, position, ancestry and recovery semantics. Reject arbitrary
multi-trade: it would introduce reentry, position populations and a much larger recovery space.
Select an explicit single-round-trip contract alongside the existing entry-only route.

## STATE_MACHINE / POSITION_MODEL (Q1, Q8)

| State | Committed economic evidence | Legal new intent | Replay-end result |
| --- | --- | --- | --- |
| FLAT_INITIAL | Funding; no entry Fill, quantity 0 | HOLD or ENTER_LONG once | FLAT_INITIAL, value 0 |
| LONG_OPEN | One full BUY applied to Ledger; quantity q > 0 | HOLD or EXIT_LONG once | OPEN_AT_END, last admitted valuation |
| FLAT_CLOSED | Matching full SELL applied; quantity 0 | HOLD only | CLOSED, value 0 |

An intent, risk approval or Order does not open/close the position. The existing accepted Fill,
committed audited ledger handoff and risk/portfolio refresh establish the position. Its durable
restart boundary is the associated completed dispatch. q comes from the actual entry Fill after
any permitted entry risk resize, never from an unfilled requested quantity. Exit uses all q.
Position state is a bounded projection of existing facts, not another persisted economic owner.

Track one pending Order separately from position state; this is execution progress, not a fourth
position state. While pending, do not call strategy to solicit another intent. Every new entry or
exit consumes its sole issuance slot; no cancel/replace, retry-as-new-order or repeated signal
loop. Exact idempotent replay of the same identity is not another Order or Fill.

No automatic end-of-replay exit. An untriggered exit yields OPEN_AT_END. No-entry is a valid flat
result for the new optional strategy. Unmatched issued Orders expire through the existing matcher
end-root behavior: an expired entry leaves FLAT_INITIAL; an expired exit leaves LONG_OPEN. Record
expiry as execution outcome, never as a Fill or completed round trip. Runtime/evidence failure,
including a rejected risk intent, remains a failed attempt; do not publish a successful closed
position. Open-at-end is a valid economic outcome, not failed recovery.

## STRATEGY_ACTION_MODEL (Q2, Q3)

Introduce Strategy Contract/SDK V2 explicitly. A closed decision carries action HOLD, ENTER_LONG
or EXIT_LONG. ENTER_LONG alone carries a positive canonical quantity; EXIT_LONG and HOLD carry
no quantity. Reject zero/negative entry quantities, unknown actions, ambiguous payloads and partial
exit requests before issuing a signal. The strategy never supplies Order, price, fees, balance,
Fill, dispatch proof, authority object or filesystem path.

V2 receives an immutable admitted-bar projection and read-only position state/quantity from the
acknowledged ledger view. Only active raw revision-0 market events invoke this first V2 strategy;
path valuation still includes every admitted root. Skip strategy invocation on a root while an
Order was pending on entry to that dispatch, including the root committing its Fill. The first
post-entry decision is therefore on the next eligible raw root. This explicit V2 timing avoids
assuming a submission is a fill; it does not alter existing V1 timing.

For every decision retain the existing runtime active-market proof. ENTER_LONG maps to the
existing LONG signal and portfolio target; EXIT_LONG maps to FLAT (target zero), not SHORT.
Portfolio computes SELL q from the current pinned snapshot. HOLD produces no economic request.
Before signal/order creation reject ENTER in LONG_OPEN/FLAT_CLOSED, EXIT in either flat state,
and any duplicate pending action. The V2 adapter may not silently turn invalid actions into HOLD.

The minimum new built-in is `single-long-hold-roots-v1`, with canonical positive target quantity,
nonnegative entry delay and positive hold-root count. Enter after the declared count of eligible
pre-entry raw roots; after the entry Fill root, count subsequent eligible raw roots and issue
EXIT when the hold count is reached. No trigger before an eligible future fill root is available;
if time runs out, remain open. No stop-loss/take-profit framework. Naming the strategy version 1
does not mean SDK V1: descriptor identity must separately state action contract V2.

Existing built-ins and local package SDK V1 keep their factories, call timing, outcome modes,
identities and serializers. Do not interpret V1 `target_quantity=0` as exit. Preserve descriptor
V1 and parameter types; new descriptor V2 adds explicit action-contract/lifecycle identity while
reusing ParameterV1 normalization. Local V2 packages, if included in implementation, require an
explicit package manifest V2 declaring SDK V2 and the bounded lifecycle; reuse the two-member ZIP,
byte hashing, trusted-local boundary and frozen-artifact handling. Do not extend manifest V1's
closed outcome vocabulary. First implementation may expose the new built-in only; V1 local
package compatibility remains mandatory, and local V2 authoring is not a prerequisite.

## ORDER_MODEL / FILL_MODEL (Q4)

One instrument, one position, at most two Orders and two unique committed Fills: one BUY, then
one SELL of exactly the filled BUY quantity. No short, reentry, reversal, scale-in/out, partial
Fill or partial exit. Entry may accept the current risk RESIZE result: the resulting smaller
Order must still fill in full. Exit must be ALLOW for exactly q; REJECT/RESIZE fails before
submission. Never treat a resized exit as a successful close.

Reuse one signal authority, planner, order authority and existing lifecycle for the entire run;
do not recreate their ID allocators per leg. Evaluate each intent against the current ledger
snapshot through existing risk authority and audit-before-submission gates. Transfer the existing
instrument hold to the exit Order only after entry processing is acknowledged, with no concurrent
pending Order. Maximum order/position/notional limits, initial funding and commission policy stay
in force. The current conservative price-bound risk context can cap both legs; do not bypass it
or create a special risk-free SELL path. No shared Ledger/Matcher relaxation is required.

Matcher continues using its exact next-eligible-bar rule, side-dependent price quantization and
full-order trade facts. SELL does not fill at strategy signal price or on the causal bar. Fact
admission must bind each leg's Order ID, causal/submission evidence, side, quantity and unique
Fill ID. Bounded issuance plus existing order/fact full-fill checks prevent a third economic Fill;
explicitly test third/unknown-order Fill rejection before ledger effects, repeated same-ID replay
as a no-op, and conflicting identities as failures. A post-hoc report count check alone is not
sufficient protection. If current fact admission cannot enforce this, stop implementation rather
than bypass it or activate the generic ancestry work in #125.

## ACCOUNTING_MODEL / COMMISSION_MODEL (Q5, Q6)

Ledger remains cash/quantity/transaction authority; Fill fee facts remain fee authority. The
reporter derives P&L from verified settlement/ledger evidence; it never posts P&L transactions.
With initial cash C0, full entry quantity q, multiplier m, entry/exit settled notionals Ne/Nx,
and independently quantized commission Ce/Cx:

- after entry: cash C1 = C0 - Ne - Ce; quantity q;
- after exit: cash C2 = C1 + Nx - Cx; quantity 0;
- closed net realized P&L = Nx - Ne - Ce - Cx = C2 - C0;
- open gross unrealized P&L = Vlast - Ne; realized P&L = 0; fees paid = Ce;
- open net P&L = gross unrealized P&L - Ce = C1 + Vlast - C0;
- never entered: realized/unrealized P&L and fees all zero.

This is the explicit convention: trade P&L is realized on closure and includes both fees; open
unrealized is gross and entry fees are separately disclosed. Do not assert realized + unrealized
alone equals net P&L while open. Use settled notional/currency arithmetic from existing evidence,
including its rounding rules, not unquantized floating-point q*p*m. Vlast follows the existing
last-admitted-close valuation with instrument multiplier/quantization. Cash must remain nonnegative.

Each Fill independently applies the existing deterministic bps, currency quantum and half-even
commission rule exactly once. Sum already-rounded fee facts; rounding the combined notional is
not equivalent. Zero fee still has its existing fee fact. Exit fees are not inferred as twice
entry fees. Ledger sequence is funding 1, entry 2, exit 3, subject to the bounded no-other-effects
contract. Verify the consecutive snapshot/transaction digest chain and replay ledger equality.

Require cash, quantity and final-equity reconciliation. For FLAT_CLOSED explicitly verify the
expected instrument quantity is zero against the final snapshot and deterministic replay; an
absent zero balance must not be mistaken for 'position never checked'. Use existing observation
semantics where a zero instrument observation is supported; do not add adjustment/correction
commands. Final closed equity equals cash. A mismatch preserves failure evidence, never success.

## REPORT_VERSIONING (Q7)

Choose BacktestReportV2 (`ea.backtest-report.v2`), with a separate closed schema and verifier.
V1 presently requires singular buy order/fill, ledger sequence 2 and a funded-to-final one-step
handoff. Keep that code route and bytes unchanged; do not emit two fills under its name.

V2 retains source/lineage/run/completion/currency and existing summary concepts. It adds a bounded
ordered execution-leg collection with role entry/exit, Order/Fill identities and hashes, side,
quantity, actual fill time/price and fee facts; an unfilled expired leg has no Fill values. It
records position state, final quantity/value, net realized P&L, gross unrealized P&L, total paid
fees, ending cash/equity/net P&L and completed_round_trips (0 or 1). Keep explicit null/absent-leg
semantics, not invented zero exit prices. Every leg must match admitted and committed audit facts.
Report order count may exceed fill count only for a verified expired leg.

New execution result schema V3 replaces singular leg fields only on the new route. New semantic
outcome V3 binds the ordered economic leg projection, lifecycle, fees and terminal balances;
attempt-only IDs/timestamps remain excluded according to existing semantic identity rules.
Scenario V4 has an independent digest domain and explicitly binds action contract V2 and
`single-long-round-trip-v1` lifecycle, strategy identity/parameters/source. It is opt-in; never
inject new defaults/fields into Scenario V1/V2/V3 serialization. Existing attempt manifest identity
machinery already binds canonical scenario/code/runtime; retain it if its closed decoder accepts
the new nested version explicitly. No new attempt-storage subsystem is proposed.

## PATH_VERSIONING

Choose EquityPathAnalysisV2 (`ea.backtest-equity-path.v2`): ADR 0038 expressly binds a single-entry
valuation using ending cash after the sole Fill. Preserve V1 meaning and reader. V2 binds V2 report
SHA and semantic outcome, and uses ordered committed leg handoffs at their admitted market roots:
funding -> entry cash/q -> mark-to-market -> exit cash/zero -> cash-only. Never use terminal cash
for pre-exit points. After each completed market root use its actual acknowledged cash/quantity;
commissions enter on their respective committed Fill roots exactly once.

Reuse `analyze_points` running-peak/trough calculation, canonical ratio precision, earliest tie
rules, 2048-point selection and extrema retention unchanged. Include funding and all post-exit
roots in full-path drawdown. Last path equity must equal the formal report or fail closed. No new
metrics, sampling authority or raw-data archive. Persisted display remains reopenable without
external CSV; regeneration retains current original-input requirements.

## LEGACY_MODEL / WEB_IMPACT

Scenario V1/V2/V3, SDK/descriptor/package V1, BacktestReportV1, path V1 and Web jobs v1/v2/v3
remain readable and unchanged, with their existing execution limits. No migration, regeneration
or rewriting of old attempts. 'Readable' does not promise cross-distribution resume: preserve the
existing exact code/runtime provenance checks. Old strategies continue producing their old route.

New jobs use a versioned Web job v4 envelope binding the new report/path versions and hashes.
Reuse snapshot hashing, hidden report/summary/path staging and atomic succeeded-job publication.
Restart still marks unfinished jobs interrupted, without automatic rerun/resume. Add schema-aware
read adapters and strict version compatibility checks; do not cast a V2 report into a V1 shape.

Keep current Run Detail and curve. Add entry/exit facts, OPEN/CLOSED/FLAT_INITIAL outcome and P&L
with the open-fee convention above. Exits absent at end render '—'. Batch and comparison reuse
common verified equity/net return/drawdown fields with currency-safe deltas; show version and
lifecycle differences rather than inventing comparable execution details across contracts.
Holdout freezes all parameters, lifecycle/action contract, package identity and compatible
funding/risk/fees/instrument; each window independently ends flat/open/closed. No forced exit,
parameter clamping, performance pass/fail or automatic promotion. Existing relations stay minimal.
Future Candidate must explicitly admit the new evidence versions; ADR 0039 is retained, not
silently broadened or implemented here.

## RECOVERY_IMPACT (Q9)

No new recovery subsystem is architecturally required. Current `resume_backtest_attempt` rebuilds
the same deterministic execution with the same identities and journal logical retries; its
completed-attempt path compares reconstructed result/audit/funding bytes. This mechanism is not
intrinsically single-Fill. Reuse it, including original data/code provenance and conflict handling.

The product layer nevertheless requires real changes, not merely MAX_FILLS=2:

- recreate deterministic action state and reuse ID authorities throughout both legs;
- recognize ordered unique accepted/committed/completed dispatch evidence for 0/1/2 Fills;
- derive expected ledger sequence 1/2/3 and final open/closed reconciliation, replacing the
  new route's current hardcoded 'any fill => sequence 2 and two observations' classification;
- observe `dispatch_durable` after either full leg, without creating new named frontiers;
- validate cash and position zero after closing, preserve final terminal publication and exact
  retry semantics, and never classify partial second-leg evidence as reconciled success.

Implement tests interrupting after funding, completed entry dispatch, completed exit dispatch,
reconciliation and terminal-before-publication. Compare same RunId, leg identities, all ledger
transactions, balances, report/path economics and audit chain with uninterrupted execution. Inject
second-leg partial/corrupt/mismatched evidence; fail closed at unsupported boundaries. Do not
promise arbitrary instruction-level or mid-effect recovery, or repair corrupted prior effects.
Existing #125 adjustment, Fill repair and general ancestry/recovery work stays inactive.

The architectural candidate is feasible, but current one-Fill resume tests are not proof that a
future two-Fill implementation works. If implementation encounters a need for new recovery,
repair, generic ancestry authority or any prohibited prerequisite, stop and return to the Product
Owner. Do not weaken existing tests or silently exclude the second dispatch from validation.

## ECONOMIC_AUTHORITY_CHANGE / NON_GOALS

The intended future change is real but confined: offline research may explicitly request one
reducing SELL through existing signal, planning, risk, order, matcher, Fill, ledger and audit
owners. It is not 'economic semantics unchanged'. No authority changes occur in this docs PR.
No broker, remote order write, Paper/Live, permission, release, deployment or real-money activation.

Exclude multiple round trips, reentry, shorts, reversal, multiple symbols/positions, partial fills
or exits, pyramiding, portfolio construction, optimizer, walk-forward, Monte Carlo, new metrics
framework, stop/take-profit framework, Candidate runtime, automatic promotion, generic lifecycle
framework and new governance. `#125_ACTIVATED=NO`.

## IMPLEMENT_NOW / architecture gate

`IMPLEMENTATION_CANDIDATE=YES`: the bounded design has a reuse path through existing owners;
no need for #125 or a new recovery subsystem was found. The architecture constraints are one
position, at most two full Fills, no short/reentry/partial/pyramiding, version-preserved legacy,
deterministic replay, and unchanged Paper/Live.

`IMPLEMENT_NOW=NO` for this Phase A delivery: ADR 0040 remains Proposed and the first change to
economic semantics is presented as a separate architecture decision before the implementation
Issue/PR, as requested. This is an explicit phase boundary, not a claim that a technical blocker
exists. `ARCHITECTURE_BLOCKED=NO`. Accepting the proposed contract can enable the separately
scoped SINGLE_LONG_ROUND_TRIP_V1 implementation; merging a Proposed document does not claim that
the runtime gate has passed or that the feature exists.

Implementation acceptance must include state/adversarial pre-effect tests, independent decimal
accounting oracles, open/closed/never-entered outcomes, ordered handoff/path validation, two-leg
resume faults, legacy compatibility and checkout-external installed-wheel Chromium through
registered data -> entry/exit -> curve/drawdown -> batch/comparison -> Holdout -> restart/reopen.
One full verifier after implementation completion and one independent implementation review,
with at most one concentrated blocker repair verification. No tests or runtime are added here.
