"""Guided local startup: one entry, one explicit check, one transient key.

The Quick Start is four commands a user has to know, and one of them is the
point at which a provider key would be easiest to handle carelessly. What these
tests describe is the outcome that replaces it: one guided entry that checks what
it needs, uses the repository's own locked setup, and hands a key to exactly one
child process without ever writing it down or reading anybody's existing
configuration.

Nothing here runs a real dependency install, a real build or a real server, and
no test needs a provider, a network or a key of its own. The only key anywhere in
this file is a synthetic string that is never sent anywhere.

The launcher has to start on the `python3` a stock macOS already has, so these
tests also hold it to that floor; the 3.12 environment it needs is the
repository's own, reached through the toolchain that already builds it.
"""

from __future__ import annotations

import ast
import getpass
import io
import json
import os
import sys
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import start_research  # noqa: E402

# One obviously synthetic value. It is never a real credential and never leaves
# the process tree these tests build.
SYNTHETIC_TOKEN = "synthetic-test-token-never-a-real-credential"
INHERITED_KEY = "an-inherited-provider-key-that-must-not-be-reused"
KEY_LIKE_NAMES = (
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "RADIAN_MODEL_API_KEY",
)


def _all_found(name: str) -> str:
    """Every probe answers with a plausible absolute path."""
    return f"/usr/local/bin/{name}"


def _without(missing: str) -> Callable[[str], str | None]:
    """Every probe answers, except the one this host does not have."""

    def probe(name: str) -> str | None:
        return None if name == missing else f"/usr/local/bin/{name}"

    return probe


def _server_step(steps: Sequence[start_research.Step]) -> start_research.Step:
    """The one step that keeps running: the loopback server child."""
    server = steps[-1]
    assert list(server.argv[1:3]) == ["web", "serve"], list(server.argv)
    return server


def _venv_python(project_root: Path) -> Path:
    """The locked environment's own interpreter: what a preflight has to use."""
    scripts = project_root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python")


