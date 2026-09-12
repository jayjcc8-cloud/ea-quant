# POSITION_LIFECYCLE_ARCHITECTURE_REVIEW_V1

Phase A, Issue #192, 2026-09-12. Survey base: merged main
`94d2769fd8e669c63a43afb565290a0d298cb0f8` (PR #191). Its CI run 34690563472 passed.
The Candidate decision remains architecture-only. This review proposes
[ADR 0040](../adr/0040-single-long-round-trip-position-lifecycle-v1.md); it is not an execution plan
or an assertion that the round-trip product already works.

## CURRENT_STATE / evidence map

Read STATUS, complete WORKFLOW, CONTRIBUTING, Issue #192, inactive #125 and ADRs 0033–0039.
The following are current code findings, not assumptions from the roadmap:

| Seam | Evidence | Consequence |
| --- | --- | --- |
| Strategy entry-only | `strategy/registry.py::EntryLogic.on_market`, `StrategyEntryV1`; `strategy/sdk_v1.py::StrategyDecisionV1` | V1 returns target quantity/None and has no exit intent or position context |
| Local package | `strategy/package.py::validate_package`; `strategy/catalog.py::package_entry` | Manifest V1 has closed entry-only outcome modes; do not silently reinterpret it |
| Product loop | `product/backtest.py::_execute` | Calls strategy only until first target; then drains replay; one retained signal/order/fill |
| Portfolio target | `portfolio/planning.py::_signed_target`, `PortfolioPlanningAuthority.plan` | FLAT maps to zero; positive current position produces SELL of its full quantity |
| Risk | `risk/authority.py::_side_capacity`, `evaluate`; `product/backtest.py::_risk_context` | SELL is understood, but limits can resize; a partial exit must be rejected before submission |
| Matching | `execution/matcher.py::_publish_batch` | Existing side-aware next-bar full-order trade facts and independently calculated fees can support the exit |
| Ledger | `portfolio/ledger.py::_apply_new_fill` settlement logic | Signed BUY/SELL cash/position deltas, fees, hash-chained transactions, no new P&L posting needed |
| Coordinator | `runtime/coordinator.py::begin_next_dispatch`, `_drive_to_window`, `complete_active_dispatch` | Audited Fill/ledger/refresh before open window; explicit completion before next root |
| Result/report | `product/backtest.py::_execute`; `product/reporting.py::_validate_result`, `_report_document` | Singular evidence, buy-only checks, max one Fill, fixed sequence 2, funded-before-every-handoff |
| Path | `product/equity_path.py::generate_equity_path_analysis`; ADR 0038 | Second handoff raises `multiple fill commits unsupported`; post-entry uses ending cash |
| Resume | `product/backtest.py::resume_backtest_attempt`, `_verified_persisted_frontier` | Reconstructs same execution with journal retry, but 'any fill' fixes sequence 2/reconciliation count |
| Journal/store | `experiments/audit.py::PosixAuditJournal.append`; `experiments/store.py::recover_incomplete_attempt` | Exact logical payload retry and verified record prefix; no intrinsic one-Fill counter here |
| Reconciliation | `reconciliation/authority.py::_compare_position_balances`; `product/backtest.py::_observation` | Missing local balance compares as zero; product currently skips position observation when empty |
| Web | `web/service.py::_decode_job`, `report`, `artifact`, `_execute`; `apps/web/src/equity-path.tsx` | Readers bind specific report/path schemas; adapters must version explicitly |

Paths in the table are relative to `src/ea/` except the explicitly named Web app path. Old
product assumptions are more extensive than changing a MAX_FILL_COUNT constant. The lower
runtime coordinator recovery module was also inspected as context; the CLI product resume path
actually reconstructs `_execute`, so this proposal does not require redesigning that module.

## Q1 — STATE_MACHINE / POSITION_MODEL

Freeze the proposed FLAT_INITIAL -> LONG_OPEN -> FLAT_CLOSED path. Only a committed full entry
Fill opens, and a committed full exit Fill closes. Actual filled quantity after risk resize is
the position quantity. Pending-order state is separate. Permit flat never-entered and open-at-end
outcomes without synthetic liquidation. No reentry, short, reversal or concurrent position.

## Q2 — STRATEGY_ACTION_MODEL

HOLD, ENTER_LONG(quantity) and EXIT_LONG(no quantity), through active-market proof and current
snapshot planning. EXIT is the existing FLAT signal, not a negative quantity or arbitrary order.
V2 consumes only eligible active raw revision-0 bars and acknowledged position context. It skips
roots entered with a pending Order; a Fill root cannot also trigger its next action in this
first contract. The small built-in uses entry delay and hold-root count. Execution price/time
remain matcher-owned. Invalid actions reject, they are not silently coerced into HOLD.

## Q3 — contract and LEGACY_MODEL

Choose an explicit action contract V2 and descriptor V2; reuse existing parameter normalization.
Keep SDK/package/descriptor V1 entry-only, including local artifacts and their old calls/results.
The first slice needs one built-in, not new local V2 package authoring. If local V2 is later
included it needs a manifest version, never new meanings for V1. Scenario V4 explicitly binds
new lifecycle/action semantics in a separate digest domain. Existing V1/V2/V3 are unchanged.

## Q4 — ORDER_MODEL / FILL_MODEL

At most two issued Orders, one pending at a time, and two unique full Fills: BUY then equal-q SELL.
Entry risk RESIZE can create a smaller full order; exit RESIZE cannot create a partial close.
Keep the same ID authorities throughout both legs. Use existing order/fact identity validation;
prove illegal third Fill rejection before ledger effects rather than checking only final counts.
Exact duplicate replay is not a new Fill. Expiry is not a Fill and does not change position.

## Q5 — COMMISSION_MODEL

Reuse each Fill's bps/half-even currency-quantized fee. Buy cash delta is -Ne-Ce; sell delta is
+Nx-Cx. Sum fee facts already rounded independently. Do not double entry fees or round combined
notionals. Existing ledger remains the authority, including cash nonnegativity and arithmetic
failure before partial mutation.

## Q6 — ACCOUNTING_MODEL

Reporter derives closed net realized P&L = Nx-Ne-Ce-Cx = ending cash-initial cash. For open
positions, realized P&L is zero, unrealized is Vlast-Ne (gross), and fees paid are separately
reported; net P&L subtracts entry fees. This removes the ambiguity of assigning entry fees to
both realized and unrealized P&L. No generic lot-accounting or analytics owner.

Closed position value is zero and equity equals cash. Verify zero position explicitly as well as
cash/final-equity equality; the current 'empty => skip position observation' needs a bounded new
route change. Ledger sequence is 1/2/3 for funding/entry/exit with no other effects.

## Q7 — REPORT_VERSIONING / PATH_VERSIONING

Choose BacktestReportV2 and EquityPathAnalysisV2. V1 reporter assumptions and ADR 0038's explicit
single-entry valuation prohibit silently widening their contract. Result V3 and semantic outcome
V3 carry ordered leg evidence/economics; old evidence serializers remain intact. V2 path reads
cash/q at each committed transition, not final cash for all post-entry bars.

The maximum-drawdown algorithm is unchanged: reuse `analyze_points`, all roots including funding
and post-exit cash, exact ratio/ties and the existing 2048-point display selection. A versioned
source/valuation contract does not imply redesigning the mathematical algorithm.

## Q8 — OPEN_AT_END

A missing exit trigger (or expired exit Order) leaves LONG_OPEN. Value at last admitted price
using the existing instrument multiplier and currency rules. Do not force exit at last close.
Missing entry is FLAT_INITIAL, not CLOSED. Distinguish failed execution from a valid open result.

## Q9 — RECOVERY_IMPACT

Concrete single-fill assumptions: `_verified_persisted_frontier` treats any complete Fill dispatch
as sequence 2 and expects two reconciliation records. A closed two-Fill trace reaches sequence 3;
copying that classifier would not recognize its reconciliation correctly. `_execute` observes
only the first durable Fill, and reporting compares every handoff's before-snapshot to funding.
These need version-scoped extensions and tests, not relaxation.

Reuse journal logical retries and deterministic reconstruction with the same RunId/code/input.
Classify the bounded ordered dispatch/snapshot chain and observe the existing dispatch_durable
frontier for either leg. No new frontier names or recovery owner. Fault tests must cover after
entry, after exit, terminal publication and corrupt/partial second-leg evidence. Preserve existing
fail-closed unsupported-interruption boundaries. No adjustment, effect repair, generic ancestry,
or #125 activation was identified as a prerequisite. Stop if implementation proves otherwise.

## WEB_IMPACT

Use Web job v4 with versioned report/path bindings and existing atomic publication. Keep old
readers and immutable snapshots. Small Position Outcome section, no redesigned UI. Preserve
currency-safe common report metrics for batch/comparison; Holdout independently runs the exact
same lifecycle/strategy parameters and can produce different open/closed outcomes. Restart reads
published evidence and interrupts unfinished jobs; it does not auto-resume trades.

## ECONOMIC_AUTHORITY_CHANGE

A future implementation deliberately expands offline economic behavior to one reducing SELL.
Risk, matching, fact, ledger, audit and reconciliation owners retain authority. The Phase A diff
has no runtime effect. Paper/Live, broker writes, permissions and release/deployment remain absent.

## Architecture gate and IMPLEMENT_NOW

| Gate | Design assessment | Evidence limit |
| --- | --- | --- |
| ONE_POSITION_ONLY / MAX_FILLS=2 | YES, one instrument and BUY/SELL pair | Requires new route invariant tests |
| SHORTING / REENTRY / PARTIAL_FILL / PYRAMIDING | NO | Must fail before effects, not just in report |
| OLD_STRATEGY_COMPAT / OLD_REPORT_COMPAT / OLD_SCENARIO_COMPAT | YES by separate versions/routes | Existing bytes and behavior must be regression-tested |
| NO_NEW_RECOVERY_SUBSYSTEM | YES, existing reconstruct/retry model | Second-leg faults still need implementation proof |
| PAPER_LIVE_UNCHANGED | YES | Offline research only |
| DETERMINISTIC_EXECUTION | YES as explicit contract | Existing targeted tests are not new-route acceptance |

IMPLEMENTATION_CANDIDATE=YES. ARCHITECTURE_BLOCKED=NO. IMPLEMENT_NOW=NO for Phase A while ADR
0040 is Proposed. This separates a feasible proposal from an accepted economic contract and
from a tested implementation. The requested architecture PR can merge as a proposed decision;
implementation is a separate Issue/worktree/PR after that decision is accepted. Do not interpret
this NO as a discovered need for a recovery subsystem. No runtime slice is delivered here.

## NON_GOALS

No MULTI_ROUND_TRIP, reentry, shorting, reversal, multi-symbol/position, partial fill/exit,
pyramiding, portfolio, optimizer, walk-forward, Monte Carlo, Paper/Live, Candidate runtime,
automatic promotion, new metrics framework, generic state-machine/governance system or #125.

## RepoKeel Retrieval Checkpoint

FOUND=YES; USED=YES; UNDERSTOOD=YES (agent technical assessment). Reused the vault notes on
commission identity/ledger facts, frozen strategy bytes, and relationship-only publication;
verified them against current source rather than treating notes as authority. The vault AGENTS.md
and these bounded notes were read in this conversation; no whole-history scan or new runtime
control plane was introduced.

IMPACT: freeze independently rounded per-leg fees; choose new identity domains instead of changing
old defaults; preserve frozen local artifacts for Holdout; reuse existing atomic evidence staging.
One file cannot substitute for economic dispatch durability, so recovery reasoning follows the
actual journal/ledger chain, not the relationship-only publication example.

NEW_KNOWLEDGE=NO; UPDATE_NEEDED=NO for the knowledge base. The proposed product design belongs
in ADR 0040. Existing knowledge needs no rewrite; no memory/vault write performed.

## Validation performed and delivery scope

26 existing targeted tests passed: the balanced buy/sell ledger round trip, adverse side-specific
matcher price rounding, and all existing `test_backtest_resume.py` cases. These were run against
unchanged base runtime code; they support reuse, not a claim of SINGLE_LONG_ROUND_TRIP_V1 support.
Docs checks and one independent read-only architecture review complete this architecture PR.
No full verifier, new engine test, installed-wheel acceptance or browser run is claimed.

Single writer: primary Codex; worktree `/private/tmp/ea-position-lifecycle-architecture-v1`, branch
`codex/position-lifecycle-architecture-v1`, base as above. Scope: review, Proposed ADR and STATUS
next-phase update. T0 documentation; one architecture Issue/PR/reviewer. Implementation is a
separate product delivery with one writer/reviewer and full-verifier target 1, if accepted.
