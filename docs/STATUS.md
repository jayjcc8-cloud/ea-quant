# Project Status

## Current Phase

**QUINE source collaboration preview — desktop candidate unpublished.** Apple Silicon macOS is the sole
product entry and final acceptance target under [ADR 0050](adr/0050-desktop-only-product-delivery.md).
Shared React/WebView and loopback HTTP remain internal implementation and test surfaces.
**main healthy / live unavailable** describes the merged baseline, not the pending branch CI.
The development branch now opens the real local research slice at `/studio`; the independent
offline v3 studio remains a synthetic design preview. Repository-wide licensing is pending.

## Phase Objective

Make the source understandable and reproducible for GitHub contributors through forks, Issues and
draft PRs. Keep the unmerged `feat/v3-real-research` implementation and its acceptance limits clear.
The Product Owner approved the v3 design; implementation acceptance and merging remain pending.
Source collaboration does not require signing, notarization, clean-Mac installation or a Paper
soak. Those remain separate binary/operational claims. No release or live connectivity is authorized.

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
- **Real v3 research on the development branch:** the native `/studio` entry reuses existing
  research, CSV, draft, preview/confirmation and frozen-report services, with Rust-owned experiments.
  It includes report references, retained uncertain requests and next-launch workspace selection.
  Model connection status is read-only; simulation, Live, batches, holdout and Candidate promotion
  are not connected in this entry. Development and self-contained local candidate build modes exist.
- **Independent offline v3:** `apps/web/studio/` implements the 39-view design with resettable
  synthetic scenarios and browser-local persistence. [v3 documentation](frontend-v3/README.md)
  links its bounded browser, regression and visual evidence; report and model-editor refinements
  have subsequent focused checks. It has no real provider, account, exchange or engine integration
  of its own. The native slice reuses its visual components with real service contracts.
  Local ignored screenshots are not hosted CI evidence.
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
- The real v3 implementation still needs Product Owner acceptance. A configured model status or
  scripted adapter test does not prove a successful real-provider call.
- No tracked LICENSE/LICENCE or third-party NOTICE file was found. `quine-core` declares MIT;
  Python and application package metadata do not declare a license. The maintainer must confirm
  repository-wide terms and the copyright notice; no declaration or ownership was changed.
- Actual same-machine OS-reboot recovery, independent first use with a user-owned CSV and normal
  default-workspace launch remain unverified. Real-provider authentication/calls were not exercised
  in this acceptance; scripted responses do not establish those results. Paid calls are optional.
- Developer ID signing, notarization, downloaded-copy installation and binary distribution are
  not completed or authorized. The retained technical package is ad-hoc signed.
- Paper 72-hour/7-day soak has no completed pass recorded; an interrupted run is not continuous
  acceptance. External brokers, Live, autonomous optimization/trading and a general untrusted-strategy
  sandbox remain unavailable.

## Blockers

Existing bounded reviews reported no qualifying blocker in their reviewed scope. Current
integration still requires applicable exact-head CI and the Product Owner's decision; do not
transfer #261 or #263 results to a newer head. The licensing gap prevents a consistent project-wide
open-source license statement. First-use, signing and soak gaps limit their respective binary or
operational claims, not source contribution. Apply [WORKFLOW](governance/WORKFLOW.md)'s four-condition rule.

## Effective ADRs

The [ADR index](adr/) preserves accepted decisions. Current entry points are
[bounded delivery](adr/0029-governance-freeze-and-bounded-delivery.md),
[Candidate authority](adr/0043-research-candidate-runtime-v1.md),
[accepted loading](adr/0047-accepted-candidate-loading-v1.md),
[local Paper](adr/0049-explicit-local-paper-profile.md) and
[desktop-only delivery](adr/0050-desktop-only-product-delivery.md).
The source preview and offline prototype do not supersede the accepted desktop delivery boundary.

## Primary Issues

- [Current development branch](https://github.com/jayjcc8-cloud/ea-quant/compare/main...feat/v3-real-research):
  cumulative desktop work and the real v3 slice, pending implementation acceptance and integration.
- [#263](https://github.com/jayjcc8-cloud/ea-quant/pull/263) remains a draft for
  `codex/native-ci-and-architecture` at `f97a4d0`; it does not contain the later real-v3 commits.
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

Checked **2026-10-28 (UTC)** from the clean source base
`92c6fcdf706bad41442aa4d1b791290632e25d9d` on `feat/v3-real-research`, matching its origin ref
before this documentation edit. [Candidate Full 40638630464](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/40638630464)
passed Python Full and the ordinary frontend job for that candidate. It is not the complete PR CI
set and does not validate this later documentation commit. PR #263 remains on its earlier head.
For merged behavior, the containing `main` commit and its CI are the durable checkpoint.
A source or documentation commit does not change the identity of a previously tested installed app.

## Phase Completion Conditions

- [x] Technical source, installed artifact and completed scoped acceptance are identified together.
- [x] Technical evidence has one linked entry; any outstanding required CI remains a merge gate.
- [x] Previously obsolete branches/checkouts were retired with unique history preserved.
- [x] Offline design preview and native real-v3 implementation boundaries are explicit.
- [ ] Repository-wide license terms and copyright notice are confirmed by the maintainer.
- [ ] Current branch required CI passes and the Product Owner chooses integration or closure.
- [ ] After integration, retire this branch and synchronize local/remote main.
- [ ] Remaining independent-user/native journeys are performed or explicitly excluded from a release claim.
- [ ] Any later binary version, signing, notarization and publication receive explicit authorization.

## Delivery Metrics

Record capability, cost, review/repair count and time to merge in the product PR. Tests, CI and
the PR hold the evidence; STATUS records the present and ROADMAP future boundaries.
