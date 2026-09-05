# Project Status

## Current Phase

**Phase 1 delivery reset — funded deterministic Backtest run/resume delivered / live
unavailable.**

When this file is read from merged `main`, the containing `main` commit and its CI are the
authoritative checkpoint. GitHub Issues and pull requests carry mutable coordination state; this
file records only durable product state and the next bounded outcome.

## Phase Objective

From an installed wheel and outside a Git checkout, validate and run one user-supplied strict
scenario with local OHLCV, deterministic initial funding, bounded risk, simulated execution,
ledger state, reconciliation, audit, and semantic result evidence. No broker, network, secret,
external write, or live capability is part of this objective.

## Completed

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
- The Dark Professional Web shell remains a deterministic read-only Mock Adapter.

## Incomplete

The funded product is one deterministic single-run MVP, not all of Phase 1 or a v0.2.0 release.
Arbitrary instruction-level recovery, BacktestReportV1, performance analytics, stochastic seed
hierarchies, strategy plugins, paper/live execution, and release publication remain unavailable.
BacktestReportV1 is next in the dependency order but is not authorized by this status update.

## Blockers

There is no known blocker to the delivered RESET-001 offline T1 product slice.

R9/R10 import-provenance findings are retained as hardening evidence. They do not reach a broker,
external authority, or real-money path and therefore do not block the fixed-input offline slice.
A new finding blocks only if it satisfies all four conditions in WORKFLOW. Unresolved Fill,
ledger, audit, storage, or reconciliation mismatch on the demonstrated path must fail closed and
cannot produce a success result.

Live trading, external order writes, secrets, deployment, tag, release, and publication remain
unavailable and require explicit Human Product Owner authorization.

## Effective ADRs

- [ADR index and immutable history](adr/)
- [ADR 0008 — Deterministic execution and reconciliation](adr/0008-deterministic-execution-and-reconciliation.md)
- [ADR 0020 — Durable audit and lifecycle coordinator](adr/0020-durable-audit-and-lifecycle-coordinator.md)
- [ADR 0022 — Audited ledger and reconciliation integration](adr/0022-audited-ledger-and-reconciliation-integration.md)
- [ADR 0024 — Portfolio snapshot reconciliation frontier](adr/0024-portfolio-snapshot-reconciliation-frontier.md)
- [ADR 0025 — Project control plane and authority precedence](adr/0025-project-control-plane-and-authority-precedence.md)
- [ADR 0027 — Phase 1 offline backtest product boundary](adr/0027-phase1-offline-backtest-product-boundary.md)
- [ADR 0029 — Governance freeze and bounded product delivery](adr/0029-governance-freeze-and-bounded-delivery.md)

ADR 0029 preserves ADR 0025 authority precedence while superseding its recursive delivery
machinery and the delivery-process requirements of ADRs 0026 and 0028. Historical ADRs remain
immutable. Merged code, tests, and CI remain the authority for actual behavior.

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
- [#83 — BacktestReportV1](https://github.com/jayjcc8-cloud/ea-quant/issues/83) follows the
  single-run and resume/failure-durability deliveries.
- [#84 — v0.2.0 product acceptance and release gate](https://github.com/jayjcc8-cloud/ea-quant/issues/84)
  owns final clean-main acceptance; tag/release/publication still requires a separate Human Product
  Owner decision.
- [#125 — Phase 1.1 reconciliation/recovery expansion](https://github.com/jayjcc8-cloud/ea-quant/issues/125)
  remains parked and does not block the Offline Backtest MVP or v0.2.0 unless explicitly promoted.
- R9/R10 Issues #149, #150, #152, #153 and PR #151 retain provenance as superseded hardening
  history after the reset governance PR merges; they are not an active delivery chain.

## Last Confirmed

- Date: **2026-09-05** (Asia/Shanghai).
- The containing merged `main` commit and its CI are the authoritative funded single-run
  checkpoint.
- Live capability: unavailable and prohibited.
- Release/tag/publication authority: not granted.

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
- [x] Quick Start identifies the supported boundary and known limitations.

These conditions complete Issues #81 and #161 only, not the full Phase 1 roadmap or a v0.2.0
release.

## Delivery Metrics

For each product pull request record: Token cost, human review time, review rounds, lead time, and
the runnable capability delivered. The reset target is one primary review, at most one
concentrated repair verification, and a demonstrable installed product capability. Governance
documents, review artifacts, and metadata-only CI runs are costs rather than product output.
