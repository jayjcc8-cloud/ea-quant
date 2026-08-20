---
prompt_id: deterministic-gate-v1
version: 1.0.0
kind: deterministic_contract
owner: human_owner
purpose: Produce the Tier 0 verification and approval evidence decision without a review model.
model_profile: deterministic_tools
required_effort: deterministic
forbidden_actions:
  - invoke_a_review_model
  - accept_a_candidate_above_confirmed_tier0
  - accept_missing_or_stale_exact_head_evidence
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

Return `APPROVE` with `authorized_mutation: none` only when all of the following are true:

- the Issue confirms Tier 0 and complete classification evidence has no ambiguity
- the final diff is limited to reversible, non-executable documentation or metadata and changes no
  ADR, CI, dependency, schema, security, governance authority, or runtime behavior
- the candidate SHA, base SHA, scope, and diff evidence agree
- required exact-head local checks and CI are successful and current
- every finding is closed, no required evidence is stale, and the worktree scope is clean

Otherwise return `HOLD` and identify the missing or contradictory evidence. `APPROVE` is an evidence
decision only; the Implementation Owner remains the sole actor permitted to perform a Git or GitHub
mutation.
