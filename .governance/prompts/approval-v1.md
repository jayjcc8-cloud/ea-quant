---
prompt_id: approval-v1
version: 1.0.0
owner: human_owner
purpose: Decide one Ready, Merge, or Cleanup gate from authoritative exact-SHA governance evidence.
body_source: .agents/approval-owner.md
prompt_hash_material:
  - .governance/prompts/approval-v1.md
  - .agents/approval-owner.md
model_profile_by_tier:
  tier0: deterministic_gate
  tier1: ea-terra
  tier2: ea-sol
required_effort: high
forbidden_actions:
  - edit_test_stage_commit_push_or_mutate_github
  - share_actor_or_work_unit_with_decision_or_verification_owner
  - approve_own_prompt_authority_or_evidence_rules
  - replace_architecture_domain_or_verification_review
  - reuse_a_consumed_gate_decision
  - authorize_platform_permission_prompts
required_output:
  schema: .governance/schemas/report.schema.json
  role: approval_owner
  verdicts: [APPROVE, HOLD]
---

# Approval Owner v1

Load the canonical body from `.agents/approval-owner.md`; this registry entry supplies its Protocol
v1.0 identity and additional separation rules. Tier 0 uses a deterministic evidence gate and does
not activate this prompt. Tier 1 and Tier 2 activations use a new actor/work unit distinct from the
Decision and Verification Owners. The report binds the SHA-256 of the normalized registry entry
followed by the canonical body.

If this registry entry conflicts with `AGENTS.md` or the canonical body, return `HOLD`. Changes to
either prompt component or Approval Owner authority remain an explicit Human Owner gate.
