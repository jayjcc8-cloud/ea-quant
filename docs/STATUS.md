# Project Status

## Current Phase

**Phase 1 — Backtest MVP: Incomplete / main healthy / live unavailable.**

When this file is read from merged `main`, the containing `main` commit and its exact CI are the
authoritative implementation checkpoint. STATUS does not predict a future squash SHA and does not
require a docs-only successor to replace one. Mutable Issue and pull-request state is resolved from
GitHub; this file records the product topology and durable checkpoint meaning.

Phase 1 is now a four-delivery product convergence effort. The historical protocol-first
#76/#97/#98/#99 route no longer blocks v0.2.0; its unfinished automatic correction, ancestry
repair, and expanded recovery work belongs to Phase 1.1 under ADR 0027.

## Phase Objective

Deliver a mature offline backtest product that installs from a wheel and deterministically runs or
resumes one funded historical scenario through point-in-time data, strategy, portfolio/risk,
matching, Fill, ledger, audit, fail-closed reconciliation, terminal state, and a stable report.

## Completed

- Phase 0 engineering foundation is released as `v0.1.0`.
- Accepted ADRs 0001–0026 and merged implementation provide typed configuration, deterministic
  market/time visibility, reproducible lineage, strategy/portfolio planning, historical matching,
  execution facts, durable audit, ledger, risk, reconciliation observations, and recovery
  foundations.
- Issue #67 / PR #73 completed the first full Tier 2 protocol loop. Issue #77 / PR #91 completed
  the behavior-equivalent coordinator recovery extraction. Issue #76 D76-001/D76-002 / PR #96
  completed rank-20 reconciliation observation ordering and trace v2.
- Issue #101 / PRs #102/#103 delivered and repaired the dormant #97 structural prerequisite. It
  remains historical evidence and does not activate a product correction route.
- Issue #108 / PR #110 and Issue #111 / PRs #112/#113 repaired Ready separation and hosted-runner
  audit headroom. PR #117 fixed trusted dispatch so the Web job validates its event route before
  checkout. GitHub-hosted Linux CI reclaims only three fixed unused toolchains—the Android SDK,
  .NET, and GHC directories—before the unchanged full-profile headroom gate.
- Issue #106 / PR #114 delivered the bounded Dark Professional Web shell using a deterministic
  read-only Mock Adapter. It adds no API, runtime adapter, database, real data, recovery command,
  or trading mutation.
- ADR 0027 accepts the offline-product, installed-distribution, funding, recovery-support, and
  fail-closed reconciliation boundary. ADR 0028 accepts the Tier 2 four-party delivery model.
- Delivery 2's canonical initial funding, RunManifest v2 installed provenance, and funded
  fail-closed kernel are delivered by this file's containing `main` commit; this does not claim
  the separate CLI, reporting, or release deliveries.

## Incomplete

The ordered delivery sequence is:

1. **Authority and workflow convergence** — Issue #120; two ADRs, STATUS/WORKFLOW/ROADMAP, Router,
   path-aware CI, and governance tests. The containing `main` commit is this delivery's checkpoint.
2. **Initial funding and fail-closed kernel** — add canonical initial cash/genesis outcome,
   RunManifest v2 installed provenance, strict replay/conflict behavior, and supported recovery.
3. **Sample strategies and end-to-end CLI** — fold #82 into #81; deliver `always-flat-v1`,
   `bounded-long-v1`, strict BacktestScenario v1, and installed `validate/run/resume` commands.
4. **Reporting and release readiness** — #83 owns `BacktestReportV1`; #84 owns only clean-main
   acceptance and v0.2.0 release readiness.

Post-merge migration for Delivery 1 must preserve history:

- close PR #105 without rebase or merge;
- close #76, #97, #98, and #99 with terminal `status:superseded`, linking ADR 0027 and Phase 1.1;
- close #109 after the CI convergence is verified;
- move #82's end-to-end acceptance into #81 and close #82 as superseded;
- retain #83 for reporting and #84 for final release readiness only.

Release tag and publication remain unperformed and require separate explicit Human Owner approval.

## Blockers

- Phase 1 cannot close until Deliveries 2–4 are merged and verified on `main`.
- A product PR may freeze at most two candidates; a blocker on candidate two returns to design.
- Any unresolved Fill, duplicate/conflicting economic fact, ledger conflict, corrupt journal,
  incomplete audit chain, or manifest/scenario drift must stop without a successful terminal or
  success report.

The superseded #97→#99 chain is not a v0.2.0 blocker. No blocker authorizes Phase 2, paper/live
trading, real Web integration, automatic correction, release, tag, or publication.

## Effective ADRs

