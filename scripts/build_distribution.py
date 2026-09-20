#!/usr/bin/env python3
"""Build an unpublished, commit-bound engine/Web bundle from a clean checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def capture(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True).strip()


def build(output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError("output must be separate from the checkout")
    if capture("git", "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("distribution requires a clean committed checkout")
    commit = capture("git", "rev-parse", "HEAD")
    epoch = int(capture("git", "show", "-s", "--format=%ct", "HEAD"))
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    destination = output / f"ea-quant-{version}-{commit[:12]}.zip"
    if destination.exists():
        raise ValueError("output bundle already exists")
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
    subprocess.run(["npm", "ci", "--prefix", "apps/web"], cwd=ROOT, env=env, check=True)
    subprocess.run(["npm", "run", "build", "--prefix", "apps/web"], cwd=ROOT, env=env, check=True)
    with tempfile.TemporaryDirectory(prefix="ea-distribution-") as temporary:
        stage = Path(temporary)
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--clear",
                "--build-constraints",
                str(ROOT / "build-constraints.txt"),
                "--require-hashes",
                "--out-dir",
                str(stage),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
        wheels = list(stage.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("expected exactly one wheel")
        wheel = wheels[0]
        requirements = subprocess.check_output(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--extra",
                "web",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
            ],
            cwd=ROOT,
            env=env,
        )
        members = {wheel.name: wheel.read_bytes(), "requirements.txt": requirements}
        for path in sorted((ROOT / "apps/web/dist").rglob("*")):
            if path.is_symlink():
                raise ValueError("Web assets must not be symlinks")
            if path.is_file():
                members["ui/" + path.relative_to(ROOT / "apps/web/dist").as_posix()] = (
                    path.read_bytes()
                )
        if "ui/index.html" not in members:
            raise ValueError("production Web index is missing")
        for name in capture("git", "ls-files", "examples/web-scenarios").splitlines():
            path = ROOT / name
            if path.is_symlink():
                raise ValueError("example inputs must not be symlinks")
            members["scenarios/" + path.name] = path.read_bytes()
        members["INSTALL.md"] = f"""# EA local research bundle

Unpublished build of commit `{commit}`, Python distribution version `{version}`.
This bundle contains a noneditable wheel, matching production Web UI and example scenarios.
Verify SHA256SUMS before installation. The manifest binds every payload file to this commit.

Prerequisites: Python 3.12 with venv/pip on macOS or Linux. Dependency installation may use the
configured package index; runtime research is local and offline. Node.js and the source checkout
are not needed. Run these commands from this extracted directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --no-deps {wheel.name}
.venv/bin/ea --version
.venv/bin/ea doctor
.venv/bin/ea web serve --scenario-root "$PWD/scenarios" \\
  --workspace "$PWD/workspace" --ui-dir "$PWD/ui"
```

Open http://127.0.0.1:8765/backtests. Select bounded-long, validate, run and download its report.
Stop the service with Ctrl+C. Keep the workspace to reopen historical results. Optional explicit
--data-root and --strategy-root directories must be separate from the scenario/UI/workspace roots.

This artifact is an unpublished verification candidate. It creates no Git tag, release, registry
publication, deployment or live trading permission.
""".encode()
        hashes = {
            name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(members.items())
        }
        members["manifest.json"] = (
            json.dumps(
                {
                    "schema": "ea.distribution-bundle.v1",
                    "commit": commit,
                    "version": version,
                    "source_date_epoch": epoch,
                    "published": False,
                    "files": hashes,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()
        members["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
            for name, payload in sorted(members.items())
        ).encode()
        if capture("git", "rev-parse", "HEAD") != commit or capture(
            "git", "status", "--porcelain", "--untracked-files=all"
        ):
            raise ValueError("checkout changed during build")
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(max(epoch, 315532800), UTC)
        with (
            destination.open("xb") as handle,
            zipfile.ZipFile(
                handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive,
        ):
            for name, payload in sorted(members.items()):
                info = zipfile.ZipInfo(name, stamp.timetuple()[:6])
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, payload, compresslevel=9)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    path = build(args.output_dir)
    print(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}")


if __name__ == "__main__":
    main()
