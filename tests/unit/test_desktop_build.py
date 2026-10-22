"""Artifact selection only; external build and install commands are mocked."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_build_installs_only_its_own_temporary_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = script("build_desktop")
    out = tmp_path / "desktop"
    legacy = out / "wheels"
    legacy.mkdir(parents=True)
    old = legacy / "ea_quant-0.1.0-py3-none-any.whl"
    old.write_bytes(b"previous build")
    fixed = out / "build-wheels"
    fixed.mkdir()
    sentinel = fixed / "sentinel.txt"
    sentinel.write_text("keep")
    current = "ea_quant-0.2.0-py3-none-any.whl"
    builds: list[Path] = []
    installs: list[Path] = []

    def run(*args: Any, cwd: Path | None = None) -> None:
        if args[:2] == ("uv", "build"):
            directory = Path(args[args.index("--out-dir") + 1])
            assert directory.parent == out
            assert directory.name.startswith("build-wheels-")
            assert not list(directory.iterdir())
            (directory / current).touch()
            builds.append(directory)
        elif args[:3] == ("uv", "pip", "install"):
            installed = Path(args[-1])
            assert installed.exists()
            assert installed.parent == builds[-1]
            assert installed.name == current
            installs.append(installed)

    monkeypatch.setattr(module, "run", run)
    assert module.build_and_install_wheel(out, tmp_path / "python") is None
    assert module.build_and_install_wheel(out, tmp_path / "python") is None

    assert len(builds) == 2
    assert builds[0] != builds[1]
    assert len(installs) == 2
    assert not any(path.exists() for path in builds)
    assert old.read_bytes() == b"previous build"
    assert [path.name for path in legacy.iterdir()] == [old.name]
    assert sentinel.read_text() == "keep"
    assert [path.name for path in fixed.iterdir()] == [sentinel.name]


def test_build_refuses_an_empty_wheel_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = script("build_desktop")
    calls: list[tuple[Any, ...]] = []

    def run(*args: Any, cwd: Path | None = None) -> None:
        calls.append(args)

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(SystemExit, match="exactly one"):
        module.build_and_install_wheel(tmp_path / "desktop", tmp_path / "python")
    assert not any(call[:3] == ("uv", "pip", "install") for call in calls)


def test_build_refuses_multiple_candidate_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = script("build_desktop")
    calls: list[tuple[Any, ...]] = []

    def run(*args: Any, cwd: Path | None = None) -> None:
        calls.append(args)
        if args[:2] == ("uv", "build"):
            directory = Path(args[args.index("--out-dir") + 1])
            (directory / "ea_quant-0.2.0-py3-none-any.whl").touch()
            (directory / "ea_quant-0.2.0-py3-none-any-other.whl").touch()

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(SystemExit, match="exactly one"):
        module.build_and_install_wheel(tmp_path / "desktop", tmp_path / "python")
    assert not any(call[:3] == ("uv", "pip", "install") for call in calls)
