from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from ea.config import (
    ConfigurationError,
    Environment,
    RunMode,
    SecretRef,
    load_configuration,
)


def _write_yaml(path: Path, *, environment: str, run_mode: str) -> Path:
    path.write_text(
        f"schema_version: 1\nenvironment: {environment}\nrun:\n  mode: {run_mode}\n",
        encoding="utf-8",
    )
    return path


def test_default_snapshot_is_typed_and_transitively_immutable(
    isolated_ea_environment: None,
) -> None:
    loaded = load_configuration()

    assert loaded.config_path is None
    assert loaded.snapshot.schema_version == 1
    assert loaded.snapshot.environment is Environment.DEVELOPMENT
    assert loaded.snapshot.run.mode is RunMode.BACKTEST
    mutable_run = cast(Any, loaded.snapshot.run)
    with pytest.raises(ValidationError, match="frozen"):
        mutable_run.mode = RunMode.PAPER


def test_secret_reference_is_an_opaque_immutable_identifier() -> None:
    reference = SecretRef(identifier="broker.primary/api-key")

    assert reference.identifier == "broker.primary/api-key"
    mutable_reference = cast(Any, reference)
    with pytest.raises(ValidationError, match="frozen"):
        mutable_reference.identifier = "broker.secondary/api-key"
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        SecretRef(identifier="RAW SECRET=value")


def test_selected_yaml_is_loaded_and_resolved(
    isolated_ea_environment: None,
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "settings.yaml",
        environment="staging",
        run_mode="paper",
    )

    loaded = load_configuration(config_path=config_path)

    assert loaded.config_path == config_path.resolve()
    assert loaded.snapshot.environment is Environment.STAGING
    assert loaded.snapshot.run.mode is RunMode.PAPER


def test_relative_cli_config_path_resolves_from_invocation_cwd(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "settings.yaml",
        environment="staging",
        run_mode="paper",
    )
    monkeypatch.chdir(tmp_path)

    loaded = load_configuration(config_path=Path("settings.yaml"))

    assert loaded.config_path == config_path
    assert loaded.snapshot.environment is Environment.STAGING
    assert loaded.snapshot.run.mode is RunMode.PAPER


