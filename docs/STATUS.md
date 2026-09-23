# Project Status

## Current Phase

**Research Validation — bounded repeated long-only offline research.**
**main healthy / live unavailable.**

When this file is read from merged `main`, the containing `main` commit and its CI are the
authoritative checkpoint. GitHub Issues and pull requests carry mutable coordination state; this
file records only durable product state and the next bounded outcome.

## Phase Objective

Product positioning: AI-native quantitative R&D and strategy promotion system. Commercial north
star: reduce Idea → Evidence-ready Candidate time and cost. AI integration is not delivered.

From an installed wheel and outside a Git checkout, validate and run one user-supplied strict
scenario with local OHLCV, deterministic initial funding, bounded risk, simulated execution,
ledger state, reconciliation, audit, and semantic result evidence. The same bounded path is also
available through a single-user Web UI served on loopback only. No broker, remote network, secret,
external write, deployment, or live capability is part of this objective.

## Completed

- Issue #220 adds PPV-04 Structured Operational Logging V1 to the existing scenario runtime.
  Machine-readable operational events reuse run, strategy, signal, order, client submission and
  Fill identities through simulation, ledger updates and reconciliation. Logs remain observational;
  audit and ledger retain authority. See [logging contract](operational-logging.md).

- Issue #218 / ADR 0046 adds Production Runtime Profile V1: the existing commit-bound bundle,
  strict pinned installed launcher, external persistent workspace/inputs and one loopback-only
  systemd service. Explicit activation and compatible application rollback preserve state;
  unknown downgrade compatibility fails closed. Linux CI exercises the installed supervisor path.
  Actual VPS deployment and reboot acceptance remain NOT_YET_HOST_VERIFIED. See
  [runtime operations and limitations](production-runtime.md). Paper/Live remains unavailable.

- Phase 1 delivery reset is complete; its offline and governance-freeze boundaries remain in force.

- Phase 0 engineering foundation is released as `v0.1.0`.
- `main@969f5f086acd9c28f80e3960a56b83981b1d0e5d` is the verified pre-reset baseline.
- Merged code already provides deterministic time/data handling, strategy interfaces, portfolio
  and risk controls, matching, execution facts, ledger, reconciliation, audit, and recovery
  components.
- ADR 0027 fixes the offline-only product and fail-closed boundary.
- ADR 0029 freezes governance expansion and restores bounded T1 product delivery.
- The installed `ea backtest validate --scenario FILE` command strictly validates
  `BacktestScenario v1` and selected local OHLCV without economic mutation.
- The installed `ea backtest run --scenario FILE --output-root DIR` command runs outside a Git
  checkout and creates a fresh UUID4 attempt with funding, audit, and result evidence.
- Initial cash is a positive, currency-bound, quantized, balanced sequence-1 transaction.
  Equivalent replay is exactly once; conflicting funding fails closed without mutation.
- `always-flat-v1` completes with funded cash and no trade. `bounded-long-v1` uses the existing
  strategy, risk, order, matcher, Fill, ledger, reconciliation, lifecycle, audit, and identity
  authorities.
- Maximum order quantity, maximum position, available cash, and maximum notional are enforced
  before trading mutation through existing allow/resize/reject risk semantics; a funded ledger
  also rejects any Fill that would create negative cash.
- The public strategy seam rejects future, non-active market payloads. Equivalent fresh processes
  retain the same lineage and semantic outcome while using different RunIds.
- The fixed accepted path traverses signal, risk acceptance, simulated order and Fill, portfolio
  cash/position state, reconciliation, and audit before one successful terminal record.
- The risk-rejected path creates no order, Fill, cash, or position mutation.
- A reconciliation mismatch records failure evidence without a success terminal or success
  report.
- Every fresh offline demo attempt now receives a distinct UUID4 `RunId`; equivalent attempts
  retain the same canonical reproducibility lineage and semantic/economic outcome digest.
- The current deterministic demo records `randomness_profile = none` and
  `master_seed = not_applicable`; attempt-bound IDs, paths, audit identities, and non-economic
  timestamps do not enter its semantic outcome projection.
- The installed `ea backtest resume --run-dir ATTEMPT_DIR` command verifies the attempt's persisted
  scenario, data, distribution, instrument, funding, risk, execution, randomness, journal, and
  reconciliation identity before continuing the same RunId and lineage.
- Supported resume frontiers are funding-durable before trading, a completed durable economic
  dispatch, and reconciled economics before final success publication. Existing audit logical
  retries rebuild in-memory authorities without duplicate funding, Order, Fill, or ledger effects.
