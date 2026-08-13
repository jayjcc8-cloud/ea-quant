# ADR 0022: Audited Ledger and Reconciliation Integration

Date: 2026-08-13

## Status

Proposed

## Context

Accepted ADRs 0008, 0010, 0014, 0020, and 0021 define the deterministic root order, the
Fill-derived append-only ledger, trusted execution-fact outcomes, durable audited handoffs, and
the staged active-dispatch window. Issue #61 deliberately stops at an
`AuditedExecutionFactHandoff`: it proves that an authoritative processing outcome and any named
Fill are durable, but it does not apply that Fill to the canonical ledger.

The remaining boundary is safety-critical. Applying a raw Fill would bypass the inbound audit
gate. Applying before an outcome is durable would make crash recovery ambiguous. Publishing a
portfolio snapshot before the ledger result is durable would permit look-ahead after restart.
Treating a venue position or cash snapshot as authoritative would overwrite transaction evidence.
Finally, a Fill carrying incomplete or contradictory ancestry must still retain its complete
economics exactly once while forcing reconciliation and blocking new submissions.

This decision freezes the Phase 1 contract before executable implementation. It does not authorize
live trading, broker writes, automatic balance correction, or arbitrary ledger postings.

## Scope

This decision defines:

- the only eligible audited-handoff-to-ledger path;
- one result for every handoff, including handoffs with no Fill;
- audit-before-publication ordering for ledger results and portfolio snapshots;
- monotone reconciliation and risk-halt behavior;
- explicit, separately authorized reconciliation adjustments;
- dispatch-completion frontiers, exact retry, and restart recovery; and
- the composition boundary that keeps mutable ledger, reconciliation, and risk capabilities
  private to the lifecycle owner.

## Non-goals

- strategy evaluation, order planning, result/report adapters, or the backtest CLI;
- venue submission, credentials, network I/O, or a live reconciliation poller;
- automatic correction from aggregate position or cash snapshots;
- changing Fill economics, fact classification, matcher eligibility, or root ranks;
- importing a general accounting, event-sourcing, or broker SDK dependency; and
- initial funding. Initial funding remains a later manifest-bound, separately authorized economic
  transition and cannot use the Fill or correction APIs defined here.

## Reuse decision

The implementation reuses `ea.portfolio.ledger.PortfolioLedger`, the canonical portfolio values,
the existing execution-fact authority resolvers, the POSIX audit journal, and the lifecycle
coordinator. A narrow adapter is smaller and safer than a second ledger.

The standard-library `sqlite3` module is rejected as a competing persistence and transaction
authority. General accounting and event-sourcing packages are rejected because none of the locked
project dependencies preserves the exact Fill, run, specification, audit-acknowledgement, root,
and replay identities required here; adding one would introduce a second schema and a larger
supply-chain and recovery surface. No dependency is added.

## Decision

### Ownership and dependency direction

`ea.core` owns immutable dependency-neutral ledger-integration and reconciliation messages,
strict canonical codecs, and domain-separated digests.

`ea.portfolio.ledger` remains the sole mutable economic authority. Its existing `apply_fill`
semantics remain unchanged. A later narrow method may apply a factory-issued reconciliation
adjustment, but it accepts neither caller-supplied postings nor mutable balances.

`ea.runtime` owns orchestration, audit gates, failure state, and recovery. It may call portfolio
and risk ports but cannot construct a Fill, transaction, snapshot, or adjustment.

`ea.composition` constructs the concrete ledger and risk adapters and gives their mutable
capabilities only to the lifecycle coordinator. Public lifecycle bundles expose read-only history
and snapshot views. Inner portfolio and risk modules do not import runtime, audit, composition, or
each other.

### Closed immutable values

The following factory-only values use strict canonical JSON, exact runtime types, closed enums,
unsigned 64-bit sequences, canonical UTC, and domain-separated SHA-256 digests.

`LedgerHandoffOutcome` binds:

- run ID, dispatch sequence, ingress identity, and audited-handoff digest;
- processing-outcome digest and outcome-record acknowledgement digest;
- optional exact Fill ID and Fill digest;
- one closed action: `not_applicable`, `effect_committed`, or `failed`;
- the optional original canonical `LedgerApplyOutcome` bytes and digest retained for the first
  application, never a retry-dependent replacement;
- before and after portfolio snapshot versions and digests;
- exact `requires_reconciliation` and `halt_requested` flags; and
- an optional closed failure reason.

