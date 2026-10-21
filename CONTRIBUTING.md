# Contributing

Read [STATUS](docs/STATUS.md), the complete [WORKFLOW](docs/governance/WORKFLOW.md), the current
Issue and its directly relevant ADRs. This contributor entry is not a second workflow.

## Contribution Flow

Use the existing checkout and one bounded branch per objective; record scope, acceptance, tier,
owner, writer and exact base in the Issue/PR. Commit verified slices promptly and update the same
PR. After integration, retire its branch and return to current main. Keep scratch/build/evidence
under ignored `.local/`. Implement behavior test-first and run the required checks.

Use the tier and bounded review loop in [WORKFLOW](docs/governance/WORKFLOW.md): T0 needs CI;
T1 one primary review; T2/T3 one fresh independent adversarial review on exact HEAD. Repair
qualifying blockers once and verify those blockers and direct regressions. The Product Owner
then chooses merge, scope reduction or closure; do not create another reviewer chain.

Direct pushes to `main` are forbidden. PRs normally use squash merge. Accepted ADRs are immutable;
supersede them under [`docs/adr/`](docs/adr/). Update STATUS/ADR only when durable state changes.
Independent review means a fresh read-only context, not a second human or GitHub account.
T3, release, deployment and other protected operations retain explicit Human Product Owner approval.

## Local Environment

Requirements are read from `pyproject.toml`; do not duplicate version pins in task documentation.

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
```

The bootstrap creates the repository-local `venv/` and performs a locked editable sync. Do not
create a `.venv -> venv` symlink or bypass build isolation.

For local development and acceptance work, explicitly set the code working directory, runtime
data root, and absolute artifact root separately; use that artifact root for JSON and screenshots
regardless of the tool's working directory. Selecting a source directory does not expand sandbox
write permissions. If the intended output root is not writable, use the supported permission
request and respect its outcome; do not redirect persistent artifacts to Documents or temporary
directories as a workaround. Formal research data may remain in an explicitly selected external
runtime data root; it need not live in the source checkout.

## Internal UI and service checks

The [Mac desktop](docs/desktop-macos.md) is the sole QUINE product entry and final acceptance
target. `apps/web` is shared React/WebView code; `src/ea/web` and the `web` extra remain required
desktop dependencies. Keep `ea web serve` for internal development, installed integration tests
and historical compatibility. [Frontend instructions](apps/web/README.md) describe those tools,
not a supported browser installation. Do not copy the frontend or migrate IPC for this boundary.

The scripted CSV preview (`apps/web/preview/first-use.html`) is an internal synthetic fixture
with no API, engine or model calls; see the frontend instructions for its launcher. It is not
proof of real import/model success. Narrow content and text scaling still need layout checks,
without a mobile-product promise.

## Required Checks

The repository-owned verifier is the executable authority:

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

`quality` checks the lock/environment boundary, isolated installed package, reproducibility gate,
lint, format, types, tests, and doctor. `full` additionally proves byte-identical wheel builds and
an installed wheel outside the repository. `quality` does not run the release-level dual build.
CI routes changed paths through `scripts/ci_routes.py`: docs-only changes run focused governance
tests, Python pull requests run `quality`, and `main` runs installed-wheel smoke. Pull requests
run installed browser E2E as an internal carrier for UI, HTTP, shared engine/contract, dependency, build and example changes;
the full browser job does not repeat on the merged-main push. Unknown paths take the conservative
route. A newly added Python module skips browser E2E only when the current installed Web/CLI import
path cannot reach it; changed or removed modules and uncertain import analysis retain browser E2E.
Frontend lint, types, unit tests and build run for Web/Node/CI changes. Release candidates
run `full`; T2/T3 retain the exact-SHA candidate-full workflow. T0/T1 do not need it merely to
prove more process.

Shared component, business and API regressions run once according to risk. Preserve existing job
IDs and safety/economic assertions. The installed desktop is the final product acceptance object
for native lifecycle, workspace ownership, WebView restrictions, focus/keyboard/IME and text
scaling; a browser pass is not native acceptance. Do not duplicate shared business E2E into a
second desktop suite. [STATUS](docs/STATUS.md) links current native evidence and unverified scope.

`production-runtime.yml` is historical server compatibility verification, not QUINE release
acceptance. Manual dispatch is available; existing path-triggered checks remain until any required
check dependencies are separately resolved. `build_distribution.py`, Linux `production_runtime.py`
and `deploy/*` remain historical tools, outside new QUINE product releases. Existing services and
Mac Paper drills are not uninstalled or removed.

Tests must be deterministic and must not require private data. Secrets, broker credentials, and
non-versionable datasets never enter Git.

## State and Release Updates

- Current implementation and CI state update [STATUS](docs/STATUS.md).
- Stable architectural or long-lived boundary changes use a superseding ADR.
- Phase entry/exit criteria update [ROADMAP](docs/ROADMAP.md).
- Deferred hardening becomes a linked Issue, not an unowned note.

Release, tag, deployment, and publication require explicit Human Product Owner approval.
