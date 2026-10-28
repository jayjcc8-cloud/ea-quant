from __future__ import annotations

import io
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


def _job_commands(job: dict[str, object]) -> str:
    steps = job["steps"]
    assert isinstance(steps, list)
    return " ".join(
        " ".join(str(step.get("run", "")).split()) for step in steps if isinstance(step, dict)
    )


def test_ci_checks_the_rust_core_with_its_own_path_trigger() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = yaml.safe_load(workflow)["jobs"]["rust_core"]

    assert job["needs"] == "classify"
    assert job["if"].strip() == "needs.classify.outputs.rust_core == 'true'"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10
    assert "${{ github.workspace }}/.local/rust-target" in job["env"]["CARGO_TARGET_DIR"]
    assert "continue-on-error" not in job

    checkout = job["steps"][0]
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"

    commands = _job_commands(job)
    assert "rustup toolchain install stable --profile minimal" in commands
    assert "rustup default stable" in commands
    assert (
        "cargo test --locked --manifest-path crates/quine-core/Cargo.toml --lib "
        "--test store --test cli --test canonical_identity"
    ) in commands
    assert "--test stage2" not in commands
    assert "cargo build" not in commands
    assert "tauri" not in commands.lower()


def test_ci_checks_the_macos_tauri_shell_with_its_own_path_trigger() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    job = yaml.safe_load(workflow)["jobs"]["macos_shell"]

    assert job["needs"] == "classify"
    assert job["if"].strip() == "needs.classify.outputs.macos_shell == 'true'"
    assert job["runs-on"] == "macos-15"
    assert job["timeout-minutes"] == 20
    assert "${{ github.workspace }}/.local/desktop/target" in job["env"]["CARGO_TARGET_DIR"]
    assert "continue-on-error" not in job

    checkout = job["steps"][0]
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"

    commands = _job_commands(job)
    assert 'test "$(uname -m)" = "arm64"' in commands
    assert "rustup toolchain install stable --profile minimal" in commands
    assert "rustup default stable" in commands
    assert "cargo check --locked --manifest-path apps/desktop/src-tauri/Cargo.toml" in commands
    for forbidden in ("tauri build", "npm run build", "codesign", "notarize", "cargo build"):
        assert forbidden not in commands


def test_ci_native_jobs_keep_pr_and_main_event_semantics() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    outputs = jobs["classify"]["outputs"]
    for name in ("rust_core", "macos_shell"):
        assert outputs[name] == "${{ steps.routes.outputs." + name + " }}"
        condition = jobs[name]["if"]
        assert "${{" not in condition
        assert "github.event_name" not in condition
        assert "continue-on-error" not in jobs[name]


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


@pytest.mark.parametrize(
    ("path", "rust_core", "macos_shell"),
    [
        ("crates/quine-core/src/core.rs", True, False),
        ("crates/quine-core/Cargo.toml", True, False),
        ("crates/quine-core/Cargo.lock", True, False),
        ("crates/quine-core/tests/store.rs", True, False),
        ("crates/quine-core/README.md", False, False),
        ("apps/desktop/src-tauri/src/main.rs", False, True),
        ("apps/desktop/src-tauri/Cargo.toml", False, True),
        ("apps/desktop/src-tauri/Cargo.lock", False, True),
        ("apps/desktop/src-tauri/build.rs", False, True),
        ("apps/desktop/src-tauri/tauri.conf.json", False, True),
        ("apps/desktop/src-tauri/icons/icon.icns", False, True),
        ("apps/desktop/loading/index.html", False, True),
        ("apps/desktop/package.json", False, True),
        ("apps/desktop/README.md", False, False),
        ("Cargo.toml", True, True),
        ("Cargo.lock", True, True),
        ("rust-toolchain", True, True),
        ("rust-toolchain.toml", True, True),
        (".cargo/config.toml", True, True),
        ("scripts/build_desktop.py", True, True),
        (".github/workflows/ci.yml", True, True),
        ("scripts/ci_routes.py", True, True),
        (".github/workflows/candidate-full.yml", False, False),
        (".github/workflows/production-runtime.yml", False, False),
        ("docs/STATUS.md", False, False),
        ("README.md", False, False),
        ("NOTES.md", False, False),
        ("NOTES.MD", False, False),
        ("design/README.md", False, False),
        (".cargo/README.md", False, False),
        ("src/ea/web/service.py", False, False),
        ("apps/web/src/app.tsx", False, False),
        ("tests/unit/test_execution_messages.py", False, False),
        ("unrecognized-build-input", True, True),
    ],
)
def test_ci_routes_target_the_two_rust_builds(
    path: str, rust_core: bool, macos_shell: bool
) -> None:
    routes = ci_routes.classify([path])
    assert (routes.rust_core, routes.macos_shell) == (rust_core, macos_shell)


