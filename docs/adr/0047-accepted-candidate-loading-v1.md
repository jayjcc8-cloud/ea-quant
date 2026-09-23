# ADR 0047: Accepted Candidate Loading V1

Date: 2026-09-23

## Status

Accepted for implementation — Issue #208, Personal Production V2 Phase A PPV-07.

## Decision

Extend ADR 0043 with an explicit accepted-candidate loading guard. Candidate existence or an
ACCEPTED research decision does not grant execution permission. The caller must request the
specific offline operation; later Paper composition needs its own authorized entry.

Share one read-only evidence reader between Web decisions and runtime loading. Recheck the
canonical record, source/Holdout relationship, captured snapshots, report/path identities and
frozen package bytes. Do not start a Web service or execute package code to inspect this binding.
Expose immutable candidate, artifact, parameter and configuration identities to the caller.
Executable loading additionally requires the recorded EA code/distribution identity; inspection
remains available for intact historical evidence after an upgrade.

Before the accepted offline run executes package validation or strategy code, compare the strict
scenario's normalized non-data configuration against the accepted configuration. Configuration
includes strategy/lifecycle, instrument, funding, risk, execution costs and randomness. Data may
be a separately validated input; original source/Holdout data identities remain pinned in the
binding. Resolve a local strategy only from the freshly verified frozen accepted artifact.

Use the existing scenario loader and simulation engine. Persist accepted identity in an attempt
sidecar and carry candidate_id into operational logging without changing legacy report, ledger,
audit or economic identity schemas. A changed binding or artifact fails closed before execution.

## Validation and limits

Verify explicit lifecycle selection, nominal report/path V1/V2/V3 evidence, immutable decisions,
artifact/configuration mismatch, no-code inspection, deterministic accepted loading and the real
CLI path. Existing installed Web acceptance exercises creation, acceptance and reopen after
external source deletion. Trusted local package execution is not a Python sandbox. Continuous
Paper runtime, external connectivity, Live authority and durable recovery remain outside PPV-07.
