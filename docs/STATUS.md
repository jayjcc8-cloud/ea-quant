# Project Status

## Current Phase

**QUINE Mac desktop research candidate — implemented locally, unpublished, acceptance incomplete.**
[ADR 0050](adr/0050-desktop-only-product-delivery.md) makes Apple Silicon macOS the sole product
entry, release artifact and final acceptance target. Shared React/WebView, loopback HTTP and
`ea web serve` remain internal integration and compatibility surfaces.

**main healthy / live unavailable** refers to the verified merged checkpoint below, not to the
newer local candidate. Mac-local simulated Paper is available separately; external Paper
providers and Live are unavailable. VPS verification is outside current desktop delivery.

## Phase Objective

Converge the existing desktop candidate: freeze its source identity, finish the applicable
verification and native acceptance, and obtain a bounded integration/release decision. Reuse
existing evidence and address concrete failures; do not start another implementation of the
research workspace or repeat completed experiments just to recreate evidence.

The product objective remains reducing Idea → Evidence-ready Candidate time and cost. AI can
propose draft changes and explain evidence; Apply, Candidate acceptance and execution confirmation
remain separate actions. Formal results belong to the deterministic engine and frozen inputs.

## Completed

These are implemented capabilities with inherited code/test evidence, not a claim that every path
has been re-accepted on the latest desktop build.

- **Deterministic research engine:** strict local OHLCV/scenario validation, replay-safe funding,
  bounded risk, versioned commission/slippage/latency, execution facts, ledger reconciliation,
  supported durable resume, formal reports, equity paths and trade analytics. The existing
  fail-closed economic and audit authorities remain unchanged.
- **Research evidence:** immutable local strategy packages and data versions, explicit bounded
  batches, pair comparison, chronological holdout, and human ACCEPTED/REJECTED Candidate decisions.
  Candidate loading checks frozen identity; a decision alone grants no execution permission.
  See [strategy packages](local-strategy-package-v2.md) and [Candidate lifecycle](candidate-lifecycle.md).
- **AI research and experiment ownership on merged main:** Rust experiment ownership and the
  bounded research integration landed through PRs #257 and #258. Model-backed work requires
  configured credentials; unknown responses are retained without blind resend. The old statement
  that AI integration is entirely undelivered no longer describes the code.
