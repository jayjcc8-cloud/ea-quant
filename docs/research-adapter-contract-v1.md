# Research Adapter Contract V1

RF-01.1 deliverable. This is the frozen one-way contract from an offline mature research
framework into EA, as required by RF-00 §8 row RF-01.1 ("a written contract for seams B and C: the
parameter-vector schema, the `strategy.py` template, and the emit rules").

This document is **normative but inert**. It defines interfaces only. It adds no production code,
no dependency, no runtime change, no Package V4 implementation, and no architecture refactor. Every
normative statement below is derived from an existing, merged EA contract — the citations are the
authority, not this document.

Prerequisite: RF-00 (`docs/reviews/RESEARCH_FRAMEWORK_INTEGRATION_READINESS_V1.md`), whose seams A–E
this contract refines into implementable interfaces.

Normative keywords: **MUST**, **MUST NOT**, **MAY**.

## 1. INPUT CONTRACT

Seam A, read-only. A `ResearchInput` is the only thing research reads from EA, and it is a
description of an already-registered capture — never a new data path.

| Field | Type / source | Rule |
|---|---|---|
| `capture_path` | absolute path to a registered capture | MUST resolve to a regular file; MUST NOT be a symlink (`read_regular`, `src/ea/strategy/package.py:220`) |
| `profile` | `"ea-phase1-ohlcv-csv-v1"` | MUST equal `PHASE1_HISTORICAL_MARKET_PROFILE` (`src/ea/runtime/historical.py:51`) |
| `data_sha256` | hex SHA-256 | The immutable source fingerprint. Computed by EA, never by research. |
| `record_count` | int | With `data_sha256`, the whole of EA's `_FingerprintInput` (`src/ea/product/scenario.py:99`) |
| `instrument` | `venue`, `symbol`, `specification_id`, `specification_set_id`, `settlement_currency`, `price_quantum`, `quantity_quantum`, `currency_quantum`, `contract_multiplier` | Exactly **one** instrument (ADR 0037); this is the full `_InstrumentInput` (`src/ea/product/scenario.py:111`) |
| `window` | `start_utc`, `end_utc` | Exactly `_DataInput.start_utc` / `.end_utc` (`src/ea/product/scenario.py:104`); the replay window is a property of the capture, not of the research run |

Rules:

1. Research MUST treat `ResearchInput` as read-only. There is **no write-back**: research MUST NOT
   write to EA capture storage, the workspace, the runtime root, or any EA durable state.
2. Identity is the fingerprint. Two `ResearchInput`s with different `data_sha256` or
   `record_count` are different inputs; a re-derived capture with the same path is a **different**
   input and MUST NOT be treated as the same evidence.
3. Research MUST NOT invent instrument fields, quanta, or a settlement currency. If the capture's
   instrument does not match a scenario's `_InstrumentInput`, the emitted artifact is invalid and
   EA will reject it — research must not "repair" it.
4. `ResearchInput` carries **no** economic authority: no fills, cash, fees, position, P&L, or
   admission state. Those remain EA's alone (RF-00 §3 KEEP).
5. The research-side *materialization* of this input (a CSV or panel form) is RF-01.2's business.
   That form is derived, disposable, and never a source of truth — EA's capture and fingerprint
   remain canonical (RF-00 §10, Qlib `.bin` lock-in risk).

## 2. OUTPUT CONTRACT

Seams B and C. The pipeline is exactly:

```
ResearchCandidateSpec
      ↓
strategy.py + manifest.json
      ↓
ordinary .eastrategy
```

### 2.1 ResearchCandidateSpec

The emitter's input. It is a research-side record; it is **not** a Candidate and confers nothing.

| Element | Content | Constraint |
|---|---|---|
| strategy / rule identity | `strategy_id`, `strategy_version`, `display_name` | MUST match `[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}`; `display_name` 1..128 chars (`src/ea/strategy/package.py:155`) |
| canonical numeric parameters | ordered `ParameterV1` records | ≤ **32** (`src/ea/strategy/package.py:164`); see §3 |
| research provenance | framework name/version, study id, objective name | Descriptive metadata only. MUST NOT be presented as evidence. |
| framework metadata | feature set, search space, seed | Descriptive only. MUST NOT cross into the artifact. |
| source data identity | `data_sha256`, `record_count`, `window`, `profile` | MUST equal the `ResearchInput` actually used |
| **execution result authority** | **none** | A `ResearchCandidateSpec` MUST NOT carry an execution result, economic outcome, or P&L claim of its own |

`ResearchCandidateSpec` MUST be immutable once emitted. Re-running research produces a new spec with
a new provenance record; it MUST NOT mutate a spec that already produced an artifact.

### 2.2 Manifest

The manifest MUST be byte-exact canonical JSON —
`json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`
encoded ASCII (`canonical_json`, `src/ea/strategy/package.py:28`). EA rejects any other byte
sequence (`package.py:133`).

Top-level keys MUST be exactly `{"schema_version", "package_id", "strategy"}`. `strategy` keys MUST
be exactly `{"id", "version", "display_name", "parameters", "action_contract",
"position_lifecycle"}` (`package.py:153`).

```json
{"package_id":"<package id>","schema_version":3,"strategy":{"action_contract":"V2","display_name":"<1..128>","id":"<strategy id>","parameters":[{"default":1,"name":"slow_window","required":true,"static_maximum":null,"static_minimum":2,"type":"integer"}],"position_lifecycle":"bounded-long-round-trips-v1","version":1}}
```

Rules:

1. `action_contract` MUST be `"V2"` (`package.py:182`). There is no other action contract.
2. `schema_version` MUST be `3` (→ `bounded-long-round-trips-v1`) or `2`
   (→ `single-long-round-trip-v1`). The pairing is fixed and MUST NOT be mixed
   (`package.py:181`). `schema_version` 1 is legacy and MUST NOT be emitted.
3. Every `parameters[]` entry MUST be exactly a `ParameterV1`
   (`src/ea/strategy/registry.py:20`): keys `name`, `type`, `required`, `default`,
   `static_minimum`, `static_maximum`, all six present. `name` MUST match `[a-z][a-z0-9_]*`;
   `type` MUST be `"integer"` or `"decimal"`; `required` MUST be a JSON boolean; `default` MUST be
   an exact `int` for `integer` and an `ea-decimal-v1` string for `decimal`, with
   `static_minimum` / `static_maximum` the same type or `null`. A float anywhere is a violation.
4. The manifest MUST be produced by EA's own packer, never hand-written (§2.4).

### 2.3 `strategy.py`

MUST be valid UTF-8 (`package.py:180`) and MUST define both callables, which EA `exec`s under
`_module()` (`package.py:91`). Module template:

```python
# emitted artifact — EA execs this source; it is trusted local code, not sandboxed
from ea.strategy.sdk_v2 import PositionState


def validate_parameters(p, context):
    """Raise ValueError for an invalid parameter vector. context is
    StrategyValidationContextV1(history_bar_count, last_entry_index, quantity_quantum)."""


class Logic:
    def __init__(self, p): ...

    def on_bar(self, bar, position):
        """bar: StrategyBarV1; position: PositionViewV2(state, quantity).
        Return a CLOSED action object. All feature state is owned here."""


def create_logic(p):
    return Logic(p)
```

Rules:

1. `on_bar` MUST return exactly `{"action": "HOLD"}` or `{"action": "EXIT_LONG"}`, or exactly
   `{"action": "ENTER_LONG", "quantity": "<canonical decimal>"}`. Any other key set is rejected as
   "unsupported or ambiguous action" (`decode_action`, `src/ea/strategy/sdk_v2.py:33`).
2. `ENTER_LONG.quantity` MUST be a `str` — a canonical `ea-decimal-v1` value — and strictly
   positive. A Python `float` is rejected (`sdk_v2.py:40`).
3. The module MUST NOT import EA modules holding economic authority, MUST NOT read the filesystem
   or network, and MUST NOT carry precomputed feature values. Its only inputs are the 7
   `StrategyBarV1` fields and the parameter dict. Note that EA does **not** sandbox this code —
   local packages are trusted ("trusted executable local Python, not a sandbox", ADR 0036:25).
   Purity is an **emitter obligation**, not an enforced property; the RAD-03 sandboxed host is the
   future enforcement point, not this contract.
4. The module MUST be self-contained: no research-framework import of any kind (§4).

### 2.4 Emit rules (container)

1. The artifact MUST be produced by EA's packer — `ea strategy pack`, i.e. `pack_strategy`
   (`src/ea/strategy/package.py:259`). The adapter MUST NOT construct the ZIP itself.
2. The staging directory MUST contain **exactly** `manifest.json` and `strategy.py` — no extras, no
   subdirectories (`package.py:268`).
3. The output path MUST end `.eastrategy` and MUST NOT already exist (`open("xb")`,
   `package.py:270`). Overwriting an artifact is forbidden; emit a new path.
4. Each member MUST be ≤ 1 MiB; the artifact ≤ 2 MiB + 4096 (`package.py:19`).
5. An emitted artifact is valid only if `inspect_package` accepts it. Emitting the package **is**
   the validation step; the adapter MUST NOT report success on a packer failure.

## 3. NUMERIC BOUNDARY

Three disjoint classes. Every research-produced number belongs to exactly one, and the class
determines its fate. This is the operative core of the contract.

### 3.1 MAY become parameters (the only crossing numbers)

Only a small, literal numeric vector: ≤ 32 `ParameterV1` values, each `integer` (exact `int`) or
`decimal` (canonical `ea-decimal-v1` **string**), with static bounds where meaningful.

- A decimal parameter MUST be serialized as an `ea-decimal-v1` string. Emitting a JSON float is a
  contract violation; `ParameterV1._typed` rejects a non-`str` decimal
  (`src/ea/strategy/registry.py:49`).
- Fitted coefficients ride here as ordinary parameters. This is a **zero-contract-change** path
  (RF-00 §3 ADAPT).

### 3.2 MUST be recomputed by `Logic.on_bar`

Every derived value: indicators, moving averages, ratios, thresholds, regime flags, signal
comparisons — anything that is a function of market data.

- Research MAY use any framework to *discover* the rule and its window lengths.
- The generated `Logic` MUST recompute those values online, from the 7 `StrategyBarV1` fields, in
  its own rolling state. Precomputed or batch-computed feature values MUST NOT be shipped in the
  package.
- Reason: `StrategyBarV1` is a streaming 7-field contract (`src/ea/strategy/sdk_v1.py:13`) and the
  strategy owns its rolling state; the offline/online split must be explicit and auditable
  (RF-00 §3, §10 look-ahead risk).

### 3.3 MUST NEVER enter the causal or economic path

- Any `float64` produced by a vectorized or backend-reduced computation — numpy, numba, pandas, or
  a framework's own metrics.
- Framework performance statistics (Sharpe, Sortino, Calmar, deflated Sharpe), model scores,
  probabilities, ranks, cross-sectional or panel outputs, and search objective values.
- Non-finite values of any kind.

Reason: EA's declared policy `deterministic-ordered-float64-v1` folds `sum_ordered` as a serial
Python loop with **no backend reduction** (`src/ea/core/numeric.py`), and economic quantities are
exact `ea-decimal-v1` values (`src/ea/core/economics.py:75`). A vectorized number is not merely
inaccurate here — it is outside the declared numeric policy, so it cannot exist inside the causal
cone at all. A proxy objective score is never a reported result (RF-00 §10, high severity).

### 3.4 Escalation, explicitly deferred

A model that cannot be expressed as ≤ 32 numeric parameters MUST NOT be forced into the parameter
contract, and MUST NOT be smuggled in as code. RF-01.1 defines **no** escalation implementation:

1. Package V4 data member — **not implemented, not specified here**. Requires its own ADR.
2. The RAD-03 sandboxed strategy host — **out of scope**.

Both are recorded to preserve the decision, not to authorize work.

## 4. DEPENDENCY BOUNDARY

**Where research dependencies physically live:** outside `src/ea`, in a separate environment that
is not the installed product. Research frameworks are reached only by research-side tooling that
imports EA as a library, never the reverse.

Research dependencies MUST NOT enter:

| Boundary | Rule |
|---|---|
| `src/ea` runtime imports | No module under `src/ea` MAY import a research framework, directly or transitively. The coupling is one-way and physically absent. |
| Production wheel runtime dependency graph | No research framework MAY appear in the distribution's runtime requirements. The installed wheel is unchanged by RF-01. |
| Paper environment | The Mac-local simulated Paper runtime MUST NOT require or import research tooling. |
| Broker environment | Same, and additionally: no research dependency MAY be present on any path that can reach a broker adapter. |

Enforcement interface (to be implemented by RF-01.2/1.4, not now): a test that scans `src/ea` for
the framework module names and fails on any hit. The seam is one-way by construction — research
tooling *writes files*; it never links into EA.

Consequence: the adapter's only two outputs are a `.eastrategy` file and a provenance record.
Neither is an import.

## 5. AUTHORITY BOUNDARY

### 5.1 Evaluation boundary — a framework result is never EA evidence

Only this chain may become Candidate evidence:

```
.eastrategy
   → EA scenario validation      (ea backtest validate)
   → EA native backtest          (ea backtest run)
   → EA report / path evidence
```

A framework's metric, backtest, or ranking MUST NOT be cited as evidence for any claim about EA
behaviour — not in a report, not in a Candidate record, not in a PR body. The framework's numbers
describe the framework's own model of the market; EA's describe what EA did.

### 5.2 Selection boundary — research proposes, it never disposes

Research tooling MAY propose and search parameter sets **outside** the delivered EA product
boundary. It MUST NOT:

- automatically ACCEPT a Candidate;
- authorize Paper;
- authorize Live;
- override, supplement, or substitute for EA evidence.

### 5.3 Authority table

| Artifact | Owner | Normative basis |
|---|---|---|
| Capture identity and fingerprint | EA | ADR 0037 |
| Package identity (`artifact_sha256`), validation, exec | EA | ADR 0036 / 0041 |
| Economic outcome, report, path evidence | EA | ADR 0006, ADR 0038 |
| Candidate identity and state | EA | ADR 0039, `docs/candidate-lifecycle.md` |
| ACCEPT / REJECT decision | **Human** | ADR 0043:32, ADR 0047:12 |
| Paper / Live permission | EA (never granted by acceptance) | ADR 0047:12 |
| Proposal and search | Research (outside product boundary) | ADR 0032:58, ADR 0034:60 |

**Binding governance clauses.** ADR 0032 forbids "grid search, parameter generation, optimization,
ranking, retry orchestration, multi-strategy" within the product boundary and excludes an
"optimization engine, or execution authority" (ADR 0032:18, :58). ADR 0034 forbids
"recommendation, cross-window performance delta or automatic Parameter Delta comparison" and states
"No walk-forward, optimizer, experiment platform" (ADR 0034:48, :60). ADR 0043 states "No automatic
ranking, thresholds, paper/live" (ADR 0043:32). ADR 0047 states "ACCEPTED research decision does not
grant execution permission" (ADR 0047:12).

Sweep tooling therefore MUST terminate at: *N candidate parameter sets, each with EA-run evidence.*
It MUST NOT surface ranking, selection, or promotion as a product capability.

Candidate states are `EVALUATED` → `ACCEPTED` | `REJECTED`; decisions are terminal and immutable,
and repeating an identical decision is idempotent (`docs/candidate-lifecycle.md`).

## 6. FAILURE SEMANTICS

All failure is **closed** and non-leaking. EA's existing vocabulary is the contract; the adapter
MUST NOT invent new error surfaces.

| Condition | Detected by | Observable | Required behaviour |
|---|---|---|---|
| Malformed container, wrong members, non-canonical JSON, bad field, >32 params | `inspect_package` | `StrategyPackageError("strategy package validation failed")` | Emit nothing. Report the failure verbatim. |
| Missing `validate_parameters` / `create_logic`, or module raises at exec | `_module` (`package.py:91`) | `StrategyPackageError("strategy code contract failed")` | No executable-code detail is surfaced (`package.py:100`). |
| Staging dir has extra files, output exists or lacks `.eastrategy` | `pack_strategy` | `StrategyPackageError("strategy pack failed")` | Never overwrite; never partially write. |
| Invalid parameter vector at run time | generated `validate_parameters` | `ValueError` | Scenario validation fails; the run does not start. |
| Ambiguous / open action object, float quantity | `decode_action` | `ValueError` | The run fails closed. |
| Capture fingerprint mismatch | EA scenario validation | scenario invalid | Research MUST fail, not re-derive a "matching" fingerprint. |

Rules:

1. The adapter MUST fail closed and MUST NOT emit a partial, best-effort, or "closest" artifact. A
   failed emit leaves no `.eastrategy` file.
2. Failure MUST be reported as failure. A research-side exception MUST NOT be reported as a
   successful emit, a neutral result, or a skipped step.
3. Research failure MUST NOT mutate EA state. Because the seam is read-only (§1) and one-way (§4),
   this holds structurally: there is nothing for research to corrupt.
4. A rejected or unvalidated artifact is not evidence and MUST NOT be retained as if it were
   (§5.1).
5. EA's failure messages are stable and intentionally detail-free. Research MUST NOT attempt to
   pattern-match them to infer package internals.

## 7. PROVENANCE CONTRACT

Two kinds of provenance, with strictly different authority.

**EA-owned (computed, authoritative, never supplied by research):**

- `data_sha256` and `record_count` — capture identity (ADR 0037).
- `artifact_sha256` and `package_id` — package identity, computed over the exact artifact bytes
  (`StrategyPackageIdentityV1`, `package.py:35`).
- `semantic_outcome_sha256`, scenario digest, run manifest, audit lineage — economic evidence
  (ADR 0006, ADR 0038).

**Research-owned (descriptive, non-authoritative, MUST be labelled as such):**

- rule identity and version (`strategy_id`, `strategy_version`);
- the parameter vector, with each decimal in canonical text form;
- source data identity, which MUST equal the `ResearchInput` actually consumed (§1);
- framework name and version, study/run id, objective name and its value, search space, seed;
- the timestamp of emission.

Rules:

1. Research MUST NOT fabricate, guess, or recompute any EA-owned identity field. It MUST record the
   EA-computed values it observed, verbatim, or record their absence.
2. A provenance record MUST be sufficient to re-derive which `ResearchInput` produced which
   `artifact_sha256`. The chain is `ResearchInput → ResearchCandidateSpec → artifact_sha256 →
   RunManifest → evidence`.
3. Search objective values MUST be recorded as provenance, never as results, and MUST carry an
   explicit non-evidence label (§3.3, §5.1).
4. Provenance is descriptive only. It MUST NOT assert that a framework's result validates an EA
   claim, and it MUST NOT be used to shorten or replace the §5.1 evaluation chain.
5. Because an accepted candidate's frozen bytes are bound to recorded EA code and distribution, a
   later EA upgrade requires **new** research evidence; provenance MUST NOT be silently reused
   across an upgrade (`docs/candidate-lifecycle.md`).

## 8. RF-01.2 / 1.3 / 1.4 implementation interfaces

Each slice is independently shippable and abandonable. RF-01.5 (end-to-end flow) is out of scope
here. Framework names are illustrative; none is required by this contract.

### RF-01.2 — Capture → research export

**Interface.** Consume a `ResearchInput` (§1) and produce a documented research-side
materialization (e.g. a CSV/panel file) at a caller-specified path outside EA's durable state.

**Invariants.** Exports are read-only and never write EA state (§1.1); the exported form carries
the `data_sha256` and `record_count` it was derived from; the export is disposable and derived, and
never a source of truth; no new EA data format is introduced; EA runtime semantics are untouched.

### RF-01.3 — One search backend

**Interface.** Given a research-side objective and a declared, bounded parameter space, produce
**N candidate parameter sets**, each with its own `ResearchCandidateSpec` and its own EA-run
evidence.

**Invariants.** Runs strictly offline; imports no EA economic module (§4); terminates at "N
parameter sets with EA evidence" and no further (§5.2); MUST NOT rank, select, promote, or
auto-accept — ordering, if any, MUST be presented as unscored enumeration, not as a
recommendation (ADR 0032:58, ADR 0034:48); must respect the bounded-combination bound of ADR 0032.

### RF-01.4 — Package emitter

**Interface.** Given one `ResearchCandidateSpec`, emit a canonical `.eastrategy` via `pack_strategy`
(§2.4) and a provenance record (§7).

**Invariants.** Byte-exact canonical manifest via EA's packer only; exactly two members; never
overwrite; fail closed with no artifact on any error (§6); the generated `strategy.py` recomputes
all features online from `StrategyBarV1` (§3.2) and emits only closed action objects (§2.3); no
framework import appears in the artifact; model hosting is out of scope (§3.4).

### Slice dependency order

Per RF-00 §8, matching each slice's stated dependencies:

```
RF-01.1 (this contract)
   ├── RF-01.2 (export) ── RF-01.3 (search) ──┐
   └── RF-01.4 (emitter) ─────────────────────┴── RF-01.5 (end-to-end)
```

## 9. Explicit non-goals

- No production code, no new dependency, no framework installation.
- No runtime change; no economic semantics change.
- No Package V4 implementation and no model hosting (§3.4).
- No change to Paper, Risk, Broker, Ledger, Reconciliation, Recovery, or Live authority.
- No architecture refactor. If a proposal needs one, it is out of scope by definition.
- No E2E requirement for this unit.

## 10. Deliverable checklist

| Required | Where |
|---|---|
| INPUT CONTRACT | §1 |
| OUTPUT CONTRACT | §2 |
| NUMERIC BOUNDARY | §3 |
| DEPENDENCY BOUNDARY | §4 |
| AUTHORITY BOUNDARY | §5 |
| FAILURE SEMANTICS | §6 |
| PROVENANCE CONTRACT | §7 |
| RF-01.2 / 1.3 / 1.4 interfaces | §8 |
| Future model boundary deferred | §3.4 |
