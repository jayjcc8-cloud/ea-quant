# ADR 0002: Git and GitHub Workflow

Date: 2026-07-11

## Status

Accepted

## Context

The project is intended to run and iterate for a long time. Version control, review history, reproducibility, and GitHub integration are hard requirements.

The local repository is initialized on `main`. GitHub CLI is authenticated as `jayjcc8-cloud`, SSH authentication to GitHub has been configured successfully, and `origin` points to `git@github.com:jayjcc8-cloud/ea-quant.git`. Local Git identity is configured repo-locally.

## Decision

Use trunk-based development with lightweight feature branches.

- `main` must stay runnable and recoverable.
- Implementation work uses `codex/<topic>` or `feature/<topic>` branches.
- Commits use Conventional Commits.
- Architecture changes require an ADR.
- GitHub remote is required before the first serious implementation phase.
- Secrets never enter Git; examples use `.env.example` only.

## Initial sequence

1. Configure repo-local Git identity. Done.
2. Commit the initial architecture and project skeleton. Done.
3. Create the private GitHub repository `jayjcc8-cloud/ea-quant`. Done.
4. Connect the GitHub repository through an SSH remote. Done.
5. Push `main`. Done.
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

The workflow is active after:

- `git remote -v` shows a GitHub SSH remote.
- `main` is pushed to GitHub.
- At least one GitHub Actions workflow runs on push or PR.

The first two criteria are complete. GitHub Actions is the next workflow milestone.
