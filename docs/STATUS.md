# Project Status

## Current Phase

**Phase 1 — Backtest MVP: Incomplete / main healthy / live unavailable.**

When this file is read from merged `main`, the containing `main` commit and its exact CI are the
authoritative implementation checkpoint. The latest completed Tier 2 delivery checkpoint is
Issue #76 D76-001/D76-002 through PR #96: `VERIFIED_ON_MAIN` at
`main@b79e13c91302bd31596126e728f83eb1f8512113`; its exact merged-main CI run `32761371288`
succeeded. The reviewed source head is `3f26be26fdcb79aa26fb983041f4fc57dafd06cd`. The governance
control plane is Complete and remains in Maintenance mode. The completed #77 / PR #91 recovery
delivery is a historical checkpoint superseded as the latest checkpoint by PR #96 merged-main
verification; resolve mutable Issue and pull-request state from GitHub.

## Phase Objective

Deliver a deterministic, restart-equivalent historical backtest path from bounded local OHLCV
selection through strategy, portfolio/risk, matching, ledger/reconciliation, and a stable result
report, with durable audit evidence and no live or external order-writing capability.

## Completed

- Phase 0 engineering foundation released as `v0.1.0`.
- Governance Protocol v1.0 merged through PR #75.
- Typed configuration, canonical market data/time visibility, reproducible lineage, deterministic
  strategy/portfolio planning, historical matching, execution, audit, risk, ledger, and
  reconciliation contracts represented by Accepted ADRs 0001–0024 and merged tests.
- Issue #67 / PR #73 completed the first full Tier 2 Protocol v1 loop. Ledger/reconciliation is
  integrated with coordinator recovery at `main@ac1eb959`; exact-head adversarial, Runtime/Recovery,
  Ledger/Reconciliation, independent Verification, Merge Approval, and merged-main CI passed.
- `ADV-001/002`, `RUNTIME-004/005/006`, and `LEDGER-001/002` are FIXED. CI run `32598577572`
  confirms merged-main quality/full/build/install health.
- Issue #67 bounded cleanup completed under Cleanup Approval R22: the runtime work unit was closed,
  `/private/tmp/ea-issue67-ledger-recovery` was removed non-force, and the exact local and remote
  `codex/67-ledger-coordinator-recovery` refs were deleted. Approval report SHA-256:
  `bfc018aa48305144f47a49f7ceece822a3fcd6ee441fdfcdc76bbf19b26f3e45`.
- Issue #77 / PR #91 completed the behavior-equivalent coordinator recovery extraction at
  `main@43faa491f79615ce901fde5b306bf0bcfc1d59f9`: the coordinator is 2,636 lines and the private
  recovery helper is 1,462 lines. `ADV-001` and `VERIFY-001` are FIXED. The pinned comparison
  measured +1.2579% wall time and +0.0215% RSS, within the 10% threshold; merged-main CI run
  `32621789347` succeeded. Historical checkpoint — superseded as the latest checkpoint by PR #96
  merged-main verification.
- Bounded Issue #77 runtime cleanup/archive completed: the Approval report SHA-256 is
  `198ffc3ae4046de8e4f7ba73fdc87714f25db68bbfc168e8c2d523a50ba27ed6`; the 20-entry archived
  evidence manifest `SHA256SUMS` has SHA-256
  `7c36163ee36b6dfd3a31e2627742d118b7784d7c9ea1684637846f1cdd84ed18`.
- Issue #76 D76-001/D76-002 / PR #96 completed the rank-20 reconciliation-observation root,
  source authority, ordering, and trace-v2 delivery. The reviewed source head
  `3f26be26fdcb79aa26fb983041f4fc57dafd06cd` was squash-merged as
  `main@b79e13c91302bd31596126e728f83eb1f8512113`; the source and squash tracked trees are
  identical. Merged-main CI run `32761371288`, focused tests, governance tests, quality/full,
  coverage, reproducible wheels, isolated installation, and doctor succeeded. `APPROVAL-001/002/003`
  are FIXED; merged-main Verification report SHA-256:
  `44f8670ba240d7b89a5f06eeb6d1e407747344ab5b4b296c995cec19f43a5572`.

