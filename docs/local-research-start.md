# Start the local research application

One guided entry replaces the four-command Quick Start, and it is the one place this repository's
own start path asks for a provider key. It runs on the `python3` macOS already has, uses the
repository's own pinned toolchain and locked sync, builds the UI, and then starts the existing
`ea web serve` on loopback.

```bash
python3 scripts/start_research.py --check               # read prerequisites, create nothing
python3 scripts/start_research.py --offline             # run with no model provider at all
python3 scripts/start_research.py --provider deepseek   # deepseek; key asked for after setup
python3 scripts/start_research.py                       # asks which of those two, then starts
```

`--check` is a reading, not a setup: it runs no step, creates no workspace, writes no document and
asks for nothing. It prints the project root, the workspace, the scenario root, the UI build, the
session store, the port, the model selection, the resolved location of each prerequisite, and the
exact steps a launch would run. Run it first; if it disagrees with what you expect, nothing has
happened yet.

The provider mode uses the one profile this repository has run against, `deepseek`, and `--model`
defaults to `deepseek-flash`, the id last verified here. Selecting it says nothing about whether a
request will succeed: the launcher makes no model request at all, and the application's own
settings status reports `request_validation: not_verified`, because a configured key is not a
proven one. A model provider outside this launcher's two modes is configured by hand in a settings
document, which the backend validates; see
[the local product](radian-local-product.md#model-provider-configuration).

## What a launch does, in order

1. Validates the workspace and the port (below) before anything touches the disk.
2. Checks that `uv`, `node` and `npm` are on `PATH`. A missing one is named with the remedy this
   repository documents for it, and nothing is created, installed or started.
3. Asks which mode this session runs in — offline, or `deepseek` — unless `--offline` or
   `--provider` already decided it. Nothing has been created when this question is asked, so
   stopping there leaves the host exactly as it was. A bare return key is the offline mode, and
   `--check` reports the question instead of asking it.
4. Creates the locked environment with `scripts/bootstrap_local.py`. `uv` supplies the interpreter
   the project declares, so the `python3` you happen to have never decides what the project runs
   on — this script itself needs nothing beyond the standard library.
5. Adds the `dev` and `web` extras with `uv sync --locked`.
6. Runs `npm ci` and `npm run build` in `apps/web`.
7. Asks for the provider key — provider launches only — and starts `venv/bin/ea web serve` with the
   scenario root, the workspace and `apps/web/dist` as three separate roots and `--port` on the
   fixed loopback host. `--host` is never passed.

Nothing else is installed, and a mode that was refused or never chosen creates nothing at all.
Neither the launcher nor starting the server makes a model request: one happens only when you
start research in the application.

## The workspace

The workspace holds job, attempt and report data. It must not overlap the repository that serves
it — or the scenario root or the built UI inside it — compared by real path, so a workspace that
only looks separate because a symlink points into the source tree is refused too. The default is
`~/ea-web-workspace`; pass `--workspace DIR` to state your own.

Each launch opens a **new** session settings document at
`<workspace>/.ea-start-research/sessions/<session-id>/settings.json`:

- it is created exclusively (`O_EXCL`) with mode 0600, so an existing document is refused, never
  replaced, and a relaunch never reads or rewrites the last session's document;
- it contains no secret: `api_key` is null and `api_key_env` names the variable the key arrives in
  (see below);
- a settings directory that is a symlink is refused rather than followed;
- your own settings store — `~/.config/radian/settings.json`, or wherever `RADIAN_SETTINGS_FILE`
  points in your shell — is never read, never written and never copied. The server child is given
  this session's document explicitly.

The document's shape is the one documented in
[the local product](radian-local-product.md#model-provider-configuration).

## The key

- It is typed at a real terminal with `getpass`, after setup has succeeded — so it is never held
  across a slow install, and it is never echoed.
- With no terminal available the launcher refuses instead of falling back to an echoed read, and
  tells you to run it from a terminal or to pass `--offline`.
- A terminal that cannot turn echo off is refused the same way: `getpass` announces that case with
  a warning and then reads the key in the clear, so that warning is treated as a failure, no key is
  read, and the launch stops with a message naming the reason.
- A blank entry is re-asked; EOF and Ctrl-C end with a message, not a traceback and not a
  half-entered key.
- It reaches exactly one process, as one environment variable in the server child
  (`RADIAN_MODEL_API_KEY`). It is not an argument, not a file, not a log line, and not any other
  step's environment.
- Every child environment is built from a short whitelist (`PATH`, `HOME`, `LANG`, `LC_ALL`,
  `TERM`, `TMPDIR`, `USER`, `SHELL`, and the Windows equivalents). A provider key already exported
  in your shell, a stale `PYTHONPATH` or `VIRTUAL_ENV`, and any `RADIAN_SETTINGS_FILE` you hold are
  therefore not inherited by any step.
- `--offline` writes no `model` object at all, asks for nothing and cannot make a model request
  even when your shell is fully configured. A configured parent never leaks into it.

## Guided upload of your own CSV

The controlled data upload panel has two editors over one declaration.

**Guided.** Choose the upload mode "Explicit mapping of my own file", then choose a destination for
each of the strict profile's fifteen fields. The column choices are your file's own header row and
nothing else: no data row is read and no value is inferred. The six metadata fields may instead be
a constant you state; the three timestamp fields each name one exact form from this host's closed
allowlist, and the time zone is stated by you, never guessed. Nothing is pre-filled.

**Advanced.** The same declaration is editable as JSON, and choosing that editor ("Advanced JSON")
is explicit. There is one declaration, not two: the guided controls are read from the JSON text and
write back into it, so a column stated as JSON is the column the controls show, a guided change
never reverts it, and neither editor can overwrite the other. A declaration the controls cannot
represent — a destination outside the strict profile, a constant on a field that carries an
observation, a timestamp form outside the allowlist, a field named as both a column and a constant
— keeps the JSON editor and shows the reason, instead of dropping or re-pointing anything. Under
the simulated policy the availability destination is shown as derived and is not an editable value
the panel would then ignore.

What the panel does not do, deliberately:

- A header that repeats a name or leaves a column unnamed yields no column choices at all, with
  the reason shown: no name is invented for an unnamed column.
- A header with a quote inside an unquoted name (`clo"se"`) or with characters right after a
  closing quote (`"close"x`) is refused as a whole, again with no column choices: reading either one
  as a name would offer a column the file does not have. Quoted commas, doubled quotes and CRLF
  endings are read as the file writes them.
- A byte-order mark is never silently stripped from a name. The names are read as if it were not
  there, the mark is stated on screen, and the bytes sent are still your file's own — so the host
  refuses the source (`invalid_encoding`) rather than the page repairing it.
- A destination chosen for a column your next file no longer has stays exactly as you chose it, is
  marked "not in this file", and is still sent. The host refuses it (`mapping_column_missing`);
  nothing is re-pointed for you.
- Under the simulated availability policy no availability column or form is declared, because the
  policy derives availability itself and declaring one would contradict it.
- Changing the file, any choice, the time declaration or the policy clears the preview. A version
  is published only from a preview of the exact declaration on screen that accepted the whole
  source; the host is the only authority on whether a declaration is acceptable.
- A pasted source is sent as the text the panel holds, which is what a textarea's own value is:
  pasting CRLF text gives LF there. A file selected with the file control is sent as that file's own
  bytes, base64-encoded, so its line endings, its mark and its quoted header reach the host exactly
  as written.

A published version is never reported as verified point-in-time availability (`pit_verified` is
always false for an upload), and the source and licence strings are your assertion, not a legal
verification.

## Previewing the guided panel without a host

`apps/web/preview/first-use.html` mounts the real, unchanged panel over the in-memory scripted
fixtures in `apps/web/preview/first-use-fixtures.ts` — a handful of synthetic CSVs and a finite set
of hand-written host answers. It fetches nothing (an `/api/` request throws in the page), starts no
engine and makes no model call. Serve it with the preview's own Vite config, which sets no proxy:

```bash
cd apps/web && npx vite --config preview/vite.config.ts --port 8901
# http://127.0.0.1:8901/first-use.html?state=guided
```

The fixtures contain no CSV reader, no mapper and no validator: an answer is selected by name, and
the real host remains the authority the integration tests exercise.

A small scripted-preview banner sits above the panel. It is not sticky, so it stays out of a narrow
capture, and it labels the page as scripted wherever it is in view. Its state picker and its
`390px`/`1280px` buttons navigate on this page, so the banner and the state stay on screen; the
`390px` width caps the wrapper to the width available rather than fixing a box that overflows. One
more button loads the selected state's synthetic CSV and opens the mapped editor — nothing else is
filled in, because every choice, the zone and the forms are the user's on purpose.

## What this document does not establish

The launcher's own behaviour, the guided controls and the page above are covered by tests that run
no dependency install, no build, no server and no provider. What remains unverified by this
document is the thing only real use shows: a legal real market-data CSV from your own vendor, your
actual key, and an actual model request. Report those honestly rather than assuming them.

Related: [contributing](../CONTRIBUTING.md) for the repository's own local environment and required
checks, [the local product](radian-local-product.md) for the workspace and settings surfaces.
