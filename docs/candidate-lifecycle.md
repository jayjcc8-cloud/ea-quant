# Candidate Lifecycle V1

A research strategy becomes a Candidate only after an explicit selection of a successful source
run and its chronological Holdout. In Web Run Detail, select the Holdout and create the Candidate.
The new record is EVALUATED. Inspect its evidence and enter a reason to ACCEPT or REJECT it.
Terminal decisions are immutable; repeating the identical decision is idempotent.

Accepted status records a research decision. It does not start trading. To inspect an accepted
candidate from an existing workspace without executing strategy code:

```sh
ea candidate inspect --workspace /absolute/workspace --candidate-id UUID
```

The command checks current persisted evidence and prints machine-readable identity. Missing or
changed records, reports, paths, captured inputs or frozen artifacts fail closed. Original CSV and
strategy source files are not required for inspection. Local strategy packages remain trusted code.

To run the accepted configuration explicitly through the existing offline backtest engine:

```sh
ea candidate run --workspace /absolute/workspace --candidate-id UUID \
  --scenario /absolute/scenario.yaml --output-root /absolute/candidate-runs
```

The scenario must retain the accepted strategy, normalized parameters, lifecycle, instrument,
funding, risk and execution assumptions. Its data is validated independently. Configuration and
artifact checks precede executable package loading. Frozen accepted bytes provide the local
strategy; an arbitrary Python filename is never an accepted-candidate selector.

Each new attempt retains `candidate-binding.json`, normal economic evidence and correlated
`operational.jsonl` events with candidate_id. Existing unbound backtest and report commands keep
their established behavior. This command performs one finite offline run. Continuous local Paper
execution is composed in PPV-11; VPS and Live authorization remain denied.
