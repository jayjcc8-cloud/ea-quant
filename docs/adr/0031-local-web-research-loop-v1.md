# ADR 0031: Local Web Research Loop V1

Date: 2026-09-05

## Status

Accepted

## Context

ADR 0030 permits one real loopback browser path over the installed offline engine, but its UI is
still a sequence of isolated prepared-scenario runs. A minimal research loop needs to vary a
bounded input, retain the accepted input identity, revisit completed attempts, and compare two
results without introducing a second execution, reporting, experiment, or instrument authority.

Allowing arbitrary symbol text would cross that boundary. A symbol is inseparable from the
registered market-data selection, venue, instrument specification, and their fingerprints and
validation semantics.

## Decision

Extend the ADR 0030 local Web adapter with one bounded V1 research loop:

- `initial_cash` and bounded-long `quantity` are editable and pass through the public scenario
  validation and canonicalization contract;
- `symbol` is read-only and comes only from a registered, successfully validated scenario/data
  combination; the HTTP request schema rejects a caller-supplied symbol or other extra field;
- every accepted Web job persists its creation time, attempt identity, a domain-separated digest,
  and the full resolved, validated, normalized scenario plus source and final input identities;
- history is newest-first from persisted creation time and survives browser refresh and service
  restart;
- “Use parameters” copies values from the immutable normalized snapshot into a new unvalidated
  form and always creates a fresh attempt after validation; and
- pairwise comparison is a derived read-only view over two persisted snapshots and their verified
  formal reports. Exact decimal strings are subtracted without binary floating point. A failed or
  missing report remains “No report” and has no result delta.

The fixed acceptance constants are:

```ini
SYMBOL_FREE_TEXT=DISALLOWED
SYMBOL_SOURCE=REGISTERED_VERIFIED_SCENARIO_DATA_COMBINATION
INITIAL_CASH_EDITABLE=true
QUANTITY_EDITABLE=true
```

`input_snapshot` is not raw browser form state. It is the canonical final input identity and
scenario accepted by the service after resolution, validation, and normalization. The normalized
scenario is materialized inside the authorized workspace so the existing engine and reporter can
reload and bind the same immutable source rather than a mutable registry file.

This decision extends ADR 0030 only for the bounded workflow above. It does not create an
Experiment model, database, comparison artifact, strategy editor, upload path, symbol/data
configuration system, or new execution/report authority.

## Validation

Acceptance requires unit/component coverage plus Chromium driving a production Web build into a
repository-outside installed candidate wheel. The path must prove two successful runs with changed
quantity and exact deltas, replay from the first snapshot, persistence across refresh and service
restart, success-versus-`risk.rejected` comparison without fabricated metrics, rejection of free
symbol input, and continued HTTP idempotency and formal-report identity checks.

## Consequences

- The user can perform a small causal “change, run, compare, repeat” loop using existing product
  contracts and durable evidence.
- Historical jobs identify the exact normalized inputs that ran, even if registered source files
  later change.
- Comparison remains reconstructible and deliberately has no lifecycle or persistence of its own.
- New symbols, datasets, venues, instrument specifications, richer analytics, optimization,
  experiment tracking, paper/live trading, brokers, deployment, and package publication remain
  unavailable and require separate decisions.
