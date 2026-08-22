# Project Status

## Current Phase

**Phase 1 — Backtest MVP: Incomplete / main healthy / live unavailable.**

When this file is read from merged `main`, the containing `main` commit and its exact CI are the
authoritative implementation checkpoint. The last independently verified pre-consolidation
checkpoint is `main@af7bc08cecd70fc5479b92e4393da468727ddb55`; its CI succeeded. The #67
candidate is separate, Draft, and blocked until this control-plane iteration merges.

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
- `main@af7bc08` quality/full CI is healthy.
- Issue #67 candidate `1d3a166` is frozen with successful exact-head CI and Runtime/Recovery PASS
  evidence; it remains outside `main`.

## Incomplete

- #67: close `LEDGER-001/002` and complete all Tier 2 gates.
- #77: repay the temporary coordinator size debt through behavior-preserving decomposition.
- #76: admit reconciliation observation roots and ancestry resolution.
- [#81](https://github.com/jayjcc8-cloud/ea-quant/issues/81): add concrete sample strategies.
- [#82](https://github.com/jayjcc8-cloud/ea-quant/issues/82): compose a deterministic end-to-end
  historical backtest.
- [#83](https://github.com/jayjcc8-cloud/ea-quant/issues/83): add the result/report adapter and
  golden report.
- Re-run the Phase 1 acceptance suite on merged `main` and publish `v0.2.0`.

## Blockers

- **#67 / LEDGER-001:** the lifecycle ledger gate remains optional/caller-owned, allowing the
  sealed composition boundary to be bypassed.
- **#67 / LEDGER-002:** a completed failed-refresh retry journal is not restart-equivalent.
- Phase 1 cannot close until #67, #77, #76, sample strategy, end-to-end composition, result/report,
  and release gates complete in order.

No blocker authorizes Phase 2 work, live trading, tool migration, or unrelated refactoring.

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
- [#67 — Ledger/coordinator recovery](https://github.com/jayjcc8-cloud/ea-quant/issues/67)
- [#77 — GOV-DEBT-001 coordinator decomposition](https://github.com/jayjcc8-cloud/ea-quant/issues/77)
- [#79 — GOV-DEBT-002 consolidation file-count exception](https://github.com/jayjcc8-cloud/ea-quant/issues/79)
- [#76 — Reconciliation observation roots](https://github.com/jayjcc8-cloud/ea-quant/issues/76)
- [#81 — Deterministic sample strategies](https://github.com/jayjcc8-cloud/ea-quant/issues/81)
- [#82 — Deterministic end-to-end backtest](https://github.com/jayjcc8-cloud/ea-quant/issues/82)
- [#83 — Result adapter and golden report](https://github.com/jayjcc8-cloud/ea-quant/issues/83)
- [#84 — Phase 1 Closeout and v0.2.0](https://github.com/jayjcc8-cloud/ea-quant/issues/84)

## Last Confirmed

- Date: **2026-08-22** (Asia/Shanghai)
- Last independently verified pre-consolidation checkpoint:
  `main@af7bc08cecd70fc5479b92e4393da468727ddb55`; checkpoint CI successful.
- Current-main resolution: on merged `main`, use the containing `main` commit and its exact CI;
  from any feature branch, resolve current `main` through GitHub rather than treating that branch's
  candidate SHA as merged reality.
- Active product candidate at this confirmation: Draft #73, blocked by `LEDGER-001/002`.
- Control-plane provenance: Issue #78 / PR #80; this does not assert mutable open, closed, Draft,
  or merged state.
- Live capability: unavailable and prohibited

Update this section whenever merged code/CI, primary Issues, blockers, or Phase completion changes.

## Phase Completion Conditions

- [ ] Core governance workflow has completed a real Tier 2 loop.
- [ ] #67, #77, and #76 are Done.
- [ ] Concrete sample strategies are Done.
- [ ] Deterministic end-to-end backtest composition is Done.
- [ ] Result/report adapter and golden report are Done.
- [ ] All critical decisions are in ADRs/specifications rather than comments alone.
- [ ] STATUS matches merged code, tests, CI, and open Issues.
- [ ] Phase 1 full verification succeeds on merged `main`.
- [ ] `v0.2.0` is explicitly approved and published.

## Weekly Governance Metrics

The first durable measurement is the first scheduled weekly review after Issue #78 merges. Until
that review, unavailable values are reported as `N/A`, never estimated.

| Metric | Latest |
|---|---:|
| Accepted pull requests | N/A |
| First-pass acceptance rate | N/A |
| Average rework rounds | N/A |
| Ready-to-Merge time | N/A |
| Merges missing evidence or status updates | N/A |
| Token and human-time cost per accepted PR | N/A |
| Unresolved decisions existing only in comments | 0 identified by the #78 historical audit |
