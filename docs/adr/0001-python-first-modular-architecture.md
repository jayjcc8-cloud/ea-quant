# ADR 0001: Python-first Modular Monorepo

Date: 2026-07-11

## Status

Accepted

## Context

The project is a long-running quantitative trading system. It needs research velocity, reproducibility, risk controls, and GitHub-based engineering workflow from day one.

Mature open-source systems provide useful references:

- LEAN for modular algorithm boundaries.
- NautilusTrader for shared research/simulation/live event semantics.
- vn.py for gateway and event-engine patterns.
- Freqtrade for dry-run/live bot operations.
- vectorbt for research-stage parameter scanning.

Directly adopting a single framework would create early lock-in before the target market, broker, latency requirement, and operational constraints are validated.

## Decision

Use a Python-first modular monorepo under `src/ea`.

The first implementation should define a thin internal domain model and stable interfaces for:

- data providers
- strategy
- portfolio construction
- risk model
- execution adapter
- broker adapter
- metrics/reporting

Third-party frameworks may be integrated later behind adapters. They should not dictate the core domain model in Phase 0.

## Consequences

Positive:

- Fast research and prototyping.
- Simple onboarding and GitHub CI.
- Clear seams for future replacement.
- Lower initial complexity than adopting LEAN or NautilusTrader wholesale.

Negative:

- We must implement and test a minimal event/backtest/execution model ourselves.
- We must avoid slowly rebuilding a poor clone of mature systems.
- Production live trading will require stricter validation before any real capital is used.

## Validation

This decision should be revisited after Phase 1 and Phase 3:

- Phase 1: can the MVP backtester produce deterministic, audited results?
- Phase 3: can paper trading reuse the same strategy and risk semantics without special-case branches?

