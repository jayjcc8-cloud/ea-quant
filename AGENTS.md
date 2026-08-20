# Agent Collaboration Rules

This repository uses proportional expert review with one implementation owner for every iteration.
Git history, ADRs, Issues, pull requests, and CI are the project record; chat history is not.

The default execution mode is one active Implementation Owner. An owner named on an Issue is an
accountability, not a permanently running agent. Review agents are activated only at their gate,
receive a bounded context package, publish a SHA-bound report, and are released after their useful
knowledge is recorded.

## EA Governance Protocol v1.0

This file is the repository policy authority for Protocol v1.0. The files under `.governance/`
are versioned rules, schemas, and role prompts that implement this policy; they do not replace an
Issue, ADR, pull request, Git history, or CI result as project authority.

The governance Router is a **Classification Evidence Provider**, never a Decision Authority. It
may produce `minimum_tier_candidate`, evidence, ambiguity, lineage, and routing recommendations.
It must not emit `approved_tier`, `confirmed_tier`, mutate GitHub, or activate an agent. The
confirmed risk tier remains in the authoritative Issue. A human may always raise the tier; lowering
the Router candidate requires explicit Human Owner authorization and a SHA- and scope-bound Sol
report.

## Risk tiers and required roles

Every Issue records one risk tier, the reason for that tier, and the required owners. The highest
applicable trigger wins; uncertainty moves the Issue to the higher tier.

| Tier | Trigger | Required roles |
|---|---|---|
| **Tier 0 — low** | Documentation, comments, or reversible non-executable metadata that does not change an ADR, CI, dependencies, schemas, security, governance authority, or runtime behavior | Implementation Owner plus deterministic verification and approval gates; no review model is required |
| **Tier 1 — normal** | Executable code, configuration, dependencies, build/CI workflow, public interfaces, or implementation of an already accepted contract | Architecture Owner, Implementation Owner, and Verification Owner |
| **Tier 2 — high** | Data/time semantics, look-ahead behavior, strategy, portfolio/ledger, risk, execution/matching, reconciliation/recovery, canonical serialization or digests, credentials/security, release, live trading, or external writes | Sol Decision Owner, Implementation Owner, Adversarial Reviewer, independent Verification Owner, Approval Owner, and the relevant domain expert |

Architecture and Verification Owners are always read-only. The Implementation Owner is the only
agent or person allowed to edit the active checkout, stage, commit, push, or change iteration
state on GitHub. A coordinator may perform those actions only when it is also the recorded
Implementation Owner.

## Classification evidence and change graph

The declarative rule catalog is [`.governance/router.yaml`](.governance/router.yaml). During
Protocol v1.0 Phase 0 it is applied manually. Automation may be added only by a later Issue after
the Issue 67 retrospective.

Classification evidence records:

- rule and input hashes, evidence completeness, ambiguity, and every matched rule
- Issue lineage, related pull requests, branch history, and recent merged changes on the same
  semantic surfaces
- cumulative contract-change declarations, even when work is split across commits or pull
  requests
- changed paths plus public API, schema, canonical bytes, digest, error, ADR, CI, security,
  release, and external-write surfaces
- the minimum tier candidate, required roles, requested model profiles, and Luna eligibility

Issue wording never overrides actual diff or change-graph evidence. A candidate diff is classified
again after it is frozen. A higher result invalidates the previous context package and every report
whose required role set is now incomplete.

Production additions and deletions are measured separately. The default budgets are:

- pull request: at most 1,200 production additions, 1,000 production deletions, 3,000 total
  additions, and 15 changed files
- atomic commit: at most 400 production additions, 400 production deletions, 1,000 total
  additions, and 8 changed files
- new Python module: at most 1,500 lines

More than 1,000 deleted production lines creates a Tier 2 candidate. A size exception is agreed
before implementation and binds the base SHA, files, maximum budget, and rationale; it never skips
semantic review or verification.

## Model routing, capacity, and availability

Route each task directly to the lowest model profile expected to complete it once. Never use a
Luna-to-Terra-to-Sol rescue chain.

- Tier 0 uses Terra medium for implementation and deterministic tools for verification and the
  approval decision. Automatic approval never performs a merge or any GitHub write.
- Tier 1 normally uses Terra high. Sol high owns a Tier 1 architecture decision only when it
  changes a public boundary, dependency decision, ADR, or governance authority.
- Tier 2 runs sequentially: Sol xhigh Decision Owner, Terra high Implementation Owner, independent
  Terra high Adversarial Reviewer, independent Sol high Verification Owner, then an independent
  Sol high Approval Owner.
- Decision, implementation, adversarial, verification, and approval actors use distinct actor and
  work-unit IDs where separation is required. Decision and verification, decision and approval,
  verification and approval, and implementation and adversarial review must never share an actor.

At most two Sol roles may be active for this repository, and at most one Sol role may be active for
one work unit. Excess work is `QUEUED`, not downgraded. Existing limits on concurrent read-only
experts still apply.

