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

The normal delivery state machine is:

`Draft → Ready → In Progress → Review → Verified → Done`

`blocked` is an orthogonal label, not a lifecycle state. `Done` and `Superseded` are terminal;
each is a terminal state. `Done` means the Issue is closed after its merged-main and CI evidence is recorded.
`Superseded` uses the `status:superseded` label for unfinished work whose objective or authority
was replaced by a later Accepted ADR or successor task. The final Issue conclusion names the
replacement authority and any still-owned work before closure. Superseded never means verified,
never hides an unresolved safety finding, and cannot replace Done for delivered work.

- **Draft:** task package is incomplete or still being decided.
- **Ready:** every required task-package field and reuse decision is complete.
- **In Progress:** writer lease, branch/worktree, base SHA, and merge order are recorded.
- **Review:** the candidate diff and exact HEAD are frozen; implementation editing pauses.
- **Verified:** all required exact-HEAD tests/reports pass and blocker count is zero.
- **Done:** merged `main` CI passes; STATUS/ADR/Issue/successors are synchronized and the Issue is
  closed.
- **Superseded:** a later Accepted decision or bounded successor owns the remaining objective; the
  old Issue is closed with `status:superseded` and preserves its implementation/review history.

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
| Tier 2 — high | Data/time/look-ahead, strategy, portfolio/ledger, risk, execution/matching, reconciliation/recovery, state-machine semantics, canonical bytes/digests, credentials/security, or release/live/external writes | Sol xhigh Decision/Design, Terra high Implementation, independent Sol high Combined Safety Verification, independent Sol high Merge Approval, plus a domain expert only when the frozen contract names one |

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
is `QUEUED`, not downgraded. Tier 2 roles run sequentially. Decision/Design, Implementation,
Combined Safety Verification, and Merge Approval use distinct actor IDs. The combined safety
actor performs the adversarial and verification questions once against the exact candidate SHA;
Merge Approval consumes that report instead of repeating semantic exploration.

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

A new commit or tracked working-tree change invalidates an earlier verdict. Combined Safety
Verification binds exact candidate HEAD and covers time visibility, audit/ledger, recovery,
canonical identity, capability confinement, and fail-closed behavior. A product PR may freeze at most two frozen candidates. A blocker on the second candidate returns the work to Decision/Design;
it does not start a third patch/review round under the same contract.

When a Draft pull request becomes Ready for review, every expected automated review must finish
before merge and bind the exact candidate SHA. A pending, stale, different-SHA, or post-merge
automated review is not evidence. A PR cannot merge with an open blocker, stale required verdict,
failing/incomplete exact-head CI, or unresolved review thread.

## Expert Lifecycle

1. Activate at a named gate with question, scope, base/HEAD, evidence, and exit condition.
2. Review the bounded Context Bundle, not full chat history. Combined Safety Verification answers
   the complete six-surface Tier 2 question in one activation.
3. Report once with stable finding IDs and one exact-candidate verdict.
4. Extract durable knowledge to Issue, ADR, pull request, tests, or documentation.
5. Release the expert; do not keep a resident panel.

Experts do not spawn agents or expand scope unless the Issue explicitly allows it. At most two
read-only experts run concurrently with the Implementation Owner, and only for independent work.

## Approval and Merge Gates

Tier 0 uses its deterministic evidence decision. Tier 1/2 use the Approval Owner route unless a
mandatory Human Owner gate applies. The Approval Owner is read-only and cannot implement, repair,
reinterpret a design verdict, or mutate Git/GitHub.

Approval has three separately activated and consumed decisions:

1. **Ready:** a pre-implementation task-package decision that authorizes only `draft_to_ready`.
2. **Merge:** a frozen-candidate decision that authorizes only `squash_merge` after fresh
   authoritative state.
3. **Cleanup:** only the enumerated post-merge manifest after the merge commit is reachable from
   expected `main`.