Every audited handoff has exactly one outcome. A no-Fill handoff is `not_applicable` and binds one
unchanged snapshot. A Fill handoff is never `not_applicable`. `effect_committed` normalizes an
initial `ledger.applied` and every exact retry to the same original transaction, outcome, and
after-snapshot bytes. A retry-returned `ledger.duplicate` is resolver evidence, not a different
durable handoff result. A ledger conflict, arithmetic failure, unbalanced result, structural error,
or evidence mismatch is `failed`; it never substitutes an existing transaction or advances the
published portfolio frontier.

`LedgerApplicationCommand` binds the exact audited handoff, processing outcome, Fill, and
`requires_reconciliation` decision. It is factory-issued only after all three evidence values are
rebound. The ledger's integration method accepts this command and the exact Fill; the legacy
Fill-only method is not an integration authority.

For an outcome-bound application, transaction `requires_reconciliation` is exactly the logical OR
of ADR 0010's missing-ancestry predicate and the processing outcome's flag. The snapshot retains an
`OpenReconciliationRef` binding the Fill and processing-outcome digests for every true result, so
overfill, late-terminal, projection, and binding anomalies cannot disappear merely because the
Fill has complete local ancestry. This narrowly supersedes ADR 0010's Fill-only derivation when the
new command is used; existing Fill-only semantics remain unchanged.

The integration authority retains a non-evicting index keyed by audited-handoff digest. Each
binding stores command digest, processing-outcome digest, Fill digest, original ledger-apply
outcome bytes, transaction digest, and before/after snapshot digests. Exact command replay returns
that original binding. A ledger duplicate is accepted as integrated success only when this exact
binding already exists. If the Fill indexes exist but the integration binding does not, including
a Fill previously applied through legacy `apply_fill`, integration fails closed as
`unbound_existing_fill`; it never retroactively invents audited provenance.

For a genuinely new command the integration method precomputes the Fill/fact/entry indexes,
transaction, balances, snapshot, open reconciliation references, original apply outcome, and
handoff-command binding in one complete immutable candidate state. One state-pointer swap commits
all of them indivisibly. Any validation, encoding, allocation, or callback failure before that swap
leaves every index and observable byte unchanged; no fallible work occurs after the swap before the
original result is retained. Exact retry therefore observes either no mutation or the complete
binding. It can never observe economic mutation without provenance-index mutation.

`AuditedLedgerHandoff` binds one `LedgerHandoffOutcome` to its exact audit acknowledgement. It is
the only portfolio-update evidence exposed beyond the coordinator.

`ReconciliationObservation` is an independently admitted rank-20 root. It binds the accepted ADR
0008 root fields, one closed observation kind, source provenance, comparable watermark, and strict
kind-specific payload. Trade detail is normalized through the execution-fact path. Order detail is
projection/query evidence only. Position and cash snapshots contain complete ordered exact
balances for their declared scope and never carry a ledger transaction.

Its document contains exactly: schema/canonicalization, run/specification-set ID and digest,
observation ID, kind, source namespace/sequence, occurred/available times, watermark
namespace/sequence, declared scope kind and ID, provenance ID/payload digest, and `balances`.
`balances` is empty for trade/order detail and contains 1..32 canonically sorted exact entries for
position/cash snapshots. Each entry is exactly one instrument plus quantized quantity or one
settlement currency plus quantized amount. The declared scope is complete: omission means the
payload is invalid, not zero. Observation identity is
`(source_namespace, source_sequence)`; identical identity/bytes is replay and different bytes is
conflict. Canonical encoding must also fit the 16,384-byte audit payload cap; an oversized complete
scope is rejected before root admission and cannot be split implicitly.

`ReconciliationOutcome` binds the observation and current ledger frontier and has exactly one
existing ADR 0008 outcome code. It includes comparison evidence and a closed requested action, but
does not itself mutate the ledger.

Its document contains exactly: observation digest, acknowledged local snapshot version/digest and
ledger sequence, watermark comparison, ordered discrepancy tuple, outcome code, requested action,
proposed adjustment-command digest or null, halt requested, and dispatch sequence. Requested action
is exactly `none`, `request_missing_trade_facts`, `retain_and_halt`,
`manual_evidence_decomposition`, or `propose_single_target_adjustment`. The discrepancy tuple is
empty except for mismatch/quarantine and is capped at 32 entries.

`ReconciliationAdjustmentAuthorization` is an immutable one-use authorization for one exact
correction. It binds run/specification, observation and reconciliation-outcome digests, current
ledger sequence and acknowledged snapshot digest, one exact factory-derived correction command,
authorizer policy identity/version/digest, decision, available time, dispatch sequence, and unique
authorization and adjustment IDs. Only a trusted composition-owned authorization port can issue
it. A denial is durable evidence and can never reach the ledger.

