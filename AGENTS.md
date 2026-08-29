# Agent Entry and Safety Rules

Git history, merged code/tests/CI, ADRs, Issues, and pull requests are the project record. Chat is
not a current-state authority.

## Required Reading Order

Before starting a task, read:

1. [Current project status](docs/STATUS.md)
2. [Complete governance workflow](docs/governance/WORKFLOW.md)
3. the task Issue body and its linked ADRs/specifications;
4. only the code and evidence selected by that task package.

Read old comments only to resolve a named dispute or recover provenance. When sources conflict,
apply the authority precedence in WORKFLOW and stop on `DRIFT/BLOCKED` where required.

## Non-bypassable Safety Floor

- One checkout has **one writer**. Only the recorded Implementation Owner edits tracked files,
  stages, commits, pushes, or mutates iteration state.
- Record a writer lease, exact base SHA, branch/worktree, scope, owners, and merge order before
  tracked edits. Stop on unknown, unrelated, or conflicting changes.
- Confirm Tier 0/1/2 in the Issue. Highest semantic trigger wins; Router output is classification
  evidence only and cannot approve a tier or activate an actor.
- Tier 2 role separation is mandatory: Decision/Design, Implementation, Combined Safety Verification, and Merge Approval
  use distinct actor IDs where WORKFLOW requires it.
- Required-model unavailability is `HOLD`; **silent downgrade is forbidden**. Never use a cheap
  model → stronger model rescue chain as the default workflow.
- Every review uses an actor-specific FROZEN Context Bundle and an exact-SHA schema-valid report.
  A new commit, changed scope/base/rules, or tracked edit makes the affected verdict stale.
- A blocker, stale required verdict, failing/incomplete exact-head CI, or unresolved review thread
  forbids Ready and merge.
- Live trading, external order writes, resolved secrets, release/tag/deployment, destructive data
  operations, changes to Approval Owner authority, and platform permission prompts require
  explicit Human Owner authorization.
- Never rewrite Accepted ADR history. Supersede it with a later ADR. `docs/adr/` is the only ADR
  directory.
- Do not create a second plan, task database, state document, governance register, or Router
  authority. Governance debt exists only as labelled GitHub Issues.
- Do not weaken tests, evidence, semantic review, or safety contracts merely to satisfy a size,
  cost, or schedule gate.

## Execution Contract

Follow [WORKFLOW](docs/governance/WORKFLOW.md) for Issue lifecycle, task packages, risk/model routes,
reuse assessment, budgets, context/report schemas, expert lifecycle, approval gates, Definition of
Done, weekly metrics, and cleanup.

Use [CONTRIBUTING](CONTRIBUTING.md) for the supported local commands. Update
[STATUS](docs/STATUS.md) or an ADR whenever a completed change would otherwise leave the control
plane stale.
