# ADR 0046: Production Runtime Profile V1

Date: 2026-09-23

## Status

Accepted

## Context

The existing bundle binds the installed wheel, Web assets and dependency requirements to a source
commit. The Web process already owns one persistent workspace and binds only to loopback, but it
has no canonical server supervision or pinned activation contract.

## Decision

Under PPV-02 / Issue #218, add one Linux systemd profile for the unchanged offline research
service. Reuse the existing distribution manifest, wheel, Web CLI and persistent state authorities.
A small operator-owned launcher verifies the explicit commit/manifest and installed package,
then execs the existing Web command as one process. Keep replaceable release artifacts separate
from durable inputs/workspace; configuration and the stable launcher are operator owned.

Systemd supplies boot enablement, on-failure restart with a finite rate limit, clean stop and process
status. Configuration failures do not restart. No job is automatically retried or resumed.
Activation happens with the writer stopped. Application rollback requires established persistent
reader/writer compatibility; unknown compatibility leaves the service stopped without state edits.

This supersedes ADR 0030 only where it excludes a server runtime profile. Loopback/HTTP boundaries,
offline engine semantics, interrupted-job behavior and historical release identities are unchanged.
It does not authorize actual deployment, accounts/permissions changes, public exposure, release,
Paper/Live, brokers or later PPV capabilities.

## Validation

Unit tests cover configuration, artifact integrity and pinned installation. Real installed bundles
outside source exercise HTTP execution, formal report persistence, restart and compatible rollback.
Linux CI runs the actual systemd unit policy, kills its service process, proves bounded restart,
clean stop and boot eligibility. A target VPS and actual reboot remain separately unverified.

## Consequences

The server operator now has one explicit runtime path. The profile is not readiness, monitoring,
backup, general release management, state migration or trading recovery. Operational details,
activation prerequisites and deployment limits are in `docs/production-runtime.md`.
