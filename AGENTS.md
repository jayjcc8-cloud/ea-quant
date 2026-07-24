# Agent Collaboration Rules

This repository uses proportional expert review with one implementation owner for every iteration.
Git history, ADRs, Issues, pull requests, and CI are the project record; chat history is not.

The default execution mode is one active Implementation Owner. An owner named on an Issue is an
accountability, not a permanently running agent. Review agents are activated only at their gate,
receive a bounded context package, publish a SHA-bound report, and are released after their useful
knowledge is recorded.

## Risk tiers and required roles

Every Issue records one risk tier, the reason for that tier, and the required owners. The highest
applicable trigger wins; uncertainty moves the Issue to the higher tier.

| Tier | Trigger | Required roles |
|---|---|---|
| **Tier 0 — low** | Documentation, comments, or reversible non-executable metadata that does not change an ADR, CI, dependencies, schemas, security, or runtime behavior | Implementation Owner and Verification Owner |
| **Tier 1 — normal** | Executable code, configuration, dependencies, build/CI workflow, public interfaces, or implementation of an already accepted contract | Architecture Owner, Implementation Owner, and Verification Owner |
| **Tier 2 — high** | Data/time semantics, look-ahead behavior, strategy, portfolio/ledger, risk, execution/matching, reconciliation, credentials/security, release, live trading, or external writes | Tier 1 roles plus the relevant data, backtest, risk, security, or release expert |

Architecture and Verification Owners are always read-only. The Implementation Owner is the only
agent or person allowed to edit the active checkout, stage, commit, push, or change iteration
state on GitHub. A coordinator may perform those actions only when it is also the recorded
Implementation Owner.

## Reuse before build

Every new requirement is screened for reuse before design or implementation starts. Prefer a
maintained, mature open-source capability, an existing project dependency, or a small adapter when
it satisfies the requirement better than new bespoke code. Reuse is not automatic: a dependency
must earn its place by fitting the contract and risk profile.

The Issue records a bounded reuse assessment:

- the capability needed and the search sources or existing dependencies inspected
- up to three serious candidates, with supported/locked version, license, maintenance/activity,
  supply-chain and security posture, and technical fit
- integration, transitive-dependency, operational, migration, and lock-in costs
- the decision: reuse directly, wrap with an adapter, or build locally
- rejection reasons for candidates not selected

Tier 0 may record `N/A` with a reason. Tier 1 and Tier 2 require the assessment before
implementation; if no credible candidate exists, record the search date, sources, and why local
implementation is the smaller or safer choice. Do not perform an unbounded survey, add a
dependency only because it is popular, or replace a simple standard-library solution with a
third-party package. Material dependency or boundary decisions receive Architecture Owner review
and, when long-lived, an ADR.

## Controlled expert lifecycle

Every expert activation follows this lifecycle:

1. **Activate at a gate.** Record the concrete question, reviewed base/HEAD, scope, evidence, and
   exit condition. Do not create experts merely to keep a panel resident.
2. **Review bounded context.** Supply the expert context package below, never the complete chat.
3. **Report once.** Return the required report format and finding IDs such as `ARCH-001` or
   `VERIFY-001`.
4. **Extract knowledge.** The Implementation Owner records durable decisions, evidence, blockers,
   and follow-up work in the Issue, ADR, pull request, tests, or documentation.
5. **Release.** A completed or no-longer-useful expert receives no follow-up task and must not
   remain active. Interrupt an expert whose scope or value has expired, after obtaining a concise
   handoff when possible.

Experts must not spawn other agents or expand their scope unless the Issue explicitly authorizes
it. At most two read-only experts run concurrently with the Implementation Owner, and only when
their tasks are independent. Repeated monitoring without new evidence is not an expert task.

## Delegated Approval Owner

The user delegates routine pull-request approval to a read-only Approval Owner so that a compliant
iteration does not require a new chat confirmation at every Ready, merge, and cleanup gate. This
role is
activated only after the final candidate HEAD is frozen and every risk-tier expert has reported.
It does not replace Architecture, Verification, domain, security, risk, or release review.

The bounded evidence package contains:

- the linked Issue, risk tier, acceptance state, scope, non-goals, and writer lease
- base SHA, exact candidate SHA, and complete PR file/diff scope
- every required SHA-bound expert report and open finding ID
- exact-head CI checks, local verification evidence, merge state, reviews, and review threads
- the requested GitHub mutations and whether any mandatory HOLD condition applies

The Approval Owner is read-only. Approval uses three sequential activations:

1. **Ready gate.** While the PR is Draft, the agent returns `APPROVE` or `HOLD` only for the
   Draft-to-Ready mutation.
2. **Merge gate.** After the PR becomes Ready, the agent fetches the current authoritative state
   again and returns a new `APPROVE` or `HOLD` only for squash merge.
3. **Cleanup gate.** After merge, the agent fetches the merged PR, merge commit, target branch,
   linked Issue, source branch, and any enumerated verification worktrees, then returns a new
   `APPROVE` or `HOLD` for an ordered bounded-cleanup plan.

