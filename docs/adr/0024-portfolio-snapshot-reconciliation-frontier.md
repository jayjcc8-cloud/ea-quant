# ADR 0024: Portfolio Snapshot Reconciliation Frontier

- Status: Proposed
- Date: 2026-08-13
- Decision owners: Architecture, Ledger/Reconciliation, Runtime, Durability
- Related: ADR 0010, ADR 0022, ADR 0023, Issue #64, Draft PR #65

## Context

ADR 0010 defines `UnresolvedFillRef` narrowly: it records an economically applied Fill whose
Order, correlation, or causation ancestry is missing. ADR 0022 later defines the broader combined
reconciliation predicate as the logical OR of that missing-ancestry predicate and the exact
processing outcome's `requires_reconciliation` flag. The portfolio frontier must therefore retain
an `OpenReconciliationRef` for complete-ancestry overfill, late-terminal, projection, and binding
anomalies as well as for missing ancestry.

The first Issue #64 implementation incorrectly required every open reconciliation reference to
duplicate an unresolved-Fill reference. That made complete-ancestry processing anomalies
unrepresentable or encouraged fabrication of missing ancestry. It also added a portfolio-snapshot
v2 schema and ancestry evidence encodings without freezing their compatibility and canonical
form in an accepted decision.

No portfolio-snapshot v2 value or reconciliation adjustment has been emitted by a released
implementation. The candidate can therefore be corrected without migrating durable v2 history.
Accepted ADRs are immutable, so this decision narrowly supersedes ADR 0010's portfolio-snapshot
v1 schema table/document and specifies the ADR 0022/0023 frontier evidence needed by later
stateful integration. All unrelated ledger, posting, append, retry, and adjustment rules remain
unchanged.

## Decision

### Independent reconciliation predicates

`UnresolvedFillRef(fill_id, fill_sha256)` continues to mean only that an applied Fill has missing
Order, correlation, or causation ancestry. It is neither a general reconciliation marker nor
evidence that a processing anomaly occurred.

`OpenReconciliationRef(fill_id, fill_sha256, processing_outcome_sha256)` means that the combined
ADR 0022 reconciliation predicate was true for the exact applied Fill and processing outcome. A
complete-ancestry Fill may therefore have an open reconciliation reference and no unresolved-Fill
reference. A missing-ancestry Fill processed through the audited integration path has both. If the
same Fill occurs in both tuples, its Fill digest must match exactly.

Every open reconciliation reference has one exact `ExistingLedgerBinding` retained in the
snapshot's `open_reconciliation_bindings` tuple. The binding commits the applied ledger entry,
Fill identity and digest, and transaction digest. The reference and binding tuples are separately
canonical, have unique Fill keys, and have a one-to-one exact Fill-ID/digest correspondence. This
proves that an open reference names an applied Fill without misusing missing-ancestry state or
embedding the unbounded complete Fill index in every snapshot.

The later stateful authority defined by Issue #66 must construct the transaction, general replay
indexes, open reference, open binding, and candidate snapshot before one atomic state swap. It
adds both open tuples exactly when the combined predicate is true. Resolution removes only the
matching open reference and open binding. An ancestry resolution may additionally remove the
matching unresolved-Fill reference; a balance correction does not. Legacy ADR 0010 `apply_fill`
continues to create unresolved-Fill references only and never fabricates a processing-outcome
binding.

### Portfolio snapshot v2

`PortfolioSnapshot` uses schema version `2`, canonicalization
`ea-portfolio-snapshot-v2`, and digest domain `b"ea.portfolio-snapshot.v2\0"`. Its exact canonical
document is ADR 0010's v1 document with these version identifiers and exactly two additional
fields:

```text
{
  ...,
  "open_reconciliation_bindings": [existing_ledger_binding_document, ...],
  "open_reconciliation_refs": [open_reconciliation_reference_document, ...],
  ...
}
```

An open reconciliation reference document contains exactly `fill_id`, `fill_sha256`, and
`processing_outcome_sha256`. An existing ledger binding retains ADR 0010's exact document:
`entry_id`, `fill_id`, `fill_sha256`, and `transaction_sha256`.

Both new tuples sort by canonical Fill economic identity. Version zero requires both to be empty.
All v1 fields and invariants remain unchanged, except that the snapshot's semantic carrier also
exposes these two exact immutable tuples and the independent-predicate invariants above.

No v1 compatibility decoder or migration is added. A v1 snapshot is not a v2 snapshot. Every
downstream value that commits exact snapshot bytes or its digest, including ledger outcomes,
portfolio-risk exposure, reconciliation outcomes, and adjustment commands, deliberately receives
the v2 digest. Recovery must reject a version/domain mismatch rather than reinterpret bytes.

### Ancestry scope and provenance

The `propose_ancestry_resolution` path uses exact Order-detail evidence. Its declared scope ID is
the `RuntimeIdentifier` text:

```text
execution_order:<canonical run UUID>:<base-10 Order owner sequence>
```

Its provenance payload digest is SHA-256 over the domain
`b"ea.reconciliation-ancestry-evidence.v1\0"`, followed by the unsigned 64-bit big-endian byte
length and the strict canonical JSON payload:

```text
{
  "canonicalization": "ea-reconciliation-v1",
  "fill_id": economic_id_document,
  "fill_sha256": lowercase_sha256,
  "order_id": economic_id_document,
  "order_sha256": lowercase_sha256,
  "processing_outcome_sha256": lowercase_sha256,
  "run_id": canonical_run_UUID,
  "schema": "ea.reconciliation-ancestry-evidence.v1"
}
```

The private command factory independently verifies the exact retained open reference and binding,
Fill bytes/digest, Order bytes/digest, run and specification binding, declared scope, provenance
digest, and compatibility of every already-known ancestry field. It cannot use a different Fill,
Order, processing outcome, or portfolio frontier.

## Consequences

- Processing anomalies with complete ancestry remain representable without false unresolved
  ancestry.
- Every open reference is tied to an applied transaction while snapshots avoid the unbounded
  complete Fill index.
- Snapshot-dependent digests change once before any released v2 history exists.
- Stateful population, removal, restart recovery, and atomicity remain Issue #66 work; coordinator
  composition remains Issue #67 work.
- No dependency, external write, live-trading capability, or arbitrary adjustment API is added.

## Verification

Implementation must prove:

- strict v2 canonical bytes and domain digest, including both exact ordered open tuples;
- a complete-ancestry Fill may have an open reference/binding without an unresolved-Fill reference;
- missing-ancestry and open tuples remain independent but reject a shared Fill with a different
  digest;
- missing, duplicate, differently ordered, cross-run, or mismatched open bindings fail closed;
- exact ancestry scope/provenance bytes and cross-Fill or cross-Order substitution fail closed;
- snapshot-dependent golden digests and cross-process evidence are stable; and
- no stateful adjustment, coordinator, recovery, or external-effect authority enters Issue #64.
