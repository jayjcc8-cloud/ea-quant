#!/usr/bin/env python3
"""Run the repository-owned quality and release verification profiles."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
BUILD_CONSTRAINTS_PATH = PROJECT_ROOT / "build-constraints.txt"
UV_VERSION_PATTERN = re.compile(r"==(?P<version>[0-9]+\.[0-9]+\.[0-9]+)")
UV_OUTPUT_PATTERN = re.compile(r"uv (?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\s.*)?")


class VerificationError(RuntimeError):
    """Raised when repository verification inputs or evidence are invalid."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Version values that have one source of truth in pyproject.toml."""

    project_version: str
    required_uv_version: str


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise VerificationError(f"{field} must be a TOML table")
    return cast(dict[str, object], value)


def load_project_config(path: Path = PYPROJECT_PATH) -> ProjectConfig:
    """Load the project and uv versions without duplicating them in scripts or CI."""
    with path.open("rb") as handle:
        document = _mapping(tomllib.load(handle), "pyproject")

    project = _mapping(document.get("project"), "project")
    project_version = project.get("version")
    if not isinstance(project_version, str) or not project_version:
        raise VerificationError("project.version must be a non-empty string")

    tool = _mapping(document.get("tool"), "tool")
    uv = _mapping(tool.get("uv"), "tool.uv")
    required_version = uv.get("required-version")
    if not isinstance(required_version, str):
        raise VerificationError("tool.uv.required-version must be a string")
    match = UV_VERSION_PATTERN.fullmatch(required_version)
    if match is None:
        raise VerificationError("tool.uv.required-version must be an exact ==X.Y.Z pin")

    return ProjectConfig(
        project_version=project_version,
        required_uv_version=match.group("version"),
    )


def parse_uv_version(output: str) -> str:
    """Return the semantic version from canonical ``uv --version`` output."""
    match = UV_OUTPUT_PATTERN.fullmatch(output.strip())
    if match is None:
        raise VerificationError(f"unexpected uv --version output: {output.strip()!r}")
    return match.group("version")


def version_smoke(project_version: str) -> str:
    """Build an isolated import assertion from the configured project version."""
    return (
        "import importlib.metadata as metadata; import ea; "
        f"assert metadata.version('ea-quant') == ea.__version__ == {project_version!r}; "
        "print(ea.__version__)"
    )


def find_uv() -> str:
    """Return the global uv executable or fail with actionable guidance."""
    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError(
            "uv was not found on PATH; install the exact version required by pyproject.toml"
        )
    return str(Path(uv).resolve())


