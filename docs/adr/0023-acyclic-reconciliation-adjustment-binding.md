# ADR 0023: Acyclic Reconciliation Adjustment Binding

- Status: Proposed
- Date: 2026-08-13
- Decision owners: Architecture, Ledger/Reconciliation, Runtime, Durability
- Related: ADR 0008, ADR 0010, ADR 0020, ADR 0022, Issue #64, Draft PR #65

## Context

Accepted ADR 0022 requires a `ReconciliationOutcome` to contain the proposed adjustment-command
digest or null. The same decision requires the adjustment-command document to contain the
`ReconciliationOutcome` digest. For either adjustment variant, each digest therefore depends on
the final canonical bytes of the other value.

That mutual dependency is not an ordering inconvenience. Domain-separated SHA-256 values cannot
in general be constructed as a fixed point, and searching for one is neither bounded nor part of
the deterministic Phase 1 contract. Omitting one binding in code, using placeholder bytes, or
hashing a partially encoded value would silently create a second protocol that recovery could not
verify from the accepted documents.

The conflict was found while implementing Issue #64 after the independent
`ReconciliationObservation` contract had already been frozen at
`02333f4d08571eae1d568dea5df4f7764a640014`. No outcome, adjustment command, authorization, or
adjustment record has been emitted by a released implementation, so there is no durable v1
reconciliation-outcome history to migrate.

Accepted ADRs are immutable. This ADR narrowly supersedes ADR 0022's mutual outcome/command digest
binding and the reconciliation-outcome schema version. Every unrelated ADR 0022 ledger, risk,
authorization, publication, resource, terminal, recovery, and capability rule remains unchanged.

## Decision

### Acyclic evidence order

The only valid construction order is:

```text
observation
  -> reconciliation outcome v2
  -> exact outcome audit acknowledgement
  -> factory-derived adjustment command
  -> adjustment authorization
  -> exact authorization audit acknowledgement and audited wrapper
  -> ledger adjustment
```

`ReconciliationOutcome` commits the deterministic proposal basis; it does not claim that an
adjustment command already exists. The private command factory can run only after the exact outcome
acknowledgement has been verified. It receives the retained observation, acknowledged local
portfolio frontier, exact outcome, and the evidence required by the selected action. The command
continues to bind the complete reconciliation-outcome digest. The later authorization continues to
bind both the command digest and the prior outcome-acknowledgement digest.

This order has one direction of causality and one audit gate. There is no placeholder, partial
document, fixed-point search, mutable backfill, or retry-dependent digest.

### Reconciliation outcome v2

The reconciliation outcome schema becomes `ea.reconciliation-outcome.v2` and its digest domain is
`b"ea.reconciliation-outcome.v2\0"`. Its complete canonical document contains exactly:

- schema and canonicalization;
- run ID and dispatch sequence;
- observation digest;
- acknowledged local snapshot version and digest;
- acknowledged ledger sequence;
- watermark comparison;
- the ordered bounded discrepancy tuple;
- the existing ADR 0008 outcome code;
- one closed requested action; and
- the exact halt-requested flag.

The v1 `proposed_adjustment_command_sha256` field is removed and has no v2 replacement. For
`propose_single_target_adjustment`, the exact one-entry discrepancy, observation digest, and local
frontier are the complete proposal basis. For `propose_ancestry_resolution`, the exact observation
names the scope and provenance while the private command factory additionally rebinds the named
`OpenReconciliationRef` and independently resolved canonical Order evidence. Other actions cannot
create a command.

The outcome action matrix, discrepancy cardinality, comparison rules, halt behavior, 16,384-byte
cap, strict canonical decoding, and audit kind/subject remain those of ADR 0022. The
`reconciliation.observation_outcome` audit validator accepts only the v2 schema.

### Command and authorization

The adjustment-command schema and digest domain remain
`ea.reconciliation-adjustment-command.v1`. Its canonical document continues to bind the complete
v2 reconciliation-outcome digest. Its private factory additionally requires the exact outcome
audit acknowledgement as a process-local issuance capability, but the acknowledgement is not
duplicated into the command document.

`ReconciliationAdjustmentAuthorization` remains the durable pre-effect decision. It binds the
same outcome digest, command digest, and prior outcome-acknowledgement digest defined by ADR 0022.
The authorization and its audited wrapper therefore prove both that the proposal basis was durable
and that the command was derived after that durable boundary.

### Retry and recovery

Exact retry first resolves the v2 outcome by observation identity and dispatch sequence. If its
audit acknowledgement is missing, retry may only settle that exact append. Once acknowledged, the
private factory re-derives the same command bytes from the same authoritative observation, local
frontier, outcome, and variant evidence. An existing authorization must bind that exact command
digest and outcome acknowledgement; any mismatch is a conflict.

Recovery rejects a v1 reconciliation-outcome payload. It reconstructs the same one-way chain in
physical audit order and never synthesizes a command from an unaudited outcome. Look-ahead,
missing acknowledgement, different local frontier, different named open reference, or different
Order evidence remains fail-closed under ADR 0022.

## Consequences

- Outcome and command digests are constructible, deterministic, and restart-equivalent.
- The audited outcome remains the only durable proposal basis before authorization.
- The command cannot weaken or edit the outcome because its factory inputs and resulting digest
  are rebound by authorization.
- One schema version changes before any released durable record exists; no migration path or
  compatibility decoder is added.
- ADR 0022's resource formula and record count are unchanged because no record family is added.

## Verification

Implementation must prove:

- strict v2 outcome round-trip, closed fields, action/discrepancy matrix, and domain digest;
- v1 outcome and any command-digest field are rejected;
- command construction is unavailable before exact outcome acknowledgement;
- exact retry re-derives identical command bytes and authorization bindings;
- recovery rejects missing, stale, ahead, orphaned, or contradictory outcome/command evidence;
- cross-process bytes and digests are stable under hash seed, timezone, locale, decimal context,
  and input-order changes; and
- the existing 600,006-record, 13-GiB, and 256-MiB index bounds remain unchanged.
