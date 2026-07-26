# ADR 0009: Runtime Root Ordering Clarifications

Date: 2026-07-26

## Status

Accepted

## Context

Accepted ADR 0008 freezes deterministic root ranks, domain-specific suffixes, tagged encodings for
optional values, and duplicate-domain validation. Issue #41 is the first implementation of that
contract.

The safety suffix table names five conceptual values:

```text
(safety_kind_rank, producer_namespace, producer_sequence, subject_kind, subject_id)
```

The same ADR separately requires an optional subject to use the exact tagged encoding absent
`(0, "", "")` or present `(1, subject_kind, subject_id)`. Those statements do not say whether the
tag is nested or flattened in the comparable key. They also do not enumerate which suffix fields
form the transport identity for safety, timer, and end-of-run roots.

Leaving either point to implementation would permit two conforming-looking runtimes to order or
classify the same inputs differently. The first bounded plan also needs a construction and error
boundary that cannot be bypassed by manually instantiating a supposedly validated plan.

This decision only removes those ambiguities. It assigns no new root or local rank and does not
change the causal, availability, or no-look-ahead semantics of ADR 0008.

## Decision

### Safety suffix is one flat six-field tuple

The exact safety suffix is:

```text
(
  safety_kind_rank,
  producer_namespace,
  producer_sequence,
  subject_presence,
  subject_kind,
  subject_id,
)
```

An absent subject contributes `(0, "", "")`. A present subject contributes
`(1, subject_kind, subject_id)`. The empty strings are structural sentinels and are never valid
present identifiers.

This is the only comparable representation. A nested subject tuple, omitted presence tag, stable
sort, or caller-supplied suffix is invalid.

### Domain ingress identities are sequence authorities

Before full root-key comparison, the bounded plan validates these exact domain identities:

```text
safety:    (producer_namespace, producer_sequence)
timer:     (timer_namespace, producer_sequence)
end:       (run_id, producer_namespace, producer_sequence)
```

Producer sequences are unbounded non-negative integers and are unique within the scopes above.
Reusing an identity with a changed local kind, subject, timer ID, availability time, or other root
content is a transport conflict, not a second root. It returns
`validation.conflicting_id`.

Execution-fact ingress keeps its Accepted identity
`(source_namespace, ingress_sequence)`. Market-data identities and revision-history validation
remain exactly ADR 0004. Full runtime-order-key uniqueness is checked separately after every
domain identity check.

### Bounded plans are factory-only

`BoundedRuntimeRootPlan` is a final immutable value with a private construction seal.
`prepare_bounded_runtime_roots` is its only public creation path. The factory materializes once,
validates exact root types, validates domain identities, rejects duplicate complete keys, sorts by
the complete canonical key, and rejects an empty plan before issuing the seal.

`DeterministicRootQueue` accepts only an exact sealed plan. It does not repair, revalidate,
re-sort, or accept a duck-typed substitute.

### Error translation is closed

Issue #41 uses only `RuntimeOrderingError` with an exact `.code` from this table:

| Condition | Code |
|---|---|
| non-iterable outer input; unsupported root; wrong exact nested carrier; bool/subclass where an exact carrier is required; unsealed/wrong plan | `validation.invalid_type` |
| invalid runtime identifier grammar; non-UTC time; negative sequence; empty bounded plan; `peek` or `pop` after queue exhaustion | `validation.out_of_range` |
| duplicate market record/emission/order key; market revision availability or source-sequence regression; duplicate fact ingress identity; duplicate safety/timer/end identity; duplicate complete runtime key | `validation.conflicting_id` |

New root constructors precheck exact carrier types before calling existing UTC/run helpers. An
exact existing `MarketDataEnvelope` or `ExecutionFactIngress` is already structurally valid; when
the complete market subset raises `MarketDataValidationError`, every currently reachable batch
failure is one of the conflict conditions above and is translated to
`validation.conflicting_id`. No message text selects the code.

Queue exhaustion is a deterministic invalid access to a completed bounded plan. Both `peek` and
`pop` raise `RuntimeOrderingError(validation.out_of_range)`; neither returns `None`, repeats a
root, nor advances the index.

## Consequences

- Safety keys and golden traces have one byte- and process-independent tuple shape.
- A producer sequence identifies one root occurrence; changing the occurrence under that sequence
  is detected before sorting.
- A validated plan cannot be forged through a public dataclass constructor.
- The runtime boundary exposes one stable error family without parsing exception text.
- The clarification remains intentionally bounded: lifecycle, dispatch IDs, audit gates,
  reconciliation payloads, and incremental source frontiers still require later Issues.

## Rejected alternatives

### Nest the optional subject tuple

Rejected because it creates a different key shape from the direct tagged-field expansion selected
for literal golden vectors and cross-language implementations.

### Include local kind or payload fields in the domain identity

Rejected because it would allow one producer sequence to name several different occurrences.
Those fields belong to content and the complete key; they cannot weaken sequence uniqueness.

### Let the queue validate arbitrary plan-like objects

Rejected because it duplicates the factory invariant and permits ordering authority to drift
between construction and consumption.

### Add queue-specific outcome codes

Rejected because bounded exhaustion is an invalid range access inside this slice, not a new
persisted runtime outcome family.
