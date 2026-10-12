"""Web dependency provisioning: the bootstrap's Web extra and the shared check.

The local Web application needs an optional dependency set a default developer
environment does not carry. Three places have to agree about that: the bootstrap
every launch runs, the launcher's own dependency preflight, and the shared check
in `ea.web.server` that the preflight imports.

These tests describe one outcome. An environment built for the server really
contains the ASGI stack, and an environment that does not is reported precisely
— the module that is missing, the symbol that is not callable, and the
interpreter that looked — instead of surfacing later as a bare
`ModuleNotFoundError` from a directory that only looks installed.

Nothing here installs a package, binds a socket, starts a server, reads a
settings file or touches a key.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import ea.web.server as server  # noqa: E402
from ea.web.app import WebSettings  # noqa: E402
from scripts import bootstrap_local  # noqa: E402


def _venv_python(environment: Path) -> Path:
    """The interpreter of the environment the bootstrap builds."""
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    return scripts / ("python.exe" if os.name == "nt" else "python")


def _extras(command: Sequence[str]) -> list[str]:
    """Every `--extra` value in one command, in the order it was given."""
    return [command[index + 1] for index, part in enumerate(command) if part == "--extra"]


class _CommandRecorder:
    """Records each bootstrap command and environment instead of running it."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        self.commands.append(list(command))
        self.envs.append(dict(env))


def _run_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: Sequence[str],
) -> tuple[_CommandRecorder, Path]:
    """Run the real bootstrap entrypoint with every command captured, not executed."""
    recorder = _CommandRecorder()
    environment = tmp_path / "venv"
    monkeypatch.setattr(bootstrap_local, "find_uv", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(bootstrap_local, "ENVIRONMENT_DIR", environment)
    monkeypatch.setattr(bootstrap_local, "LEGACY_ENVIRONMENT_DIR", tmp_path / "legacy-venv")
    monkeypatch.setattr(bootstrap_local, "run", recorder)
    assert bootstrap_local.main(list(argv)) == 0
    return recorder, environment


def _sync_command(recorder: _CommandRecorder) -> list[str]:
    return next(command for command in recorder.commands if command[1:3] == ["sync", "--locked"])


def _doctor_command(recorder: _CommandRecorder) -> list[str]:
    return next(command for command in recorder.commands if command[1:3] == ["run", "--locked"])


def _available_stack() -> None:
    """These readings are about a real stack; without one there is nothing to read."""
    pytest.importorskip("fastapi")
    pytest.importorskip("starlette")
    pytest.importorskip("uvicorn")


def _fake_uvicorn(**attributes: object) -> types.ModuleType:
    """A stand-in `uvicorn` module, so no test needs to bind a socket."""
    module = types.ModuleType("uvicorn")
    for name, value in attributes.items():
        setattr(module, name, value)
    return module


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        scenario_root=tmp_path / "scenarios",
        workspace=tmp_path / "workspace",
        ui_dir=tmp_path / "ui",
        port=9123,
    )


def test_the_web_bootstrap_carries_the_extra_through_sync_and_the_final_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The install and the verification the bootstrap ends with both ask for Web."""
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else/venv")

    recorder, environment = _run_bootstrap(tmp_path, monkeypatch, ["--web"])

    sync = _sync_command(recorder)
    doctor = _doctor_command(recorder)
    # A sync with the extra followed by a doctor that re-resolves without it is
    # exactly the environment this repair is about, so both carry the extra.
    assert "web" in _extras(sync) and "dev" in _extras(sync)
    assert "web" in _extras(doctor) and "dev" in _extras(doctor)
    assert "ea" in doctor and "doctor" in doctor

    # The installed-version assertion runs the locked interpreter itself rather
    # than whatever python started the bootstrap.
    smoke = next(command for command in recorder.commands if "-c" in command)
    assert smoke[0] == str(_venv_python(environment))

    # The environment protections the bootstrap already had are unchanged.
    for env in recorder.envs:
        assert env["UV_PROJECT_ENVIRONMENT"] == str(environment)
        assert "PYTHONPATH" not in env
        assert "VIRTUAL_ENV" not in env


def test_the_default_bootstrap_stays_dev_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the flag, nothing about the dev-only default changes."""
    recorder, _ = _run_bootstrap(tmp_path, monkeypatch, [])

    assert _extras(_sync_command(recorder)) == ["dev"]
    assert "web" not in _extras(_doctor_command(recorder))


