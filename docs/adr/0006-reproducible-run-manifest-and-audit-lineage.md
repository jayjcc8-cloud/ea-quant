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

V1 is exclusively a bounded Phase 1 backtest contract. Its normalized configuration must contain
the literal `run.mode: backtest`; the builder, strict reader, and external evidence verifier reject
`paper` and `live`. Rejection occurs before UUID generation, result-directory reservation, or any
other runtime effect. A bounded paper replay or live-feed paper manifest requires a future schema
and ADR because it cannot claim the complete immutable input tuple required here.

1. **Code provenance**
   - the full 40-character lowercase Git commit;
   - an exact `worktree_clean: true` assertion collected from the repository;
   - staged, unstaged, or non-ignored untracked changes, an unavailable commit, or a changing HEAD
     fail before preparation;
   - because Phase 1 runs from an editable checkout, a future terminal reproducibility verifier must
     recheck the same HEAD and clean state. Any drift leaves the attempt incomplete. Execution from
     a separately identified immutable build artifact may replace this recheck in a future ADR.
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
   - `start_inclusive < end_exclusive`; exact `datetime` values are required;
   - no implicit pre-start warm-up is allowed, and an empty selected tuple cannot claim a first
     Phase 1 backtest.
5. **Effective parameters**
   - complete namespaced component selections and materialized tunables;
   - unique ASCII names matching `[a-z][a-z0-9_.-]{0,127}`, sorted by name;
   - v1 accepts only exact booleans, signed 64-bit integers, finite binary64 values, and 1–256
     Unicode-scalar NFC strings containing no `Cc` or `Cs` category code point. Floats serialize as
     tagged 16-hex-digit lowercase big-endian IEEE-754 bits, preserving `-0.0`;
   - values originate from typed non-secret component schemas. Credentials, tokens, signatures,
     private keys, and resolved `SecretRef` payloads never enter any manifest field or hash.
6. **Runtime and dependencies**
   - `ea_version` from the unique active-environment `ea-quant` distribution described below,
     exactly equal to both `importlib.metadata.version("ea-quant")` and `ea.__version__`;
   - `python_implementation` from `sys.implementation.name`, `python_version` from the decimal
     `sys.version_info[:3]`, `python_cache_tag` from `sys.implementation.cache_tag`, `sys_platform`
     from `sys.platform`, and `platform_tag` from `sysconfig.get_platform()`;
   - a domain-separated `uv.lock` digest and a sorted installed-distribution inventory whose names
     use PEP 503 normalization and whose exact metadata versions are 1–128-character NFC strings
     containing no `Cc` or `Cs` code point;
   - every other collected runtime token matches
     `[A-Za-z0-9][A-Za-z0-9._+-]{0,127}`;
   - fixed `numeric_policy: deterministic-ordered-float64-v1`, whose exact economic-computation
     semantics are defined below;
   - duplicate normalized package names, inconsistent EA versions, missing metadata, executable or
     install paths, `platform.platform()`, compiler text, hostname, and other host-local free text
     fail or are excluded.
7. **Randomness**
   - an exact unsigned 64-bit master seed;
   - fixed `generator: numpy-pcg64` and
     `stream_derivation: ea-sha256-component-label-v1` identifiers;
   - a unique sorted tuple of required stream labels, each matching
     `[a-z][a-z0-9_.-]{0,127}`;
   - global or unseeded randomness and order-dependent stream allocation are forbidden.

For label bytes `L`, a component PCG64 seed is the unsigned big-endian integer represented by the
first 16 bytes of:

```text
SHA256(
  b"ea.rng-stream.v1\0" +
  master_seed.to_bytes(8, "big") +
  len(L).to_bytes(2, "big") +
  L
)
```

The injected RNG capability owns this derivation; a caller label without the matching capability is
not provenance. `PYTHONHASHSEED` is deliberately excluded: v1 canonical ordering and economic
behavior must be invariant across separately started interpreters with different hash seeds.