def _document(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _written_sessions(workspace: Path) -> list[Path]:
    """Every session document one workspace holds, so a test reads what a launch wrote."""
    store = workspace / start_research.SESSION_DIRNAME / start_research.SESSIONS_DIRNAME
    return sorted(store.glob(f"*/{start_research.SETTINGS_FILENAME}"))


def _key_recorder(prompts: list[str]) -> Callable[[str], str]:
    """A key prompt that records what it was asked, and returns a synthetic token."""

    def prompt(text: str) -> str:
        prompts.append(text)
        return SYNTHETIC_TOKEN

    return prompt


class _RecordingTTY(io.StringIO):
    """A stream that is a terminal on both ends, and text this test can read back."""

    def isatty(self) -> bool:
        return True


def _terminal(monkeypatch: pytest.MonkeyPatch, *answers: str) -> io.StringIO:
    """Give this process a terminal that answers, in order, what the launcher asks.

    The returned stream is where the launcher's own output goes, so a test can read
    back what a launch announced as well as what it asked.
    """
    out = _RecordingTTY()
    monkeypatch.setattr(sys, "stdin", _RecordingTTY())
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", out)
    remaining = list(answers)

    def read(prompt: str = "") -> str:
        assert remaining, "the launcher asked more questions than this test answered"
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", read)
    return out


def test_the_launcher_starts_on_the_interpreter_macos_already_has() -> None:
    """It runs before any supported environment exists, so it cannot need one."""
    module_file = start_research.__file__
    assert module_file is not None
    source = Path(module_file).read_text(encoding="utf-8")

    # Python 3.9 grammar: no `match`, no `except*`, no 3.12 type parameters.
    tree = ast.parse(source, feature_version=9)
    deferred = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    # 3.9 evaluates annotations, so deferring them is what keeps a `str | None`
    # annotation from being a TypeError on the interpreter a user actually has.
    assert deferred


def test_the_check_reports_dependencies_without_creating_or_running_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(start_research, "which", _all_found)
    executed: list[start_research.Step] = []
    monkeypatch.setattr(start_research, "run_step", executed.append)
    monkeypatch.delenv("RADIAN_SETTINGS_FILE", raising=False)
    workspace = tmp_path / "ea-web-workspace"

    code = start_research.main(
        ["--check", "--offline", "--workspace", str(workspace), "--port", "8799"]
    )

    assert code == 0
    # A check is a reading, not a setup: it runs no step, creates no workspace,
    # writes no session document and exports nothing into this process.
    assert executed == []
    assert not workspace.exists()
    assert "RADIAN_SETTINGS_FILE" not in os.environ
    captured = capsys.readouterr()
    report = captured.out + captured.err
    assert "uv" in report
    assert "npm" in report


def test_a_missing_dependency_is_named_with_guidance_and_installs_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(start_research, "which", _without("uv"))
    executed: list[start_research.Step] = []
    monkeypatch.setattr(start_research, "run_step", executed.append)
    workspace = tmp_path / "ws"

    code = start_research.main(["--check", "--offline", "--workspace", str(workspace)])

    assert code != 0
    assert executed == []
    assert not workspace.exists()
    captured = capsys.readouterr()
    report = captured.out + captured.err
    # The missing tool is named, and the remedy is the one this repository
    # already documents for it rather than "install something".
    assert "uv" in report
    assert "docs.astral.sh" in report


def test_the_check_refuses_a_workspace_inside_the_project_that_serves_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(start_research, "which", _all_found)
    inside = PROJECT_ROOT / "docs" / "start-research-scratch"

    code = start_research.main(["--check", "--offline", "--workspace", str(inside)])

    assert code != 0
    captured = capsys.readouterr()
    assert str(inside) in captured.out + captured.err
    assert not inside.exists()


def test_offline_plans_the_documented_quick_start_with_separate_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    settings = start_research.prepare_settings(workspace, model=None)
    document = _document(settings)
    assert document["schema"] == "radian.settings.v1"
    # Offline configures no provider at all, so no model-backed step can run and
    # no request is made on launch.
    assert "model" not in document

    steps = start_research.plan_steps(
        project_root=PROJECT_ROOT,
        workspace=workspace,
        settings_file=settings,
        port=8799,
        offline=True,
        key=None,
    )
    argvs = [list(step.argv) for step in steps]

    assert ["npm", "ci"] in argvs
    assert ["npm", "run", "build"] in argvs
    extra = next(argv for argv in argvs if "--extra" in argv)
    assert "--locked" in extra and "web" in extra
    for name in KEY_LIKE_NAMES:
        assert all(name not in step.env for step in steps)

    # The 3.12 environment is created by the repository's own pinned toolchain,
    # never by the interpreter that happened to start the launcher.
    bootstrap = next(argv for argv in argvs if any("bootstrap_local.py" in part for part in argv))
    assert bootstrap[0] != sys.executable

    # The frontend is built from its own directory, and the server is the locked
    # entrypoint of this repository rather than whatever `ea` is on PATH.
    build = next(step for step in steps if list(step.argv) == ["npm", "run", "build"])
    assert build.cwd == PROJECT_ROOT / "apps" / "web"
    server = _server_step(steps)
    assert server.argv[0] == str(PROJECT_ROOT / "venv" / "bin" / "ea")

    argv = list(server.argv)
    for flag in ("--scenario-root", "--workspace", "--ui-dir", "--port"):
        assert flag in argv, flag
    assert argv[argv.index("--port") + 1] == "8799"
    # The listener is fixed to loopback by the existing command; a launcher that
    # passed a host would be widening it.
    assert "--host" not in argv
    # Scenario, workspace and UI stay three separate absolute roots.
    assert {
        argv[argv.index(flag) + 1] for flag in ("--scenario-root", "--workspace", "--ui-dir")
    } == {
        str(PROJECT_ROOT / "examples" / "web-scenarios"),
        str(workspace),
        str(PROJECT_ROOT / "apps" / "web" / "dist"),
    }
    # Only the server child is told which settings document this session owns:
    # setup never sees it, and it is never the user's own file.
    assert server.env["RADIAN_SETTINGS_FILE"] == str(settings)
    assert all("RADIAN_SETTINGS_FILE" not in step.env for step in steps[:-1])


def test_the_transient_key_reaches_only_the_server_child_and_is_never_written_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    settings = start_research.prepare_settings(workspace, model="deepseek")
    stored = settings.read_text(encoding="utf-8")
    document = _document(settings)
    model = document["model"]
    assert isinstance(model, dict)
    assert model["provider"] == "deepseek"
    # The key is entered for one child process. It is never embedded in the file.
    assert model["api_key"] is None
    env_name = model["api_key_env"]
    assert isinstance(env_name, str) and env_name
    assert SYNTHETIC_TOKEN not in stored

    # A key the user already exported, and the two variables that would let an
    # inherited interpreter shadow the locked environment, must not be inherited.
    monkeypatch.setenv("DEEPSEEK_API_KEY", INHERITED_KEY)
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else/venv")

    steps = start_research.plan_steps(
        project_root=PROJECT_ROOT,
        workspace=workspace,
        settings_file=settings,
        port=8799,
        offline=False,
        key=SYNTHETIC_TOKEN,
    )
    server = _server_step(steps)

    assert server.env[env_name] == SYNTHETIC_TOKEN
    assert server.env["RADIAN_SETTINGS_FILE"] == str(settings)
    assert all(env_name not in step.env for step in steps[:-1])
    # It is not an argument, not a command line, and not any other child's value.
    assert all(SYNTHETIC_TOKEN not in " ".join(step.argv) for step in steps)
    assert all(SYNTHETIC_TOKEN not in value for step in steps[:-1] for value in step.env.values())
    assert all("DEEPSEEK_API_KEY" not in step.env for step in steps)
    assert all(INHERITED_KEY not in step.env.values() for step in steps)
    assert all("PYTHONPATH" not in step.env for step in steps)
    assert all("VIRTUAL_ENV" not in step.env for step in steps)


def test_a_repeat_launch_opens_a_new_session_and_leaves_the_user_store_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    user_store = home / ".config" / "radian" / "settings.json"
    user_store.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "schema": "radian.settings.v1",
            "model": {"provider": "anthropic", "api_key": "user-owned-credential"},
        }
    )
    user_store.write_text(original, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    # Even a pointer the user's shell already exports is not what this session uses.
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(user_store))

    workspace = tmp_path / "workspace"
    marker = workspace / "uploads" / "raw" / "earlier-run"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"workspace data from an earlier run")

    first = start_research.prepare_settings(workspace, model="deepseek")
    first_bytes = first.read_bytes()
    second = start_research.prepare_settings(workspace, model="deepseek")

    # A relaunch opens its own session: the earlier document is not read, not
    # rewritten and not reused, and the workspace's own data is untouched.
    assert first != second
    assert first.read_bytes() == first_bytes
    assert user_store.read_text(encoding="utf-8") == original
    assert marker.read_bytes() == b"workspace data from an earlier run"
    for session in (first, second):
        written = session.read_text(encoding="utf-8")
        assert "user-owned-credential" not in written
        assert str(user_store) != str(session)

    steps = start_research.plan_steps(
        project_root=PROJECT_ROOT,
        workspace=workspace,
        settings_file=second,
        port=8799,
        offline=False,
        key=SYNTHETIC_TOKEN,
    )
    assert str(user_store) not in [value for step in steps for value in step.env.values()]


