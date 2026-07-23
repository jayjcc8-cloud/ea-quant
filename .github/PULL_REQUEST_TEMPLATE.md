## Iteration

- Issue:
- Risk tier and reason:
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

## Expert review

Every verdict is valid only for its exact `reviewed_sha`.

| Role | Reviewer | reviewed_sha | Evidence | Finding IDs | Verdict |
|---|---|---|---|---|---|
| Architecture Owner, if required | | | | | |
| Verification Owner | | | | | |
| Domain expert, if required | | | | | |

### Finding closure

| Finding ID | Severity | Owner | Resolution evidence | Status |
|---|---|---|---|---|
| | | | | |

### Knowledge extraction and agent release

- Durable decisions/evidence recorded in:
- Follow-up Issues:
- Completed experts released:
- Stale verdicts after the final HEAD: none / list

## Completion

- [ ] All blockers are resolved.
- [ ] Every required verdict binds the final candidate HEAD.
- [ ] Documentation and ADRs are synchronized.
- [ ] No unrelated changes are included.
- [ ] Follow-up work is recorded as Issues.
- [ ] Useful expert knowledge is extracted and completed agents are released.
- [ ] Version impact is declared: none / patch / minor / major.
- [ ] Worktree is clean and the PR diff has been reviewed.
