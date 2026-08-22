## Iteration and state

- Issue:
- Risk tier and reason:
- Lifecycle state: Review / Verified
- Base branch and exact base SHA:
- Branch and checkout/worktree:
- Merge order with other active work:
- Writer lease / handoff status:
- Required owners and actor separation:

## Objective and scope

- Objective:
- Scope:
- Non-goals:
- Authoritative inputs:
  - STATUS:
  - WORKFLOW:
  - ADRs/specifications:
  - Code/tests/CI or other evidence:

## Changes

<!-- Summarize important files, behavior, interfaces, and semantic surfaces. -->

## Decisions, reuse, and risk

- Related ADRs / new decision required:
- Reuse decision and evidence:
- Public API/schema/canonical bytes/digest/error impact:
- Time/look-ahead/execution/risk/recovery impact:
- Secrets/live/external-write/release impact:
- Rollback or recovery plan:

## STATUS / ADR synchronization

- STATUS change: updated / not required with evidence
- ADR change: updated / not required with evidence
- ROADMAP change: updated / not required with evidence
- No critical decision exists only in comments: yes / blocker

## Validation evidence

- Candidate HEAD SHA:
- Focused command and result:
- [ ] `uv run --no-project --python 3.12 python scripts/verify.py --profile quality`
- [ ] `uv run --no-project --python 3.12 python scripts/verify.py --profile full`
- Exact-head CI URL and conclusion:
- Verification worktree / CI identity:

## Expert review

Every verdict is valid only for its exact `reviewed_sha`.

| Role | Actor | reviewed_sha | Context/report evidence | Finding IDs | Verdict |
|---|---|---|---|---|---|
| Decision / Architecture | | | | | |
| Adversarial, if required | | | | | |
| Domain expert, if required | | | | | |
| Verification | | | | | |
| Approval gate | | | | | |

### Finding closure

| Finding ID | Severity | Owner | Repair/confirmation SHA | Status |
|---|---|---|---|---|
| | | | | |

## Delivery efficiency

- Rework rounds:
- Token cost (accepted PR total):
- Human time:
- Ready-to-Merge time:

## Completion

- [ ] Acceptance criteria are satisfied.
- [ ] All blockers are closed and every required verdict binds Candidate HEAD SHA.
- [ ] Exact-head local verification and CI succeed.
- [ ] STATUS / ADR synchronization is complete.
- [ ] No unrelated changes are included.
- [ ] Successor Issues own every deferred item.
- [ ] Useful expert knowledge is durable and completed experts are released.
- [ ] Version impact is declared: none / patch / minor / major.
- [ ] Human or delegated Ready/Merge/Cleanup gates are identified.