def test_an_existing_session_document_is_refused_rather_than_overwritten(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = start_research.prepare_settings(workspace, model="deepseek", session_id="session-1")
    kept = first.read_bytes()

    with pytest.raises(start_research.LauncherError) as failure:
        start_research.prepare_settings(workspace, model="deepseek", session_id="session-1")

    # The refusal names the path so a user can act on it, and the file it would
    # have replaced is exactly as it was.
    assert str(first) in str(failure.value)
    assert first.read_bytes() == kept


def test_the_command_line_offers_one_provider_and_no_endpoint_override(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    # Every provider but the one profile this repository has run against is refused
    # by the command line itself, and there is no endpoint option at all: a URL
    # validation surface is not something this launcher grows.
    for argv in (
        ["--provider", "anthropic"],
        ["--provider", "openai-chat-completions-compatible"],
        ["--base-url", "https://example.invalid/v1"],
    ):
        with pytest.raises(SystemExit):
            start_research.main(["--check", *argv, "--workspace", str(workspace)])

    # The two modes are alternatives, not a combination: offline writes no provider
    # at all, so stating one beside it is refused rather than quietly ignored.
    two_modes = ["--offline", "--provider", start_research.DEEPSEEK_PROVIDER]
    assert start_research.main([*two_modes, "--workspace", str(workspace)]) == 2
    assert _written_sessions(workspace) == []

    # What a launch does write is the one profile and the one endpoint, chosen
    # here rather than supplied by the user.
    document = _document(start_research.prepare_settings(workspace, model="deepseek"))
    model = document["model"]
    assert isinstance(model, dict)
    assert model["provider"] == start_research.DEEPSEEK_PROVIDER
    assert model["model"] == start_research.DEFAULT_MODEL
    assert model["base_url"] == start_research.DEEPSEEK_BASE_URL
    assert model["api_key"] is None
    assert model["api_key_env"] == start_research.KEY_ENV_NAME


def test_a_bare_launch_asks_at_a_terminal_and_a_noninteractive_one_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(start_research, "which", _all_found)
    executed: list[start_research.Step] = []
    monkeypatch.setattr(start_research, "run_step", executed.append)
    prompted: list[str] = []
    monkeypatch.setattr(start_research, "prompt_secret", _key_recorder(prompted))
    workspace = tmp_path / "workspace"

    # No mode flag and no terminal: the launch is refused instead of guessing, and
    # it does so before it creates the workspace, writes a document or runs a step.
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert start_research.main(["--workspace", str(workspace)]) == 2
    assert executed == []
    assert prompted == []
    assert not workspace.exists()

    # At a terminal, a bare return key is the offline mode: no provider is written,
    # no key is asked for, and no step is handed one.
    _terminal(monkeypatch, "")
    assert start_research.main(["--workspace", str(workspace)]) == 0
    assert prompted == []
    assert "model" not in _document(_written_sessions(workspace)[0])
    assert all(start_research.KEY_ENV_NAME not in step.env for step in executed)

    # Choosing the provider mode instead asks for a key only after setup has
    # succeeded, names the provider that was chosen, and hands the key to the
    # server child alone.
    executed.clear()
    announced = _terminal(monkeypatch, "2")
    assert start_research.main(["--workspace", str(workspace), "--port", "8799"]) == 0
    assert len(prompted) == 1 and start_research.DEEPSEEK_PROVIDER in prompted[0]
    settings = next(
        Path(line.split(None, 1)[1].strip())
        for line in announced.getvalue().splitlines()
        if line.startswith("  settings ")
    )
    model = _document(settings)["model"]
    assert isinstance(model, dict)
    assert model["provider"] == start_research.DEEPSEEK_PROVIDER
    assert model["model"] == start_research.DEFAULT_MODEL
    server = _server_step(executed)
    assert server.env[start_research.KEY_ENV_NAME] == SYNTHETIC_TOKEN
    assert all(start_research.KEY_ENV_NAME not in step.env for step in executed[:-1])


def test_an_echoing_key_prompt_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _RecordingTTY())
    monkeypatch.setattr(sys, "stderr", _RecordingTTY())
    read_in_the_clear = False

    def echoing_getpass(_prompt: str = "Password: ") -> str:
        # Exactly what `getpass` does when it cannot turn echo off: it warns, and
        # then reads the key with echo. The read below must never happen.
        nonlocal read_in_the_clear
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        read_in_the_clear = True
        return "a-key-that-would-have-been-echoed"

    monkeypatch.setattr(getpass, "getpass", echoing_getpass)

    with pytest.raises(start_research.LauncherError) as failure:
        start_research.prompt_secret("key: ")

    # The refusal is actionable, says what happened, and was raised from inside the
    # prompt: the echoed fallback read never ran, so no key was read in the clear.
    assert read_in_the_clear is False
    assert "echo" in str(failure.value)
    assert "--offline" in str(failure.value)


def test_a_setup_failure_asks_for_no_key_and_starts_no_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(start_research, "which", _all_found)
    attempted: list[start_research.Step] = []

    def failing_step(step: start_research.Step) -> None:
        attempted.append(step)
        raise start_research.LauncherError("this setup step exited with status 1")

    monkeypatch.setattr(start_research, "run_step", failing_step)
    prompted: list[str] = []
    monkeypatch.setattr(start_research, "prompt_secret", _key_recorder(prompted))
    workspace = tmp_path / "workspace"

    code = start_research.main(
        [
            "--provider",
            start_research.DEEPSEEK_PROVIDER,
            "--workspace",
            str(workspace),
            "--port",
            "8799",
        ]
    )

    # The first setup step failed, so the key question was never reached and the
    # server was never started: nothing was asked for, and nothing but the step
    # that failed was run.
    assert code == 1
    assert len(attempted) == 1
    assert list(attempted[0].argv[:3]) == ["uv", "run", "--no-project"]
    assert prompted == []
    assert all(start_research.KEY_ENV_NAME not in step.env for step in attempted)
    # The session document was written before the steps, and carries no secret.
    written = _written_sessions(workspace)
    assert len(written) == 1
    assert SYNTHETIC_TOKEN not in written[0].read_text(encoding="utf-8")


def test_the_plan_bootstraps_the_web_extra_and_reads_it_before_the_server(tmp_path: Path) -> None:
    """The environment the server needs is both asked for and actually read.

    A locked sync that installs the Web extra and a later step that resolves the
    environment without it leaves a directory that looks installed and an import
    that fails; the plan therefore has to carry the extra into the bootstrap and
    then *read* the resulting environment while nothing is listening.
    """
    workspace = tmp_path / "workspace"
    settings = start_research.prepare_settings(workspace, model=None)
    steps = start_research.plan_steps(
        project_root=PROJECT_ROOT,
        workspace=workspace,
        settings_file=settings,
        port=8799,
        offline=True,
        key=None,
    )
    argvs = [list(step.argv) for step in steps]

    bootstrap = next(argv for argv in argvs if any("bootstrap_local.py" in part for part in argv))
    assert "--web" in bootstrap

    preflights = [step for step in steps if "check_web_dependencies" in " ".join(step.argv)]
    assert len(preflights) == 1, argvs
    preflight = preflights[0]
    argv = list(preflight.argv)
    # The check runs the locked environment's own interpreter non-interactively,
    # not whatever python happened to start the launcher.
    assert argv[0] == str(_venv_python(PROJECT_ROOT))
    assert argv[1] == "-I"
    assert "-c" in argv
    code = argv[argv.index("-c") + 1]
    assert "ea.web.server" in code
    assert "check_web_dependencies" in code
    # It reads dependencies only: no root, no port, no settings document.
    assert "--workspace" not in argv

    # After the extra is installed, and before the one long-running child, so a
    # missing dependency is found while nothing is listening and no key exists.
    preflight_index = next(index for index, step in enumerate(steps) if step is preflight)
    sync_index = next(index for index, argv in enumerate(argvs) if "--extra" in argv)
    assert sync_index < preflight_index < len(steps) - 1
    assert list(steps[-1].argv[1:3]) == ["web", "serve"]

    for name in (*KEY_LIKE_NAMES, "RADIAN_SETTINGS_FILE", "PYTHONPATH", "VIRTUAL_ENV"):
        assert name not in preflight.env


def test_a_failed_web_dependency_preflight_asks_for_no_key_and_starts_no_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Web environment is reported before the key question is reached."""
    monkeypatch.setattr(start_research, "which", _all_found)
    attempted: list[start_research.Step] = []

    def failing_preflight(step: start_research.Step) -> None:
        attempted.append(step)
        if "check_web_dependencies" in " ".join(step.argv):
            raise start_research.LauncherError(
                "the Web dependency preflight failed: No module named 'starlette'"
            )

    monkeypatch.setattr(start_research, "run_step", failing_preflight)
    prompted: list[str] = []
    monkeypatch.setattr(start_research, "prompt_secret", _key_recorder(prompted))
    workspace = tmp_path / "workspace"

    code = start_research.main(
        [
            "--provider",
            start_research.DEEPSEEK_PROVIDER,
            "--workspace",
            str(workspace),
            "--port",
            "8799",
        ]
    )

    assert code == 1
    # The key is entered only after setup succeeds; the setup failed, so nothing
    # was asked for, and the listener that key belongs to never started.
    assert prompted == []
    assert not any(list(step.argv[1:3]) == ["web", "serve"] for step in attempted)

    preflight = next(step for step in attempted if "check_web_dependencies" in " ".join(step.argv))
    preflight_index = next(index for index, step in enumerate(attempted) if step is preflight)
    # Earlier setup ran, in the locked environment: the preflight reads that
    # environment rather than the interpreter that started the launcher.
    assert any("bootstrap_local.py" in " ".join(step.argv) for step in attempted)
    assert preflight_index > next(
        index for index, step in enumerate(attempted) if "--extra" in step.argv
    )
    assert preflight.argv[0] == str(_venv_python(PROJECT_ROOT))
    assert preflight.argv[1] == "-I"
    for name in (*KEY_LIKE_NAMES, "RADIAN_SETTINGS_FILE", "PYTHONPATH", "VIRTUAL_ENV"):
        assert name not in preflight.env
    assert all(SYNTHETIC_TOKEN not in value for value in preflight.env.values())


# --- the opt-in project `.env` ----------------------------------------------
#
# One more file source, off unless `--project-env` asks for it: a `.env` beside
# the launcher's own project root, read only for a provider launch whose mode was
# already stated on the command line. Every test below points the launcher's own
# project root at a directory this test owns — `main()` derives that root from the
# launcher's `__file__` — so the repository's real `.env`, if it has one, is never
# opened here, and every value in these files is a synthetic string.
#
# The reader itself is `start_research.read_project_env(path)`: the literal value
# of the one target variable, or `None` when the file or that value is absent,
# and a `LauncherError` — naming the path and nothing out of the file — when the
# file cannot be taken literally. It receives a path and reads nothing else.

PROJECT_ENV_NAME = ".env"
PROJECT_ENV_ARGV = ("--project-env",)
PROVIDER_ARGV = ("--provider", start_research.DEEPSEEK_PROVIDER)
KEY_SOURCE = start_research.KEY_ENV_NAME
PROJECT_ENV_VALUE = "synthetic-project-env-value-not-a-credential"
OTHER_ENV_VALUE = "a-value-belonging-to-another-variable"
CWD_ENV_VALUE = "value-from-the-working-directory-not-a-source"
HOME_ENV_VALUE = "value-from-the-home-directory-not-a-source"
# A file a reader has to refuse: the same variable assigned twice.
DUPLICATE_PROJECT_ENV = f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\n{KEY_SOURCE}=a-second-assignment\n"
# A file with an unrelated assignment beside the one variable that matters.
PROJECT_ENV_NOISE = f"OTHER={OTHER_ENV_VALUE}\n{KEY_SOURCE}={PROJECT_ENV_VALUE}\n"


def _fake_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A project root this test owns, named where the launcher finds its own.

    `main()` reads its project root from the launcher's `__file__`, so pointing
    that at a file inside a temporary directory is what keeps these tests off the
    repository's real `.env` and away from its real toolchain. The path is resolved
    here exactly as `main()` resolves it, so a symlinked temporary directory (macOS
    `/var` under `/private/var`) cannot make these two names differ.
    """
    project = (tmp_path / "project").resolve()
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    script = project / "scripts" / "start_research.py"
    monkeypatch.setattr(start_research, "__file__", str(script))
    return project


def _project_env_path(project: Path) -> Path:
    return project / PROJECT_ENV_NAME


def _write_project_env(project: Path, text: str) -> Path:
    path = _project_env_path(project)
    path.write_text(text, encoding="utf-8")
    return path


def _stub_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, env_text: str | None = None
) -> tuple[Path, list[start_research.Step], list[str]]:
    """One launch over a fake project whose every child is recorded, never run.

    Returns the fake project root, the steps actually run, and the prompts the key
    question was asked with, so a test can tell a launch that read the file from
    one that asked at the terminal.
    """
    monkeypatch.setattr(start_research, "which", _all_found)
    project = _fake_project_root(monkeypatch, tmp_path)
    if env_text is not None:
        _write_project_env(project, env_text)
    executed: list[start_research.Step] = []
    monkeypatch.setattr(start_research, "run_step", executed.append)
    prompted: list[str] = []
    monkeypatch.setattr(start_research, "prompt_secret", _key_recorder(prompted))
    return project, executed, prompted


