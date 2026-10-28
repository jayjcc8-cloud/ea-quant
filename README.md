# QUINE — Local Quantitative Research (Preview)

QUINE is a work-in-progress Mac research application built on the EA deterministic engine.
Contributions through **forks, Issues and draft pull requests** are welcome. The current goal is
source collaboration; this preview is not a mature trading product or a public desktop installer.
Repository-wide licensing still needs maintainer confirmation; see [Licensing](#licensing).

## What works today

The development branch `feat/v3-real-research` contains the current v3 research implementation.
It has not been merged into `main`; design approval does not mean implementation acceptance.
The Apple Silicon Mac desktop is the product target. Browser previews support development.

| Surface | Available | Limits |
| --- | --- | --- |
| Native v3 `/studio` (`apps/web/src/studio-real.tsx`) | Save research, import/check CSV, bind strategy/data, edit drafts, preview and explicitly confirm local engine experiments, read frozen reports, select next-launch workspace | Development candidate; final native acceptance is pending |
| Research assistant | Uses the existing model service when explicitly configured; draft changes and experiment confirmation remain separate | v3 model connections are read-only; real-provider calls have not been accepted |
| Offline design preview (`apps/web/studio/`) | Resettable synthetic scenes for layout and interaction work | No real engine, provider, account or exchange calls; demo results are not research evidence |
| Simulation, Live, batches, holdout, Candidate promotion | Older engine/Paper tooling remains in the repository | These flows are **not connected in native v3**; external brokers and real-money trading are unavailable |

Historical EA **0.2.0** is the package version and an earlier offline prerelease, not a new QUINE
release. No real-time coverage, profitability, investment suitability or real-money trading is
promised. A backtest describes its frozen inputs, not future returns.

## Try the design from source

Use Git and **Node.js 24 with npm** (the version family used in CI). No model key or Python service
is needed for this synthetic preview:

```sh
git clone --branch feat/v3-real-research https://github.com/jayjcc8-cloud/ea-quant.git quine
cd quine
npm --prefix apps/web ci
npm --prefix apps/web run dev:studio
```

Open <http://127.0.0.1:4176/>. Stop with Ctrl-C. If the port is occupied, the command fails rather
than replacing another service. [v3 preview instructions](docs/frontend-v3/README.md) describe
reset, persistence and focused checks. This preview does not run real research.

For the **real local research path**, follow the [source build prerequisites and development
command](docs/desktop-macos.md#source-build-prerequisites) on Apple Silicon macOS. The native
window opens `/studio`, using the existing Python services and Rust experiment owner. Build from
the branch above and select a fresh data root; an older installed app does not acquire new code.
No paid model call is needed to save research or run a bundled synthetic engine experiment.

## Work with research evidence

1. Save an idea, then bind a registered strategy and data in its draft. For your own CSV, declare
   provenance and usage rights, preview it, then explicitly publish and select its immutable version.
2. Review parameters, funding, currency and cost assumptions. Applying a proposal edits the draft
   only. Preview the inputs and explicitly confirm one local experiment.
3. Read the frozen report and its source identity. For an unknown outcome, query the original
   request; do not blindly resend it. A report or Candidate decision is not trading authorization.

Keep private datasets, credentials, workspace databases, logs and screenshots with private
content out of Git and public Issues. Use the bundled synthetic fixtures for reproductions.
Never paste API keys into research text or commit model settings. Local Python strategy plugins
are trusted executable code; there is no general sandbox for untrusted strategies.

## Improve QUINE

Start with [CONTRIBUTING](CONTRIBUTING.md): choose one small problem, reproduce it, run the
relevant checks and open a draft PR from your fork. Useful work includes clearer first use,
accessibility, reproducible bugs, tests and documentation. Maintainers handle integration and
required reviews; community contribution does not grant direct write access to `main`.

Signing, notarization, clean-Mac installation and Paper 72-hour/7-day soak are separate binary or
operational acceptance work. They are **not prerequisites for source contributions**. They remain
unverified where [STATUS](docs/STATUS.md) says so and must not be advertised as completed.

## Licensing

No repository-wide LICENSE/LICENCE or NOTICE file is currently tracked. Only
[`quine-core`](crates/quine-core/Cargo.toml) declares `MIT`; Python and application package metadata
do not declare a license. The crate declaration is not a repository-wide licensing decision.
The maintainer must confirm project terms and the corresponding copyright notice before this
repository can present a consistent open-source license. Existing declarations remain unchanged.
Dependency license fields in the npm lockfiles are third-party metadata, not the project license.

[Current state](docs/STATUS.md) · [Mac development and workspace rules](docs/desktop-macos.md) ·
[Engineering workflow](docs/governance/WORKFLOW.md) · [Desktop boundary](docs/adr/0050-desktop-only-product-delivery.md)
