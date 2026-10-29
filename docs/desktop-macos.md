# QUINE local Mac desktop

The desktop is QUINE's sole supported user entry point, release artifact and final product
acceptance target ([ADR 0050](adr/0050-desktop-only-product-delivery.md)). This is an unpublished
local candidate; no public desktop download is available. Independent browser/server/mobile
products are outside support. Shared Web code and internal test tools remain in use.

This bounded desktop candidate supports **Apple Silicon macOS**. It is a Tauri 2 window over the
existing React application, with a bundled CPython 3.12.13 distribution, locked Web dependencies,
the current installed EA wheel, and a freshly compiled Rust experiment owner. Python, Node, Cargo,
a checkout and a virtual environment are not required to run the installed application.

The ordinary bundled build is a **local technical package**, with an ad-hoc signature only. It is not Developer ID signed
or notarized, and Gatekeeper acceptance on a downloaded copy has not been established. Do not
change macOS security settings to run it. Public distribution needs authorized Apple signing and
notarization as a separate step. Intel, Windows and Linux packages are outside this slice.
These binary distribution checks are not prerequisites for GitHub source collaboration. The
development and local-candidate modes below do not invoke distribution signing or archiving.

## Open and keep your research

Keep `QUINE.app` intact and open it normally. No terminal or model key is required to save an idea
or run the bundled synthetic scenario. The scene `synthetic-bounded-long.yaml` is fixture data for
checking the application, not real market evidence. Select it explicitly when binding a strategy,
preview the experiment, then confirm one run. AI remains beside the strategy; absent model
configuration is shown honestly and cannot send a request.

The stable data root is `~/Library/Application Support/QUINE`:

- `workspace/` contains the existing Python draft/configuration projections and uploaded inputs.
- `core/` contains the existing Rust SQLite owner and its confirmed inputs and official reports.
- `scenarios/` contains user-editable registered scenarios. First use seeds the two synthetic
  sample files once; an upgrade never overwrites them.
- `settings.json` is the existing model settings format. It starts absent. Native v3's model page
  reuses the desktop editor to create/edit/cancel/save named connections, including provider,
  model ID, Base URL and write-only API key inputs. Set a workspace default here; in the research
  assistant, explicitly choose a connection or follow that default. Existing fixed research
  selections do not change when the default changes. Historical calls and approved rounds keep
  their recorded connection identity.
  Save checks local format only; it never authenticates or calls a provider. Credentials are
  stored in this existing file with mode 0600, not in WebView storage. Blank key retains the
  current key; changing provider/endpoint requires re-entry or explicit clearing. No developer
  environment/key is inherited. HTTP endpoints are limited to loopback; remote endpoints use HTTPS.
- `logs/desktop.log` and `logs/core.log` contain local lifecycle diagnostics. No environment or
  settings documents are logged.

Replacing or moving the application leaves these files in place. Reopen the same root to recover
saved strategies and reports. No automatic updater or background daemon is installed. The optional
`QUINE_DATA_ROOT` environment variable is an explicit absolute root override for controlled local
acceptance; the ordinary app uses the default above. Settings displays the current root and accepts
an explicit absolute path to an existing empty directory or an existing QUINE workspace. Save
records a next-launch selection only. Wait for active work to finish, quit, then reopen; interrupted
work retains the owner's unknown/recovery semantics. The application rechecks the target and its
exclusive lock at startup. An unavailable target, including a failure before its Web service is
ready (for example an unwritable log), returns to the default workspace after cleanup with a visible
notice. The existing Settings screen can cancel the pending selection or select another directory.
The failed selection remains recorded until the user changes it; neither data nor permissions are
changed for recovery. A failure after readiness, or an uncertain shutdown, remains fatal and never
silently switches workspaces. Recovery makes only one attempt; it never migrates or deletes old
data. The embedded Web task translates nonzero framework startup exits into the same session
failure path before they can terminate the event loop; normal return, normal exit, cancellation and
signals retain shutdown semantics. Cancel pending selection keeps the current root.
There is no multi-workspace manager or native directory-picker permission.