Ready reviews a complete Draft task package: its objective, scope, non-goals, authoritative inputs,
acceptance criteria, risk tier/rationale, validation, expected outputs, reuse assessment, and
owners. It also requires Architecture review is PASS, budget and any exception are approved, and
dependencies and named blockers are resolved. A Ready decision does not require a PR, candidate SHA,
implementation diff, writer lease, exact-head tests, or hosted CI. Incomplete acceptance criteria
produces `HOLD`; unapproved budget produces `HOLD`. Only after Ready may a distinct Implementation
Owner record a branch, worktree, base SHA, and writer lease before entering In Progress.

Merge reviews the frozen candidate and requires PR and exact candidate SHA, valid writer-lease
history and complete scoped diff, implementation and acceptance completion, focused and required
tests under the CI route below, hosted CI SUCCESS at exact candidate HEAD, the current Combined
Safety Verification verdict, completed exact-SHA automated review, scope and budget PASS, current
mergeability/reviews, and zero unresolved review threads. Merge Approval verifies freshness and
completeness but does not repeat the combined report's semantic exploration.
Missing CI or an unfrozen candidate produces `HOLD`. A Ready decision is not Merge evidence and does
not authorize `squash_merge`; Cleanup remains separately activated after merge.

Tier 0 uses the existing deterministic gate in these same `ready` and `merge` modes. Its `ready`
mode evaluates the complete Draft task package without candidate artifacts and, on APPROVE,
authorizes only `draft_to_ready`. Its `merge` mode retains the PR, exact candidate SHA, hosted CI,
scope, and budget evidence above and, on APPROVE, authorizes only `squash_merge`.

Immediately before each write, the Implementation Owner re-fetches head/base SHA, state, CI,
mergeability, reviews, and unresolved threads. Any unexpected change aborts the mutation and
requires a fresh decision.

For Ready and Merge, Approval returns `HOLD` for missing or contradictory applicable task-package or
candidate evidence, open blockers, stale applicable verdicts, changes to Approval Owner
authority/prompt/rules, live/external order writing, credentials/resolved secrets, production
release/deployment/tag, irreversible/destructive data operations, out-of-repository action, or a
platform permission prompt. For Merge, Approval returns `HOLD` for non-successful exact-head CI or
unresolved threads. An absent candidate or CI is not a Ready defect and must not cause Ready HOLD.
These mandatory cases require explicit Human Owner authorization.

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

An active Issue body is limited to 300 lines. Before it exceeds that limit, replace obsolete detail
with a current bounded task package and links to immutable ADRs, commits, reports, or successor
Issues. Do not copy CI logs, temporary hashes, or comment history into the active body.

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
- STATUS is current and identifies its checkpoint as the containing `main` commit rather than a
  guessed future SHA;
- the Issue contains a final conclusion;
- the pull request contains validation evidence and current reports;
- no critical decision exists only in comments;
- Successor Issues own every deferred item;
- useful expert knowledge is durable and completed experts are released;
- the branch/worktrees complete the approved cleanup manifest.

Expected automated review and STATUS synchronization complete before merge. Do not create a
docs-only successor merely to finish expected review or to replace a candidate SHA in STATUS.

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

CI selects the minimum sufficient route without weakening required evidence:

- docs-only pull requests run the focused governance checks;
- Python pull requests run `quality`;
- each frozen candidate uses one trusted exact-SHA `full` dispatch, subject to the two-candidate
  limit above;
- `main` builds and installs the wheel and runs core smoke checks;
- a release candidate runs the complete `full` release verification; and
- the Web job runs only when Web files, Node lock files, or CI workflow paths change.

A mixed change takes the union of its applicable routes. A CI workflow change exercises
governance, Python quality, and Web. A metadata-only change never triggers complete `full`
verification. The trusted dispatch validates the main-hosted workflow before checking out its
exact candidate SHA.

Do not change dependencies or locks to bypass a verifier. Tests are deterministic and contain no
private data or secrets. Phase boundaries increment the pre-1.0 minor version; compatible fixes
within a Phase increment patch. Release/tag/publication always requires explicit Human approval.
