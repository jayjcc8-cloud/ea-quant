# Trade Analytics V1

Issue #205 adds a read-only projection of existing verified Report V2/V3 evidence. Run detail,
batch analysis and pair comparison share `GET /api/backtests/{job_id}/trade-analytics`.
No analytics record is stored, and no strategy runs when analytics is read. The response binds
its source report SHA-256 and engine RunId. Legacy Report V1 stays readable with complete-trade
analytics unavailable. Missing, failed or corrupted reports do not produce statistics.

Only completed entry/exit pairs contribute. Winning, losing and breakeven classification uses
settled realized P&L after both Fill fees. An open position is shown separately and excluded.
Win rate divides wins by all closed trades, including breakeven trades. Average win is the mean
of positive realized P&L; average loss is the signed mean of negative realized P&L. Payoff ratio
is average win divided by the absolute average loss. An absent denominator or population is
`null`, shown as Unavailable rather than zero or infinity.

Holding duration is exit Fill time minus entry Fill time, in seconds with microsecond inputs.
It does not use order submission or wall-clock processing time. Negative or timezone-free
holding intervals reject. Averages and ratios use decimal arithmetic, 18 decimal places and
half-even rounding independent of the ambient decimal context. Monetary values retain the
settlement currency. Pair comparison does not subtract money across currencies.

Batch sorting is explicit, stable by original member ordinal for ties and missing-last in both
directions. Default member order remains unchanged. Sorting and comparison are human inspection;
there is no best-strategy label, ranking recommendation or selection permission.

Acceptance includes hand-computed mixed outcomes, zero-trade and open-position reports,
fee-induced losses, immutable report identity, restart/source-removal and corruption rejection.
Installed-wheel Chromium covers the actual multi-round-trip run, statistics, batch sorting,
comparison and restart/reopen without original strategy/data files.
