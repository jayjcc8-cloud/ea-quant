# ADR 0006: Reproducible Run Manifest and Audit Lineage

Date: 2026-07-22

## Status

Proposed

## Context

Accepted ADR 0003 requires every mode to use mandatory run-scoped audit and result boundaries,
ADR 0004 defines canonical point-in-time market data, and ADR 0005 provides one frozen normalized
configuration snapshot. None of those decisions defines what identifies one execution attempt,
which inputs must be captured before a backtest starts, how those inputs are hashed, or who owns
the result directory.

Without that contract, two runs can share a label while using different revisions, code, defaults,
dependencies, or randomness. A manifest written or amended after execution could describe inputs
that were not actually used. File paths, modification times, random UUIDs, unordered mappings, or
final-only bars are not reproducibility evidence.

Issue #14 owns the minimum Phase 1 manifest and lineage boundary. Issue #15 still owns matching,
execution outcomes, Fill, ledger, and reconciliation semantics. This ADR does not define a full
experiment-tracking service or the future audit-event payload schema.

## Decision

### Separate attempt identity from reproducibility identity

V1 uses two immutable identifiers:

- `run_id` is one canonical lowercase RFC 4122 UUID version 4 with the RFC variant. It identifies
  one execution attempt, its audit history, and its result directory.
- `lineage_sha256` is the deterministic SHA-256 identity of the complete normalized
  reproducibility specification. Equivalent attempts have the same lineage digest.

The UUID is generated from an injected operating-system/provider capability only after every
required lineage input validates. It is excluded from the lineage preimage and MUST NOT consume,
seed, derive from, or change trading randomness, ordering, fills, or results. Invalid UUID versions,
variants, casing, aliases, URNs, braces, or nil values fail.

Two attempts may have equal lineage and different `run_id` values. They remain distinct because
their audit histories can fail or diverge operationally even when their declared reproducibility
inputs are identical. A typed `RunReference` carries both values. Audit and result boundaries
receive this value from the prepared run context rather than constructing identifiers themselves.

### Required v1 lineage specification

The lineage preimage is a closed, versioned value containing all of the following:

1. **Code provenance**
   - the full 40-character lowercase Git commit;
   - an exact `worktree_clean: true` assertion collected from the repository;
   - staged, unstaged, or non-ignored untracked changes, an unavailable commit, or a changing HEAD
     fail before preparation.
2. **Configuration provenance**
   - the fully materialized normalized ADR 0005 values: `schema_version`, `environment`, and
     `run.mode`;
   - canonicalization identifier `ea-settings-v1`;
   - a recomputable domain-separated configuration SHA-256;
   - YAML text, source order, `EA_CONFIG_PATH`, local paths, and raw secrets are excluded.
3. **Data provenance**
   - canonicalization identifier `ea-market-data-envelope-v1`;
   - SHA-256 and record count for the exact immutable event tuple supplied to replay;
   - source, source sequence, revision, `available_at`, interval boundaries, adjustment,
     structured venue/symbol, event kind, and every OHLCV binary64 bit pattern are identity-bearing.
4. **Replay window**
   - exact UTC `[start_inclusive, end_exclusive)` values with literal timezone `UTC`;
   - selection field `available_at` and initial state `empty` are fixed in v1;
   - no implicit pre-start warm-up is allowed.
5. **Effective parameters**
   - complete namespaced component selections and materialized tunables;
   - unique names sorted canonically;
   - v1 accepts only exact booleans, signed 64-bit integers, finite binary64 values, and safe NFC
     strings. Floats serialize as tagged 16-hex-digit big-endian IEEE-754 bits, preserving `-0.0`.
6. **Runtime and dependencies**
   - EA distribution version, Python implementation and exact version, ABI/cache tag, platform,
     a domain-separated `uv.lock` digest, and a sorted PEP 503-normalized installed-distribution
     inventory;
   - duplicate normalized package names, missing metadata, or host-local free text fail.
7. **Randomness**
   - an exact unsigned 64-bit master seed;
   - a named generator and versioned component-stream derivation scheme;
   - a fixed recorded `PYTHONHASHSEED` in the supported integer range;
   - global or unseeded randomness and order-dependent stream allocation are forbidden.