Each stream label has exactly one component owner. Its narrow capability wraps exactly
`numpy.random.PCG64(component_seed).random_raw(size=None)`, exposes one unsigned 64-bit word per
call, and is never shared between components or invoked concurrently. `numpy.random.Generator`, its
distribution methods, vector-sized raw draws, another BitGenerator, and global NumPy randomness are
not V1 APIs. Any integer, uniform, or distribution transform must be a separately specified,
tracked, code-defined ordered scalar algorithm or a future versioned randomness policy. Draws occur
serially in deterministic runtime-dispatch order; their count and order may depend only on prior
deterministic state, never task scheduling or worker completion. A retry constructs every stream
again from the same master seed and label.

The normative vector for master seed `0` and label `matcher.primary` has component seed
`148415519428445905247115446051054473768`; its first four raw words are
`1256042036395257240`, `5315971738016275038`, `8151513107568622237`, and
`4281872282868094881`.

`deterministic-ordered-float64-v1` does not claim that the entire process has one thread. It covers
the complete economic causal cone: any computation capable of changing a feature, signal, target,
intent, risk decision, order, match/fill, position, cash, ledger/P&L, later-consumed runtime state,
or mandatory deterministic result. Within that cone, binary64 arithmetic, comparison, iteration,
tie-breaking, and aggregation use one code-defined total order and serial evaluation. Phase 1
allows exact Python `float` basic arithmetic in that order and unsigned words obtained through the
narrow PCG64 capability above. Except for that raw-word capability, BLAS, NumPy numeric kernels,
Polars, DuckDB, GPU, architecture-dispatched math, fused operations, and any parallel or unordered
floating-point reduction cannot influence economic behavior. A component whose operations are not
proven bit-stable under this policy cannot claim compatibility and needs a new policy whose backend
identity enters lineage.

This policy governs binary64 operations that occur in the causal cone; it does not authorize
binary64 storage or arithmetic where Accepted ADR 0004 requires `Decimal` or scaled integers.
Order, Fill, cash, position, ledger, and P&L values retain that exact-accounting requirement.

Parallel work is permitted only when each item is independently deterministic, completion order
cannot feed back into the economic causal cone, and outputs are canonically merged before any
consumer sees them. Sorting the output of a nondeterministic floating-point reduction does not make
that reduction valid. Optional telemetry that cannot feed back into decisions or mandatory results
is outside this numeric claim.

Paths, hostname, PID, username, branch, remote URL, wall-clock creation/completion time, Python hash
seed, result status, output artifacts, and `run_id` do not enter the lineage preimage.

### Supported editable runtime evidence

Phase 1 reproducible runs use the repository's locked editable virtual environment, but metadata
discovery must not count its source-tree `egg-info` as a second installed dependency or allow an
unrelated source tree to shadow the reviewed code.

The runtime collector therefore enforces all of these rules before constructing evidence:

1. The interpreter has `sys.flags.isolated == 1`, `sys.flags.safe_path == 1`,
   `sys.flags.no_user_site == 1`, `sys.flags.no_site == 0`,
   `sys.flags.dont_write_bytecode == 1`, `sys.dont_write_bytecode is True`,
   `sys.flags.optimize == 0`, `sys.pycache_prefix is None`, and
   `sys.prefix != sys.base_prefix`. A tracked standard-library-only outer launcher started with the
   equivalent of `python -I -B` performs the source/import preflight below before importing `ea` and
   then enters preparation. `PYTHONPATH`, the current directory, the user site, optimized mode, an
   external bytecode-cache prefix, and site-disabled startup are unsupported.
2. The collector, not a caller, obtains `purelib` and `platlib` from the active interpreter's
   `sysconfig.get_paths()`. Both values must be absolute existing real directories. It resolves
   them strictly, deduplicates physical aliases before discovery, and fails on an empty set.
3. It calls `importlib.metadata.distributions(path=active_roots)` exactly once over that deduplicated
   root list. The resolved `distribution.locate_file("")` for each result must be one of those
   directories. Names are PEP 503 normalized, sorted, and unique; duplicate normalized names fail
   even when versions are equal.
