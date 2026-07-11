# ADR 0002: Git and GitHub Workflow

Date: 2026-07-11

## Status

Accepted

## Context

The project is intended to run and iterate for a long time. Version control, review history, reproducibility, and GitHub integration are hard requirements.

The local repository is initialized on `main`, but no remote is currently configured. GitHub CLI is authenticated as `jayjcc8-cloud`, and SSH authentication to GitHub has been configured successfully. Local Git identity should be configured repo-locally before the first commit.

## Decision

Use trunk-based development with lightweight feature branches.

- `main` must stay runnable and recoverable.
- Implementation work uses `codex/<topic>` or `feature/<topic>` branches.
- Commits use Conventional Commits.
- Architecture changes require an ADR.
- GitHub remote is required before the first serious implementation phase.
- Secrets never enter Git; examples use `.env.example` only.

## Initial sequence

1. Configure repo-local Git identity.
2. Commit the initial architecture and project skeleton.
3. Create the private GitHub repository `jayjcc8-cloud/ea-quant`.
4. Connect the GitHub repository through an SSH remote.
5. Push `main`.
6. Add GitHub Actions for lint, typecheck, and tests.
7. Open future changes through PRs.

## Commit conventions

- `docs:` documentation and ADRs
- `feat:` user-visible capability
- `fix:` bug fix
- `test:` tests
- `ci:` CI configuration
- `chore:` project maintenance
- `refactor:` behavior-preserving structure change

## Validation

The workflow is considered active only after:

- `git remote -v` shows a GitHub remote.
- `main` is pushed to GitHub.
- At least one GitHub Actions workflow runs on push or PR.
