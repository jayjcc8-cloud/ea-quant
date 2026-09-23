from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import production_runtime as runtime  # noqa: E402


def make_profile(tmp_path: Path) -> Path:
    bundle = tmp_path / "releases" / ("a" * 40)
    bundle.mkdir(parents=True)
    (bundle / "ui").mkdir()
    (bundle / "ui/index.html").write_text("real UI placeholder")
    with zipfile.ZipFile(bundle / "ea_quant-0.2.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("ea/__init__.py", '__version__ = "0.2.0"\n')
    files = {
        p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in bundle.rglob("*")
        if p.is_file()
    }
    manifest = bundle / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "ea.distribution-bundle.v1",
                "commit": "a" * 40,
                "version": "0.2.0",
                "files": files,
            }
        )
    )
    (tmp_path / "scenarios").mkdir()
    config = tmp_path / "runtime.toml"
    config.write_text(f'''schema = "ea.production-runtime.v1"
bundle_root = "{bundle}"
commit = "{"a" * 40}"
manifest_sha256 = "{hashlib.sha256(manifest.read_bytes()).hexdigest()}"
scenario_root = "{tmp_path / "scenarios"}"
workspace = "{tmp_path / "workspace"}"
port = 18765
''')
    return config


def test_command_reuses_existing_isolated_cli_and_external_workspace(tmp_path: Path) -> None:
    profile = runtime.load_profile(make_profile(tmp_path))
    identity = runtime.verify_bundle(profile)
    command = runtime.command(profile)
    assert command[:5] == [
        str(profile.bundle_root / ".venv/bin/python"),
        "-I",
        "-m",
        "ea.cli.app",
        "web",
    ]
    assert command[5] == "serve"
    assert command[command.index("--workspace") + 1] == str(tmp_path / "workspace")
    assert "--host" not in command
    assert identity["commit"] == "a" * 40
    assert not profile.workspace.exists()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("port = 18765", "port = true"),
        ("port = 18765", "port = 80"),
        ("port = 18765", "port = 65536"),
        ("port = 18765", "port = 18765\nlive = true"),
        ("ea.production-runtime.v1", "unknown"),
    ],
)
def test_strict_configuration_rejects_unknown_and_unsafe_fields(
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    config = make_profile(tmp_path)
    config.write_text(config.read_text().replace(before, after))
    with pytest.raises(ValueError):
        runtime.load_profile(config)


def test_rejects_workspace_in_replaceable_release(tmp_path: Path) -> None:
    config = make_profile(tmp_path)
    profile = runtime.load_profile(config)
    config.write_text(
        config.read_text().replace(
            str(tmp_path / "workspace"), str(profile.bundle_root / "workspace")
        )
    )
    with pytest.raises(ValueError, match="overlap"):
        runtime.load_profile(config)


def test_rejects_wrong_commit_corrupt_manifest_payload_and_symlink(tmp_path: Path) -> None:
    config = make_profile(tmp_path)
    profile = runtime.load_profile(config)
    asset = profile.bundle_root / "ui/index.html"
    asset.write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        runtime.verify_bundle(profile)
    asset.unlink()
    outside = tmp_path / "outside"
    outside.write_text("real UI placeholder")
    asset.symlink_to(outside)
    with pytest.raises(ValueError, match="regular"):
        runtime.verify_bundle(profile)
    manifest = profile.bundle_root / "manifest.json"
    manifest.write_text(manifest.read_text().replace("a" * 40, "b" * 40))
    with pytest.raises(ValueError, match="manifest"):
        runtime.verify_bundle(profile)


def test_rejects_source_interpreter_before_serving(tmp_path: Path) -> None:
    profile = runtime.load_profile(make_profile(tmp_path))
    with pytest.raises(ValueError, match="interpreter"):
        runtime.verify_installation(profile)


def test_relative_roots_and_overlapping_inputs_rejected(tmp_path: Path) -> None:
    config = make_profile(tmp_path)
    config.write_text(config.read_text().replace(str(tmp_path / "workspace"), "workspace"))
    with pytest.raises(ValueError, match="absolute"):
        runtime.load_profile(config)
