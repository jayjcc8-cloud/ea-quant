# ADR 0034: Chronological Holdout V1

Date: 2026-09-08

## Status

Accepted

## Context

Issue #180 adds the selected first slice of OUT_OF_SAMPLE_VALIDATION_V1. Users explicitly
select one completed bounded-long-v1 job, including an ordinary batch member, for a later
registered scenario evaluation. The system cannot establish whether users previously viewed data.

## Decision

ChronologicalHoldoutV1 stores only schema/version, validation identity, created_at, source_job_id
and holdout_job_id. Jobs own execution truth, immutable canonical snapshots own input truth,
and verified BacktestReportV1 reports own results. No metrics or scenario fields are persisted
in the relationship.

The source must be succeeded with verified report and canonical snapshot. Backend copies exact
normalized target_quantity and entry_delay_bars from that snapshot. The creation request contains
only source job and target registered scenario identities; parameters cannot be supplied.

Target configuration must match strategy ID, full instrument specification and settlement currency,
funding, risk, execution and commission policy/rate, and randomness profile. Only replay window,
selected-data fingerprint and consequent scenario identity differ. Target defaults cannot replace
source parameters. The existing loader validates target data and fingerprint; the existing
parameterizer revalidates the frozen delay against the target dynamic maximum without clamping.
Canonical UTC chronology requires target.start_utc > source.end_utc; equality and overlap reject,
and gaps are allowed.

All validation including the single execution slot precedes persistence. Stage the ordinary job
record and minimal relationship in one directory, fsync and atomically rename the directory into
the job index. A published directory contains both; hidden staging directories are not jobs.
Subsequent job writes use the existing atomic file replacement. This requires no database or new
execution/recovery authority. Execution uses the existing executor, attempt, engine, ledger,
audit and report path; restart marks unfinished jobs interrupted under existing semantics.

The Web create/detail surfaces display frozen parameters, windows, data fingerprints, commission
assumptions and the two independent formal after-fee reports with run links. Reload derives all
content from persisted relations, jobs, snapshots and verified reports. Unavailable reports stay
unavailable. The original batch is unchanged.

Product claim: chronological holdout evaluation. No claim of unseen data, unbiased validation,
statistical independence or generalization; no validation pass/fail, automated candidate selection,
recommendation, cross-window performance delta or automatic Parameter Delta comparison.

## Validation

Source eligibility/report integrity, exact parameter freeze, strict chronology, individual
configuration mismatches, target dynamic bounds, atomic publication failure injection and restart
are contract tests. Real installed-wheel Chromium creates a two-member positive-commission IS
batch, explicitly selects one member, runs independent OOS, opens both reports, restarts and
reopens the same relation. One T1 review and existing CI complete acceptance.

## Consequences

No walk-forward, optimizer, experiment platform, new governance, new recovery frontier, arbitrary
windows or uploads, richer statistics, paper/live trading, broker, release or deployment is added.
Phase 1.1/#125 stays draft, blocked and inactive.