def _run(capsys: pytest.CaptureFixture[str], argv: Sequence[str]) -> tuple[int, str]:
    """Run one launcher command and return its status and everything it printed."""
    code = start_research.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_the_project_env_flag_is_refused_without_an_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The file source is a provider decision: stated without one, it is refused.

    Stated beside `--offline`, or on a launch — or a check — that has not chosen a
    provider yet, it is refused before the mode question is asked, before anything
    is created and before the file is opened, so a user who typed it by mistake
    still has a host exactly as they left it.
    """
    _project, executed, prompted = _stub_launch(
        monkeypatch,
        tmp_path,
        env_text=DUPLICATE_PROJECT_ENV,
    )
    asked: list[str] = []

    def _refuse_to_ask(prompt: str = "") -> str:
        asked.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", _refuse_to_ask)
    workspace = tmp_path / "workspace"

    for argv in (
        [*PROJECT_ENV_ARGV, "--offline", "--workspace", str(workspace)],
        [*PROJECT_ENV_ARGV, "--check", "--workspace", str(workspace)],
        [*PROJECT_ENV_ARGV, "--workspace", str(workspace)],
    ):
        code, report = _run(capsys, argv)
        assert code == 2, (argv, report)
        # The refusal names the flag, so what is reported is this conflict and not
        # the mode question or the terminal question this launch never reached.
        assert "--project-env" in report, (argv, report)
        assert PROJECT_ENV_VALUE not in report, (argv, report)

    assert executed == []
    assert prompted == []
    assert asked == []
    assert not workspace.exists()


def test_without_the_flag_the_project_env_file_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in source is off by default, and an offline launch never has one.

    The file placed here is one a reader has to refuse — the same variable twice —
    so a launch that opened it would stop. The provider launch asks at the terminal
    instead, and the offline launch asks for nothing at all.
    """
    project, executed, prompted = _stub_launch(
        monkeypatch,
        tmp_path,
        env_text=DUPLICATE_PROJECT_ENV,
    )
    env_file = _project_env_path(project)
    before = env_file.read_text(encoding="utf-8")
    workspace = tmp_path / "workspace"
    provider_argv = [*PROVIDER_ARGV, "--workspace", str(workspace), "--port", "8799"]

    assert start_research.main(provider_argv) == 0
    server = _server_step(executed)
    assert server.env[KEY_SOURCE] == SYNTHETIC_TOKEN
    assert len(prompted) == 1
    assert all(PROJECT_ENV_VALUE not in value for step in executed for value in step.env.values())
    assert all(PROJECT_ENV_VALUE not in " ".join(step.argv) for step in executed)

    asked_before = list(prompted)
    executed.clear()
    assert start_research.main(["--offline", "--workspace", str(workspace)]) == 0
    assert prompted == asked_before
    assert KEY_SOURCE not in _server_step(executed).env
    # The provider session carries a provider; the offline one carries none, so
    # the mode that cannot make a request is the mode that was written.
    written = _written_sessions(workspace)
    assert sum("model" in _document(path) for path in written) == 1
    # Nothing read the file, and nothing wrote it either.
    assert env_file.read_text(encoding="utf-8") == before