Paths, hostname, PID, username, branch, remote URL, wall-clock creation/completion time, result
status, output artifacts, and `run_id` do not enter the lineage preimage.

### Knowledge-time data fingerprint

The data boundary materializes the candidate iterable once, validates it under ADR 0004, orders it
by the canonical admission key, selects only records satisfying

```text
start_inclusive <= available_at < end_exclusive
```

and returns one immutable tuple together with its fingerprint. The historical feed must replay that
same tuple; reopening or re-reading mutable input after hashing is invalid.

The fingerprint starts a SHA-256 context with `b"ea.market-data.v1\0"`. For every selected record
in canonical admission order it appends an unsigned 64-bit big-endian byte length followed by the
record's canonical JSON bytes. It finally appends the unsigned 64-bit big-endian record count.

File path, mtime, original row order, and a dataset label are excluded. Hashing only final/latest
bars or OHLCV payloads is forbidden because it discards revision, source, and knowledge-time
lineage. A correction exactly at `end_exclusive` is excluded; a late correction before that bound
is included even when its bar interval is older.

### Canonical bytes and hash domains

Project canonical JSON v1 is UTF-8 without BOM, whitespace, or trailing newline; object keys are
lexicographically sorted and separators are exactly `,` and `:`. Field names are closed. UTC
timestamps use exactly six fractional digits followed by `Z`. Unsupported objects, arbitrary
`repr`, non-string keys, sets, paths, non-finite floats, unpaired surrogates, control characters,
and non-NFC strings fail instead of being normalized silently.

Hashes are domain-separated:

```text
config_sha256  = SHA256(b"ea.config.v1\0"   + canonical_normalized_settings)
uv_lock_sha256 = SHA256(b"ea.uv-lock.v1\0"  + exact_uv_lock_bytes)
lineage_sha256 = SHA256(b"ea.run-spec.v1\0" + canonical_lineage_spec)
```

The manifest envelope contains `manifest_schema_version: 1`, `run_id`, `lineage_sha256`, and the
complete lineage specification. The run and configuration digests are derived, never accepted as
unverified caller claims. A strict reader detects duplicate keys, requires exact nested fields,
recomputes every digest, and requires byte-for-byte canonical reserialization.

The plain SHA-256 of persisted manifest bytes is computed after the write and may be carried by the
audit header or artifact index. It cannot be embedded inside the file it hashes.

### Preparation, ownership, and immutability

The outer experiments boundary owns preparation in this order:

1. validate complete lineage, code cleanliness, immutable selected data, parameters, runtime, and
   randomness;
2. compute configuration, data, lock, and lineage digests;
3. generate one UUID4 `run_id` without touching the simulation RNG;
4. atomically reserve `resolved_result_root/<run_id>/`;
5. exclusively persist canonical `manifest.json`, flush it, and never open it for writing again;
6. return a prepared context containing the path, manifest-file digest, and `RunReference`;
7. only then bind/start mandatory audit, result adapters, feed, and runtime.

An existing file, directory, or symlink at the run path is a collision and fails without adoption,
reuse, deletion, or overwrite. A retry constructs a new attempt with a new UUID, even when lineage
is unchanged. The trusted result-root location is operational metadata and never enters lineage.
Policy and runtime components do not receive the filesystem root.

The prepared manifest is transitively immutable and never gains terminal status or artifacts.
Completion/failure evidence and a future sorted artifact index are separate write-once records.
A failed attempt keeps its owned directory and manifest as honest incomplete evidence. A prepared
manifest alone is not a claim that a backtest completed reproducibly.

### Audit and output lineage

Every audit record carries the prepared `RunReference`; every top-level output either embeds it or
is bound by a write-once artifact index carrying the same reference and manifest-file digest.
Missing or mismatched attempt or lineage identity fails before append/write. Runtime may import the
dependency-neutral `RunReference` but does not import manifest serializers, configuration,
filesystem stores, or data adapters.

V1 does not put a final audit-file digest inside the immutable manifest. That would create a cycle
between manifest completion and the ADR 0003 requirement that audit closes last. Issue #14 freezes
the shared reference and preparation contract; later runtime/result work implements concrete audit
and artifact payloads without changing these identities.

