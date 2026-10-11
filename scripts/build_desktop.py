"""Build a current-source, self-contained arm64 Mac application (no publication).

Run with the project's Python. All build/install tools are build-time only.
The explicit allowlist below is the entire runtime payload, never a checkout copy.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".local" / "desktop"
RUNTIME = OUT / "runtime"


def run(*args: str | Path, cwd: Path = ROOT) -> None:
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True, env=os.environ)


def main() -> None:
    global OUT, RUNTIME
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    OUT = args.output.resolve()
    RUNTIME = OUT / "runtime"
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This slice supports building on macOS arm64 only.")
    OUT.mkdir(parents=True, exist_ok=True)
    # Reuse only the private build stage; sources are rebuilt on every invocation.
    RUNTIME.mkdir(exist_ok=True)
    os.environ["UV_CACHE_DIR"] = str(OUT / "uv-cache")
    os.environ["NPM_CONFIG_CACHE"] = str(OUT / "npm-cache")
    os.environ["CARGO_HOME"] = str(ROOT / ".local/rust/cargo")
    os.environ["RUSTUP_HOME"] = str(ROOT / ".local/rust/rustup")
    # macOS 27 dyld rejects LINKEDIT produced by stripping some proc-macro dylibs.
    os.environ["CARGO_PROFILE_RELEASE_STRIP"] = "none"
    os.environ["CARGO_TARGET_DIR"] = str(OUT / "target")
    os.environ["PATH"] = str(ROOT / ".local/rust/cargo/bin") + os.pathsep + os.environ["PATH"]
    run("uv", "python", "install", "3.12.13", "--no-bin", "--install-dir", OUT / "python-downloads")
    python_dist = OUT / "python-downloads/cpython-3.12.13-macos-aarch64-none"
    if not (RUNTIME / "python").exists():
        shutil.copytree(python_dist, RUNTIME / "python", symlinks=True)
    python = RUNTIME / "python/bin/python3.12"
    requirements = OUT / "requirements.txt"
    run(
        "uv",
        "export",
        "--locked",
        "--extra",
        "web",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "-o",
        requirements,
    )
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python,
        "--break-system-packages",
        "--require-hashes",
        "--no-deps",
        "-r",
        requirements,
    )
    run("uv", "build", "--wheel", "--out-dir", OUT / "wheels")
    wheel = next((OUT / "wheels").glob("ea_quant-*.whl"))
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python,
        "--break-system-packages",
        "--reinstall-package",
        "ea-quant",
        "--no-deps",
        wheel,
    )
    # Build-only console scripts carry absolute shebangs. Keep them outside the
    # payload; the app and core use python -I -B -m and need no installed CLI.
    omitted = OUT / "omitted-build-tools"
    omitted.mkdir(exist_ok=True)
    for tool in (RUNTIME / "python/bin").iterdir():
        if tool.name not in {"python", "python3", "python3.12"}:
            tool.replace(omitted / tool.name)
    for metadata in (RUNTIME / "python/lib/python3.12/site-packages").glob(
        "*.dist-info/direct_url.json"
    ):
        metadata.replace(omitted / (metadata.parent.name + "-direct_url.json"))
    # uv patches libpython's install name to its install location; restore relocatability.
    # Executable/extension load commands already use @rpath or @loader_path.
    dylib = RUNTIME / "python/lib/libpython3.12.dylib"
    run("install_name_tool", "-id", "@rpath/libpython3.12.dylib", dylib)
    run("npm", "ci", cwd=ROOT / "apps/web")
    run("npm", "run", "build", cwd=ROOT / "apps/web")
    shutil.copytree(ROOT / "apps/web/dist", RUNTIME / "ui", dirs_exist_ok=True)
    (RUNTIME / "samples").mkdir(exist_ok=True)
    shutil.copyfile(
        ROOT / "examples/web-scenarios/bounded-long.yaml",
        RUNTIME / "samples/synthetic-bounded-long.yaml",
    )
    shutil.copyfile(ROOT / "examples/web-scenarios/prices.csv", RUNTIME / "samples/prices.csv")
    run(
        "cargo",
        "build",
        "--locked",
        "--release",
        "--manifest-path",
        ROOT / "crates/quine-core/Cargo.toml",
    )
    shutil.copyfile(OUT / "target/release/quine-core", RUNTIME / "quine-core")
    (RUNTIME / "quine-core").chmod(0o755)
    # Ad-hoc local signatures only. No account, certificate, notarization or OS changes.
    for path in RUNTIME.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                magic = handle.read(4)
            if magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                run("codesign", "--force", "--sign", "-", path)
    config = OUT / "bundle-config.json"
    config.write_text(json.dumps({"bundle": {"resources": {str(RUNTIME) + "/": "runtime/"}}}))
    run("npm", "ci", cwd=ROOT / "apps/desktop")
    run("npm", "run", "build", "--", "--config", config, cwd=ROOT / "apps/desktop")
    app = OUT / "target/release/bundle/macos/QUINE.app"
    run("codesign", "--verify", "--deep", "--strict", app)
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, OUT / "QUINE-macos-arm64.zip")
    print(f"Local ad-hoc signed, NOT notarized: {app}")


if __name__ == "__main__":
    main()