`ReconciliationTransaction` is cause-discriminated and is not ADR 0010's Fill-bound
`LedgerTransaction`. Both variants use the same ledger-owned entry sequence, previous-transaction
digest chain, and snapshot-version increment:

- `balance_correction` names exactly one instrument-position or settlement-currency cash target,
  the observed amount, local amount, and exact non-zero delta. The ledger derives exactly two
  opposite postings. Position uses `portfolio.position` and `external.inventory`; cash uses
  `portfolio.cash` and `external.settlement`. Callers supply no account, commodity, amount,
  posting, entry ID, or order.
- `ancestry_resolution` names exactly one existing open reconciliation reference, its original
  Fill and outcome digests, and newly proven correlation evidence. It has zero economic postings,
  removes only that exact reference, and changes no cash, position, or rounding balance.

For `balance_correction`, `delta = observed_amount - local_amount` under the existing exact
canonical-decimal arithmetic. An instrument target requires the delta to be an exact multiple of
its quantity quantum and derives, in this order,
`portfolio.position += delta` and `external.inventory -= delta`. A cash target requires the delta
to be an exact multiple of that currency's quantum and derives, in this order,
`portfolio.cash += delta` and `external.settlement -= delta`. Both commodity sums are exactly zero.
Overflow, zero delta, wrong grid/currency/specification, or an unrepresentable opposite amount
returns a closed failed outcome before mutation. Rounding and arbitrary residual postings are
forbidden for corrections.

No command may combine variants or targets. A comparison with zero delta is `match`, not an
adjustment. A snapshot with zero or more than one discrepant target is not automatically
correctable and returns `quarantined`. This Phase 1 limit keeps every economic correction at
exactly two derived postings and prevents a balanced-posting envelope from becoming an arbitrary
ledger API.

Its document contains exactly: run ID, ledger entry ID, adjustment ID, authorization digest,
observation/outcome/command digests, variant, target, before/observed/delta amounts where
applicable, resolved reconciliation reference where applicable, previous transaction digest,
instrument-specification-set ID/digest, ordered derived postings, and occurred/available times.
The ledger uses one cause-discriminated `CanonicalPortfolioTransaction` union of the existing
Fill-derived transaction and this reconciliation transaction for its shared previous-digest chain;
neither variant fabricates fields belonging to the other.

`ReconciliationAdjustmentOutcome` binds the authorization, resulting reconciliation transaction
and snapshot, and one closed result: `applied`, `duplicate`, `conflict`, or `failed`.

The adjustment command document contains exactly: schema/canonicalization, run/specification
binding, adjustment ID, observation and reconciliation-outcome digests, acknowledged ledger
sequence and snapshot digest, variant, target, local/observed/delta amounts or exact open-reference
and newly proven ancestry fields, and dispatch sequence. Its factory derives every field except the
already canonical observation/outcome evidence. The authorization document adds authorization ID,
policy ID/version/digest, decision, command digest, and the prior reconciliation-outcome
acknowledgement digest. It does not contain its own record ID, payload digest, acknowledgement, or
chain head. After that payload is appended and exactly acknowledged, an
`AuditedReconciliationAdjustmentAuthorization` binds authorization digest, authorization record ID,
acknowledgement digest, and chain head; only this wrapper reaches the ledger. The adjustment outcome
document adds result, failure/conflict kind, original transaction ID/digest or null, and
before/after snapshot digests. Closed failure kinds are `invalid_command`, `stale_frontier`,
`arithmetic_failure`, and `unbalanced`; closed conflicts are `authorization_id_collision`,
`adjustment_id_collision`, `observation_already_consumed`, and `index_inconsistent`.

`PortfolioRiskRefresh` is separate from `RiskStateSnapshot`. It binds the run, policy identity and
digest, exact portfolio snapshot version/digest, current monotone risk-state version/digest,
derived exposure digest, dispatch sequence, and whether submission remains permitted. It neither
pretends that the current `0|1` halt version is a portfolio version nor mutates the ledger.

The exposure digest is exactly SHA-256 of
`b"ea.portfolio-risk-exposure.v1\0" + len(snapshot_bytes)_u64 + snapshot_bytes`, where
`snapshot_bytes` are the strict canonical bytes of the candidate portfolio snapshot already bound
by the exact ledger-outcome acknowledgement. Refresh
sequence starts at one and advances once per completed dispatch frontier, including a no-Fill
frontier. Its replay key is `(run_id, dispatch_sequence, ordered_ledger_ack_frontier_sha256)`.
Exact replay returns the original refresh bytes; the same key with different bytes is a conflict.
Its canonical document contains exactly those bindings plus refresh sequence, ordered ledger
acknowledgement-frontier digest, risk-state digest, exposure digest, submission-permitted flag, and
previous-refresh digest or null.

