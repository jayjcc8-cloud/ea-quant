# Project Status

## Current Phase

**Phase 1 — Backtest MVP: Incomplete / main healthy / live unavailable.**

When this file is read from merged `main`, the containing `main` commit and its exact CI are the
authoritative implementation checkpoint. The latest completed Tier 2 delivery checkpoint is
`main@43faa491f79615ce901fde5b306bf0bcfc1d59f9`; its exact merged-main CI run `32621789347`
succeeded. The completed Tier 2 recovery delivery is preserved by Issue #77 and PR #91; resolve
mutable Issue and pull-request state from GitHub.

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
  `32621789347` succeeded.
- Bounded Issue #77 runtime cleanup/archive completed: the Approval report SHA-256 is
  `198ffc3ae4046de8e4f7ba73fdc87714f25db68bbfc168e8c2d523a50ba27ed6`; the 20-entry archived
  evidence manifest `SHA256SUMS` has SHA-256
  `7c36163ee36b6dfd3a31e2627742d118b7784d7c9ea1684637846f1cdd84ed18`.

## Incomplete

- #76: admit reconciliation observation roots and ancestry resolution.
- [#81](https://github.com/jayjcc8-cloud/ea-quant/issues/81): add concrete sample strategies.
- [#82](https://github.com/jayjcc8-cloud/ea-quant/issues/82): compose a deterministic end-to-end
  historical backtest.
- [#83](https://github.com/jayjcc8-cloud/ea-quant/issues/83): add the result/report adapter and
  golden report.
- Re-run the Phase 1 acceptance suite on merged `main` and publish `v0.2.0`.

## Blockers

- Phase 1 cannot close until #76, sample strategy, end-to-end composition, result/report, and
  release gates complete in order. The completed #77 delivery no longer blocks this chain.

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
- [#81 — Deterministic sample strategies](https://github.com/jayjcc8-cloud/ea-quant/issues/81)
- [#82 — Deterministic end-to-end backtest](https://github.com/jayjcc8-cloud/ea-quant/issues/82)
- [#83 — Result adapter and golden report](https://github.com/jayjcc8-cloud/ea-quant/issues/83)
- [#84 — Phase 1 Closeout and v0.2.0](https://github.com/jayjcc8-cloud/ea-quant/issues/84)

## Last Confirmed

- Date: **2026-08-23** (Asia/Shanghai)
- The last independently verified pre-consolidation checkpoint,
  `af7bc08cecd70fc5479b92e4393da468727ddb55`, remains immutable provenance from **2026-08-22**;
  the current confirmation below records the later #77 delivery and cleanup.
- Latest completed Tier 2 checkpoint:
  `main@43faa491f79615ce901fde5b306bf0bcfc1d59f9`; merged-main CI run `32621789347`
  succeeded.
- Current-main resolution: on merged `main`, use the containing `main` commit and its exact CI;
  from any feature branch, resolve current `main` through GitHub rather than treating that branch's
  candidate SHA as merged reality.
- Issue #77 / PR #91 delivered the behavior-equivalent coordinator recovery extraction. Exact-SHA
  Decision, Adversarial, and Verification evidence records `ADV-001`/`VERIFY-001` FIXED; the
  performance delta is +1.2579% wall / +0.0215% RSS. Runtime cleanup/archive completed under the
  bounded Cleanup Approval report and verified 20-entry archive manifest recorded above.
- Control-plane provenance: Issue #78 / PR #80; this does not assert mutable open, closed, Draft,
  or merged state.
- Live capability: unavailable and prohibited

Update this section whenever merged code/CI, primary Issues, blockers, or Phase completion changes.

## Phase Completion Conditions

- [x] Core governance workflow has completed real Tier 2 loops through Issue #67 / PR #73 and
  the delivered Issue #77 / PR #91 recovery extraction.
- [ ] #76 is Done; the remaining ordered product work follows #76.
- [ ] Concrete sample strategies are Done.
- [ ] Deterministic end-to-end backtest composition is Done.
- [ ] Result/report adapter and golden report are Done.
- [ ] All critical decisions are in ADRs/specifications rather than comments alone.
- [ ] STATUS matches merged code, tests, CI, and open Issues.
- [ ] Phase 1 full verification succeeds on merged `main`.
- [ ] `v0.2.0` is explicitly approved and published.

## Weekly Governance Metrics

The first governed delivery sample is Issue #77 / PR #91. Values without an authoritative source
remain `N/A`, never estimated.

| Metric | Latest |
|---|---:|
| Accepted pull requests | 1 (#91) |
| First-pass acceptance rate | 0% (false) |
| Average rework rounds | 6 tracked implementation retries: five `model-capability`, one `context-assembly` |
| Ready-to-Merge time | N/A — no authoritative Ready and merge interval endpoints were recorded |
| HOLD duration | N/A — no authoritative HOLD interval endpoints were recorded |
| Merges missing evidence or status updates | N/A — the sample has exact merged-main evidence; this bounded STATUS synchronization is its current successor |
| Token and human-time cost per accepted PR | N/A — platform/API exposes no authoritative token counts or human-time ledger |
| Unresolved decisions existing only in comments | 0 known; decisions are not known to exist only in comments |

Two evidence-only `context-assembly` reworks repaired `VERIFY-001` evidence and do not increase
the six implementation retries. Three drift incidents were recorded. Human correction
interventions: 0.
