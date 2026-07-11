# Agent Collaboration Rules

This repository uses expert review with a single implementation owner for every iteration.
Git history, ADRs, Issues, pull requests, and CI are the project record; chat history is not.

## Required roles

Every Issue must name at least these roles:

- **Architecture Owner**: read-only; checks scope, boundaries, and ADR consistency.
- **Implementation Owner**: the only agent or person allowed to edit the active checkout.
- **Verification Owner**: read-only; independently verifies acceptance criteria and evidence.

Add a data, backtest, risk, security, or release expert only when the Issue touches that domain.
Small changes do not require a full expert panel.

## Write isolation

- A shared checkout has exactly one writer.
- Read-only experts must not modify tracked/source files, stage, commit, push, or change external
  GitHub state. They may run verification that creates ignored ephemeral artifacts such as test,
  type-check, lint, package, or virtual-environment caches.
- Parallel writers require separate worktrees, separate branches, and a declared merge order.
- Never mix unrelated cleanup or refactoring into an iteration.
- Stop when the worktree contains changes whose ownership or scope is unclear.

## Expert context package

Experts receive only the context required for their review:

- Issue, goal, and explicit non-goals
- base branch and base commit SHA
- relevant Accepted or Proposed ADRs
- files and modules in scope
- acceptance criteria and verification commands
- current diff or pull request
- known risks and blockers

Do not use a complete chat transcript as project context.

## Review output

Every expert report must include:

- reviewed scope and HEAD SHA
- evidence inspected
- findings classified as blocker, major, or minor
- recommended action
- final verdict

A pull request cannot become ready or merge while any blocker remains open.

## Iteration handoff

Handoffs use the Issue, ADRs, commit SHA, pull request diff, verification evidence, and unresolved
follow-up Issues. The next iteration starts from a clean, synchronized `main` branch.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the Git and release workflow.