- Successful completed attempts are validated non-mutating no-ops. Failed terminals, ambiguous
  state, committed journal corruption, or identity conflicts reject without a success result.
- Final success is an atomic `result.json` publication after durable terminal evidence; handled
  internal failures retain classified evidence without exception detail or false success.
- The installed `ea backtest report --run-dir ATTEMPT_DIR --output-dir REPORT_DIR` command reads
  only a verified completed attempt and atomically publishes canonical `report.json` and
  `summary.txt` in a separate directory without running or resuming trading.
- `BacktestReportV1` binds its field sources to manifest, funding, journal/exported audit, result,
  and admitted OHLCV evidence. It reports last-price equity, net P&L, total return, unique
  Order/Fill counts, and policy-proved Phase 1 zero commission using exact decimal arithmetic.
- Repeated reporting of one attempt is byte-stable; equivalent independent attempts retain
  distinct RunIds while their accepted semantic and economic report projection remains equal.
- The Product Owner accepted #84 for commit
  `ebd509e25bca86c00eaa09de7746445ccc891914` and the retained
  `ea_quant-0.2.0-py3-none-any.whl` with SHA-256
  `04fc44fbbceab47854291e9ef073d15378f496c482df9cd998e8081f525694d6`.
- Annotated tag `v0.2.0` resolves to that accepted commit. The corresponding
  [GitHub Release](https://github.com/jayjcc8-cloud/ea-quant/releases/tag/v0.2.0) is published as a
  prerelease with only the accepted wheel and its SHA-256 checksum asset. No package-registry
  publication or deployment was performed.
- ADR 0030 permits a post-v0.2.0, loopback-only Web adapter without changing the released
  v0.2.0 identity or any engine, economic, risk, recovery, reconciliation, or report authority.
- The installed candidate's `ea web serve` command fixes the listener to `127.0.0.1`, serves the
  production React build and same-origin API, and keeps scenario, workspace, and UI roots separate.
- The real `/backtests` UI lists and validates registered strict scenarios, creates one fresh
  idempotent job through the existing engine, displays its formal report, survives refresh and
  completed-job service restart, and downloads only the two report artifacts.
- The local job index is atomic and separate from attempt evidence. A workspace has one service
  writer, one active job, no queue, and interrupted jobs are preserved without automatic rerun.
- Host/origin/header/content-type, identifier, artifact allowlist, and resolved-root checks fail
  closed at the new HTTP boundary. No CORS relaxation, arbitrary browser path, upload, or CDN is
  part of the product.
- ADR 0031 adds one bounded local research loop without creating an Experiment or instrument/data
  configuration system: initial cash and quantity are editable, while symbol remains read-only and
  bound to a registered, verified scenario/data combination.
- Each new Web job stores the resolved, validated, normalized scenario snapshot, source and final
  identities, an input digest, creation time, and attempt identity. History is newest-first and
  “Use parameters” starts a fresh validation/run rather than mutating or resuming prior evidence.
- Pairwise comparison reads two persisted snapshots and verified formal reports, computes exact
  decimal deltas, and leaves failed or unavailable reports without fabricated metrics or deltas.
- Issue #182 / ADR 0035 replaces the strategy-specific Web surface with StrategyDescriptorV1,
  resolved backend constraints and a closed built-in StrategyRegistryV1. Bounded-long and moving
  average entry use generic integer/decimal parameters through single-run, history reuse, batch,
  comparison and Chronological Holdout. Always-flat remains CLI-compatible and not research-visible.
- BacktestScenarioV2 binds strategy ID/version and the complete canonical parameter map in a
  separate digest domain; V1 identities and historical snapshots/relations remain unchanged.
- Moving average entry uses deterministic rolling state from admitted raw revision-0 bars and the
  existing active market, signal, risk, Order/Fill, matcher, ledger, audit and report authorities.
  Dynamic history/next-bar constraints remain backend-owned; target holdout defaults never replace
  frozen source parameters. AI model integration remains unavailable; local artifact loading is bounded by ADR 0036.
- ADR 0032 adds a bounded 2-10 member Web experiment batch for one registered strategy/scenario.
  The backend validates the full set before creating jobs, rejects normalized duplicates, runs
  normal jobs serially through the existing engine, and persists only batch-to-job membership.
- Batch detail derives presentation state from member jobs, keeps mixed failures readable, reuses
  formal-report summaries and pair comparison, and survives restart without React-state authority.
- ADR 0033 derives a bounded Analysis V1 directly on batch detail: all members retain canonical
  parameters and status, while verified reports expose equity, net P&L, and total return with
  currency. Explicit exact-decimal sorting, status filtering, stable missing-last behavior, and
  the existing pair comparison support human judgment without ranking or recommendation.

- Issue #178 adds explicit `deterministic-commission-v1` at registered scenario execution policy.
  Canonical Decimal bps in [0,10000] produce one half-even currency-quantized commission per Fill,
  balanced ledger cash effects, versioned semantic evidence, and verified BacktestReportV1 fees.
  The existing Web history, comparison and batch analysis consume the after-fee formal results.
  Legacy zero-fee scenario identities remain unchanged, and all three supported resume frontiers
  remain available without new recovery states or historical migration.

- Issue #180 / ADR 0034 adds Chronological Holdout V1: explicit successful source selection,
  backend-frozen canonical strategy parameters, strictly later compatible registered scenario,
  atomic job/relationship publication, and independent after-fee formal reports. The minimal
  relationship and both runs reopen after restart; original batch membership stays unchanged.
  The claim is chronological holdout evaluation; no statistical conclusion or automatic selection.

- Issue #185 / ADR 0036 adds deterministic two-member `.eastrategy` artifacts, the public
  Strategy SDK V1, explicit local artifact roots and additive catalogs without built-in shadowing.
  Scenario V3 binds the complete artifact SHA and canonical parameter map; V1/V2 identities stay
  unchanged. Executed package bytes are preserved in jobs/attempts and reused exactly by Holdout.
- The installed CLI provides `strategy pack`, `validate` and `inspect`; a new trusted local
  strategy requires no EA source edit or rebuild. Generic Web controls, history/reuse, batch and
  comparison retain source identity. Executable Python is trusted local code, not sandboxed;
  upload, URLs, package installation, dependency bundles and implicit scanning are unsupported.

- Issue #187 / ADR 0037 adds optional explicit `--data-root` and strict local OHLCV discovery.
  Full-capture data-only derivation reuses the historical decoder and preserves V1/V2/V3 identity
  domains. Raw SHA is provenance; dataset filenames are non-semantic. Generic Web run/reuse,
  batch, comparison and frozen chronological Holdout retain immutable dataset evidence and old
  reports remain readable after external data removal. `ea data inspect` emits canonical JSON.

- Issue #189 / ADR 0038 adds separate deterministic equity-path evidence and exact maximum
  drawdown, with an initial funding anchor, post-market-root valuation and commission at Fill
  commitment. New Web v3 jobs atomically publish a bounded 2048-point curve beside unchanged
  BacktestReportV1. Run detail, Batch sorting, Comparison and Holdout read persisted path evidence;
  v1/v2 jobs remain readable without migration. No strategy or economic authority expands.

- Issue #198 / ADR 0041 adds independent Local StrategyPackageV2 through the existing CLI
  pack/validate/inspect commands. Trusted local Action V2 code enters Scenario V4 without source
  edits to EA, with generic Web controls, Report V2/Path V2, Batch, comparison and frozen Holdout.
  Exact artifact bytes and normalized parameters persist through source deletion and restart.
  Package/SDK V1 and built-in V1/V2 remain compatible. A stateful MA crossover example illustrates
  the single-entry/full-exit contract. #125 is closed not-planned as historical provenance.

- Issue #200 / ADR 0042 adds the bounded-long-round-trips-v1 core route: explicit Scenario V5
  binds max_round_trips (1..256), reuses Action V2 and existing economic owners, and publishes
  Result/Semantic V4 and Report V3 for ordered complete trades and optional open positions.
  Multi-dispatch resume preserves deterministic identities and the funding-plus-fill ledger
  sequence. Package V3 retains Action/SDK V2 while preserving planning identities across local
  action quantities. Path V3 and Web Job V5 expose ordered trades, acknowledged-balance paths,
  Batch, comparison and frozen chronological Holdout. The installed reference MA crossover uses
  captured real OHLCV and retains readable source/holdout evidence after source/CSV deletion.

- Issue #203 admits the existing six-account Fill structure when fractional settlement needs
  both commission and a rounding residual. Legacy V1 reports and equity paths use the existing
  currency settlement arithmetic; zero-position reports and supported resume remain compatible.

- Issue #205 adds read-only Trade Analytics V1 over verified Report V2/V3: closed-trade counts,
  after-fee win/loss/breakeven statistics, payoff ratio and Fill-based holding durations across
  Run Detail, Batch and Comparison. Missing denominators remain unavailable and open positions
  are excluded. See [Trade Analytics V1](trade-analytics-v1.md).

- Issue #207 extends the existing dataset catalog with source-scoped bar/revision counts,
  observed durations and uncovered interval diagnostics. Single-run and batch selectors expose
  safe decoder locations and instrument incompatibility before submission; strict validation,
  input identities and historical evidence remain authoritative.

- Issue #212 / ADR 0044 adds explicit deterministic adverse slippage in basis points to the
  existing next-bar-close policy. Actual Fill prices drive commission, settlement and formal
  reports; persisted Web assumptions, compatible Holdout and supported resume retain the policy.
  Omitted slippage preserves historical identities. Liquidity models remain future work.

- Issue #214 / ADR 0045 adds explicit execution latency in milliseconds, independently of strategy
  entry delay. Matching and fact/history verification use the first eligible bar strictly after
  the original submission-time bound. Combined costs, frozen Web assumptions and supported resume
  retain this policy; source exhaustion preserves expiry without fabricated fills. Liquidity remains future work.

- Issue #208 / ADR 0043 adds explicit research Candidates over verified source and chronological
  Holdout evidence. Immutable exact-evidence fingerprints and human ACCEPTED/REJECTED reasons
  persist under the existing workspace lock; decisions grant no execution permission. PPV-07 /
  ADR 0047 adds read-only accepted identity inspection and guarded offline execution through
  `ea candidate inspect/run`. Fresh artifact, parameter and configuration identity is checked
  before executable package loading; logs and `candidate-binding.json` retain the identity.
  See [Candidate usage](candidate-lifecycle.md). Continuous Paper composition remains PPV-11.

## Incomplete

The v0.2.0 prerelease and bounded local Web research loop remain one deterministic offline
backtest/reporting product, not the broader validation, optimization, paper, or live roadmap. Arbitrary
instruction-level or Web recovery, symbol/data editing, uploads, broader experiment tracking,
automatic optimization, broader performance analytics, remote strategy plugins, paper/live execution,
package-registry publication, and deployment remain unavailable. Phase 1.1/#125 is closed not-planned (historical provenance). Research Foundation adds schema-driven strategies and immutable local artifacts to commission and Chronological Holdout.
Path-aware Research Analysis V1 is delivered. PR #191 records the Strategy Lifecycle architecture
review and Proposed ADR 0039; Candidate runtime is delivered under Issue #208 / ADR 0043. Issue #192 / Accepted ADR 0040
delivers SINGLE_LONG_ROUND_TRIP_V1 under Issue #195: one entry, one full exit and at most two
Orders/Fills through existing offline owners. Action V2, Scenario V4, Result/Semantic V3 and
Report V2 bind per-leg fees, open/closed economics and deterministic two-leg resume. Path V2
uses each acknowledged cash/position balance and the unchanged full-path drawdown algorithm.
Web Job V4 persists the new report/path versions; Run Detail, Batch, Comparison and Holdout use
those verified artifacts, including restart/reopen without external CSV. Existing V1 routes
remain unchanged. New metrics, optimizer, arbitrary multi-trade and Agent Research Tools remain
future work. The next decision follows actual strategy/data research; Candidate evidence and human decisions are delivered under Issue #208. Issue #200 separately authorizes bounded repeated long round trips;
its research integration is delivered. Trade Analytics V1 is delivered under Issue #205; broader research validation remains bounded.

## Blockers

There is no known blocker to the delivered RESET-001 offline T1 product slice.

R9/R10 import-provenance findings are retained as hardening evidence. They do not reach a broker,
external authority, or real-money path and therefore do not block the fixed-input offline slice.
A new finding blocks only if it satisfies all four conditions in WORKFLOW. Unresolved Fill,
ledger, audit, storage, or reconciliation mismatch on the demonstrated path must fail closed and
cannot produce a success result.

Live trading, external order writes, secrets, deployment, and package-registry publication remain
unavailable and require separate explicit Human Product Owner authorization. The authorization
used for `v0.2.0` was limited to the GitHub tag and prerelease assets recorded above.

## Effective ADRs

- [ADR index and immutable history](adr/)
- [ADR 0008 — Deterministic execution and reconciliation](adr/0008-deterministic-execution-and-reconciliation.md)
- [ADR 0020 — Durable audit and lifecycle coordinator](adr/0020-durable-audit-and-lifecycle-coordinator.md)
- [ADR 0022 — Audited ledger and reconciliation integration](adr/0022-audited-ledger-and-reconciliation-integration.md)
- [ADR 0024 — Portfolio snapshot reconciliation frontier](adr/0024-portfolio-snapshot-reconciliation-frontier.md)
- [ADR 0025 — Project control plane and authority precedence](adr/0025-project-control-plane-and-authority-precedence.md)
- [ADR 0027 — Phase 1 offline backtest product boundary](adr/0027-phase1-offline-backtest-product-boundary.md)
- [ADR 0029 — Governance freeze and bounded product delivery](adr/0029-governance-freeze-and-bounded-delivery.md)
- [ADR 0030 — Local Web offline backtest integration](adr/0030-local-web-offline-backtest-integration.md)
- [ADR 0031 — Local Web Research Loop V1](adr/0031-local-web-research-loop-v1.md)
- [ADR 0032 — Web Bounded Experiment Batch V1](adr/0032-web-bounded-experiment-batch-v1.md)
- [ADR 0033 — Web Experiment Analysis V1](adr/0033-web-experiment-analysis-v1.md)
- [ADR 0034 — Chronological Holdout V1](adr/0034-chronological-holdout-v1.md)
- [ADR 0035 — Schema-driven Strategy Contract V1](adr/0035-schema-driven-strategy-contract-v1.md)
- [ADR 0036 — Immutable Local Strategy Package V1](adr/0036-immutable-local-strategy-package-v1.md)
- [ADR 0037 — Registered Local OHLCV Research Input V1](adr/0037-registered-local-ohlcv-research-input-v1.md)
- [ADR 0038 — Equity Path and Maximum Drawdown V1](adr/0038-equity-path-and-maximum-drawdown-v1.md)

- [ADR 0041 — Immutable Local Strategy Action V2 Package V1](adr/0041-immutable-local-strategy-action-v2-package-v1.md) — Accepted; local V2 authoring on existing offline research routes.
- [ADR 0040 — Single Long Round Trip Position Lifecycle](adr/0040-single-long-round-trip-position-lifecycle-v1.md) — Accepted; bounded offline single-round-trip runtime and research integration delivered.

- [ADR 0042 — Bounded Long Round Trips V1](adr/0042-bounded-long-round-trips-v1.md) — Accepted; repeated offline research and versioned evidence.
- [ADR 0044 — Deterministic Adverse Slippage V1](adr/0044-deterministic-adverse-slippage-v1.md) — explicit offline execution cost assumption.
- [ADR 0045 — Deterministic Execution Latency V1](adr/0045-deterministic-execution-latency-v1.md) — explicit minimum post-submission delay.

Proposed architecture (not implemented capabilities):

- [ADR 0039 — Research Candidate Identity](adr/0039-strategy-lifecycle-and-research-candidate-identity-v1.md) — superseded for implementation by ADR 0043.

ADR 0029 preserves ADR 0025 authority precedence while superseding its recursive delivery
machinery and the delivery-process requirements of ADRs 0026 and 0028. Historical ADRs remain
immutable. Merged code, tests, and CI remain the authority for actual behavior.

- [ADR 0043 — Research Candidate Runtime V1](adr/0043-research-candidate-runtime-v1.md) — implements the proposed Candidate identity and decision contract.
- [ADR 0047 — Accepted Candidate Loading V1](adr/0047-accepted-candidate-loading-v1.md) — guards frozen artifact/configuration loading and propagates accepted identity.

## Primary Issues

- [#158 — Backtest attempt identity and semantic outcome contract](https://github.com/jayjcc8-cloud/ea-quant/issues/158)
  separates attempt identity, reproducibility lineage, and semantic/economic comparison for the
  installed offline path.
- [#154 — RESET-001: restore bounded product delivery and freeze governance expansion](https://github.com/jayjcc8-cloud/ea-quant/issues/154)
  is completed by the governance-freeze change and PR #156's installed offline vertical slice.
- [#81 — Funded deterministic Backtest single-run MVP](https://github.com/jayjcc8-cloud/ea-quant/issues/81)
  delivers strict scenario validation, funding, bounded limits, flat/bounded-long strategies, and
  the installed validate/run surface.
- [#161 — Supported Backtest resume and failure durability](https://github.com/jayjcc8-cloud/ea-quant/issues/161)
  adds same-attempt installed resume across three bounded durable frontiers.
- [#83 — BacktestReportV1](https://github.com/jayjcc8-cloud/ea-quant/issues/83) adds deterministic,
  read-only reports over completed single-run and resumed attempts.
- [#84 — v0.2.0 product acceptance and release gate](https://github.com/jayjcc8-cloud/ea-quant/issues/84)
  records the accepted clean-main product and the completed limited GitHub prerelease path.
- [#125 — Phase 1.1 reconciliation/recovery expansion](https://github.com/jayjcc8-cloud/ea-quant/issues/125)
  is closed not-planned; future concrete needs require a new bounded Issue.
- [#167 — Local real Web backtest loop](https://github.com/jayjcc8-cloud/ea-quant/issues/167)
  adds the bounded browser-to-engine-to-report path without activating Phase 1.1 or Phase 2.
- [#169 — Local Web Research Loop V1](https://github.com/jayjcc8-cloud/ea-quant/issues/169)
  adds bounded parameter replay, normalized input history, and pairwise comparison without
  activating a symbol/data editor, Experiment model, Phase 1.1, or Phase 2.
- [#171 — Web Strategy Parameter Experiment V1](https://github.com/jayjcc8-cloud/ea-quant/issues/171)
  adds one real delayed-entry parameter to the existing bounded-long strategy and carries exactly
  two strategy inputs through backend contract, engine, snapshot, reuse, and comparison.
- [#174 — Web Bounded Experiment Batch V1](https://github.com/jayjcc8-cloud/ea-quant/issues/174)
  groups 2-10 explicit parameter combinations into normal persisted Web jobs with whole-batch
  validation, serial execution, restart recovery, mixed-state detail, and comparison reuse.
- [#176 — Web Experiment Analysis V1](https://github.com/jayjcc8-cloud/ea-quant/issues/176)
  derives sortable and filterable batch analysis from existing jobs, snapshots, and reports while
  preserving currency, missing-report, restart, and human-judgment boundaries.
- R9/R10 Issues #149, #150, #152, #153 and PR #151 retain provenance as superseded hardening
  history after the reset governance PR merges; they are not an active delivery chain.

## Last Confirmed

- Date: **2026-09-12** (Asia/Shanghai).
- The containing merged `main` commit and its CI are the authoritative durable-state checkpoint;
  the released product identity remains the exact commit and wheel recorded above.
- Live capability: unavailable and prohibited.
- GitHub prerelease: `v0.2.0` published; package registry: not published; deployment: not performed.

## Phase Completion Conditions

- [x] Strict local scenario validation performs no economic mutation.
- [x] Initial funding is exactly once, auditable, replay-safe, and conflict-safe.
- [x] Flat and bounded-long paths enforce order, position, cash, and notional constraints.
- [x] Future market payload is rejected at the public strategy execution seam.
- [x] Selected direct failures retain evidence without a success result or terminal.
- [x] Fresh processes use distinct RunIds with equivalent lineage and semantic economics.
- [x] The installed wheel validates and runs user-supplied scenario/data outside a Git checkout.
- [x] Supported interruptions resume the same trusted attempt without duplicate economic effects.
- [x] Completed, failed, conflicting, corrupt, and partial-publication attempts have deterministic
  fail-closed or idempotent behavior.
- [x] Completed attempts produce byte-stable canonical reports without modifying source evidence.
- [x] Report valuation, exact economics, fee/count semantics, and field sources are independently
  testable and available from the installed wheel outside a checkout.
- [x] Quick Start identifies the supported boundary and known limitations.
- [x] An installed candidate wheel and matching Web dist complete the loopback-only real browser
  path through validation, a fresh attempt, formal report, refresh, restart, and artifact download.
- [x] The local Web path replays normalized inputs into fresh attempts, compares two successes and
  a success with risk rejection, persists across restart, and rejects free-text symbol input.
- [x] The local Web path creates a bounded 2-10 member experiment set, executes normal jobs,
  persists grouping and member evidence across restart, and pair-compares any two members.
- [x] Batch analysis displays canonical inputs beside selected formal results, sorts and filters
  the derived read model, preserves missing values and currencies, and rebuilds after restart.

These conditions complete Issues #81, #161, and #83 and the bounded v0.2.0 GitHub prerelease gate
in #84. They do not activate Phase 1.1/#125, further Phase 2 capabilities, publication, or deployment.

## Delivery Metrics

For each product pull request record: Token cost, human review time, review rounds, lead time, and
the runnable capability delivered. The reset target is one primary review, at most one
concentrated repair verification, and a demonstrable installed product capability. Governance
documents, review artifacts, and metadata-only CI runs are costs rather than product output.