## Incomplete

- [#76](https://github.com/jayjcc8-cloud/ea-quant/issues/76) remains the OPEN
  `status:in-progress` + `blocked` parent. D76-001/D76-002 are complete; D76-003..D76-008 remain
  serialized through [#97](https://github.com/jayjcc8-cloud/ea-quant/issues/97) →
  [#98](https://github.com/jayjcc8-cloud/ea-quant/issues/98) →
  [#99](https://github.com/jayjcc8-cloud/ea-quant/issues/99) → final #76 reconciliation.
- #97 is the next ordered successor, but remains `status:draft` + `blocked`, with no writer lease
  or implementation authority. “Next” does not mean unlocked.
- [#81](https://github.com/jayjcc8-cloud/ea-quant/issues/81): add concrete sample strategies.
- [#82](https://github.com/jayjcc8-cloud/ea-quant/issues/82): compose a deterministic end-to-end
  historical backtest.
- [#83](https://github.com/jayjcc8-cloud/ea-quant/issues/83): add the result/report adapter and
  golden report.
- Re-run the Phase 1 acceptance suite on merged `main` and publish `v0.2.0`.

## Blockers

- Phase 1 cannot close until #76 completes #97 → #98 → #99 → final reconciliation, followed by
  sample strategy, end-to-end composition, result/report, and release gates in order. #97 is not
  unlocked. The completed #77 delivery no longer blocks this chain.
- Phase 1 Closeout #84 remains OPEN with `status:ready` + `blocked`.

No blocker authorizes Phase 2 work, live trading, tool migration, or unrelated refactoring.
Governance remains in maintenance mode.

## Effective ADRs

- [ADR index and immutable history](adr/)
- [ADR 0007 — Delegated Approval Owner](adr/0007-delegated-approval-owner.md)
- [ADR 0008 — Deterministic execution and reconciliation](adr/0008-deterministic-execution-and-reconciliation.md)
- [ADR 0020 — Durable audit and lifecycle coordinator](adr/0020-durable-audit-and-lifecycle-coordinator.md)
- [ADR 0022 — Audited ledger and reconciliation integration](adr/0022-audited-ledger-and-reconciliation-integration.md)
- [ADR 0024 — Portfolio snapshot reconciliation frontier](adr/0024-portfolio-snapshot-reconciliation-frontier.md)
- [ADR 0025 — Project control plane and authority precedence](adr/0025-project-control-plane-and-authority-precedence.md)

Merged code/tests/CI describe actual behavior. ADRs describe normative intent. Any conflict is
`DRIFT/BLOCKED`, not an implicit new decision.

## Primary Issues

- Control-plane provenance: [#78](https://github.com/jayjcc8-cloud/ea-quant/issues/78) and
  [PR #80](https://github.com/jayjcc8-cloud/ea-quant/pull/80). This reference does not assert
  mutable open, closed, Draft, or merged state; resolve that state from GitHub.
- [#67 — Ledger/coordinator recovery (Done)](https://github.com/jayjcc8-cloud/ea-quant/issues/67)
- [#87 — #67 STATUS closeout](https://github.com/jayjcc8-cloud/ea-quant/issues/87)
- [#89 — #67 cleanup status synchronization](https://github.com/jayjcc8-cloud/ea-quant/issues/89)
- [#77 — GOV-DEBT-001 coordinator decomposition (delivered by PR #91)](https://github.com/jayjcc8-cloud/ea-quant/issues/77)
- [#79 — GOV-DEBT-002 consolidation file-count exception](https://github.com/jayjcc8-cloud/ea-quant/issues/79)
- [#76 — Reconciliation observation roots](https://github.com/jayjcc8-cloud/ea-quant/issues/76)
- [#97 — Read-only observation and completion successor](https://github.com/jayjcc8-cloud/ea-quant/issues/97)
- [#98 — Execution fact, Fill, ledger, and position/cash successor](https://github.com/jayjcc8-cloud/ea-quant/issues/98)
- [#99 — Order/Fill ancestry authority successor](https://github.com/jayjcc8-cloud/ea-quant/issues/99)
- [#81 — Deterministic sample strategies](https://github.com/jayjcc8-cloud/ea-quant/issues/81)
- [#82 — Deterministic end-to-end backtest](https://github.com/jayjcc8-cloud/ea-quant/issues/82)
- [#83 — Result adapter and golden report](https://github.com/jayjcc8-cloud/ea-quant/issues/83)
- [#84 — Phase 1 Closeout and v0.2.0](https://github.com/jayjcc8-cloud/ea-quant/issues/84)

## Last Confirmed

- Date: **2026-08-25** (Asia/Shanghai)
- The last independently verified pre-consolidation checkpoint,
  `af7bc08cecd70fc5479b92e4393da468727ddb55`, remains immutable provenance from **2026-08-22**;
  the current confirmation below records the later #77 delivery and cleanup.
- Latest completed Tier 2 checkpoint: Issue #76 D76-001/D76-002 / PR #96 is
  `VERIFIED_ON_MAIN` at `main@b79e13c91302bd31596126e728f83eb1f8512113`; merged-main CI run
  `32761371288` succeeded. The reviewed source head is
  `3f26be26fdcb79aa26fb983041f4fc57dafd06cd`.
- Current-main resolution: on merged `main`, use the containing `main` commit and its exact CI;
  from any feature branch, resolve current `main` through GitHub rather than treating that branch's
  candidate SHA as merged reality.
- Issue #77 / PR #91 remains historical delivery evidence for the behavior-equivalent coordinator
  recovery extraction. It is superseded as the latest checkpoint by PR #96 merged-main
  verification; its exact-SHA findings, performance comparison, and cleanup/archive evidence remain
  recorded above.
- Issue #76 remains OPEN with `status:in-progress` + `blocked`. D76-001/D76-002 are complete;
  #97/#98/#99 and final reconciliation remain. #97 is the next ordered successor but remains
  `status:draft` + `blocked` with no writer lease or implementation authority. #84 remains OPEN
  with `status:ready` + `blocked`.
- Control-plane provenance: Issue #78 / PR #80; this does not assert mutable open, closed, Draft,
  or merged state.
- Live capability: unavailable and prohibited

Update this section whenever merged code/CI, primary Issues, blockers, or Phase completion changes.

## Phase Completion Conditions

- [x] Core governance workflow has completed real Tier 2 loops through Issue #67 / PR #73 and
  the delivered Issue #77 / PR #91 recovery extraction.
- [ ] #76 is Done; D76-001/D76-002 are complete, while #97 → #98 → #99 → final reconciliation
  remain in their mandatory order.
- [ ] Concrete sample strategies are Done.
- [ ] Deterministic end-to-end backtest composition is Done.
- [ ] Result/report adapter and golden report are Done.
- [ ] All critical decisions are in ADRs/specifications rather than comments alone.
- [ ] STATUS matches merged code, tests, CI, and open Issues.
- [ ] Phase 1 full verification succeeds on merged `main`.
- [ ] `v0.2.0` is explicitly approved and published.

## Weekly Governance Metrics

The first fully instrumented governed delivery sample remains Issue #77 / PR #91. PR #96 is the
latest verified delivery checkpoint, but values without an authoritative source remain `N/A` and
are never estimated.

| Metric | Latest |
|---|---:|
| Accepted pull requests | 2 (#91, #96) |
| First-pass acceptance rate | 0% (0/2; both required tracked revisions after their first review candidates) |
| Average rework rounds | N/A — #77 recorded 6 tracked implementation retries; #96 did not record a normalized total suitable for averaging |
| Ready-to-Merge time | N/A — no authoritative Ready and merge interval endpoints were recorded |
| Merges missing evidence or status updates | 0% (0/2) — PR #91 and PR #96 have exact evidence, and this STATUS synchronization closes the PR #96 state lag |
| Token and human-time cost per accepted PR | N/A — platform/API exposes no authoritative token counts or human-time ledger |
| Unresolved decisions existing only in comments | 0 known; decisions are not known to exist only in comments |