def test_the_shared_check_passes_on_the_installed_stack_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed stack names every symbol the server needs; reading it is inert."""
    _available_stack()
    opened: list[str] = []

    def no_socket(*args: object, **kwargs: object) -> object:
        opened.append("socket")
        raise AssertionError("the dependency check must not open a socket")

    def no_application(settings: object) -> object:
        raise AssertionError("the dependency check must not build the application")

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(server, "create_app", no_application)
    before = dict(os.environ)

    server.check_web_dependencies()

    # It reads the installed packages and nothing else: no listener, no
    # workspace, no settings file and no change to the environment it runs in.
    assert opened == []
    assert dict(os.environ) == before


def test_a_missing_starlette_behind_fastapi_is_named_precisely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import that actually fails is reported, with the interpreter that looked."""
    _available_stack()
    for name in [name for name in sys.modules if name.split(".")[0] in ("fastapi", "starlette")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    # The damaged install: `import fastapi` is attempted again, and the
    # distribution its own imports need is gone.
    monkeypatch.setitem(sys.modules, "starlette", None)

    with pytest.raises(server.WebDependencyError) as failure:
        server.check_web_dependencies()

    message = str(failure.value)
    assert re.search(r"\bstarlette\b", message)
    assert sys.executable in message


def test_a_uvicorn_without_a_callable_run_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace-only package imports cleanly and still cannot serve."""
    _available_stack()
    namespace = types.ModuleType("uvicorn")
    cast(Any, namespace).__path__ = []
    monkeypatch.setitem(sys.modules, "uvicorn", namespace)

    with pytest.raises(server.WebDependencyError) as failure:
        server.check_web_dependencies()

    message = str(failure.value)
    assert re.search(r"\buvicorn\b", message)
    assert re.search(r"\brun\b", message)
    assert sys.executable in message


def test_serve_local_web_keeps_the_missing_module_detail_from_create_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A dependency the application needs is named, and nothing starts."""
    _available_stack()
    started: list[str] = []
    monkeypatch.setitem(
        sys.modules, "uvicorn", _fake_uvicorn(run=lambda *args, **kwargs: started.append("run"))
    )

    def missing(settings: WebSettings) -> object:
        raise ModuleNotFoundError("No module named 'multipart'", name="multipart")

    monkeypatch.setattr(server, "create_app", missing)

    with pytest.raises(server.WebDependencyError) as failure:
        server.serve_local_web(_settings(tmp_path))

    assert "multipart" in str(failure.value)
    assert started == []


def test_an_application_bug_is_not_reported_as_a_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing application is not swallowed as a successful dependency read."""
    _available_stack()
    monkeypatch.setitem(sys.modules, "uvicorn", _fake_uvicorn(run=lambda *args, **kwargs: None))

    def broken(settings: WebSettings) -> object:
        raise ValueError("a bug in the application, not a missing dependency")

    monkeypatch.setattr(server, "create_app", broken)

    with pytest.raises(ValueError):
        server.serve_local_web(_settings(tmp_path))


def test_serve_local_web_still_starts_the_one_fixed_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The check is added in front of the same server, not instead of it."""
    _available_stack()
    calls: list[dict[str, object]] = []

    def run(application: object, **kwargs: object) -> None:
        calls.append({"application": application, **kwargs})

    monkeypatch.setitem(sys.modules, "uvicorn", _fake_uvicorn(run=run))
    application = object()
    monkeypatch.setattr(server, "create_app", lambda settings: application)

    server.serve_local_web(_settings(tmp_path))

    assert calls == [
        {
            "application": application,
            "host": "127.0.0.1",
            "port": 9123,
            "proxy_headers": False,
            "server_header": False,
        }
    ]
