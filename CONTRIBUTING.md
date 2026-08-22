# Contributing

Start with [current status](docs/STATUS.md), then read the complete
[governance workflow](docs/governance/WORKFLOW.md) and the relevant task Issue/ADRs. This file is a
contributor entry point, not a second workflow specification.

## Contribution Flow

1. Create or accept one complete task-package Issue.
2. Confirm Tier 0/1/2, reuse assessment, required owners, and acceptance criteria.
3. Record the writer lease, exact base SHA, isolated branch/worktree, and merge order.
4. Open one Draft pull request and keep its diff within the Issue scope.
5. Implement through focused Conventional Commits and test-first evidence where behavior changes.
6. Freeze exact HEAD, run required verification, and complete the risk-tier review gates.
7. Synchronize STATUS/ADR/task evidence and create successor Issues for deferred work.
8. Obtain the applicable Ready, merge, and cleanup decisions defined by WORKFLOW.

Direct pushes to `main` are forbidden. Pull requests use squash merge. Accepted ADRs are immutable;
replace a decision with a superseding ADR under [`docs/adr/`](docs/adr/).

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

`quality` checks the uv/lock/environment boundary, isolated installed package, reproducibility
gate, lint, format, types, tests, and doctor. `full` also proves byte-identical isolated wheel
builds and validates the installed wheel outside the repository. Pull requests and CI use `full`.

Tests must be deterministic and must not require private data. Secrets, broker credentials, and
non-versionable datasets never enter Git.

## State and Release Updates

- Current implementation/CI changes update [STATUS](docs/STATUS.md).
- Stable architectural, semantic, governance, or long-lived boundary changes update an ADR.
- Phase boundaries and exit/entry criteria update [ROADMAP](docs/ROADMAP.md).
- Deferred work becomes a successor Issue, never an unowned “later” note.

Phase completion increments the pre-1.0 minor version; compatible within-Phase fixes increment the
patch version. Release, tag, deployment, and publication require explicit Human Owner approval.
