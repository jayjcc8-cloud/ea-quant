# ADR 0033: Web Experiment Analysis V1

Date: 2026-09-06

## Status

Accepted

## Context

ADR 0032 makes a bounded batch and its member jobs durable, but the batch detail preserves only
creation order. A user can inspect members individually or pair-compare them, yet cannot organize
the existing inputs and formal results enough to identify which two runs deserve closer human
inspection.

The batch remains a relationship and navigation layer. Jobs remain execution truth, canonical
input snapshots remain input truth, and verified `BacktestReportV1` documents remain result truth.
An analysis record, cached metric projection, or browser ranking conclusion would duplicate those
authorities without product need at the 2-10 member bound.

## Decision

Extend only the existing batch detail presentation with a derived Analysis V1:

- every existing member remains one row with its persisted `target_quantity` and
  `entry_delay_bars`, current presentation status, and navigation to the existing run detail;
- a succeeded member with a verified available report displays final equity, net P&L, and total
  return from the existing report contract;
- monetary values retain their settlement currency, and no FX normalization or cross-currency
  result delta is introduced;
- explicit read-time sorting supports member order, the two parameters, and the three displayed
  report fields using exact decimal comparison, original member ordinal as a stable tie-breaker,
  and missing values last in either direction;
- status filtering operates on the existing derived presentation states and defaults to All;
- a small summary counts member states and available verified reports from the current read model;
  and
- exactly two selected rows continue to navigate to the existing pair comparison and its
  currency-safe Parameter Delta and Result Diff behavior.

Default presentation preserves member ordinal rather than declaring a result ranking. Sort,
filter, and checkbox selection are ephemeral browser state and are not restored after reload.
Restart reconstruction uses only the persisted batch relationship, jobs, canonical snapshots,
and verified reports.

This decision creates no new backend endpoint, persistence, metric, report contract, comparison
engine, optimization, recommendation, or analysis truth source.

## Validation

Component tests cover exact ascending and descending parameter/result sorting, stable ties,
missing-last behavior, required mixed-state filters, currency display, unavailable reports, and
pair navigation. Installed-wheel Chromium creates a real mixed three-member batch, sorts and
filters it, opens the existing comparison, restarts the service, and confirms the same analysis
content is reconstructed outside a source checkout.

## Consequences

- Users can move from bounded experiment execution to structured inspection and then to manual
  pairwise judgment without the system naming a best or recommended run.
- A missing or unavailable report never hides a member or fabricates a metric.
- The 2-10 member bound needs no pagination, query engine, cache, analytics database, or saved view.
- Automatic selection, optimization, richer analytics, statistical inference, paper/live trading,
  brokers, deployment, and publication remain outside this product boundary.
