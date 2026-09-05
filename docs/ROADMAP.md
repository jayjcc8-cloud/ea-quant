# Roadmap

This document defines Phase boundaries. Current delivery status belongs in [STATUS](STATUS.md);
task detail belongs in Issues; accepted decisions belong in [ADRs](adr/).

## Phase 0 — Engineering Foundation

Purpose: establish the repository, deterministic Python environment, configuration boundary, CI,
testing, ADR process, reproducibility primitives, and safe collaboration rules.

Exit: merged, verified, and tagged `v0.1.0`.

## Phase 1 — Backtest MVP

Purpose: deliver one mature offline backtest product that composes bounded OHLCV input, strict
scenario validation, initial funding, strategy signals, portfolio/risk decisions,
execution/matching, ledger reconciliation, durable audit, supported recovery, and a stable result
report from an installed distribution.

Entry: Phase 0 is released and `main` is healthy.

## Phase 1 Delivery Order

1. **RESET offline vertical slice — COMPLETE.** The installed deterministic demonstration proves
   the bounded signal-to-report path without claiming the funded Backtest MVP.
2. **Backtest Identity contract — COMPLETE.** Run/attempt identity, lineage, semantic comparison,
   and seed ownership are defined for the accepted product surface.
3. **Funded deterministic Backtest single-run — #81 — COMPLETE.** Strict scenario input, initial
   funding, limits, strategies, and installed `validate`/`run` commands are delivered.
4. **Resume and failure durability — COMPLETE.** The installed product resumes the same trusted
   attempt from the funded, completed-dispatch, and reconciled pre-publication durable frontiers,
   preserving exactly-once economics and failing closed on ambiguous or conflicting evidence.
5. **BacktestReportV1 — #83 — COMPLETE.** Canonical JSON/text and semantic outcome comparison are
   delivered from accepted product evidence.
6. **v0.2.0 product acceptance and release gate — #84 — COMPLETE.** The Product Owner accepted one
   exact clean `main` product and published its fixed tag, wheel, and checksum as a GitHub
   prerelease. No package-registry publication or deployment was performed.

This is a product dependency order, not a sequence of governance phases or actor chains.

## Phase 1 Non-goals

- Live trading, broker/exchange writes, credentials, deployment, or production operations.
- Walk-forward research, parameter optimization, sophisticated transaction-cost modelling, or
  production experiment tracking.
- Multiple live adapters, multi-strategy orchestration, or portfolio-scale production scheduling.
- Tool migration or governance automation without repeated evidence of manual failure.
- Automatic reconciliation correction, balance adjustment, ancestry repair, or exhaustive recovery
  across dormant correction combinations.
- Initial positions, multiple settlement currencies, real Web/runtime integration, or research-grade
  performance analytics.

## Phase 1 Exit Criteria

- Historical inputs enforce point-in-time visibility and deterministic identity.
- `RunManifest v2` binds installed provenance, one strict scenario, one instrument specification,
  one settlement currency, initial cash, data, strategy, limits, and seed lineage.
- `ea backtest validate`, `run`, and `resume` execute from an installed wheel outside a Git checkout.
- `always-flat-v1` and `bounded-long-v1` run through portfolio/risk, matcher, ledger, audit, and a
  successful terminal path without look-ahead or duplicate economic mutation.
- Supported interruption recovery is economically and audit-equivalent to uninterrupted execution.
- Reconciliation, ledger, journal, manifest, or scenario conflicts detect, audit, report, and stop;
  they never produce a successful terminal state or success report.
- `BacktestReportV1` emits canonical JSON and text; exact-run regeneration is byte-identical and
  independent runs expose a stable semantic outcome digest.
- Required ADRs, STATUS, Issues, tests, and CI agree; no critical decision survives only in comments.
- Full product acceptance passes on merged `main`; the Human Product Owner has published the fixed
  accepted artifact as the `v0.2.0` GitHub prerelease while retaining the package-registry and
  deployment prohibitions.

## Phase 1.1 — Reconciliation and Recovery Expansion

Phase 1.1 may begin after the offline v0.2.0 product is accepted. It owns the superseded
#76/#97/#98/#99 product scope: explicit reconciliation adjustment authorization, ancestry repair,
additional observation/fact combinations, and broader recovery matrices. It may not weaken the
Phase 1 fail-closed product boundary or expose live/external writes. It remains unactivated and
requires a separate Product Owner decision.

## Phase 2 Entry Gate

Phase 2 may start only after every Phase 1 Exit Criterion is true and `v0.2.0` exists. Open Phase 1
debt cannot be relabelled as Phase 2 work to bypass this gate.

The existence of the `v0.2.0` prerelease does not itself activate Phase 2; no Phase 2 work is
authorized by the release decision.

Phase 2 adds serious backtest research capabilities: realistic fees/slippage/latency, out-of-sample
validation, walk-forward evaluation, experiment tracking, and richer data/version evidence.

## Phase 3 — Paper Trading

Entry: Phase 2 evidence demonstrates credible deterministic research semantics. Purpose: connect
real or simulated market feeds while reusing OMS, risk, reconciliation, and monitoring boundaries;
risk bypass remains impossible.

## Phase 4 — Small Live Trading

Entry: paper operation is stable and explicitly approved. Purpose: one broker/exchange, low
frequency, small capital, strict limits, kill switch, reconciliation, secret isolation, and
operational runbooks.

## Phase 5 — Small-scale Production

Entry: small live operation has durable recovery and operational evidence. Purpose: multi-strategy
and multi-environment operation, monitoring/alerts, periodic reports, backup, and deployment
discipline without weakening safety contracts.
