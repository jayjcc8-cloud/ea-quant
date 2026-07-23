from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import bootstrap_local, verify  # noqa: E402


def test_project_versions_come_from_pyproject() -> None:
    config = verify.load_project_config()

    assert config.project_version == bootstrap_local.project_version()
    assert config.required_uv_version == "0.11.28"
    assert config.project_version in verify.version_smoke(config.project_version)
    assert config.project_version in bootstrap_local.version_smoke(config.project_version)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("uv 0.11.28", "0.11.28"),
        ("uv 0.11.28 (Homebrew 2026-01-01)", "0.11.28"),
    ],
)
def test_parse_uv_version(output: str, expected: str) -> None:
    assert verify.parse_uv_version(output) == expected


@pytest.mark.parametrize("output", ["", "0.11.28", "uv latest", "uv 0.11"])
def test_parse_uv_version_rejects_ambiguous_output(output: str) -> None:
    with pytest.raises(verify.VerificationError):
        verify.parse_uv_version(output)


def test_verification_environment_uses_canonical_environment_path(tmp_path: Path) -> None:
    environment = verify.verification_environment(tmp_path / "venv")

    assert Path(environment["UV_PROJECT_ENVIRONMENT"]) == (tmp_path / "venv").resolve()
    assert "PYTHONPATH" not in environment
    assert "VIRTUAL_ENV" not in environment


def test_single_wheel_requires_exactly_one_file(tmp_path: Path) -> None:
    with pytest.raises(verify.VerificationError, match="expected one wheel"):
        verify.single_wheel(tmp_path)

    version = verify.load_project_config().project_version
    wheel = tmp_path / f"ea_quant-{version}-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    assert verify.single_wheel(tmp_path) == wheel

    (tmp_path / f"other-{version}-py3-none-any.whl").write_bytes(b"other")
    with pytest.raises(verify.VerificationError, match="expected one wheel"):
        verify.single_wheel(tmp_path)


def test_verify_wheel_metadata_requires_one_metadata_file(tmp_path: Path) -> None:
    version = verify.load_project_config().project_version
    wheel = tmp_path / f"ea_quant-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"ea_quant-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\n")

    verify.verify_wheel_metadata(wheel)

    invalid_wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(invalid_wheel, "w") as archive:
        archive.writestr("README.txt", "missing metadata")
    with pytest.raises(verify.VerificationError, match=r"expected one \.dist-info/WHEEL"):
        verify.verify_wheel_metadata(invalid_wheel)
