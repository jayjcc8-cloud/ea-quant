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
is stale.

## Iteration handoff

Handoffs use the Issue, ADRs, commit SHA, pull request diff, verification evidence, extracted expert
knowledge, and unresolved follow-up Issues. The next iteration starts from a clean, synchronized
`main` branch.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the Git and release workflow.