def test_only_the_project_root_is_looked_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no search: the working directory and the home directory are not sources."""
    _project, executed, prompted = _stub_launch(monkeypatch, tmp_path)
    working = tmp_path / "working"
    working.mkdir()
    (working / PROJECT_ENV_NAME).write_text(f"{KEY_SOURCE}={CWD_ENV_VALUE}\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / PROJECT_ENV_NAME).write_text(f"{KEY_SOURCE}={HOME_ENV_VALUE}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(working)
    workspace = tmp_path / "workspace"
    argv = [*PROVIDER_ARGV, *PROJECT_ENV_ARGV, "--workspace", str(workspace), "--port", "8799"]

    code, report = _run(capsys, argv)

    # The project root holds no `.env`, so the terminal asks, and neither stray
    # file is a source: nothing from them reaches a step or the screen.
    assert code == 0
    assert len(prompted) == 1
    assert _server_step(executed).env[KEY_SOURCE] == SYNTHETIC_TOKEN
    values = [value for step in executed for value in step.env.values()]
    assert not any(CWD_ENV_VALUE in value for value in values)
    assert not any(HOME_ENV_VALUE in value for value in values)
    assert CWD_ENV_VALUE not in report and HOME_ENV_VALUE not in report


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\n", PROJECT_ENV_VALUE),
        (f"{KEY_SOURCE} = {PROJECT_ENV_VALUE}\n", PROJECT_ENV_VALUE),
        (f"{KEY_SOURCE} ={PROJECT_ENV_VALUE}   \n", PROJECT_ENV_VALUE),
        (f'{KEY_SOURCE}="{PROJECT_ENV_VALUE}"\n', PROJECT_ENV_VALUE),
        (f"{KEY_SOURCE}='{PROJECT_ENV_VALUE}'\n", PROJECT_ENV_VALUE),
        (
            f"# the key, if you keep one here\n\nOTHER={OTHER_ENV_VALUE}\n"
            f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\n",
            PROJECT_ENV_VALUE,
        ),
        (PROJECT_ENV_NOISE, PROJECT_ENV_VALUE),
        # A CRLF file, and ordinary tabs around the assignment: line syntax this reads.
        (f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\r\n", PROJECT_ENV_VALUE),
        (f"{KEY_SOURCE}\t=\t{PROJECT_ENV_VALUE}\n", PROJECT_ENV_VALUE),
        # Nothing is expanded: what the file holds is what the value is.
        (f"{KEY_SOURCE}=${{NOT_A_VARIABLE}}\n", "${NOT_A_VARIABLE}"),
        # A `#` inside matching quotes is part of the value, not a comment.
        (f'{KEY_SOURCE}="#{OTHER_ENV_VALUE}"\n', f"#{OTHER_ENV_VALUE}"),
    ],
)
def test_the_project_env_reader_takes_one_literal_value(
    tmp_path: Path, text: str, expected: str
) -> None:
    """One target variable, one line: the value is the text, and never an expansion."""
    path = tmp_path / PROJECT_ENV_NAME
    path.write_text(text, encoding="utf-8")

    assert start_research.read_project_env(path) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n   \n",
        "# only a comment\n",
        f"OTHER={OTHER_ENV_VALUE}\n",
        f"# {KEY_SOURCE}={PROJECT_ENV_VALUE}\n",
        f"{KEY_SOURCE}=\n",
        f"{KEY_SOURCE}   =\n",
        f'{KEY_SOURCE}=""\n',
    ],
)
def test_a_project_env_file_naming_no_value_yields_nothing(tmp_path: Path, text: str) -> None:
    """A file that names no value is not a failure: it is simply no answer."""
    path = tmp_path / PROJECT_ENV_NAME
    path.write_text(text, encoding="utf-8")

    assert start_research.read_project_env(path) is None


def test_a_missing_project_env_file_yields_nothing(tmp_path: Path) -> None:
    assert start_research.read_project_env(tmp_path / PROJECT_ENV_NAME) is None


@pytest.mark.parametrize(
    ("text", "kept_out", "reason"),
    [
        (f"{KEY_SOURCE}=first-value\n{KEY_SOURCE}=second-value\n", "second", "more than once"),
        (f"{KEY_SOURCE}=value-with-nul\x00here\n", "value-with-nul", "control character"),
        (f'{KEY_SOURCE}="never-closed\n', "never-closed", "quote"),
        (f'{KEY_SOURCE}="first-line\nsecond-line"\n', "second-line", "quote"),
        (f"{KEY_SOURCE}=value-with-a-control\x07here\n", "value-with-a-control", "control"),
        # A line this format cannot read is refused, never ignored and never half-taken.
        (f"{KEY_SOURCE} synthetic-only\n", "synthetic-only", "NAME=value assignment"),
        (f"{KEY_SOURCE}\n", KEY_SOURCE, "NAME=value assignment"),
        ("a line of prose rather than an assignment\n", "prose", "NAME=value assignment"),
        (f"{KEY_SOURCE}=first-half\ncontinued-value\n", "half", "NAME=value assignment"),
        (f"{KEY_SOURCE}=value\nand a stray line\n", "stray", "NAME=value assignment"),
        # The shell's `export` form and an inline comment are stated, not passed over.
        (f"export {KEY_SOURCE}={PROJECT_ENV_VALUE}\n", PROJECT_ENV_VALUE, "export"),
        (f"export OTHER={OTHER_ENV_VALUE}\n", OTHER_ENV_VALUE, "export"),
        ("export OTHER\n", "OTHER", "export"),
        (f"{KEY_SOURCE}=value # note\n", "note", "inline comment"),
        # A control beside the value or around the assignment is never trimmed away.
        (f"{KEY_SOURCE}=synthetic-only\x0b\n", "synthetic-only", "control character"),
        (f"{KEY_SOURCE}=\x0bvalue\n", "value", "control character"),
        (f"{KEY_SOURCE}=va\x0blue\n", "value", "control character"),
        (f"{KEY_SOURCE}=value\x0c\n", "value", "control character"),
        (f"\x0b{KEY_SOURCE}=value\n", "value", "control character"),
    ],
)
def test_a_malformed_project_env_file_is_refused_without_echoing_it(
    tmp_path: Path, text: str, kept_out: str, reason: str
) -> None:
    """A file that cannot be taken literally is a refusal, never a value and never a prompt.

    `str.strip` removes some control characters, so a reader that trims a line before it
    is checked would quietly accept what is left of it; every case here is refused
    instead, and for the reason it names.
    """
    path = tmp_path / PROJECT_ENV_NAME
    path.write_text(text, encoding="utf-8")

    with pytest.raises(start_research.LauncherError) as failure:
        start_research.read_project_env(path)

    message = str(failure.value)
    # The refusal names the file so it can be fixed and says which form it could not
    # read, and carries no line, no value and no decoder or operating-system detail.
    assert str(path) in message
    assert reason in message
    assert kept_out not in message
    assert len(message) < 500


def test_a_project_env_that_is_a_fifo_is_refused_without_reading_it(tmp_path: Path) -> None:
    """A FIFO is refused from its own status, before anything could open it."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("this host cannot make a FIFO")
    path = tmp_path / PROJECT_ENV_NAME
    os.mkfifo(path)

    with pytest.raises(start_research.LauncherError) as failure:
        start_research.read_project_env(path)

    assert "not a regular file" in str(failure.value)


