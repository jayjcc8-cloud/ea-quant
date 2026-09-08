# ADR 0035: Schema-driven Strategy Contract V1

Date: 2026-09-08

## Status

Accepted

## Decision

Research Foundation introduces StrategyDescriptorV1 and a closed distribution-only
StrategyRegistryV1. Descriptors bind schema version, strategy ID/version, display name,
research visibility and ordered integer/decimal parameter definitions with required/default
and static bounds. Loaded data and instrument context resolve dynamic constraints in the
backend; the Web consumes those constraints without strategy-specific branches.

BacktestScenarioV2 binds a closed normalized parameter map and strategy ID/version using
independent canonicalization and digest domain. Decimal strings use ea-decimal-v1; runtime
types, unknown/missing keys and duplicate YAML keys reject. Canonical keys are sorted.
V1 serialization and identities remain unchanged; historical inputs and relationships are
never migrated. Read adapters may derive generic parameters from V1 evidence.

Built-ins are bounded-long-v1, always-flat-v1 (not research-visible), and
moving-average-entry-v1. The latter accumulates only admitted active market closes and
enters once when fast SMA exceeds slow SMA after sufficient history. Only admitted raw
revision-0 bars increment its window; revisions are ignored rather than counted twice.
Signals are issued only while a subsequent executable bar exists. Parameters require
1 <= fast_window < slow_window, slow_window >= 2 and positive quantized target_quantity.
Data must offer sufficient history and a subsequent executable bar. No window shortening,
future price access, wall clock, files, network or randomness belongs to strategy logic.

Execution adapters decide entry through the existing active-market verifier and signal
issuer. Risk, portfolio planning, order authorization, matching, Fill, ledger, audit,
reconciliation and report authorities remain unchanged.

Single-run, immutable history reuse, explicit 2..10-member batch duplicate checks and
same-strategy/version parameter deltas consume the entire canonical parameter map.
Different strategies have no parameter delta. ADR 0034 relationship-only authority remains:
jobs are execution truth, snapshots input truth, verified reports result truth. Holdout
freezes every source parameter and requires matching strategy ID/version; target defaults
cannot override it and target dynamic validation reruns without clamping. Historical
ChronologicalHoldoutV1 bytes and referenced jobs/reports remain unchanged.

## Scope and validation

Two distinct real strategies must traverse installed-wheel Chromium research through
history/reuse, batch/analysis, manually selected holdout and restart/reopen. Tests preserve
V1 identity and economics, reject invalid contracts/parameters, verify deterministic state
and enforce existing economic authority. Independent review searches for strategy-specific
product orchestration. No AI, external packages/plugins, dataset registry, optimizer,
walk-forward, new analytics, paper/live, deployment, governance or recovery frontier.

Future direction only: Research Foundation → Local Strategy Package → Dataset Registry
→ Candidate → Agent Research Tools.
