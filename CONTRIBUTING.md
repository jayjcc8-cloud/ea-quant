# Contributing

## Source of truth

- ADRs record stable architectural and trading-semantics decisions.
- Issues define one iteration's goal, non-goals, risks, and acceptance criteria.
- Pull requests record the actual diff, expert conclusions, and verification evidence.
- CI records machine-reproducible quality results.
- Plans are historical execution records, not live project state.
- Chat history is not a project source of truth.

## Iteration workflow

1. Create one Issue with owners, scope, non-goals, base SHA, risks, and acceptance criteria.
2. Create one branch for that Issue, for example `codex/12-data-schema`.
3. Open a Draft pull request early and keep its scope limited to the Issue.
4. Use Conventional Commits and keep every commit logically focused.
5. Run the required local checks and record the results in the pull request.
6. Obtain the required read-only expert and independent verification conclusions.
7. Resolve every blocker and wait for CI to pass.
8. After user approval, squash merge to `main` and delete the branch.

Direct pushes to `main` are not allowed. The Phase 0 bootstrap predates the Issue requirement;
all iterations after `v0.1.0` must link an Issue.

## Architecture decisions

Create an ADR for decisions that change module boundaries, data or time semantics, execution or
risk behavior, reproducibility guarantees, or long-lived external interfaces. Do not create ADRs
for small, reversible implementation details.

After the first baseline tag `v0.1.0`, Accepted ADRs are immutable. Replace a decision with a new
ADR that marks the old ADR as superseded. Phase 0 may correct its own ADRs before that baseline is
published.

## Required checks

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv run ea doctor
```

Tests must be deterministic and must not depend on private local market data. Secrets, broker
credentials, tokens, and non-versionable datasets must never enter Git.

## Definition of Done

An iteration is complete only when:

- Issue acceptance criteria are satisfied and the diff remains in scope.
- required tests, documentation, ADRs, configuration, and lock files are synchronized.
- local checks and pull request CI pass.
- expert evidence is recorded and blockers are zero.
- no secrets or inappropriate data are present.
- the user has approved and the pull request is merged into `main`.

## Versioning and releases

Use pre-1.0 Semantic Versioning:

- a completed Phase increments the minor version;
- a compatible fix within a Phase increments the patch version;
- ordinary pull requests do not create tags.

At a Phase boundary, verify the merged `main` branch and create an annotated tag. Phase 0 is
`v0.1.0`; Phase 1 is planned as `v0.2.0`.
