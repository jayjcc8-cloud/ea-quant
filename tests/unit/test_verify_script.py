from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import bootstrap_local, ci_routes, verify  # noqa: E402


def test_ci_checks_out_and_asserts_the_event_commit() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    expected_sha = "${{ github.event.pull_request.head.sha || github.sha }}"

    assert f"ref: {expected_sha}" in workflow
    assert f"EXPECTED_SHA: {expected_sha}" in workflow
    assert 'run: test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in workflow


def test_ci_routes_and_runs_installed_web_e2e() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "python scripts/ci_routes.py" in workflow
    assert "git diff --no-renames --name-status -z" in workflow
    assert "web_e2e:" in workflow
    jobs = yaml.safe_load(workflow)["jobs"]
    assert jobs["web_e2e"]["needs"] == ["classify", "quality"]
    assert jobs["frontend"]["needs"] == "classify"
    web_e2e_condition = jobs["web_e2e"]["if"]
    assert "!cancelled()" in web_e2e_condition
    assert "github.event_name == 'pull_request'" in web_e2e_condition
    assert "needs.classify.outputs.web_e2e == 'true'" in web_e2e_condition
    assert "needs.quality.result == 'success'" in web_e2e_condition
    assert "needs.quality.result == 'skipped'" in web_e2e_condition
    assert "npx playwright install --with-deps chromium" in workflow
    assert "npm run test:e2e" in workflow
    web_steps = "\n".join(step.get("run", "") for step in jobs["web_e2e"]["steps"])
    assert "Reclaim hosted-runner audit headroom" in workflow
    assert "sudo rm -rf -- /usr/local/lib/android/sdk" in web_steps
    assert "required_bytes = 15 * 1024**3" in web_steps


@pytest.mark.parametrize(
    ("path", "python", "web", "web_e2e"),
    [
        ("src/ea/execution/matcher.py", True, False, True),
        ("src/ea/core/execution_messages.py", True, False, True),
        ("src/ea/web/service.py", True, False, True),
        ("pyproject.toml", True, False, True),
        ("build-constraints.txt", True, False, True),
        ("apps/web/src/app.tsx", False, True, True),
        ("examples/web-scenarios/flat.yaml", True, False, True),
        ("tests/unit/test_execution_messages.py", True, False, False),
        ("docs/STATUS.md", False, False, False),
        ("unrecognized-build-input", True, True, True),
    ],
)
def test_ci_routes_cover_shared_runtime_contracts_and_unknown_paths(
    path: str, python: bool, web: bool, web_e2e: bool
) -> None:
    route = ci_routes.classify([path])
    assert (route.python, route.web, route.web_e2e) == (python, web, web_e2e)


def test_candidate_routes_keep_release_full_and_frontend_gate() -> None:
    assert ci_routes.classify(["docs/STATUS.md"], candidate=True).web is False
    assert ci_routes.classify(["apps/web/src/app.tsx"], candidate=True).web is True
    assert ci_routes.classify(["docs/STATUS.md"], candidate=True, release=True).web is True
    assert ci_routes.classify([], candidate=True).web is True


def test_added_modules_only_skip_browser_when_outside_installed_imports(tmp_path: Path) -> None:
    source = tmp_path / "src"
    documents = {
        "ea/__init__.py": "",
        "ea/cli/__init__.py": "",
        "ea/cli/app.py": "from ea.web import server\n",
        "ea/web/__init__.py": "",
        "ea/web/server.py": "from .service import create_app\n",
        "ea/web/service.py": "from ea.execution import authority\n",
        "ea/execution/__init__.py": "from .authority import authorize\n",
        "ea/execution/authority.py": "from ea.core import messages\n",
        "ea/execution/order_lifecycle.py": "from ea.core import messages\n",
        "ea/core/__init__.py": "",
        "ea/core/messages.py": "",
    }
    for name, contents in documents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    reachable = ci_routes.installed_python_modules(source)
    assert reachable is not None
    assert "ea.web.service" in reachable
    assert "ea.execution" in reachable
    assert "ea.execution.authority" in reachable
    assert "ea.core.messages" in reachable
    assert "ea.execution.order_lifecycle" not in reachable

    standalone = "src/ea/execution/order_lifecycle.py"
    added = ci_routes.classify(
        [standalone, "tests/unit/test_order_lifecycle.py"],
        added_paths=frozenset({standalone, "tests/unit/test_order_lifecycle.py"}),
        source_root=source,
    )
    assert added.python is True
    assert added.web_e2e is False
    assert ci_routes.classify([standalone], source_root=source).web_e2e is True
    assert (
        ci_routes.classify(
            [standalone], added_paths=frozenset({standalone}), source_root=source
        ).web_e2e
        is False
    )
    assert (
        ci_routes.classify(
            ["src/ea/execution/authority.py"],
            added_paths=frozenset({"src/ea/execution/authority.py"}),
            source_root=source,
        ).web_e2e
        is True
    )

    (source / "ea/web/server.py").write_text("import importlib\nimportlib.import_module(name)\n")
    assert ci_routes.installed_python_modules(source) is None
    assert (
        ci_routes.classify(
            [standalone], added_paths=frozenset({standalone}), source_root=source
        ).web_e2e
        is True
    )


def test_real_installed_graph_skips_independent_added_order_module(tmp_path: Path) -> None:
    source = tmp_path / "src"
    shutil.copytree(PROJECT_ROOT / "src" / "ea", source / "ea")
    module = source / "ea" / "execution" / "order_lifecycle_probe.py"
    module.write_text("from ea.core import OrderId\n", encoding="utf-8")
    path = "src/ea/execution/order_lifecycle_probe.py"

    reachable = ci_routes.installed_python_modules(source)
    assert reachable is not None
    assert "ea.execution.authority" in reachable
    assert "ea.core.execution_messages" in reachable
    assert "ea.execution.order_lifecycle_probe" not in reachable
    routes = ci_routes.classify(
        [path, "tests/unit/test_order_lifecycle_probe.py", "docs/STATUS.md"],
        added_paths=frozenset({path, "tests/unit/test_order_lifecycle_probe.py"}),
        source_root=source,
    )
    assert routes.python is True
    assert routes.governance is True
    assert routes.web_e2e is False


def test_route_parser_uses_git_status_not_path_name() -> None:
    statuses = ci_routes.parse_change_statuses(
        b"A\0src/ea/execution/order_lifecycle.py\0M\0src/ea/web/service.py\0"
    )
    assert statuses == {
        "src/ea/execution/order_lifecycle.py": "A",
        "src/ea/web/service.py": "M",
    }
    assert ci_routes.parse_change_statuses(b"T\0src/ea/execution/order_lifecycle.py\0") == {
        "src/ea/execution/order_lifecycle.py": "T"
    }


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
    assert ("uv", "sync", "--locked", "--extra", "dev", "--extra", "web") in commands
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
