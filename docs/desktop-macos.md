# QUINE local Mac desktop

This bounded desktop candidate supports **Apple Silicon macOS**. It is a Tauri 2 window over the
existing React application, with a bundled CPython 3.12.13 distribution, locked Web dependencies,
the current installed EA wheel, and a freshly compiled Rust experiment owner. Python, Node, Cargo,
a checkout and a virtual environment are not required to run the installed application.

This is a **local technical package**, with an ad-hoc signature only. It is not Developer ID signed
or notarized, and Gatekeeper acceptance on a downloaded copy has not been established. Do not
change macOS security settings to run it. Public distribution needs authorized Apple signing and
notarization as a separate step. Intel, Windows and Linux packages are outside this slice.

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
- `settings.json` is the existing model settings format. It starts absent. The settings screen
  shows its exact path and provides provider, model ID, Base URL and write-only API key inputs.
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
exclusive lock at startup. An unavailable target returns to the default workspace with a visible
notice; it never migrates or deletes old data. Cancel pending selection keeps the current root.
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

Quit with Command-Q or close the last window. The desktop closes its session pipe and waits for
its backend; backend exit closes the core's liveness pipe. Interrupted experiments retain the
existing owner recovery/unknown semantics on reopen. Exiting is not an assertion that an active
research job succeeded or was durably cancelled. A crash uses the same owner-loss boundary; work
is never automatically resent. Python strategy plugins remain trusted executable local code,
not sandboxed untrusted code.

## Local build and acceptance

From the selected source checkout, with the repository's local Rust toolchain and locked Python
build environment available:

```sh
venv/bin/python scripts/build_desktop.py --output "$PWD/.local/desktop-settings"
```

Build outputs live under the explicitly selected output root (default `<checkout>/.local/desktop/`). The resulting app is at
`target/release/bundle/macos/QUINE.app`; the convenient local transport archive is
`QUINE-macos-arm64.zip`. The build uses the public npm/crates registries and uv's official
python-build-standalone distribution, leaves the global toolchains untouched, and never reads a
project `.env`. It rebuilds React, the wheel and Rust core from the current source. The runtime
payload is explicitly limited to installed Python, core, built UI and two fixture files.

The build keeps release stripping disabled: this host's macOS 27 dyld rejected stripped Rust
proc-macro libraries with `mis-aligned LINKEDIT string pool`. This affects build size, not runtime
authority. The package preserves dependency license metadata. No third-party source tree,
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
owned-core failure and recovery. It makes zero model requests. Native window/first-use/quit
verification is additional to this API-level acceptance, not implied by it.

The desktop grants no shell/filesystem Tauri plugins or frontend IPC capabilities, denies popup
windows and navigation outside its own session origin, and retains the existing Host/Origin/
request-header checks. Its HTTP listener reserves a kernel-selected loopback port before starting
ASGI; it never probes or reuses a pre-existing research port. A private short IPC socket avoids the
macOS Unix-path length limit while the explicitly configured durable core root remains stable.
