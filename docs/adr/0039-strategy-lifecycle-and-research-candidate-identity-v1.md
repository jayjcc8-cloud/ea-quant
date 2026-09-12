# ADR 0039: Strategy Lifecycle and Research Candidate Identity V1

Date: 2026-09-12

## Status

Proposed — architecture decision for review; no Candidate runtime is implemented.

## Context

ADR 0033–0038 provide independent immutable research evidence, but no persisted research
judgment over a selected evidence set. Jobs own execution state, snapshots own inputs,
BacktestReportV1 owns formal results, and EquityPathAnalysisV1 owns derived path evidence.
A Candidate must not replace any of these authorities.

## Decision

A StrategyCandidate is a persisted research-layer record binding one immutable strategy
configuration to an explicitly identified set of completed research evidence and a research
decision state. It has no economic or execution authority.

### Identity

Use UUID4 `candidate_id` for a record and a domain-separated SHA-256 `fingerprint` for its
immutable evidence identity (option C). UUID alone is easy to reference but cannot identify
identical content. Hash-only identity conflates a content identity with independently recorded
research decisions. UUID plus fingerprint distinguishes both without an index or new database.
Multiple explicitly created records may share a fingerprint; no deduplication/uniqueness claim.

The fingerprint projection is a closed object containing:

- `schema = ea.research-candidate.v1`, `schema_version = 1`;
- strategy ID/version, complete normalized parameter map, and implementation identity;
- source and holdout evidence references, each containing job UUID, engine RunId,
  `input_sha256`, canonical scenario SHA, canonical data SHA/count, formal report SHA,
  equity-path SHA and recorded EA code SHA/distribution name/version;
- one holdout reference containing validation UUID and SHA-256 of the exact persisted
  `holdout.json` bytes.

Implementation identity for a local strategy includes package ID and the complete artifact
SHA from its persisted scenario source; for a built-in it includes strategy ID/version and
report `source.code_sha256` plus `source.distribution`. Source and holdout must use the same
implementation; require matching EA code/distribution as well as matching local artifact
identity in this V1. Historical cross-distribution relations remain readable but are ineligible
for Candidate creation. Never substitute the currently installed version or current catalog.

Canonicalize with the existing Web JSON convention: sorted keys, ASCII escaping, compact
separators, no NaN/Infinity, and exactly one final LF. Hash
`b"ea.research-candidate.fingerprint.v1\x00"` interpreted as the domain text followed by one
NUL byte, concatenated with these canonical projection bytes. Use lowercase 64-digit hex.
Integers remain integers, decimal parameters use their existing canonical decimal strings.
Do not hash UUID of the Candidate record, status, reason, timestamps or display labels.

Same exact evidence/configuration yields the same fingerprint across distinct Candidate
records. A changed parameter, strategy, package, input or evidence changes the projection.
A fresh equivalent backtest normally has a new RunId/report SHA and therefore a different
Candidate fingerprint. This is exact evidence identity, not semantic strategy equivalence.
Strategy identity, Candidate record/content identity, backtest RunId, and Web job UUID are
separate: one strategy can have many configurations, runs, jobs and research decisions.

### Evidence

V1 requires exactly one successful source job and one successful chronological holdout job,
both with canonical snapshots, verified report and path evidence, and their existing relation.
The source job must equal the relation's source and the second job its holdout. No auto-selected
holdout, backtest rerun, report regeneration or derived metric computation occurs at creation.
If several holdouts exist, the user explicitly chooses one validation UUID.

At create, every detail read, and every decision update:

1. Strictly decode the Candidate and recompute its fingerprint. Reject unknown fields, wrong
   types, duplicate JSON keys, noncanonical bytes, invalid UUIDs/digests or inconsistent state.
2. Resolve workspace-owned references with existing regular-file/root checks. Read fresh
   persisted evidence, not browser state or a cached prior integrity verdict. Missing or corrupt
   evidence returns Candidate unavailable and cannot be accepted.
3. Recheck each job's succeeded state, snapshot digest and canonical scenario digest; verify
   job RunId against the referenced report and path. Match all pinned hashes, scenario/data
   identities, normalized parameters, strategy and implementation identities across their
   authorities. Do not merely compare a stored digest with itself.
4. Use existing report/path readers, additionally compare report scenario/data to the snapshot
   and path scenario/data to report and snapshot. The path must bind the same report and semantic
   outcome. Verify frozen local package bytes against their artifact SHA without executing them.
5. Check the exact relationship bytes/hash and endpoints, frozen parameters, existing holdout
   configuration compatibility and strict chronology from the two snapshots. Require identical
   code/distribution as above. Do not load current external CSV or execute strategy validation
   hooks merely to reopen a Candidate.

Hash integrity detects mismatch against pinned local evidence; it is not a signature or a defense
against a trusted filesystem owner replacing every record and hash. No signature/RBAC system.

