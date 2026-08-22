# EA Governance Workflow

This is the complete durable workflow contract for humans, Codex, OpenCode, and other harnesses.
`AGENTS.md` is the automatically loaded entry point and non-bypassable safety floor. If the two
conflict, stop with `DRIFT/BLOCKED` and repair the inconsistency before continuing.

## Authority Precedence

Interpret repository information in this order:

1. **merged code, test results, and CI** — implemented reality;
2. **Accepted ADRs and formal specifications** — normative intent;
3. **docs/STATUS.md** — sole human-readable current project state;
4. **Issue and pull-request bodies** — task and candidate authority;
5. **Issue comments, pull-request comments, and chat** — historical evidence only.

Comments never silently override an ADR or current task body. Code that conflicts with an ADR is
not a new decision: record `DRIFT/BLOCKED`, then repair code or accept a superseding ADR. STATUS
that conflicts with merged code/tests/CI must be corrected.

The control plane separates history, decisions, present, and future:

- old Issues and pull requests preserve history;
- `docs/adr/` preserves accepted decisions;
- `docs/STATUS.md` preserves the present;
- `docs/ROADMAP.md` preserves Phase boundaries and future entry/exit criteria.

The Router is a Classification Evidence Provider, never a Decision Authority or task database.

## Issue Lifecycle

The state machine is:

`Draft → Ready → In Progress → Review → Verified → Done`

`blocked` is an orthogonal label, not a lifecycle state. `Done` means the Issue is closed after its
merged-main and CI evidence is recorded.

- **Draft:** task package is incomplete or still being decided.
- **Ready:** every required task-package field and reuse decision is complete.
- **In Progress:** writer lease, branch/worktree, base SHA, and merge order are recorded.
- **Review:** the candidate diff and exact HEAD are frozen; implementation editing pauses.
- **Verified:** all required exact-HEAD tests/reports pass and blocker count is zero.
- **Done:** merged `main` CI passes; STATUS/ADR/Issue/successors are synchronized and the Issue is
  closed.

An Issue without acceptance criteria cannot become Ready. A pull request without exact-HEAD
evidence cannot become Verified. A critical change with stale STATUS or ADR cannot become Done.

## Task Package

Every Issue contains:

- **Objective:** the one outcome that changes.
- **Scope:** allowed files, systems, and external state.
- **Non-goals:** work explicitly excluded.
- **Authoritative Inputs:** STATUS, this WORKFLOW, relevant ADRs/specifications, code, and evidence.
- **Acceptance Criteria:** verifiable completion conditions.
- **Risk Tier:** Tier 0, Tier 1, or Tier 2 with the highest trigger and rationale.
- **Validation:** deterministic tests, static checks, runtime/CI, and required human/model reviews.
- **Expected Outputs:** code, tests, ADR, status update, report, or successor Issue.
- **Reuse Assessment:** Tier 1/2 evidence; Tier 0 may record `N/A` with a reason.
- **Owners:** role/actor assignments and required separation for Tier 1/2.

Agents default to STATUS, WORKFLOW, task-linked ADRs, and Issue-selected code/evidence. They inspect
old comments only to resolve a named dispute or recover provenance.

## Risk Tiers and Model Routing

Highest trigger wins; uncertainty raises the candidate tier. Issue wording cannot override the
diff or cumulative change graph.

| Tier | Triggers | Required route |
|---|---|---|
| Tier 0 — low | Reversible non-executable docs/comments/metadata with no ADR, CI, dependency, schema, security, governance-authority, or runtime effect | Terra medium implementation; deterministic verification and approval decision |
| Tier 1 — normal | Executable code/config/dependency/CI, public interface, governance authority, or implementation of an accepted contract | Architecture Owner, Terra high Implementation Owner, Terra high Verification Owner |
| Tier 2 — high | Data/time/look-ahead, strategy, portfolio/ledger, risk, execution/matching, reconciliation/recovery, state-machine semantics, canonical bytes/digests, credentials/security, or release/live/external writes | Sol xhigh Decision, Terra high implementation, independent Terra high adversarial, independent Sol high verification, independent Sol high approval, plus domain expert |

These are minimum classifications. A Human may always raise the confirmed tier in the Issue;
lowering the Router candidate requires the governed evidence and authorization defined below.

Route directly to the lowest profile expected to complete the task once. Never use a
Luna → Terra → Sol rescue chain. Model routing optimizes accepted-task total cost, not price per
million Tokens.

Required Sol/Terra unavailability, retirement, rate limit, or gate failure returns `HOLD`; silent
downgrade is forbidden. A temporary substitution requires explicit Human Owner authorization and
a governance-debt Issue. A Sol-required Tier 2 gate cannot be replaced by Terra or Luna.

