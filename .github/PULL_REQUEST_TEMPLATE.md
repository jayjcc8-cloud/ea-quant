## Iteration

- Issue:
- Milestone / target version:
- Base branch and base SHA:
- Architecture Owner:
- Implementation Owner:
- Verification Owner:

## Goal

<!-- State the single outcome this PR delivers. -->

## Non-goals

<!-- List work that is explicitly outside this PR. -->

## Changes

<!-- Summarize the important files, behavior, and interfaces changed. -->

## Decisions and assumptions

- Related ADRs:
- New assumptions:
- Data/config/dependency impact, or N/A with reason:

## Quant and operational risk

- Look-ahead bias / time semantics, or N/A with reason:
- Execution and fill assumptions, or N/A with reason:
- Reproducibility impact, or N/A with reason:
- Secrets, live-trading flags, and external writes:
- Rollback plan:

## Verification

- [ ] `uv --version` matches `[tool.uv].required-version`
- [ ] `uv lock --check` and `git diff --exit-code HEAD -- uv.lock`
- [ ] `UV_PROJECT_ENVIRONMENT=venv uv sync --locked --extra dev`
- [ ] Installed metadata and `ea.__version__` both report the target version under `python -I`
- [ ] `venv/bin/ea doctor`
- [ ] `UV_PROJECT_ENVIRONMENT=venv uv run --locked ruff check .`
- [ ] `UV_PROJECT_ENVIRONMENT=venv uv run --locked ruff format --check .`
- [ ] `UV_PROJECT_ENVIRONMENT=venv uv run --locked mypy`
- [ ] `UV_PROJECT_ENVIRONMENT=venv uv run --locked pytest -q`
- [ ] `UV_PROJECT_ENVIRONMENT=venv uv run --locked ea doctor`
- [ ] Commit-derived `SOURCE_DATE_EPOCH` plus two isolated, hashed wheel builds compare byte-identically
- [ ] Clean `--no-deps` wheel install, `uv pip check`, and outside-repository smoke (when packaging changes)
- CI result:

## Expert review

| Role | Reviewer | HEAD SHA | Verdict | Blockers |
|---|---|---|---|---|
| Architecture Owner | | | | |
| Verification Owner | | | | |
| Domain expert, if required | | | | |

## Completion

- [ ] All blockers are resolved.
- [ ] Documentation and ADRs are synchronized.
- [ ] No unrelated changes are included.
- [ ] Follow-up work is recorded as Issues.
- [ ] Version impact is declared: none / patch / minor / major.
- [ ] Worktree is clean and the PR diff has been reviewed.
