from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from ea.config.settings import Environment, RunMode, Settings, _SettingsInput

_APPLICATION_ENVIRONMENT_NAMES = frozenset(
    {
        "EA_CONFIG_PATH",
        "EA_ENVIRONMENT",
        "EA_RUN_MODE",
    }
)


class ConfigurationError(ValueError):
    """A safe, user-facing configuration failure."""


class _EnvironmentInput(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EA_", extra="forbid")

    config_path: Path | None = None
    environment: Environment | None = None
    run_mode: RunMode | None = None


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    snapshot: Settings
    config_path: Path | None


def _safe_validation_message(source: str, error: ValidationError) -> str:
    problems: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        if item["type"] == "extra_forbidden":
            problems.append(f"unknown field '{location}'")
        elif item["type"] == "live_mode_unavailable":
            problems.append("live mode is unavailable in this build")
        else:
            problems.append(f"invalid field '{location}' ({item['type']})")
    return f"{source}: {'; '.join(problems)}"


def _validate_process_environment_names(environ: Mapping[str, str]) -> None:
    seen: dict[str, str] = {}
    unknown: set[str] = set()
    noncanonical: set[str] = set()
    duplicates: set[str] = set()

    for name in environ:
        normalized = name.upper()
        if not normalized.startswith("EA_"):
            continue
        if normalized in seen and seen[normalized] != name:
            duplicates.update({seen[normalized], name})
        else:
            seen[normalized] = name
        if normalized not in _APPLICATION_ENVIRONMENT_NAMES:
            unknown.add(name)
        elif name != normalized:
            noncanonical.add(name)

    problems: list[str] = []
    if unknown:
        problems.append(f"unknown names: {', '.join(sorted(unknown))}")
    if noncanonical:
        problems.append(f"non-canonical names: {', '.join(sorted(noncanonical))}")
    if duplicates:
        problems.append(f"ambiguous duplicates: {', '.join(sorted(duplicates))}")
    if problems:
        raise ConfigurationError(f"process environment: {'; '.join(problems)}")


def _load_process_environment() -> dict[str, Any]:
    _validate_process_environment_names(os.environ)
    source = EnvSettingsSource(
        _EnvironmentInput,
        case_sensitive=False,
        env_prefix="EA_",
    )
    return dict(source())


def _settings_values_from_environment(values: Mapping[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if values.get("environment") is not None:
        settings["environment"] = values["environment"]
    if values.get("run_mode") is not None:
        settings["run"] = {"mode": values["run_mode"]}
    return settings


def _validate_settings_layer(source: str, values: Mapping[str, Any]) -> None:
    try:
        _SettingsInput.model_validate(values)
    except ValidationError as error:
        raise ConfigurationError(_safe_validation_message(source, error)) from error


def _resolve_config_path(path: str | Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve()
        is_file = resolved.is_file()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError("cannot resolve selected config path") from error
    if not is_file:
        raise ConfigurationError(f"selected config path is not a file: {resolved}")
    return resolved


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(f"cannot read selected config file: {path}") from error

    try:
        document = yaml.load(content, Loader=_UniqueKeySafeLoader)
    except (yaml.YAMLError, ValueError, RecursionError) as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigurationError(f"selected config file contains invalid YAML{location}") from error

    if document is None:
        raise ConfigurationError("selected config file is empty")
    if not isinstance(document, dict):
        raise ConfigurationError("selected config file root must be a mapping")
    if "schema_version" not in document:
        raise ConfigurationError("selected config file is missing required 'schema_version'")

    values = dict(document)
    _validate_settings_layer("YAML", values)
    return values


def load_configuration(
    *,
    config_path: str | Path | None = None,
    environment: str | Environment | None = None,
    run_mode: str | RunMode | None = None,
) -> LoadedConfiguration:
    """Load and validate one configuration snapshot at the outer boundary."""

    process_values = _load_process_environment()

    selected_path = config_path
    if selected_path is None:
        selected_path = process_values.get("config_path")

    resolved_path = _resolve_config_path(selected_path) if selected_path is not None else None
    yaml_values = _load_yaml(resolved_path) if resolved_path is not None else {}
    process_settings = _settings_values_from_environment(process_values)
    cli_settings = _settings_values_from_environment(
        {
            "environment": environment,
            "run_mode": run_mode,
        }
    )

    _validate_settings_layer("process environment", process_settings)
    _validate_settings_layer("CLI", cli_settings)

    merged: dict[str, Any] = {}
    for source in (yaml_values, process_settings, cli_settings):
        merged.update(source)

    try:
        snapshot = Settings.model_validate(merged)
    except ValidationError as error:
        raise ConfigurationError(_safe_validation_message("configuration", error)) from error

    return LoadedConfiguration(snapshot=snapshot, config_path=resolved_path)
