# Closed price rule to local simulated Paper

`price-rule-entry-v1` is a built-in Strategy V1 event interface in Scenario V2.
Its rule document has its own version 1. It emits a quantity at most once,
never emits an exit, and records entry at decision time (not Fill time).
Those versions are independent of Action V2, package schema 2 and Scenario V4.

The new explicit `examples/local-strategies/price-rule-action-v2` package has
strategy ID `price-rule-action-v1`, strategy version 1, package schema 2,
Action V2, and `single-long-round-trip-v1` lifecycle. This lifecycle permits an
open long at the end; the package adds no exit. It imports the distribution's
shared `PriceRuleLogic.on_prices` evaluator. The ordinary accepted binding
pins both package bytes and EA implementation identity. It is trusted local
code, not a sandboxed upload.

Mapping:

| State / condition | Action |
| --- | --- |
| FLAT_INITIAL, history not ready or rule false | HOLD |
| FLAT_INITIAL, full history ready and rule true, first decision | ENTER_LONG with frozen target_quantity |
| Entry decision already emitted, including an unfilled order | HOLD |
| LONG_OPEN or FLAT_CLOSED | HOLD |

The bounded Fraction-based SMA includes the current visible raw original bar.
All operands must be ready, including NOT/ANY branches. The historical and
Paper owners enforce active dispatch and availability before this interface.
Existing next-event matching, latency, costs, risk, broker, fact dedup and
ledger authorities still own execution; a signal is never a Fill. Stop
preserves an open position and drains already issued facts without liquidation.

Use normal pack/validate, new Scenario V4 source and chronological Holdout,
new EVALUATED Candidate, explicit research decision and read-only simulation
preparation, then a separately invoked bounded `paper start`. A copied schema
number cannot convert a historical Candidate. Do not rewrite its reports,
snapshots, decisions or accepted binding. EVALUATED/ACCEPTED grants no start
authorization. Code changes require fresh validation evidence.

A fresh independent run initializes an empty window and unused entry flag.
Repeated start using an existing attempt ID rejects before effects. Public
`paper resume` retains its current read-only terminal/incomplete inspection;
it does not continue a feed. The lower-level feed restore currently does not
reconstruct an arbitrary package's rolling window. This package declares
`requires_market_history_on_restore = True`; the restore path refuses it before
rebuilding economic owners. This is deliberately a limited fresh-run bridge,
not crash-continuation support. Complete crash continuation requires durable
visible-bar/strategy checkpoint semantics and a separate bounded change.

Task14 acceptance uses only six-row synthetic source/Holdout and a generated
local feed. It proves wiring and exact arithmetic, not economic merit or
production readiness. No external broker, account, daemon or Live authority
is added.
