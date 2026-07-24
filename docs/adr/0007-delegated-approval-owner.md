# ADR 0007: Delegated Approval Owner

Date: 2026-07-24

## Status

Accepted

This ADR narrowly supersedes ADR 0002 only where ADR 0002 requires a new user confirmation before
every squash merge. All other ADR 0002 decisions remain Accepted and unchanged.

## Context

ADR 0002 and the controlled expert workflow require CI, current SHA-bound expert verdicts, zero
blockers, and user approval before merge. The evidence gates protect the repository, but repeated
chat confirmation after every completed gate adds no independent technical evidence and makes
long-running iteration unnecessarily dependent on chat context.

On 2026-07-24 the user explicitly requested an agent that can perform routine approval without
asking for confirmation every time. Chat is not a project source of truth, so the delegation,
limits, evidence, and revocation behavior must be durable repository records.

A privileged GitHub Action or bot would add persistent write credentials and an event-processing
attack surface. GitHub required reviews and auto-merge cannot validate this repository's bounded
expert context and report schema. A read-only agent that returns a decision while the existing
Implementation Owner performs mutations preserves least privilege.

## Decision

Establish a repository-native Approval Owner described by `.agents/approval-owner.md`.

- It is read-only and never changes files, Git, GitHub, CI, permissions, or another expert report.
- It activates only after the exact candidate is frozen and all risk-tier experts and checks pass.
- It must directly read authoritative GitHub PR, CI, review, thread, and merge state. If it cannot,
  it returns `HOLD`.
- Its decision is bound to one exact SHA, one evidence state, and one gate.
- The Implementation Owner re-fetches authoritative mutable state immediately before every write;
  any mismatch aborts and requires a new decision.

Approval uses three sequential gates:

1. A Ready-gate `APPROVE` authorizes only Draft-to-Ready.
2. After Ready changes PR state, a new activation and new authoritative evidence are required. A
   merge-gate `APPROVE` authorizes only squash merge.
3. After merge changes PR and target-branch state, a third activation reads the merged PR, merge
   commit, target branch, linked Issue, source branch, and enumerated verification worktrees. A
   cleanup-gate `APPROVE` authorizes only its ordered bounded-cleanup plan.

The Ready decision is consumed by the Ready mutation and can never authorize merge. The merge
decision is consumed by the merge and can never authorize cleanup.

Post-merge branch deletion is limited to the merged PR's feature branch. A verification worktree
may be removed only when its exact path was recorded before review, it is bound to the Issue and
candidate, its status is clean, and non-force `git worktree remove` succeeds. Ambiguous, dirty,
shared, unrelated, or forced cleanup requires explicit user approval.

The cleanup report records an ordered action manifest and the expected state transition for each
action. Before every cleanup write, the Implementation Owner re-fetches authoritative state:
completed earlier actions must match the manifest and all remaining preconditions must be
unchanged. Any unexpected transition aborts cleanup and requires a new report.

The Approval Owner always returns `HOLD` for changes to its own authority or prompt; stale,
missing, contradictory, or coordinator-only GitHub evidence; open blockers; failing or incomplete
exact-head CI; or unresolved review threads. Ready and merge require a clean pre-merge state.
Cleanup instead requires a merged PR and the exact merge commit reachable from the expected target
branch. It also returns `HOLD` for live-trading enablement, real external order writes,
credential/resolved-secret handling, production releases/deployments/tags/publication,
irreversible data operations, destructive recovery, out-of-repository actions, and platform
permission prompts.

Those HOLD cases remain explicit-user gates. The agent cannot weaken the condition, self-approve a
policy change, or bypass a permission prompt. The user may revoke the standing delegation at any
time.

## Consequences

- Routine compliant PRs require three short read-only approval activations but no repeated chat
  confirmation.
- Ready, merge, and cleanup cannot share one potentially stale decision.
- GitHub availability is a deliberate fail-closed dependency for delegated approval.
- The Implementation Owner remains the sole writer and is responsible for execution-time rechecks.
- Changes to this ADR, `AGENTS.md` authority, or the agent template require explicit user approval.

## Validation

- Architecture and Release/Security reviews confirm role separation and fail-closed boundaries.
- Verification confirms `AGENTS.md`, `CONTRIBUTING.md`, and the agent template agree.
- A dry governance review demonstrates separate Ready, merge, and cleanup reports for one unchanged
  candidate SHA and demonstrates `HOLD` when authoritative GitHub access is unavailable.