def test_a_fifo_put_in_place_of_the_file_cannot_block_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open is non-blocking, and what it opened is checked before it is read.

    The path is a regular file when the reader first looks at it and a FIFO with no
    writer by the moment it is opened — the window between those two checks. A
    blocking open waits there forever, so this test fails on the flags it was called
    with instead of hanging, and the refusal comes from the descriptor that was
    actually opened rather than from the path that was checked.
    """
    if not hasattr(os, "mkfifo"):
        pytest.skip("this host cannot make a FIFO")
    path = tmp_path / PROJECT_ENV_NAME
    path.write_text(f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\n", encoding="utf-8")
    real_open = os.open
    seen: list[int] = []

    def _open_beside_a_fifo(target: os.PathLike[str], flags: int) -> int:
        path.unlink()
        os.mkfifo(path)
        assert flags & os.O_NONBLOCK, "this open of a FIFO with no writer would wait"
        seen.append(flags)
        return real_open(target, flags)

    monkeypatch.setattr(os, "open", _open_beside_a_fifo)

    with pytest.raises(start_research.LauncherError) as failure:
        start_research.read_project_env(path)

    assert seen, "the path was opened"
    assert str(path) in str(failure.value)
    assert PROJECT_ENV_VALUE not in str(failure.value)


def test_a_project_env_file_that_is_not_a_plain_file_is_refused(tmp_path: Path) -> None:
    """A link, a directory or something too large is refused, not followed or read."""
    real = tmp_path / "real.env"
    real.write_text(f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\n", encoding="utf-8")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    directory = tmp_path / "dir.env"
    directory.mkdir()

    for path in (link, directory):
        with pytest.raises(start_research.LauncherError) as failure:
            start_research.read_project_env(path)
        assert str(path) in str(failure.value)

    oversized = tmp_path / "oversized.env"
    oversized.write_bytes(b"#" + b"x" * (4 * 1024 * 1024))
    with pytest.raises(start_research.LauncherError) as failure:
        start_research.read_project_env(oversized)
    assert str(oversized) in str(failure.value)


def test_a_project_env_file_that_is_not_text_is_refused(tmp_path: Path) -> None:
    path = tmp_path / PROJECT_ENV_NAME
    path.write_bytes(f"{KEY_SOURCE}=".encode() + b"\xff\xfe\x80value-with-bad-bytes\n")

    with pytest.raises(start_research.LauncherError) as failure:
        start_research.read_project_env(path)

    message = str(failure.value)
    assert "value-with-bad-bytes" not in message
    assert "codec" not in message.lower()


def test_an_unreadable_project_env_file_is_refused_safely(tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("a root process can read a file with no permission bits")
    path = tmp_path / PROJECT_ENV_NAME
    path.write_text(f"{KEY_SOURCE}={PROJECT_ENV_VALUE}\n", encoding="utf-8")
    path.chmod(0o000)

    try:
        with pytest.raises(start_research.LauncherError) as failure:
            start_research.read_project_env(path)
    finally:
        path.chmod(0o600)

    message = str(failure.value)
    assert "Errno" not in message
    assert PROJECT_ENV_VALUE not in message


def test_the_project_env_value_reaches_only_the_final_server_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value from the file is this session's key: one child, one variable, no echo."""
    project, executed, prompted = _stub_launch(
        monkeypatch,
        tmp_path,
        env_text=PROJECT_ENV_NOISE,
    )
    # A key this process already holds: it is neither the value used nor changed.
    monkeypatch.setenv(KEY_SOURCE, INHERITED_KEY)
    workspace = tmp_path / "workspace"
    argv = [*PROVIDER_ARGV, *PROJECT_ENV_ARGV, "--workspace", str(workspace), "--port", "8799"]

    code, report = _run(capsys, argv)

    assert code == 0
    # The file answered, so the hidden prompt was never reached.
    assert prompted == []
    server = _server_step(executed)
    assert server.env[KEY_SOURCE] == PROJECT_ENV_VALUE
    # No setup step — bootstrap, sync, preflight, build — is handed a credential.
    assert all(KEY_SOURCE not in step.env for step in executed[:-1])
    assert all(PROJECT_ENV_VALUE not in " ".join(step.argv) for step in executed)
    assert all(
        PROJECT_ENV_VALUE not in value for step in executed[:-1] for value in step.env.values()
    )
    # The session document names the variable, never the value.
    stored = _written_sessions(workspace)[0].read_text(encoding="utf-8")
    assert KEY_SOURCE in stored
    assert PROJECT_ENV_VALUE not in stored
    # Nothing printed the value, the inherited key is not reused, and this
    # process's own environment is exactly as it was.
    assert PROJECT_ENV_VALUE not in report
    assert all(INHERITED_KEY not in value for step in executed for value in step.env.values())
    assert os.environ[KEY_SOURCE] == INHERITED_KEY
    # The file is a source, never a target.
    assert _project_env_path(project).read_text(encoding="utf-8") == PROJECT_ENV_NOISE


