# Contributing

## Source of truth

- ADRs record stable architectural and trading-semantics decisions.
- Issues define one iteration's goal, non-goals, risks, and acceptance criteria.
- Pull requests record the actual diff, expert conclusions, and verification evidence.
- CI records machine-reproducible quality results.
- Plans are historical execution records, not live project state.
- Chat history is not a project source of truth.

## Iteration workflow

1. Create one Issue with risk tier and reason, owners, scope, non-goals, base SHA, risks, and
   acceptance criteria.
2. Before design or code, complete the bounded open-source reuse assessment required by
   [AGENTS.md](AGENTS.md): inspect existing dependencies and up to three serious mature candidates,
   then record the reuse, adapter, or local-build decision and rejection reasons. Tier 0 may record
   `N/A` with a reason.
3. Record the Implementation Owner's writer lease: branch, checkout/worktree identity, base SHA,
   start state, and merge order when other work is active.
4. Create one branch for that Issue, for example `codex/12-data-schema`.
5. Open a Draft pull request early and keep its scope limited to the Issue.
6. Use Conventional Commits and keep every commit logically focused.
7. Activate only the experts required by the Issue risk tier, at the gates defined in
   [AGENTS.md](AGENTS.md).
8. Run the repository verification entry point and record its exact-HEAD result in the pull
   request.
9. Resolve every finding blocker, refresh stale SHA-bound verdicts, and wait for CI to pass.
10. Extract durable expert knowledge into the Issue, ADR, pull request, tests, or follow-up Issues;
   release completed experts.
11. After user approval, squash merge to `main` and delete the branch.

Direct pushes to `main` are not allowed. The Phase 0 bootstrap predates the Issue requirement;
all iterations after `v0.1.0` must link an Issue.

## Architecture decisions

Create an ADR for decisions that change module boundaries, data or time semantics, execution or
risk behavior, reproducibility guarantees, or long-lived external interfaces. Do not create ADRs
for small, reversible implementation details.

For Tier 1 and Tier 2 work, the reuse assessment is part of the Issue's acceptance evidence, not
an informal chat note. Prefer an already locked dependency when it fits. New dependencies require
the same supported/locked version, license, maintenance, supply-chain, security, integration,
migration, and lock-in evidence as other candidates. A local implementation must state why it is
smaller or safer to own. Revisit the decision only when the requirement or material candidate
evidence changes.

After the first baseline tag `v0.1.0`, Accepted ADRs are immutable. Replace a decision with a new
ADR that marks the old ADR as superseded. Phase 0 may correct its own ADRs before that baseline is
published.

## Required checks

The repository-owned verification entry point is the source of truth for executable checks:

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

`quality` verifies the supported uv version, lock immutability, locked environment, isolated
installed metadata, CLI, the reproducible preparation gate, lint, formatting, types, tests, and
doctor. `full` includes `quality` and adds byte-identical isolated wheel builds plus a clean-wheel
installation and outside-repository smoke test. Pull requests and CI use `full`; `quality` is the
faster implementation loop.

The script reads the project version and required uv version from `pyproject.toml`; contributor
documentation and CI must not duplicate those values or maintain a second command list.

Tests must be deterministic and must not depend on private local market data. Secrets, broker
credentials, tokens, and non-versionable datasets must never enter Git.

The supported frontend version is declared once by `[tool.uv].required-version` in
`pyproject.toml`. The isolated PEP 517 backend closure is separately pinned and hash-verified by
`build-constraints.txt`; `uv.lock` does not replace that build constraint. Never use
`--no-build-isolation` for the canonical build.

The `full` profile installs the built wheel with `--no-deps` into an independently created
environment whose runtime dependencies came from `uv sync --locked --no-install-project`. It runs
`uv pip check`, compares distribution metadata with `ea.__version__`, and runs the installed
`ea doctor` outside the repository.

## Expert review gates

- Tier 0 activates the Verification Owner only for the final candidate SHA.
- Tier 1 activates the Architecture Owner once the design/diff is reviewable and the Verification
  Owner only for the final candidate SHA.
- Tier 2 follows Tier 1 and adds the relevant domain expert at the decision or implementation gate
  named by the Issue.

Reports and finding closures are recorded in the pull request. Any new commit makes earlier
verdicts stale; re-review is scoped to the old-to-new SHA delta and open finding IDs. File-producing
verification runs in CI or an isolated verification worktree, never in the Implementation Owner's
active checkout.

## Definition of Done

An iteration is complete only when:

- Issue acceptance criteria are satisfied and the diff remains in scope.
- required reuse assessment and dependency decision evidence are recorded.
- required tests, documentation, ADRs, configuration, and lock files are synchronized.
- local checks and pull request CI pass.
- required exact-HEAD expert evidence is current and blockers are zero.
- useful expert knowledge is recorded and completed agents are released.
- no secrets or inappropriate data are present.
- the user has approved and the pull request is merged into `main`.

## Versioning and releases

Use pre-1.0 Semantic Versioning:

- a completed Phase increments the minor version;
- a compatible fix within a Phase increments the patch version;
- ordinary pull requests do not create tags.

At a Phase boundary, verify the merged `main` branch and create an annotated tag. Phase 0 is
`v0.1.0`; Phase 1 is planned as `v0.2.0`.