Candidate stores only the small identity projection and lifecycle metadata. Strategy display,
parameters and research-input detail derive from verified pinned snapshots (the small normalized
parameter map in the projection is an equality assertion, not a second editable authority).
Do not copy descriptors, reports, path points, OHLCV, Fill histories or batch membership.
Batch/comparison remain navigation and derived read models. Optional batch/comparison evidence
is excluded from V1: there is no persisted comparison fact to hash, and batch membership is not
required to judge the selected source/holdout. A later evidence schema needs its own decision.

### State and mutability

V1 creates directly in `EVALUATED` only after the full evidence check. EVALUATED means evidence
complete, not profitable, validated statistically or approved. It does not impose thresholds.
Legal transitions are only `EVALUATED -> ACCEPTED` and `EVALUATED -> REJECTED`.
Both terminal decisions require a nonempty human-provided reason. ACCEPTED means worth retaining
or considering for a separately authorized next stage. It grants no running permission.

DRAFT is deliberately excluded from the persisted V1 schema. An incomplete evidence set cannot
become complete without changing an immutable fingerprint. Allowing evidence attachment would
require a revision identity or a different mutability contract. The user's proposed four-state
model is therefore reduced explicitly, rather than silently mutating an old Candidate.
Incomplete jobs remain existing research jobs with Candidate unavailable. Once all required
evidence exists the user may create a new Candidate. No branching or predecessor relationship.

All identity fields, evidence references, fingerprint, created_at and evaluated_at are immutable.
Creation sets created_at = evaluated_at using existing UTC timestamp formatting. Mutable fields
are status and the initially-null decision. The sole terminal transition atomically writes
outcome, reason and decided_at; decision outcome must match status, with decided_at no earlier
than evaluated_at. Terminal metadata cannot be edited in V1. An exact retry of the same decision
is an idempotent no-op preserving its timestamp; all other terminal updates reject. Evidence
changes require explicit new Candidate creation, never in-place replacement or auto-generation.

### Persistence and legacy

Use `workspace/candidates/<candidate_id>.json`, under the existing single service writer/lock.
A future implementation should reuse the existing Web canonicalization and atomic replacement
pattern: stage one file in the same directory, flush/fsync, replace, fsync the directory, then
update memory and acknowledge. State plus decision live in that one record; there is no second
index, journal or multi-file transaction. Hidden incomplete temporary files are not candidates.
After restart, only published strictly valid records are visible. An uncertain write response
must be resolved by rereading, not automatically creating another record or rerunning research.
No recovery state machine, execution slot, queue, database or new economic frontier is required.

Historical jobs and their bytes remain unchanged and readable. No automatic Candidate creation
and no migration. A legacy job lacking snapshot/path cannot be a V1 Candidate; show Candidate
unavailable. Complete eligible existing v3 evidence may be selected explicitly. A corrupt
Candidate must not prevent an unrelated historical job/report from reopening.

Persisted evidence is re-verifiable after restart and external CSV removal. Re-execution is
conditional on obtaining the exact original data/code/runtime dependencies; ADR 0037 does not
retain all raw data. Candidate does not promise a self-contained replay bundle or restore deleted
evidence. Identity is a reproducibility specification, not an archive guarantee.

### Authority and non-goals

No Candidate operation creates Order/Fill, changes Ledger/Risk/Matcher, dispatches a strategy,
changes BacktestReportV1 or EquityPathAnalysisV1, resumes a run, activates Paper/Live, or changes
execution permission. No engine reader may treat ACCEPTED as authorization. Future Paper/Live
promotion requires an independent architecture decision and explicit Product Owner authorization;
Candidate ID/fingerprint could be an evidence input only, never a promotion token.

V1 excludes optimizer, search, automatic creation/ranking/promotion, multi-trade, portfolio,
Paper/Live, risk approval, workflow engine, RBAC, notifications, branching/merge, experiment
tracking platform, new metrics (Sharpe/Sortino/Calmar/win rate), Monte Carlo, walk-forward,
new governance, historical migration, and new recovery support.

## Implementation decision and validation

`IMPLEMENT_NOW=NO`: deliver the preferred architecture-only result this round. The narrower
EVALUATED-only proposal resolves the immutable-DRAFT conflict, but materially changes the
suggested create/evaluate interaction; retain it as an explicit proposed product contract before
committing runtime/UI behavior. This is a scope recommendation, not a claim that the existing
engine needs redesign or that an economic stopping condition was reached.

`ARCHITECTURE_REVIEW_BLOCKED=NO`. A future small T1 slice can implement this proposal without
new economic authority. It should use one Issue/PR/writer and one review, a Run Detail create
entry with explicit holdout selection and a small Candidate Detail with evidence, status and
reason. Test identity differences/repeatability, cross-evidence mismatches and corruption,
legal/illegal/retried decisions, atomic failure/restart, and unchanged legacy reads. Installed
wheel Chromium must prove Strategy/Data -> Run -> path -> Holdout -> create EVALUATED Candidate
-> ACCEPTED or REJECTED -> service restart -> same readable Candidate. No separate Evaluate
button is needed for this chosen model. Full verifier and runtime acceptance belong to that
implementation, not to this docs-only review.
