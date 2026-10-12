#!/usr/bin/env python3
"""Guided local research startup: one entry, one explicit check, one transient key.

The Quick Start is four commands a user has to know, and one of them is where a
provider key is easiest to handle carelessly. This script is the single entry that
replaces them: it checks what it needs, uses the repository's own pinned toolchain
and locked sync, builds the UI, and then starts the existing `ea web serve`.

Three properties are deliberate and each is enforced below rather than described:

* It runs on the `python3` a stock macOS already has. Nothing outside the standard
  library is imported here, and every 3.12-only step is handed to the repository's
  own locked environment through `uv`.
* It never picks a key up from the environment: the child process environment is
  built from a short whitelist, so an exported provider key and a stale
  `PYTHONPATH`/`VIRTUAL_ENV` cannot reach any step. By default the one key this
  script handles is typed by the user at a real terminal, after setup has already
  succeeded, and is handed to the server child alone. An explicit `--project-env`
  replaces that prompt with the single value this project's own `.env` names — read
  at the same point, used in the same one child, and never copied, printed or
  written anywhere. The application itself still never reads a `.env`; the value
  becomes one environment variable in one child, which is what ADR 0005 asks for.
* Each launch opens a new, exclusive, non-secret session settings document under
  the workspace. The document names the provider and the *name* of the environment
  variable the key arrives in; it never contains the key. A user's own settings
  store is never read, never written and never copied.

`--check` reads prerequisites and prints the plan. It creates nothing, installs
nothing, builds nothing, starts nothing, asks for nothing and opens no `.env`: the
key source it names is where a launch *would* read, not something it reads.

One launch runs in one of two modes: offline, or the one provider profile this
repository has validated end to end. A mode is chosen by `--offline` or
`--provider`, and a launch that was given neither asks at the terminal, before
anything is created, so a user who stops at that question has a host exactly as
they left it. Choosing the provider mode says nothing about whether a request
will succeed: this script verifies no model request, and the app's own status
view is what reports whether a request can be made.

Usage is documented in `docs/local-research-start.md`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# The child environment is built from this whitelist. It carries what a build and
# a loopback server need to work, and nothing else: no exported credentials, no
# `PYTHONPATH`, no `VIRTUAL_ENV`, and no settings pointer the parent happened to
# hold. Anything a step genuinely needs is added explicitly, by name, at the step.
CHILD_ENV_NAMES = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "USER",
    "SHELL",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)

# Prerequisites, each with the remedy this repository already documents. A tool is
# only ever looked up, never installed: this script runs no package manager of its
# own and no dependency install beyond the locked project setup.
UV_GUIDANCE = "https://docs.astral.sh/uv/getting-started/installation/"
NODE_GUIDANCE = "https://nodejs.org/en/download"
TOOL_GUIDANCE = {"uv": UV_GUIDANCE, "node": NODE_GUIDANCE, "npm": NODE_GUIDANCE}
TOOL_NAMES = ("uv", "node", "npm")

# The Python the project declares (`requires-python` in pyproject.toml), handed to
# `uv` so the version a user happens to have on PATH never decides what runs.
SUPPORTED_PYTHON = "3.12"
BOOTSTRAP_SCRIPT = "scripts/bootstrap_local.py"
LOCKED_EXTRAS = ("dev", "web")
# The one check that reads the locked environment rather than describing it. It is
# a short program so that a failure is one import error, not a stack of them, and
# it reads dependencies only: no settings document, no workspace, no key.
WEB_PREFLIGHT_SOURCE = "import ea.web.server as server; server.check_web_dependencies()"
SCENARIO_ROOT_PARTS = ("examples", "web-scenarios")
UI_BUILD_PARTS = ("apps", "web")
UI_DIST_PARTS = ("apps", "web", "dist")

SETTINGS_SCHEMA = "radian.settings.v1"
SESSION_DIRNAME = ".ea-start-research"
SESSIONS_DIRNAME = "sessions"
SETTINGS_FILENAME = "settings.json"
USER_STORE_PARTS = (".config", "radian", "settings.json")

# The name the session key travels under in the server child only. It is ours, not
# a vendor's, so an inherited vendor key is never the credential these steps get.
KEY_ENV_NAME = "RADIAN_MODEL_API_KEY"

# The opt-in file source: one `.env` beside this script's own project root, and only
# ever that path. It is read only when `--project-env` asks for it, only for a
# provider launch whose mode was already stated on the command line, and only after
# every setup step has succeeded.
PROJECT_ENV_FILENAME = ".env"
# One variable on one line is tiny. Anything larger is not that file, and is refused
# by its size rather than read into memory.
PROJECT_ENV_MAX_BYTES = 64 * 1024

# The one provider profile this launcher offers, and the model id this repository
# has actually run against. Another provider, another endpoint or another key
# source is configured by hand and validated by the backend, which reads whatever
# settings document it is given; nothing here is a general configuration surface.
DEEPSEEK_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-flash"
# The one endpoint the backend's DeepSeek profile accepts. It is written as it is
# rather than offered as an option: a settings document pointing anywhere else is
# not a plan this launcher can make.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# The two modes a terminal may choose between, and the attempts a launch gives.
MODE_OFFLINE = "1"
MODE_PROVIDER = "2"
MODE_ATTEMPTS = 3

MIN_PORT = 1024
MAX_PORT = 65535
DEFAULT_PORT = 8765
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SECRET_ATTEMPTS = 3


class LauncherError(RuntimeError):
    """One actionable startup problem, reported without a traceback."""


@dataclass(frozen=True)
class Step:
    """One child process: its exact argv, its own working directory and its env."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


