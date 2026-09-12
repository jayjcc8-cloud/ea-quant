# STRATEGY_LIFECYCLE_ARCHITECTURE_REVIEW_V1

Architecture survey of merged main `f098cd765dbddd831627e02ee9ae0ebf7e1a72e5`, 2026-09-12.
Origin main matched after fetch; tracked/untracked worktree changes were absent. Main CI
34688643235 succeeded. Issue #189 is closed; #125 remains inactive. No Candidate Issue exists
among open issues. This document is the requested survey, not another execution plan/state store.
The proposed durable contract is [ADR 0039](../adr/0039-strategy-lifecycle-and-research-candidate-identity-v1.md).

## CURRENT_STATE

| Authority | Current repository evidence | Architectural consequence |
| --- | --- | --- |
| Strategy | ADR 0035/0036; `src/ea/strategy/`; scenario V1/V2/V3 | ID/version alone does not bind local code; freeze package SHA and recorded EA code identity |
| Input | `service.py::_input_snapshot`, `_decode_job`, `_holdout_source` | Reuse versioned snapshot hash; recompute scenario hash and cross-check report, not just snapshot self-consistency |
| Attempt | `product/identity.py::canonical_backtest_lineage_bytes` | RunId is attempt-specific; lineage is not Candidate identity |
| Report | `product/reporting.py`; `service.py::report` | Report source already records code SHA/distribution and scenario/data; no report mutation needed |
| Path | ADR 0038; `product/equity_path.py`; `service.py::artifact` | Job v3 binds path SHA; path binds report/RunId/semantic outcome; older jobs lack it |
| Holdout | `web/holdout.py`; `service.py::_load_holdouts`, `_prepare_holdout` | Persisted relation is IDs/time only; read-time consistency must inspect both evidence chains |
| Batch/comparison | ADR 0032/0033; `service.py::BatchRecord`; Web derived views | Batch groups jobs; comparison has no independently persisted evidence artifact |
| Publication | `service.py` job/batch atomic writers and `create_holdout`; reporting fsync helpers | A Candidate plus decision fits one atomic file; no new transaction/recovery system |
| Legacy | ADR 0037/0038 and existing v1/v2/v3 decode | Read compatibility does not imply eligibility for new Candidate creation |

Inspected ADR 0033–0038 in full, STATUS, complete WORKFLOW, CONTRIBUTING and current completed
Issue #189. Current report/path checks are reusable, but not a complete Candidate evidence validator:
Candidate must add comparisons of all pinned identities and both relationship endpoints.

## PROBLEM

There is no persisted research decision object linking a chosen configuration and completed
source/holdout evidence. Adding an editable experiment snapshot or execution lifecycle would
create a competing authority. Also, incomplete DRAFT plus immutable evidence cannot support
in-place evidence completion without defining revisions. These are design issues, not current
runtime defects.

## CANDIDATE_DEFINITION

Q1: A persisted research-layer record binding one immutable strategy configuration to an
explicitly identified completed evidence set and a research decision state. No trading authority.

## IDENTITY_MODEL

Q2: Choose C, UUID record identity plus canonical content fingerprint. A (UUID only) lacks stable
content comparison; B (hash only) conflates evidence equivalence with record/decision identity.
C supports separate research decisions over the same evidence without duplicating evidence.
ADR 0039 defines exact domain, serialization, fields and exclusions.

Q3: All normalized parameters are identity. Different quantity or threshold is a different
fingerprint. New run evidence also changes fingerprint even when its economics are equivalent.
Record UUID, job UUID, engine RunId, strategy ID and content fingerprint are distinct.

## EVIDENCE_MODEL

Bind exactly one completed source and one completed chronological holdout, including both input
hashes, reports and paths, the persisted relation hash, and actual implementation identities.
Code identity is available in report source even for built-ins. Require equal recorded EA
code/distribution for source/holdout to avoid attributing cross-code results to one immutable
configuration. Never infer implementation from today's registry. Batch and comparison are
optional navigation, excluded from the V1 identity schema. Evidence is referenced, not copied or
regenerated. ADR 0039 specifies fail-closed cross-checks and the trusted-local-filesystem limit.

## STATE_MODEL