A Ready decision is consumed and invalid as soon as the PR state changes; it can never authorize
merge. A merge decision is consumed by the merge and can never authorize cleanup. The
Implementation Owner remains the only actor allowed to change Git or GitHub state and records each
Approval Owner report in the PR before acting. A new commit, changed base, changed acceptance state,
new review thread, changed check result, or any other unexpected reviewed evidence change
invalidates the current gate decision.

An Approval Owner must return `HOLD` when evidence is missing or contradictory, a blocker is open,
a required verdict is stale, CI is not successful on the exact candidate, or a review thread is
unresolved. Ready and merge gates also require a clean pre-merge state. The cleanup gate instead
requires the PR to be merged and its exact merge commit to be reachable from the expected target
branch. It must also return `HOLD` for:

- a change to the Approval Owner's own authority, prompt, evidence rules, or prohibited actions
- live-trading enablement or a real external order-writing path
- broker/exchange credentials or resolved secret handling
- a production deployment, release, tag, or package publication
- an irreversible data operation, destructive recovery, or action outside the repository scope
- an action that requires a Codex, operating-system, or platform permission prompt

The Approval Owner must obtain mutable PR, review, merge, and CI state through authoritative
read-only GitHub access. Coordinator-supplied summaries, URLs, or run IDs are context, not proof.
If authoritative access is unavailable, the decision is `HOLD`.

Immediately before each authorized GitHub write, the Implementation Owner re-fetches the
authoritative head/base SHA, gate-appropriate PR state, CI conclusions, merge state, reviews, and
unresolved threads. Any unexpected mismatch aborts the write and requires a new report for that
gate. A cleanup report contains an ordered action manifest and expected state transition for each
action; before every cleanup write, completed earlier actions must match that manifest and all
unconsumed preconditions must remain unchanged.

Those cases still require explicit user authorization. The role cannot waive a HOLD condition,
approve its own policy, change required expert findings, or bypass platform permissions. Within the
delegated scope, a recorded Ready-gate `APPROVE` authorizes only Draft-to-Ready. A later recorded
merge-gate `APPROVE` authorizes only squash merge. After GitHub records the merge, a cleanup-gate
`APPROVE` may authorize its enumerated deletion of that PR's merged feature branch, removal of
eligible verification worktrees, and Issue closure when necessary.

Completed verification-worktree cleanup is authorized only when the exact path was enumerated in
the Issue writer lease or PR before review, the worktree is bound to that Issue and reviewed
candidate, and its status is clean. Removal must use non-force `git worktree remove`; a dirty,
missing, shared, unrelated, or ambiguous worktree requires explicit user approval. The user may
revoke this standing delegation at any time.

The reusable context and report contract live in
[`.agents/approval-owner.md`](.agents/approval-owner.md).
The decision and its narrow supersession of ADR 0002 are recorded in
[ADR 0007](docs/adr/0007-delegated-approval-owner.md).

## Writer lease and isolation

Before editing, the Issue or Draft pull request records a writer lease containing:

- Implementation Owner
- branch and checkout/worktree path or identifier
- base branch and base commit SHA
- lease start and handoff status
- merge order when another worktree or branch is active

A checkout has exactly one writer. Read-only experts sharing that checkout may only inspect state;
they must not run commands expected to create caches, environments, build output, or other files.
An expert that executes tests, type checks, builds, or other file-producing verification uses a
dedicated verification worktree (or CI) and still must not modify tracked files, stage, commit,
push, or change GitHub state.

Parallel writers require separate worktrees, separate branches, explicit writer leases, and a
declared merge order. Never mix unrelated cleanup or refactoring into an iteration. Stop when a
worktree contains changes whose ownership or scope is unclear.

## Expert context package

Experts receive only the context required for their review:

- Issue goal, risk tier, explicit non-goals, and owner roles
- base branch, base commit SHA, and exact `reviewed_sha`
- relevant Accepted or Proposed ADRs
- files and modules in scope
- acceptance criteria and verification commands
- current diff or pull request
- known risks, blockers, and open finding IDs

Do not use a complete chat transcript as project context.

## Review validity and output

Every expert report must include:

- reviewed scope and exact `reviewed_sha`
- evidence inspected
- findings with stable IDs classified as blocker, major, or minor
- recommended action and finding owner
- final verdict

A verdict applies only to its exact `reviewed_sha`. A new commit or tracked working-tree change
makes the verdict stale. Re-review receives the previous reviewed SHA, new SHA, open finding IDs,
and the delta between those SHAs; it does not repeat the full context unless the change invalidates
the original assumptions. The final Verification Owner verdict must bind the exact candidate HEAD.

A pull request cannot become ready or merge while any blocker remains open or any required verdict
is stale. A delegated Approval Owner decision cannot be issued until these conditions are already
satisfied.

## Iteration handoff

Handoffs use the Issue, ADRs, commit SHA, pull request diff, verification evidence, extracted expert
knowledge, and unresolved follow-up Issues. The next iteration starts from a clean, synchronized
`main` branch.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the Git and release workflow.
