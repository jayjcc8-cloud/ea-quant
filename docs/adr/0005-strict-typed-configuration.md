# ADR 0005: Strict Typed Configuration

Date: 2026-07-18

## Status

Accepted

## Context

ADR 0003 requires the composition root to receive one validated configuration snapshot and select
exactly one mode profile. Inner components must not read environment variables, files, command-line
arguments, or a mode name. The current Phase 0 placeholder does not satisfy that boundary:

- `env` is an unconstrained string;
- independent paper/live booleans can represent contradictory modes;
- unknown settings are ignored;
- `.env.example` declares `EA_CONFIG_PATH`, but the application never loads that YAML file;
- the example YAML contains untyped future strategy, risk, and data fields;
- raw broker credential names appear next to application configuration; and
- configuration source precedence and the immutable handoff boundary are unspecified.

Those gaps could make identical commands behave differently because of ambient process state,
silently accept misspelled safety settings, or let a future run manifest hash an unstable and
partially validated representation.

Issue #13 owns the minimum configuration contract needed before Phase 1 runtime implementation.
Issue #14 owns run identity, manifest serialization, and configuration hashing. Issue #15 owns
execution and reconciliation semantics.

## Decision

### One immutable snapshot

The outer configuration module owns a transitively frozen, versioned `Settings` model. Its initial
fields are:

- `schema_version`: exactly integer `1`;
- `environment`: one of `development`, `staging`, or `production`;
- `run.mode`: one of `backtest`, `paper`, or `live`.

The snapshot is the only configuration value passed to the composition root. Inner components
receive narrow capabilities or values derived by the composition root and cannot load or retain
configuration sources.

`run.mode` is the sole mode selector. The legacy `paper_trading_enabled` and
`live_trading_enabled` booleans are removed without aliases. `live` remains part of the stable
vocabulary, but current configuration loading rejects it because ADR 0003 declares the live
profile unavailable. Enabling live construction requires a future superseding decision with an
independent authorization contract; a boolean in YAML, environment, or CLI is insufficient.

The validated model is immutable. Issue #14 may later define canonical serialization and hashing,
but it must consume this validated snapshot rather than raw source dictionaries.

Every source is structurally validated before merging, but live availability is evaluated exactly
once on the final merged snapshot. A valid lower-priority `live` value can therefore be safely
overridden by a higher-priority `backtest`; invalid names, shapes, schema versions, or enum values
in any source still fail.

### Explicit source selection and precedence

Configuration is loaded once at the CLI/composition boundary in this order, from lowest to highest
priority:

1. typed model defaults;
2. one explicitly selected YAML file;
3. the current process environment;
4. explicit CLI value overrides.

A higher-priority source replacing a lower-priority value is normal precedence, not a conflict.
Sources are not deep-merged after validation, and no component may reload them during a run.

The YAML path is selected by the CLI `--config` option when present, otherwise by the exact process
environment name `EA_CONFIG_PATH`. A relative path is resolved from the invocation working
directory. The selector is loader metadata and is not a field in the normalized snapshot. A
supplied empty, missing, unreadable, non-file, malformed, empty-invalid, or non-mapping path fails
before a snapshot is returned.

YAML is a single versioned document with a mapping root. It must declare integer
`schema_version: 1`. Duplicate mapping keys, unknown keys at any depth, custom object tags, and
executable constructors are rejected before a snapshot is produced.

The application does not implicitly read `.env`. `.env.example` documents supported process
environment names that a user or process manager may explicitly export; hidden file discovery is
not a configuration source.

### Strict names and diagnostics

Every source is closed by default:

- YAML allows only fields defined by the typed root;
- supported application environment names are exactly `EA_ENVIRONMENT`, `EA_RUN_MODE`, and
  `EA_CONFIG_PATH`;
- any other case-insensitive `EA_*` name fails, including the removed Phase 0 names;
- duplicate environment names that normalize to the same supported name fail;
- CLI rejects unknown options through its command schema; and
- invalid values and source errors produce concise configuration diagnostics without raw values,
  secret material, or a Python traceback.

The configuration API does not infer aliases such as `env`, root-level `mode`, or live/paper booleans.
Changing the public names is an explicit compatibility decision, not silent coercion.

### CLI boundary

Shared CLI options collect only source selection and typed overrides. Commands call one loader and
receive a loaded configuration containing:

- the immutable `Settings` snapshot; and
- the resolved YAML path, if one was selected, for human diagnostics only.

The CLI may report the environment, run mode, and selected path. It must not print raw source
mappings or future resolved credentials.

### Secret-reference boundary

Raw credentials, tokens, signatures, private keys, and vendor authentication objects are not
application settings and must not enter YAML, supported `EA_*` process environment, the normalized
snapshot, CLI diagnostics, or audit data. Issue #13 does not choose or implement a secret store.

`SecretRef` is a frozen, validated opaque identifier value independent of a broker schema. A future
adapter may receive that value plus a resolver at the outer adapter boundary. Resolution occurs
only when constructing that adapter, and the resolved payload never returns to the snapshot or
domain/runtime messages. Because no current adapter needs credentials, the minimum Issue #13
settings root contains no secret-reference field and rejects raw broker credential names as
unknown input.

## Consequences

Positive:

- misspelled, removed, or future-looking settings fail instead of being ignored;
- one mode value replaces contradictory booleans;
- live behavior remains unavailable and fail-closed;
- CLI, YAML, and process-environment behavior is deterministic and testable;
- the composition root receives an immutable value suitable for the future #14 lineage boundary;
- raw secrets stay outside configuration and diagnostics.

Negative:

- the Phase 0 `EA_ENV`, `EA_PAPER_TRADING_ENABLED`, and `EA_LIVE_TRADING_ENABLED` names are breaking
  removals before Phase 1;
- the existing example YAML must be reduced to fields that have an accepted contract;
- users who want dotenv-style local development must explicitly export/process that file rather
  than relying on hidden application discovery;
- future configuration sections require explicit typed schema additions rather than arbitrary
  dictionaries.

## Alternatives considered

### Keep independent paper/live booleans

Rejected because combinations can be contradictory and inner code may branch on flags rather than
one composition profile.

### Ignore unknown fields for forward compatibility

Rejected because misspelled safety settings would appear accepted while having no effect.

### Put raw secrets in environment-backed settings

Rejected because settings snapshots, diagnostics, manifests, and audit records could accidentally
retain or print them.

### Define strategy, risk, data, manifest, and execution configuration now

Rejected because those domains do not yet have accepted Phase 1 contracts. Arbitrary dictionaries
would move ambiguity rather than remove it.

## Validation

Issue #13 must include tests proving:

- default, YAML, process-environment, and CLI precedence;
- CLI-over-environment YAML path selection and relative-path behavior;
- required schema version, single-document safe YAML, duplicate-key rejection, and recursive
  closed-schema validation;
- missing, malformed, custom-tagged, empty-invalid, and non-mapping YAML failure;
- rejection of unknown YAML fields, unknown or duplicate `EA_*` names, removed aliases, and invalid
  enum values;
- proof that `.env` is not read implicitly;
- immutable snapshots and isolated repeated loads;
- final-snapshot `live` rejection with no constructible live profile, including safe
  higher-precedence override behavior;
- independent frozen `SecretRef` validation without any raw credential field;
- CLI diagnostics contain no traceback or raw unknown values;
- examples and `ea doctor` match the public contract; and
- the full repository quality and reproducible-build gates remain green.