`submission_permitted` is derived, never caller supplied. It is true exactly when the final bound
risk state is not halted, the coordinator is running, the proposed joint publication values equal
the final internal portfolio/risk values, there will be no open reconciliation reference after
that candidate publication, and no ledger, refresh, reconciliation, correction, or audit operation
other than the current refresh acknowledgement/publication remains pending. It is evaluated
against that exact post-publication candidate, not the previous currently published frontier.
After the acknowledgement, the facade must publish exactly that candidate or fail closed;
publication cannot recompute or change the immutable refresh. Otherwise the flag is false.
The flag is evidence, not submission authority. Every later strategy or authorization callback
must re-read and bind the newly published joint frontier after the swap before it may act.

The audit vocabulary adds exact logical kinds and subjects for
`portfolio.ledger_handoff_outcome`, `risk.portfolio_refresh`,
`reconciliation.observation_outcome`, `reconciliation.adjustment_authorization`, and
`reconciliation.adjustment_outcome`. Each subject is the domain-separated digest of its complete
canonical payload. No pre-ledger authorization record is added: the audited fact handoff is the
only Fill-application gate.

The new canonical contracts and digest domains are closed:

| Value | Schema | Domain |
|---|---|---|
| ledger application command | `ea.ledger-application-command.v1` | `b"ea.ledger-application-command.v1\0"` |
| ledger handoff outcome | `ea.ledger-handoff-outcome.v1` | `b"ea.ledger-handoff-outcome.v1\0"` |
| audited ledger handoff | `ea.audited-ledger-handoff.v1` | `b"ea.audited-ledger-handoff.v1\0"` |
| reconciliation observation/outcome | `ea.reconciliation-observation.v1` / `ea.reconciliation-outcome.v1` | the matching schema text plus `b"\0"` |
| adjustment command/authorization | `ea.reconciliation-adjustment-command.v1` / `ea.reconciliation-adjustment-authorization.v1` | the matching schema text plus `b"\0"` |
| audited adjustment authorization | `ea.audited-reconciliation-adjustment-authorization.v1` | `b"ea.audited-reconciliation-adjustment-authorization.v1\0"` |
| reconciliation transaction/outcome | `ea.reconciliation-transaction.v1` / `ea.reconciliation-adjustment-outcome.v1` | the matching schema text plus `b"\0"` |
| portfolio risk refresh | `ea.portfolio-risk-refresh.v1` | `b"ea.portfolio-risk-refresh.v1\0"` |

Every digest uses `SHA-256(domain + payload_length_u64 + canonical_payload)`. Decoders reject
unknown/missing fields, non-canonical JSON, bool-as-int, subclasses, floats, unbounded arrays, and
invalid enum or digest text. Adjustment authorization decisions are exactly `allowed` or `denied`;
command variants are exactly `balance_correction` or `ancestry_resolution`; targets are exactly
`instrument_position`, `settlement_cash`, or `open_reconciliation_ref`. Requested actions are
exactly `none`, `request_missing_trade_facts`, `retain_and_halt`,
`manual_evidence_decomposition`, `propose_single_target_adjustment`, or
`propose_ancestry_resolution`. A denial has no transaction or snapshot fields.

`EconomicOwnerKind` gains `reconciliation.authorization` and `reconciliation.adjustment`.
Authorization sequence belongs only to the private authorization authority. Adjustment identity
uses that authorization's exact proposed adjustment ID; the ledger entry still receives the next
shared `ledger.entry` sequence. Non-evicting indexes cover authorization ID, adjustment ID,
observation identity, and command digest. Exact ID plus identical bytes returns the original
outcome. Same ID, observation, or already-consumed authorization with different bytes is
`conflict`; a denied authorization is permanently burned.

The reconciliation-root order is separately closed:

1. admit and validate the exact rank-20 observation root;
2. compare it with the frozen local ledger watermark and snapshot without mutation;
3. append/verify `reconciliation.observation_outcome`;
4. for requested actions `none`, `request_missing_trade_facts`, `retain_and_halt`, or
   `manual_evidence_decomposition`, publish no correction and move to risk refresh and completion;
5. for a correctable same-watermark mismatch or one order-detail ancestry-resolution row, obtain
   and append/verify one exact `reconciliation.adjustment_authorization`, then construct its
   audited authorization wrapper before any economic effect;