def which(name: str) -> str | None:
    """Return the executable for one prerequisite, or None when it is absent."""
    return shutil.which(name)


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child environment from the whitelist, plus the named additions."""
    env: dict[str, str] = {}
    for name in CHILD_ENV_NAMES:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if extra:
        env.update(extra)
    return env


def run_step(step: Step) -> None:
    """Run one step in the foreground and turn any failure into one message."""
    print(f"+ {shlex.join(list(step.argv))}", flush=True)
    try:
        subprocess.run(list(step.argv), cwd=str(step.cwd), env=dict(step.env), check=True)
    except FileNotFoundError as error:
        raise LauncherError(
            f"the command {step.argv[0]!r} could not be started: {error}"
        ) from error
    except subprocess.CalledProcessError as error:
        raise LauncherError(
            f"{step.argv[0]!r} exited with status {error.returncode}; "
            "the steps above it are the repository's own and can be rerun the same way"
        ) from error


def prompt_secret(prompt: str) -> str:
    """Read the session key from a real terminal, or refuse rather than echo it."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise LauncherError(
            "no terminal is available to enter a key without echoing it; run this from a "
            "terminal, or pass --offline to start with no model provider at all"
        )
    for _ in range(SECRET_ATTEMPTS):
        try:
            with warnings.catch_warnings():
                # `getpass` reads the key *with echo* when it cannot turn echo off,
                # and says so with this warning. A key typed into an echoed prompt is
                # not the private entry this launcher promises, so the warning is
                # treated as the failure it describes — raised from inside that call,
                # so the echoed read below it never happens.
                warnings.simplefilter("error", getpass.GetPassWarning)
                entered = getpass.getpass(prompt)
        except getpass.GetPassWarning as warning:
            raise LauncherError(
                f"this terminal cannot turn echo off while a key is typed ({warning}), and no "
                "key was read; use a terminal that can, or pass --offline to run with no model "
                "provider"
            ) from warning
        except (EOFError, KeyboardInterrupt) as error:
            raise LauncherError(
                "no key was entered; start again, or pass --offline to run with no model provider"
            ) from error
        key = entered.strip()
        if key:
            return key
        print("no key was entered. Enter a key, or stop and pass --offline.", file=sys.stderr)
    raise LauncherError(
        "no key was entered after three attempts; start again, or pass --offline to run with "
        "no model provider"
    )


