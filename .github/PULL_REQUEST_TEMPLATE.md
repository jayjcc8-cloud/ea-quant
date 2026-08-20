## Iteration

- Issue:
- Risk tier and reason:
- Classification evidence ID / rules hash / minimum tier candidate:
- Confirmed tier authority:
- Issue lineage / related PRs / cumulative contract change:
- Milestone / target version:
- Base branch and base SHA:
- Branch and checkout/worktree:
- Merge order with other active work:
- Architecture Owner:
- Implementation Owner:
- Verification Owner:
- Domain expert(s), or N/A:
- Writer lease started / handoff status:

## Goal

<!-- State the single outcome this PR delivers. -->

## Non-goals

<!-- List work that is explicitly outside this PR. -->

## Changes

<!-- Summarize the important files, behavior, and interfaces changed. -->

## Decisions and assumptions

- Related ADRs:
- New assumptions:
- Data/config/dependency impact, or N/A with reason:

## Reuse assessment

Complete before implementation for Tier 1/2. Tier 0 may use `N/A` with a reason.

- Capability needed:
- Existing project/standard-library capability inspected:
- Search sources and date:

| Candidate and supported/locked version | License | Maintenance / supply-chain / security evidence | Technical fit | Integration / migration / lock-in cost | Decision or rejection reason |
|---|---|---|---|---|---|
| | | | | | |

- Decision: reuse directly / adapter / local build / N/A
- Why this is the smallest safe ownership choice:

## Quant and operational risk

- Look-ahead bias / time semantics, or N/A with reason:
- Execution and fill assumptions, or N/A with reason:
- Reproducibility impact, or N/A with reason:
- Secrets, live-trading flags, and external writes:
- Rollback plan:

## Verification

- Candidate HEAD SHA:
- [ ] `uv run --no-project --python 3.12 python scripts/verify.py --profile quality`
- [ ] `uv run --no-project --python 3.12 python scripts/verify.py --profile full`
- CI result:
- Verification worktree / CI run:
- Context Bundle ID / state / payload hash:

## Expert review

Every verdict is valid only for its exact `reviewed_sha`.

| Role | Actor / work unit | Model / effort | Prompt ID / hash | Context hash | reviewed_sha | Finding IDs | Verdict |
|---|---|---|---|---|---|---|---|
| Architecture or Decision Owner, if required | | | | | | | |
| Adversarial Reviewer, if required | | | | | | | |
| Verification Owner or deterministic gate | | | | | | | |
| Domain expert, if required | | | | | | | |
| Approval Owner or automatic evidence gate | | | | | | | |

### Finding closure

| Finding ID | Severity | Owner | Resolution SHA / evidence | Status | Successor / debt Issue |
|---|---|---|---|---|---|
| | | | | | |

### Governance debt and exceptions

- Tier/model/size exceptions: none / list with authorization
- `governance-debt` Issues: none / list with expiry
- Production additions / deletions / total additions / changed files:

### Knowledge extraction and agent release

- Durable decisions/evidence recorded in:
- Follow-up Issues:
- Completed experts released:
- Stale verdicts after the final HEAD: none / list

## Completion

- [ ] All blockers are resolved.
- [ ] Every required verdict binds the final candidate HEAD.
- [ ] Documentation and ADRs are synchronized.
- [ ] Required reuse assessment and dependency decision evidence are recorded.
- [ ] No unrelated changes are included.
- [ ] Follow-up work is recorded as Issues.
- [ ] Useful expert knowledge is extracted and completed agents are released.
- [ ] Version impact is declared: none / patch / minor / major.
- [ ] Worktree is clean and the PR diff has been reviewed.