6. rebind observation, local frontier, authorization policy, and active lease, then apply the
   factory-issued adjustment once;
7. append/verify `reconciliation.adjustment_outcome`, resolve/engage and rebind the monotone halt,
   then derive and append/verify risk refresh from that exact internal snapshot and final risk
   state;
8. advance the acknowledged publication frontier only after those acknowledgements; and
9. bind all ordered evidence in dispatch completion before runtime acknowledgement.

An absent or denied authorization retains the mismatch and halt and performs no adjustment.

### Exact handoff eligibility

For each handoff, the coordinator resolves the exact processing outcome by ingress identity and
digest, re-encodes it, and requires its digest to equal the handoff. If the handoff names a Fill,
the coordinator resolves that exact Fill by ID and digest and re-encodes it. Missing, extra,
conflicting, or non-canonical evidence is fatal.

Eligibility is literal:

| Processing evidence | Ledger action |
|---|---|
| no Fill ID and no Fill digest | emit `not_applicable`; no mutation |
| both Fill ID and digest, exact Fill resolves, action is `accepted` or `unresolved` | issue the exact command and apply once or resolve its retained original result |
| only one Fill field, unresolved Fill, or digest mismatch | fail closed; no new mutation |

An accepted or unresolved anomalous fact may carry a real Fill and that Fill must apply once.
Duplicate, conflicting, and invalid fact outcomes cannot carry a Fill under ADR 0014 and therefore
produce a no-mutation result. `requires_reconciliation` is true when either the processing outcome
requests it or the applied/duplicate transaction carries the ADR 0010 unresolved-ancestry flag.

### Per-dispatch order and publication gate

For one active dispatch, the order is:

1. complete the ADR 0020 matcher and execution-fact audit gates;
2. for each audited handoff in batch order, rebind the active lease, outcome, Fill, ledger frontier,
   and risk state;
3. apply or resolve the Fill through the sole ledger authority;
4. append and verify `portfolio.ledger_handoff_outcome` for that exact result;
5. only after that acknowledgement, construct the `AuditedLedgerHandoff`; do not yet publish the
   ledger's internal snapshot;
6. resolve or engage the first monotone risk halt required by the complete ledger frontier, then
   rebind that final risk state;
7. construct and append/verify the exact `PortfolioRiskRefresh` from the final internal ledger
   snapshot and final risk state;
8. atomically advance the acknowledged portfolio/risk publication frontier to those exact
   acknowledged values;
9. after every handoff has one ledger acknowledgement, freeze the ledger frontier;
10. run later strategy/portfolio/risk stages only against that frozen published snapshot; and
11. append `runtime.dispatch_completed`, binding the ordered fact and ledger acknowledgement
   frontiers, final portfolio snapshot digest, final risk-state digest, and any later-stage
   evidence, before acknowledging the runtime lease.

No callback return is trusted without re-reading every mutable authority it could have changed.
The coordinator rebinds active lease, fact outcome, Fill, ledger result/snapshot, and risk state
after each external callback and immediately before completion append and runtime acknowledgement.

`AcknowledgedLifecycleFrontier` is a composition-owned facade with separate internal and published
portfolio and risk states. The mutable ledger may advance its internal snapshot at step 3 and the
risk authority may engage its internal halt at step 6, but every public portfolio/risk history
view, `PortfolioFreshnessPort`, `RiskFreshnessPort`, strategy, authorization, completion encoder,
and terminal evidence reads only the jointly published state. While a ledger result or refresh
acknowledgement is pending, both published states remain the previous acknowledged frontier and the
coordinator is the only holder of the pending internal values. The facade advances the portfolio
snapshot, risk state, and refresh bytes in one state-pointer swap only after exact ledger and
refresh acknowledgements; an exception resolves to exact old or exact new joint publication bytes,
otherwise conflict. The concrete ledger, risk authority, and their immediate state properties never
escape composition.

The completion record moves to a new schema version. An old completion record cannot be interpreted
as proving a ledger frontier. Within one audit journal the order is physical and normative:
fact-outcome acknowledgement precedes ledger-outcome acknowledgement, which precedes dispatch
completion. Recovery rejects a different order even when the logical payloads are otherwise valid.

### Ledger failure and mandatory drain

Ledger, ledger-audit, or risk-refresh failure enters the coordinator's monotone `failing` state
and blocks new submission authorization. The coordinator still drains every already issued
execution ingress in batch order and attempts its fact and ledger audit records. It does not
acknowledge the runtime lease until all required records and dispatch completion are durable.