Luna is optional, non-authoritative extraction only when raw evidence exceeds about 80,000 Tokens,
20 files, or 10,000 lines. Its output must be schema-checked and discardable.

At most two Sol roles may be active for the repository and at most one for a work unit. Excess work
is `QUEUED`, not downgraded. Tier 2 roles run sequentially unless their read-only questions are
provably independent. Decision, implementation, adversarial, verification, and approval actors
use distinct IDs wherever separation is required.

## Classification and Change Graph

`.governance/router.yaml` provides `minimum_tier_candidate`, evidence, ambiguity, lineage, route,
and Luna eligibility. It never emits an approved tier or activates an actor.

Classification inspects Issue lineage, related pull requests, branch history, recent merged work,
changed paths, and cumulative semantic surfaces. Public API, schema, canonical bytes, digest,
error, ADR, CI, security, release, or external-write changes upgrade the candidate even when split
across individually small pull requests.

Reclassify the frozen candidate diff. A higher candidate supersedes its Context Bundles and any
report set missing the newly required roles.

Default size budgets:

| Unit | Production additions | Production deletions | Total additions | Changed files |
|---|---:|---:|---:|---:|
| Pull request | 1,200 | 1,000 | 3,000 | 15 |
| Atomic commit | 400 | 400 | 1,000 | 8 |

A new Python module is limited to 1,500 lines. More than 1,000 deleted production lines creates a
Tier 2 candidate. An exception binds base SHA, files, maximum budget, rationale, owner, expiry, and
repair condition before implementation; it never skips semantic review.

## Reuse Before Build

Tier 1/2 tasks record the capability, inspected sources/dependencies, up to three serious
candidates, supported/locked version, license, maintenance, supply-chain/security posture,
technical fit, integration/migration/operational/lock-in cost, and the reuse/adapter/local-build
decision. Tier 0 may record `N/A` with a reason.

Prefer a maintained existing dependency, current project capability, standard library, or small
adapter when it is safer than bespoke code. Do not perform an unbounded survey or add a dependency
only because it is popular. Long-lived dependency/boundary decisions receive Architecture review
and an ADR when material.

## Writer Lease and Agent Concurrency

Before tracked edits, record Implementation Owner, branch, checkout/worktree, base branch/SHA,
lease start/handoff, isolation, and merge order. A checkout has exactly one writer. Only the
Implementation Owner may edit tracked files, stage, commit, push, or mutate iteration state.

Read-only experts sharing a checkout do not run file-producing commands. Builds/tests/type checks
run in CI or a dedicated verification worktree. Parallel writers require distinct worktrees,
branches, leases, and declared merge order. Stop on unknown or unrelated changes.

One actor may hold at most **one active task for Tier 1/2**, or **two independent tasks for Tier 0**.
Do not open a new exploration direction until the current task has a reviewable artifact.

### Focused-green checkpoints

Long iterations define one atomic file/symbol slice before editing, implement only that slice, and
run its focused tests and focused static checks. Once focused-green, the Implementation Owner
creates a clean checkpoint commit, records its checkpoint SHA and evidence in the Issue/PR, and
pushes it when a branch/remote is available and the platform permits.
This happens **before the next independent slice** begins.

At most one bounded dirty slice may exist. A Git/platform write failure is recorded immediately;
do not continue accumulating independent slices on the uncheckpointed tree. Never checkpoint
known failures, unrelated/user-owned changes, secrets, caches, generated output, or ambiguous
ownership. Commit messages name the atomic contract rather than generic “WIP”.

A checkpoint SHA protects recovery and handoff; it does not authorize Ready or merge, refresh a
stale report, replace final quality/full/CI, or waive independent exact-HEAD verification. Squash
merge may still keep public history concise. Context recovery starts from the latest recorded clean
checkpoint plus at most the one bounded dirty slice.

## Context and Review Evidence

Every activation receives an actor-specific Context Bundle conforming to
`.governance/schemas/context-manifest.schema.json`:

`CREATED → FROZEN → USED → SUPERSEDED → ARCHIVED`

Only FROZEN bundles produce reports. Immutable references store authority identifiers, paths,
selection references, and hashes, not copied authority content or chat transcripts. Optional
derived views declare `type: derived`, `authority: none`, and `non_evidentiary: true`.

Source, base/candidate SHA, rules, role question, scope, or required-role drift supersedes the
bundle. Used/superseded/archived bundles cannot produce new reports.

Reports conform to `.governance/schemas/report.schema.json` and bind role, actor/work unit, base,
exact reviewed SHA, Context hash, evidence, findings, model/effort/version, Codex version,
prompt ID/version/hash, Router hash, and skill versions/hashes.

