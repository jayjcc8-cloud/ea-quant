# Start the local research application

One guided entry replaces the four-command Quick Start, and it is the one place this repository's
own start path handles a provider key. It runs on the `python3` macOS already has, uses the
repository's own pinned toolchain and locked sync, builds the UI, and then starts the existing
`ea web serve` on loopback.

```bash
python3 scripts/start_research.py --check               # read prerequisites, create nothing
python3 scripts/start_research.py --offline             # run with no model provider at all
python3 scripts/start_research.py --provider deepseek   # deepseek; key asked for after setup
python3 scripts/start_research.py                       # asks which of those two, then starts
python3 scripts/start_research.py --provider deepseek --project-env   # key read from this .env
```

`--check` is a reading, not a setup: it runs no step, creates no workspace, writes no document,
asks for nothing and opens no `.env`. It prints the project root, the workspace, the scenario root,
the UI build, the session store, the port, the model selection, the key source a launch would use,
the resolved location of each prerequisite, and the exact steps a launch would run. Run it first; if
it disagrees with what you expect, nothing has happened yet.

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
7. Resolves the provider key — provider launches only: from the project's own `.env` when
   `--project-env` was given and that file names a value, otherwise typed at the terminal — and
   starts `venv/bin/ea web serve` with the scenario root, the workspace and `apps/web/dist` as
   three separate roots and `--port` on the fixed loopback host. `--host` is never passed.

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

By default the key is typed at a real terminal, and this script stores nothing:

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
  (`RADIAN_MODEL_API_KEY`). It is not an argument, not a log line, and not any other step's
  environment, and this script never writes it to a file — the opt-in `.env` below is a source it
  may read, never one it creates or changes.
- Every child environment is built from a short whitelist (`PATH`, `HOME`, `LANG`, `LC_ALL`,
  `TERM`, `TMPDIR`, `USER`, `SHELL`, and the Windows equivalents). A provider key already exported
  in your shell, a stale `PYTHONPATH` or `VIRTUAL_ENV`, and any `RADIAN_SETTINGS_FILE` you hold are
  therefore not inherited by any step.
- `--offline` writes no `model` object at all, asks for nothing and cannot make a model request
  even when your shell is fully configured. A configured parent never leaks into it.

### Reading the key from this project's own `.env` (opt-in)

`--project-env` replaces that prompt with the one value in the repository's own `.env`. It is
opt-in, and explicit, because a file is a place a key can be left behind:

```bash
python3 scripts/start_research.py --check --provider deepseek --project-env   # names the source only
python3 scripts/start_research.py --provider deepseek --project-env           # reads it, then starts
```

- It is valid only beside a provider already chosen on the command line — `--provider deepseek`, or
  `--model ID`, which chooses the same provider. With `--offline`, or on a launch that has not
  chosen a provider yet, it is refused before anything is created, asked or read.
- The only path it can read is `<project root>/.env`, derived from the script's own location. There
  is no `--env-file` argument, and no search of the working directory, your home directory or
  anywhere else: the *application* still reads no `.env` at all (ADR 0005), and this launcher reads
  one file only because you named the flag.
- Source precedence is simple: `--project-env` is the source; the file is read after every setup
  step has succeeded and immediately before the server child starts. A key exported in your shell
  is never a fallback and never wins.
- **A missing file, a missing variable, or an empty value is not an error.** The launch says so in
  one line, naming the path and no value, and asks at the terminal exactly as it would have.
- **A file it cannot take literally is refused**, naming the path and nothing out of the file: the
  variable assigned twice, a line that is not a `NAME=value` assignment, an `export NAME=value`
  line, an unquoted `#` in a value, a quote that never closes, a value that spans lines or holds a
  control character, text that is not UTF-8, a symlink, anything that is not a regular file — a
  FIFO included — a file larger than 64 KiB, or a file this user cannot read. It never falls back
  to the prompt and never takes half a value.
- **Only `RADIAN_MODEL_API_KEY` is read, and only in one plain literal form.** A line is blank, a
  full-line comment (`# …`), or an assignment `NAME=value` on that one line. An assignment to any
  other name is left alone. Ordinary spaces and tabs around the assignment and around the value are
  not part of the value, a trailing CR is a CRLF file's line ending, and the value is the rest of
  the line with one matching pair of single or double quotes removed. Nothing is expanded, escaped
  or executed, so the text in the file is the text of the key — `${…}` is those characters, not a
  variable. This is not a dotenv implementation, and the forms it does not read are refused instead
  of guessed at: `export NAME=value` is refused rather than passed over, an inline comment
  (`NAME=value # note`) is refused rather than left inside the key — write the whole value in quotes
  if it really contains a `#` — and a value continued onto a second line is refused rather than
  joined. Any other control character in a line — beside the value, around the assignment, or a tab
  inside the value itself — is refused rather than trimmed away.
- The value is used in the server child's environment and nowhere else — never in bootstrap, sync,
  the dependency preflight, the UI build, an argument, the session document (which names the
  variable, never the value) or any log line.
- **The launcher reads `.env`; it never creates, edits, truncates or deletes it.** Saving a real
  credential there is yours to do by hand, in your own editor, and it is the one step this document
  deliberately does not script: a key typed as a command argument ends up in your shell history, and
  appending a second line to a file that already assigns the variable is the duplicate assignment
  refused above. So open or create the file in the editor you use — on macOS, `open -e .env` — and
  save the value there yourself, as that file's single line:

  ```
  RADIAN_MODEL_API_KEY=your-key-here
  ```

  `.env` is already covered by this repository's `.gitignore`, so check `git check-ignore .env`
  before you put anything in it.
- A key being present is not authentication. The application's own settings status reports
  `request_validation: not_verified`, because a configured key is not a proven one; and starting
  the launcher — from the file or from a typed key — approves no research request. A request
  happens only when you start research in the application.

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
no dependency install, no build, no server and no provider. The `--project-env` path is covered the
same way, over synthetic files in temporary directories only: no test reads this repository's own
`.env`. What remains unverified by this document is the thing only real use shows: a legal real
market-data CSV from your own vendor, your actual key, and an actual model request. Report those
honestly rather than assuming them.

Related: [contributing](../CONTRIBUTING.md) for the repository's own local environment and required
checks, [the local product](radian-local-product.md) for the workspace and settings surfaces.