If `apply_fill` returns `ledger.applied` or `ledger.duplicate`, that authoritative result is
retained. If the subsequent audit append fails, retry resolves the exact ledger result and retries
only the same logical audit key; it never applies different economics. A returned conflict or
failure is itself audited as evidence, leaves the portfolio frontier unchanged, and keeps the run
failing. An exception is resolved against authoritative ledger history before any retry. Three
states are accepted: exact result retained, provably no mutation with unchanged frontier, or
conflict. An ambiguous or advanced frontier is a conflict, never permission to call again.

Accounting is never rolled back when a risk refresh fails after the ledger result is durable. The
new internal snapshot remains authoritative to the coordinator, the public frontier remains the
previous acknowledged snapshot, downstream strategy/submission stays blocked, and exact retry
re-derives and audits only the same refresh.

No audited ledger handoff, snapshot publication, risk refresh, strategy call, or submission is
permitted for an unacknowledged ledger result.

### Risk and reconciliation behavior

Any fact outcome with `halt_requested`, any ledger transaction with
`requires_reconciliation`, any ledger conflict/failure, or any reconciliation mismatch engages a
monotone public halt. A new closed risk halt reason `reconciliation_required` identifies the first
such transition. The first halt cause remains authoritative; later causes are retained in ledger
and reconciliation outcomes without rewriting risk history.

An aggregate position or cash observation is compared only at a comparable watermark:

- equal state produces `reconciliation.match`;
- a lower remote watermark produces `reconciliation.local_ahead_stale` and cannot clear a halt;
- a higher remote watermark produces `reconciliation.remote_ahead` and requests missing
  transaction facts;
- the same watermark with different economics produces `reconciliation.mismatch` and fails closed;
- missing, malformed, or incomparable evidence produces `invalid` or `quarantined`.

The comparison/action table is normative:

| Comparison | Outcome | Requested action | Adjustment eligible |
|---|---|---|---|
| exact equal watermark and balances | `reconciliation.match` | `none` | no |
| remote watermark lower | `reconciliation.local_ahead_stale` | `retain_and_halt` | no |
| remote watermark higher | `reconciliation.remote_ahead` | `request_missing_trade_facts` | no |
| equal watermark, exactly one complete quantized target differs | `reconciliation.mismatch` | `propose_single_target_adjustment` | yes, subject to authorization |
| equal watermark, more than one target differs | `reconciliation.quarantined` | `manual_evidence_decomposition` | no |
| watermark absent/incomparable or payload incomplete | `reconciliation.invalid` | `retain_and_halt` | no |
| unresolved Fill correlation without new exact ancestry | `reconciliation.unresolved_correlation` | `retain_and_halt` | no |
| exact order-detail evidence proves complete ancestry for one named open reference | `reconciliation.match` | `propose_ancestry_resolution` | yes, subject to authorization |

Comparison canonicalizes the complete declared observation scope and the acknowledged local
snapshot before inspecting values. Identity, watermark, scope, and canonical validation precede
economic comparison. A balance command is a pure function of observation bytes, local snapshot
bytes, and the one differing target. An ancestry command is a pure function of exact order-detail
bytes, the named open reference, and independently resolved canonical Order evidence. The
authorizer cannot edit either command.

The authorization policy is factory-bound by exact ID, version, and canonical digest. It may only
return `allowed` or `denied` for the exact proposed command and frontier. `allowed` requires an
eligible row above, unchanged active lease, unchanged acknowledged frontier, unchanged monotone
halt state, and an unused authorization/adjustment identity. Every failed freshness condition is
`denied`; callers cannot override the decision or supply replacement economics. Phase 1 ships no
automatic production authorizer. Test/backtest composition may bind a deterministic manifest-owned
policy, while live/external authority remains a future decision.

Observations never overwrite balances, fabricate a Fill, remove an unresolved-Fill reference, or
clear a halt. A discovered complete trade returns through the execution-fact path.

Only an exact acknowledged `ReconciliationAdjustmentAuthorization` may reach the adjustment
method. The adjustment is balanced, idempotent, linked to its observation and authorization, and
assigned the next ledger-owned sequence. It may resolve only the explicitly named unresolved Fill
or exact discrepancy. It cannot modify or delete prior transactions. Exact replay returns the
original outcome; identity or frontier reuse with different bytes is a conflict. Phase 1 has no
automatic authorizer and therefore cannot silently correct a snapshot.

### Recovery and exact retry

Durable recovery scans the journal in physical sequence and reconstructs, before exposing a
lifecycle:

- fact handoffs and their authoritative fact/Fill histories;
- ledger handoff outcomes, transactions, indexes, snapshots, and unresolved-Fill references;
- reconciliation observations, outcomes, authorizations, adjustments, and one-use indexes;
- the first risk halt and exact risk-state snapshot; and
- the incomplete or completed coordinator frontier.

Recovery replays ledger operations into a fresh empty run/specification-bound ledger and requires
the resulting canonical transaction, outcome, and snapshot bytes to equal the journal evidence at
each sequence. It does not copy private mutable state. Fact, ledger, risk-refresh, and
reconciliation histories must end exactly at the recovered coordinator frontier; a future history
injection is a conflict.

Recovery also rebuilds the non-evicting handoff-command integration index before classifying any
duplicate Fill. It reconstructs the internal ledger frontier first and advances the separate
joint published portfolio/risk frontier only at each verified ledger-plus-refresh acknowledgement
boundary. It replays an internal halt without exposing it through `RiskFreshnessPort` until the
matching refresh acknowledgement is verified. A crash between internal mutation and publication
therefore recovers the previous public portfolio/risk states plus one pending exact command; it
cannot expose internal state early or misclassify a legacy Fill as integrated.

For an incomplete dispatch, recovery groups by dispatch sequence and batch order but also enforces
the physical audit order. A fact outcome with no ledger record is eligible for the same exact
ledger operation. A retained ledger result with no acknowledgement may only retry its audit
append. An acknowledged ledger result is never applied again except as exact replay verification.
Completion without the complete ordered ledger frontier is invalid.

Reopened acknowledgements are accepted only after the journal's exact-retry path performs a fresh
durability barrier and independent readback. Synthetic acknowledgements from decoded record fields
are insufficient.

### Concurrency and capability confinement

The lifecycle coordinator serializes all economic mutation. Public methods use a non-reentrant
mutation guard. Reentrant or concurrent processing fails before any effect. Ledger, adjustment
authorization, risk mutation, and recovery-consumption capabilities are private, unforgeable,
one-use where applicable, and never present in public bundle fields or `__all__` exports.

Fresh and recovered composition use reservation states `available -> assembling -> committed`,
with `available` rollback only when failure is provably clean and no mutable authority was
published or retired. Any uncertain cleanup or partial authority retirement becomes permanent
`failed`. A recovered economic history can be consumed by exactly one lifecycle.

Terminalization is permitted only when the runtime has no active lease, the internal ledger
frontier equals the acknowledged published portfolio frontier, and the internal risk state equals
the acknowledged published risk frontier; every admitted handoff has an acknowledged
ledger outcome; the final risk refresh is acknowledged; every reconciliation observation has an
acknowledged outcome; and no allowed adjustment, pending audit, unpublished mutation, or recovery
reservation remains. Terminal evidence binds the final published snapshot, risk refresh, open
reconciliation-reference aggregate, and ordered reconciliation frontier digests. A terminal record
with any missing or ahead history is invalid.

### Resource bounds

The extension preserves the accepted Phase 1 admission bound. Let:

- `M` be admitted market roots;
- `R` be admitted reconciliation roots, with `M + R <= 100,000`;
- `D = M + R + 1` be all dispatches including bounded end;
- `T` be trade-detail reconciliation roots that normalize to one execution-fact handoff each;
- `H` be all audited execution-fact handoffs, including those trade details and bounded-end expiry
  handoffs; ADR 0017 and ADR 0018 give `H <= M + T` because at most one Order chain is created per
  market dispatch, each Order emits at most one trade or expiry ingress, and each trade-detail root
  emits at most one fact ingress;
- `B` be adjustment authorization decisions, including denials; and
- `A` be allowed adjustment outcomes, with `A <= B`.

Only position/cash snapshot roots with one correctable discrepancy and order-detail roots with one
exact ancestry-resolution row can request adjustment authorization; trade-detail roots cannot. At
most one decision exists per non-trade root. Therefore `T + B <= R`.

The record families are: preparation/failing/terminal `3`, submission authorization at most `M`,
batch/completion/risk-refresh `3D`, fact outcomes plus ledger outcomes `2H`, reconciliation
observation outcomes `R`, adjustment authorization decisions `B`, and allowed adjustment outcomes
`A`. The exact conservative bound is:

```text
3 + M + 3D + 2H + R + B + A
= 4*M + 4*R + 2H + B + A + 6
<= 6*M + 4*R + 2*T + B + A + 6
<= 6*M + 4*R + 2*(T + B) + 6
<= 6*(M + R) + 6
<= 600,006 records
```