4. Ambient `importlib.metadata.distributions()` may contain no distribution rooted outside the
   active roots except zero or one source-tree `ea-quant` projection rooted at the reviewed
   repository's exact `src/` directory. That projection, when present, must have the same normalized
   name and exact version as the unique active-root `ea-quant`; every other editable, path,
   user-site, or ambient distribution fails.
5. The active-root inventory contains exactly one `ea-quant`. Its UTF-8 `direct_url.json` is parsed
   with duplicate-key rejection and is a closed object with exactly `url` and `dir_info`;
   `dir_info` is exactly `{"editable":true}`. `url` has the `file` scheme, empty authority, and no
   credentials, query, or fragment, and its decoded absolute path resolves to the same Git
   repository. The imported regular `ea` package has exactly the resolved
   `<repository>/src/ea` search location and `<repository>/src/ea/__init__.py` origin.
6. Every non-empty `sys.path` entry is absolute and unique after non-strict resolution. It must be
   either below the resolved `sys.base_prefix` without traversing a `site-packages` or
   `dist-packages` directory, exactly one active metadata root, or exactly the reviewed
   repository's resolved `src/` directory. Any other current-directory, user-site, editable,
   injected, relative, or empty import root fails.
7. The collector scans that entire repository `src/` import root, not only `src/ea`. `ea` is the
   only importable top-level package or module allowed in V1. Other than bytecode accepted by rule
   8, every regular package-data file and every source or extension-module candidate below
   `src/ea` must be tracked at the reviewed HEAD; every already-loaded `ea` module must originate at
   one of those tracked files. Source-tree `ea_quant.egg-info` is permitted only as the already
   validated non-importable metadata projection. Every other top-level importable candidate fails
   even if ignored or tracked. The scan uses `lstat`, never follows links, and rejects every symlink
   or other non-regular file/directory below `src/`; every resolved allowed import or package entry
   must remain below the real `src/ea` tree, except the non-importable metadata projection.
8. A file matching an `importlib.machinery.BYTECODE_SUFFIXES` suffix is allowed only at the exact
   unoptimized `importlib.util.cache_from_source()` path for a corresponding tracked `src/ea/*.py`
   source and the active `sys.implementation.cache_tag`. The preflight checks the exact interpreter
   magic, loads one code object with no trailing bytes, recompiles the tracked source bytes with
   their resolved filename using `mode="exec"`, `flags=0`, `dont_inherit=True`, and `optimize=0`,
   then requires
   `marshal.dumps(loaded_code, 2) == marshal.dumps(recompiled_code, 2)`. Marshal format version 2 is
   fixed here because versions 3 and later preserve reference/interning identity that can differ
   between semantically identical compilations; version 2 still covers the complete nested code
   structure and observable filename, qualname, line-table, and exception-table fields. Comparing
   `CodeType` values with `==` is forbidden because it omits observable fields; comparing the
   original cache payload or using the default marshal version is also forbidden. Any mismatch,
   optimized/legacy/top-level/sourceless bytecode, cache for an untracked source, or parse failure is
   rejected. Every ignored source or extension-module candidate likewise fails, and tracked
   extension/source candidates are allowed only inside the bound `ea` package.
9. The tracked outer launcher first requires that `sys.modules` contain neither `ea` nor any
   `ea.*` name, then performs the repository, source, cache, symlink, and shadow parts of rules 5–8
   before any `ea` import. Thus an unverified source cache or shadow candidate cannot execute first.
   After the authorized import, the runtime collector repeats rules 1–8 including loaded-module
   origin checks, but does not repeat the launcher-only empty-`sys.modules` predicate. Bytecode
   writes remain disabled. Neither layer deletes, rewrites, or adopts a source-tree artifact.

These locations are operational verification inputs and never enter the manifest or lineage.
Repository evidence binds the resolved repository to the same clean commit before and after runtime
collection so the check cannot be redirected mid-collection. The inventory and lock digest prove
the declared locked environment identity; V1 is not an adversarial attestation that trusted
site-packages files were not modified in place.

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

Every record is exactly this closed object; `event_time` is omitted because ADR 0004 derives it from
`interval_end`. The example values are normative for names, nesting, timestamp form, and binary64
encoding:

```json
{
  "adjustment": "raw",
  "available_at": "2026-01-02T09:31:00.000000Z",
  "close_bits": "4059200000000000",
  "high_bits": "4059400000000000",
  "interval_end": "2026-01-02T09:31:00.000000Z",
  "interval_start": "2026-01-02T09:30:00.000000Z",
  "kind": "bar",
  "low_bits": "4058c00000000000",
  "open_bits": "4059000000000000",
  "revision": 0,
  "source": "primary.raw",
  "source_sequence": 0,
  "symbol": "AAPL",
  "venue": "XNAS",
  "volume_bits": "4024000000000000"
}
```

Each `*_bits` value is exactly 16 lowercase hexadecimal digits from `struct.pack(">d", value)`.
`record_count` is an exact integer in `1..2**64-1`; booleans are never integers.

### Normative v1 manifest schema

The complete lineage specification has exactly this closed shape. Ellipses below denote values of
the stated grammar, not optional fields. All arrays shown as sorted are stored in that order.

```json
{
  "code": {
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "worktree_clean": true
  },
  "configuration": {
    "canonicalization": "ea-settings-v1",
    "normalized": {
      "environment": "development",
      "run": {"mode": "backtest"},
      "schema_version": 1
    },
    "sha256": "<64 lowercase hexadecimal digits>"
  },
  "data": {
    "canonicalization": "ea-market-data-envelope-v1",
    "record_count": 1,
    "sha256": "<64 lowercase hexadecimal digits>"
  },
  "lineage_schema_version": 1,
  "parameters": [
    {"name": "matcher.enabled", "type": "boolean", "value": true},
    {"name": "strategy.label", "type": "string", "value": "baseline"},
    {"name": "strategy.lookback", "type": "integer", "value": 20},
    {"name": "strategy.threshold", "type": "float64", "value": "3fb999999999999a"}
  ],
  "randomness": {
    "generator": "numpy-pcg64",
    "master_seed": 0,
    "stream_derivation": "ea-sha256-component-label-v1",
    "stream_labels": ["matcher.primary", "strategy.primary"]
  },
  "replay_window": {
    "end_exclusive": "2026-02-01T00:00:00.000000Z",
    "initial_state": "empty",
    "selection_field": "available_at",
    "start_inclusive": "2026-01-01T00:00:00.000000Z",
    "timezone": "UTC"
  },
  "runtime": {
    "distributions": [
      {"name": "ea-quant", "version": "0.1.1"}
    ],
    "ea_version": "0.1.1",
    "numeric_policy": "deterministic-ordered-float64-v1",
    "platform_tag": "macosx-11.0-arm64",
    "python_cache_tag": "cpython-312",
    "python_implementation": "cpython",
    "python_version": "3.12.13",
    "sys_platform": "darwin",
    "uv_lock_sha256": "<64 lowercase hexadecimal digits>"
  }
}
```

Parameter entries are an array rather than a JSON object so type tags are explicit; entries are
sorted by their unique `name`. Their exact `type` vocabulary is `boolean`, `float64`, `integer`, and
`string`, with the matching `value` representation shown above. Arbitrary nested values, null,
lists, mappings, paths, and user-defined objects are not part of v1.

The normalized configuration's `run` object is likewise closed to the literal
`{"mode":"backtest"}`. Another otherwise valid ADR 0005 mode is not a v1 manifest value.

The persisted manifest is the closed object

```text
{
  "lineage_sha256": Digest,
  "manifest_schema_version": 1,
  "run_id": RunId,
  "spec": LineageSpec
}
```

where `LineageSpec` is the complete object above, `Digest` is exactly 64 lowercase hexadecimal
digits, and `RunId` is the canonical 36-character lowercase hyphenated UUID4 form. These grammar
names are explanatory notation and are never serialized as strings or placeholder keys.

### Canonical bytes and hash domains

Project canonical JSON v1 is UTF-8 without BOM, insignificant whitespace, or trailing newline.
Every schema key is ASCII and keys are sorted by ascending Unicode code point, which is identical
to UTF-8 byte order for these keys. Arrays retain their schema-defined order; parameters,
distributions, and stream labels are validated and sorted before encoding. Separators are exactly
`,` and `:`.