def test_source_precedence_is_cli_process_yaml_defaults(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "settings.yaml",
        environment="development",
        run_mode="backtest",
    )
    monkeypatch.setenv("EA_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("EA_ENVIRONMENT", "staging")
    monkeypatch.setenv("EA_RUN_MODE", "backtest")

    loaded = load_configuration(environment="production", run_mode="paper")

    assert loaded.config_path == config_path.resolve()
    assert loaded.snapshot.environment is Environment.PRODUCTION
    assert loaded.snapshot.run.mode is RunMode.PAPER


def test_cli_config_path_overrides_process_path(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process_config = _write_yaml(
        tmp_path / "process.yaml",
        environment="staging",
        run_mode="backtest",
    )
    cli_config = _write_yaml(
        tmp_path / "cli.yaml",
        environment="production",
        run_mode="paper",
    )
    monkeypatch.setenv("EA_CONFIG_PATH", str(process_config))

    loaded = load_configuration(config_path=cli_config)

    assert loaded.config_path == cli_config.resolve()
    assert loaded.snapshot.environment is Environment.PRODUCTION
    assert loaded.snapshot.run.mode is RunMode.PAPER


def test_dotenv_is_not_loaded_implicitly(
    isolated_ea_environment: None,
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "EA_ENVIRONMENT=production\nEA_RUN_MODE=paper\n",
        encoding="utf-8",
    )

    loaded = load_configuration()

    assert loaded.snapshot.environment is Environment.DEVELOPMENT
    assert loaded.snapshot.run.mode is RunMode.BACKTEST


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "selected config file is empty"),
        ("- development\n- backtest\n", "root must be a mapping"),
        ("environment: [\n", "contains invalid YAML"),
        ("value: !!python/object:builtins.str {}\n", "contains invalid YAML"),
        (
            "schema_version: 1\nenvironment: development\n---\nschema_version: 1\n",
            "contains invalid YAML",
        ),
    ],
)
def test_invalid_yaml_documents_fail_concisely(
    isolated_ea_environment: None,
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(config_path=config_path)


@pytest.mark.parametrize(
    "content",
    [
        (
            "schema_version: 1\n"
            "environment: development\n"
            "environment: staging\n"
            "run:\n"
            "  mode: backtest\n"
        ),
        ("schema_version: 1\nenvironment: development\nrun:\n  mode: backtest\n  mode: paper\n"),
    ],
)
def test_duplicate_yaml_keys_fail_before_collapse(
    isolated_ea_environment: None,
    tmp_path: Path,
    content: str,
) -> None:
    config_path = tmp_path / "duplicate.yaml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="contains invalid YAML"):
        load_configuration(config_path=config_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "environment: development\nrun:\n  mode: backtest\n",
            "missing required 'schema_version'",
        ),
        (
            "schema_version: 2\nenvironment: development\nrun:\n  mode: backtest\n",
            "invalid field 'schema_version'",
        ),
        (
            'schema_version: "1"\nenvironment: development\nrun:\n  mode: backtest\n',
            "invalid field 'schema_version'",
        ),
    ],
)
def test_yaml_schema_version_is_required_and_strict(
    isolated_ea_environment: None,
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    config_path = tmp_path / "schema.yaml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(config_path=config_path)


def test_missing_selected_file_fails_concisely(
    isolated_ea_environment: None,
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.yaml"

    with pytest.raises(ConfigurationError, match="selected config path is not a file"):
        load_configuration(config_path=missing_path)


@pytest.mark.parametrize(
    "content",
    [
        (
            "schema_version: 1\n"
            "environment: development\n"
            "future_setting: ignored\n"
            "run:\n"
            "  mode: backtest\n"
        ),
        "schema_version: 1\nenv: development\nrun:\n  mode: backtest\n",
        "schema_version: 1\nenvironment: development\nmode: backtest\n",
        "schema_version: 1\npaper_trading_enabled: true\n",
        "schema_version: 1\nlive_trading_enabled: false\n",
        (
            "schema_version: 1\n"
            "environment: development\n"
            "run:\n"
            "  mode: backtest\n"
            "  future_setting: ignored\n"
        ),
    ],
)
def test_unknown_or_removed_yaml_fields_fail(
    isolated_ea_environment: None,
    tmp_path: Path,
    content: str,
) -> None:
    config_path = tmp_path / "unknown.yaml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown field"):
        load_configuration(config_path=config_path)


def test_unknown_process_environment_name_fails_without_value_leak(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "must-not-appear"
    monkeypatch.setenv("EA_FUTURE_SETTING", secret_value)

    with pytest.raises(ConfigurationError) as captured:
        load_configuration()

    assert "EA_FUTURE_SETTING" in str(captured.value)
    assert secret_value not in str(captured.value)


def test_noncanonical_and_duplicate_process_names_fail(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUN_MODE", "backtest")
    monkeypatch.setenv("ea_run_mode", "paper")

    with pytest.raises(ConfigurationError, match="ambiguous duplicates"):
        load_configuration()


@pytest.mark.parametrize(
    "name",
    [
        "EA_ENV",
        "EA_PAPER_TRADING_ENABLED",
        "EA_LIVE_TRADING_ENABLED",
    ],
)
def test_removed_environment_names_fail(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv(name, "true")

    with pytest.raises(ConfigurationError, match=name):
        load_configuration()


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("environment", "sandbox"),
        ("run_mode", "simulation"),
        ("environment", "STAGING"),
        ("run_mode", "PAPER"),
    ],
)
def test_invalid_typed_values_fail_without_echoing_value(
    isolated_ea_environment: None,
    source: str,
    value: str,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        if source == "environment":
            load_configuration(environment=value)
        else:
            load_configuration(run_mode=value)

    expected_field = "run.mode" if source == "run_mode" else source
    assert expected_field in str(captured.value)
    assert value not in str(captured.value)


def test_live_mode_is_unavailable_and_fails_closed(
    isolated_ea_environment: None,
) -> None:
    with pytest.raises(ConfigurationError, match="live mode is unavailable in this build"):
        load_configuration(run_mode="live")


def test_lower_priority_live_is_safely_overridden_before_final_gate(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "live.yaml",
        environment="production",
        run_mode="live",
    )
    monkeypatch.setenv("EA_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("EA_RUN_MODE", "live")

    loaded = load_configuration(run_mode="backtest")

    assert loaded.snapshot.run.mode is RunMode.BACKTEST


def test_completed_snapshot_does_not_change_after_environment_mutation(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUN_MODE", "backtest")
    first = load_configuration()
    monkeypatch.setenv("EA_RUN_MODE", "paper")
    second = load_configuration()

    assert first.snapshot.run.mode is RunMode.BACKTEST
    assert second.snapshot.run.mode is RunMode.PAPER


def test_unprefixed_process_secret_is_not_configuration(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "outside-configuration-boundary"
    monkeypatch.setenv("BROKER_API_KEY", secret_value)

    loaded = load_configuration()

    assert secret_value not in loaded.snapshot.model_dump_json()
