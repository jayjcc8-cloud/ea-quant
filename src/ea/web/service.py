"""Bounded scenario registry and persistent single-job Web service state."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml

from ea.core import RunId
from ea.data.historical import HistoricalMarketDataError
from ea.plugin.declarative import DeclarationStoreV1
from ea.plugin.local_strategy import (
    LOCAL_STRATEGY_ENTRYPOINT,
    LocalStrategyCapabilityV1,
    activate_local_strategy,
    select_local_strategy,
)
from ea.plugin.manifest import ManifestError
from ea.plugin.selection import BINDING_SCHEMA, PluginBindingV1
from ea.product import (
    BacktestRunFailure,
    BacktestScenarioError,
    LoadedBacktestScenario,
    generate_backtest_report,
    load_backtest_scenario,
    parameterize_backtest_scenario,
    run_backtest_scenario,
)
from ea.product.equity_path import generate_equity_path_analysis
from ea.product.reporting import _fsync_directory, _write_report_file
from ea.product.scenario import (
    parameterize_strategy_scenario,
    rebind_research_dataset,
    resolved_scenario_parameters,
    scenario_digest_domain,
)
from ea.product.trade_analytics import trade_analytics
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import StrategyPackage, inspect_package, read_regular, validate_package
from ea.strategy.registry import project_parameters
from ea.web import candidates
from ea.web.datasets import LocalResearchDatasetRegistryV1
from ea.web.holdout import HoldoutRecord, compatible, decode_holdout, require_chronology
from ea.web.local_packages import LOCAL_SCENARIO_ID, LocalStrategyStoreV1
from ea.web.uploads import (
    MAPPED_VERSION_SCHEMA,
    PROVIDER_VERSION_SCHEMA,
    UPLOAD_VERSION_SCHEMA,
    ControlledUploadStoreV1,
    LocalResearchDatasetCatalogV1,
)

_SCENARIO_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_JOB_STATES = frozenset({"accepted", "running", "succeeded", "failed", "interrupted"})
_ARTIFACTS = frozenset({"report.json", "summary.txt", "equity-path.json"})
_MAX_SCENARIO_BYTES = 64 * 1024
_INPUT_DIGEST_DOMAIN = b"ea.local-web-input.v1\0"


_REPORT_SCHEMAS = {
    "ea.local-web-job.v4": "ea.backtest-report.v2",
    "ea.local-web-job.v5": "ea.backtest-report.v3",
}
_PATH_SCHEMAS = {
    "ea.local-web-job.v4": "ea.backtest-equity-path.v2",
    "ea.local-web-job.v5": "ea.backtest-equity-path.v3",
}

# Every input snapshot schema this validator accepts, oldest first. The last
# entry is the version the validator currently produces, so a capability bound to
# it drifts when a new schema is added here.
VALIDATOR_INPUT_SCHEMAS: tuple[str, ...] = (
    "ea.local-web-input.v1",
    "ea.local-web-input.v2",
)


def _job_schema(scenario: LoadedBacktestScenario) -> str:
    return {4: "ea.local-web-job.v4", 5: "ea.local-web-job.v5"}.get(
        scenario.schema_version, "ea.local-web-job.v3"
    )


def _binds_local_strategy(plugin_binding: object) -> bool:
    """Whether one frozen binding selects a workspace-local strategy package.

    This is the only marker that separates the new explicitly trusted slot path
    from the legacy strategy-root path: both carry a ``local-package`` source, but
    only the new one freezes the capability in the job's binding.
    """
    if type(plugin_binding) is not dict:
        return False
    try:
        binding = PluginBindingV1.decode(plugin_binding)
    except ManifestError:
        return False
    component = binding.component("strategy")
    return component is not None and component.entrypoint == LOCAL_STRATEGY_ENTRYPOINT


def _scenario_document(payload: bytes) -> dict[str, Any]:
    """One scenario document, YAML or canonical JSON, exactly as it is written."""
    try:
        document = yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ManifestError(
            "scenario_invalid", "the scenario is not one readable document"
        ) from None
    if type(document) is not dict:
        raise ManifestError("scenario_invalid", "the scenario is not one mapping")
    return document


def _default_parameters(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """The declared defaults of one package descriptor, or a refusal.

    A virtual scenario has to name every parameter of the package it selects, so a
    descriptor whose parameters carry no default cannot be registered rather than
    being guessed at.
    """
    parameters: dict[str, Any] = {}
    for parameter in descriptor.get("parameters") or []:
        name = parameter.get("name") if isinstance(parameter, Mapping) else None
        if type(name) is not str or "default" not in parameter:
            raise ManifestError(
                "package_invalid",
                "every declared parameter of a local strategy needs a default the "
                "virtual scenario can state",
            )
        parameters[name] = parameter["default"]
    return parameters


class WebBoundaryError(ValueError):
    """A request crosses a configured local Web boundary."""


class ScenarioNotFoundError(WebBoundaryError):
    """A registered scenario does not exist."""


class InputChangedError(WebBoundaryError):
    """Validated input identity no longer matches the current files."""


class RequestConflictError(WebBoundaryError):
    """An idempotency key was reused with different input."""


class ServiceBusyError(WebBoundaryError):
    """The single active-job slot is occupied."""


class JobNotFoundError(WebBoundaryError):
    """A workspace job does not exist."""


class BatchNotFoundError(WebBoundaryError):
    """A workspace experiment batch does not exist."""


class DuplicateBatchRunError(WebBoundaryError):
    """A batch repeats one normalized strategy parameter combination."""


class ReportUnavailableError(WebBoundaryError):
    """A job has no verified immutable report."""


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _directory(path: Path, *, label: str, create: bool) -> Path:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        value = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        raise WebBoundaryError(f"{label} is unavailable") from None
    if not stat.S_ISDIR(value.st_mode):
        raise WebBoundaryError(f"{label} must be a directory")
    return resolved


def _contained(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (AttributeError, ValueError):
        return False


def roots_overlap(*roots: Path) -> bool:
    """Return whether any two resolved authorization roots contain one another."""
    return any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    )


def _scenario_identity(scenario: LoadedBacktestScenario) -> dict[str, object]:
    fingerprint = scenario.dataset.selection.fingerprint
    research = (
        {} if scenario.research_input_bytes is None else json.loads(scenario.research_input_bytes)
    )
    return {
        **(
            {"dataset_id": research["dataset_id"], "source_sha256": research["source_sha256"]}
            if research
            else {}
        ),
        "scenario_sha256": scenario.scenario_sha256.value,
        "data_sha256": fingerprint.sha256.value,
        "record_count": fingerprint.record_count,
    }


def _validate_web_integer_parameters(scenario: LoadedBacktestScenario) -> None:
    if scenario.schema_version in (4, 5) and any(
        type(value) is int and abs(value) > 9007199254740991
        for value in scenario.strategy_parameters.values()
    ):
        raise BacktestScenarioError("Web integer parameters must be within the exact safe range")


def _strategy_parameter_contracts(
    scenario: LoadedBacktestScenario,
    *,
    defaults: LoadedBacktestScenario | None = None,
) -> list[dict[str, object]]:
    source = scenario if defaults is None else defaults
    _validate_web_integer_parameters(scenario)
    _validate_web_integer_parameters(source)
    parameters = scenario.strategy_parameters
    resolved = resolved_scenario_parameters(scenario)
    contracts = [
        p.document(parameters[p.parameter.name], source.strategy_parameters[p.parameter.name])
        for p in resolved
    ]
    if scenario.schema_version in (4, 5):
        for contract in contracts:
            if contract["type"] == "integer":
                contract["maximum"] = (
                    min(9007199254740991, contract["maximum"])
                    if contract["maximum"] is not None
                    else 9007199254740991
                )
    return contracts


def _scenario_summary(
    scenario_id: str,
    scenario: LoadedBacktestScenario,
    *,
    defaults: LoadedBacktestScenario | None = None,
) -> dict[str, object]:
    return {
        "scenario_id": scenario_id,
        "name": Path(scenario_id).stem,
        "valid": True,
        "input_identity": _scenario_identity(scenario),
        "summary": {
            "strategy_id": scenario.strategy_id.value,
            "venue": scenario.instrument.venue.code,
            "symbol": scenario.instrument.symbol,
            "initial_cash": scenario.initial_cash.text,
            "parameters": scenario.strategy_parameters,
            "strategy_version": scenario.strategy_version,
            "record_count": scenario.dataset.selection.fingerprint.record_count,
            "commission": json.loads(scenario.canonical_bytes)["execution"].get("commission"),
            "slippage": json.loads(scenario.canonical_bytes)["execution"].get("slippage"),
            "latency": json.loads(scenario.canonical_bytes)["execution"].get("latency"),
            "data": json.loads(scenario.canonical_bytes)["data"],
        },
        "strategy_parameters": _strategy_parameter_contracts(scenario, defaults=defaults),
        "strategy_descriptor": scenario.strategy_entry.descriptor.document(),
    }


def _parameterized(
    scenario: LoadedBacktestScenario, parameters: dict[str, object]
) -> LoadedBacktestScenario:
    expected_source = parameters.get("strategy_source")
    if expected_source is not None and expected_source != json.loads(scenario.canonical_bytes)[
        "strategy"
    ].get("source"):
        raise BacktestScenarioError("strategy implementation changed; frozen artifact is required")
    initial_cash = parameters.get("initial_cash")
    if type(initial_cash) is not str:
        raise BacktestScenarioError("initial_cash must be an ea-decimal-v1 string")
    strategy_parameters = parameters.get("strategy_parameters")
    if strategy_parameters is None:
        # Compatibility for the original public quantity-only request, isolated in V1 adapter.
        return parameterize_backtest_scenario(
            scenario,
            initial_cash=initial_cash,
            quantity=cast(str | None, parameters.get("quantity")),
        )
    if type(strategy_parameters) is not dict:
        raise BacktestScenarioError("strategy_parameters must be a mapping")
    if parameters.get("quantity") is not None:
        raise BacktestScenarioError("quantity conflicts with strategy_parameters")
    return parameterize_strategy_scenario(
        scenario, initial_cash=initial_cash, parameters=strategy_parameters
    )


def _input_snapshot(
    scenario_id: str,
    scenario: LoadedBacktestScenario,
    source_identity: dict[str, object],
) -> tuple[bytes, str]:
    _validate_web_integer_parameters(scenario)
    snapshot = {
        "schema": VALIDATOR_INPUT_SCHEMAS[0],
        "scenario_id": scenario_id,
        "source_identity": source_identity,
        "identity": _scenario_identity(scenario),
        "scenario": json.loads(scenario.canonical_bytes),
    }
    if scenario.research_input_bytes is not None:
        snapshot["schema"] = VALIDATOR_INPUT_SCHEMAS[-1]
        snapshot["research_input"] = json.loads(scenario.research_input_bytes)
    payload = _canonical_json(snapshot)
    return payload, sha256(_INPUT_DIGEST_DOMAIN + payload).hexdigest()


def _created_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ScenarioRegistry:
    """Resolve only immediate YAML files registered under one authorized root."""

    def __init__(self, root: Path, strategy_root: Path | None = None) -> None:
        self.strategy_root = strategy_root
        self.root = _directory(root, label="scenario root", create=False)

    def _path(self, scenario_id: str) -> Path:
        if (
            not 1 <= len(scenario_id) <= 128
            or any(character not in _SCENARIO_ID for character in scenario_id)
            or Path(scenario_id).suffix not in {".yaml", ".yml"}
        ):
            raise ScenarioNotFoundError("scenario is not registered")
        candidate = self.root / scenario_id
        try:
            resolved = candidate.resolve(strict=True)
            value = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            raise ScenarioNotFoundError("scenario is not registered") from None
        if (
            not _contained(resolved, self.root)
            or not stat.S_ISREG(value.st_mode)
            or value.st_size > _MAX_SCENARIO_BYTES
        ):
            raise ScenarioNotFoundError("scenario is not registered")
        return resolved

    def load(
        self, scenario_id: str, *, catalog: ResearchStrategyCatalogV1 | None = None
    ) -> LoadedBacktestScenario:
        scenario = load_backtest_scenario(
            self._path(scenario_id), strategy_root=self.strategy_root, catalog=catalog
        )
        if not _contained(scenario.scenario_path, self.root) or not _contained(
            scenario.data_path, self.root
        ):
            raise BacktestScenarioError("scenario input resolves outside the authorized root")
        return scenario

    def source_document(self, scenario_id: str) -> dict[str, Any]:
        """One registered scenario's own declaration, read statically and bounded.

        Path validation and YAML only: no strategy loader, no package import, no
        catalog. It exists so a caller that must decide something *about* a scenario
        before loading it can do so without giving the loader the chance to pull a
        strategy source in first.
        """
        path = self._path(scenario_id)
        try:
            payload = read_regular(path, self.root, limit=_MAX_SCENARIO_BYTES)
        except (OSError, ValueError):
            raise ScenarioNotFoundError("scenario is not registered") from None
        try:
            document = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BacktestScenarioError("the scenario is not one readable document") from None
        if type(document) is not dict:
            raise BacktestScenarioError("the scenario is not one mapping")
        return document

    def validate(self, scenario_id: str) -> dict[str, object]:
        return _scenario_summary(scenario_id, self.load(scenario_id))

    def list(self) -> list[dict[str, object]]:
        try:
            candidates = sorted(
                path.name
                for path in self.root.iterdir()
                if path.suffix in {".yaml", ".yml"} and not path.name.startswith(".")
            )
        except OSError:
            raise WebBoundaryError("scenario root cannot be listed") from None
        if len(candidates) > 100:
            raise WebBoundaryError("scenario root exceeds the 100-file limit")
        results: list[dict[str, object]] = []
        for scenario_id in candidates:
            try:
                results.append(self.validate(scenario_id))
            except (BacktestScenarioError, ScenarioNotFoundError) as error:
                message = str(error)
                if message == "data fingerprint conflicts with captured replay selection":
                    message = "data fingerprint conflicts with selected market data"
                results.append(
                    {
                        "scenario_id": scenario_id,
                        "name": Path(scenario_id).stem,
                        "valid": False,
                        "error_code": "scenario_invalid",
                        "message": message,
                    }
                )
        return results


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    request_id: str
    scenario_id: str
    input_identity: dict[str, object]
    status: str
    schema: str = "ea.local-web-job.v3"
    created_at: str | None = None
    input_snapshot_bytes: bytes | None = None
    input_sha256: str | None = None
    attempt_id: str | None = None
    engine_run_id: str | None = None
    report_sha256: str | None = None
    summary_sha256: str | None = None
    equity_path_sha256: str | None = None
    error_code: str | None = None
    message: str | None = None
    strategy_package: StrategyPackage | None = None
    # Exact capability identity this run was accepted with. Additive and absent
    # on records written before capability binding existed, so those stay
    # readable and byte-identical.
    plugin_binding: dict[str, object] | None = None
    # The published version of an uploaded input this run read, including its
    # declared availability policy and source assertion. Additive and absent for
    # a registered input, which has no published version to name.
    dataset_version: dict[str, object] | None = None

    def document(self, *, presentation: bool = False) -> dict[str, object]:
        document: dict[str, object] = {
            "schema": self.schema,
            "job_id": self.job_id,
            "request_id": self.request_id,
            "scenario_id": self.scenario_id,
            "input_identity": self.input_identity,
            "status": self.status,
            "engine_run_id": self.engine_run_id,
            "report_sha256": self.report_sha256,
            "summary_sha256": self.summary_sha256,
            "error_code": self.error_code,
            "message": self.message,
            "report_ready": (
                self.status == "succeeded"
                and self.report_sha256 is not None
                and self.summary_sha256 is not None
                and (
                    self.schema
                    not in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}
                    or self.equity_path_sha256 is not None
                )
            ),
        }
        if self.schema in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}:
            document["equity_path_sha256"] = self.equity_path_sha256
        if self.schema in {
            "ea.local-web-job.v2",
            "ea.local-web-job.v3",
            "ea.local-web-job.v4",
            "ea.local-web-job.v5",
        }:
            if (
                self.created_at is None
                or self.input_snapshot_bytes is None
                or self.input_sha256 is None
            ):
                raise RuntimeError("v2 Web job is missing immutable input evidence")
            document.update(
                {
                    "created_at": self.created_at,
                    "input_snapshot": json.loads(self.input_snapshot_bytes),
                    "input_sha256": self.input_sha256,
                    "attempt_id": self.attempt_id,
                }
            )
        if self.plugin_binding is not None:
            document["plugin_binding"] = self.plugin_binding
        if self.dataset_version is not None:
            document["dataset_version"] = self.dataset_version
        if presentation and self.input_snapshot_bytes is not None:
            strategy = json.loads(self.input_snapshot_bytes)["scenario"]["strategy"]
            entry = ResearchStrategyCatalogV1(
                () if self.strategy_package is None else (self.strategy_package,)
            ).resolve(strategy)
            # A frozen local package is presented from its own descriptor's
            # projection, never from BUILTIN_STRATEGIES: the local entry's parameter
            # validation callback is a no-op, so rendering history reads the
            # persisted canonical snapshot and the descriptor alone. Nothing here
            # imports or runs the package's Python, not even after a revocation.
            callback_normalized = "source" in strategy or self.schema in {
                "ea.local-web-job.v4",
                "ea.local-web-job.v5",
            }
            document["parameters"] = (
                entry.normalize(strategy["parameters"])
                if callback_normalized or _binds_local_strategy(self.plugin_binding)
                else project_parameters(strategy)
            )
            document["strategy_descriptor"] = entry.descriptor.document()
        return document


@dataclass(frozen=True, slots=True)
class BatchRecord:
    batch_id: str
    created_at: str
    scenario_id: str
    input_identity: dict[str, object]
    member_job_ids: tuple[str, ...]
    schema: str = "ea.local-web-batch.v1"

    def document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "scenario_id": self.scenario_id,
            "input_identity": self.input_identity,
            "member_job_ids": list(self.member_job_ids),
        }


@dataclass(frozen=True, slots=True)
class PreparedJob:
    scenario: LoadedBacktestScenario
    input_snapshot_bytes: bytes
    input_sha256: str


def _decode_job(payload: bytes) -> JobRecord:
    try:
        document = json.loads(payload)
        if type(document) is not dict or _canonical_json(document) != payload:
            raise ValueError
        schema = document.get("schema")
        if schema not in {
            "ea.local-web-job.v1",
            "ea.local-web-job.v2",
            "ea.local-web-job.v3",
            "ea.local-web-job.v4",
            "ea.local-web-job.v5",
        }:
            raise ValueError
        status_value = document.get("status")
        identity = document.get("input_identity")
        if type(status_value) is not str or status_value not in _JOB_STATES:
            raise ValueError
        if type(identity) is not dict:
            raise ValueError
        fields = ("job_id", "request_id", "scenario_id")
        if any(type(document.get(field)) is not str for field in fields):
            raise ValueError
        optional = (
            "engine_run_id",
            "report_sha256",
            "summary_sha256",
            "error_code",
            "message",
        )
        if any(
            document.get(field) is not None and type(document.get(field)) is not str
            for field in optional
        ):
            raise ValueError
        if type(document.get("report_ready")) is not bool:
            raise ValueError
        created_at: str | None = None
        input_snapshot_bytes: bytes | None = None
        input_sha256: str | None = None
        attempt_id: str | None = None
        if schema in {
            "ea.local-web-job.v2",
            "ea.local-web-job.v3",
            "ea.local-web-job.v4",
            "ea.local-web-job.v5",
        }:
            created_at_value = document.get("created_at")
            snapshot = document.get("input_snapshot")
            input_sha256_value = document.get("input_sha256")
            attempt_id_value = document.get("attempt_id")
            if (
                type(created_at_value) is not str
                or type(snapshot) is not dict
                or type(input_sha256_value) is not str
                or (attempt_id_value is not None and type(attempt_id_value) is not str)
            ):
                raise ValueError
            if (
                set(snapshot)
                != (
                    {"schema", "scenario_id", "source_identity", "identity", "scenario"}
                    | (
                        {"research_input"}
                        if snapshot.get("schema") == VALIDATOR_INPUT_SCHEMAS[-1]
                        else set()
                    )
                )
                or snapshot.get("schema") not in set(VALIDATOR_INPUT_SCHEMAS)
                or snapshot.get("scenario_id") != document["scenario_id"]
                or snapshot.get("source_identity") != identity
                or type(snapshot.get("identity")) is not dict
                or type(snapshot.get("scenario")) is not dict
            ):
                raise ValueError
            if any(
                (schema == job_schema) != (snapshot["scenario"].get("schema_version") == version)
                for job_schema, version in (("ea.local-web-job.v4", 4), ("ea.local-web-job.v5", 5))
            ):
                raise ValueError
            datetime.strptime(created_at_value, "%Y-%m-%dT%H:%M:%S.%fZ")
            input_snapshot_bytes = _canonical_json(snapshot)
            if (
                sha256(_INPUT_DIGEST_DOMAIN + input_snapshot_bytes).hexdigest()
                != input_sha256_value
            ):
                raise ValueError
            created_at = created_at_value
            input_sha256 = input_sha256_value
            attempt_id = attempt_id_value
        if schema in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}:
            digest = document.get("equity_path_sha256")
            if digest is not None and (
                type(digest) is not str
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError
            if status_value == "succeeded" and digest is None:
                raise ValueError
        # Optional and additive: a job accepted before capability binding is
        # still a valid record, and a bound one must carry a readable binding.
        plugin_binding = document.get("plugin_binding")
        if plugin_binding is not None and (
            type(plugin_binding) is not dict
            or set(plugin_binding) != {"schema", "binding_sha256", "components"}
            or plugin_binding.get("schema") != BINDING_SCHEMA
            or type(plugin_binding.get("binding_sha256")) is not str
            or type(plugin_binding.get("components")) is not list
        ):
            raise ValueError
        # Optional and additive, same rule: a run over a registered input has no
        # published version, and one over an uploaded version must carry it.
        dataset_version = document.get("dataset_version")
        if dataset_version is not None and (
            type(dataset_version) is not dict
            or dataset_version.get("schema")
            not in {UPLOAD_VERSION_SCHEMA, MAPPED_VERSION_SCHEMA, PROVIDER_VERSION_SCHEMA}
            or type(dataset_version.get("dataset_id")) is not str
            or type(dataset_version.get("availability_policy")) is not dict
        ):
            raise ValueError
        return JobRecord(
            job_id=document["job_id"],
            request_id=document["request_id"],
            scenario_id=document["scenario_id"],
            input_identity=identity,
            status=status_value,
            schema=schema,
            created_at=created_at,
            input_snapshot_bytes=input_snapshot_bytes,
            input_sha256=input_sha256,
            attempt_id=attempt_id,
            engine_run_id=document.get("engine_run_id"),
            report_sha256=document.get("report_sha256"),
            summary_sha256=document.get("summary_sha256"),
            equity_path_sha256=document.get("equity_path_sha256")
            if schema in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}
            else None,
            error_code=document.get("error_code"),
            message=document.get("message"),
            plugin_binding=plugin_binding,
            dataset_version=dataset_version,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise WebBoundaryError("workspace job index is invalid") from None


def _decode_batch(payload: bytes) -> BatchRecord:
    try:
        document = json.loads(payload)
        if type(document) is not dict or _canonical_json(document) != payload:
            raise ValueError
        if set(document) != {
            "schema",
            "batch_id",
            "created_at",
            "scenario_id",
            "input_identity",
            "member_job_ids",
        }:
            raise ValueError
        if document.get("schema") != "ea.local-web-batch.v1":
            raise ValueError
        fields = ("batch_id", "created_at", "scenario_id")
        if any(type(document.get(field)) is not str for field in fields):
            raise ValueError
        identity = document.get("input_identity")
        member_ids = document.get("member_job_ids")
        if type(identity) is not dict or type(member_ids) is not list:
            raise ValueError
        if (
            not 2 <= len(member_ids) <= 10
            or len(set(member_ids)) != len(member_ids)
            or any(type(member_id) is not str for member_id in member_ids)
        ):
            raise ValueError
        datetime.strptime(document["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        return BatchRecord(
            batch_id=document["batch_id"],
            created_at=document["created_at"],
            scenario_id=document["scenario_id"],
            input_identity=identity,
            member_job_ids=tuple(member_ids),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise WebBoundaryError("workspace batch index is invalid") from None


class WorkspaceLease:
    """Cross-process exclusive writer lease for one Web workspace."""

    def __init__(self, workspace: Path) -> None:
        self._path = workspace / "service.lock"
        self._descriptor: int | None = None

    def acquire(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o600:
                raise OSError
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._descriptor = descriptor
        except (BlockingIOError, OSError):
            if descriptor is not None:
                os.close(descriptor)
            raise WebBoundaryError(
                "workspace is already owned by another local Web service"
            ) from None

    def release(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None


class WebService:
    """Persistent, single-active-job adapter over the existing product functions."""

    def __init__(
        self,
        scenario_root: Path,
        workspace: Path,
        *,
        strategy_root: Path | None = None,
        data_root: Path | None = None,
    ) -> None:
        resolved_scenarios = _directory(scenario_root, label="scenario root", create=False)
        try:
            resolved_workspace = workspace.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise WebBoundaryError("workspace is unavailable") from None
        if roots_overlap(resolved_scenarios, resolved_workspace):
            raise WebBoundaryError("scenario and workspace roots must not overlap")
        if strategy_root is not None and (
            not strategy_root.is_absolute()
            or roots_overlap(strategy_root.resolve(), resolved_scenarios, resolved_workspace)
        ):
            raise WebBoundaryError("strategy root must be absolute and separate")
        self.datasets = None
        if data_root is not None:
            other_roots = [resolved_scenarios, resolved_workspace]
            if strategy_root is not None:
                other_roots.append(strategy_root.resolve())
            if not data_root.is_absolute() or roots_overlap(data_root.resolve(), *other_roots):
                raise WebBoundaryError("data root must be absolute and separate")
            try:
                self.datasets = LocalResearchDatasetRegistryV1(data_root)
            except (OSError, ValueError):
                raise WebBoundaryError("data root is unavailable") from None
        self.registry = ScenarioRegistry(resolved_scenarios, strategy_root)
        self.workspace = _directory(resolved_workspace, label="workspace", create=True)
        self.jobs_dir = _directory(self.workspace / "jobs", label="job index", create=True)
        self.candidates_dir = _directory(
            self.workspace / "candidates", label="candidates", create=True
        )
        self.batches_dir = _directory(self.workspace / "batches", label="batch index", create=True)
        self.inputs_dir = _directory(self.workspace / "inputs", label="input store", create=True)
        self.runs_dir = _directory(self.workspace / "runs", label="attempt root", create=True)
        self.reports_dir = _directory(self.workspace / "reports", label="report root", create=True)
        for directory, label in (
            (self.jobs_dir, "job index"),
            (self.candidates_dir, "candidates"),
            (self.batches_dir, "batch index"),
            (self.inputs_dir, "input store"),
            (self.runs_dir, "attempt root"),
            (self.reports_dir, "report root"),
        ):
            if not _contained(directory, self.workspace):
                raise WebBoundaryError(f"{label} resolves outside workspace")
        # The second data entrance and the user declaration store are both
        # workspace-local, so a restart finds exactly what was published and
        # registered before it.
        try:
            self.uploads = ControlledUploadStoreV1(self.workspace)
            self.declarations = DeclarationStoreV1(self.workspace)
            self.local_strategy = LocalStrategyStoreV1(self.workspace)
        except (OSError, ValueError):
            raise WebBoundaryError("workspace upload store is unavailable") from None
        if (
            not _contained(self.uploads.root, self.workspace)
            or not _contained(self.declarations.root, self.workspace)
            or not _contained(self.local_strategy.root.parent, self.workspace)
        ):
            raise WebBoundaryError("workspace upload store resolves outside workspace")
        self.dataset_catalog = LocalResearchDatasetCatalogV1(self.datasets, self.uploads)
        self._lease = WorkspaceLease(self.workspace)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ea-local-web")
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._requests: dict[str, str] = {}
        self._batches: dict[str, BatchRecord] = {}
        self._holdouts: dict[str, HoldoutRecord] = {}
        self._active: str | None = None

    def start(self) -> None:
        self._lease.acquire()
        try:
            self._load_jobs()
            self._load_batches()
            self._load_holdouts()
        except Exception:
            self._lease.release()
            raise

    def stop(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._lease.release()
        self.local_strategy.close()

    def _job_path(self, job_id: str) -> Path:
        bundle = self.jobs_dir / job_id
        return bundle / "job.json" if bundle.is_dir() else self.jobs_dir / f"{job_id}.json"

    def _batch_path(self, batch_id: str) -> Path:
        return self.batches_dir / f"{batch_id}.json"

    def _materialize_scenario(
        self,
        job_id: str,
        scenario: LoadedBacktestScenario,
    ) -> LoadedBacktestScenario:
        if scenario.strategy_package is not None:
            package_path = self.inputs_dir / f"{job_id}.eastrategy"
            with package_path.open("xb") as handle:
                handle.write(scenario.strategy_package.artifact_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            package_path.chmod(0o444)
        document = json.loads(scenario.canonical_bytes)
        document.pop("canonicalization")
        document["data"]["path"] = str(scenario.data_path)
        path = self.inputs_dir / f"{job_id}.json"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(_canonical_json(document))
                handle.flush()
                os.fsync(handle.fileno())
            directory_descriptor = os.open(self.inputs_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            if scenario.research_input_bytes is not None:
                self._check_dataset(scenario)
                return replace(scenario, scenario_path=path)
            materialized = load_backtest_scenario(
                path,
                catalog=ResearchStrategyCatalogV1(
                    () if scenario.strategy_package is None else (scenario.strategy_package,)
                ),
            )
            if (
                materialized.canonical_bytes != scenario.canonical_bytes
                or materialized.scenario_sha256 != scenario.scenario_sha256
                or materialized.data_path != scenario.data_path
            ):
                raise WebBoundaryError("normalized input identity conflicts")
            return materialized
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise

    def _write(self, record: JobRecord) -> None:
        payload = _canonical_json(record.document())
        descriptor, raw_path = tempfile.mkstemp(prefix=".job-", dir=self.jobs_dir)
        pending = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self._job_path(record.job_id))
            directory_descriptor = os.open(
                self._job_path(record.job_id).parent, os.O_RDONLY | os.O_DIRECTORY
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            pending.unlink(missing_ok=True)

    def _store(self, record: JobRecord) -> None:
        self._write(record)
        self._jobs[record.job_id] = record
        self._requests[record.request_id] = record.job_id

    def _write_batch(self, record: BatchRecord) -> None:
        payload = _canonical_json(record.document())
        descriptor, raw_path = tempfile.mkstemp(prefix=".batch-", dir=self.batches_dir)
        pending = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self._batch_path(record.batch_id))
            directory_descriptor = os.open(self.batches_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            pending.unlink(missing_ok=True)

    def _load_jobs(self) -> None:
        paths = sorted(self.jobs_dir.glob("*.json")) + sorted(
            path
            for path in self.jobs_dir.glob("*/job.json")
            if not path.parent.name.startswith(".")
        )
        if len(paths) > 1000:
            raise WebBoundaryError("workspace exceeds the 1000-job limit")
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                    raise OSError
                record = _decode_job(path.read_bytes())
            except OSError:
                raise WebBoundaryError("workspace job index is unavailable") from None
            if path != self._job_path(record.job_id) or record.job_id in self._jobs:
                raise WebBoundaryError("workspace job index conflicts")
            if record.request_id in self._requests:
                raise WebBoundaryError("workspace request index conflicts")
            if record.status in {"accepted", "running"}:
                record = replace(
                    record,
                    status="interrupted",
                    error_code="service_restarted",
                    message="local service stopped before completion; the job was not rerun",
                )
                self._write(record)
            if record.input_snapshot_bytes is not None:
                strategy = json.loads(record.input_snapshot_bytes)["scenario"]["strategy"]
                if "source" in strategy:
                    payload = read_regular(
                        self.inputs_dir / f"{record.job_id}.eastrategy", self.inputs_dir
                    )
                    if _binds_local_strategy(record.plugin_binding):
                        # A job whose binding froze a workspace-local package is read
                        # statically on every restart: trust may have been revoked
                        # since it ran, and history must never re-execute the Python.
                        package = inspect_package(
                            payload, expected_sha256=strategy["source"]["artifact_sha256"]
                        )
                    else:
                        package = validate_package(
                            payload, expected_sha256=strategy["source"]["artifact_sha256"]
                        )
                        ResearchStrategyCatalogV1((package,)).resolve(strategy)
                    record = replace(record, strategy_package=package)
            self._jobs[record.job_id] = record
            self._requests[record.request_id] = record.job_id

    def _load_batches(self) -> None:
        paths = sorted(self.batches_dir.glob("*.json"))
        if len(paths) > 1000:
            raise WebBoundaryError("workspace exceeds the 1000-batch limit")
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                    raise OSError
                record = _decode_batch(path.read_bytes())
            except OSError:
                raise WebBoundaryError("workspace batch index is unavailable") from None
            if path.name != f"{record.batch_id}.json" or record.batch_id in self._batches:
                raise WebBoundaryError("workspace batch index conflicts")
            try:
                members = [self._jobs[job_id] for job_id in record.member_job_ids]
            except KeyError:
                raise WebBoundaryError("workspace batch membership conflicts") from None
            if any(
                member.scenario_id != record.scenario_id
                or member.input_identity != record.input_identity
                for member in members
            ):
                raise WebBoundaryError("workspace batch membership conflicts")
            self._batches[record.batch_id] = record

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            records = sorted(
                self._jobs.values(),
                key=lambda record: (
                    record.created_at is not None,
                    record.created_at or "",
                    record.job_id,
                ),
                reverse=True,
            )
            return [record.document(presentation=True) for record in records]

    def _bind_dataset(
        self, scenario: LoadedBacktestScenario, dataset_id: str | None
    ) -> LoadedBacktestScenario:
        if dataset_id is None:
            return scenario
        if self.datasets is None and not self.uploads.has(dataset_id):
            raise WebBoundaryError("no data root is configured")
        try:
            path, dataset = self.dataset_catalog.load(dataset_id)
            return rebind_research_dataset(
                scenario, data_path=path, dataset=dataset, dataset_id=dataset_id
            )
        except (HistoricalMarketDataError, OSError, ValueError):
            raise WebBoundaryError("dataset is invalid, incompatible, or unavailable") from None

    def _check_dataset(self, scenario: LoadedBacktestScenario) -> None:
        if scenario.research_input_bytes is None:
            return
        evidence = json.loads(scenario.research_input_bytes)
        if self.datasets is None and not self.uploads.has(evidence["dataset_id"]):
            raise InputChangedError("dataset is unavailable")
        try:
            _, current = self.dataset_catalog.load(evidence["dataset_id"])
            if current.source_bytes_sha256 != scenario.dataset.source_bytes_sha256:
                raise ValueError
        except (HistoricalMarketDataError, OSError, ValueError):
            raise InputChangedError("dataset changed; validate it again") from None

    def _dataset_version(self, dataset_id: object) -> dict[str, object] | None:
        """The published version of one uploaded input, if it is one."""
        if type(dataset_id) is not str:
            return None
        return self.uploads.manifest(dataset_id)

    def _require_upload_binding(
        self, input_identity: dict[str, object], plugin_binding: dict[str, object] | None
    ) -> None:
        """An uploaded input is usable only through the data capability bound to it.

        A registered input keeps the existing default path: it carries no version
        evidence to pin, so no binding is required. An uploaded version does, and
        the data component of the plugin binding is the only record that freezes
        that evidence alongside the accepted input. A caller that names an upload
        without that binding — the direct backtest and batch paths — is refused
        here, before any materialization, reservation or engine effect.
        """
        dataset_id = input_identity.get("dataset_id")
        if type(dataset_id) is not str or not self.uploads.has(dataset_id):
            return
        if plugin_binding is None:
            raise InputChangedError(
                "an uploaded dataset is usable only through a bound research recipe; "
                "this request carries no capability binding"
            )
        try:
            binding = PluginBindingV1.decode(plugin_binding)
        except ManifestError:
            raise InputChangedError(
                "the capability binding on this request is not readable"
            ) from None
        data = binding.component("data")
        if data is None or data.plugin_id != f"ea.builtin.data.{dataset_id}":
            raise InputChangedError("the bound data capability does not name this uploaded dataset")
        evidence = data.identity.get("version_evidence_sha256")
        try:
            current = self.uploads.version_evidence_digest(dataset_id)
        except (HistoricalMarketDataError, OSError, ValueError):
            # A version whose evidence can no longer be re-derived — an edited
            # declaration, an edited raw source or an edited capture — is a changed
            # input, not an unreadable one, and it fails before any job exists.
            raise InputChangedError(
                "the published version of this dataset can no longer be re-derived; "
                "re-select the dataset and confirm again"
            ) from None
        if evidence != current:
            raise InputChangedError(
                "the bound data capability does not match this version's evidence; "
                "re-select the dataset and confirm again"
            )

    def _is_local_scenario(self, scenario_id: object) -> bool:
        return scenario_id == LOCAL_SCENARIO_ID

    def local_strategy_status(self) -> dict[str, object]:
        """What the workspace-local package slot currently is; read-only."""
        return self.local_strategy.status()

    def local_strategy_capability(self) -> LocalStrategyCapabilityV1 | None:
        """The current trusted capability, or None. Never establishes trust."""
        return self.local_strategy.capability()

    def local_strategy_binding(self) -> dict[str, object] | None:
        """The exact frozen binding the current slot selects, or None."""
        return self.local_strategy.binding()

    def local_strategy_facts(self) -> dict[str, object] | None:
        """The static facts the research catalog needs, without importing anything.

        These are what the virtual scenario is: strategy identity, instrument and
        default parameters read from the retained scenario and the package
        descriptor. Nothing here loads, compiles or executes the package.
        """
        try:
            capability = self.local_strategy.capability()
            scenario_path = self.local_strategy.scenario_path()
        except (OSError, ManifestError):
            return None
        if capability is None or scenario_path is None:
            return None
        try:
            document = _scenario_document(scenario_path.read_bytes())
        except (OSError, ValueError, ManifestError):
            return None
        instrument = document.get("instrument")
        data = document.get("data")
        funding = document.get("funding")
        if type(instrument) is not dict or type(data) is not dict or type(funding) is not dict:
            return None
        descriptor = capability.descriptor
        return {
            "scenario_id": LOCAL_SCENARIO_ID,
            "name": "Local strategy package",
            "valid": True,
            "strategy_id": descriptor.get("strategy_id"),
            "display_name": descriptor.get("display_name"),
            "instrument": {
                "venue": instrument.get("venue"),
                "symbol": instrument.get("symbol"),
            },
            "initial_cash": funding.get("initial_cash"),
            "parameters": [
                {
                    "name": parameter.get("name"),
                    "type": parameter.get("type"),
                    "minimum": parameter.get("static_minimum"),
                    "maximum": parameter.get("static_maximum"),
                    "default": parameter.get("default"),
                }
                for parameter in descriptor.get("parameters") or []
            ],
        }

    def inspect_local_strategy(self, *, manifest: object, payload: object) -> dict[str, object]:
        """Read one captured package statically: no trust, no write, no execution."""
        return self.local_strategy.inspect(manifest, payload).document()

    def register_local_strategy(
        self,
        *,
        manifest: object,
        payload: object,
        trusted: object,
        effects: object,
        base_scenario_id: object,
        expected_plugin_id: object,
        expected_package_version: object,
        expected_manifest_digest: object,
        expected_implementation_digest: object,
    ) -> dict[str, object]:
        """Trust one exact captured identity, on explicit operator decisions only.

        The base scenario this borrows data, funding, risk and execution from is a
        registered one and must not itself be local, so no second package is ever
        pulled in by a registration. The virtual scenario is built from the base
        document and retains only the strategy replacement.
        """
        if type(base_scenario_id) is not str:
            raise ManifestError("base_scenario_required", "a base scenario id is required")
        self._require_ordinary_base(base_scenario_id)
        base = self.registry.load(base_scenario_id)
        scenario = self._local_scenario_bytes(
            base, manifest, payload, expected_implementation_digest
        )
        return self.local_strategy.register(
            manifest=manifest,
            payload=payload,
            trusted=trusted,
            effects=effects,
            base_scenario_id=base_scenario_id,
            expected_plugin_id=expected_plugin_id,
            expected_package_version=expected_package_version,
            expected_manifest_digest=expected_manifest_digest,
            expected_implementation_digest=expected_implementation_digest,
            scenario=scenario,
            scenario_id=LOCAL_SCENARIO_ID,
            registered_at=_created_at(),
        )

    def revoke_local_strategy(self) -> dict[str, object]:
        """Withdraw trust in the workspace-local slot, durably."""
        return self.local_strategy.revoke()

    def _require_ordinary_base(self, scenario_id: str) -> None:
        """Refuse a local-sourced base scenario before any loader could import it.

        The base is read as one bounded YAML document first, through the registry's
        own path validation and no-follow read, so a base that names a local package
        is refused here rather than by a loader that would have had to load — and so
        import — that package to find out. Registering this slot borrows data,
        funding, risk and execution from an ordinary registered scenario, and never
        pulls a second package's Python in to do it.
        """
        document = self.registry.source_document(scenario_id)
        strategy = document.get("strategy")
        declared = strategy.get("source") if type(strategy) is dict else None
        if type(declared) is dict and declared.get("kind") == "local-package":
            raise ManifestError(
                "base_scenario_local",
                "a local strategy scenario cannot borrow from another local package; "
                "choose a registered scenario whose strategy this host already owns",
            )

    def _local_scenario_bytes(
        self,
        base: LoadedBacktestScenario,
        manifest: object,
        payload: object,
        expected_implementation_digest: object,
    ) -> bytes:
        """The retained virtual scenario: the base document, strategy replaced.

        Only ``strategy`` and ``schema_version`` differ from the registered base
        scenario, whose data, funding, risk and execution are kept as they are. The
        data path is pinned absolutely, because the retained YAML lives in the
        workspace slot rather than beside its base.
        """
        capability = self.local_strategy.inspect(manifest, payload)
        if expected_implementation_digest is not None and (
            "sha256:" + capability.artifact_sha256 != expected_implementation_digest
        ):
            raise ManifestError(
                "grant_identity_mismatch", "the captured bytes are not the granted implementation"
            )
        document = _scenario_document(base.canonical_bytes)
        # The canonical capture carries the loader-owned canonicalization record, the
        # same way _materialize_scenario drops it before writing a job document.
        document.pop("canonicalization", None)
        base_strategy = document.get("strategy")
        if type(base_strategy) is not dict or "source" in base_strategy:
            raise ManifestError(
                "base_scenario_invalid",
                "a local strategy scenario needs one registered built-in base scenario; "
                "a base scenario that already names a local package is refused",
            )
        descriptor = capability.descriptor
        schema_version = {1: 3, 2: 4, 3: 5}.get(capability.package_schema_version)
        if schema_version is None:
            raise ManifestError("package_invalid", "unsupported local strategy package schema")
        strategy: dict[str, object] = {
            "id": descriptor.get("strategy_id"),
            "version": descriptor.get("strategy_version"),
            "source": capability.source(),
            "parameters": _default_parameters(descriptor),
        }
        if schema_version in {4, 5}:
            strategy["action_contract"] = "V2"
            lifecycle = descriptor.get("position_lifecycle")
            if lifecycle not in {"single-long-round-trip-v1", "bounded-long-round-trips-v1"}:
                raise ManifestError(
                    "package_invalid",
                    "an action-contract local package must declare its position lifecycle",
                )
            strategy["position_lifecycle"] = lifecycle
        if schema_version == 5:
            limit = descriptor.get("max_round_trips")
            if type(limit) is not int:
                raise ManifestError(
                    "package_invalid",
                    "a bounded-round-trip local package must declare max_round_trips",
                )
            strategy["max_round_trips"] = limit
        document["schema_version"] = schema_version
        document["strategy"] = strategy
        document["data"] = {**document["data"], "path": str(base.data_path)}
        return yaml.safe_dump(document, sort_keys=False).encode("utf-8")

    def _local_catalog(
        self, plugin_binding: dict[str, object] | None = None
    ) -> ResearchStrategyCatalogV1:
        """The engine catalog for the current slot, re-checked against trust now.

        A revoked, replaced or drifted slot is a refusal here, at the moment of this
        read, rather than a fallback to whatever was once registered. Given a frozen
        binding, its strategy component must be exactly the capability this slot's
        current grant selects, so a slot replaced since a job was accepted refuses
        that job instead of running the new package.
        """
        capability = self.local_strategy.capability()
        if capability is None:
            raise ManifestError(
                "grant_required",
                "this workspace has no currently trusted local strategy package",
            )
        grant = self.local_strategy.grant()
        current = select_local_strategy(capability, grant=grant)
        if plugin_binding is not None:
            # A caller's binding carries every layer its recipe selected; only the
            # strategy component names this slot. That component must be exactly the
            # one this slot's current grant selects, and activation then runs over
            # the strategy-only binding: this host's one-entry catalog is the only
            # thing that can vouch for this capability, and the other layers keep
            # the verification their own catalogs and the frozen binding give them.
            frozen = PluginBindingV1.decode(plugin_binding)
            selected = frozen.component("strategy")
            expected = current.component("strategy")
            if selected is None or expected is None or selected.document() != expected.document():
                raise ManifestError(
                    "binding_drift",
                    "the frozen binding does not select the local strategy this slot "
                    "currently grants",
                )
        retained = self.local_strategy.retained()
        if retained is None:
            raise ManifestError("slot_invalid", "the local strategy slot is unreadable")
        return activate_local_strategy(
            current, retained[0], catalog=capability.catalog(), grant=grant
        )

    def _load_scenario(self, scenario_id: str) -> LoadedBacktestScenario:
        """Load one registered scenario, or the current workspace-local slot."""
        if not self._is_local_scenario(scenario_id):
            return self.registry.load(scenario_id)
        path = self.local_strategy.scenario_path()
        if path is None:
            raise ScenarioNotFoundError("no trusted local strategy package is registered")
        try:
            return load_backtest_scenario(path, catalog=self._local_catalog())
        except ManifestError:
            raise
        except (BacktestScenarioError, OSError, ValueError):
            raise ScenarioNotFoundError("the local strategy scenario is unreadable") from None

    def _require_local_binding(
        self, scenario_id: str, plugin_binding: dict[str, object] | None
    ) -> None:
        """The local slot is reachable only through its own exact bound capability.

        The direct scenario, batch and holdout paths carry no research recipe, so
        they name no capability that could freeze this identity. They are refused
        here, before any materialization, reservation or engine effect, and the
        binding must match the slot as it is *now*.
        """
        if not self._is_local_scenario(scenario_id):
            return
        current = self.local_strategy.binding()
        if current is None:
            raise ManifestError(
                "grant_required",
                "this workspace has no currently trusted local strategy package; "
                "register one explicitly before running it",
            )
        if plugin_binding is None:
            raise ManifestError(
                "local_strategy_unbound",
                "a local strategy scenario runs only through its bound capability; "
                "this request carries no capability binding",
            )
        try:
            binding = PluginBindingV1.decode(plugin_binding)
        except ManifestError:
            raise ManifestError(
                "local_strategy_unbound", "the capability binding on this request is not readable"
            ) from None
        strategy = binding.component("strategy")
        expected = PluginBindingV1.decode(current).component("strategy")
        if strategy is None or expected is None or strategy.document() != expected.document():
            raise ManifestError(
                "local_strategy_unbound",
                "the bound strategy capability is not the local package this workspace "
                "currently trusts; inspect and trust it again",
            )

    def validate_scenario(
        self,
        scenario_id: str,
        *,
        parameters: dict[str, object] | None = None,
        dataset_id: str | None = None,
        source_sha256: str | None = None,
    ) -> dict[str, object]:
        scenario = self._bind_dataset(self._load_scenario(scenario_id), dataset_id)
        if (
            source_sha256 is not None
            and scenario.dataset.source_bytes_sha256.value != source_sha256
        ):
            raise InputChangedError("dataset changed; explicitly select and validate it again")
        source_summary = _scenario_summary(scenario_id, scenario)
        if parameters is None:
            return source_summary
        normalized = _parameterized(scenario, parameters)
        normalized_summary = _scenario_summary(scenario_id, normalized, defaults=scenario)
        source_summary["summary"] = normalized_summary["summary"]
        source_summary["strategy_parameters"] = normalized_summary["strategy_parameters"]
        source_summary["normalized_input_identity"] = normalized_summary["input_identity"]
        return source_summary

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise JobNotFoundError("job was not found in this workspace") from None

    def lookup_job_by_request_id(self, request_id: str) -> JobRecord:
        """Read the job already bound to one idempotency key; create nothing.

        This is the recovery-side view of the existing request index: a caller
        that reserved ``request_id`` before an interrupted ``create_job`` can
        discover whether the job and its attempt already exist instead of
        submitting a second identity. It never starts work.
        """
        with self._lock:
            job_id = self._requests.get(request_id)
            if job_id is None:
                raise JobNotFoundError("no job is bound to this request_id") from None
            return self._jobs[job_id]

    def list_batches(self) -> list[dict[str, object]]:
        """Return one persisted summary per batch, newest first, without members."""
        with self._lock:
            summaries: list[dict[str, object]] = []
            for record in self._batches.values():
                running = any(
                    self._jobs[job_id].status in {"accepted", "running"}
                    for job_id in record.member_job_ids
                )
                summaries.append(
                    {
                        **record.document(),
                        "member_count": len(record.member_job_ids),
                        "status": "running" if running else "complete",
                    }
                )
            summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return summaries

    def get_batch(self, batch_id: str) -> dict[str, object]:
        with self._lock:
            try:
                record = self._batches[batch_id]
            except KeyError:
                raise BatchNotFoundError("batch was not found in this workspace") from None
            members: list[dict[str, object]] = []
            for job_id in record.member_job_ids:
                member = self._jobs[job_id]
                presentation_status = member.status
                if member.status == "accepted":
                    presentation_status = "queued"
                elif member.status == "failed" and member.error_code == "risk.rejected":
                    presentation_status = "risk.rejected"
                members.append(
                    {
                        **member.document(presentation=True),
                        "presentation_status": presentation_status,
                    }
                )
            running = any(member["status"] in {"accepted", "running"} for member in members)
            return {
                **record.document(),
                "member_count": len(members),
                "status": "running" if running else "complete",
                "members": members,
            }

    def _prepare_job(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        parameters: dict[str, object] | None,
        captured: LoadedBacktestScenario | None = None,
    ) -> PreparedJob:
        scenario = (
            captured
            if captured is not None
            else self._bind_dataset(
                self._load_scenario(scenario_id),
                cast(str | None, input_identity.get("dataset_id")),
            )
        )
        if _scenario_identity(scenario) != input_identity:
            raise InputChangedError("scenario input changed; validate it again")
        if parameters is not None:
            scenario = _parameterized(scenario, parameters)
        snapshot_bytes, input_sha256 = _input_snapshot(
            scenario_id,
            scenario,
            input_identity,
        )
        return PreparedJob(
            scenario=scenario,
            input_snapshot_bytes=snapshot_bytes,
            input_sha256=input_sha256,
        )

    def create_job(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        parameters: dict[str, object] | None = None,
        request_id: str,
        plugin_binding: dict[str, object] | None = None,
    ) -> tuple[JobRecord, bool]:
        with self._lock:
            self._require_upload_binding(input_identity, plugin_binding)
            self._require_local_binding(scenario_id, plugin_binding)
            prepared = self._prepare_job(
                scenario_id=scenario_id,
                input_identity=input_identity,
                parameters=parameters,
            )
            existing_id = self._requests.get(request_id)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if (
                    existing.scenario_id != scenario_id
                    or existing.input_identity != input_identity
                    or existing.input_snapshot_bytes != prepared.input_snapshot_bytes
                    or existing.plugin_binding != plugin_binding
                ):
                    raise RequestConflictError("request_id is already bound to different input")
                return existing, False
            if self._active is not None:
                raise ServiceBusyError("one local backtest is already active")
            job_id = str(uuid4())
            attempt_id = RunId(str(uuid4()))
            scenario = self._materialize_scenario(job_id, prepared.scenario)
            record = JobRecord(
                schema=_job_schema(prepared.scenario),
                strategy_package=prepared.scenario.strategy_package,
                job_id=job_id,
                request_id=request_id,
                scenario_id=scenario_id,
                input_identity=input_identity,
                status="accepted",
                created_at=_created_at(),
                input_snapshot_bytes=prepared.input_snapshot_bytes,
                input_sha256=prepared.input_sha256,
                attempt_id=attempt_id.value,
                plugin_binding=plugin_binding,
                dataset_version=self._dataset_version(input_identity.get("dataset_id")),
            )
            self._store(record)
            self._active = record.job_id
            self._executor.submit(self._run, record.job_id, scenario)
            return record, True

    def create_batch(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        initial_cash: str,
        runs: list[dict[str, object]],
    ) -> dict[str, object]:
        with self._lock:
            if not 2 <= len(runs) <= 10:
                raise BacktestScenarioError("batch run count must be between 2 and 10")
            if self._active is not None:
                raise ServiceBusyError("one local backtest or batch is already active")
            # A batch carries no capability binding at all, so an uploaded input
            # has nothing to freeze its provenance: this path is refused before any
            # batch member is prepared, materialized or reserved.
            self._require_upload_binding(input_identity, None)
            self._require_local_binding(scenario_id, None)
            prepared: list[PreparedJob] = []
            combinations: set[bytes] = set()
            captured = self._bind_dataset(
                self._load_scenario(scenario_id),
                cast(str | None, input_identity.get("dataset_id")),
            )
            for strategy_parameters in runs:
                item = self._prepare_job(
                    captured=captured,
                    scenario_id=scenario_id,
                    input_identity=input_identity,
                    parameters={
                        "initial_cash": initial_cash,
                        "strategy_parameters": strategy_parameters,
                    },
                )
                strategy = json.loads(item.scenario.canonical_bytes)["strategy"]
                combination = _canonical_json(strategy)
                if combination in combinations:
                    raise DuplicateBatchRunError(
                        "batch contains a duplicate normalized parameter combination"
                    )
                combinations.add(combination)
                prepared.append(item)

            batch_id = str(uuid4())
            records: list[JobRecord] = []
            executable: list[tuple[str, LoadedBacktestScenario]] = []
            try:
                for index, item in enumerate(prepared, start=1):
                    job_id = str(uuid4())
                    scenario = self._materialize_scenario(job_id, item.scenario)
                    record = JobRecord(
                        schema=_job_schema(item.scenario),
                        strategy_package=item.scenario.strategy_package,
                        job_id=job_id,
                        request_id=f"batch-{batch_id}-{index}",
                        scenario_id=scenario_id,
                        input_identity=input_identity,
                        status="accepted",
                        created_at=_created_at(),
                        input_snapshot_bytes=item.input_snapshot_bytes,
                        input_sha256=item.input_sha256,
                        attempt_id=RunId(str(uuid4())).value,
                        dataset_version=self._dataset_version(input_identity.get("dataset_id")),
                    )
                    self._store(record)
                    records.append(record)
                    executable.append((job_id, scenario))
                batch = BatchRecord(
                    batch_id=batch_id,
                    created_at=_created_at(),
                    scenario_id=scenario_id,
                    input_identity=input_identity,
                    member_job_ids=tuple(record.job_id for record in records),
                )
                self._write_batch(batch)
                self._batches[batch.batch_id] = batch
                self._active = batch.batch_id
                self._executor.submit(self._run_batch, batch.batch_id, tuple(executable))
            except Exception:
                self._active = None
                self._batches.pop(batch_id, None)
                self._batch_path(batch_id).unlink(missing_ok=True)
                for record in records:
                    self._jobs.pop(record.job_id, None)
                    self._requests.pop(record.request_id, None)
                    self._job_path(record.job_id).unlink(missing_ok=True)
                    (self.inputs_dir / f"{record.job_id}.json").unlink(missing_ok=True)
                raise
            return self.get_batch(batch.batch_id)

    def _load_holdouts(self) -> None:
        for path in sorted(self.jobs_dir.glob("*/holdout.json")):
            if path.parent.name.startswith("."):
                continue
            if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 65536:
                raise WebBoundaryError("holdout index is invalid")
            relation = decode_holdout(path.read_bytes())
            if (
                relation.holdout_job_id != path.parent.name
                or relation.validation_id in self._holdouts
                or relation.source_job_id not in self._jobs
                or relation.holdout_job_id not in self._jobs
            ):
                raise WebBoundaryError("holdout membership conflicts")
            self._holdouts[relation.validation_id] = relation
        for job in self._jobs.values():
            if self._job_path(job.job_id).name == "job.json" and not any(
                item.holdout_job_id == job.job_id for item in self._holdouts.values()
            ):
                raise WebBoundaryError("holdout job has no relationship")

    def list_holdouts(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                item.document()
                for item in sorted(
                    self._holdouts.values(),
                    key=lambda item: (item.created_at, item.validation_id),
                    reverse=True,
                )
            ]

    def get_holdout(self, validation_id: str) -> dict[str, str]:
        with self._lock:
            try:
                return self._holdouts[validation_id].document()
            except KeyError:
                raise JobNotFoundError("holdout was not found in this workspace") from None

    def _holdout_source(self, source_job_id: str) -> dict[str, Any]:
        source = self.get_job(source_job_id)
        report = json.loads(self.report(source_job_id))
        # Reuse canonical snapshot digest validation, including legacy exclusion.
        checked = _decode_job(_canonical_json(source.document()))
        if checked.input_snapshot_bytes is None:
            raise WebBoundaryError("source has no immutable input snapshot")
        snapshot = json.loads(checked.input_snapshot_bytes)
        document: dict[str, Any] = snapshot["scenario"]
        entry = ResearchStrategyCatalogV1(
            () if source.strategy_package is None else (source.strategy_package,)
        ).resolve(document["strategy"])
        if not entry.descriptor.research_visible:
            raise WebBoundaryError("source strategy is not research-visible")
        scenario_digest = sha256(
            scenario_digest_domain(document["schema_version"])
            + _canonical_json(document).rstrip(b"\n")
        ).hexdigest()
        if scenario_digest != snapshot["identity"]["scenario_sha256"]:
            raise WebBoundaryError("source scenario identity conflicts with snapshot")
        entry.normalize(document["strategy"]["parameters"]) if document["schema_version"] in (
            4,
            5,
        ) or "source" in document["strategy"] else project_parameters(document["strategy"])
        if (
            report.get("source", {}).get("scenario_sha256")
            != snapshot["identity"]["scenario_sha256"]
        ):
            raise WebBoundaryError("source report and snapshot identity conflict")
        return document

    def _prepare_holdout(
        self,
        source_job_id: str,
        scenario_id: str,
        dataset_id: str | None = None,
        source_sha256: str | None = None,
    ) -> PreparedJob:
        source = self._holdout_source(source_job_id)
        if _binds_local_strategy(self.get_job(source_job_id).plugin_binding):
            # A holdout carries no capability binding of its own, so there is nothing
            # here that could freeze or re-check a local package identity: this path
            # is refused rather than run against a trust that was never named again.
            raise WebBoundaryError(
                "a holdout over a workspace-local strategy package is not supported"
            )
        source_package = self.get_job(source_job_id).strategy_package
        frozen_catalog = (
            None if source_package is None else ResearchStrategyCatalogV1((source_package,))
        )
        target = self.registry.load(scenario_id, catalog=frozen_catalog)
        if dataset_id is not None:
            if self.datasets is None:
                raise WebBoundaryError("no data root is configured")
            path, dataset = self.datasets.load(dataset_id)
            if source_sha256 is not None and dataset.source_bytes_sha256.value != source_sha256:
                raise InputChangedError("holdout dataset changed; reselect it")
            target = rebind_research_dataset(
                target,
                data_path=path,
                dataset=dataset,
                dataset_id=dataset_id,
                frozen_document=source,
            )
        document = json.loads(target.canonical_bytes)
        if not compatible(source, document):
            raise WebBoundaryError("holdout configuration is incompatible with source")
        require_chronology(source, document)
        normalized = parameterize_strategy_scenario(
            target,
            initial_cash=source["funding"]["initial_cash"],
            parameters=dict(
                source["strategy"]["parameters"]
                if source_package is not None or source["schema_version"] in (4, 5)
                else project_parameters(source["strategy"])
            ),
        )
        payload, digest = _input_snapshot(scenario_id, normalized, _scenario_identity(target))
        return PreparedJob(normalized, payload, digest)

    def holdout_candidates(self, source_job_id: str) -> list[dict[str, object]]:
        with self._lock:
            self._holdout_source(source_job_id)
            candidates = []
            for path in sorted(self.registry.root.glob("*.y*ml")):
                scenario_id = path.name
                try:
                    prepared = self._prepare_holdout(source_job_id, scenario_id)
                except (WebBoundaryError, BacktestScenarioError, ValueError):
                    continue
                candidates.append(_scenario_summary(scenario_id, prepared.scenario))
            if self.datasets is not None:
                scenario_id = self.get_job(source_job_id).scenario_id
                for entry in self.datasets.list():
                    if not entry["valid"]:
                        continue
                    try:
                        prepared = self._prepare_holdout(
                            source_job_id, scenario_id, cast(str, entry["dataset_id"])
                        )
                    except (OSError, ValueError):
                        continue
                    summary = _scenario_summary(scenario_id, prepared.scenario)
                    summary["name"] = entry["dataset_id"]
                    candidates.append(summary)
            return candidates

    def create_holdout(
        self,
        *,
        source_job_id: str,
        scenario_id: str,
        dataset_id: str | None = None,
        source_sha256: str | None = None,
    ) -> dict[str, str]:
        with self._lock:
            prepared = self._prepare_holdout(source_job_id, scenario_id, dataset_id, source_sha256)
            if self._active is not None:
                raise ServiceBusyError("one local backtest or batch is already active")
            job_id = str(uuid4())
            relation = HoldoutRecord(str(uuid4()), _created_at(), source_job_id, job_id)
            record = JobRecord(
                schema=_job_schema(prepared.scenario),
                strategy_package=prepared.scenario.strategy_package,
                job_id=job_id,
                request_id=f"holdout-{relation.validation_id}",
                scenario_id=scenario_id,
                input_identity=json.loads(prepared.input_snapshot_bytes)["source_identity"],
                status="accepted",
                created_at=relation.created_at,
                input_snapshot_bytes=prepared.input_snapshot_bytes,
                input_sha256=prepared.input_sha256,
                attempt_id=RunId(str(uuid4())).value,
            )
            pending = Path(tempfile.mkdtemp(prefix=".holdout-", dir=self.jobs_dir))
            published = self.jobs_dir / job_id
            try:
                scenario = self._materialize_scenario(job_id, prepared.scenario)
                for name, document in (
                    ("job.json", record.document()),
                    ("holdout.json", relation.document()),
                ):
                    with (pending / name).open("xb") as handle:
                        os.fchmod(handle.fileno(), 0o600)
                        handle.write(_canonical_json(document))
                        handle.flush()
                        os.fsync(handle.fileno())
                descriptor = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(pending, published)
                descriptor = os.open(self.jobs_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._jobs[job_id] = record
                self._requests[record.request_id] = job_id
                self._holdouts[relation.validation_id] = relation
                self._active = job_id
                self._executor.submit(self._run, job_id, scenario)
            except Exception:
                self._active = None
                self._jobs.pop(job_id, None)
                self._requests.pop(record.request_id, None)
                self._holdouts.pop(relation.validation_id, None)
                shutil.rmtree(published, ignore_errors=True)
                (self.inputs_dir / f"{job_id}.json").unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(pending, ignore_errors=True)
            return relation.document()

    def _run_batch(
        self,
        batch_id: str,
        members: tuple[tuple[str, LoadedBacktestScenario], ...],
    ) -> None:
        try:
            for job_id, scenario in members:
                self._run(job_id, scenario, release_active=False)
        finally:
            with self._lock:
                if self._active == batch_id:
                    self._active = None

    def _run(
        self,
        job_id: str,
        scenario: LoadedBacktestScenario,
        *,
        release_active: bool = True,
    ) -> None:
        with self._lock:
            record = replace(self._jobs[job_id], status="running")
            self._store(record)
        try:
            if record.attempt_id is None:
                raise RuntimeError("accepted Web job has no reserved attempt identity")
            self._check_dataset(scenario)
            if _binds_local_strategy(record.plugin_binding):
                # Immediately before the engine is reached, the frozen binding and
                # the current trust are re-checked: a revocation between admission
                # and this moment refuses the run instead of starting it. It cannot
                # interrupt Python that has already begun.
                self._local_catalog(record.plugin_binding)
            result = run_backtest_scenario(
                scenario,
                self.runs_dir,
                run_id=RunId(record.attempt_id),
            )
            run_id = result.output_directory.name
            if run_id != record.attempt_id:
                raise RuntimeError("engine attempt identity conflicts with accepted Web job")
            record = replace(record, engine_run_id=run_id)
            with self._lock:
                self._store(record)
            try:
                report_dir = self.reports_dir / job_id
                with tempfile.TemporaryDirectory(
                    prefix=".path-pending-", dir=self.reports_dir
                ) as temporary:
                    staged = Path(temporary) / "artifacts"
                    generated = generate_backtest_report(result.output_directory, staged)
                    analysis = generate_equity_path_analysis(
                        result.output_directory, generated.report
                    )
                    path_payload = analysis.canonical_bytes
                    _write_report_file(staged / "equity-path.json", path_payload)
                    _fsync_directory(staged)
                    os.rename(staged, report_dir)
                    _fsync_directory(self.reports_dir)
                payload = generated.report.canonical_bytes
                summary_payload = generated.report.summary_bytes
                document = json.loads(payload)
                if (
                    type(document) is not dict
                    or document.get("schema")
                    != (_REPORT_SCHEMAS.get(record.schema, "ea.backtest-report.v1"))
                    or document.get("run_id") != run_id
                ):
                    raise ReportUnavailableError("generated report identity conflicts")
            except Exception:
                completed = replace(
                    record,
                    status="failed",
                    report_sha256=None,
                    summary_sha256=None,
                    error_code="report_failed",
                    message="the engine succeeded but the formal report was unavailable",
                )
            else:
                completed = replace(
                    record,
                    status="succeeded",
                    report_sha256=sha256(payload).hexdigest(),
                    summary_sha256=sha256(summary_payload).hexdigest(),
                    equity_path_sha256=sha256(path_payload).hexdigest(),
                    error_code=None,
                    message=None,
                )
        except BacktestRunFailure as error:
            if error.output_directory.name != record.attempt_id:
                completed = replace(
                    record,
                    status="failed",
                    error_code="web.internal_failure",
                    message="the local backtest failed unexpectedly",
                )
            else:
                completed = replace(
                    record,
                    status="failed",
                    engine_run_id=error.output_directory.name,
                    error_code=error.code.value,
                    message=str(error),
                )
        except (BacktestScenarioError, InputChangedError):
            completed = replace(
                record,
                status="failed",
                error_code="input_changed",
                message="scenario input changed; validate it again",
            )
        except Exception:
            completed = replace(
                record,
                status="failed",
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        with self._lock:
            self._store(completed)
            if release_active and self._active == job_id:
                self._active = None

    def _candidate_projection(self, validation_id: str) -> dict[str, Any]:
        from ea.web.candidate_evidence import CandidateEvidenceReader

        relation = self.get_holdout(validation_id)
        return CandidateEvidenceReader(self.workspace).projection(
            validation_id, relation["holdout_job_id"]
        )

    def _write_candidate(self, record: dict[str, Any]) -> None:
        payload = candidates.canonical(record)
        candidates.decode_record(payload)
        if self.candidates_dir.is_symlink() or self.candidates_dir.resolve() != self.candidates_dir:
            raise ValueError("candidate directory is invalid")
        descriptor, name = tempfile.mkstemp(prefix=".candidate-", dir=self.candidates_dir)
        pending = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self.candidates_dir / f"{record['candidate_id']}.json")
            _fsync_directory(self.candidates_dir)
        finally:
            pending.unlink(missing_ok=True)

    def create_candidate(self, validation_id: str) -> dict[str, Any]:
        with self._lock:
            record = candidates.create_record(
                self._candidate_projection(validation_id),
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
            self._write_candidate(record)
            return record

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        from ea.web.candidate_evidence import CandidateEvidenceReader

        with self._lock:
            return CandidateEvidenceReader(self.workspace).record(candidate_id)

    def decide_candidate(self, candidate_id: str, outcome: str, reason: str) -> dict[str, Any]:
        with self._lock:
            before = self.get_candidate(candidate_id)
            after = candidates.decide_record(
                before, outcome, reason, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            )
            if after != before:
                self._write_candidate(after)
            return after

    def list_candidates(self) -> list[dict[str, Any]]:
        with self._lock:
            records = []
            for path in sorted(self.candidates_dir.glob("*.json")):
                if path.name.startswith("."):
                    continue
                try:
                    records.append(self.get_candidate(path.stem))
                except (OSError, ValueError, KeyError, TypeError):
                    records.append({"candidate_id": path.stem, "status": "UNAVAILABLE"})
            return records

    def report(self, job_id: str) -> bytes:
        record = self.get_job(job_id)
        if record.status != "succeeded" or record.report_sha256 is None:
            raise ReportUnavailableError("verified report is not available for this job")
        path = self.reports_dir / job_id / "report.json"
        try:
            payload = path.read_bytes()
        except OSError:
            raise ReportUnavailableError("verified report is unavailable") from None
        try:
            document: Any = json.loads(payload)
        except json.JSONDecodeError:
            raise ReportUnavailableError("verified report is invalid") from None
        if (
            sha256(payload).hexdigest() != record.report_sha256
            or type(document) is not dict
            or document.get("schema")
            != (_REPORT_SCHEMAS.get(record.schema, "ea.backtest-report.v1"))
            or document.get("run_id") != record.engine_run_id
        ):
            raise ReportUnavailableError("verified report identity conflicts")
        return payload

    def trade_analytics(self, job_id: str) -> dict[str, Any]:
        payload = self.report(job_id)
        try:
            return trade_analytics(payload)
        except (ValueError, KeyError, TypeError):
            raise ReportUnavailableError(
                "Verified complete-trade analytics are unavailable"
            ) from None

    def artifact(self, job_id: str, name: str) -> bytes:
        if name not in _ARTIFACTS:
            raise JobNotFoundError("artifact was not found")
        report_payload = self.report(job_id)
        if name == "report.json":
            return report_payload
        record = self.get_job(job_id)
        if name == "equity-path.json":
            if (
                record.schema
                not in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}
                or record.equity_path_sha256 is None
            ):
                raise ReportUnavailableError("Path analysis unavailable for this legacy run.")
            try:
                payload = read_regular(self.reports_dir / job_id / name, self.reports_dir / job_id)
                document = json.loads(payload)
                report = json.loads(report_payload)
                if (
                    sha256(payload).hexdigest() != record.equity_path_sha256
                    or document["schema"]
                    != (_PATH_SCHEMAS.get(record.schema, "ea.backtest-equity-path.v1"))
                    or document["run_id"] != record.engine_run_id
                    or document["report_sha256"] != record.report_sha256
                    or document["semantic_outcome_sha256"]
                    != report["completion"]["semantic_outcome_sha256"]
                ):
                    raise ValueError
                return payload
            except (OSError, ValueError, KeyError, TypeError):
                raise ReportUnavailableError("verified path analysis is unavailable") from None
        if record.summary_sha256 is None:
            raise ReportUnavailableError("verified summary is not available for this job")
        path = self.reports_dir / job_id / name
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError
            payload = path.read_bytes()
        except OSError:
            raise ReportUnavailableError("verified artifact is unavailable") from None
        if sha256(payload).hexdigest() != record.summary_sha256:
            raise ReportUnavailableError("verified artifact identity conflicts")
        return payload


__all__ = [
    "InputChangedError",
    "JobNotFoundError",
    "BatchNotFoundError",
    "DuplicateBatchRunError",
    "ReportUnavailableError",
    "RequestConflictError",
    "ScenarioNotFoundError",
    "ServiceBusyError",
    "WebBoundaryError",
    "WebService",
]
