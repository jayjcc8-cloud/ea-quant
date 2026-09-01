# Project Status

## Current Phase

**Phase 1 delivery reset — offline simulated vertical slice: Incomplete / main healthy / live unavailable.**

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
- The Dark Professional Web shell remains a deterministic read-only Mock Adapter.

## Incomplete

- Issue #154 / RESET-001 first lands the governance freeze and bounded delivery rules in ADR 0029.
- The same Issue then delivers one thin installed product composition over existing components,
  with deterministic accepted, rejected, and reconciliation-failure evidence.
- Quick Start and installed-wheel acceptance must describe and prove the runnable command.

The broader v0.2.0 scenario schema, funding/manifest expansion, resume matrix, sample-strategy
catalogue, and full reporting surface remain future product work. They are not silently claimed by
the reset slice.

## Blockers

There is no known blocker to the offline T1 product slice.

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

- [#154 — RESET-001: restore bounded product delivery and freeze governance expansion](https://github.com/jayjcc8-cloud/ea-quant/issues/154)
- R9/R10 Issues #149, #150, #152, #153 and PR #151 retain provenance as superseded hardening
  history after the reset governance PR merges; they are not an active delivery chain.
- Phase 1.1 owns automatic reconciliation correction, ancestry repair, and broader recovery work.

## Last Confirmed

- Date: **2026-09-01** (Asia/Shanghai).
- Verified pre-reset baseline: `main@969f5f086acd9c28f80e3960a56b83981b1d0e5d`.
- Current write authority: Issue #154, one isolated writer/worktree per PR.
- Live capability: unavailable and prohibited.
- Release/tag/publication authority: not granted.

## Phase Completion Conditions

- [ ] ADR 0029 and the governance freeze are merged with CI green.
- [ ] One installed command runs outside a Git checkout from a built wheel.
- [ ] A fixed deterministic input traverses signal, risk, order, Fill, cash, position,
  reconciliation, and audit.
- [ ] A risk-rejected input creates no economic mutation.
- [ ] A reconciliation mismatch fails without a success result.
- [ ] One primary review, any single concentrated repair verification, and required CI are complete.
- [ ] Quick Start identifies the supported boundary and known limitations.

These conditions complete RESET-001, not the full Phase 1 roadmap or a v0.2.0 release.

## Delivery Metrics

For each product pull request record: Token cost, human review time, review rounds, lead time, and
the runnable capability delivered. The reset target is one primary review, at most one
concentrated repair verification, and a demonstrable installed product capability. Governance
documents, review artifacts, and metadata-only CI runs are costs rather than product output.
