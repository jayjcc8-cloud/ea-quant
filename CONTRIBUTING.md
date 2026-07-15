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
uv --version
uv lock --check
git diff --exit-code HEAD -- uv.lock
UV_PROJECT_ENVIRONMENT=venv uv sync --locked --extra dev
venv/bin/python -I -c "import importlib.metadata as m; import ea; assert m.version('ea-quant') == ea.__version__ == '0.1.1'"
venv/bin/ea doctor
UV_PROJECT_ENVIRONMENT=venv uv run --locked ruff check .
UV_PROJECT_ENVIRONMENT=venv uv run --locked ruff format --check .
UV_PROJECT_ENVIRONMENT=venv uv run --locked mypy
UV_PROJECT_ENVIRONMENT=venv uv run --locked pytest -q
UV_PROJECT_ENVIRONMENT=venv uv run --locked ea doctor
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
UV_PROJECT_ENVIRONMENT=venv uv build --wheel --clear \
  --build-constraints build-constraints.txt --require-hashes --out-dir build/wheel-a
UV_PROJECT_ENVIRONMENT=venv uv build --wheel --clear \
  --build-constraints build-constraints.txt --require-hashes --out-dir build/wheel-b
cmp build/wheel-a/*.whl build/wheel-b/*.whl
venv/bin/python -c "import hashlib, pathlib; p = next(pathlib.Path('build/wheel-a').glob('*.whl')); print(hashlib.sha256(p.read_bytes()).hexdigest(), p)"
```

Tests must be deterministic and must not depend on private local market data. Secrets, broker
credentials, tokens, and non-versionable datasets must never enter Git.

The supported frontend version is declared once by `[tool.uv].required-version` in
`pyproject.toml`. The isolated PEP 517 backend closure is separately pinned and hash-verified by
`build-constraints.txt`; `uv.lock` does not replace that build constraint. Never use
`--no-build-isolation` for the canonical build.

Packaging changes must additionally install the built wheel with `--no-deps` into an independently
created environment whose runtime dependencies came from `uv sync --locked --no-install-project`.
Run `uv pip check`, an isolated import that compares distribution metadata with `ea.__version__`,
then the installed `ea doctor` from outside the repository. The CI workflow is the executable
reference for this clean-wheel smoke test.

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