Q4: Holdout and both report/path sets are mandatory for EVALUATED. Recommend creation directly
as EVALUATED; no persisted DRAFT in V1. Then one terminal ACCEPTED or REJECTED decision. This
explicit alternative avoids a new evidence-revision mechanism and a misleading Evaluate button.
EVALUATED asserts completeness only; it is not a performance assessment.

## MUTABILITY_MODEL

Q6: Identity/evidence and creation/evaluation times never change. Status and decision change
once through a valid transition; terminal reason/outcome/time are frozen. Exact retry is a no-op.
Different evidence needs a new record. No version tree, revision history or general lifecycle engine.

## PERSISTENCE_MODEL

One canonical versioned JSON file per UUID under workspace/candidates; strict closed decoder,
existing service lock, staged file fsync, atomic replace, directory fsync. Do not add an index.
Reverify references on reads and transitions; unavailable evidence must not appear approved or
valid. Restart is ordinary record reopening, not backtest recovery. No external strategy execution
or CSV reopening is required to display previously persisted evidence.

## LEGACY_MODEL

No migration/rewrite. Missing Candidate is normal. Missing canonical snapshot/path makes a job
ineligible, while existing history stays readable. Explicit creation from complete existing v3
source/holdout evidence is allowed by the proposed contract. Evidence retention is not guaranteed
by a hash: deleted CSV may prevent re-execution, but not existing evidence reads (ADR 0037).

## ECONOMIC_AUTHORITY_BOUNDARY

Q5: ACCEPTED changes no permissions. Candidate has no engine dispatch, Order, Fill, Ledger,
Risk, Matcher, recovery or promotion authority. Future Paper/Live requires a distinct decision.
No stop-condition dependency was found: no migration, report mutation, new recovery frontier,
optimizer, multi-trade or strategy execution redesign is required for this proposal.

## V1_SCOPE

The architecture freezes a proposed reference-only Candidate type and EVALUATED-to-decision
contract. A subsequent implementation can offer explicit holdout selection from Run Detail,
minimal Candidate Detail, and a reason-bearing terminal decision. No runtime feature is claimed
by this review. ADR status remains Proposed until Product Owner acceptance/merge.

## NON_GOALS

All exclusions in ADR 0039 apply: no search/ranking/automatic promotion, metrics, Paper/Live,
workflow/governance framework, database, raw-data archive, migration, or recovery subsystem.

## IMPLEMENTATION_RECOMMENDATION

`IMPLEMENT_NOW=NO`. Prefer the requested ADR-only deliverable this round because the proposed
EVALUATED-only interaction is a deliberate reduction of the suggested DRAFT -> EVALUATED flow.
Do not bury that product choice inside an implementation. Technical feasibility is YES, but
feasibility does not require taking the optional code slice. This is not an authorization failure
or an economic blocker. No runtime code, schema implementation, UI or product tests are changed.

`ARCHITECTURE_REVIEW_BLOCKED=NO`.

## RepoKeel Retrieval Checkpoint

FOUND=YES; USED=YES; UNDERSTOOD=YES (agent's technical understanding, not the user's).
Read the engineering vault AGENTS.md and three relevant inbox notes: batch relationships,
atomic holdout relationship publication, and frozen strategy artifact bytes. These are supporting
experience, not current authority; rechecked their claims against the current code above.

IMPACT: choose reference-only evidence, cross-check scenario/report rather than digest
self-consistency, bind frozen package bytes, and use one atomic Candidate file instead of
replicating batch contents or introducing multi-record transactions. The existing holdout's
directory transaction is unnecessary when Candidate creation creates no new job.

NEW_KNOWLEDGE=NO; UPDATE_NEEDED=NO for the knowledge base. The new proposed product decision
belongs in ADR 0039; no duplicate vault note or memory update is warranted.

## Delivery boundary

Sole writer: primary Codex. Base as above. Branch `codex/strategy-lifecycle-v1`, isolated worktree
`/private/tmp/ea-strategy-lifecycle-v1`; only this review and ADR 0039, one documentation PR in
merge order. T0 by reachable impact; one read-only independent architectural review requested
by the task. No full verifier or installed-browser acceptance is appropriate without runtime
changes. Proposed/frozen design choices must not be reported as implemented or Accepted ADR.