Strings are emitted as literal UTF-8 (`ensure_ascii=false`): quotation mark and reverse solidus use
their required two-character JSON escapes, solidus is not escaped, and no `\u` escape is emitted.
All accepted value strings are NFC and contain no Unicode `Cc` or `Cs` code point, so other control
escapes are unnecessary. Integers use the shortest base-10 form with no leading plus, leading zero,
or negative zero. The only JSON booleans are lowercase `true` and `false`; null and JSON numbers
with a fraction/exponent are absent from the schema. UTC timestamps use exactly six fractional
digits followed by `Z`.

Unsupported objects, arbitrary `repr`, non-string keys, sets, paths, non-finite floats, unpaired
surrogates, controls, non-NFC strings, or a value outside its field grammar fail instead of being
coerced or normalized silently.

Hashes are domain-separated:

```text
config_sha256  = SHA256(b"ea.config.v1\0"   + canonical_normalized_settings)
uv_lock_sha256 = SHA256(b"ea.uv-lock.v1\0"  + exact_uv_lock_bytes)
lineage_sha256 = SHA256(b"ea.run-spec.v1\0" + canonical_lineage_spec)
```

The manifest builder accepts typed evidence objects from the Git/runtime and data collectors, not
caller-supplied digest strings. It derives configuration and lineage digests. Verification is split
because the small manifest intentionally does not embed the selected event tuple or `uv.lock`:

- `read_manifest(bytes)` detects duplicate keys, requires every exact nested field, validates UUID
  and digest forms, recomputes the embedded configuration and lineage digests, and requires
  byte-for-byte canonical reserialization;
- `verify_manifest_evidence(manifest, data, uv_lock_bytes, repository, runtime)` recomputes data and
  lock digests, re-establishes code/cleanliness and runtime inventory, and compares all external
  evidence without mutating the manifest.

Reading structurally valid bytes never by itself grants a verified or completed reproducibility
claim.

The plain SHA-256 of persisted manifest bytes is computed from the read-back bytes after durable
publication and is required in the first acknowledged audit record and future artifact index. It
cannot be embedded inside the file it hashes.

### Preparation, ownership, and immutability

The outer experiments boundary owns preparation in this order:

1. validate complete lineage, code cleanliness, immutable selected data, parameters, runtime, and
   randomness;
2. compute configuration, data, lock, and lineage digests;
3. generate one UUID4 `run_id` without touching the simulation RNG;
4. atomically reserve `resolved_result_root/<run_id>/` and its fixed `audit/` and `outputs/`
   children;
5. exclusively create canonical `manifest.json`, durably publish and verify it, and never open it
   for writing again;
6. return a prepared context containing narrow child capabilities, the manifest-file digest, and
   `RunReference`;
7. only then bind/start mandatory audit, result adapters, feed, and runtime.

The trusted result root must already exist, its final directory entry must be a real directory and
not a symlink, and the store operates on its resolved path. The target is exactly one direct child
named by the already validated UUID; its resolved parent must equal the resolved root. UUID grammar
therefore makes absolute, separator, backslash, `..`, NUL, and traversal names unrepresentable.

Reservation uses one atomic directory creation with mode `0700`. An existing file, directory, or
symlink is a collision and fails without adoption, reuse, deletion, or overwrite. Only the store
creates root-level files and the fixed `audit/` and `outputs/` children. Composition may grant the
monitoring adapter only the `audit/` capability and the result adapter only the `outputs/`
capability; neither receives the run root, creates siblings, or traverses upward.

Within the unshared reserved directory, publication uses direct exclusive no-follow creation of
`manifest.json` with mode `0600`. The store writes the complete canonical bytes, flushes Python and
OS buffers, `fsync`s the file, closes it, reopens it read-only/no-follow, verifies byte equality and
computes the plain manifest digest from those read-back bytes, then `fsync`s the run directory and
result root directory. No `PreparedRun` or run-bound adapter capability escapes before every step
succeeds. Direct creation is atomic at the API boundary because the reserved directory is not
published to any consumer until completion.

