#!/usr/bin/env python3
"""Create and verify the supported local VSCode environment."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_DIR = PROJECT_ROOT / "venv"
LEGACY_ENVIRONMENT_DIR = PROJECT_ROOT / ".venv"
VERSION_SMOKE = (
    "import importlib.metadata as metadata; import ea; "
    "assert metadata.version('ea-quant') == ea.__version__ == '0.1.1'; "
    "print(ea.__version__)"
)


def find_uv() -> str:
    """Return the global uv executable or exit with installation guidance."""
    uv = shutil.which("uv")
    if uv is not None:
        uv_path = Path(uv).resolve()
        legacy_path = LEGACY_ENVIRONMENT_DIR.resolve()
        if uv_path == legacy_path or legacy_path in uv_path.parents:
            print(
                "uv resolved inside legacy .venv; deactivate it and install global uv "
                "before retrying.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return str(uv_path)

    print(
        "uv was not found on PATH. Install the exact version declared by "
        "[tool.uv].required-version in pyproject.toml using "
        "https://docs.astral.sh/uv/getting-started/installation/#standalone-installer "
        "and rerun this script.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run one visible bootstrap command and fail on any non-zero exit."""
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, check=True, cwd=cwd, env=env)


def main() -> int:
    """Synchronize venv and verify imports and entrypoints without source-path bypasses."""
    uv = find_uv()
    if ENVIRONMENT_DIR.is_symlink():
        print(
            "error: venv must be a real directory, not a symlink; replace it before retrying.",
            file=sys.stderr,
        )
        return 2

    if LEGACY_ENVIRONMENT_DIR.exists():
        print(
            "warning: legacy .venv is unsupported and was left untouched; VSCode uses venv/.",
            file=sys.stderr,
        )

    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(ENVIRONMENT_DIR)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)

    run([uv, "--version"], cwd=PROJECT_ROOT, env=env)
    run([uv, "sync", "--locked", "--extra", "dev"], cwd=PROJECT_ROOT, env=env)

    scripts_dir = ENVIRONMENT_DIR / ("Scripts" if os.name == "nt" else "bin")
    python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
    entrypoint = scripts_dir / ("ea.exe" if os.name == "nt" else "ea")

    with tempfile.TemporaryDirectory(prefix="ea-bootstrap-") as temporary_directory:
        outside_repository = Path(temporary_directory)
        run(
            [str(python), "-I", "-c", VERSION_SMOKE],
            cwd=outside_repository,
            env=env,
        )
        run([str(entrypoint), "doctor"], cwd=outside_repository, env=env)

    run([uv, "run", "--locked", "ea", "doctor"], cwd=PROJECT_ROOT, env=env)
    print(f"Local environment ready: {ENVIRONMENT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
