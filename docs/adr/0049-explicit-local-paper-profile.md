# ADR 0049: Explicit local simulated Paper profile

- Status: Accepted
- Date: 2026-09-27
- Related: Issue #232; ADRs 0003, 0020, 0022, 0027, 0043, 0047, 0048
- Supersedes: ADR 0027 only for the explicitly invoked local simulated Paper profile below

## Context

The Product Owner authorized PPV-07 through PPV-11 on Mac, using one accepted Candidate, one
simulated account/instrument, a generated local feed and the local PaperBroker. Candidate
acceptance remains research evidence admission. Historical execution and a `paper` configuration
enum do not themselves provide a continuous Paper process or grant execution permission.

## Decision

Expose `ea paper start`, `status` and `stop`. Start is the separate explicit local simulation
operation. It admits fresh accepted Candidate evidence and its frozen configuration/package
bytes before executing the strategy. The minimum strategy profile uses the existing Action V2
long-only lifecycle; unsupported profiles reject before package execution. Live and unsupported
global configuration overrides reject without reading account credentials or connecting to a
provider. Existing offline Candidate documents retain their historical scope and identities.

Compose PPV-10 incremental market dispatch with the existing StrategySignalAuthority, portfolio
planner, Risk and Order authorities, PPV-08 command tracker, PPV-09 PaperBroker, execution-fact
authority, ledger, reconciliation and POSIX audit journal. No second Fill, balance, risk-policy
or economic engine is introduced. A fixed configured price cycle generates one current Bar per
real poll; it does not replay a historical dataset. UTC visibility/event age and monotonic process
time retain their distinct roles. Finite source exhaustion is a failure reason, never healthy
market progress.

The composition owns two mandatory effect boundaries:

1. Before broker submission, require the exact active market proof, current portfolio/risk
   versions, no halt or stop, fresh data and current cash/notional admission. Use the configured
   simulation price bound with existing slippage, settlement and commission arithmetic. Append
   and verify the exact local-Paper pre-effect audit acknowledgement, then recheck freshness and
   stop immediately before transport. An unknown or burned attempt never authorizes resend.
2. For source-issued, visible execution facts, durably acknowledge the exact ingress and existing
   processing outcome before the existing audited handoff reaches the ledger. Acknowledge the
   ledger outcome and portfolio/risk publication before another market decision. Duplicate facts
   retain evidence without a second Fill or cash/position effect. Audit failure cannot be hidden
   by a success status or replacement balance.

Add truthful Paper audit payloads where historical matcher/request carriers cannot represent
this path. Preserve all existing canonical identity domains, historical schemas and golden
evidence. Use the existing terminal journal boundary with a distinct Paper payload when required.

Continuous dispatch must not grow an unbounded in-memory refresh history. The Paper composition
uses bounded latest-refresh retention in the same portfolio/risk refresh owner; historical
construction keeps its existing replay history. Current exact retries and strictly increasing,
contiguous dispatch publication remain enforced; old evicted evidence cannot be used to resume.
Orders and strategy round trips remain bounded by the accepted configuration. Existing journal
capacity limits fail closed rather than being weakened.

Each start creates a fresh exclusively owned attempt. Status is an observational projection of
acknowledged economics, with valuation price/time, correlated Candidate/account/order/Fill IDs
and process state. Stop closes new market decisions, drains already issued facts within a bound
and releases the source, audit/store lease and process. It does not promise liquidation. Retain
open positions and any incomplete fact/order state truthfully. Unknown or previously used
attempts preserve evidence and reject restart; no automatic recovery or order resend is added.

Local reconciliation compares the existing ledger with funding and retained Fill evidence. It
does not claim an external broker balance observation. Installed acceptance must additionally
use independently calculated amounts, not only compare two outputs of the implementation.

## Boundaries and acceptance

Historical backtest/report/recovery behavior remains unchanged. External broker or third-party
paper accounts, credentials, Live, VPS deployment, release/tag/publication, recovery, long soak
and production readiness remain outside this decision.

Issue #232 requires a fixed-code, non-editable installation outside Git on Mac; fresh source,
Holdout and accepted Candidate evidence; actual continuous strategy/risk/order/Fill/ledger
round trips; observable money/positions and correlated logs; duplicate protection; one controlled
failure; and user stop with no subsequent orders and released resources. Record the finite real
run interval and exact artifact. A passed local interval is not a long-duration stability claim.