def test_the_flag_works_with_the_model_id_that_chooses_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--model` states the provider too, so it opts into the file source the same way."""
    _project, executed, prompted = _stub_launch(
        monkeypatch,
        tmp_path,
        env_text=PROJECT_ENV_NOISE,
    )
    workspace = tmp_path / "workspace"
    argv = [
        "--model",
        start_research.DEFAULT_MODEL,
        *PROJECT_ENV_ARGV,
        "--workspace",
        str(workspace),
        "--port",
        "8799",
    ]

    code = start_research.main(argv)

    assert code == 0
    assert prompted == []
    assert _server_step(executed).env[KEY_SOURCE] == PROJECT_ENV_VALUE


@pytest.mark.parametrize(
    "env_text",
    [
        None,
        "",
        f"OTHER={OTHER_ENV_VALUE}\n",
        f"{KEY_SOURCE}=\n",
        f'{KEY_SOURCE}=""\n',
    ],
)
def test_a_project_env_file_without_a_value_falls_back_to_the_hidden_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env_text: str | None,
) -> None:
    """No file, no variable, or an empty value: the terminal asks, as it always did."""
    project, executed, prompted = _stub_launch(monkeypatch, tmp_path, env_text=env_text)
    workspace = tmp_path / "workspace"

    argv = [*PROVIDER_ARGV, *PROJECT_ENV_ARGV, "--workspace", str(workspace), "--port", "8799"]
    code, report = _run(capsys, argv)

    assert code == 0
    assert len(prompted) == 1
    assert _server_step(executed).env[KEY_SOURCE] == SYNTHETIC_TOKEN
    # The fallback explains itself without carrying anything out of the file.
    assert str(_project_env_path(project)) in report
    assert OTHER_ENV_VALUE not in report


@pytest.mark.parametrize(
    "env_text",
    [
        DUPLICATE_PROJECT_ENV,
        f"{KEY_SOURCE}=value-with-nul\x00here\n",
        f'{KEY_SOURCE}="never-closed\n',
    ],
)
def test_a_malformed_project_env_file_refuses_the_launch_rather_than_asking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env_text: str,
) -> None:
    """A file that cannot be read cleanly stops the launch; it never becomes a prompt."""
    project, executed, prompted = _stub_launch(monkeypatch, tmp_path, env_text=env_text)
    workspace = tmp_path / "workspace"

    argv = [*PROVIDER_ARGV, *PROJECT_ENV_ARGV, "--workspace", str(workspace), "--port", "8799"]
    code, report = _run(capsys, argv)

    assert code != 0
    assert prompted == []
    assert not any(list(step.argv[1:3]) == ["web", "serve"] for step in executed)
    # The refusal names the file it could not use, and echoes nothing from it.
    assert str(_project_env_path(project)) in report
    for leaked in ("a-second-assignment", "value-with-nul", "never-closed"):
        assert leaked not in report


def test_a_check_names_the_planned_key_source_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A check reports where a key would come from and opens nothing to say so."""
    project, executed, prompted = _stub_launch(
        monkeypatch,
        tmp_path,
        env_text=DUPLICATE_PROJECT_ENV,
    )
    workspace = tmp_path / "workspace"
    argv = ["--check", *PROVIDER_ARGV, *PROJECT_ENV_ARGV, "--workspace", str(workspace)]

    code, report = _run(capsys, argv)

    assert code == 0
    # The planned source is named, and nothing was read to name it: the file here
    # is one a reader has to refuse.
    assert str(_project_env_path(project)) in report
    assert PROJECT_ENV_VALUE not in report
    assert prompted == []
    assert executed == []
    assert not workspace.exists()

    # Without the flag the planned source is still the terminal, and the project
    # file is not part of the plan at all.
    code, plain = _run(capsys, ["--check", *PROVIDER_ARGV, "--workspace", str(workspace)])
    assert code == 0
    assert str(_project_env_path(project)) not in plain


def test_a_setup_failure_stops_before_the_project_env_file_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The file is read after setup succeeds, so a failed setup never reaches it."""
    monkeypatch.setattr(start_research, "which", _all_found)
    project = _fake_project_root(monkeypatch, tmp_path)
    _write_project_env(project, DUPLICATE_PROJECT_ENV)
    attempted: list[start_research.Step] = []

    def failing_step(step: start_research.Step) -> None:
        attempted.append(step)
        raise start_research.LauncherError("this setup step exited with status 1")

    monkeypatch.setattr(start_research, "run_step", failing_step)
    prompted: list[str] = []
    monkeypatch.setattr(start_research, "prompt_secret", _key_recorder(prompted))
    workspace = tmp_path / "workspace"

    argv = [*PROVIDER_ARGV, *PROJECT_ENV_ARGV, "--workspace", str(workspace), "--port", "8799"]
    code, report = _run(capsys, argv)

    assert code == 1
    assert len(attempted) == 1
    # The failure reported is the step that failed: had the file been read first,
    # this one — which a reader has to refuse — would be what stopped the launch.
    assert "this setup step exited with status 1" in report
    assert "a-second-assignment" not in report
    assert prompted == []
    assert not any(list(step.argv[1:3]) == ["web", "serve"] for step in attempted)
