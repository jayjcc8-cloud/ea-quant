#!/usr/bin/env python3
"""Validate a pinned existing bundle and exec its one installed offline Web process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import sysconfig
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Profile:
    bundle_root: Path
    commit: str
    manifest_sha256: str
    scenario_root: Path
    workspace: Path
    port: int
    data_root: Path | None = None
    strategy_root: Path | None = None


def absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("runtime paths must be absolute")
    return Path(value).resolve()


def load_profile(path: Path) -> Profile:
    document = tomllib.loads(path.read_text())
    required = {
        "schema",
        "bundle_root",
        "commit",
        "manifest_sha256",
        "scenario_root",
        "workspace",
        "port",
    }
    if not required <= document.keys() or document.keys() - required - {
        "data_root",
        "strategy_root",
    }:
        raise ValueError("missing or unknown runtime configuration field")
    if document["schema"] != "ea.production-runtime.v1":
        raise ValueError("unsupported runtime configuration schema")
    for key, size in [("commit", 40), ("manifest_sha256", 64)]:
        if not isinstance(document[key], str) or not re.fullmatch(
            "[0-9a-f]{" + str(size) + "}", document[key]
        ):
            raise ValueError("invalid pinned artifact identity")
    if type(document["port"]) is not int or not 1024 <= document["port"] <= 65535:
        raise ValueError("invalid loopback port")
    paths = {
        key: absolute_path(document[key])
        for key in ("bundle_root", "scenario_root", "workspace", "data_root", "strategy_root")
        if key in document
    }
    roots = list(paths.values())
    if any(
        a.is_relative_to(b) or b.is_relative_to(a)
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise ValueError("release, workspace and input roots must not overlap")
    for key, root in paths.items():
        if key != "workspace" and not root.is_dir():
            raise ValueError("configured input or bundle directory is missing")
    return Profile(
        bundle_root=paths["bundle_root"],
        commit=document["commit"],
        manifest_sha256=document["manifest_sha256"],
        scenario_root=paths["scenario_root"],
        workspace=paths["workspace"],
        port=document["port"],
        data_root=paths.get("data_root"),
        strategy_root=paths.get("strategy_root"),
    )


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def regular_member(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("invalid bundle member")
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
        raise ValueError("bundle member must be regular, not a symlink")
    if not path.is_file():
        raise ValueError("bundle member must be a regular file")
    return path


def verify_bundle(profile: Profile) -> dict[str, Any]:
    manifest_bytes = regular_member(profile.bundle_root, "manifest.json").read_bytes()
    if digest(manifest_bytes) != profile.manifest_sha256:
        raise ValueError("pinned manifest hash mismatch")
    manifest = json.loads(manifest_bytes)
    if (
        manifest.get("schema") != "ea.distribution-bundle.v1"
        or manifest.get("commit") != profile.commit
    ):
        raise ValueError("pinned manifest commit/schema mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or "ui/index.html" not in files:
        raise ValueError("invalid bundle file manifest")
    for name, expected in files.items():
        if digest(regular_member(profile.bundle_root, name).read_bytes()) != expected:
            raise ValueError("bundle payload hash mismatch")
    wheels = [name for name in files if name.endswith(".whl") and "/" not in name]
    if len(wheels) != 1:
        raise ValueError("bundle must contain exactly one wheel")
    return {
        "commit": profile.commit,
        "version": manifest["version"],
        "manifest_sha256": profile.manifest_sha256,
        "wheel": wheels[0],
        "wheel_sha256": files[wheels[0]],
        "bundle_root": str(profile.bundle_root),
        "workspace": str(profile.workspace),
        "host": "127.0.0.1",
        "port": profile.port,
        "live_available": False,
    }


def verify_installation(profile: Profile) -> dict[str, Any]:
    identity = verify_bundle(profile)
    environment = profile.bundle_root / ".venv"
    if Path(sys.prefix).resolve() != environment or sys.prefix == sys.base_prefix:
        raise ValueError("runtime interpreter must be the pinned bundle .venv")
    site = Path(sysconfig.get_path("purelib")).resolve()
    if not site.is_relative_to(environment):
        raise ValueError("installed package must belong to the pinned environment")
    with zipfile.ZipFile(profile.bundle_root / identity["wheel"]) as wheel:
        members = {
            name for name in wheel.namelist() if name.startswith("ea/") and not name.endswith("/")
        }
        if not members:
            raise ValueError("wheel contains no EA package")
        for name in members:
            if regular_member(site, name).read_bytes() != wheel.read(name):
                raise ValueError("installed EA differs from pinned wheel")
        installed = {
            p.relative_to(site).as_posix()
            for p in (site / "ea").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        }
        if installed != members:
            raise ValueError("installed EA contains unpinned files")
    identity["python_prefix"] = sys.prefix
    return identity


def command(profile: Profile) -> list[str]:
    result = [
        str(profile.bundle_root / ".venv/bin/python"),
        "-I",
        "-m",
        "ea.cli.app",
        "web",
        "serve",
        "--scenario-root",
        str(profile.scenario_root),
        "--workspace",
        str(profile.workspace),
        "--ui-dir",
        str(profile.bundle_root / "ui"),
        "--port",
        str(profile.port),
    ]
    for name in ("data_root", "strategy_root"):
        value = getattr(profile, name)
        if value is not None:
            result.extend(["--" + name.replace("_", "-"), str(value)])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "serve"])
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        profile = load_profile(arguments.config)
        identity = verify_installation(profile)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"production runtime refused: {error}", file=sys.stderr)
        raise SystemExit(78) from None
    print(json.dumps(identity, sort_keys=True), flush=True)
    if arguments.action == "serve":
        argv = command(profile)
        os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
