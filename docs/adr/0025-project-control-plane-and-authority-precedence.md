# ADR 0025: Project Control Plane and Authority Precedence

Date: 2026-08-22

## Status

Accepted

This ADR narrowly supersedes ADR 0002 and ADR 0007 only where they assign the complete workflow
contract to `AGENTS.md` or `CONTRIBUTING.md`. Their Git, branch, review, delegated-approval, and
release decisions remain Accepted. The complete workflow contract now lives in
[`docs/governance/WORKFLOW.md`](../governance/WORKFLOW.md); `AGENTS.md` is an automatically loaded
entry point and safety floor, and `CONTRIBUTING.md` is a contributor entry point.

## Context

Phase 1 accumulated correct code and valuable review evidence, but current state, future intent,
accepted decisions, and historical discussion were repeated across README, architecture notes,
Issues, pull requests, comments, and chat. The repetitions aged independently. Agents then had to
read long histories, infer which statement still applied, and spend stronger-model capacity
reconciling avoidable drift.

The project needs one small repository control plane that is readable by people and agents while
preserving GitHub history as evidence. It must not create a second task database, rewrite Accepted
ADRs, or treat a generated Router classification as authority.

## Decision

Repository information is interpreted in this order:

1. **merged code, test results, and CI** — evidence of actual implemented behavior;
2. **Accepted ADRs and formal specifications** — normative intent and accepted constraints;
3. **docs/STATUS.md** — the sole human-readable statement of current project state;
4. **Issue and pull-request bodies** — authoritative task packages and candidate evidence;
5. **Issue comments, pull-request comments, and chat** — historical evidence only.

Code passing its tests does not silently supersede an ADR. If implemented behavior conflicts with
normative intent, record `DRIFT/BLOCKED`, repair the implementation or accept a superseding ADR,
and update STATUS. If STATUS conflicts with merged code/tests/CI, correct STATUS promptly.

The control plane has five core state-document classes:

- `README.md`: project identity, current Phase/health, shortest supported run path, and navigation;
- `docs/STATUS.md`: the present, including completion, gaps, blockers, active ADRs/Issues, evidence
  date, completion conditions, and the latest governance metrics;
- `docs/ROADMAP.md`: Phase boundaries, non-goals, entry gates, and exit criteria;
- `docs/adr/NNNN-*.md`: accepted architecture, trading-semantics, governance, and workflow decisions;
- `docs/governance/WORKFLOW.md`: lifecycle, task-package, risk/model, evidence, approval, Definition
  of Done, and governance-debt rules.

`docs/adr/` remains the only ADR directory. Existing Accepted ADRs are immutable and are superseded
only by a later ADR. Router output remains classification evidence, never task or decision
authority. Derived summaries may reduce reading cost but are non-evidentiary and must link their
sources.

Every new Issue is a bounded task package. Long Issues are compressed into a current summary and
successor Issues rather than extended indefinitely. Historical Issues, pull requests, comments,
and chats remain intact.

## Rejected Alternatives

- **Keep README, AGENTS, CONTRIBUTING, architecture, and comments synchronized manually.** This
  preserves multiple current-state copies and makes drift inevitable.
- **Move ADRs to `docs/decisions/`.** This creates a second ADR namespace and breaks existing links.
- **Make the Router or an external project-management tool authoritative.** Either creates a
  second task/decision source and hides GitHub lineage.
- **Rewrite or delete historical comments.** This destroys useful audit evidence without improving
  present-state clarity.
- **Automate the entire protocol immediately.** Real use of the minimum control plane must reveal
  repeated omissions before Router CLI or CI governance automation is justified.

## Consequences

- Agents start with STATUS and WORKFLOW, then read only task-linked ADRs, code, and evidence.
- Comments can explain history but cannot silently change a current decision or task package.
- Critical decisions found only in comments must be promoted to an ADR, formal specification, or
  successor Issue before Phase 1 closes.
- Governance changes must update the control plane and its offline consistency tests.
- This consolidation changes information ownership, not product runtime behavior, risk tiers,
  model separation, or delegated-approval semantics.

## Implementation and Validation

Issue #78 and its Consolidation pull request implement this ADR. Validation requires:

- offline tests for required files, sections, links, task-template fields, Tier vocabulary, ADR
  location, and governance-consumer references;
- an evidence-backed classification of all 37 pull requests;
- STATUS consistency with exact `main`, CI, and open Issues;
- Tier 2 Decision, Adversarial, and independent Verification reports;
- explicit Human Owner Ready, merge, and cleanup authorization because Approval Owner authority is
  among the changed governance inputs.

## References

- [Issue #78](https://github.com/jayjcc8-cloud/ea-quant/issues/78)
- [Issue #67](https://github.com/jayjcc8-cloud/ea-quant/issues/67)
- [ADR 0002](0002-git-github-workflow.md)
- [ADR 0007](0007-delegated-approval-owner.md)
- [Governance Workflow](../governance/WORKFLOW.md)
- [Current Status](../STATUS.md)
- [Roadmap](../ROADMAP.md)
