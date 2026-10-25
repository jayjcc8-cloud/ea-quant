# Project Status

## Current Phase

**QUINE Mac desktop development baseline — unpublished.** Apple Silicon macOS is the sole
product entry and final acceptance target under [ADR 0050](adr/0050-desktop-only-product-delivery.md).
Shared React/WebView and loopback HTTP remain internal implementation and test surfaces.
**main healthy / live unavailable** describes the merged baseline, not the pending branch CI.
The independent offline v3 studio has not replaced the native product entry.

## Phase Objective

Bring the existing `codex/native-ci-and-architecture` changes into one reviewable draft PR,
synchronize the development branch and follow its exact-head CI. Merge remains a Product Owner
decision; retire this branch only after integration. No release or live connectivity is authorized.

## Completed

- **Engine and evidence:** strict inputs, deterministic funding/costs/risk, reconciliation,
  supported resume, frozen reports and trade analytics; immutable strategies/data, bounded
  batches, comparison, holdout and human Candidate decisions.
- **Desktop research:** model/workspace configuration, controlled CSV import, draft editing,
  reviewable proposals, separate experiment confirmation and independent frozen reports.
  AI cannot replace engine evidence; Apply edits the draft without executing it.
- **Research ownership and lifecycle:** Rust-owned experiments, conversation continuity,
  bounded research grants, retained unknown outcomes, explicit recovery boundaries, cooperative
  native exit and window/text-scaling fixes. Unknown requests are not blindly resent.
