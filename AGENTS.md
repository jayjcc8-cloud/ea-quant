# Agent Entry and Safety Rules

Git history, merged code/tests/CI, ADRs, Issues, and pull requests are the project record. Chat is
not current-state authority.

## Required Reading

Before work, read [STATUS](docs/STATUS.md), the complete
[WORKFLOW](docs/governance/WORKFLOW.md), the current Issue, and only its directly relevant ADRs,
code, tests, and evidence. Do not reload the full governance history unless resolving a named
dispute.

## Primary objective

Deliver the acceptance criteria of the current product Issue. Governance is a constraint on
delivery, not the deliverable. Prefer the simplest local, reversible implementation that works;
do not introduce governance roles, manifests, approval stages, states, or repository-wide
abstractions unless the Product Owner explicitly requests them.

## Safety floor

- One checkout has **one writer**. Record the writer, branch/worktree, exact base, scope, and merge
  order before tracked edits. Stop on unknown or conflicting changes.
- Classify T0/T1/T2/T3 by actual reachable impact. Router output is advisory. A model may request a
  higher tier only with a reproducible path; the Product Owner decides.
- Live trading, broker/external order writes, secrets/permissions, release/tag/deployment,
  irreversible migration, and destructive data operations require explicit Human Product Owner
  authorization.
- Never rewrite Accepted ADR history. Supersede it in `docs/adr/`.
- Do not create a second plan, task database, state register, or governance control plane.
- Do not weaken runtime tests or economic safety invariants to satisfy cost or schedule.

## Blocking findings

A finding blocks only when all four conditions hold: it is reachable from the current diff or
direct execution path; it is reproducible or has a concrete trace; it violates an explicit
acceptance criterion or creates direct data, permission, financial, or irreversible risk; and it
can become a failing test, minimal reproduction, or concrete exploit. Other findings go to the
Hardening Backlog and do not block.

Do not require proof that all possible defects are absent.

## Review limit

T1–T3 receive one primary review. If blockers exist, perform one concentrated repair and one
verification limited to those blockers and direct regressions. Then the Product Owner chooses
merge, scope reduction, or closure. Do not open another reviewer chain.

## Model routing

Model selection is operator/tooling configuration, not governance. It deliberately stays out of
`.governance/router.yaml`, which prohibits `select_model`; router tier output remains advisory and
unchanged. The concrete role-to-upstream binding is machine-local and is not recorded here.

| Work | Model role | Reasoning effort |
| --- | --- | --- |
| Normal engineering: implementation, refactor with established semantics, code reading, documentation, test maintenance, CLI/UI, routine bug fixes, lint/type repair, repository navigation | Sonnet | High |
| Mechanical, low-risk subtasks once the semantic decision is already made | Haiku or Sonnet | Low–Medium |
| Critical semantic engineering: architecture ownership, runtime or risk authority, accounting/settlement correctness, reconciliation, recovery and idempotency design, broker authority, Paper → Live boundary, major cross-module refactoring, ambiguous irreversible migration or deletion | Opus | Highest available |
| Critical review: release candidate, adversarial review of a critical diff | Opus, fresh context | Highest available |
| Subagents | Sonnet | as parent |

Escalate by **semantic risk, not workload**. A long task, a large diff, many files, many tests, a
large context, or tedium are not escalation grounds on their own.

Critical review uses exactly one fresh-context reviewer, given the work-unit objective, invariants,
exact diff, directly affected implementation and tests, and acceptance criteria — never the
implementer's chain of reasoning or self-justification. Review only for real blockers. Do not build
reviewer-of-reviewer chains or automatic multi-model voting.

## Completion

When acceptance tests pass, the tier-required CI/review is green, and no qualifying blocker
remains, report the change as mergeable. Use [CONTRIBUTING](CONTRIBUTING.md) for commands and update
[STATUS](docs/STATUS.md) or a superseding ADR only when the durable project state changes.
