# ADR 0032: Web Bounded Experiment Batch V1

Date: 2026-09-06

## Status

Accepted

## Context

ADR 0031 provides a durable single-run research loop and pairwise comparison. Repeating that
flow manually does not preserve the fact that a small set of parameter combinations belongs to
one user experiment. Frontend submission of multiple independent jobs would also allow an
invalid later member to leave a partially created experiment.

The existing Web service deliberately has one active execution slot and one single-thread
executor. V1 needs bounded grouping and eventual execution, not a new queue, scheduler, worker
pool, optimization engine, or execution authority.

## Decision

Add one minimal persisted `ExperimentBatch` relationship for the local Web adapter:

- a batch binds one registered, validated strategy/scenario and contains 2-10 member jobs;
- the backend validates and normalizes every member before creating any job, rejects duplicate
  normalized strategy-parameter combinations, and returns HTTP 422 for an invalid batch;
- every member is a normal Web job with its own immutable input snapshot, attempt identity,
  lifecycle, engine execution, and formal report;
- the existing single-thread executor runs batch members serially, while one batch occupies the
  existing active slot;
- batch persistence stores only batch identity, creation time, shared scenario identity, and
  ordered member job IDs; job persistence remains the authority for inputs, states, and results;
- batch status is derived from member jobs, with `accepted` presented as `queued` and a failed
  job carrying `risk.rejected` presented as `risk.rejected`; and
- the batch page displays a member table and sends any two selected jobs to the existing
  pairwise comparison route.

The V1 parameter surface remains exactly `target_quantity` and `entry_delay_bars`. Result summary
uses only existing formal-report equity, net P&L, and total return fields. Missing reports produce
`No report` and no fabricated delta.

## Validation

Acceptance requires API and integration tests for 2/10-member success, 1/11-member rejection,
whole-batch validation, dynamic maximums, unknown fields, normalized duplicates, real engine
differences, restart recovery, and mixed member states. Frontend tests cover configuration
controls, backend-driven limits, submission, status/result rendering, and pair selection.

Installed-wheel Chromium must create and complete a two-member batch through actual loopback HTTP,
verify distinct persisted snapshots and formal results, reuse Parameter Delta and Result Delta,
restart the service, and reopen the same persisted batch outside a source checkout.

## Consequences

- Users can run and revisit one explicit bounded parameter set without manual repetition.
- One member failure does not hide successful members or make the batch unreadable.
- Execution remains serial and local; parallel speed is not a V1 requirement.
- Grid search, parameter generation, optimization, ranking, retry orchestration, multi-strategy or
  multi-scenario batches, paper/live trading, brokers, deployment, and publication remain outside
  the product boundary.
