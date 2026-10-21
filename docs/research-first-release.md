# QUINE first desktop release

The Apple Silicon macOS desktop is an **unpublished local candidate** under
[ADR 0050](adr/0050-desktop-only-product-delivery.md). This page defines how to interpret release
evidence; [STATUS](STATUS.md) owns implemented capabilities, dated checkpoints and remaining work.
Do not maintain a second capability checklist or copy CI totals here.

## Evidence required for a release decision

- **One identified candidate:** source SHA, installed application identity and verification evidence
  must describe the same delivered changes. A local patch is not covered by an older remote CI run.
- **Engineering verification:** use the existing [contributor checks](../CONTRIBUTING.md#required-checks)
  and tier-required review. Full includes coverage thresholds, reproducible wheel builds and
  installed acceptance. Quality, browser E2E and historical server compatibility retain their own
  scopes; none alone is Full or native acceptance.
- **Installed desktop acceptance:** use the existing [first-use card](first-use-acceptance.md) for
  workspace ownership, local data, separate experiment confirmation, frozen reports, native
  lifecycle, focus/keyboard/IME and text scaling. Reuse shared business/API checks; do not create
  a duplicate desktop business suite. A scripted model or synthetic CSV is fixture evidence.
- **Distribution decision:** the owner chooses a version and authorizes signing, notarization and
  publication. Local ad-hoc packages do not establish downloaded-copy Gatekeeper acceptance.
  Build instructions and package limitations belong in [desktop-macos.md](desktop-macos.md).

These are the existing verification and delivery boundaries, not a new approval workflow.
Resolve remaining limitations explicitly; do not convert an unperformed check into a pass.

## Claims and scope

The research workflow can save ideas without a model, use explicitly supplied local data and
produce engine-owned reports. AI requires an explicitly configured provider, can propose or
explain, and cannot replace formal evidence. Apply edits a draft; Candidate acceptance and
execution confirmation remain separate. Unknown responses are not blindly resent.

Mac-local Paper is simulated. Its 72-hour/7-day soak belongs to the applicable Paper-maturity
claim; desktop UI or research checks cannot establish it. External Paper providers, Live,
autonomous optimization/trading and a general untrusted-code sandbox remain outside delivery.
No real-time, all-market, robustness or profitability claim follows from a passing example.

The historical package version is `0.2.0`; its GitHub prerelease is not this desktop candidate.
No next version or tag is selected by this page. Publishing a release, registry package or
announcement and deploying a service require explicit owner authorization.

## Entry points

- Users: [desktop setup](desktop-macos.md), [start research](local-research-start.md),
  [first-use acceptance](first-use-acceptance.md).
- Contributors: [CONTRIBUTING](../CONTRIBUTING.md), [STATUS](STATUS.md),
  [WORKFLOW](governance/WORKFLOW.md).
- Contract references: [strategy packages](local-strategy-package-v2.md),
  [Candidate lifecycle](candidate-lifecycle.md), [Paper operations](local-paper-runtime.md),
  [historical/internal API](radian-local-product.md).