Every new payload is capped at 16,384 bytes and does not embed a complete snapshot; it binds strict
digests and bounded identity tuples. Using the existing 4,096-byte header and 48-byte framing, the
conservative all-large-frame upper bound is below 13 GiB. Composition requires 13 GiB free on the
verified result filesystem, enforces a hard 13 GiB journal cap and a 600,006-record cap, and rejects
admission before mutation when either cannot be met.

Recovery indexes retain only compact identities, digests, record offsets, and current snapshots;
canonical payloads are streamed from journal offsets. The reopen index remains capped at 256 MiB
measured resident memory, so an implementation must prove its compact entry layout at the
600,006-record seam. At that seam all record-ID/logical-key indexes are fixed-width offset and
digest structures; variable canonical payloads, balance arrays, and posting arrays are not retained
in the reopen index. The implementation evidence records measured peak RSS and rejects admission
before journal creation if its concrete layout cannot stay within 256 MiB. The packed index budget
is at most 384 bytes per admitted record plus at most 16 MiB of fixed tables and allocator slack:
`600,006 * 384 + 16 MiB < 256 MiB`. A Python object/dictionary per record does not satisfy this
contract. Scanning is linear in
verified bytes plus records. These limits cannot be
implemented by silently reducing the accepted 100,000-root bound.

## Atomic implementation sequence

Each focused-green slice creates and pushes a checkpoint SHA before the next slice starts:

1. core values, codecs, audit kinds/schemas, strict decoders, and boundary tests;
2. ledger handoff authority and authoritative result resolver;
3. coordinator ledger gate, completion schema, mandatory drain, and callback rebinding;
4. ledger/risk recovery replay and composition confinement;
5. reconciliation observations and comparison outcomes;
6. separately authorized reconciliation adjustments and recovery; and
7. full property, failure-injection, cross-process, resource-bound, and golden-trace evidence.

A slice is atomic only when its focused tests and the repository quality profile pass. A failed
slice is repaired or reverted before unrelated work begins. Final review still binds the exact
candidate SHA and does not inherit approval from an earlier checkpoint.

## Required evidence

- every handoff yields exactly one ordered ledger result;
- a real anomalous Fill applies exactly once and engages reconciliation halt;
- no-Fill, duplicate, conflict, invalid, unresolved, and corrected paths are exhaustive;
- audit failure after ledger mutation retries only the same result record;
- ledger failure drains remaining issued ingresses and exposes no unaudited snapshot;
- callback drift, reentry, concurrency, committed-then-raised, and torn-tail injection fail closed;
- completion cannot precede or omit any fact or ledger acknowledgement;
- restart at every mutation/audit boundary produces byte-identical history and final snapshots;
- injected future fact, ledger, risk, or reconciliation history is rejected;
- aggregate observations never overwrite the ledger and stale/incomparable evidence cannot clear
  a halt;
- adjustment authorization is one-use, exact-frontier-bound, balanced, and never publicly
  constructible;
- import-boundary and public-surface tests prove capability confinement;
- cross-process canonical vectors and reproducible golden traces match; and
- full quality, typing, coverage, build, isolated install, and exact-head CI profiles pass.

## Consequences

Positive:

- audited execution evidence reaches canonical economics exactly once;
- portfolio and risk consumers cannot observe unaudited or future state;
- crash recovery proves the same ledger, snapshot, halt, and correction frontier; and
- reconciliation remains evidence-driven and cannot silently replace the ledger.

Negative:

- dispatch completion and recovery schemas grow;
- the coordinator gains another mandatory durable stage and failure matrix; and
- a usable funded backtest still needs an explicit initial-funding contract.

## Rejected alternatives

### Apply raw Fills directly from the fact authority

Rejected because it bypasses the ADR 0020 audit gate and permits unaudited economic publication.

### Audit intent before applying and assume success

Rejected because a ledger conflict or arithmetic failure would make the journal claim economics
that never became authoritative.

### Apply the Fill and publish before auditing the result

Rejected because a crash can expose a snapshot that recovery cannot prove. The mutation may be
retained internally, but publication waits for its exact result acknowledgement.

### Make broker snapshots authoritative

Rejected because snapshots are observations, not transaction evidence, and would create silent
balancing entries or erase provenance.

### Expose an arbitrary posting or restore API

Rejected because it creates a second mutation authority and defeats Fill and adjustment
authorization identities.

### Implement the complete slice in one final checkpoint

Rejected because it couples independent failure domains and makes a late context or tool failure
strand a large dirty worktree. Focused-green atomic checkpoint SHAs are part of this iteration's
execution contract.
