# Roadmap

This document defines Phase boundaries. Current delivery status belongs in [STATUS](STATUS.md);
task detail belongs in Issues; accepted decisions belong in [ADRs](adr/).

## Phase 0 — Engineering Foundation

Purpose: establish the repository, deterministic Python environment, configuration boundary, CI,
testing, ADR process, reproducibility primitives, and safe collaboration rules.

Exit: merged, verified, and tagged `v0.1.0`.

## Phase 1 — Backtest MVP

Purpose: provide one deterministic historical path that composes bounded OHLCV input, strategy
signals, portfolio/risk decisions, execution/matching, ledger/reconciliation, durable audit,
recovery, and a stable result report.

Entry: Phase 0 is released and `main` is healthy.

## Phase 1 Non-goals

- Live trading, broker/exchange writes, credentials, deployment, or production operations.
- Walk-forward research, parameter optimization, sophisticated transaction-cost modelling, or
  production experiment tracking.
- Multiple live adapters, multi-strategy orchestration, or portfolio-scale production scheduling.
- Tool migration or governance automation without repeated evidence of manual failure.

## Phase 1 Exit Criteria

- Historical inputs enforce point-in-time visibility and deterministic identity.
- Strategy, portfolio/risk, matcher, ledger/reconciliation, audit, and recovery compose without
  look-ahead or duplicate economic mutation.
- At least one concrete sample strategy runs through a deterministic end-to-end backtest.
- A result/report adapter emits a versioned golden report for fixed inputs.
- Required ADRs, STATUS, Issues, tests, and CI agree; no critical decision survives only in comments.
- Full verification passes on merged `main`; Human Owner approves and publishes `v0.2.0`.

## Phase 2 Entry Gate

Phase 2 may start only after every Phase 1 Exit Criterion is true and `v0.2.0` exists. Open Phase 1
debt cannot be relabelled as Phase 2 work to bypass this gate.

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
