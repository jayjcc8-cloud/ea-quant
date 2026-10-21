# QUINE local Mac desktop

The desktop is QUINE's sole supported user entry point, release artifact and final product
acceptance target ([ADR 0050](adr/0050-desktop-only-product-delivery.md)). This is an unpublished
local candidate; no public desktop download is available. Independent browser/server/mobile
products are outside support. Shared Web code and internal test tools remain in use.

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
- `settings.json` is the existing model settings format. It starts absent. Resources → Model connections
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
owned-core failure and recovery. It makes zero model requests. Final installed-desktop acceptance owns native window/first-use/quit, workspace ownership,
WebView navigation, focus/keyboard, IME and text scaling. Shared business/component/API checks
are reused according to risk, not duplicated into a second desktop business suite. The installed
API check does not imply native success. Native narrow-window, full keyboard and Chinese IME
acceptance remain unverified; use the [first-use card](first-use-acceptance.md).

The desktop grants no shell/filesystem Tauri plugins or frontend IPC capabilities, denies popup
windows and navigation outside its own session origin, and retains the existing Host/Origin/
request-header checks. Its HTTP listener reserves a kernel-selected loopback port before starting
ASGI; it never probes or reuses a pre-existing research port. A private short IPC socket avoids the
macOS Unix-path length limit while the explicitly configured durable core root remains stable.