def read_project_env(path: Path) -> str | None:
    """The one key value an opt-in project `.env` names, or None when it names none.

    This reads exactly one path for exactly one variable, and only ever because
    `--project-env` asked for it. There is no dotenv implementation here: a line is
    blank, a full-line `#` comment, or one `NAME=value` assignment. A line naming any
    other variable is left alone; the target's value is the rest of its line, with one
    matching pair of single or double quotes removed and nothing else interpreted. A
    trailing CR of a CRLF file is its line ending, ordinary spaces and tabs around the
    assignment and the value are not part of the value, and no other character is
    trimmed. Nothing is evaluated — no escape, no interpolation, no command
    substitution — so the text in the file is the text of the key.

    What this format does not read is refused rather than half-taken: a line that is
    not an assignment, an `export NAME=value` line, an unquoted `#` in a value, a
    quote that never closes, and a control character in a line. The refusal says which
    of those it was, and names the path only.

    A file that is absent, or that names no value, returns None so the caller can ask
    at the terminal instead. A file this function cannot take literally is a
    `LauncherError` naming the path and nothing out of the file: no line, no value,
    no decoder text and no operating-system error detail — a message like that would
    be a place the value could leak to.
    """
    if path.is_symlink():
        raise LauncherError(
            f"refusing to read {path}: it is a symlink, so its value would not be this "
            "project's own file and could change after it was read"
        )
    try:
        status = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        # Deliberately not the operating system's own message: it can carry more about
        # this file than a user needs, and this line says what to check.
        raise LauncherError(
            f"cannot read {path}; check that it is a file this user can read"
        ) from None
    _require_plain_project_env_file(path, status)
    # The open itself must not be able to wait: a path that was a regular file when it
    # was checked can be a FIFO by the time it is opened, and a blocking open of a FIFO
    # with no writer never returns.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise LauncherError(
            f"cannot read {path}; check that it is a file this user can read"
        ) from None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            # The descriptor is checked, not trusted: what was opened is what is read,
            # so a path swapped between the check above and this open is refused here,
            # and the file object closes it either way.
            _require_plain_project_env_file(path, os.fstat(descriptor))
            raw = handle.read(PROJECT_ENV_MAX_BYTES + 1)
    except OSError:
        raise LauncherError(
            f"cannot read {path}; check that it is a file this user can read"
        ) from None
    if len(raw) > PROJECT_ENV_MAX_BYTES:
        # Only reachable if the file grew after it was measured; the read above is
        # bounded either way.
        raise LauncherError(
            f"refusing to read {path}: it is larger than {PROJECT_ENV_MAX_BYTES} bytes, and a "
            f"file naming one {KEY_ENV_NAME} value is not"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LauncherError(f"refusing to read {path}: it is not UTF-8 text") from None
    return _project_env_value(text, path)


def _require_plain_project_env_file(path: Path, status: os.stat_result) -> None:
    """Refuse anything that is not one small regular file, before a byte of it is read.

    It is applied to the path once and to the opened descriptor again, so this also
    covers a FIFO, a socket or a device, none of which is ever read.
    """
    if not stat.S_ISREG(status.st_mode):
        raise LauncherError(f"refusing to read {path}: it is not a regular file")
    if status.st_size > PROJECT_ENV_MAX_BYTES:
        raise LauncherError(
            f"refusing to read {path}: it is larger than {PROJECT_ENV_MAX_BYTES} bytes, and a "
            f"file naming one {KEY_ENV_NAME} value is not"
        )


def _project_env_value(text: str, path: Path) -> str | None:
    """The target variable's literal value in one `.env` text, or None when absent.

    Lines are split on `\\n` alone — the one other line ending read here is the CR of
    a CRLF file — and only spaces and tabs are trimmed around a line, the assignment
    and the value. A control character cannot quietly become a line break, and it is
    refused rather than trimmed away into a shorter value or a different assignment.
    """
    assigned = False
    value = ""
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        if _project_env_control(stripped):
            raise LauncherError(
                f"refusing to read {path}: a line holds a control character, and this file is "
                "read as plain text rather than trimmed around one"
            )
        if stripped.split(None, 1)[0] == "export":
            raise LauncherError(
                f"refusing to read {path}: an 'export NAME=value' line is not a form this "
                "reads; write the assignment as NAME=value"
            )
        name, separator, remainder = stripped.partition("=")
        if not separator:
            raise LauncherError(
                f"refusing to read {path}: a line is not a NAME=value assignment, and this "
                "file is read as assignments only"
            )
        if name.strip(" \t") != KEY_ENV_NAME:
            continue
        if assigned:
            raise LauncherError(
                f"refusing to read {path}: {KEY_ENV_NAME} is assigned more than once, so which "
                "value is meant cannot be known"
            )
        assigned = True
        value = _project_env_literal(remainder.strip(" \t"), path)
    # An assignment that names nothing usable is not a value: the caller asks at the
    # terminal, exactly as it would for a file that is not there.
    return value if value.strip(" \t") else None


def _project_env_control(line: str) -> bool:
    """True when a line holds a character this plain-text format cannot carry.

    A tab is ordinary whitespace beside an assignment, so it is not one of them; every
    other control character is refused rather than stripped, because stripping one
    could turn the line into an assignment it does not look like.
    """
    return any(ord(character) < 32 and character != "\t" for character in line)


def _project_env_literal(raw: str, path: Path) -> str:
    """One target line's value: the text itself, with one matching quote pair removed.

    An unquoted `#` is refused rather than kept: it reads like an inline comment this
    format does not strip, and taking it as the key would hand a guess to the provider.
    Quoting the whole value is how a `#` that belongs to it is written.
    """
    if raw[:1] in ("'", '"'):
        if len(raw) < 2 or raw[-1] != raw[0]:
            raise LauncherError(
                f"refusing to read {path}: the {KEY_ENV_NAME} value opens a quote that never "
                "closes, and a value continued onto a second line is not a form this reads"
            )
        return _project_env_text(raw[1:-1], path)
    if "#" in raw:
        raise LauncherError(
            f"refusing to read {path}: the unquoted {KEY_ENV_NAME} value holds a '#', and an "
            "inline comment is not a form this reads; put the value in quotes if it really "
            "contains one"
        )
    return _project_env_text(raw, path)


def _project_env_text(value: str, path: Path) -> str:
    """A value as it will be used: one line of printable characters, or a refusal."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LauncherError(
            f"refusing to read {path}: the {KEY_ENV_NAME} value contains a control character, "
            "and a key is one line of printable characters"
        )
    return value


def _session_key(*, project_env: bool, project_root: Path, provider: str | None) -> str:
    """The key for one launch: the opt-in file's value when it has one, else the terminal.

    This is called only after every setup step has succeeded, so the key is never held
    across a slow install. What it prints is where the key came from and never the key
    itself; what it returns goes no further than the one child environment the caller
    builds from it.
    """
    if project_env:
        env_path = project_root / PROJECT_ENV_FILENAME
        value = read_project_env(env_path)
        if value is not None:
            print(f"using the {KEY_ENV_NAME} value in {env_path}; it is not printed.")
            return value
        print(f"no {KEY_ENV_NAME} value in {env_path}; the key is asked for here.")
    return prompt_secret(
        f"{provider} API key for this session (typed here, never written to disk): "
    )


def choose_offline() -> bool:
    """Ask, at a terminal, which of the two modes this launch runs in.

    Nothing has been created when this runs, so stopping here leaves the host
    exactly as it was. A bare return key is the offline mode: the mode that writes
    no provider at all is the one that cannot make a request.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LauncherError(
            "no terminal is available to choose how this session runs; pass --offline to start "
            f"with no model provider, or --provider {DEEPSEEK_PROVIDER} to use one"
        )
    print("How should this session run?")
    print(f"  {MODE_OFFLINE}. Offline. No provider is written, so no request is possible.")
    print(f"  {MODE_PROVIDER}. {DEEPSEEK_PROVIDER}. One key is typed after setup succeeds, and the")
    print("     request the app makes with it is not verified by this script.")
    for _ in range(MODE_ATTEMPTS):
        try:
            answer = input(f"Choose {MODE_OFFLINE} or {MODE_PROVIDER} [{MODE_OFFLINE}]: ").strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise LauncherError(
                "no mode was chosen; start again, or pass --offline or "
                f"--provider {DEEPSEEK_PROVIDER}"
            ) from error
        if answer in ("", MODE_OFFLINE):
            return True
        if answer == MODE_PROVIDER:
            return False
        print(f"enter {MODE_OFFLINE} or {MODE_PROVIDER}.", file=sys.stderr)
    raise LauncherError(
        f"no mode was chosen after {MODE_ATTEMPTS} attempts; pass --offline or "
        f"--provider {DEEPSEEK_PROVIDER}"
    )


def bootstrap_step(project_root: Path) -> Step:
    """Create the locked 3.12 environment with the repository's own bootstrap.

    The Web extra is asked for by name: the server this launch ends with cannot
    start without it, and a bootstrap that installs the development extra alone
    leaves exactly the environment that fails later.
    """
    argv = (
        "uv",
        "run",
        "--no-project",
        "--python",
        SUPPORTED_PYTHON,
        "python",
        BOOTSTRAP_SCRIPT,
        "--web",
    )
    return Step(
        argv=argv,
        cwd=project_root,
        env=child_env({"UV_PROJECT_ENVIRONMENT": str(project_root / "venv")}),
    )


def sync_step(project_root: Path) -> Step:
    """Add the Web extra the server needs to the same locked environment."""
    extras = [part for extra in LOCKED_EXTRAS for part in ("--extra", extra)]
    return Step(
        argv=("uv", "sync", "--locked", *extras),
        cwd=project_root,
        env=child_env({"UV_PROJECT_ENVIRONMENT": str(project_root / "venv")}),
    )


def npm_step(project_root: Path, args: Sequence[str]) -> Step:
    """Run one npm script from the UI's own directory."""
    return Step(
        argv=("npm", *args),
        cwd=project_root.joinpath(*UI_BUILD_PARTS),
        env=child_env(),
    )


def _scripts_dir(project_root: Path) -> Path:
    """The installed scripts directory of the locked environment."""
    return project_root / "venv" / ("Scripts" if os.name == "nt" else "bin")


def _entrypoint(project_root: Path) -> Path:
    """The installed entrypoint of the locked environment, not whatever is on PATH."""
    return _scripts_dir(project_root) / ("ea.exe" if os.name == "nt" else "ea")


def _venv_python(project_root: Path) -> Path:
    """The interpreter of the locked environment: what a check has to run in."""
    return _scripts_dir(project_root) / ("python.exe" if os.name == "nt" else "python")


def web_preflight_step(project_root: Path) -> Step:
    """Read the Web dependency stack of the locked environment before anything starts.

    It runs that environment's own interpreter in isolated mode, so no
    `PYTHONPATH` and no user site-packages can make a broken install look whole,
    and it reads nothing else: no settings document, no workspace, no key.
    """
    return Step(
        argv=(str(_venv_python(project_root)), "-I", "-c", WEB_PREFLIGHT_SOURCE),
        cwd=project_root,
        env=child_env(),
    )


def server_step(
    *,
    project_root: Path,
    workspace: Path,
    settings_file: Path,
    port: int,
    key: str | None,
) -> Step:
    """The one long-running child: the existing loopback server, with its own settings."""
    env = child_env({"RADIAN_SETTINGS_FILE": str(settings_file)})
    if key is not None:
        # The key exists in this one environment and nowhere else: not in an argv,
        # not in a file, and not in any other step.
        env[KEY_ENV_NAME] = key
    return Step(
        argv=(
            str(_entrypoint(project_root)),
            "web",
            "serve",
            "--scenario-root",
            str(project_root.joinpath(*SCENARIO_ROOT_PARTS)),
            "--workspace",
            str(workspace),
            "--ui-dir",
            str(project_root.joinpath(*UI_DIST_PARTS)),
            "--port",
            str(port),
        ),
        cwd=project_root,
        env=env,
    )


def plan_steps(
    *,
    project_root: Path,
    workspace: Path,
    settings_file: Path,
    port: int,
    offline: bool,
    key: str | None,
) -> tuple[Step, ...]:
    """Every step of one launch, in order, with no side effect of any kind.

    The scenario root, the workspace and the built UI stay three separate roots, and
    the server keeps the loopback host the existing command fixes: no `--host` is
    passed, here or anywhere else. An offline launch carries no key at all, whatever
    the caller passed, so a configured parent cannot leak a provider into it.

    The dependency preflight follows the locked install and precedes everything
    that would build, listen or ask for a key, so an environment that only looks
    installed is reported while the host is still untouched.
    """
    return (
        bootstrap_step(project_root),
        sync_step(project_root),
        web_preflight_step(project_root),
        npm_step(project_root, ("ci",)),
        npm_step(project_root, ("run", "build")),
        server_step(
            project_root=project_root,
            workspace=workspace,
            settings_file=settings_file,
            port=port,
            key=None if offline else key,
        ),
    )


def _new_session_id() -> str:
    """A name for one launch: unique, readable, and not a secret."""
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"


def _settings_document(*, model: str | None, model_name: str | None) -> dict[str, object]:
    """The session document: a provider selection, and the *name* of a key variable.

    No secret of any kind is represented here. `api_key` is null and `api_key_env`
    names the variable the server child receives its key in, which is how a key is
    used without ever being written down.
    """
    if model is None:
        # Offline. No `model` object at all, so no provider can be selected and no
        # request can be made — even when the user's own environment has a key.
        return {"schema": SETTINGS_SCHEMA}
    if model != DEEPSEEK_PROVIDER:
        raise LauncherError(
            f"--provider must be {DEEPSEEK_PROVIDER}, the one profile this launcher offers, or "
            "pass --offline to run with no model provider"
        )
    name = DEFAULT_MODEL if model_name is None else model_name.strip()
    if not name:
        raise LauncherError(
            "--model must name a model id; the backend refuses a provider with no model"
        )
    return {
        "schema": SETTINGS_SCHEMA,
        "model": {
            "provider": model,
            "model": name,
            "base_url": DEEPSEEK_BASE_URL,
            "api_key": None,
            "api_key_env": KEY_ENV_NAME,
        },
    }


def _absolute(path: Path) -> Path:
    """The path the user gave, made absolute. Symlinks are left as they are."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _sessions_dir(workspace: Path) -> Path:
    """The session store of one workspace. Reading this line creates nothing."""
    return workspace / SESSION_DIRNAME / SESSIONS_DIRNAME


def _user_store() -> Path:
    """Where the backend would keep a user's own settings. Never read here."""
    return Path.home().joinpath(*USER_STORE_PARTS)


def _ensure_session_dir(workspace: Path, session: str) -> Path:
    """Create the private session directory one level at a time.

    The workspace itself is created here rather than assumed, because a first
    launch has none. Every level below it is checked before it is used, so a
    symlink planted where a settings directory belongs is refused instead of
    followed, and no part of the session store is ever created through one.
    """
    if not workspace.exists():
        try:
            workspace.mkdir(parents=True)
        except OSError as error:
            raise LauncherError(f"cannot create the workspace {workspace}: {error}") from error
    current = workspace
    for name in (SESSION_DIRNAME, SESSIONS_DIRNAME, session):
        current = current / name
        if current.is_symlink():
            raise LauncherError(
                f"refusing to use {current}: it is a symlink, so a session document "
                f"written there would not stay under {workspace}"
            )
        if current.exists():
            if not current.is_dir():
                raise LauncherError(f"refusing to use {current}: it exists and is not a directory")
            continue
        try:
            os.mkdir(current, 0o700)
        except OSError as error:
            raise LauncherError(f"cannot create {current}: {error}") from error
    return current


def prepare_settings(
    workspace: Path,
    *,
    model: str | None,
    session_id: str | None = None,
    model_name: str | None = None,
) -> Path:
    """Write one new session settings document and return its path.

    The document is exclusive: it is created with `O_EXCL` and mode 0600, so an
    existing file is refused rather than replaced, and a relaunch always opens a new
    session instead of reusing or rewriting the last one. A user's own settings store
    is never read and never copied.
    """
    root = _absolute(workspace)
    session = session_id if session_id is not None else _new_session_id()
    if not SESSION_ID_PATTERN.match(session):
        raise LauncherError(
            f"session id {session!r} is not a plain name; use letters, digits, dot, dash "
            "or underscore, starting with a letter or digit"
        )
    document = _settings_document(model=model, model_name=model_name)
    path = _ensure_session_dir(root, session) / SETTINGS_FILENAME
    payload = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise LauncherError(
            f"refusing to replace the existing session document {path}; every launch opens a "
            "new session, so remove that file or pass a different --workspace"
        ) from error
    except OSError as error:
        raise LauncherError(f"cannot create {path}: {error}") from error
    try:
        if os.write(descriptor, payload) != len(payload):
            raise LauncherError(f"the session document {path} was not written completely")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _validate_port(port: int) -> int:
    """Keep the port inside the range the existing command accepts."""
    if not MIN_PORT <= port <= MAX_PORT:
        raise LauncherError(
            f"--port must be between {MIN_PORT} and {MAX_PORT}; the server listens on "
            "127.0.0.1 only and this launcher passes no --host"
        )
    return port


def _described(path: Path, as_given: Path) -> str:
    """Name a path, and the form it was given in when that reads differently."""
    if str(as_given) == str(path):
        return str(path)
    return f"{path} (given as {as_given})"


def _validate_workspace(*, requested: Path, workspace: Path, project_root: Path) -> None:
    """Refuse a workspace that overlaps the project that serves it.

    The comparison is by real path, so a workspace that only *looks* separate
    because a symlink points it into the source tree is refused too.
    """
    resolved = Path(os.path.realpath(str(workspace)))
    reserved = (
        ("the project that serves it", project_root),
        ("the scenario root", project_root.joinpath(*SCENARIO_ROOT_PARTS)),
        ("the UI build", project_root.joinpath(*UI_DIST_PARTS)),
    )
    for label, root in reserved:
        target = Path(os.path.realpath(str(root)))
        if resolved == target or resolved.is_relative_to(target) or target.is_relative_to(resolved):
            raise LauncherError(
                f"the workspace {_described(workspace, requested)} overlaps {label} at {root}; "
                "job, attempt and report data belong outside the project that serves them, so "
                "pass --workspace with a directory of its own"
            )
    if workspace.exists() and not workspace.is_dir():
        raise LauncherError(f"the workspace {workspace} exists and is not a directory")


def _missing_tools() -> list[tuple[str, str]]:
    """The prerequisites that are absent, each with the remedy for it."""
    return [(name, TOOL_GUIDANCE[name]) for name in TOOL_NAMES if which(name) is None]


def _print_check(
    *,
    project_root: Path,
    workspace: Path,
    port: int,
    offline: bool,
    provider: str | None,
    project_env: bool,
    steps: Sequence[Step],
) -> None:
    """Print where this launch would put everything. Pure: it starts nothing."""
    print("EA local research launcher: check only, nothing is built or started.")
    print()
    print(f"  project root   {project_root}")
    print(f"  workspace      {workspace}")
    print(f"  scenario root  {project_root.joinpath(*SCENARIO_ROOT_PARTS)}")
    print(f"  UI build       {project_root.joinpath(*UI_DIST_PARTS)}")
    print(f"  session store  {_sessions_dir(workspace)}")
    print(f"                 one new document per launch; {_user_store()} is never read")
    print("                 and never copied")
    print(f"  port           {port} on 127.0.0.1 (the host is fixed; no --host is passed)")
    if offline:
        print("  model          offline: no provider is written, so no request can be made")
        print("  key source     none: an offline launch asks for no key at all")
    elif provider is None:
        # This check asks nothing, so it reports the question a launch would ask.
        print(
            f"  model          asked at the terminal first: offline, or {DEEPSEEK_PROVIDER} "
            "with a key typed after setup"
        )
        print("  key source     the terminal, after setup succeeds")
    else:
        print(f"  model          {provider}; the key is asked for once, after setup succeeds")
        if project_env:
            # A check opens no file: it reports the source a launch would read.
            print(f"  key source     {project_root / PROJECT_ENV_FILENAME} (--project-env)")
            print("                 used when it names a value, asked for at the terminal when")
            print("                 it does not; a file it cannot read literally stops the")
            print("                 launch. This check opened nothing.")
        else:
            print("  key source     the terminal, typed after setup succeeds")
    for name in TOOL_NAMES:
        print(f"  {name:<14} {which(name)}")
    print("  steps")
    for index, step in enumerate(steps, start=1):
        print(f"                 {index}. {shlex.join(list(step.argv))}")
        print(f"                    in {step.cwd}")
    print()
    print(f"  After a launch the app is at http://127.0.0.1:{port}; this check created nothing.")
    print("  Full usage: docs/local-research-start.md")


def _parser() -> argparse.ArgumentParser:
    """The whole command line: one check, one launch, five options."""
    parser = argparse.ArgumentParser(
        prog="python3 scripts/start_research.py",
        description=(
            "Check, build and start the local research Web application from this repository's "
            "own locked environment. The application is served on 127.0.0.1 only."
        ),
        epilog="Full usage: docs/local-research-start.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check prerequisites and print the plan; create, install, build and start nothing",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="start with no model provider: no key is asked for, and no model request is possible "
        "(this is also the answer a bare return key gives at the mode question)",
    )
    parser.add_argument(
        "--project-env",
        action="store_true",
        help="read the key from this project's own .env instead of asking for it; needs the "
        "provider mode stated with --provider or --model, and asks at the terminal when that "
        "file names no value (a file it cannot read literally stops the launch, and the file is "
        "never created or changed here)",
    )
    parser.add_argument(
        "--workspace",
        default=str(Path.home() / "ea-web-workspace"),
        metavar="DIR",
        help="directory for job, attempt and report data, outside this project "
        "(default: ~/ea-web-workspace)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"loopback port on 127.0.0.1 (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=(DEEPSEEK_PROVIDER,),
        metavar="NAME",
        help=f"use {DEEPSEEK_PROVIDER} for this session instead of asking; it is the one profile "
        "this launcher offers",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="ID",
        help=f"model id for {DEEPSEEK_PROVIDER} (default: {DEFAULT_MODEL}); passing it also "
        "chooses the provider mode",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one check or one guided launch, and return the exit status."""
    options = _parser().parse_args(list(argv) if argv is not None else sys.argv[1:])
    project_root = Path(__file__).resolve().parents[1]
    offline = bool(options.offline)
    model_name = None if options.model is None else str(options.model)
    project_env = bool(options.project_env)
    # A mode is asked for only when nothing on the command line chose one. Either
    # flag is a decision; a launch given neither asks at a terminal.
    asked = not offline and options.provider is None and model_name is None
    provider = None if offline or asked else DEEPSEEK_PROVIDER
    requested = Path(str(options.workspace)).expanduser()
    workspace = _absolute(requested)

    try:
        if offline and (options.provider is not None or model_name is not None):
            raise LauncherError(
                "--offline writes no provider at all, so it cannot be combined with --provider "
                "or --model; pass one or the other"
            )
        if project_env and (offline or asked):
            # A file source is a provider launch's decision, and it is refused here
            # rather than later: at this point nothing has been created, no question
            # has been asked, and no file has been opened.
            raise LauncherError(
                "--project-env reads the key from this project's own .env, which only a "
                "provider launch has a use for; pass it with --provider "
                f"{DEEPSEEK_PROVIDER} or --model, not with --offline or on a launch that has "
                "not chosen a provider"
            )
        _validate_workspace(requested=requested, workspace=workspace, project_root=project_root)
        port = _validate_port(int(options.port))
        # Validating the selection is pure, so a check reports a model id the
        # document cannot carry without writing a document a launch would replace.
        _settings_document(model=provider, model_name=model_name)
        planned = plan_steps(
            project_root=project_root,
            workspace=workspace,
            settings_file=_sessions_dir(workspace) / "<new session>" / SETTINGS_FILENAME,
            port=port,
            offline=offline,
            key=None,
        )
    except LauncherError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    missing = _missing_tools()
    if missing:
        # Nothing has been created yet, and nothing will be: a host that cannot
        # build gets guidance, not a half-made workspace.
        for name, guidance in missing:
            print(f"error: {name} was not found on PATH.", file=sys.stderr)
            print(f"  install it as this repository documents: {guidance}", file=sys.stderr)
        print("  nothing was created, installed, built or started.", file=sys.stderr)
        return 2

    if options.check:
        _print_check(
            project_root=project_root,
            workspace=workspace,
            port=port,
            offline=offline,
            provider=provider,
            project_env=project_env,
            steps=planned,
        )
        return 0

    if asked:
        # Settled before anything is created: the document this launch writes
        # records the mode, and a user who stops at this question has a host
        # exactly as they left it.
        try:
            offline = choose_offline()
        except LauncherError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        provider = None if offline else DEEPSEEK_PROVIDER

    # The workspace is created by `prepare_settings` itself, so a first launch and
    # a relaunch take exactly the same path.
    try:
        settings_file = prepare_settings(workspace, model=provider, model_name=model_name)
        steps = plan_steps(
            project_root=project_root,
            workspace=workspace,
            settings_file=settings_file,
            port=port,
            offline=offline,
            key=None,
        )
    except LauncherError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print("EA local research launcher: one session, one key, no secret written by this script.")
    print(f"  workspace  {workspace}")
    print(f"  settings   {settings_file}")
    print("             a new session document; your own settings store is never read")
    print(f"  port       {port} on 127.0.0.1")
    if offline:
        print("  model      offline: no provider is written, so no request can be made")
    elif project_env:
        print(f"  model      {provider} ({model_name or DEFAULT_MODEL}); the key is read from")
        print(f"             {project_root / PROJECT_ENV_FILENAME} when it names a value, and is")
        print("             asked for here after setup when it does not")
    else:
        print(f"  model      {provider} ({model_name or DEFAULT_MODEL}); the key is asked for")
        print("             after setup succeeds, and this script verifies no model request")
    print()

    try:
        for step in steps[:-1]:
            run_step(step)
        final = steps[-1]
        if not offline:
            # The key is resolved only now: setup is done, so it is never held
            # across a slow install, and it reaches this one child's environment.
            key = _session_key(
                project_env=project_env,
                project_root=project_root,
                provider=provider,
            )
            final = replace(final, env={**final.env, KEY_ENV_NAME: key})
        print(f"Starting the local application on http://127.0.0.1:{port} (Ctrl-C stops it).")
        run_step(final)
    except LauncherError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