- **Merged technical acceptance:** [PR #261](https://github.com/jayjcc8-cloud/ea-quant/pull/261)
  integrated the desktop baseline into main `f684a596c862c98f1c45ec8dc92188c90d65f4fb`.
  Its PR/main CI, unchanged local Full and hosted Full passed. Scoped native input/window/report/
  quit/reopen evidence belongs to the frozen `37bfc0f` source; the later clean main package and its
  installed lifecycle checks are identified in #261. Those results do not validate newer code.
- **Pending desktop branch:** unsent-draft/storage-failure recovery, isolated wheel builds,
  path-driven native CI, architecture documentation, research workbench contracts, retained context
  and report streaming, and shared desktop UI refinements are committed on the current branch.
- **Independent offline v3:** `apps/web/studio/` implements the 39-view design with resettable
  synthetic scenarios and browser-local persistence. [v3 documentation](frontend-v3/README.md)
  links its bounded browser, regression and visual evidence; report and model-editor refinements
  have subsequent focused checks. It has no real provider, account, exchange or engine integration
  and has not replaced the native entry. Local ignored screenshots are not hosted CI evidence.
- **Local simulated Paper:** M1 recovery, M2 risk guards and M3 health/backup/alerts are delivered.
  M4 fault-campaign and gate-preparation tooling exist; see [Paper operations](local-paper-runtime.md).
- **Cleanup:** #259/#260 were closed after containment was verified. Redundant branches and old
  development checkouts were retired; unique prototype source and historical evidence were
  preserved in verified local archives. Current research data and the existing Paper service remain.
- **Historical release:** [EA v0.2.0](https://github.com/jayjcc8-cloud/ea-quant/releases/tag/v0.2.0)
  is the limited offline prerelease, not a QUINE desktop release.

## Incomplete

- Current-branch required remote CI and the Product Owner's merge decision remain separate gates.
  Local tests and earlier SHA results do not establish a remote pass for the final head.
- Offline v3 is not connected to real providers, accounts or the engine. Its synthetic results and
  Chromium input checks do not prove native WebView acceptance or a signed package.
- Actual same-machine OS-reboot recovery, independent first use with a user-owned CSV and normal
  default-workspace launch remain unverified. Real-provider authentication/calls were not exercised
  in this acceptance; scripted responses do not establish those results. Paid calls are optional.
- Developer ID signing, notarization, downloaded-copy installation and public distribution are
  not completed or authorized. The retained technical package is ad-hoc signed.
- Paper 72-hour/7-day soak has no completed pass recorded; an interrupted run is not continuous
  acceptance. External brokers, Live, autonomous optimization/trading and a general untrusted-strategy
  sandbox remain unavailable.

## Blockers

Existing bounded implementation reviews and report/model-editor visual checks reported no
qualifying blocker in their reviewed scope. Current integration still requires its own CI outcome;
do not transfer #261's completed checks to this branch. First-use and distribution gaps continue
to limit release claims. Apply the four-condition rule in [WORKFLOW](governance/WORKFLOW.md).

## Effective ADRs

The [ADR index](adr/) preserves accepted decisions. Current entry points are
[bounded delivery](adr/0029-governance-freeze-and-bounded-delivery.md),
[Candidate authority](adr/0043-research-candidate-runtime-v1.md),
[accepted loading](adr/0047-accepted-candidate-loading-v1.md),
[local Paper](adr/0049-explicit-local-paper-profile.md) and
[desktop-only delivery](adr/0050-desktop-only-product-delivery.md).
The offline prototype does not supersede the accepted desktop delivery boundary.

## Primary Issues

- [Current branch](https://github.com/jayjcc8-cloud/ea-quant/compare/main...codex/native-ci-and-architecture):
  cumulative desktop fixes/native CI and independent offline v3, pending review and integration.
- [#261](https://github.com/jayjcc8-cloud/ea-quant/pull/261): merged desktop integration and
  baseline cleanup, including contained and closed #259/#260.
- [#262](https://github.com/jayjcc8-cloud/ea-quant/pull/262): merged CI-only timeout prerequisite;
  no test, coverage threshold or product behavior change.
- [#249](https://github.com/jayjcc8-cloud/ea-quant/pull/249) and
  [#252](https://github.com/jayjcc8-cloud/ea-quant/pull/252): closed, deferred framework analysis
  and contract drafts. Historical source remains available; no framework support was implemented.
- [#251](https://github.com/jayjcc8-cloud/ea-quant/issues/251): legacy-demo retirement remains a
  separate refactor because backtest, Candidate and Paper use its shared primitives.
- [#217](https://github.com/jayjcc8-cloud/ea-quant/issues/217): bounded latency-label hardening.
  [#241](https://github.com/jayjcc8-cloud/ea-quant/issues/241)'s alert-command and roadmap
  documentation corrections are included in this baseline.

## Last Confirmed

Checked **2026-10-25 (UTC)** against a clean `9603e603e3b50ec1eb81f67cbbb3ba8d0d5bd8ca`
checkout, remote branch `90e7170e64547db36579f2d7af5d5aad3cc8ce43` and merged main `f684a59`.
The branch has a linear history; `4b6096e` adds offline v3, `2095afb` refines the report and
`9603e60` refines model connections. The current PR is the authority for the final pushed head,
validation and CI outcome after this documentation update. For merged behavior, the containing `main` commit
and its CI are the durable checkpoint. A later source commit does not change the
identity of a previously tested installed application.

## Phase Completion Conditions

- [x] Technical source, installed artifact and completed scoped acceptance are identified together.
- [x] Technical evidence has one linked entry; any outstanding required CI remains a merge gate.
- [x] Previously obsolete branches/checkouts were retired with unique history preserved.
- [x] Independent offline v3 and its integration/acceptance boundaries are explicit.
- [ ] Current branch required CI passes and the Product Owner chooses integration or closure.
- [ ] After integration, retire this branch and synchronize local/remote main.
- [ ] Remaining independent-user/native journeys are performed or explicitly excluded from a release claim.
- [ ] Public version, signing, notarization and publication receive explicit authorization.

## Delivery Metrics

Record capability, cost, review/repair count and time to merge in the product PR. Tests, CI and
the PR hold the evidence; STATUS records the present and ROADMAP future boundaries.