### Package boundaries

- `ea.core.run` owns dependency-neutral digest, attempt ID, replay-window, data-fingerprint, and
  shared run-reference values.
- `ea.data.fingerprint` owns exact canonical market-data selection and fingerprint semantics.
- `ea.experiments.manifest` owns frozen lineage/manifest values, canonical serialization, strict
  reading, and digest verification.
- `ea.experiments.provenance` owns outer Git/runtime collection.
- `ea.experiments.store` owns atomic local result-directory reservation and write-once manifest
  persistence.
- the future composition root is the only bridge from the final `Settings` snapshot and prepared
  data into experiments/runtime wiring.

Inner core and runtime modules never import configuration or outer experiments/data adapters. The
full manifest never enters strategy, portfolio, risk, execution, or matching policy.

## Consequences

Positive:

- Every attempt has one unambiguous audit/result identity while equivalent reruns remain directly
  comparable by deterministic lineage.
- Final bars, reordered files, paths, and timestamps cannot masquerade as point-in-time data
  lineage.
- Missing or mutable provenance fails before runtime effects.
- The manifest remains small, local, write-once, and usable without an experiment service.

Negative:

- Preparation must collect and validate more metadata before a backtest can start.
- Identical reruns consume separate result directories rather than acting as implicit cache hits.
- V1's parameter and UTC/data-window grammars are intentionally narrow.
- A prepared manifest cannot by itself prove successful completion; terminal evidence remains a
  separate runtime/result responsibility.

## Alternatives considered

### Use one content-addressed run ID

Rejected. It conflates equivalent inputs with a single execution attempt, merges separate audit
histories, and prevents ordinary same-root retries after a failed attempt. Deterministic comparison
is provided by `lineage_sha256` instead.

### Use only a random run ID

Rejected. It distinguishes attempts but cannot prove that code, data, configuration, parameters,
runtime, and seed are equivalent.

### Hash source files or final bars

Rejected. Paths/mtime are host-dependent, while final-only bars erase revision and knowledge-time
history and can conceal look-ahead.

### Rewrite one manifest at completion

Rejected. It permits prepared evidence to change after execution and creates circular dependencies
with audit and output hashes.

### Store arbitrary JSON parameters

Rejected. Unspecified floats, Unicode, mappings, containers, and object serialization make golden
lineage hashes unstable and allow hidden behavioral inputs.

## Non-goals

- Full experiment tracking, artifact registry, cloud storage, retention, aliases, or cache reuse.
- Performance-report, strategy, matcher, execution, Fill, ledger, or reconciliation schemas.
- Historical feed, runtime coordinator, audit adapter, or result adapter implementation.
- Warm-up/decision-window splitting, parallel floating-point policy, or distributed execution.
- Making fabricated upstream `available_at` values honest; source adapters remain responsible for
  ADR 0004 lineage.

## Validation

Issue #14 must prove this decision with:

- literal golden canonical bytes and digests for configuration, one data record, lineage, and a
  fixed-UUID manifest;
- same lineage plus two UUIDs yielding equal lineage hashes, distinct directories, and unchanged
  RNG/economic behavior;
- mutation tests proving every lineage-bearing field changes the correct digest while UUID, paths,
  and input insertion order do not;
- property tests for market-data permutation invariance and sensitivity to source, revision,
  sequence, availability, interval, identity, adjustment, and every float bit;
- exact `[start_inclusive, end_exclusive)` knowledge-time boundary tests;
- strict-reader duplicate/unknown/tamper/noncanonical-byte failures;
- transitive immutability and caller-owned-input mutation tests;
- temporary-Git dirty/staged/untracked and changing-HEAD failures;
- cross-process stability across Python hash seed, timezone, CWD, and result roots;
- atomic directory collision, symlink, traversal, concurrency, and no-overwrite tests;
- ordered-spy proof that manifest persistence precedes audit/feed/output start;
- audit/output reference mismatch failures;
- full repository quality, reproducible-wheel, clean-wheel, expert, and exact-head CI gates.