- **Cumulative desktop branch:** model profiles, workspace settings, controlled CSV import,
  research navigation/editing, proposal Apply/undo/reject, separate experiment confirmation,
  frozen-report presentation, conversation continuity and read-only Paper explanations.
  [PR #261](https://github.com/jayjcc8-cloud/ea-quant/pull/261) carries the cumulative candidate;
  the newer local commits also add unknown-run recovery, bounded research grants, cooperative
  native exit and zoom/layout fixes. These local commits are not covered by the remote PR CI.
- **Mac-local simulated Paper:** M1 recovery/reconciliation/kill-switch, M2 operational risk
  guards and M3 health/backup/alerts are delivered. M4's fault campaign and gate-preparation
  commands are delivered; the RC freeze and 72-hour/7-day soak have no completed gate recorded.
  See [Personal Production V1](personal-production-v1.md), [Paper usage](local-paper.md) and
  [Mac operator tools](local-paper-runtime.md). Existing services remain supported.
- **Historical releases:** Phase 0 `v0.1.0` and the limited offline `v0.2.0` GitHub prerelease.
  The latter binds commit `ebd509e25bca86c00eaa09de7746445ccc891914` and wheel SHA-256
  `04fc44fbbceab47854291e9ef073d15378f496c482df9cd998e8081f525694d6` under
  [release v0.2.0](https://github.com/jayjcc8-cloud/ea-quant/releases/tag/v0.2.0).
  It is not a QUINE desktop release; package-registry publication and deployment were not performed.

## Incomplete

- **Latest candidate verification:** local fixes extend beyond the last remote CI checkpoint.
  Neither that earlier CI nor targeted local checks establish Full acceptance of the current tree.
- **Full verification:** the last attempted Candidate Full was cancelled at its 15-minute limit;
  it did not produce complete coverage, reproducible-wheel and installed acceptance evidence.
  Resolve the existing bounded execution plan before another attempt; do not weaken assertions
  or coverage thresholds, or repeatedly rerun the unchanged timeout.
- **Native and independent-user acceptance:** complete Chinese IME composition/selection,
  keyboard/focus, narrow-window plus text-scaling, and the user-data/model research path are
  not established for one final installed candidate. Earlier isolated native observations and
  browser/fixture checks retain their limited scope. Use the [first-use card](first-use-acceptance.md).
- **Desktop distribution:** local ad-hoc packages are not Developer ID signed/notarized public
  builds. No public desktop release, version/tag decision or external distribution is completed.
- **Scope limits:** no external broker connectivity, Live, autonomous optimization/trading,
  general untrusted strategy sandbox, or robustness/profit claim. Paper maturity additionally
  requires the applicable soak evidence; research workflow checks do not supply it.

## Blockers

The current desktop candidate is not release-ready while its verification and native acceptance
remain incomplete. This does not revoke the delivered offline or local simulated Paper slices.
Unknown-run recovery and exit fixes exist locally; their presence alone does not establish
successful recovery of a user's historical run or whole-candidate acceptance.

Only reproducible findings meeting all four [WORKFLOW](governance/WORKFLOW.md) conditions block
an implementation change. R9/R10 governance provenance remains historical hardening, not an active
review chain. Live/external writes, secrets/permissions, release and destructive operations retain
explicit Human Product Owner authorization.

## Effective ADRs

Use the [ADR index](adr/) for immutable decisions, not a second copied status list. Current entry
points are [bounded delivery](adr/0029-governance-freeze-and-bounded-delivery.md),
[Candidate authority](adr/0043-research-candidate-runtime-v1.md),
[accepted loading](adr/0047-accepted-candidate-loading-v1.md),
[local Paper](adr/0049-explicit-local-paper-profile.md) and
[desktop-only delivery](adr/0050-desktop-only-product-delivery.md).
ADR 0050 withdraws independent browser/server product support while retaining shared runtime,
Mac Paper operations and historical compatibility tooling. Accepted ADR history is unchanged.

## Primary Issues

- [PR #261](https://github.com/jayjcc8-cloud/ea-quant/pull/261) is the existing cumulative desktop
  delivery vehicle, still draft. Its ancestry includes
  [#259](https://github.com/jayjcc8-cloud/ea-quant/pull/259) and
  [#260](https://github.com/jayjcc8-cloud/ea-quant/pull/260); both remain open drafts, not additional
  feature work to reimplement. Their closure/integration awaits the owner's decision.
- [#251](https://github.com/jayjcc8-cloud/ea-quant/issues/251) defers legacy offline-demo retirement.
  Backtest, Candidate, round-trip and simulation paths still import its shared primitives; deleting
  it directly would break the product. Keep the dedicated refactor separate from release cleanup.
- [#241](https://github.com/jayjcc8-cloud/ea-quant/issues/241) and
  [#217](https://github.com/jayjcc8-cloud/ea-quant/issues/217) remain bounded documentation/label
  hardening. Historical Phase 1.1 issue #125 is closed not-planned, not a pending delivery chain.

## Last Confirmed

Checked **2026-10-21 (Asia/Shanghai)** against Git refs, GitHub PR metadata and workflow results:

| Checkpoint | Source identity | Verified scope |
| --- | --- | --- |
| Merged main | `a78d4fee895d0a6fb8530546c43e98090be9f807` | [Main CI success](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/38270906174) |
| Remote PR #261 | `a51eeb8ef71f4487f1ca1cd020b5c99a7546ce02` | [PR CI success](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/39372036213), [historical server compatibility success](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/39372036243); not Full |
| Local candidate before this documentation cleanup | `27e53f7fc59dc986726eb6dee61e8f947c0d23e5` | `feat/quine-research-config`, seven commits ahead of remote; no hosted CI for this SHA |

The [cancelled Full](https://github.com/jayjcc8-cloud/ea-quant/actions/runs/39231781379) tested
`655baca78933af38c4e65ba16887c42ef57972a4`, not the newer remote or local candidate. Its workflow
metadata points at trusted main; that is distinct from the candidate checked out by the job.

When this file is read from merged `main`, the containing `main` commit and its CI are the durable
checkpoint. The table above is a dated observation, not a movable declaration that later commits
have passed. GitHub owns mutable PR/CI state; local screenshots and chat are supporting evidence.

## Phase Completion Conditions

- [x] Historical offline release and bounded local research/Paper capabilities are recorded.
- [ ] Final desktop source and installed artifact are identified and verified together.
- [ ] Required CI/review and Full release verification pass for the applicable candidate.
- [ ] Applicable native/user acceptance has evidence; remaining limits receive an owner decision.
- [ ] Public version, signing/notarization and publication receive explicit authorization.

## Delivery Metrics

Record per product PR: runnable capability, Token cost, review/repair count and time to merge.
Keep evidence once in tests, CI and the PR; do not copy chronological delivery logs into this page.
The bounded review and completion rules remain in [WORKFLOW](governance/WORKFLOW.md).
