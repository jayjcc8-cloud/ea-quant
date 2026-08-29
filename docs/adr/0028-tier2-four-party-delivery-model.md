# ADR 0028: Tier 2 Four-Party Delivery Model

Date: 2026-08-29

## Status

Accepted

This ADR supersedes ADRs 0007, 0025, and 0026 only where the Tier 2 route requires separate
adversarial and verification activations or allows expected automated review to finish after
merge. Ready, frozen-candidate Merge, post-merge Cleanup, exact-SHA evidence, one-writer safety,
model availability, and mandatory Human Owner gates remain unchanged.

## Context

The five-role Tier 2 route repeatedly asked separate adversarial, verification, and approval
actors to rediscover the same semantics. Findings created patch/review loops, exact-SHA verdicts
went stale after each repair, and delivery effort concentrated on proving protocol completeness
rather than producing the Phase 1 product.

Independence remains necessary. Repetition does not. A single exact-candidate safety review can
adversarially test the important semantic surfaces and issue the verification verdict. Merge
Approval can then decide from that report and current delivery state without performing another
open-ended semantic exploration.

## Decision

### Four parties

Every Tier 2 delivery uses exactly four accountable parties:

1. **Decision/Design** freezes objective, public and persistent contracts, safety boundary,
   acceptance matrix, budget, and non-goals before implementation.
2. **Implementation** is the sole writer for the recorded branch/worktree lease and produces the
   bounded candidate through test-first focused checkpoints.
3. **Combined Safety Verification** is one independent read-only actor reviewing the exact
   candidate SHA. It performs both adversarial analysis and verification across time visibility,
   audit/ledger, recovery, canonical identity, capability confinement, and fail-closed behavior.
4. **Merge Approval** is a separate read-only actor that consumes the Decision record, Combined
   Safety Verification report, exact-head CI, scope/budget, reviews, and unresolved-thread state.
   It approves or holds merge; it does not restart semantic exploration.

Decision/Design, Implementation, Combined Safety Verification, and Merge Approval use distinct
actor IDs. An actor unavailable at its required model/effort produces `HOLD`; silent downgrade or
self-approval remains forbidden.

### Candidate and review convergence

A product PR may freeze at most two candidates. Candidate one receives the complete Combined
Safety Verification. Repairs invalidate its verdict and may produce candidate two. If candidate
two still has a blocker, the work returns to Decision/Design; a third patch/review round is not
opened under the same frozen contract.

When a Draft pull request becomes Ready for review, every expected automated review must finish
before merge and bind the exact candidate SHA. A pending, stale, post-merge, or different-SHA
automated review is not delivery evidence. Ready for review is GitHub presentation state; it does
not replace the governed Issue lifecycle or candidate Merge gate.

### Combined report

The Combined Safety Verification report uses the existing exact-SHA report contract and
verification role. Its scope must state conclusions for all six surfaces:

- time visibility and no-look-ahead;
- audit and ledger ordering/idempotency;
- restart/resume equivalence and corruption behavior;
- canonical identity, bytes, decimal, and lineage;
- capability confinement and absence of live/external writes; and
- fail-closed terminal/report behavior.

One report may record multiple stable finding IDs, but there is one verdict for the candidate.
Merge Approval may check evidence freshness and completeness; it does not commission another
adversarial review unless the candidate, contract, rules, or required scope changed.

### Lower tiers

Tier 0 and Tier 1 routes are unchanged. The Router remains classification evidence only. It names
the four Tier 2 roles but cannot approve a tier, activate an actor, authorize a mutation, or waive
Human Owner authority.

## Validation

Governance tests must prove:

- Router and WORKFLOW name the same four ordered Tier 2 parties;
- the combined safety scope contains all six required surfaces;
- Implementation cannot also be Safety Verification or Merge Approval;
- Safety Verification cannot also be Decision/Design or Merge Approval;
- at most two frozen product candidates are permitted;
- expected automated review is exact-SHA and pre-merge; and
- Approval requires current report/CI/thread evidence but does not repeat semantic discovery.

## Consequences

- Tier 2 keeps independent design, implementation, verification, and approval accountability.
- One comprehensive safety report replaces two overlapping semantic review activations.
- Candidate churn has an explicit stop condition and returns unresolved design problems to the
  correct authority.
- Automated review becomes a pre-merge gate instead of post-merge paperwork.

## Rejected Alternatives

### Remove independent safety review

Rejected because time, ledger, recovery, and canonical errors can pass implementation-owned tests.

### Let the implementer approve merge

Rejected because it collapses the evidence and decision roles and violates the one-writer safety
model.

### Keep unlimited patch/review rounds

Rejected because repeated blockers after two frozen candidates indicate an unstable design, not
an implementation queue.