- [ADR index and immutable history](adr/)
- [ADR 0008 — Deterministic execution and reconciliation](adr/0008-deterministic-execution-and-reconciliation.md)
- [ADR 0020 — Durable audit and lifecycle coordinator](adr/0020-durable-audit-and-lifecycle-coordinator.md)
- [ADR 0022 — Audited ledger and reconciliation integration](adr/0022-audited-ledger-and-reconciliation-integration.md)
- [ADR 0024 — Portfolio snapshot reconciliation frontier](adr/0024-portfolio-snapshot-reconciliation-frontier.md)
- [ADR 0025 — Project control plane and authority precedence](adr/0025-project-control-plane-and-authority-precedence.md)
- [ADR 0026 — Pre-implementation Ready and candidate Merge approval](adr/0026-pre-implementation-ready-and-candidate-merge-approval.md)
- [ADR 0027 — Phase 1 offline backtest product boundary](adr/0027-phase1-offline-backtest-product-boundary.md)
- [ADR 0028 — Tier 2 four-party delivery model](adr/0028-tier2-four-party-delivery-model.md)

Merged code/tests/CI describe actual behavior. ADRs describe normative intent. Any conflict is
`DRIFT/BLOCKED`, not an implicit new decision.

## Primary Issues

- [#120 — Authority, workflow, and CI convergence](https://github.com/jayjcc8-cloud/ea-quant/issues/120)
- [#76 — Historical reconciliation parent](https://github.com/jayjcc8-cloud/ea-quant/issues/76)
- [#97 — Historical read-only observation successor](https://github.com/jayjcc8-cloud/ea-quant/issues/97)
- [#98 — Historical Fill/ledger successor](https://github.com/jayjcc8-cloud/ea-quant/issues/98)
- [#99 — Historical ancestry authority successor](https://github.com/jayjcc8-cloud/ea-quant/issues/99)
- [#81 — Deterministic sample strategies and end-to-end backtest](https://github.com/jayjcc8-cloud/ea-quant/issues/81)
- [#82 — Historical end-to-end composition task](https://github.com/jayjcc8-cloud/ea-quant/issues/82)
- [#83 — Result adapter and golden report](https://github.com/jayjcc8-cloud/ea-quant/issues/83)
- [#84 — Phase 1 closeout and v0.2.0 readiness](https://github.com/jayjcc8-cloud/ea-quant/issues/84)
- [#111 — hosted-runner audit headroom](https://github.com/jayjcc8-cloud/ea-quant/issues/111)
  remains verified historical CI evidence.

Control-plane provenance remains Issue #78 / PR #80; that reference does not assert
mutable open, closed, Draft, or merged state.

## Last Confirmed

- Date: **2026-08-30** (Asia/Shanghai).
- Current merged baseline before Delivery 2 is
  `main@969f5f086acd9c28f80e3960a56b83981b1d0e5d`; when this file reaches `main`, the containing
  `main` commit and exact CI replace that pre-candidate reference.
- The last independently verified pre-consolidation checkpoint from **2026-08-22** remains
  `af7bc08cecd70fc5479b92e4393da468727ddb55` as immutable provenance, not current state.
- Issue #106 is closed and its Web shell remains Mock-only. There is no outstanding docs-only
  Gate C action and no authority to start UI-002.
- Historical audit-headroom wording “PR #110 still requires a fresh unchanged-head CI run” and
  “#108 is not thereby complete” is retained only to show the former condition; later merged-main
  evidence satisfied it.
- #76/#97/#98/#99 and PR #105 retain provenance but their unfinished product scope is superseded by
  ADR 0027 after the Delivery 1 GitHub migration.
- Live capability: unavailable and prohibited.

## Phase Completion Conditions

- [x] Core deterministic time, execution, audit, ledger, and recovery foundations exist on `main`.
- [x] Phase 1 product and four-party governance boundaries are accepted in ADRs 0027/0028.
- [x] Canonical initial funding and RunManifest v2 are Done.
- [ ] Installed `ea backtest validate/run/resume` is Done for flat and bounded-long scenarios.
- [ ] Supported resume is equivalent to uninterrupted execution and all named conflicts fail closed.
- [ ] `BacktestReportV1` and cross-process golden evidence are Done.
- [ ] Wheel installation from a temporary directory succeeds without a Git checkout.
- [ ] STATUS, Issues, reviews, and exact-head/main CI agree with zero unresolved threads.
- [ ] Human Owner separately approves and publishes `v0.2.0`.

## Weekly Governance Metrics

The latest completed delivery sample remains the five product/control pull requests #91, #96,
#102, #103, and #114; docs-only synchronization pull requests are excluded.

| Metric | Latest |
|---|---:|
| Accepted pull requests | 5 |
| First-pass acceptance rate | 0% (0/5) |
| Average rework rounds | N/A — historical tasks did not record comparable totals |
| Ready-to-Merge time | N/A — authoritative endpoints were not recorded |
| Merges missing evidence or status updates | 0% (0/5) |
| Token and human-time cost per accepted PR | N/A — no authoritative ledger exists |
| Unresolved decisions existing only in comments | 0 known |