Any failure after reservation retains a poisoned incomplete directory and any partial evidence;
the store never cleans, adopts, or retries it, and returns no prepared context. A retry constructs a
new attempt with a new UUID even when lineage is unchanged. The trusted result-root location is
operational metadata and never enters lineage. Policy and runtime components receive neither it nor
the manifest path.

The prepared manifest is transitively immutable and never gains terminal status or artifacts.
Completion/failure evidence and a future sorted artifact index are separate write-once records.
A failed attempt keeps its owned directory and manifest as honest incomplete evidence. A prepared
manifest alone is not a claim that a backtest completed reproducibly. Issue #14 exposes no
`completed_reproducibly` boolean or terminal claim API.

A future terminal verifier must bind the same `RunReference`, manifest digest, exact prepared data
tuple, mandatory audit/result evidence, and rechecked Git HEAD/clean state, lock digest, and runtime
inventory. Drift or missing evidence leaves the attempt failed/incomplete without rewriting the
manifest.

### Audit and output lineage

The first mandatory audit append must carry and acknowledge the prepared `RunReference` and
manifest-file digest before feed/runtime admission. Every later audit record carries the same
reference; every top-level output either embeds it or is bound by a write-once artifact index
carrying the same reference and manifest-file digest.
Missing or mismatched attempt or lineage identity fails before append/write. Runtime may import the
dependency-neutral `RunReference` but does not import manifest serializers, configuration,
filesystem stores, or data adapters.

Issue #14 proves this rule with typed reference binders and fake audit/result boundaries. Concrete
audit payloads and adapters remain deferred and cannot weaken the binder contract.

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
- Warm-up/decision-window splitting, live-feed paper runs, or distributed economic execution.
  Reproducible v1 runs use the fixed ordered-float64 policy; a broader numeric/backend policy
  requires a superseding decision.
- Adversarial executable or installed-file attestation. V1 binds a clean editable source checkout,
  isolated import topology, locked dependency identity, and exact runtime inventory.
- Making fabricated upstream `available_at` values honest; source adapters remain responsible for
  ADR 0004 lineage.
- Generic manifest depth/size limits beyond the bounded v1 parameter, distribution, and data-count
  fields; those limits may be tightened without accepting arbitrary schema values.

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
- temporary-Git dirty/staged/untracked and changing-HEAD failures at preparation and terminal
  evidence verification;
- isolated editable-runtime tests for deduplicated `purelib`/`platlib`, unique active
  distributions, direct-URL/current-repository binding, imported-EA origin, and rejection of
  ambient/path/user-site metadata or import roots;
- outer-launcher tests proving source/cache/symlink preflight occurs before any `ea` import and
  rejecting optimized, site-disabled, writable-bytecode, or external-pycache-prefix startup;
- pre-effect `backtest` acceptance plus `paper` and `live` builder/reader/verifier rejection;
- cross-process lineage stability across different Python hash seeds, timezone, CWD, and result
  roots;
- fixed RNG seed and initial `random_raw(size=None)` word vectors, duplicate-label/owner failures,
  rejection of broader NumPy draw APIs, serial draw-order invariance, and proof UUID generation does
  not consume or alter the simulation RNG;
- numeric-policy conformance tests proving canonical economic iteration/aggregation and rejection
  of unordered, parallel, backend-dispatched, or schedule-dependent economic computation;
- ignored/top-level source, extension, tampered/source-equivalent cache, sourceless-bytecode,
  file-symlink, and directory-symlink shadow tests across the complete repository `src/` import
  root, including observable nested-code tampering, marshal reference/interning noise, and proof that
  preflight occurs before any `ea` import;
- atomic directory collision, root/target symlink, traversal, concurrency, durability-failure,
  poisoned-directory retention, and no-overwrite tests;
- ordered-spy proof that manifest persistence precedes audit/feed/output start;
- audit/output reference mismatch failures;
- full repository quality, reproducible-wheel, clean-wheel, expert, and exact-head CI gates.
