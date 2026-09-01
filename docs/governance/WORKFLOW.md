# EA Governance Workflow

This is the complete delivery workflow. `AGENTS.md` is the short entry point. Governance is a
constraint on delivery, not the deliverable.

## Authority Precedence

Interpret repository information in this order:

1. **merged code, test results, and CI** — implemented reality;
2. **Accepted ADRs and formal specifications** — normative intent;
3. **docs/STATUS.md** — current human-readable state;
4. **Issue and pull-request bodies** — task and candidate scope;
5. **Issue comments, pull-request comments, and chat** — supporting history.

Accepted ADRs are immutable. A later Accepted ADR may supersede a process or decision without
rewriting its history. STATUS records the present; ROADMAP records future phase boundaries.

The Product Owner owns priority, final tier, residual-risk acceptance, merge/stop decisions, and
Token budget. Router is advisory and cannot activate actors, create review chains, or approve a
tier.

## Issue Lifecycle

Use the existing labels as lightweight coordination:

`Draft → Ready → In Progress → Review → Done`

`status:superseded` closes work replaced by a later Issue or ADR. `blocked` is orthogonal. Labels
do not create separate model approvals. An Issue is Ready when its objective, scope, non-goals,
acceptance criteria, tier rationale, validation, and owner are clear enough to implement.

## Task Package

Every active Issue states:

- one objective;
- allowed scope and non-goals;
- verifiable acceptance criteria;
- T0/T1/T2/T3 with actual-impact rationale;
- tests/CI/review required by that tier; and
- Product Owner and Implementer.

Keep the active body under 300 lines. Link evidence instead of copying logs, hashes, or historical
arguments into multiple state sources.

## Risk Tiers

Classify by **actual reachable impact**, not keywords, abstract importance, or a hypothetical
future caller.

| Tier | Actual impact | Default route |
|---|---|---|
| T0 | Docs, developer tools, non-runtime UI, test helpers | Implementer + CI |
| T1 | Research, backtest, offline simulation, analytics, non-authoritative services | Implementer + one Reviewer + CI |
| T2 | Operational order, risk, ledger, recovery, reconciliation, or security authority | Implementer + one independent adversarial Reviewer + exact-head CI |
| T3 | Real-money release, secrets/permissions, irreversible migration | T2 + Human Product Owner approval + staged rollback |

Offline code does not become T2 merely because it uses words such as risk, ledger, or execution.
A model may ask to raise the Product Owner's tier only with a reproducible direct execution path.

Exact-SHA independent review evidence is mandatory only for T2/T3. Legacy Context Bundle/report
schemas remain available but are not gates for T0/T1.

## Writer Isolation

Before tracked edits, record one Implementer, exact base SHA, branch/worktree, allowed scope, and
merge order. A checkout has one writer. Preserve unrelated/user-owned changes and stop on unknown
or conflicting state.

Use test-first development for behavior changes. Create ordinary checkpoint commits when useful;
they are recovery aids, not new approval gates.

## Blocking Findings

A finding blocks only when all four conditions hold:

1. it is reachable from the current diff or its direct execution path;
2. it is reproducible or has a concrete execution trace;
3. it violates an explicit acceptance criterion or creates direct data, permission, financial,
   or irreversible operational risk; and
4. it can be expressed as a failing test, minimal reproduction, or concrete exploit.

Speculative future risks, wording inconsistencies, unreachable edge cases, unrelated legacy
defects, and general hardening go to the **Hardening Backlog**. They do not block the current PR.
A Reviewer may not block because the change lacks proof that all possible defects are absent.

Severity follows consequence in the current reachable product, not governance metadata. A stale
STATUS sentence is not P1; bypassing an operational risk limit or corrupting authoritative state
can be.

## Review Cycle

The finite product loop is:

`Implement → CI → one primary review → one concentrated repair (if needed) → one verification → merge/owner adjudication`

The one verification checks only the primary blockers and direct regressions. It may not reopen a
repository-wide audit or introduce new non-catastrophic blockers. New hardening work becomes a
linked Issue. After this cycle, the Product Owner chooses merge, scope reduction, or closure.

T0 needs no independent model review. T1 uses one Reviewer. T2/T3 use one fresh independent
adversarial Reviewer on exact HEAD. Additional domain advice is non-authoritative unless the
Product Owner explicitly requests it.

## Merge Gates

- **T0:** scoped diff and required CI pass.
- **T1:** acceptance tests and CI pass; one primary review has no qualifying blocker, or its one
  repair verification passes.
- **T2:** T1 plus exact-head CI and one independent adversarial review on that SHA.
- **T3:** T2 plus explicit Human Product Owner approval and a stated rollout/rollback plan.

Unresolved comments block only when they satisfy the four-condition rule. A commit invalidates a
review only when it changes the reviewed blocker or relevant direct execution path.

Direct pushes to `main` are forbidden. Use pull requests and squash merge unless the Product Owner
explicitly selects another recoverable integration method.

## Governance Freeze

Until RESET-001's offline simulated vertical slice merges, do not add governance roles, states,
manifests, schemas, approval phases, plugins, or repository-wide governance abstractions.

A governance change is allowed only when evidence shows that an existing rule:

1. permits a reproducible real operational risk;
2. causes a deterministic wrong merge or data damage; or
3. directly prevents a product PR from reaching a finite decision.

The R9/R10 provenance race is retained as hardening evidence. It does not block an offline T1
vertical slice with no broker, external write, release, or recovery-support claim.

## Evidence and Deferred Work

Keep evidence once, at its natural authority: tests for behavior, ADRs for durable decisions,
STATUS for the present, Issues for backlog, and PRs for candidate discussion. Do not copy the same
CI output or hash into STATUS, Issue, PR, and a second register.

Deferred qualifying work becomes a GitHub Issue. Governance debt may still use the existing
`governance-debt` label, but ordinary technical hardening needs no new governance taxonomy.

## Definition of Done

A task is Done when:

- acceptance criteria are satisfied by merged code and tests;
- tier-required CI and bounded review are complete;
- no four-condition blocker remains;
- STATUS/ADR is updated if durable state changed;
- deferred non-blockers have an owner or linked backlog Issue; and
- the Issue records the final conclusion and closes.

Do not require an extra docs-only PR, Cleanup approval, or proof of universal correctness.

## Delivery Metrics

Track per merged product PR:

- Token cost;
- primary review and repair count;
- Ready-to-Merge time;
- escaped qualifying defects; and
- runnable product capability delivered.

Reviewer finding count is not a success metric.

## Release and Destructive Actions

Live/paper broker connectivity, external order writes, resolved secrets, release/tag/publication,
deployment, irreversible migration, destructive data operations, and platform permission prompts
require explicit Human Product Owner authorization. These controls are not relaxed by tier labels.

Cleanup must name the exact worktree/branch/artifact and preserve historical evidence. Never force
remove ambiguous or dirty state.
