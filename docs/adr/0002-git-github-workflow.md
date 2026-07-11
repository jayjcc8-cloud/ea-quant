# ADR 0002: Git and GitHub Workflow

Date: 2026-07-11

## Status

Accepted

## Context

The project is intended to run and iterate for a long time. Version control, review history, reproducibility, and GitHub integration are hard requirements.

The local repository was initialized on `main`, SSH access was validated during bootstrap, and
`origin` points to `git@github.com:jayjcc8-cloud/ea-quant.git`. Local Git identity is configured
repo-locally. Authentication is an operational prerequisite that must be re-verified before each
GitHub release action rather than recorded here as permanent state.

## Decision

Use trunk-based development with lightweight feature branches.

- `main` must stay runnable and recoverable.
- One Issue maps to one branch and one Draft pull request.
- Implementation work uses issue-scoped branches such as `codex/12-data-schema`.
- Commits use Conventional Commits.
- Architecture changes require an ADR.
- GitHub remote is required before the first serious implementation phase.
- Secrets never enter Git; examples use `.env.example` only.
- Expert reviewers and the Verification Owner are read-only; a shared checkout has one writer.
- Pull requests require passing CI, zero expert blockers, and user approval before squash merge.
- Phase releases are verified on `main` and published as annotated Semantic Versioning tags.

The required roles, context package, and handoff format are defined in `AGENTS.md`. Contribution,
Definition of Done, and release rules are defined in `CONTRIBUTING.md`.

## Initial sequence

1. Configure repo-local Git identity. Done.
2. Commit the initial architecture and project skeleton. Done.
3. Create the private GitHub repository `jayjcc8-cloud/ea-quant`. Done.
4. Connect the GitHub repository through an SSH remote. Done.
5. Push `main`. Done.
6. Add GitHub Actions for lint, typecheck, and tests. Done.
7. Open future changes through PRs. Active with Phase 0 PR #1.

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
- Expert review and a single-writer implementation boundary are recorded in the repository.
- Phase delivery is verified on `main` and tagged.

Git, SSH, GitHub, pull request CI, and the expert workflow are active. Phase 0 is complete after
PR #1 is merged, `main` is re-verified, and annotated tag `v0.1.0` is pushed.