def verification_environment(environment_dir: Path) -> dict[str, str]:
    """Return an isolated process environment for repository verification."""
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(environment_dir.resolve())
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def environment_dir() -> Path:
    """Resolve the configured project environment relative to the repository."""
    configured = Path(os.environ.get("UV_PROJECT_ENVIRONMENT", "venv"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def run(
    command: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    env: Mapping[str, str],
) -> None:
    """Run one visible command and fail immediately on a non-zero exit."""
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, check=True, cwd=cwd, env=dict(env))


def run_capture(
    command: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    env: Mapping[str, str],
) -> str:
    """Run one visible command and return stripped standard output."""
    print(f"+ {shlex.join(command)}", flush=True)
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip()
    if output:
        print(output, flush=True)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return output


def environment_tools(environment: Path) -> tuple[Path, Path]:
    """Return the project Python and ea entrypoint paths."""
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    entrypoint = scripts / ("ea.exe" if os.name == "nt" else "ea")
    if not python.is_file() or not entrypoint.is_file():
        raise VerificationError(f"project environment is incomplete: {environment}")
    return python, entrypoint


def verify_uv(uv: str, config: ProjectConfig, env: Mapping[str, str]) -> None:
    """Require the active uv executable to match the exact project pin."""
    installed = parse_uv_version(run_capture([uv, "--version"], env=env))
    if installed != config.required_uv_version:
        raise VerificationError(
            f"uv {installed} is active; project requires {config.required_uv_version}"
        )


def verify_quality(uv: str, config: ProjectConfig, env: Mapping[str, str]) -> None:
    """Run the locked environment, static analysis, test, and CLI quality gates."""
    verify_uv(uv, config, env)
    run([uv, "lock", "--check"], env=env)
    run(["git", "diff", "--exit-code", "HEAD", "--", "uv.lock"], env=env)
    run([uv, "sync", "--locked", "--extra", "dev"], env=env)

    python, entrypoint = environment_tools(environment_dir())
    build_tool_smoke = (
        "import importlib.util as util; "
        "assert util.find_spec('setuptools') is None; "
        "assert util.find_spec('wheel') is None"
    )
    with tempfile.TemporaryDirectory(prefix="ea-quality-smoke-") as temporary_directory:
        outside_repository = Path(temporary_directory)
        run([str(python), "-I", "-c", build_tool_smoke], cwd=outside_repository, env=env)
        run(
            [str(python), "-I", "-c", version_smoke(config.project_version)],
            cwd=outside_repository,
            env=env,
        )
        run([str(entrypoint), "doctor"], cwd=outside_repository, env=env)

    run([uv, "run", "--locked", "ruff", "check", "."], env=env)
    run([uv, "run", "--locked", "ruff", "format", "--check", "."], env=env)
    run([uv, "run", "--locked", "mypy"], env=env)
    run([uv, "run", "--locked", "pytest", "-q"], env=env)
    run([uv, "run", "--locked", "ea", "doctor"], env=env)


def single_wheel(directory: Path) -> Path:
    """Return the only wheel in a canonical build output directory."""
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise VerificationError(f"expected one wheel in {directory}, found {len(wheels)}")
    return wheels[0]


def verify_wheel_metadata(wheel: Path) -> None:
    """Require one readable WHEEL metadata file and print it as build evidence."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = sorted(
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        )
        if len(metadata_files) != 1:
            raise VerificationError(
                f"expected one .dist-info/WHEEL in {wheel}, found {len(metadata_files)}"
            )
        print(archive.read(metadata_files[0]).decode("utf-8"), end="", flush=True)


def verify_full(uv: str, config: ProjectConfig, env: Mapping[str, str]) -> None:
    """Run quality plus reproducible-build and clean-wheel verification."""
    verify_quality(uv, config, env)

    source_date_epoch = run_capture(["git", "show", "-s", "--format=%ct", "HEAD"], env=env)
    if not source_date_epoch.isdecimal():
        raise VerificationError("HEAD commit timestamp is not a decimal SOURCE_DATE_EPOCH")
    build_env = dict(env)
    build_env["SOURCE_DATE_EPOCH"] = source_date_epoch

    wheel_a_dir = PROJECT_ROOT / "build" / "wheel-a"
    wheel_b_dir = PROJECT_ROOT / "build" / "wheel-b"
    build_arguments = [
        "--wheel",
        "--clear",
        "--build-constraints",
        str(BUILD_CONSTRAINTS_PATH),
        "--require-hashes",
    ]
    run(
        [uv, "build", *build_arguments, "--out-dir", str(wheel_a_dir)],
        env=build_env,
    )
    run(
        [uv, "build", *build_arguments, "--out-dir", str(wheel_b_dir)],
        env=build_env,
    )

    wheel_a = single_wheel(wheel_a_dir)
    wheel_b = single_wheel(wheel_b_dir)
    if wheel_a.read_bytes() != wheel_b.read_bytes():
        raise VerificationError("isolated wheel builds are not byte-identical")
    digest = hashlib.sha256(wheel_a.read_bytes()).hexdigest()
    print(f"{digest}  {wheel_a}", flush=True)
    verify_wheel_metadata(wheel_a)

    with tempfile.TemporaryDirectory(prefix="ea-wheel-verify-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        clean_environment = temporary_root / "venv"
        clean_env = verification_environment(clean_environment)
        run([uv, "sync", "--locked", "--no-install-project"], env=clean_env)
        clean_python, clean_entrypoint = environment_tools(clean_environment)
        run(
            [uv, "pip", "install", "--python", str(clean_python), "--no-deps", str(wheel_a)],
            env=clean_env,
        )
        run([uv, "pip", "check", "--python", str(clean_python)], env=clean_env)
        outside_repository = temporary_root / "outside"
        outside_repository.mkdir()
        run(
            [str(clean_python), "-I", "-c", version_smoke(config.project_version)],
            cwd=outside_repository,
            env=clean_env,
        )
        run([str(clean_entrypoint), "doctor"], cwd=outside_repository, env=clean_env)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the verification profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("quality", "full"),
        default="quality",
        help="quality runs code gates; full also verifies wheels",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected profile and return a stable process exit code."""
    args = parse_args(argv)
    profile = cast(str, args.profile)
    try:
        config = load_project_config()
        uv = find_uv()
        env = verification_environment(environment_dir())
        if profile == "full":
            verify_full(uv, config, env)
        else:
            verify_quality(uv, config, env)
    except VerificationError as error:
        print(f"verification error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(
            f"verification command failed with exit code {error.returncode}",
            file=sys.stderr,
        )
        return error.returncode or 1

    print(f"Verification profile passed: {profile}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
