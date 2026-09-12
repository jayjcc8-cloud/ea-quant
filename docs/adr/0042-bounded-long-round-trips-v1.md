# ADR 0042: Bounded Long Round Trips V1

Date: 2026-09-12

## Status

Accepted — Product Owner execution plan and Issue #200. This decision adds an independent
bounded offline lifecycle; ADRs 0040/0041 and all historical versions keep their meanings.

## Contract and ownership

Scenario V5 explicitly binds Action V2, `bounded-long-round-trips-v1`, and integer
`max_round_trips` in 1..256 in its independent canonical identity. There is no implicit bound
inserted into legacy scenarios. The acknowledged position is FLAT or LONG. Existing
PositionViewV2 projects these as FLAT_INITIAL/LONG_OPEN for Action V2 callbacks; FLAT_CLOSED
remains exclusive to the single-round-trip route. No new action vocabulary or economic owner.

FLAT accepts ENTER_LONG with positive quantity; LONG accepts EXIT_LONG for the full acknowledged
quantity. HOLD has no effect. Entry after the completed-trip limit, entry while long, exit while
flat and partial/resized exits fail closed. Pending orders suppress callbacks, including their
Fill root. End of replay never forces an exit. Outcomes are FLAT_NO_TRADE, FLAT_AFTER_TRADES and
OPEN_AT_END. A strategy requesting entry beyond its bound fails, rather than being silently
converted to HOLD.

Keep one signal, planning, risk, order, matcher, ledger and audit authority per attempt. Ordered
full BUY/SELL legs use the existing next-eligible-root matcher and source-issued ingress checks.
Entry risk resize remains allowed; rejected intents fail without a successful result. One pending
order and one position remain the maximum. Ledger sequence is `1 + committed_fill_count`.
Reconciliation checks cash and acknowledged quantity, explicitly including zero after closure.

Recovery recognizes ordered completed economic dispatch evidence and replays deterministic
strategy state and the existing ID owners under the same RunId. Reuse funding, dispatch,
reconciliation and terminal publication frontiers; incomplete/corrupt evidence fails closed.
No new named recovery state, journal, ancestry system or #125 activation.

## Versioned evidence and accounting

Result/Semantic V4 and BacktestReportV3 are independent closed versions. A report contains ordered
complete trades with entry/exit Order and Fill identities, quantity, settled notionals, each
rounded Fill fee and after-fee realized P&L. An optional open position retains its entry evidence,
entry fee, last valuation and gross unrealized P&L. Aggregate realized P&L sums completed trades;
net P&L equals ending cash plus last position value minus initial funding. Open entry fees are
paid expenses, not realized closed-trade P&L. Never round a combined fee instead of each Fill.
All economic projection follows verified committed evidence and existing settlement arithmetic.

Research integration uses Package V3 with existing Action/SDK V2, Path V3 and Web Job V5. Reuse
existing artifact storage and immutable job/report publication. Path V3 uses acknowledged cash
and position after each root across all legs and the unchanged drawdown/tie/display-point rules.
Batch remains membership. Comparison reads persisted formal results with currency-safe deltas
and explicit lifecycle/version differences. Holdout freezes package bytes, strategy, parameters,
lifecycle, bound, funding, risk, fees and instrument, changing only the allowed later data/window.

## Delivery and non-goals

PR A delivers core V5 execution, Result/Semantic V4, Report V3 and multi-dispatch recovery.
PR B delivers Package V3 and research integration after A merges. Real stateful MA crossover
acceptance must show at least three complete trades from an installed non-editable wheel through
Chromium, including restart and reading source/holdout evidence after source/CSV deletion.

Exclude short, reversal, pyramiding, partial fills/exits, concurrent positions, multiple
instruments, optimizer/search, Candidate, Paper/Live/broker, release/deployment and governance
expansion. Retain all legacy routes. One review per implementation PR and one final full verifier
with one classified-blocker repair budget follow the authorized execution plan.