If a required Sol or Terra profile is unavailable, rate-limited, retired, or cannot complete its
gate, the result is `HOLD`; another profile must not silently take over. A temporary substitution
requires explicit Human Owner authorization and a governance-debt Issue. A Sol-required Tier 2
gate cannot be replaced by Terra or Luna. Luna is optional, non-authoritative, and eligible only
when raw evidence exceeds about 80,000 tokens, 20 files, or 10,000 lines. Luna output must be
schema-checked and discardable; Luna unavailability never blocks the gate.

The canonical role prompts live under [`.governance/prompts/`](.governance/prompts/). A role report
binds the prompt ID, version, and SHA-256. Prompt changes are governance changes, not invisible
session customization.

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

Tier 2 roles are sequential unless their bounded questions are provably independent. The Decision
Owner must be released before the independent Verification Owner is activated. The Approval Owner
checks governance evidence only and must not reinterpret or repair the design verdict.

## Delegated Approval Owner

The user delegates routine pull-request approval to a read-only Approval Owner so that a compliant
iteration does not require a new chat confirmation at every Ready, merge, and cleanup gate. This
role is
activated only after the final candidate HEAD is frozen and every risk-tier expert has reported.
It does not replace Architecture, Verification, domain, security, risk, or release review.
Tier 0 instead uses the deterministic evidence decision defined above and does not activate this
model role; all Git and GitHub mutations remain Implementation Owner actions.

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

The Approval Owner prompt registry entry lives in
[`.governance/prompts/approval-v1.md`](.governance/prompts/approval-v1.md) and loads the canonical
body from [`.agents/approval-owner.md`](.agents/approval-owner.md), preserving ADR 0007 consumers
without maintaining two prompt bodies.
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

## Expert context bundle

Every activation receives a distinct Context Bundle conforming to
[`.governance/schemas/context-manifest.schema.json`](.governance/schemas/context-manifest.schema.json).
Only a `FROZEN` bundle may be used. Its lifecycle is:

`CREATED -> FROZEN -> USED -> SUPERSEDED -> ARCHIVED`

The immutable reference layer contains authority identifiers, paths, selection references, and
hashes, not copied authority contents or chat transcripts. It references:

- Issue goal, risk tier, explicit non-goals, and owner roles
- base branch, base commit SHA, and exact `reviewed_sha`
- relevant Accepted or Proposed ADRs
- files and modules in scope
- acceptance criteria and verification commands
- current diff or pull request
- known risks, blockers, and open finding IDs

The optional derived-view layer may reference an ephemeral summary, dependency map, or risk map.
Each view declares `type: derived`, `authority: none`, `non_evidentiary: true`, its source hashes,
generator, and content hash. Derived views reduce reading cost but cannot independently prove a
fact, finding, or verdict. Context-governance runtime state stores only their references and
hashes. Source, base/candidate SHA, rule, role question, or scope drift supersedes the bundle and
requires a successor. A used, superseded, or archived bundle cannot produce a new report.

## Review validity and output

Every expert report conforms to
[`.governance/schemas/report.schema.json`](.governance/schemas/report.schema.json) and includes:

- reviewed scope and exact `reviewed_sha`
- evidence inspected
- findings with stable IDs classified as blocker, major, or minor
- recommended action and finding owner
- final verdict
- role, actor ID, work-unit ID, base SHA, and context payload hash
- model ID, reasoning effort, available served model version or `unavailable`, Codex version,
  prompt ID/version/hash, Router rules hash, and skill versions or content hashes

Finding IDs use `<DOMAIN>-<NNN>` inside a work unit and are referenced across work units as
`<work-unit>/<DOMAIN>-<NNN>`. Sequences are never reused. Finding states are `OPEN`, `FIXED`,
`SUPERSEDED`, and `ACCEPTED_RISK`. A `FIXED` finding binds the claimed repair SHA and remains stale
until the responsible reviewer confirms it. `SUPERSEDED` links its successor. `ACCEPTED_RISK`
requires explicit Human Owner authorization, rationale, expiry, and a governance-debt Issue.

A verdict applies only to its exact `reviewed_sha`. A new commit or tracked working-tree change
makes the verdict stale. Re-review receives the previous reviewed SHA, new SHA, open finding IDs,
and the delta between those SHAs; it does not repeat the full context unless the change invalidates
the original assumptions. The final Verification Owner verdict must bind the exact candidate HEAD.

A pull request cannot become ready or merge while any blocker remains open or any required verdict
is stale. A delegated Approval Owner decision cannot be issued until these conditions are already
satisfied.

## Governance debt

GitHub Issues labeled `governance-debt` are the only governance-debt register. Use identifiers
`GOV-DEBT-NNN`; record the source exception or finding, owner, risk, repair condition, expiry, and
status. Temporary model substitution, tier downgrade, size exception, accepted risk, or stale rule
must create debt. Default expiry is at most 90 days. Extension requires explicit Human Owner
authorization; permanent decisions require an ADR rather than indefinite debt. Phase 0 records
this policy manually and does not add CI automation.

## Iteration handoff

Handoffs use the Issue, ADRs, commit SHA, pull request diff, verification evidence, extracted expert
knowledge, and unresolved follow-up Issues. The next iteration starts from a clean, synchronized
`main` branch.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the Git and release workflow.
