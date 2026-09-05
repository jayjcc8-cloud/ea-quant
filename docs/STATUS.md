# Project Status

## Current Phase

**Phase 1 delivery reset — RESET-001 and Backtest Identity contract delivered / main healthy /
live unavailable.**

When this file is read from merged `main`, the containing `main` commit and its CI are the
authoritative checkpoint. GitHub Issues and pull requests carry mutable coordination state; this
file records only durable product state and the next bounded outcome.

## Phase Objective

From an installed wheel and outside a Git checkout, run one deterministic offline historical
simulation command that demonstrates the existing market-data, strategy, risk, order, Fill,
ledger, reconciliation, audit, and reporting path. No broker, network, secret, external write, or
live capability is part of this objective.

## Completed

- Phase 0 engineering foundation is released as `v0.1.0`.
- `main@969f5f086acd9c28f80e3960a56b83981b1d0e5d` is the verified pre-reset baseline.
- Merged code already provides deterministic time/data handling, strategy interfaces, portfolio
  and risk controls, matching, execution facts, ledger, reconciliation, audit, and recovery
  components.
- ADR 0027 fixes the offline-only product and fail-closed boundary.
- ADR 0029 freezes governance expansion and restores bounded T1 product delivery.
- The installed `ea backtest run --output <path>` command runs outside a Git checkout from a
  built wheel and produces deterministic report and audit artifacts.
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
- The Dark Professional Web shell remains a deterministic read-only Mock Adapter.

## Incomplete

RESET-001 is a thin offline demonstration, not the funded Backtest MVP. The broader v0.2.0
scenario schema, initial funding, stochastic seed hierarchy, resume/recovery matrix,
sample-strategy catalogue, and full reporting surface remain future product work. They are not
silently claimed by this slice or the Backtest Identity contract.

The next authorized candidate is #81, the funded deterministic Backtest single-run MVP. It still
requires fresh explicit Product Owner authorization; no #81 implementation is active under this
status update.

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
  follows the Backtest Identity contract and owns scenario, funding, limits, strategies, validate,
  and run.
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
- The containing merged `main` commit and its CI are the authoritative RESET-001 checkpoint.
- Live capability: unavailable and prohibited.
- Release/tag/publication authority: not granted.

## Phase Completion Conditions

- [x] ADR 0029 and the governance freeze are merged with CI green.
- [x] One installed command runs outside a Git checkout from a built wheel.
- [x] A fixed deterministic input traverses signal, risk, order, Fill, cash, position,
  reconciliation, and audit.
- [x] A risk-rejected input creates no economic mutation.
- [x] A reconciliation mismatch fails without a success terminal or success report.
- [x] One primary review, one concentrated repair verification, and required CI are complete.
- [x] Quick Start identifies the supported boundary and known limitations.

These conditions complete RESET-001, not the full Phase 1 roadmap or a v0.2.0 release.

## Delivery Metrics

For each product pull request record: Token cost, human review time, review rounds, lead time, and
the runnable capability delivered. The reset target is one primary review, at most one
concentrated repair verification, and a demonstrable installed product capability. Governance
documents, review artifacts, and metadata-only CI runs are costs rather than product output.
