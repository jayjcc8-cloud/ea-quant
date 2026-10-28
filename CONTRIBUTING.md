# Contributing

Help improve this development preview through a fork, an Issue or a draft PR. Start with the
[README](README.md) and [current state](docs/STATUS.md), then read the code and tests for your
change. Original code contributions use [MIT](LICENSE); contributors retain their copyright.
Identify third-party material and preserve its license and notices.

## Contribution flow

1. Search [existing Issues](https://github.com/jayjcc8-cloud/ea-quant/issues). For a bug, include
   the source commit, OS/tool versions, minimal steps, expected/actual result and synthetic input.
   For a feature, describe one useful outcome and how to check it. Never attach private data.
2. Use the existing Issue form. State scope/non-goals, acceptance and checks in plain language.
   Name yourself as implementer and the repository maintainer as Product Owner; propose the
   closest risk tier and let the maintainer confirm it. You do not need to recruit internal roles.
3. Fork the repository and create one branch per change. Start ordinary fixes from `main`;
   coordinate v3 work against `feat/v3-real-research` while it remains unmerged. Record the base
   commit and target branch in the PR so reviewers can reproduce the correct implementation.
4. Make the smallest useful change, add a regression test for behavior changes and run relevant
   checks below. Open a **draft PR** early, targeting the agreed upstream branch. Link the Issue,
   explain the before/after behavior, and include commands/results plus redacted UI evidence.
   Use “not measured” for unavailable effort metrics; do not invent evidence.

Maintainers manage CI, risk classification and the bounded review in [WORKFLOW](docs/governance/WORKFLOW.md).
There is no requirement to read the full governance history before reporting a bug. Each shared
checkout has one writer: record writer, branch/worktree, exact base, scope and merge order before
edits, and stop on conflicting changes. Direct pushes to `main` are forbidden; integration normally
uses a reviewed squash merge. Design approval alone does not authorize merging the implementation.

## Local environment

Run from the repository root. Python **3.12** and the exact global `uv` version in
[`pyproject.toml`](pyproject.toml) are required; [`.python-version`](.python-version) selects the family.
The current uv pin is `0.11.28`. For engine/API development:

```sh
python3 scripts/bootstrap_local.py --web
venv/bin/ea doctor
```

Use Python 3.12 for `python3`. Bootstrap creates `venv/` and syncs locked dev + Web dependencies;
omit `--web` for engine-only work. Do not create a `.venv -> venv` symlink or bypass build isolation.
Node.js **24** with npm matches [CI](.github/workflows/ci.yml); install UI dependencies with
`npm --prefix apps/web ci`. The [offline preview](docs/frontend-v3/README.md) needs only Node/npm.
For real v3, use the [Mac source-build prerequisites](docs/desktop-macos.md#source-build-prerequisites)
and `--development` build; these include Xcode tools and the builder's repository-local Rust setup.

Keep code, runtime data and an explicitly selected absolute artifact root separate. Build scratch
belongs under ignored `.local/`; runtime data must stay outside the checkout and app bundle.
For delegated work, honor the assigned artifact root and filesystem permissions. Never commit
secrets, provider settings, broker credentials, private datasets or generated workspace databases.
Tests must be deterministic and run without private data or paid provider calls.

## Required checks

Choose checks for the changed path; a docs contribution does not need a desktop build or Full run.
The following commands are existing local entry points (run only the relevant rows):

| Change | Focused command from the repository root |
| --- | --- |
| Docs / contribution entry | `venv/bin/python -m pytest -q tests/unit/test_project_control.py tests/unit/test_governance_protocol.py` and `git diff --check` |
| Python behavior | `venv/bin/python -m pytest -q tests/unit/<affected_test>.py` |
| Real v3 research UI | `npm --prefix apps/web test -- src/studio-real.test.tsx` |
| Offline design preview | `npm --prefix apps/web run test:studio` |

For shared UI changes also run `npm --prefix apps/web run lint`,
`npm --prefix apps/web run typecheck` and `npm --prefix apps/web run build`.
For offline studio changes use `npm --prefix apps/web run lint:studio`,
`npm --prefix apps/web run typecheck:studio` and `npm --prefix apps/web run build:studio`.
The [frontend guide](apps/web/README.md) covers installed browser E2E;
[native checks](docs/desktop-macos.md#native-ci-checks) cover Rust contracts and macOS shell compile.
Shared business tests do not prove native lifecycle, workspace ownership, keyboard/IME or WebView behavior.

The repository-owned Python verifier provides the broader profiles when required:

```sh
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

`quality` checks locked installation, reproducibility, lint, format, types, tests and doctor.
`full` additionally checks byte-identical wheel builds and an installed wheel outside the repository.
CI uses [`scripts/ci_routes.py`](scripts/ci_routes.py) to select docs, Python, frontend, installed
browser and native checks. T0/T1 do not need candidate-full merely for extra process; T2/T3 retain
their exact-head requirements. Preserve safety assertions and coverage gates. Report skipped or
unrun checks honestly; a previous commit's green CI is not evidence for a new head.

## Engineering boundaries

Preserve frozen reports, explicit experiment confirmation, unknown-outcome recovery and the
separation between AI draft edits and execution. The [desktop ADR](docs/adr/0050-desktop-only-product-delivery.md)
defines the product entry; `ea web serve` remains an internal development/test tool. Existing Paper
operator tools and historical server checks remain separate from native v3.
Do not rewrite Accepted ADRs; propose a superseding ADR when a lasting boundary must change.
Update STATUS only when durable state changes. Automated agents also follow [AGENTS.md](AGENTS.md).

Live/broker writes, secrets or permissions changes, releases/tags/publication, deployment,
irreversible migrations and destructive data operations require explicit Human Product Owner
authorization. Source collaboration does not require signing, notarization, clean-Mac acceptance
or a Paper soak; those checks still govern the separate claims they validate.
