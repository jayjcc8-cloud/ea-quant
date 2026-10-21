# Roadmap

This document defines phase boundaries, not a second progress register. Current capabilities,
remaining acceptance and the next bounded outcome belong in [STATUS](STATUS.md); tasks belong in
Issues and architectural decisions in [ADRs](adr/).

## Current delivery direction

[ADR 0050](adr/0050-desktop-only-product-delivery.md) makes the Mac desktop QUINE's sole product
entry and release target. Shared React/WebView, HTTP, internal browser checks and historical server
compatibility remain. The current objective is convergence of the existing research candidate,
not an independent browser/server/mobile product, IPC migration or replacement research engine.

The commercial objective is reducing Idea → Evidence-ready Candidate time and cost. The existing
boundary is research → frozen evidence → human Candidate decision → separately confirmed local
simulation. A later phase name never authorizes external connectivity or Live.

## Phase 0 — Engineering Foundation

Purpose: establish the repository, locked Python environment, configuration, CI, tests,
reproducibility primitives and collaboration rules.

Exit: merged, verified and tagged `v0.1.0`. Delivery history remains in Git and the release record;
the original bootstrap implementation checklist is not an active development backlog.

## Phase 1 — Backtest MVP

Purpose: one installed offline backtest product composing bounded OHLCV input, strict scenario
validation, initial funding, strategy/portfolio/risk decisions, execution facts, ledger
reconciliation, durable audit, supported recovery and formal reports.

Entry: released Phase 0 and a healthy merged main. Exit criteria:

- Point-in-time input visibility and deterministic run/attempt identities.
- Installed `ea backtest validate|run|resume` outside a source checkout.
- Replay-safe funding and supported interruption recovery without duplicate economic effects.
- Bounded risk and fail-closed ledger, journal, manifest, scenario and reconciliation conflicts.
- Byte-stable formal reports and equivalent independent semantic/economic outcomes.
- Required acceptance, CI and review, followed by the explicitly authorized `v0.2.0` prerelease.

This historical exit is complete. The release does not authorize later phases automatically.
Broker/exchange writes, real funds, external credentials, production deployment, automatic
reconciliation correction and arbitrary instruction-level recovery were outside that slice.

## Phase 1.1 — Reconciliation and Recovery Expansion

The former expansion under #125 is closed not-planned. Broader adjustment/ancestry repair and
recovery matrices need a new bounded Issue and explicit owner priority; old unchecked plans do
not reactivate them. Preserve the existing fail-closed authorities.

## Phase 2 — Research and Validation

Phase 2 concerns richer data/version evidence, execution assumptions, out-of-sample evaluation,
experiment analysis and authoring assistance. The `v0.2.0` prerelease alone authorizes none of it;
bounded Issues/ADRs have since delivered specific capabilities listed in [STATUS](STATUS.md).
Do not reopen completed strategy packages, data registration, Candidate, trade analytics or AI
integration as future prerequisites merely because an older roadmap listed them that way.

Further research work needs a concrete user outcome and acceptance criteria. Walk-forward
analysis, general optimization, broader strategy semantics and an untrusted-code host are not
implied by the current bounded research loop. A small dataset or passing example demonstrates
workflow, not generalization, robustness or profitability.

## Phase 3 — Paper Trading

Purpose: operate simulated or external feeds while reusing order, risk, reconciliation and
monitoring boundaries; risk bypass remains impossible. Mac-local simulated Paper is already
available under [ADR 0049](adr/0049-explicit-local-paper-profile.md). This does not establish an
external Paper provider or account connection.

[Personal Production V1](personal-production-v1.md) owns the existing maturity-gate definitions.
PPV identifiers name capabilities, not execution order. RC freeze and the 72-hour/7-day soak remain
separate acceptance work; VPS is not a desktop development prerequisite. Follow the existing
operator tools and preserve running services and evidence.

## Phase 4 — Small Live Trading

Entry requires stable, evidenced Paper operation and explicit Human Product Owner authorization.
Scope would be one broker/exchange, low frequency, small capital, strict limits, kill switch,
reconciliation, secret isolation and operational runbooks. Live remains unavailable; this roadmap
is not authorization to implement or activate it.

## Phase 5 — Small-scale Production

Entry requires durable small-live recovery and operational evidence. Scope may include multiple
strategies/environments, monitoring, backup and deployment discipline without weakening safety.
Each expansion requires a bounded product decision; no automated promotion or release is implied.
