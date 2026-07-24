from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import bootstrap_local, verify  # noqa: E402


def test_ci_checks_out_and_asserts_the_event_commit() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    expected_sha = "${{ github.event.pull_request.head.sha || github.sha }}"

    assert f"ref: {expected_sha}" in workflow
    assert f"EXPECTED_SHA: {expected_sha}" in workflow
    assert 'run: test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in workflow


def test_project_versions_come_from_pyproject() -> None:
    config = verify.load_project_config()

    assert config.project_version == bootstrap_local.project_version()
    assert config.required_uv_version == "0.11.28"
    assert config.project_version in verify.version_smoke(config.project_version)
    assert config.project_version in bootstrap_local.version_smoke(config.project_version)
    assert config.line_coverage_floor == 86
    assert config.branch_coverage_floor == 71


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


def test_environment_tools_require_ea_only_after_python(tmp_path: Path) -> None:
    scripts = tmp_path / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir()
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.touch()

    assert verify.environment_python(tmp_path) == python
    with pytest.raises(verify.VerificationError, match="ea entrypoint is missing"):
        verify.environment_tools(tmp_path)

    entrypoint = scripts / ("ea.exe" if os.name == "nt" else "ea")
    entrypoint.touch()
    assert verify.environment_tools(tmp_path) == (python, entrypoint)


def test_quality_profile_runs_reproducible_gate_before_static_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "python"
    entrypoint = tmp_path / "ea"
    python.touch()
    entrypoint.touch()
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(verify, "verify_uv", lambda *_args: None)
    monkeypatch.setattr(verify, "environment_dir", lambda: tmp_path)
    monkeypatch.setattr(verify, "environment_tools", lambda _environment: (python, entrypoint))
    monkeypatch.setattr(
        verify,
        "run",
        lambda command, **_kwargs: commands.append(tuple(str(part) for part in command)),
    )

    verify.verify_quality(
        "uv",
        verify.ProjectConfig(
            project_version="0.1.1",
            required_uv_version="0.11.28",
            line_coverage_floor=86,
            branch_coverage_floor=71,
        ),
        {},
    )

    gate = (str(python), "-I", "-B", str(verify.REPRODUCIBLE_RUN_PATH))
    lint = ("uv", "run", "--locked", "ruff", "check", ".")
    pytest_commands = [command for command in commands if "pytest" in command]
    assert gate in commands
    assert commands.index(gate) < commands.index(lint)
    assert pytest_commands == [("uv", "run", "--locked", "pytest", "-q")]


def test_full_quality_path_runs_one_coverage_pytest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "python"
    entrypoint = tmp_path / "ea"
    python.touch()
    entrypoint.touch()
    commands: list[tuple[str, ...]] = []
    reports: list[Path] = []

    monkeypatch.setattr(verify, "verify_uv", lambda *_args: None)
    monkeypatch.setattr(verify, "environment_dir", lambda: tmp_path)
    monkeypatch.setattr(verify, "environment_tools", lambda _environment: (python, entrypoint))
    monkeypatch.setattr(
        verify,
        "run",
        lambda command, **_kwargs: commands.append(tuple(str(part) for part in command)),
    )
    monkeypatch.setattr(
        verify,
        "verify_coverage_report",
        lambda path, _config: reports.append(path),
    )

    verify.verify_quality(
        "uv",
        verify.ProjectConfig(
            project_version="0.1.1",
            required_uv_version="0.11.28",
            line_coverage_floor=86,
            branch_coverage_floor=71,
        ),
        {},
        measure_coverage=True,
    )

    pytest_commands = [command for command in commands if "pytest" in command]
    assert len(pytest_commands) == 1
    assert "--cov=ea" in pytest_commands[0]
    assert "--cov-branch" in pytest_commands[0]
    assert reports and reports[0].name == "coverage.json"


def test_coverage_report_uses_separate_raw_line_and_branch_totals(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {
                    "covered_lines": 86,
                    "num_statements": 100,
                    "missing_lines": 14,
                    "covered_branches": 71,
                    "num_branches": 100,
                    "missing_branches": 29,
                    "percent_covered": 99.99,
                }
            }
        ),
        encoding="utf-8",
    )
    config = verify.ProjectConfig(
        project_version="0.1.1",
        required_uv_version="0.11.28",
        line_coverage_floor=86,
        branch_coverage_floor=71,
    )

    verify.verify_coverage_report(report, config)

    failing = json.loads(report.read_text(encoding="utf-8"))
    failing["totals"]["covered_branches"] = 70
    failing["totals"]["missing_branches"] = 30
    report.write_text(json.dumps(failing), encoding="utf-8")
    with pytest.raises(verify.VerificationError, match="branch coverage"):
        verify.verify_coverage_report(report, config)


@pytest.mark.parametrize(
    "totals",
    [
        {},
        {
            "covered_lines": True,
            "num_statements": 1,
            "missing_lines": 0,
            "covered_branches": 1,
            "num_branches": 1,
            "missing_branches": 0,
        },
        {
            "covered_lines": 0,
            "num_statements": 0,
            "missing_lines": 0,
            "covered_branches": 0,
            "num_branches": 0,
            "missing_branches": 0,
        },
        {
            "covered_lines": 1,
            "num_statements": 2,
            "missing_lines": 0,
            "covered_branches": 1,
            "num_branches": 2,
            "missing_branches": 0,
        },
    ],
)
def test_coverage_report_rejects_incomplete_or_inconsistent_totals(
    tmp_path: Path,
    totals: dict[str, object],
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": totals}), encoding="utf-8")

    with pytest.raises(verify.VerificationError):
        verify.load_coverage_totals(report)


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