`desktop-preferences.json` in the original default/override root stores the next workspace and the
existing light/dark/system theme. These survive random-port changes and restart. Model settings
stay with their workspace. Unsaved form changes and cancellation do not write anything. Only the
theme is exposed in the desktop appearance section; no language toggle claims incomplete translation.
Insecure/unreadable existing settings are refused rather than changing user file permissions.
Never choose a source checkout, application bundle or symlink as a data root.

A second launch using the same root is refused with a recoverable error window; it does not
connect to, replace or stop the first session. Quit the extra window. Startup/runtime failure shows
an error page with a log location; quit and reopen after correcting the problem. There is no silent
fallback to another service or experiment owner.

Quit with Command-Q or close the last window, then confirm. The research core first closes new
dispatch admission and cancels its own unfinished work through the existing stop barrier. The
desktop waits for whole-process-group stop proof, durable terminal readback and local Paper
shutdown before closing the core's liveness pipe. Unconfirmed stop or persistence keeps the
window open for an explicit retry; it never reports successful shutdown or redispatches work.
Completed results remain completed. Existing historical unknown records remain frozen and do
not prevent an otherwise idle session from closing; quitting does not recover or release them.
A crash before this handshake retains the owner-loss/unknown boundary on reopen. Work is never
automatically resent. Python strategy plugins remain trusted executable local code, not
sandboxed untrusted code.

## Local build and acceptance

The first real v3 slice opens at `/studio` in the native window. It reuses the existing
research, CSV, experiment-preview/confirmation and official report services. Saving and applying
a proposal do not run an experiment. Unknown confirmation receipts retain their request identity
and offer reads only. Simulation, Live, batches, holdout and Candidate promotion are unavailable
in this entry. Model connections can be configured here; saving does not test authentication.
Return to a research and explicitly send a message to authorize one model call and zero backtests.
The desktop has one normal entry;
`/strategies` remains a migration fallback without a parallel navigation link. The standalone
offline studio remains a synthetic design preview and is not the desktop candidate. The v3
research/report header, detail layout and assistant surface are shared with that design, while
all displayed records in `/studio` come from existing service contracts.

### Source build prerequisites

Clone current `main` for the native v3 slice, or the exact PR revision you intend to test.
From the selected checkout, prepare:

- Apple Silicon macOS and Apple's Xcode Command Line Tools (including `clang` and
  `install_name_tool`); see [Tauri's macOS prerequisites](https://v2.tauri.app/start/prerequisites/#macos).
- Git, Python 3.12, global `uv` at the exact version in [`pyproject.toml`](../pyproject.toml)
  (currently 0.11.28), and Node.js 24/npm as used by CI.
- Stable Rust via rustup. [`build_desktop.py`](../scripts/build_desktop.py) explicitly uses
  `.local/rust/cargo` and `.local/rust/rustup`; a toolchain installed only in the usual home
  directories will not populate those locations.

For a fresh checkout, install the local Rust toolchain using the
[official rustup installer and custom homes](https://rust-lang.github.io/rustup/installation/index.html#choosing-where-to-install).
Run in the repository root; these settings apply to the current shell:

```sh
export CARGO_HOME="$PWD/.local/rust/cargo"
export RUSTUP_HOME="$PWD/.local/rust/rustup"
export PATH="$CARGO_HOME/bin:$PATH"
mkdir -p .local/rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o .local/rust/rustup-init.sh
sh .local/rust/rustup-init.sh --profile minimal --default-toolchain stable --no-modify-path -y
python3 scripts/bootstrap_local.py --web
venv/bin/ea doctor
```

Use Python 3.12 for `python3`. Existing local toolchains need no reinstall. The build downloads
locked dependencies and CPython 3.12.13, builds the shared UI and Rust core, and stages its own
runtime. It requires network access to the public dependency sources, not a provider API key.
No Apple Developer account is required for the development mode below.

For a local native development candidate without signing or creating a distribution archive:

```sh
venv/bin/python scripts/build_desktop.py --development --output "$PWD/.local/desktop-dev"
QUINE_DATA_ROOT=/absolute/path/to/fresh-acceptance-data \
  .local/desktop-dev/development/QUINE-Development.app/Contents/MacOS/quine-desktop
```

Replace the data-root placeholder with a fresh absolute directory outside the checkout and app.
Use a different build output if that development candidate is already running. The window opens
the real `/studio` entry. Saving research and a bundled synthetic experiment require no model key.

Keep the development directory and its `Resources/runtime` target together. The development
wrapper has only enough metadata for macOS window identification and points to the build runtime;
it is not an installable or distributable package. It uses the native close/Command-Q confirmation
and cooperative shutdown. The build below remains the separate app-bundle path.

For a self-contained **local candidate** without invoking signing, notarization or archiving:

```sh
venv/bin/python scripts/build_desktop.py --local-candidate --output "$PWD/.local/desktop-first-use"
```

This stages `candidate/QUINE-Local.app` with the same native shell and a private copy of the
runtime inside `Contents/Resources/runtime`. Move the intact app independently of the checkout;
the build directory is not required to run it. The build refuses an existing candidate rather
than overwriting an app that may be open. Its separate bundle identifier distinguishes it from
the development wrapper, but the normal data-root rules remain unchanged: select an isolated
root for acceptance and never open two instances on one workspace. In v3, click **本地研究空间**
to inspect or explicitly change the next-launch workspace using the existing settings contract.

This mode does not establish Developer ID signing, notarization, downloaded-copy Gatekeeper
acceptance or clean-Mac first use. Existing toolchain-produced binary signatures are not a
distribution signature. If macOS refuses the app, leave that refusal intact; do not change
quarantine attributes or security settings. Record source revision, `source_dirty` and `build_id`
from the bundled `build-info.json` with any acceptance results.

From the selected source checkout, with the repository's local Rust toolchain and locked Python
build environment available:

```sh
venv/bin/python scripts/build_desktop.py --output "$PWD/.local/desktop"
```

Build outputs live under the explicitly selected output root (default `<checkout>/.local/desktop/`). The resulting app is at
`target/release/bundle/macos/QUINE.app`; the convenient local transport archive is
`QUINE-macos-arm64.zip`. The build uses the public npm/crates registries and uv's official
python-build-standalone distribution, leaves the global toolchains untouched, and never reads a
project `.env`. It rebuilds React, the wheel and Rust core from the current source. The runtime
payload is explicitly limited to installed Python, core, built UI and two fixture files.

The build keeps release stripping disabled: this host's macOS 27 dyld rejected stripped Rust
proc-macro libraries with `mis-aligned LINKEDIT string pool`. This affects build size, not runtime
authority. Installed Python dependency metadata is retained; the repository currently has no
consolidated third-party NOTICE file. This build description is not a completed licensing review.
No third-party source tree,
private settings, research data, development logs or screenshots are copied into the bundle.

Run the installed lifecycle acceptance using a **fresh** absolute root, outside the app:

```sh
venv/bin/python scripts/accept_desktop.py \
  --runtime "$PWD/.local/desktop/target/release/bundle/macos/QUINE.app/Contents/Resources/runtime" \
  --root "$PWD/.local/desktop/installed-acceptance"
```

It uses an empty HOME and `/usr/bin:/bin` PATH, launches the installed interpreter, refuses a
second session, checks foreign-Origin rejection, confirms a synthetic strategy through the Rust
owner, reads the real official report, closes/reopens and compares persisted evidence, then checks
owned-core failure and recovery. It makes zero model requests. Final installed-desktop acceptance owns native window/first-use/quit, workspace ownership,
WebView navigation, focus/keyboard, IME and text scaling. Shared business/component/API checks
are reused according to risk, not duplicated into a second desktop business suite. The installed
API check does not imply native success. Scoped native keyboard/IME, minimum-window and text-scaling
checks passed for the frozen technical package; [PR #261](https://github.com/jayjcc8-cloud/ea-quant/pull/261)
records the exact identity and limits. Independent first use remains separate; use the
[first-use card](first-use-acceptance.md).

The desktop grants no shell/filesystem Tauri plugins or frontend IPC capabilities, denies popup
windows and navigation outside its own session origin, and retains the existing Host/Origin/
request-header checks. Its HTTP listener reserves a kernel-selected loopback port before starting
ASGI; it never probes or reuses a pre-existing research port. A private short IPC socket avoids the
macOS Unix-path length limit while the explicitly configured durable core root remains stable.


## Explicit unknown disposition

New dispatches retain local execution-machine, OS boot and database-file identity in the
core transaction before spawning. A core-confirmed unknown can prepare recovery only when
that original machine and workspace are still verifiable. Preparation retains the attempt
and its revision; it neither cancels nor releases it. The user must then quit QUINE and
restart the same machine. A later explicit release rechecks the trusted boot identity,
original machine/file, attempt and fence in the single-owner store transaction.

Release only changes concurrency occupancy. The outcome remains `unknown`; frozen input,
original intent/job/attempt, dispatch consumption and model reservations remain. Old intent
replays cannot spawn; late results or cancellation cannot reactivate a released unknown.
A new experiment requires a new explicit preview and confirmation. If cancellation changed
the record after preparation, a fresh explicit preparation and another OS restart are needed.
Application restart, a missing PID, an idle lock or client-supplied boot claims are not proof.

Legacy records without execution origin remain held. Migration does not invent that origin.
Copied/restored/moved databases and unavailable host evidence also fail closed. The user's
existing databases are not part of isolated development acceptance. Injected cross-boot tests
prove decision rules only; a real cross-boot native acceptance is still required before claiming
that journey has been verified on a Mac.

## Read-only distribution preflight

`scripts/check_desktop_distribution.py APP --expected-source-sha SHA --expected-version VERSION
--expected-team-id TEAM` checks build identity, clean-source metadata, bundle version, nested
signature integrity, the main executable's hardened-runtime flag, the supplied Developer ID
team and an existing stapled notarization ticket. It neither signs nor uploads anything.
A missing team, ad-hoc signature, missing proof or failed command is a failure, not a skipped
pass. Passing this check is not release authorization or proof of the research/Full/clean-Mac
installation journeys. The default local build remains an ad-hoc technical package.

Interactive research grants now authorize at most 24 requests per new task or explicit
continuation. The interface states the new-message allowance and the persisted task's actual
cumulative authorization, used and remaining counts. Historic approvals and idempotent grant
replays retain their original values. Request count is not a monetary cap.

## Native CI checks

Two native jobs extend [path routing](../CONTRIBUTING.md#required-checks) without changing
existing job IDs or the shared E2E policy:

- `rust_core` runs on `ubuntu-latest` when `crates/quine-core/**` (non-Markdown), the root
  `Cargo.toml`/`Cargo.lock`, `rust-toolchain*`, `.cargo/**`, `scripts/build_desktop.py`,
  `scripts/ci_routes.py` or `.github/workflows/ci.yml` changes. It installs a minimal stable Rust
  toolchain and runs
  `cargo test --locked --manifest-path crates/quine-core/Cargo.toml --lib --test store --test cli --test canonical_identity`.
  These are the unit, store, CLI and canonical-identity contract tests. `tests/stage2.rs` is the
  real-engine Python journey and is deliberately excluded; this job does not claim to run the
  complete Rust integration suite and needs no Python environment.
- `macos_shell` runs on the official `macos-15` Apple Silicon standard runner when
  `apps/desktop/**` (non-Markdown), the root Rust inputs above, `scripts/build_desktop.py`,
  `scripts/ci_routes.py` or `.github/workflows/ci.yml` changes. It asserts `uname -m` is `arm64`,
  installs minimal stable Rust and runs
  `cargo check --locked --manifest-path apps/desktop/src-tauri/Cargo.toml`. This is a compile-only
  check: it does not build an installable package, launch the app, link the Python runtime or sign
  anything. The runner label table is authoritative:
  <https://docs.github.com/en/actions/reference/runners/github-hosted-runners> lists `macos-15` as
  arm64 and the Intel label separately as `macos-15-intel`.

Both jobs use paths under `.local/` for cargo targets, set a bounded timeout, keep
`persist-credentials: false` with the same exact event checkout as the other jobs, and fail rather
than continue on error. Markdown, `docs/**`, ordinary `src/ea/**` Python, `apps/web/**` React,
plain Python tests and other existing workflows do not trigger them. Unknown paths, an empty diff
and unsupported Git statuses route conservatively to both checks. Neither native job adds a cargo
cache action, artifact hash, packaging, signing, publication, frontend build or second E2E suite.
