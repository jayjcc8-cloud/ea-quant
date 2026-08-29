# ADR 0026: Pre-implementation Ready and Candidate Merge Approval

Date: 2026-08-29

## Status

Accepted

This ADR supersedes only ADR 0007 clauses that activate Approval Owner Ready after a frozen
candidate or require candidate evidence for Ready. ADR 0007's read-only role, exact-SHA candidate
Merge evidence, bounded Cleanup, HOLD conditions, and all unrelated decisions remain Accepted.

## Context

The lifecycle is `Draft → Ready → In Progress → Review → Verified → Done`, yet the superseded
ADR 0007 wording made Ready depend on implementation artifacts that are not legal until In Progress.
That ordering prevented a lawful Draft-to-Ready transition. The existing report representation already
has `ready | merge | cleanup` gates and `draft_to_ready | squash_merge` mutations, so a fourth gate
or schema change is neither needed nor allowed.

## Decision

### Ready is pre-implementation task-package authority

Ready evaluates a complete Draft task package: objective, scope, non-goals, authoritative inputs,
acceptance criteria, risk tier/rationale, validation, expected outputs, reuse assessment, and owners.
It also requires Architecture review is PASS, budget and any exception are approved, and dependencies
and named blockers are resolved. It does not require a PR, candidate SHA, implementation diff, writer
lease, exact-head tests, or hosted CI. Incomplete acceptance criteria produces `HOLD`; unapproved
budget produces `HOLD`. An APPROVE Ready report authorizes only `draft_to_ready`.

Only after Ready may a distinct Implementation Owner record a branch, worktree, exact base SHA, and
writer lease, then transition the Issue into In Progress. Ready is never candidate approval and is
not reusable as Merge or Cleanup authority.

### Merge remains frozen-candidate delivery authority

Merge requires PR and exact candidate SHA, valid writer-lease history and complete scoped diff,
implementation and acceptance completion, focused and required full tests, hosted CI SUCCESS at exact
candidate HEAD, current required expert/Verification verdicts, scope and budget PASS, current
mergeability/reviews, and zero unresolved review threads. Missing CI or an unfrozen candidate
produces `HOLD`. An APPROVE Merge report authorizes only `squash_merge`.

Cleanup remains the separately activated post-merge gate and its ordered bounded-cleanup authority is
unchanged.

### Tier 0 uses the existing two modes

Tier 0 uses the existing deterministic gate with the report-schema `ready` and `merge` values. A
complete candidate-less Draft task package in `ready` mode receives APPROVE only when the Ready
requirements above pass and authorizes only `draft_to_ready`. Its `merge` mode retains PR, candidate
SHA, hosted CI, scope, and budget checks; it returns HOLD for missing CI or an unfrozen candidate and
authorizes only `squash_merge` on APPROVE.

## Consequences

- Ready is legal before implementation without weakening candidate validation.
- Merge continues to bind a complete frozen candidate and exact-head hosted CI.
- The lifecycle, Router, risk tiers/model routes, default budgets, merge policy, and Cleanup policy
  are unchanged.
- The report schema and gate enum are unchanged; the existing values express the two evidence modes.

## Validation

Governance protocol regressions prove all of the following:

1. A complete Draft package without PR, candidate, lease, or CI can produce Ready APPROVE and only
   `draft_to_ready`.
2. Incomplete acceptance criteria and an unapproved budget each produce Ready HOLD.
3. Candidate evidence is mandatory at Merge; missing CI or an unfrozen candidate produces HOLD.
4. WORKFLOW, the Approval Owner body, this ADR, the deterministic gate, and CONTRIBUTING place Ready
   before implementation consistently.
