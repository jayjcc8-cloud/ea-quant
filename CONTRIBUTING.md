# Contributing

Start with [current status](docs/STATUS.md), then read the complete
[governance workflow](docs/governance/WORKFLOW.md), the current Issue, and its directly relevant
ADRs. This is a contributor entry point, not a second workflow.

## Contribution Flow

1. Define one objective, finite acceptance criteria, scope/non-goals, actual-impact tier, and
   owner in a GitHub Issue.
2. Record one writer, exact base SHA, isolated branch/worktree, scope, and merge order.
3. Implement behavior test-first. Prefer current project capabilities and local reversible changes
   over a new framework.
4. Open one pull request and run the required checks.
5. T0 merges after CI. T1 receives one primary review. T2/T3 receive one independent adversarial
   review on exact HEAD; T3 also requires Human Product Owner approval and rollback.
6. If the primary review has qualifying blockers, perform one concentrated repair and one
   verification limited to those blockers and direct regressions.
7. The Product Owner then chooses merge, scope reduction, or closure. Do not create another
   reviewer chain.
8. Update STATUS/ADR only when durable state changes; link non-blocking hardening to an Issue.

Direct pushes to `main` are forbidden. Pull requests normally use squash merge. Accepted ADRs are
immutable; supersede them under [`docs/adr/`](docs/adr/).

## Local Environment

Requirements are read from `pyproject.toml`; do not duplicate version pins in task documentation.

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
```

The bootstrap creates the repository-local `venv/` and performs a locked editable sync. Do not
create a `.venv -> venv` symlink or bypass build isolation.

## Required Checks

The repository-owned verifier is the executable authority:

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

`quality` checks the lock/environment boundary, isolated installed package, reproducibility gate,
lint, format, types, tests, and doctor. `full` additionally proves byte-identical wheel builds and
an installed wheel outside the repository. CI remains path-aware: docs-only changes run focused
governance tests, Python pull requests run `quality`, `main` runs installed-wheel smoke, Web runs
only for Web/Node/CI paths, and release candidates run `full`. T2/T3 use the existing exact-SHA
candidate-full workflow; T0/T1 do not need it merely to prove more process.

Tests must be deterministic and must not require private data. Secrets, broker credentials, and
non-versionable datasets never enter Git.

## State and Release Updates

- Current implementation and CI state update [STATUS](docs/STATUS.md).
- Stable architectural or long-lived boundary changes use a superseding ADR.
- Phase entry/exit criteria update [ROADMAP](docs/ROADMAP.md).
- Deferred hardening becomes a linked Issue, not an unowned note.

Release, tag, deployment, and publication require explicit Human Product Owner approval.
