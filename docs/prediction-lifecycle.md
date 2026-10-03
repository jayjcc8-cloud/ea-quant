# Offline binary claim lifecycle (C2)

This separate local entry demonstrates non-OHLCV resolution and gross payout.
It does not integrate Polymarket, a broker, an account, a wallet or a chain.
No strategy is generated or invoked, no ordinary sell/Fill represents settlement,
and existing price scenarios/economic owners are unchanged.

## Source and time boundary

`examples/prediction/historical-resolved.json` describes the actual Donald Trump
YES/NO child contract in the 2024 presidential-election event. The official
[historical event page](https://polymarket.com/event/presidential-election-winner-2024/will-donald-trump-win-the-2024-us-presidential-election?tid=1724420032971)
was read without authentication on 2026-10-03. It displays Trump as Yes and says
that the event is resolved. Its indexed rule identifies agreement of AP, Fox News
and NBC, with inauguration as fallback if they have not agreed by 2025-01-20.
The local fixture consumes the venue's published final judgment; it does not
independently adjudicate those news sources. A probability/price alone is never
converted to a judgment by code.

No exact historical resolution/first-publication timestamp was verified. The
fixture conservatively binds `rules_observed_at` and result `available_at` to
2026-10-03T11:10:08Z, after the source reads. Before this bound a fresh journal
returns pending/not_visible with zero settlement and no result projection.
This is a current capture of a historical result, **not** proof that EA knew it
on election day. An already advanced journal rejects earlier queries; use a
fresh journal for a retrospective boundary demonstration.

The published $1/$0 payoff is modeled in `USD_SIM`: simulated dollar units,
face value 1 per winning claim, losing claim 0, quantum 0.01. This is neither
real USD cash nor a claim about current or historical collateral-token identity.
Ten pre-endowed YES claims start with local cash zero and settle to gross cash
10. No acquisition, price history, cost, fee, investment return or P&L is modeled.

`synthetic-void.json` is an explicitly invented cancelled-event contract.
Its named synthetic authority voids both sides with fixed 0.4 USD_SIM per claim:
ten claims settle to 4. This is a contractual cancellation payout, not an
assertion about an actual venue void or refunding a historical purchase price.

## Local use

Use existing dependencies with the clone's `src` on PYTHONPATH:

```sh
PYTHONPATH=src python -m ea.product.prediction \
  --fixture examples/prediction/historical-resolved.json \
  --journal /tmp/ea-c2-historical.json --as-of 2024-11-05T00:00:00Z
PYTHONPATH=src python -m ea.product.prediction \
  --fixture examples/prediction/historical-resolved.json \
  --journal /tmp/ea-c2-historical.json --as-of 2026-10-03T11:10:08Z
```

Repeat the second command: `duplicate_noop`, one settlement, cash 10, zero open
claims. Repeat in a fresh process to demonstrate restart/replay. For cancellation,
select the synthetic fixture and a separate journal at the capture bound.
`observation: null` means missing judgment and only reports current state.

Pending/disputed/unknown observations have no payouts and never report success.
Only source/authority/contract-bound explicit resolved or void judgments retire
claims and create balanced cash/settlement-clearing postings. Resolved zero-payout
losers are valid settled claims; void records payout but `success=false`, never a
winning judgment. Contradictory terminal revisions are rejected without mutation;
manual adjudication is outside this entry. Same terminal judgment is a no-op,
including after restart and through stale in-memory instances.

`CanonicalDecimal`, currency quantization, `settle_product`, and UTC validation
are reused. PortfolioLedger's closed Fill/funding/adjustment authority has no
resolution input, so this small settlement journal owns only this offline domain.
A locked complete document atomically records judgment and its settlement inputs;
report replay revalidates them and derives balances and postings. No separately
stored balance can double a payout. Publication exceptions never return success.
POSIX file locking covers reload/replay/publish, and conflicting bootstrap or
invalid/corrupt/duplicate-key input fails closed. At most 256 observations and
1 MiB local documents; no plugin lifecycle framework.

Evidence is trusted, curated local input bound to its named source. This is not
cryptographic source authentication or automated venue resolution ingestion.
Whole-file malicious rewrites are outside the local trust model; validation is
semantic, not tamper-proof chain certification. Installed wheel/central CLI/Web
integration, real model product E2E, real-money accounting, multi-contract portfolios,
correction/reversal mechanics and external settlement remain unverified/unavailable.
