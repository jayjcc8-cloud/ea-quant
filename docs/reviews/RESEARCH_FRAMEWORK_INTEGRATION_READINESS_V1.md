# Research Framework Integration Readiness V1

RF-00 assessment. Scope: based on `main` at `8a100df`, evaluate mature quant research
frameworks (Qlib, vectorbt, Backtrader, and better-fitting alternatives) for compatibility
with the delivered EA product, and fix the minimal, low-invasion fusion path after M4 RC.

This document is analysis, a contract draft, and one measured spike. It changes no runtime,
adds no dependency, and alters no economic semantics.

## 1. Executive conclusion

**EA should not adopt a research framework as a runtime dependency. It should adopt one as an
offline research dependency behind a one-way artifact boundary that already exists.**

The boundary is not a design proposal — it is the delivered `.eastrategy` package contract plus
the strict Scenario loader. A research framework's job ends when it emits a numeric parameter
vector (or, later, a small model). Everything after that point is EA's existing, unmodified
economic chain: strategy package → signal authority → portfolio → risk → matcher → ledger →
reconciliation → audit.

The decisive constraint is not data format. It is three converging facts established from EA's
own source and ADRs:

1. **The strategy contract is streaming, not batch.** `StrategyBarV1` is exactly seven fields and
   the strategy maintains its own rolling state across `on_bar` calls. Research frameworks
   compute features in batch over a whole panel.
2. **EA's numeric policy forbids backend reduction inside the causal cone.**
   `deterministic-ordered-float64-v1` folds `sum_ordered` without a backend reduction, so a
   numpy/Numba vectorized number can never be dropped into the economic path.
3. **The expressible action space is tiny.** `HOLD | ENTER_LONG(quantity) | EXIT_LONG` over a
   single long position (`single-long-round-trip-v1`, `bounded-long-round-trips-v1`). Any
   cross-sectional, short, or multi-position alpha must undergo an explicit semantic reduction
   before it can exist in EA at all.

Facts 2 and 3 mean the adapter must carry **decisions and rules, not numbers computed by the
framework**. Fact 1 means the framework must stay entirely offline. Together they make a
one-way, offline-to-package seam the only low-invasion path — and the good news is that seam is
already built and already strict.

### Acceptance standard (explicit)

Per the task's own acceptance constraint: **a successful import or a passing demo is not
evidence of mature integration.** Section 6 records what the spike did and did not prove. The
completion standard used here is that each seam is named against a real EA contract, each
capability is assigned an authority owner, and the one thing the spike claims is the one thing it
actually measured through EA's own loader, matcher, ledger and reconciler.

## 2. Framework fit matrix

Maintenance, license and dependency facts below were verified against the PyPI JSON API, the
GitHub REST API and primary project documentation on 2026-09-29. Where only a secondary source
existed it is marked.

