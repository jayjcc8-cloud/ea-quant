# Project Status

## Current Phase

**QUINE Mac desktop development baseline — unpublished.** Apple Silicon macOS is the sole
product entry and final acceptance target under [ADR 0050](adr/0050-desktop-only-product-delivery.md).
Shared React/WebView and loopback HTTP remain internal implementation and test surfaces.
**main healthy / live unavailable** does not imply a public desktop release or real-money authority.

## Phase Objective

Maintain one clean local/remote development baseline for the existing desktop implementation.
Complete bounded changes in the current PR, commit verified slices, retire integrated branches,
and reuse acceptance evidence. No new feature, framework integration or release is part of this cleanup.

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
- **Technical acceptance:** one clean source and installed package were identified together.
  Exact-source PR CI, unchanged local Full, installed lifecycle checks and scoped native
  input/window/report/quit/reopen checks passed. Hosted Full and integration CI are tracked
  separately in [PR #261](https://github.com/jayjcc8-cloud/ea-quant/pull/261); only completed,
  successful runs establish a pass and satisfy the merge gate.
- **Local simulated Paper:** M1 recovery, M2 risk guards and M3 health/backup/alerts are delivered.
  M4 fault-campaign and gate-preparation tooling exist; see [Paper operations](local-paper-runtime.md).
- **Cleanup:** #259/#260 were closed after containment was verified. Redundant branches and old
  development checkouts were retired; unique prototype source and historical evidence were
  preserved in verified local archives. Current research data and the existing Paper service remain.
- **Historical release:** [EA v0.2.0](https://github.com/jayjcc8-cloud/ea-quant/releases/tag/v0.2.0)
  is the limited offline prerelease, not a QUINE desktop release.

## Incomplete

- Actual same-machine OS-reboot recovery, independent first use with a user-owned CSV and normal
  default-workspace launch remain unverified. Real-provider authentication/calls were not exercised
  in this acceptance; scripted responses do not establish those results. Paid calls are optional.
- Developer ID signing, notarization, downloaded-copy installation and public distribution are
  not completed or authorized. The retained technical package is ad-hoc signed.
- Paper 72-hour/7-day soak has no completed pass recorded. External brokers, Live, autonomous
  optimization/trading and a general untrusted-strategy sandbox remain unavailable.

## Blockers

The candidate's one primary review and bounded repair verification left no qualifying
implementation blocker. Remaining first-use and distribution gaps limit release claims; they do
not erase the completed engine, local simulated Paper or scoped native evidence.
Apply the four-condition finding rule in [WORKFLOW](governance/WORKFLOW.md).

## Effective ADRs

The [ADR index](adr/) preserves accepted decisions. Current entry points are
[bounded delivery](adr/0029-governance-freeze-and-bounded-delivery.md),
[Candidate authority](adr/0043-research-candidate-runtime-v1.md),
[accepted loading](adr/0047-accepted-candidate-loading-v1.md),
[local Paper](adr/0049-explicit-local-paper-profile.md) and
[desktop-only delivery](adr/0050-desktop-only-product-delivery.md).
This cleanup does not rewrite accepted ADR history.

## Primary Issues

- [#261](https://github.com/jayjcc8-cloud/ea-quant/pull/261): cumulative desktop integration and
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

Checked **2026-10-21 (Asia/Shanghai)**. The frozen technical source is
`37bfc0f2ecb1507f1121f0292b108e01608aa22f`; its installed package was built clean from that commit.
[Candidate PR CI](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/39588177248) passed.
[Hosted Full](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/39608960827) checks that exact
source from trusted main. Package identity, native scope and CI history are maintained in
[#261](https://github.com/jayjcc8-cloud/ea-quant/pull/261).
Documentation and ignore-rule reconciliation do not change the frozen production source.
For merged behavior, the containing `main` commit and its CI are the durable checkpoint.
A later commit does not silently change the tested application's identity.

## Phase Completion Conditions

- [x] Technical source, installed artifact and completed scoped acceptance are identified together.
- [x] Technical evidence has one linked entry; any outstanding required CI remains a merge gate.
- [x] Obsolete development branches/checkouts are retired with unique history preserved.
- [ ] Remaining independent-user/native journeys are performed or explicitly excluded from a release claim.
- [ ] Public version, signing, notarization and publication receive explicit authorization.

## Delivery Metrics

Record capability, cost, review/repair count and time to merge in the product PR. Tests, CI and
the PR hold the evidence; STATUS records the present and ROADMAP future boundaries.
