# ADR 0029: Governance Freeze and Bounded Product Delivery

## Status

Accepted

## Context

EA-Quant's control plane grew faster than its product surface. Routine work accumulated Ready,
candidate, context-bundle, multi-role verification, approval, and cleanup gates. A reviewer could
always discover another theoretically valid boundary while no actor was accountable for shipping
a runnable product slice. R9 exhausted two candidates after its code, CI, and full verification
were green; R10 then proposed another framework before the first installed backtest existed.

The project still needs strict controls where code can affect real orders, authoritative economic
state, secrets, or irreversible operations. Applying that cost to documentation, offline
simulation, and ordinary product composition is counterproductive.

## Decision

### Primary objective

Deliver the acceptance criteria of the current product Issue. Governance is a constraint on
delivery, not the deliverable. The Product Owner may choose the simplest local, reversible
implementation that satisfies those criteria.

### Actual-impact risk tiers

- **T0:** documentation, developer tooling, non-runtime UI, and test helpers. Implementer plus CI.
- **T1:** research, backtest, offline simulation, analytics, and services without operational
  economic authority. Implementer, one Reviewer, and CI.
- **T2:** real operational order submission, risk limits, ledger, recovery, reconciliation, or
  security authority. Implementer, one independent adversarial Reviewer, and exact-head CI.
- **T3:** real-money release, secrets/permissions, or irreversible migration. T2 plus explicit
  Human Product Owner approval, staged rollout, and rollback.

Classification follows actual reachable impact, not keywords or theoretical future use. The
Router is advisory and cannot activate actors or raise a confirmed tier. A model may ask the
Product Owner to raise a tier only with a reproducible execution path.

### Three accountable subjects

- **Product Owner:** priority, tier, residual-risk acceptance, final merge and Token budget.
- **Implementer:** scoped implementation, tests, CI evidence, and known limitations.
- **Reviewer:** current diff, direct execution paths, explicit acceptance criteria, and qualifying
  blockers. T0 does not require an independent Reviewer.

The mandatory four-party sequence in ADR 0028 and the pre-implementation/merge/cleanup approval
sequence in ADR 0026 no longer apply. Actor-specific FROZEN context bundles and schema reports are
optional tools, not delivery gates.

### Blocking findings

A finding blocks only when all four conditions hold:

1. it is reachable from the current diff or its direct execution path;
2. it is reproducible or has a concrete execution trace;
3. it violates an explicit acceptance criterion or creates direct data, permission, financial,
   or irreversible operational risk; and
4. it can be represented by a failing test, minimal reproduction, or concrete exploit.

Speculative future risks, wording differences, unreachable edges, unrelated legacy defects, and
general hardening belong in the Hardening Backlog and do not block. Delivery proves the agreed
contract; it does not require proof that all possible defects are absent.

### Finite review

Every product PR receives one primary review. If that review finds blockers, the Implementer gets
one concentrated repair and the same Reviewer gets one verification limited to those blockers and
direct regressions. A new non-catastrophic issue found in verification enters the backlog. After
one implementation-review-repair cycle, the Product Owner chooses merge, scope reduction, or
closure; another reviewer chain is not created.

### Governance Freeze

Until the first offline simulated vertical slice merges, the project will not add governance
roles, states, manifests, schemas, approval phases, or repository-wide governance abstractions.
Governance may change only when a reproducible rule permits real operational risk, causes a
deterministic wrong merge/data loss, or directly prevents a product PR from reaching a finite
decision.

Existing runtime tests, safety invariants, CI, immutable ADR history, one-writer isolation, and
Human authorization for live/external/destructive/release actions remain mandatory.

## Superseded Process Decisions

This ADR preserves the authority precedence and immutable-history rules in ADR 0025 while
superseding its recursive task-package and evidence machinery. It supersedes the delivery-process
requirements of ADR 0026 and the four-party role chain of ADR 0028. ADR 0027's offline product and
no-live boundary remains effective.

## Consequences

- T0/T1 work can reach a finite merge decision without exact-SHA multi-party reports.
- T2/T3 retain exact-head and independent adversarial scrutiny proportional to actual impact.
- Historical R9/R10 findings remain evidence and backlog, but do not block an offline T1 slice.
- Review efficiency is measured by Token cost, review rounds, lead time, and runnable capability.
- The Product Owner explicitly owns residual-risk and stop/merge decisions.

## Rejected Alternatives

- Continuing R10 before a product slice: rejected because it extends provenance governance rather
  than delivering user-visible capability.
- Removing all safety controls: rejected because T2/T3 paths need strict evidence.
- Building a second simplified trading engine: rejected because current main already contains the
  required deterministic market, risk, execution, ledger, reconciliation, and audit components.

## Implementation and Validation

RESET-001 (#154) updates the entry rules, workflow, Router, templates, STATUS, and governance
tests, then delivers one installed offline simulated vertical slice in a separate T1 PR.
Historical ADR files are not rewritten.

## References

- ADR 0025 — Project control plane and authority precedence
- ADR 0026 — Pre-implementation Ready and candidate Merge approval
- ADR 0027 — Phase 1 offline backtest product boundary
- ADR 0028 — Tier 2 four-party delivery model
- Issue #154 — RESET-001