def test_ci_rust_routes_union_without_relaxing_existing_routes() -> None:
    routes = ci_routes.classify(
        [
            "crates/quine-core/src/store.rs",
            "apps/desktop/src-tauri/src/main.rs",
            "docs/STATUS.md",
        ]
    )
    assert (routes.rust_core, routes.macos_shell) == (True, True)
    assert routes.docs_only is False
    assert routes.governance is True
    assert routes.python is True
    assert routes.web is True
    assert routes.web_e2e is True
    assert routes.ci is False

    markdown_only = ci_routes.classify(
        ["crates/quine-core/README.md", "apps/desktop/README.md", "docs/STATUS.md"]
    )
    assert (markdown_only.rust_core, markdown_only.macos_shell) == (False, False)


@pytest.mark.parametrize("path", ["NOTES.md", "NOTES.MD", "design/README.md", ".cargo/README.md"])
def test_ci_known_markdown_keeps_old_routes_without_native_builds(path: str) -> None:
    routes = ci_routes.classify([path])
    assert (routes.rust_core, routes.macos_shell) == (False, False)
    assert routes.docs_only is False
    assert routes.governance is True
    assert routes.python is True
    assert routes.web is True
    assert routes.web_e2e is True
    assert routes.ci is False


def test_ci_markdown_does_not_mask_a_native_source_change() -> None:
    core = ci_routes.classify(["crates/quine-core/src/lib.rs", "design/README.md"])
    assert (core.rust_core, core.macos_shell) == (True, False)
    cargo = ci_routes.classify([".cargo/config.toml", ".cargo/README.md"])
    assert (cargo.rust_core, cargo.macos_shell) == (True, True)


def test_ci_rust_routes_are_conservative_for_empty_and_unknown_inputs() -> None:
    empty = ci_routes.classify([])
    assert (empty.rust_core, empty.macos_shell) == (True, True)
    assert empty.docs_only is False
    assert empty.governance is True
    assert empty.python is True
    assert empty.web is True
    assert empty.web_e2e is True
    assert empty.ci is True

    unknown = ci_routes.classify(["unrecognized-build-input"])
    assert (unknown.rust_core, unknown.macos_shell) == (True, True)


def test_candidate_routes_carry_the_computed_rust_flags() -> None:
    core = ci_routes.classify(["crates/quine-core/src/lib.rs"], candidate=True)
    shell = ci_routes.classify(["apps/desktop/src-tauri/src/main.rs"], candidate=True)
    docs = ci_routes.classify(["docs/STATUS.md"], candidate=True)
    assert (core.rust_core, core.macos_shell) == (True, False)
    assert (shell.rust_core, shell.macos_shell) == (False, True)
    assert (docs.rust_core, docs.macos_shell) == (False, False)


def _route_cli_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: bytes,
) -> dict[str, str]:
    stdin = io.TextIOWrapper(io.BytesIO(raw))
    monkeypatch.setattr(sys, "argv", ["ci_routes.py"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert ci_routes.main() == 0
    return dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines() if line)


def test_route_cli_emits_both_new_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _route_cli_output(
        monkeypatch,
        capsys,
        b"A\0apps/desktop/src-tauri/src/main.rs\0M\0crates/quine-core/src/core.rs\0",
    )
    assert output["rust_core"] == "true"
    assert output["macos_shell"] == "true"


def test_route_cli_handles_deletes_and_split_renames(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted = _route_cli_output(monkeypatch, capsys, b"D\0apps/desktop/src-tauri/src/old.rs\0")
    assert (deleted["rust_core"], deleted["macos_shell"]) == ("false", "true")

    renamed = _route_cli_output(
        monkeypatch,
        capsys,
        b"D\0crates/quine-core/src/old.rs\0A\0crates/quine-core/src/new.rs\0",
    )
    assert (renamed["rust_core"], renamed["macos_shell"]) == ("true", "false")


def test_route_cli_treats_unsupported_git_status_conservatively(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _route_cli_output(monkeypatch, capsys, b"T\0docs/STATUS.md\0")
    assert (output["rust_core"], output["macos_shell"]) == ("true", "true")


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


def test_real_installed_graph_keeps_browser_e2e_when_the_graph_is_unknown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    shutil.copytree(PROJECT_ROOT / "src" / "ea", source / "ea")
    module = source / "ea" / "execution" / "order_lifecycle_probe.py"
    module.write_text("from ea.core import OrderId\n", encoding="utf-8")
    path = "src/ea/execution/order_lifecycle_probe.py"

    # The real server resolves its runtime imports dynamically, so this graph is
    # unknown: an added module cannot be proven unreachable from the installed path.
    assert ci_routes.installed_python_modules(source) is None
    routes = ci_routes.classify(
        [path, "tests/unit/test_order_lifecycle_probe.py", "docs/STATUS.md"],
        added_paths=frozenset({path, "tests/unit/test_order_lifecycle_probe.py"}),
        source_root=source,
    )
    assert routes.python is True
    assert routes.governance is True
    assert routes.web_e2e is True


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
    assert "-vv" in pytest_commands[0]
    assert "--durations=25" in pytest_commands[0]
    assert "faulthandler_timeout=120" in pytest_commands[0]
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
