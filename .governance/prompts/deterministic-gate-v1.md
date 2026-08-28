---
prompt_id: deterministic-gate-v1
version: 1.1.0
kind: deterministic_contract
owner: human_owner
purpose: Produce the Tier 0 Ready or Merge evidence decision without a review model.
model_profile: deterministic_tools
required_effort: deterministic
forbidden_actions:
  - invoke_a_review_model
  - accept_a_candidate_above_confirmed_tier0
  - accept_missing_or_stale_exact_head_evidence_at_merge
  - reinterpret_ambiguous_classification_as_tier0
  - authorize_or_perform_git_or_github_mutations
  - convert_hold_to_approve
required_output:
  schema: .governance/schemas/report.schema.json
  role: deterministic_gate
  verdicts: [APPROVE, HOLD]
---

# Deterministic Tier 0 Evidence Gate v1

This is a versioned decision contract, not a model prompt. A deterministic tool or the
Implementation Owner evaluates it against authoritative evidence and records the contract ID,
version, and content hash in the report provenance fields.

Use the existing report-schema `gate` value to select one of two modes; this contract adds no gate
or schema enum.

For `ready`, return `APPROVE` with `authorized_mutation: draft_to_ready` only when the Issue confirms
Tier 0 with unambiguous classification evidence, is Draft, and supplies a complete Draft task
package: objective, scope, non-goals, authoritative inputs, acceptance criteria, risk tier/rationale,
validation, expected outputs, reuse assessment, and owners. The package must show Architecture review
is PASS, budget and any exception are approved, and dependencies/named blockers are resolved. It does
not require a PR, candidate SHA, implementation diff, writer lease, exact-head tests, or hosted CI.
Incomplete acceptance criteria produces `HOLD`; unapproved budget produces `HOLD`. APPROVE
authorizes only `draft_to_ready`.

For `merge`, return `APPROVE` with `authorized_mutation: squash_merge` only when Tier 0 remains
unambiguous and the frozen candidate satisfies all of the following:

- the final diff is limited to reversible, non-executable documentation or metadata and changes no
  ADR, CI, dependency, schema, security, governance authority, or runtime behavior
- PR and exact candidate SHA, base SHA, scope, and complete diff evidence agree
- valid writer-lease history and complete scoped diff are recorded
- required exact-head local checks and hosted CI SUCCESS at exact candidate HEAD are current
- scope and budget pass; every finding is closed; no required evidence is stale; the worktree scope
  is clean; and reviews/threads are current and resolved

Missing CI or an unfrozen candidate produces `HOLD`. APPROVE authorizes only `squash_merge`.
Otherwise return `HOLD` and identify the missing or contradictory evidence. The Implementation Owner
remains the sole actor permitted to perform the Git or GitHub mutation.
