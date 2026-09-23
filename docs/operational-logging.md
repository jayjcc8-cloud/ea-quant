# Structured Operational Logging V1

Scenario runs write `operational.jsonl` beside attempt evidence. This machine-readable stream
explains the observed runtime path; it is not a journal, ledger, recovery input or authorization
source. Existing `audit.jsonl`, manifest, funding, result and formal reports keep their authority.

## Identity

- `run_id` is the existing attempt UUID and `strategy_id` is the selected strategy identity.
- `candidate_id` and `account_id` are nullable when the runtime has no such identity. They must
  never be inferred from a job ID, strategy name or ledger posting category.
- `correlation_id` is the originating strategy signal's existing EconomicId, carried through
  intent, risk, order, fact, Fill and ledger. `market_event_id` links that decision to admitted data.
- `order_id` and `fill_id` use the canonical digests of existing run-scoped EconomicIds.
  `client_order_id` exposes
  the existing stable client submission key, not a newly allocated order identity.
- `operation` distinguishes a fresh execution from observational replay during supported resume.

Each line is one JSON object with schema `ea.operational-log.v1`, event name, timestamp and sequence, with
applicable identity and outcome fields. Decimal economic values stay decimal strings. Context
is explicitly passed per run so concurrent Web workers cannot inherit another run's identity.
Only selected scalars are logged; strategy source, full configuration, arbitrary exception text
and credentials are not log payloads.

## Reconstruction and failure

The event names are `run.started`, `portfolio.funded`, `market.event`, `strategy.decision`,
`risk.decision`, `order.created`, `broker.submitted`, `execution.fact`, `execution.fill`,
`portfolio.updated`, `reconciliation.result`, and `run.completed`/`run.failed`. Broker events in
this work unit describe the existing local historical simulator. The legacy run-only RESET demo
is unchanged; logging is provided by the strict scenario path used by both CLI and Web jobs.

Filter by `run_id`, then follow the market event and signal correlation to the risk decision and
order/client key. Match the simulator submission result, execution facts and Fill to those IDs;
portfolio observations describe acknowledged state. Reconciliation observations report the
existing authority's result. A risk denial retains its run, strategy and signal correlation while
creating no order or Fill.

For example, parse the whole stream with `jq -c . operational.jsonl`, or select a run with
`jq -c --arg run RUN_ID 'select(.run_id == $run)' operational.jsonl`. Natural-language parsing is
not required. Operational events can repeat during resume; they do not represent additional
economic effects. Completed-attempt verification remains a non-mutating operation.

Observability failures are best effort and cannot turn an authorized/rejected decision or ledger
effect into another result. Missing/truncated operational output is therefore not proof that an
economic event did not occur; inspect authoritative evidence. Remote collectors, rotation policy
for long-running services and monitoring infrastructure are outside this bounded logging unit.

## Validation

`test_operational_logging.py` verifies serialization/context/sink behavior.
`test_operational_logging_runtime.py` exercises real scenario composition, causal reconstruction,
risk denial, repeated trades and economic parity with logging disabled or failing. Existing
single-run, round-trip and supported-resume tests retain their economic assertions.
