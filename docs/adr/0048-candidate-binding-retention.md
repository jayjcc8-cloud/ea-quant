# ADR 0048: Candidate binding retention

Date: 2026-09-27

## Status

Accepted for implementation — Issue #208, PPV-07 concentrated repair.

## Decision

Supersede ADR 0047's sidecar-only persistence detail. A new Candidate-bound attempt also
records the sidecar SHA-256 in its existing canonical attempt manifest. Existing audit/store
manifest binding protects this marker. Missing or changed binding evidence rejects resume
and reporting before reconstructing executable strategy code; legacy unbound attempts retain
their exact manifest bytes and identities. No recovery frontier or second provenance store is added.

Within one Candidate admission operation, read each evidence file once and pass the verified
immutable bytes onward. A later operation creates a fresh reader; no cross-run cache is allowed.
Normalize local V1 optional parameters from the non-executable frozen package descriptor before
configuration comparison. Complete V2/V3 action parameter maps remain mandatory.

## Validation

Direct tests cover missing bindings on completed and interrupted attempts, directory containment,
Mac workspace aliases, optional defaults and rejection before executing mismatched local code.
Existing unbound resume/report tests retain the legacy contract.