Finding IDs are `<DOMAIN>-<NNN>` per work unit; cross-work-unit references include the work-unit ID.
States are `OPEN`, `FIXED`, `SUPERSEDED`, and `ACCEPTED_RISK`. A FIXED finding is stale until its
reviewer confirms the repair SHA. ACCEPTED_RISK requires explicit Human Owner rationale, expiry,
and a governance-debt Issue.

A new commit or tracked working-tree change invalidates an earlier verdict. Final Verification
binds exact candidate HEAD. A PR cannot become Ready or merge with an open blocker, stale required
verdict, failing/incomplete exact-head CI, or unresolved review thread.

## Expert Lifecycle

1. Activate at a named gate with question, scope, base/HEAD, evidence, and exit condition.
2. Review the bounded Context Bundle, not full chat history.
3. Report once with stable finding IDs.
4. Extract durable knowledge to Issue, ADR, pull request, tests, or documentation.
5. Release the expert; do not keep a resident panel.

Experts do not spawn agents or expand scope unless the Issue explicitly allows it. At most two
read-only experts run concurrently with the Implementation Owner, and only for independent work.

## Approval and Merge Gates

Tier 0 uses its deterministic evidence decision. Tier 1/2 use the Approval Owner route unless a
mandatory Human Owner gate applies. The Approval Owner is read-only and cannot implement, repair,
reinterpret a design verdict, or mutate Git/GitHub.

Approval has three separately activated and consumed decisions:

1. **Ready:** Draft-to-Ready only.
2. **Merge:** squash merge only after fresh authoritative state.
3. **Cleanup:** only the enumerated post-merge manifest after the merge commit is reachable from
   expected `main`.

Immediately before each write, the Implementation Owner re-fetches head/base SHA, state, CI,
mergeability, reviews, and unresolved threads. Any unexpected change aborts the mutation and
requires a fresh decision.

Approval returns `HOLD` for missing/contradictory evidence, open blockers, stale verdicts,
non-successful exact-head CI, unresolved threads, changes to Approval Owner authority/prompt/rules,
live/external order writing, credentials/resolved secrets, production release/deployment/tag,
irreversible/destructive data operations, out-of-repository action, or a platform permission
prompt. These mandatory cases require explicit Human Owner authorization.

Verification-worktree cleanup requires a pre-enumerated path bound to the Issue/candidate, a clean
status, and non-force `git worktree remove`. Ambiguous, dirty, shared, unrelated, or forced cleanup
requires explicit Human approval.

## Governance Debt

GitHub Issues labelled `governance-debt` are the only debt register. IDs are `GOV-DEBT-NNN` and
record source exception/finding, owner, risk, repair condition, expiry, and status. Size exception,
tier downgrade, model substitution, accepted risk, and stale rule all create debt.

Default expiry is at most 90 days. Extension requires explicit Human authorization. Permanent
decisions use an ADR instead of indefinite debt.

## Long-item Compression

Pause comment growth and publish a current summary when any threshold is met:

- **30 or more comments**;
- **two decision reversals**;
- **three independent problems** in one item;
- **repeated misreading** of an obsolete conclusion.

The summary contains **Current Decision**, **Implemented**, **Unresolved**, **Superseded
Statements**, **Evidence**, and **Successor Issues**. Update the Issue/PR body or link the new
authority, then close or freeze the old item. History remains intact.

## Definition of Done

A task is Done only when all are true:

- acceptance criteria are satisfied;
- required merged code and tests exist on `main`;
- exact-head and merged-main CI are successful;
- required ADR updates are accepted;
- STATUS is current;
- the Issue contains a final conclusion;
- the pull request contains validation evidence and current reports;
- no critical decision exists only in comments;
- Successor Issues own every deferred item;
- useful expert knowledge is durable and completed experts are released;
- the branch/worktrees complete the approved cleanup manifest.

“Tests pass” alone is not Done. “Handle later” without a successor Issue is not Done.

## Weekly Governance Metrics

Review only these delivery metrics:

1. accepted pull requests;
2. first-pass acceptance rate;
3. average rework rounds;
4. Ready-to-Merge time;
5. merge proportion missing evidence or status updates;
6. Token and human-time cost per accepted pull request;
7. unresolved decisions existing only in Issue/PR comments.

The seventh metric must trend down. Counts without acceptance evidence do not represent progress.

## Git, Verification, and Release

One Issue maps to one branch and one Draft pull request. Direct pushes to `main` are forbidden.
Commits use Conventional Commits and remain logically focused. Pull requests use squash merge.

Canonical verification:

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

Do not change dependencies or locks to bypass a verifier. Tests are deterministic and contain no
private data or secrets. Phase boundaries increment the pre-1.0 minor version; compatible fixes
within a Phase increment patch. Release/tag/publication always requires explicit Human approval.