| Framework | Version / status (2026-09-29) | License | Adds to EA's dep tree | Forces its own execution engine? | Fits EA? |
|---|---|---|---|---|---|
| **Qlib** | Active (Microsoft Research) | MIT | numpy, pandas, scipy, lightgbm, torch (optional), cython `.bin` reader | **No** — separate backtest module, but its data/model layer is independently usable | **Partial — data+feature+model layer only, offline** |
| **vectorbt (OSS)** | **1.1.1, 2026-09-26** — alive again: 1.0.0 (Apr 2026), 1.1.0 (Jul), 1.1.1 (Sep) | Apache-2.0 **+ Commons Clause** (not OSI; no resale) | numpy≥2.4.6, **pandas≥3.0.3**, numba≥0.66, scipy, scikit-learn, matplotlib, plotly | Simulates its own portfolio, but **decoupled**: indicators and metrics compute standalone | **Yes — offline sweep + metrics only** |
| **Backtrader** | **Frozen: 1.9.78.123, 2023-04-19**; **issue creation restricted**; classifiers end at Python 3.7 | **GPLv3+** copyleft | zero core deps | **Yes, structurally.** Cerebro instantiates its own `BrokerSimulator`; no substitutable execution seam | **No** |
| **optuna** | 5.0.0, 2026-09-07 | MIT (not stated on PyPI; historical) | numpy, sqlalchemy, alembic, pyyaml, tqdm, colorlog | No | **Yes — search layer** |
| **TA-Lib (python)** | 0.8.1, 2026-09-21; wheels bundle the C library | **wrapper license unverified** (C lib historically BSD-3) | **numpy only** | No | **Yes — indicator reference** |
| **talipp** | 2.7.0, 2025-09-09 (≈12 mo stale) | MIT | **none at all** | No | Yes — incremental indicators, O(1) update/remove |
| **exchange-calendars** | 4.13.2, 2026-03-10 | Apache-2.0 | pandas, pyluach, toolz, tzdata | No | Yes — the one valuable piece of the zipline lineage |
| **nautilus_trader** | 2.0.0rc5, 2026-09-15 (1.x final: 1.231.0) | LGPL-3.0-or-later | pandas, pyarrow, fsspec (pinned), msgspec, uvloop; **112–189 MB wheel** | **Yes — it *is* a runtime**; backtests reuse live components | **No — reference oracle only** |
| **zipline-reloaded** | 3.1.1, 2025-07-19; dependabot-only since 2025-11 | Apache-2.0 | **28 core deps** incl. bcolz-zipline, h5py, tables, sqlalchemy | Yes | **No — take exchange-calendars instead** |
| **mlflow / dvc** | 3.16.1 / 3.67.1 | (Databricks hdr) / Apache-2.0 | ~20 deps / **42 deps** | No | Deferred — as a read-only view only |
| **pandas-ta** | 0.4.71b0, 2025-09-14 | — | numba pinned ==0.61.2 | No | **No — GitHub repo returns 404; live supply-chain dispute (issue #30 open, unanswered)** |

Two corrections to the prevailing 2024–2025 blog consensus, both registry-verified:

- **"vectorbt OSS is abandoned" is false as of 2026.** Four releases in six months including a
  new optional Rust engine and a numerical-correctness fix to the deflated Sharpe ratio.
- **Backtrader is worse than "unmaintained."** Issue creation is restricted, so a bug cannot even
  be filed; there has been no release since April 2023.

## 3. KEEP / REPLACE / ADAPT

"Replace" here means *stop self-building and consume a mature implementation offline*. It never
means removing EA authority.

### KEEP — must remain EA authority, never delegated

| Capability | Why it cannot move |
|---|---|
| **Economic semantics** — fills, fees, cash, position, P&L | `ea-fill-v1`, `deterministic-commission-v1`, the ledger and reconciliation are the product. A framework's fill convention diverging from EA's is a defect, not a feature. |
| **Deterministic numeric policy** | `deterministic-ordered-float64-v1` `sum_ordered` has no backend reduction. Vectorized framework arithmetic is definitionally outside the causal cone. |
| **Point-in-time admission** | `MarketDataEnvelope(payload, source, available_at, source_sequence, revision)` + `AdmissionCursor` + `Adjustment.RAW` — the no-look-ahead guarantee is enforced by EA, not by a framework. |
| **Action space + lifecycle** | V2 actions and `single-long-round-trip-v1` / `bounded-long-round-trips-v1` are the contract. |
| **Risk / limits / approval** | `max_order_quantity`, `max_position_quantity`, `max_notional` and the risk approval chain. |
| **Audit lineage + reproducibility** | ADR 0006 run manifest, the POSIX hash-chained append-only journal, `causal_market_sha256`, lineage digests. |
| **Candidate acceptance** | ADR 0043/0047: acceptance is a human research decision that grants **no** execution permission. |
| **Paper / Live authority** | Out of scope for this task by instruction, and structurally unreachable from research code. |

### REPLACE — stop self-building, consume mature offline

| Today | Replace with | Note |
|---|---|---|
| Hand-rolled indicator math in research scripts | **TA-Lib** (numpy-only, bundled C wheels) or **talipp** (zero deps) | Reference fidelity for 150+ indicators is not worth re-deriving |
| Ad-hoc `itertools.product` sweep loops | **optuna** (TPE/CMA-ES, pruning, crash-safe `load_if_exists=True` studies) | numpy-only deps; composes with the manifest rather than replacing it |
| Hand-rolled performance statistics | **vectorbt** metrics accessors — Sharpe, Sortino, Calmar, Omega, **deflated Sharpe ratio** | Consume metrics only; ignore `pf.value()` / `pf.trades` / `pf.positions` |
| Bespoke trading-calendar handling | **exchange-calendars** | Pulled directly, not via zipline |
| Feature engineering for small models | **Qlib** feature/expression layer | Produces numbers for fitting; never enters the runtime |

### ADAPT — keep EA's contract, change only the research-side producer

| Surface | Adaptation |
|---|---|
| **`.eastrategy` package** | No change. The adapter *emits* one. Fitted coefficients ride as ordinary numeric scenario parameters (≤32, `integer`/`decimal` only). |
| **Scenario V4/V5** | No change. The adapter emits the parameter block; EA validates it as it does today. |
| **`StrategyBarV1`** | No change. Research features are recomputed inside the generated `Logic` from the 7 authoritative fields, so the online/offline split is explicit and auditable. |
| **Model hosting** | Deferred. When a model outgrows 32 numeric params → Package V4 data member; for arbitrary code → the RAD-03 sandboxed strategy host. |

### Explicitly NOT to be replaced

Backtrader (frozen, GPLv3+, restricted issues, and a structurally inescapable second execution
engine); zipline-reloaded (dormant, 28 deps); nautilus_trader as a research tool (it *is* a
runtime — 112–189 MB wheels); `pandas-ta` (repository gone, open supply-chain dispute).

## 4. Answers to the seven questions

**Q1 — Which capabilities should EA stop self-building?**
Indicator math, parameter search, performance statistics, trading calendars, and feature
engineering. These are mature, well-tested commodity layers where EA's hand-rolled versions are
strictly worse and where being worse is invisible until it corrupts a research conclusion.

**Q2 — Which must remain EA authority?**
Everything in the KEEP table. The one-line rule: **a framework may decide what to propose; it may
never decide what happened.** Fills, cash, positions, fees, admission and audit are statements
about what happened and stay in EA, permanently.

**Q3 — Qlib's most reasonable insertion point?**
At the **feature and model layer, strictly offline**, upstream of the parameter vector. Qlib is
strongest exactly where EA is deliberately empty (panel feature engineering, expression DSL,
Alpha158/Alpha360, a model zoo) and its `.bin` float32 store plus its own backtest module are
exactly what must not enter EA. Concretely: Qlib consumes a registered EA OHLCV capture, emits
features and fitted coefficients; the adapter converts those into scenario parameters.

**Q4 — How do data model / feature / experiment / model-signal map to an EA Candidate?**
The mapping is a strict narrowing, with the authority boundary at the last step:

| Research side (Qlib / vectorbt) | EA side | Authority |
|---|---|---|
| Panel DataFrame / `Dataset` | one registered capture, **exactly one instrument** (ADR 0037) | EA owns identity |
| `Feature` / expression | recomputed online inside `Logic.on_bar` from `StrategyBarV1` | EA owns the 7 fields |
| Model / fitted coefficients | numeric scenario parameters (≤32, `integer`/`decimal`) | EA's `ParameterV1` contract |
| `Signal` (cross-sectional, continuous, ranked) | reduced to `HOLD / ENTER_LONG(q) / EXIT_LONG` | **EA owns the reduction** |
| `Experiment` / run record | EA `RunManifest` (ADR 0006) + the batch/holdout/comparison loop | EA owns lineage |
| Model artifact (too large for params) | Package V4 data member, or RAD-03 sandboxed host | **deferred** |
| "Best" model by framework metric | **not** a Candidate — a human accepts (ADR 0043/0047) | **human owns acceptance** |

**Q5 — Is an adapter needed rather than intruding into EA runtime?**
Yes, and it is not a matter of taste. Three independent EA contracts make intrusion impossible
without changing economic semantics or the numeric policy — both forbidden by this task. The
adapter is **one-way (research → artifact) and offline**. Research code never imports EA runtime
modules that hold economic authority; it only writes a package and a parameter block.

**Q6 — Which existing EA code could be replaced by mature frameworks?**
Only research-side code, and mostly code that does not exist yet. Within the delivered product,
the honest answer is: **almost none of the runtime**, because the runtime is the differentiator
and is held to a determinism standard these frameworks do not meet. The replaceable surface is
future research tooling — sweep loops, indicator implementations, metrics.

**Q7 — Which must never be replaced?**
The KEEP table, with the sharpest edge on the numeric policy and the ledger/reconciliation pair.
Replacing either would mean the product no longer means what it says.

## 5. Recommended framework and rationale

**Primary: none as a runtime dependency. As the offline research layer, in order:**

1. **optuna for search** — the highest value per unit of integration cost. numpy-only deps,
   0.4 MB, and it supplies what is genuinely expensive to hand-roll correctly: TPE/CMA-ES
   samplers, pruning, and crash-safe resumable studies. Critically it records *only* params and
   objective values — no code hash, no data hash, no environment — so it complements the EA
   manifest rather than competing with it. **The manifest stays the system of record.**
2. **TA-Lib for indicators** — numpy-only, 3–4 MB, wheels since 0.6.5 bundle the C library. 150+
   indicators with years of reference-fidelity testing. *(Verify the wrapper's license first —
   it is unpopulated on PyPI.)*
3. **vectorbt (OSS) for sweeps and metrics** — now genuinely maintained again. Use the indicator
   and `from_signals` paths plus the metrics accessors; ignore the portfolio simulation. Its
   unique asset is the deflated Sharpe ratio, a statistic vectorbt itself shipped a correctness
   fix for in September 2026 — a fair proxy for how easy it is to get wrong.
4. **Qlib for features and models** — the strongest panel/feature/model layer, inserted strictly
   offline and strictly upstream of the parameter vector.
5. **talipp** (zero deps, O(1) append/update/remove) — a good fit if the walk-forward loop needs
   cheap "mutate the last bar and recompute", and a hedge against TA-Lib's license question.

**Case against each rejected framework:**

- **Backtrader — structurally disqualified.** It is a second execution engine by construction:
  Cerebro instantiates its own `BrokerSimulator`, orders have no route out, fills default to
  "completely executed in a single shot," and the market-order convention (next bar's open) is
  baked in. Adopting it would mean two engines whose fills disagree — precisely the failure ADR
  0003 exists to prevent. Add GPLv3+ copyleft, a freeze since April 2023, restricted issue
  creation, and classifiers ending at Python 3.7, and there is no version of this worth doing.
- **nautilus_trader — right idea, wrong layer.** It is a *runtime*, and its own docs say backtests
  reuse live components. That is the thing the adapter exists to keep out. Its real value is as a
  differential-testing oracle with genuine component parity — a future option, not an RF-01 one.
- **zipline-reloaded — take the calendar, leave the rest.** 28 core deps including
  `bcolz-zipline`/`h5py`/`tables`, and dormant since July 2025.
- **pandas-ta — do not depend on it.** The original GitHub repository returns 404, PyPI history
  was reportedly wiped, and the open supply-chain dispute has had no maintainer response.

**Governance note (binding).** ADR 0032, 0034 and 0043 forbid grid search, parameter generation,
optimization, ranking, automatic selection and promotion *within the product boundary*. Sweep
tooling therefore must terminate at "here are N candidate parameter sets, each with EA-run
evidence." No framework feature that automates selection may be surfaced as a product capability.

## 6. Measured spike — what was and was not proven

An isolated spike was run **outside** the repository, using stdlib only, against a registered EA
capture. It performed a 64-combination search (4 slow windows × 4 entry thresholds × 4 exit
thresholds) with a deliberately crude proxy objective, then emitted the winning numeric vector as
a real `.eastrategy` package (canonical `manifest.json` + `strategy.py`).

Results, all produced by EA's own tooling:

| Step | Command | Result |
|---|---|---|
| Package the research output | `ea strategy pack` | `artifact_sha256 62119b58b9696d7d027c391857aa468ffc52a322c8bfdab6ee7097adf878a60d`, schema `ea-strategy-package-v2`, Action **V2**, `single-long-round-trip-v1` |
| Inspect the capture | `ea data inspect` | `data_sha256 2bbb316b…`, 300 records |
| Validate the scenario | `ea backtest validate` | `scenario valid: BacktestScenario v4` |
| **Run the economics** | `ea backtest run` | **`status: success`, `terminal_state: completed`, 2 execution legs, 1 completed round trip, `ending_cash 9997.27 USD`, `reconciliation {cash: match, position: match}`, `semantic_outcome_sha256 594059e4…`, `ea.backtest-single-run-result.v3`** |

**What this proves.** A research-side numeric search over a registered capture becomes a valid,
executable EA strategy package, validates against the strict Scenario loader, and drives the real
economic chain — portfolio, risk approval, matcher, fills with commission, ledger, reconciliation
and the hash-chained audit journal — with **no EA runtime change, no new dependency, and no
economic-semantics change**. The seam is real and it already exists.

**What this does not prove, and what no demo could.** The spike used a crude proxy objective and a
64-point grid, not a real framework. It did not exercise Qlib, optuna, vectorbt or TA-Lib. It
proves the *boundary* works; it does not prove that any specific framework produces good research
through it, nor that a fitted parameter vector has any out-of-sample value. Per the task's own
acceptance constraint, the integration is not "complete" on the strength of this run — the run's
only job was to replace an assumed seam with a measured one.

## 7. Integration seams (explicit)

**Seam A — data in (EA → research), read-only.**
A registered capture (strict 15-column `ea-phase1-ohlcv-csv-v1` profile, SHA-256 canonical
fingerprint) is read by research tooling. Research never writes to EA data. Single instrument
(ADR 0037).

**Seam B — parameters out (research → EA), one-way.**
Research emits a numeric parameter vector. It rides as ordinary scenario parameters
(`ParameterV1`: `integer` or `decimal`, ≤32). **No contract change.** If a fitted model exceeds 32
parameters the escalation path is a Package V4 data member, then the RAD-03 sandboxed host.

**Seam C — strategy code (research → EA), one-way.**
Research emits `strategy.py` exposing `validate_parameters(p, context)` and `create_logic(p)`
returning a `Logic` with `on_bar(bar, position) -> dict`. Packed as an ordinary `.eastrategy`.
The module is `exec`'d under EA's existing `_module()` contract requiring both callables, inside
the 1 MiB / 2-member envelope.

**Seam D — evaluation (EA owns, exclusively).**
Every candidate parameter set is evaluated by EA's own backtest, one explicit batch member each
(ADR 0032's 2–10 combo bound). The framework's numbers are never the acceptance evidence.

**Seam E — acceptance (human owns, exclusively).**
ADR 0043/0047. Acceptance grants no execution permission.

**Non-seams (must not be crossed).** Research code must never import EA modules holding economic
authority; framework execution engines never run inside EA; no framework number enters the causal
cone; no automatic ranking or promotion.

## 8. Minimal RF-01 implementation breakdown

Scoped to the smallest useful increment that respects every ADR above. Each slice is independently
shippable and independently abandonable.

| # | Slice | Deliverable | Depends on | Explicitly out of scope |
|---|---|---|---|---|
| RF-01.1 | **Adapter contract draft** | A written contract for seams B and C: the parameter-vector schema, the `strategy.py` template, and the emit rules. **Documentation only, no code.** | — | Any framework |
| RF-01.2 | **Capture → research export** | `ea data inspect` extended (or a sibling) to export a registered capture to a documented research-side form without touching EA runtime semantics | RF-01.1 | New formats in EA |
| RF-01.3 | **One search backend** | optuna behind the RF-01.1 contract, offline, terminating at "N parameter sets with EA-run evidence" | RF-01.1, RF-01.2 | Ranking, auto-selection, promotion |
| RF-01.4 | **Package emitter** | A tool that turns a chosen parameter vector into a canonical `.eastrategy` (the spike's `build_spike.py`, hardened) | RF-01.1 | Model hosting |
| RF-01.5 | **Target flow, end to end** | One real strategy from research → package → Scenario V4/V5 → EA backtest → Candidate, with EA evidence | RF-01.3, RF-01.4 | Paper, Live |

RF-01.5 is the flow in section 9. Anything beyond RF-01.5 — model hosting, panel/multi-instrument
support, walk-forward — requires its own ADR, because ADR 0032/0034 cap the product boundary at
explicit bounded combinations and forbid walk-forward and optimization claims.

## 9. Target flow: research → Candidate → EA Paper

One real strategy, using the spike's rule as the worked example:

1. **Research (offline, framework-owned).** Qlib/vectorbt/TA-Lib consume a registered EA capture
   (`data_sha256 2bbb316b…`, 300 records, one instrument). Features are computed in batch. optuna
   searches the parameter space.
2. **Emit (adapter, one-way).** The chosen vector becomes scenario parameters; the fixed rule
   becomes `strategy.py`. Nothing else crosses.
3. **Package (EA contract, unchanged).** `ea strategy pack` → `artifact_sha256 62119b58…`, schema
   `ea-strategy-package-v2`, Action V2, `single-long-round-trip-v1`.
4. **Validate (EA contract, unchanged).** `ea backtest validate` → `scenario valid:
   BacktestScenario v4`.
5. **Evaluate (EA authority).** `ea backtest run` → `status success`, `terminal_state completed`,
   2 execution legs, 1 round trip, `ending_cash 9997.27 USD`, `reconciliation {cash: match,
   position: match}`, `semantic_outcome_sha256 594059e4…`, `ea.backtest-single-run-result.v3`,
   hash-chained audit journal.
6. **Batch and holdout (EA authority).** Additional parameter sets run as explicit batch members
   (ADR 0032); the chronological holdout (ADR 0034) provides the two-job relationship. No
   walk-forward, no optimizer, no statistical pass/fail claim.
7. **Candidate (human authority).** A human accepts, citing the EA evidence. Acceptance grants no
   execution permission (ADR 0043/0047).
8. **Paper (EA authority, unchanged and untouched by this task).** The accepted candidate loads at
   the existing Paper Candidate-loading boundary into `LOCAL_SIMULATED` Paper.
   **Paper / Risk / Broker / Ledger / Reconciliation / Recovery / Live authority are not modified
   by RF-00 and are not modified by RF-01 as scoped here.**

The framework appears only in steps 1–2. Steps 3–8 are the delivered product, unmodified.

## 10. Risks and migration boundaries

| Risk | Severity | Mitigation |
|---|---|---|
| **Proxy objective mistaken for economic truth** | **High** | Only EA's backtest is acceptance evidence (Seam D). The spike's proxy score is never reported as a result. |
| **Look-ahead smuggled through features** | **High** | EA's admission cursor stays authoritative; generated `Logic` recomputes features online from `StrategyBarV1` only. Batch-computed features must never be precomputed into the package. |
| **Framework dependency creep into production** | **High** | The seam is one-way. Research deps live in a separate extra/environment; nothing in `src/ea` may import them. |
| **Governance violation by automation** | **High** | ADR 0032/0034/0043 bound the product: sweeps terminate at N evidenced candidates; no ranking, selection or promotion. |
| **vectorbt license (Apache-2.0 + Commons Clause)** | Medium | Not OSI. Fine for internal research; blocks resale of a product consisting primarily of it. Keep it out of any shipped artifact. |
| **TA-Lib wrapper license unverified** | Medium | Confirm before depending on it; `talipp` (MIT, zero deps) is the fallback. |
| **Qlib `.bin` float32 lock-in** | Medium | Never let Qlib's store become a source of truth; EA's capture and fingerprint remain canonical. |
| **pandas 3 / numba / scipy in the research env** | Medium | Isolate in a research environment. EA's runtime stays at pydantic/numpy/pyyaml/typer. |
| **Spike code mistaken for production** | Medium | The spike lives outside the repo and is not merged; RF-01.4 hardens it deliberately or discards it. |

**Migration boundaries (hard):**

- No third-party framework enters `src/ea` or the installed distribution.
- No framework enters the causal cone — the numeric policy forbids it and this task forbids changing it.
- No EA economic semantics change, at any point, for any framework reason.
- Paper / Risk / Broker / Ledger / Reconciliation / Recovery / Live authority are untouched.
- Nothing in RF-01 requires an architecture refactor; if a proposal does, it is out of scope by definition.

## 11. Deliverable checklist

| Required | Where |
|---|---|
| FRAMEWORK_FIT_MATRIX | §2 |
| KEEP / REPLACE / ADAPT list | §3 |
| Recommended framework + rationale | §5 |
| Explicit integration seams | §7 |
| Minimal RF-01 implementation breakdown | §8 |
| Risks and migration boundaries | §10 |
| One real strategy research → Candidate → EA Paper flow | §9 |
| The seven questions answered | §4 |
