# ADR 0030: Local Web Offline Backtest Integration

Date: 2026-09-05

## Status

Accepted

## Context

ADR 0027 confined the v0.2.0 product to installed offline CLI commands and retained the Web shell
as a deterministic Mock Adapter. The released engine now has strict scenario validation, fresh
attempt execution, durable reports, and installed-wheel evidence. A local user still has to return
to the terminal for every operation and cannot complete that existing path through a real browser.

## Decision

Permit one post-v0.2.0, local-only integration:

- an HTTP service fixed to `127.0.0.1`, with explicit scenario, workspace, and built-UI roots;
- one active offline job, a persistent application index, and idempotent request IDs;
- React pages that validate a registered scenario, create a fresh attempt through the existing
  product functions, display the formal `BacktestReportV1`, refresh saved jobs, and download only
  `report.json` or `summary.txt`;
- exact Host and same-origin write checks, JSON plus a custom request header, bounded identifiers,
  root containment, and no CORS relaxation; and
- optional Web dependencies, so the core wheel remains usable without the Web extra.

The Web index is not an economic ledger or recovery authority. It records only job-to-attempt and
report identities. Reads never run, resume, repair, or regenerate an attempt. A service restart
marks an unconfirmed active job interrupted and does not retry it.

This decision supersedes ADR 0027 only where its capability confinement prohibits an API service
and real Web adapter. All v0.2.0 historical claims remain unchanged. The accepted identity,
economic, risk, audit, reconciliation, recovery, and report validation semantics remain unchanged.

## Validation

Acceptance requires Playwright Chromium to drive the production React build over actual loopback
HTTP into a non-editable, repository-outside candidate-wheel installation. The path must create a
new engine attempt and formal report, preserve exact decimal strings, survive browser refresh and
service restart for completed jobs, download the generated artifacts, reject invalid input and
browser-boundary violations, and show business failure without a synthetic success report.

## Consequences

- Users gain one real local browser-to-engine offline loop without a deployment or server product.
- The service cannot bind a LAN or public interface and cannot accept arbitrary filesystem paths
  from the browser.
- Phase 1.1/#125, Phase 2, paper/live trading, brokers, external orders, credentials, deployment,
  package-registry publication, tags, and Releases remain inactive and unavailable.
- Broader research UI, uploads, scenario editing, cancellation, automatic resume, multiple active
  jobs, authentication, and distributed scheduling require separate product decisions.
