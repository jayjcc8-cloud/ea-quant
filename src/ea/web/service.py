"""Bounded scenario registry and persistent single-job Web service state."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

import yaml

from ea.core import RunId
from ea.core.encoding import canonical_json_bytes
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
    BacktestScenarioError,
    LoadedBacktestScenario,
    load_backtest_scenario,
    parameterize_backtest_scenario,
)
from ea.product.reporting import _fsync_directory
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
from ea.web import candidates, worker
from ea.web.datasets import LocalResearchDatasetRegistryV1
from ea.web.holdout import HoldoutRecord, compatible, decode_holdout, require_chronology
from ea.web.local_packages import LOCAL_SCENARIO_ID, LocalStrategyStoreV1
from ea.web.uploads import (
    MAPPED_VERSION_SCHEMA,
    OFFLINE_HISTORY_VERSION_SCHEMA,
    PROVIDER_VERSION_SCHEMA,
    UPLOAD_VERSION_SCHEMA,
    ControlledUploadStoreV1,
    LocalResearchDatasetCatalogV1,
)

_SCENARIO_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
#: ``accepted``/``running`` are live; ``stopping`` is a cancellation the
#: controller has accepted but not yet confirmed, and ``stopped`` is the
#: confirmed stop. A restart reads every non-terminal state as ``interrupted``.
_JOB_STATES = frozenset(
    {"accepted", "running", "stopping", "stopped", "succeeded", "failed", "interrupted"}
)
_LIVE_JOB_STATES = frozenset({"accepted", "running", "stopping"})
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
    return canonical_json_bytes(document) + b"\n"


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


#: The exact keys one admitted input snapshot carries, and the exact keys one
#: admitted input identity carries. The source binding pair is present only when
#: the input is bound to a registered dataset.
_SNAPSHOT_KEYS = frozenset({"schema", "scenario_id", "source_identity", "identity", "scenario"})
_IDENTITY_KEYS = frozenset({"scenario_sha256", "data_sha256", "record_count"})
_BOUND_IDENTITY_KEYS = _IDENTITY_KEYS | {"dataset_id", "source_sha256"}


def _input_identity_block(value: object) -> dict[str, object] | None:
    """One admitted input identity, or ``None`` when it is not exactly one."""
    if type(value) is not dict:
        return None
    identity = cast(dict[str, object], value)
    keys = frozenset(identity)
    if keys not in {_IDENTITY_KEYS, _BOUND_IDENTITY_KEYS}:
        return None
    if (
        type(identity["scenario_sha256"]) is not str
        or type(identity["data_sha256"]) is not str
        or type(identity["record_count"]) is not int
    ):
        return None
    if keys == _BOUND_IDENTITY_KEYS and (
        type(identity["dataset_id"]) is not str or type(identity["source_sha256"]) is not str
    ):
        return None
    return identity


def read_input_snapshot(value: object) -> dict[str, object] | None:
    """The canonical snapshot document ``_input_snapshot`` renders, or ``None``.

    The same shape every durable job record enforces: the schema, the scenario it
    was admitted as, the source identity it was read under, the normalized
    identity it runs as, and the whole canonical scenario. A partial document, an
    extra block or an unreadable identity is not evidence about what ran, so a
    caller that must decide what a stored snapshot means is given ``None`` rather
    than a repaired guess.
    """
    if type(value) is not dict:
        return None
    snapshot = cast(dict[str, object], value)
    schema = snapshot.get("schema")
    extra = {"research_input"} if schema == VALIDATOR_INPUT_SCHEMAS[-1] else set()
    if (
        frozenset(snapshot) != (_SNAPSHOT_KEYS | extra)
        or schema not in set(VALIDATOR_INPUT_SCHEMAS)
        or type(snapshot.get("scenario_id")) is not str
        or not snapshot.get("scenario_id")
        or _input_identity_block(snapshot.get("source_identity")) is None
        or _input_identity_block(snapshot.get("identity")) is None
        or type(snapshot.get("scenario")) is not dict
        or not snapshot.get("scenario")
        or ("research_input" in snapshot and type(snapshot.get("research_input")) is not dict)
    ):
        return None
    return snapshot


def input_snapshot_sha256(snapshot: dict[str, object]) -> str:
    """The digest one snapshot document carries, recomputed from its own bytes."""
    return sha256(_INPUT_DIGEST_DOMAIN + _canonical_json(snapshot)).hexdigest()


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
    #: Which owner holds this run. Absent for a run this workspace stores itself;
    #: an owner name means the record is a read-only projection of another owner
    #: and nothing here may publish, overwrite or rerun it.
    owner: str | None = None
    #: Where one owner's published artifacts live, when they are not this
    #: workspace's own report root. Never persisted: it belongs to the projection.
    artifacts_dir: Path | None = None

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


class ExternalJobOwner(Protocol):
    """Another owner's runs, projected read-only into this workspace's vocabulary.

    A workspace stores the jobs it accepted. An experiment owner that keeps its
    own runs elsewhere — the Rust quine core — is read through this seam, so the
    existing run, report, path and comparison views can read an owner-registered
    success without this workspace publishing a second record of it.
    """

    def job(self, job_id: str) -> JobRecord | None: ...

    def stop_job(self, job_id: str) -> JobRecord | None: ...


@dataclass(frozen=True, slots=True)
class _Intent:
    """What one supervisor concluded about an attempt, before it is durable.

    This is a request to the commit gate, never job state: `_finalize` reloads
    the current record and arbitrates this against a cancellation that may have
    been accepted in the meantime. Only the paths named here are ever promoted,
    and only when that arbitration allows it.
    """

    outcome: str
    #: Whether this attempt's compute is positively confirmed to be over: the
    #: owned process group was shown gone, or no compute was ever spawned for
    #: it. This is a fact about execution, kept apart from whether the task was
    #: cancelled: a cancellation may only be recorded as a confirmed stop when
    #: this holds, and an unproven attempt never upgrades to one.
    compute_ended: bool = False
    engine_run_id: str | None = None
    error_code: str | None = None
    message: str | None = None
    report_sha256: str | None = None
    summary_sha256: str | None = None
    equity_path_sha256: str | None = None
    attempt_staging: Path | None = None
    report_staging: Path | None = None


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


@dataclass(frozen=True, slots=True)
class ExpectedInput:
    """The one confirmed input an owned submission must reproduce exactly.

    ``snapshot`` is the canonical input snapshot the user confirmed — schema,
    scenario id, source identity, normalized identity, the whole canonical
    scenario and any research input — and ``input_sha256`` is that snapshot's own
    digest. A submission whose final materialized scenario, data and cost
    assumptions produce anything else is refused before it is published.
    """

    snapshot: dict[str, object]
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
            not in {
                UPLOAD_VERSION_SCHEMA,
                MAPPED_VERSION_SCHEMA,
                PROVIDER_VERSION_SCHEMA,
                OFFLINE_HISTORY_VERSION_SCHEMA,
            }
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
        external: ExternalJobOwner | None = None,
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
        # Staging for the per-attempt worker processes. It is never served: an
        # attempt or report reaches the roots above only through one controller
        # promotion, so an interrupted run leaves nothing an official read can
        # mistake for evidence.
        self.worker_dir = _directory(
            self.workspace / ".worker", label="worker control root", create=True
        )
        for directory, label in (
            (self.jobs_dir, "job index"),
            (self.candidates_dir, "candidates"),
            (self.batches_dir, "batch index"),
            (self.inputs_dir, "input store"),
            (self.runs_dir, "attempt root"),
            (self.reports_dir, "report root"),
            (self.worker_dir, "worker control root"),
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
        #: Another owner's runs, read only: a job this workspace does not store is
        #: looked up there, and a stop for one is forwarded to its owner. This
        #: workspace keeps no record of it either way.
        self._external = external
        # The live handle of every worker this controller spawned. Cancellation
        # signals one of these and nothing else, so a stale durable record can
        # never make this process kill an unrelated one.
        self._runs: dict[str, worker.WorkerProcess] = {}
        # Test-only seam: one hook module the worker runs immediately before the
        # engine reads market data. The product never sets it.
        self._test_hook: str | None = None
        # Set once shutdown starts: no job admitted after this point may start
        # compute, and no new job may be admitted at all.
        self._closing = False
        # Jobs whose record could not be re-read after an unconfirmed write. This
        # process stops writing them rather than risk overwriting a state the
        # durable file may already hold; it is a memory-only gate, not a store.
        self._uncertain: set[str] = set()

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
        self._stop_live_workers()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._lease.release()
        self.local_strategy.close()

    def _stop_live_workers(self) -> None:
        """Stop every admitted attempt this controller still owns, within a bound.

        Shutdown covers the whole admitted set, not only the jobs that already
        registered a live handle: a job that was accepted but has not started,
        one queued behind a batch member, and one between admission and its
        handle registration are all marked ``stopping`` first. That durable mark
        is what makes those threads refuse to spawn compute, and what keeps a
        result landing in the same window from being published as a success.
        """
        with self._lock:
            self._closing = True
            stopping = [
                job_id
                for job_id, record in self._jobs.items()
                if record.status in _LIVE_JOB_STATES and not self._writes_frozen(job_id)
            ]
            for job_id in stopping:
                self._store(replace(self._jobs[job_id], status="stopping"))
            live = sorted(self._runs)
        for job_id in live:
            with self._lock:
                process = self._runs.get(job_id)
            if process is not None:
                process.request_stop()

    def stop_job(self, job_id: str) -> JobRecord:
        """Accept a cancellation request for one live job.

        The returned record is the *accepted* request, not a confirmed stop: the
        job is ``stopping`` and the supervising thread records ``stopped`` once
        the process group is gone. A job that already reached a terminal state is
        returned unchanged, so a cancellation that arrives after a committed
        result never rewrites that result. A run another owner registered is
        stopped through that owner, which answers with its own record; this
        workspace writes nothing for it either way.
        """
        record = self.get_job(job_id)
        if record.owner is not None:
            if self._external is None:
                raise JobNotFoundError("job was not found in this workspace")
            stopped = self._external.stop_job(job_id)
            if stopped is None:
                raise JobNotFoundError("job was not found in this workspace")
            return stopped
        with self._lock:
            record = self.get_job(job_id)
            if record.status not in _LIVE_JOB_STATES or self._writes_frozen(job_id):
                return record
            stopping = replace(
                record,
                status="stopping",
                error_code=None,
                message="the run is stopping; no result of it will be published",
            )
            self._store_adopting(stopping)
            process = self._runs.get(job_id)
        if process is not None:
            process.request_stop()
        return stopping

    def _store_adopting(self, record: JobRecord) -> None:
        """Write one job record, adopting whatever the durable file says.

        A write can replace the durable record and still raise on the directory
        barrier that follows it. Leaving the old in-memory state in place there
        would let a later cancellation or failure overwrite a success that is
        already committed on disk, so the durable record wins: it is re-read and
        adopted before the failure is reported. When even that read fails the
        job is frozen instead, so no later write can overwrite whatever the
        unconfirmed write may have committed.
        """
        try:
            self._store(record)
        except Exception:
            self._adopt_durable_job(record.job_id)
            raise

    def _adopt_durable_job(self, job_id: str) -> None:
        """Take the durable record as truth after an unconfirmed write.

        If it cannot be read back, nothing here can say what is durable, so the
        job is frozen: this process stops writing that job's record rather than
        letting a stale in-memory state overwrite a success the failed write may
        already have committed. The durable file stays the only authority, and a
        restart re-reads it as usual.
        """
        path = self._job_path(job_id)
        try:
            adopted = _decode_job(read_regular(path, path.parent))
        except (OSError, ValueError):
            self._uncertain.add(job_id)
            return
        if adopted.job_id != job_id:
            self._uncertain.add(job_id)
            return
        self._jobs[job_id] = adopted
        self._requests[adopted.request_id] = job_id

    def _stop_requested(self, job_id: str) -> bool:
        """Whether this attempt must stop rather than be published.

        A cancellation is a durable ``stopping`` mark. Shutdown is the same
        verdict held in memory, so it still stops and suppresses publication for
        every admitted job even if that mark's write did not confirm durability.
        """
        with self._lock:
            record = self._jobs[job_id]
            return record.status == "stopping" or (
                self._closing and record.status in _LIVE_JOB_STATES
            )

    def _writes_frozen(self, job_id: str) -> bool:
        """Whether this process may no longer write one job's record.

        True after a write whose durability could not be confirmed and whose
        durable record could not be read back. The job keeps the state it has in
        memory for reads, and every later write to it is refused rather than
        allowed to overwrite a possibly committed success.
        """
        return job_id in self._uncertain

    def _worker_control(self, job_id: str) -> Path:
        """The staging/rendezvous directory of one attempt."""
        path = self.worker_dir / job_id
        path.mkdir(mode=0o700, exist_ok=True)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise WebBoundaryError("worker control directory is unavailable") from None
        if resolved != path or not _contained(resolved, self.worker_dir):
            raise WebBoundaryError("worker control directory resolves outside the workspace")
        return resolved

    def _discard_worker_staging(self, job_id: str) -> None:
        """Remove one attempt's staging; it is never authoritative evidence."""
        shutil.rmtree(self.worker_dir / job_id, ignore_errors=True)

    def _worker_request(
        self, job_id: str, record: JobRecord, scenario: LoadedBacktestScenario
    ) -> dict[str, object]:
        """The versioned frozen-input request for one attempt.

        The expected digests come from the *admitted* scenario and the frozen
        research evidence this attempt was accepted with, never from a fresh read
        of the workspace: a file that drifted between admission and this moment
        must not become the new expectation for itself. Both frozen files are
        compared against those admitted identities here, so drift before the
        request is refused before any compute starts, and the digests carried in
        the request refuse drift after it.
        """
        frozen = self._frozen_scenario_bytes(scenario)
        if scenario.scenario_path != self.inputs_dir / f"{job_id}.json":
            raise WebBoundaryError("frozen scenario identity conflicts with the accepted job")
        if read_regular(scenario.scenario_path, self.inputs_dir) != frozen:
            raise InputChangedError("frozen scenario changed before dispatch")
        package = scenario.strategy_package
        package_path: Path | None = None
        package_sha256: str | None = None
        if package is not None:
            package_path = self.inputs_dir / f"{job_id}.eastrategy"
            if read_regular(package_path, self.inputs_dir) != package.artifact_bytes:
                raise InputChangedError("frozen strategy artifact changed before dispatch")
            package_sha256 = package.identity.artifact_sha256
        return worker.build_request(
            job_id=job_id,
            attempt_id=cast(str, record.attempt_id),
            scenario_path=scenario.scenario_path,
            scenario_sha256=sha256(frozen).hexdigest(),
            # The identity of the scenario this job actually admitted: the one the
            # user's parameters were applied to, not the registered source it was
            # derived from, so a legitimate override is never mistaken for drift.
            scenario_identity_sha256=scenario.scenario_sha256.value,
            research_input_bytes=scenario.research_input_bytes,
            package_path=package_path,
            package_sha256=package_sha256,
        )

    def _spawn_worker(self, control: Path) -> worker.WorkerProcess:
        return worker.spawn(control, test_hook=self._test_hook)

    def _job_path(self, job_id: str) -> Path:
        bundle = self.jobs_dir / job_id
        return bundle / "job.json" if bundle.is_dir() else self.jobs_dir / f"{job_id}.json"

    def _batch_path(self, batch_id: str) -> Path:
        return self.batches_dir / f"{batch_id}.json"

    def _frozen_scenario_bytes(self, scenario: LoadedBacktestScenario) -> bytes:
        """The exact bytes one attempt's frozen scenario file holds.

        This is the admitted scenario's own canonical document, so what is
        written at admission and what is expected at dispatch are built by one
        function and cannot drift apart.
        """
        document = json.loads(scenario.canonical_bytes)
        document.pop("canonicalization")
        document["data"]["path"] = str(scenario.data_path)
        return _canonical_json(document)

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
        payload = self._frozen_scenario_bytes(scenario)
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
                handle.write(payload)
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
            if record.status in _LIVE_JOB_STATES:
                # No live handle survived the restart, so a job that was accepted,
                # running or waiting to confirm a stop is interrupted: its staging
                # is abandoned, nothing is re-dispatched, and no partial promotion
                # is ever read as an official report.
                record = replace(
                    record,
                    status="interrupted",
                    error_code="service_restarted",
                    message="local service stopped before completion; the job was not rerun",
                )
                self._write(record)
                self._discard_worker_staging(record.job_id)
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
            return [self._presented(record).document(presentation=True) for record in records]

    def registered_scenario_file(self, scenario_id: str) -> Path:
        """The resolved file of one registered scenario, and nothing else.

        It exists so a caller that must render this scenario's frozen input through
        another process can name the same file this service loads, under the same
        registration and path rules. It reads no scenario content and creates
        nothing; loading the file is still the loader's own authority.
        """
        if not isinstance(scenario_id, str):
            raise ScenarioNotFoundError("scenario is not registered")
        if self._is_local_scenario(scenario_id):
            path = self.local_strategy.scenario_path()
            if path is None:
                raise ScenarioNotFoundError("no trusted local strategy package is registered")
            return path
        return self.registry._path(scenario_id)

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

    def prepare_input_snapshot(
        self,
        scenario_id: str,
        *,
        parameters: dict[str, object] | None = None,
        dataset_id: str | None = None,
        source_sha256: str | None = None,
    ) -> dict[str, object]:
        """The exact normalized input this workspace would admit for one job.

        This is the authoritative preview of an engine submission: it resolves the
        same dataset binding, normalizes the same parameters through the same
        validator and renders the same input snapshot ``create_job`` would freeze,
        and it creates nothing — no job, no reservation, no engine effect. A caller
        that shows the user what would run and later confirms it compares the
        admitted snapshot's own identity and digest against what was previewed,
        instead of trusting a hash it computed itself. The identity is re-derived
        between the two steps, so a change between them is refused here.
        """
        with self._lock:
            validated = self.validate_scenario(
                scenario_id,
                parameters=parameters,
                dataset_id=dataset_id,
                source_sha256=source_sha256,
            )
            identity = cast(dict[str, object], validated["input_identity"])
            self._require_upload_binding(identity, None)
            self._require_local_binding(scenario_id, None)
            prepared = self._prepare_job(
                scenario_id=scenario_id,
                input_identity=identity,
                parameters=parameters,
            )
            return {
                "input_identity": _scenario_identity(prepared.scenario),
                "input_sha256": prepared.input_sha256,
                "snapshot": json.loads(prepared.input_snapshot_bytes),
            }

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._presented(self._jobs[job_id])
            except KeyError:
                pass
        # A run this workspace does not store may be owned by another owner. Only a
        # read-only projection of the owner's own record is returned here — never a
        # record written into this workspace's index, and never a cached answer: an
        # owner that cannot be reached raises instead.
        if self._external is None:
            raise JobNotFoundError("job was not found in this workspace")
        projected = self._external.job(job_id)
        if projected is None:
            raise JobNotFoundError("job was not found in this workspace")
        return projected

    def _artifact_root(self, record: JobRecord) -> Path:
        """Where one record's published artifacts are read from.

        A run this workspace ran has its own report root; a run another owner
        registered is read from that owner's own publication, which is the only
        copy of it and the one its digest was registered against.
        """
        return (
            record.artifacts_dir
            if record.artifacts_dir is not None
            else self.reports_dir / record.job_id
        )

    def _presented(self, record: JobRecord) -> JobRecord:
        """One job as a reader may see it, including a state nothing can prove.

        A job whose record could not be confirmed durable is reported as
        interrupted with its own code instead of being presented as running
        forever: the local service no longer writes that record, so it cannot
        honestly claim the run is still under way. This is a read projection only
        — the durable file stays the single authority and a restart re-reads it.
        """
        if record.job_id not in self._uncertain or record.status not in _LIVE_JOB_STATES:
            return record
        return replace(
            record,
            status="interrupted",
            error_code="job_state_uncertain",
            message=(
                "this job's durable record could not be confirmed after an unconfirmed "
                "write; the service no longer writes it, and a restart re-reads it"
            ),
        )

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

    def _require_expected_input(
        self,
        scenario_id: str,
        scenario: LoadedBacktestScenario,
        prepared: PreparedJob,
        expected: ExpectedInput,
    ) -> None:
        """Refuse a materialized input that is not the one that was confirmed.

        The comparison is made against the actual scenario this job would run —
        its materialized document, its data as it is read back and its declared
        cost assumptions — and never against a caller's hash of something else.
        A snapshot that differs in any block names a run the user did not confirm,
        so it is refused here, before the job is stored and before the engine is
        reached. The checked object is the same one the record and the run use.
        """
        confirmed = read_input_snapshot(expected.snapshot)
        if (
            confirmed is None
            or confirmed.get("scenario_id") != scenario_id
            or input_snapshot_sha256(confirmed) != expected.input_sha256
        ):
            # A confirmation whose own document does not carry the digest it
            # claims, or that names another scenario, is not readable evidence.
            raise WebBoundaryError("the confirmed input envelope is unreadable")
        source_identity = confirmed["source_identity"]
        actual_bytes, actual_sha256 = _input_snapshot(
            scenario_id, scenario, cast(dict[str, object], source_identity)
        )
        if actual_bytes != prepared.input_snapshot_bytes:
            raise WebBoundaryError("the materialized input is not the one that was admitted")
        if actual_sha256 != expected.input_sha256 or json.loads(actual_bytes) != confirmed:
            raise InputChangedError(
                "the input this run would freeze is not the input that was confirmed; "
                "preview the experiment again and confirm the new one"
            )

    def _require_existing_is_expected(self, existing: JobRecord, expected: ExpectedInput) -> None:
        """Refuse to resolve a request identity onto a job that froze other input.

        The comparison uses the published job's own frozen snapshot — never a
        fresh read of the live source, which may legitimately have moved on since
        a successful run — and requires the claimed digest to be recomputable from
        that document, so a stored block edited under a retained digest is refused
        rather than accepted as the confirmed run.
        """
        stored = existing.input_snapshot_bytes
        document: object = None
        if type(stored) is bytes:
            try:
                document = json.loads(stored)
            except ValueError:
                document = None
        frozen = read_input_snapshot(document)
        if (
            frozen is None
            or frozen != expected.snapshot
            or existing.input_sha256 != expected.input_sha256
            or input_snapshot_sha256(frozen) != expected.input_sha256
        ):
            raise InputChangedError(
                "the run already bound to this request is not the input that was confirmed; "
                "preview the experiment again and confirm the new one"
            )

    def _require_replay_is_expected(
        self,
        existing: JobRecord,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        parameters: dict[str, object] | None,
        plugin_binding: dict[str, object] | None,
        expected: ExpectedInput,
    ) -> None:
        """Resolve one owned replay from the record the request already published.

        A request that already published a job is answered from that record and
        from the caller's own confirmation alone: the registered scenario, its
        data and its cost assumptions are not read again, so a source that moved
        on or disappeared after a completed run cannot re-label, re-run or refuse
        what that run froze. Every claim the replay makes — request, scenario,
        source identity, plugin binding, the confirmation's own snapshot, digest
        and normalized identity — is held to the record, so anything but the same
        input is refused rather than resolved.
        """
        if existing.input_snapshot_bytes is None or existing.input_sha256 is None:
            raise InputChangedError(
                "the run already bound to this request carries no readable input evidence"
            )
        self._require_existing_is_expected(existing, expected)
        if (
            existing.scenario_id != scenario_id
            or existing.input_identity != input_identity
            or existing.plugin_binding != plugin_binding
        ):
            raise RequestConflictError("request_id is already bound to different input")
        if expected.snapshot.get("source_identity") != existing.input_identity:
            raise InputChangedError(
                "the run already bound to this request is not the input that was confirmed; "
                "preview the experiment again and confirm the new one"
            )
        self._require_replay_parameters(existing, parameters)

    def _require_replay_parameters(
        self, existing: JobRecord, parameters: dict[str, object] | None
    ) -> None:
        """Hold a replay's own explicit parameters to the input the record froze.

        The stored canonical snapshot is the authority and it is projected exactly
        as a reader is shown it, so explicit cash, strategy parameters, quantity or
        strategy source that do not describe that input name a different request.
        A replay that carries no parameters claims nothing beyond the confirmation
        it already supplies.
        """
        if parameters is None:
            return
        frozen = cast(
            dict[str, object], json.loads(cast(bytes, existing.input_snapshot_bytes))["scenario"]
        )
        strategy = cast(dict[str, object], frozen["strategy"])
        if parameters.get("strategy_source") not in (None, strategy.get("source")):
            raise RequestConflictError("request_id is already bound to different input")
        initial_cash = parameters.get("initial_cash")
        if type(initial_cash) is not str:
            raise BacktestScenarioError("initial_cash must be an ea-decimal-v1 string")
        if initial_cash != cast(dict[str, object], frozen["funding"])["initial_cash"]:
            raise RequestConflictError("request_id is already bound to different input")
        strategy_parameters = parameters.get("strategy_parameters")
        if strategy_parameters is None:
            # The original quantity-only request shape claims one parameter, and
            # the frozen strategy block is where the record carries it.
            quantity = parameters.get("quantity")
            if quantity is not None and quantity != strategy.get("target_quantity"):
                raise RequestConflictError("request_id is already bound to different input")
            return
        if type(strategy_parameters) is not dict:
            raise BacktestScenarioError("strategy_parameters must be a mapping")
        if parameters.get("quantity") is not None:
            raise BacktestScenarioError("quantity conflicts with strategy_parameters")
        entry = ResearchStrategyCatalogV1(
            () if existing.strategy_package is None else (existing.strategy_package,)
        ).resolve(strategy)
        stored = existing.document(presentation=True)["parameters"]
        try:
            claimed = entry.normalize(cast(dict[str, object], strategy_parameters))
        except ValueError:
            raise RequestConflictError("request_id is already bound to different input") from None
        if claimed != stored:
            raise RequestConflictError("request_id is already bound to different input")

    def create_job(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        parameters: dict[str, object] | None = None,
        request_id: str,
        plugin_binding: dict[str, object] | None = None,
        expected_input: ExpectedInput | None = None,
        before_publish: Callable[[], None] | None = None,
    ) -> tuple[JobRecord, bool]:
        """Admit, freeze and publish one job.

        Two optional arguments exist for an owned submission whose confirmed input
        is frozen elsewhere. ``expected_input`` is the confirmed snapshot the
        materialized input must reproduce exactly. ``before_publish`` is the
        owner's own dispatch gate: it is called after that final check and
        immediately before the first durable job record, and it must affirm that
        publication is still this operation's to make, or raise. Either one may
        refuse; a refusal publishes nothing and submits nothing.
        """
        with self._lock:
            self._require_upload_binding(input_identity, plugin_binding)
            self._require_local_binding(scenario_id, plugin_binding)
            existing_id = self._requests.get(request_id)
            if existing_id is not None and expected_input is not None:
                # An owned replay is answered from what this request already
                # published, before any read of the registered source: a scenario
                # that changed or disappeared after the run completed is not
                # evidence about what that run froze.
                existing = self._jobs[existing_id]
                self._require_replay_is_expected(
                    existing,
                    scenario_id=scenario_id,
                    input_identity=input_identity,
                    parameters=parameters,
                    plugin_binding=plugin_binding,
                    expected=expected_input,
                )
                return existing, False
            prepared = self._prepare_job(
                scenario_id=scenario_id,
                input_identity=input_identity,
                parameters=parameters,
            )
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
            if self._active is not None or self._closing:
                raise ServiceBusyError("one local backtest is already active")
            job_id = str(uuid4())
            attempt_id = RunId(str(uuid4()))
            scenario = self._materialize_scenario(job_id, prepared.scenario)
            if expected_input is not None:
                self._require_expected_input(scenario_id, scenario, prepared, expected_input)
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
            if before_publish is not None:
                before_publish()
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
            if self._active is not None or self._closing:
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
        if source.owner is not None:
            # A chronology holdout continues the source attempt this workspace ran:
            # it re-reads that attempt's evidence and freezes the source's own
            # parameters into the later scenario. A run another owner registered has
            # no attempt here to continue, and this path would otherwise freeze the
            # source snapshot into a second Python job this workspace would run and
            # publish itself. Both entry points — the candidate query and the create
            # call — come through here first, so the refusal happens before any job,
            # relation or admitted input exists and before any worker or executor is
            # reached. There is no fallback: the source is not run again under
            # another owner.
            raise WebBoundaryError(
                "a chronological holdout over a run another owner registered is not "
                "supported here; that run's evidence is read through its own owner"
            )
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
            if self._active is not None or self._closing:
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
        """Supervise one attempt and hand one intent to the commit gate.

        Nothing here persists a terminal result. Admission and finalization are
        each one locked operation on the current job record, so a cancellation
        request can only be ordered against them, never lost between them.
        """
        try:
            self._run_attempt(job_id, scenario)
        finally:
            with self._lock:
                if release_active and self._active == job_id:
                    self._active = None

    def _run_attempt(self, job_id: str, scenario: LoadedBacktestScenario) -> None:
        admitted = self._admit(job_id)
        if admitted is None:
            # A cancellation was already accepted for this job. It settles as
            # stopped here, without ever spawning compute.
            self._finalize(job_id, _Intent(outcome="stopped", compute_ended=True))
            return
        try:
            intent = self._execute(job_id, admitted, scenario)
        except (BacktestScenarioError, InputChangedError):
            # Unproven by default. Only `_execute` knows where the spawn boundary
            # is, so it is the only place allowed to report that a failure cannot
            # have started compute; anything escaping it stays unconfirmed.
            intent = _Intent(
                outcome="failed",
                error_code="input_changed",
                message="scenario input changed; validate it again",
            )
        except Exception:
            intent = _Intent(
                outcome="failed",
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        self._finalize(job_id, intent)

    def _admit(self, job_id: str) -> JobRecord | None:
        """Take an accepted job as this thread's running attempt, or refuse.

        A job that is no longer ``accepted`` has already been settled or had its
        cancellation accepted, so this attempt must not start: returning ``None``
        keeps a stop that landed before this thread ran from being overwritten by
        an unconditional ``running`` write.
        """
        with self._lock:
            current = self._jobs[job_id]
            if current.status != "accepted" or self._closing or self._writes_frozen(job_id):
                return None
            running = replace(current, status="running")
            self._store_adopting(running)
            return running

    def _execute(self, job_id: str, record: JobRecord, scenario: LoadedBacktestScenario) -> _Intent:
        try:
            if record.attempt_id is None:
                raise RuntimeError("accepted Web job has no reserved attempt identity")
            self._check_dataset(scenario)
            if _binds_local_strategy(record.plugin_binding):
                # Immediately before the engine is reached, the frozen binding and the
                # current trust are re-checked: a revocation between admission and this
                # moment refuses the run instead of starting it. It cannot interrupt
                # Python that has already begun, in this process or in the worker.
                self._local_catalog(record.plugin_binding)
            control = self._worker_control(job_id)
            worker.write_request(control, self._worker_request(job_id, record, scenario))
        except (BacktestScenarioError, InputChangedError):
            # Nothing up to here can start compute, so a failure now is an attempt
            # that never began.
            return _Intent(
                outcome="failed",
                compute_ended=True,
                error_code="input_changed",
                message="scenario input changed; validate it again",
            )
        except Exception:
            return _Intent(
                outcome="failed",
                compute_ended=True,
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        if self._stop_requested(job_id):
            # The cancellation landed after admission but before the spawn: no
            # compute is started for it, and the request already written is only
            # staging that the commit gate never serves.
            return _Intent(outcome="stopped", compute_ended=True)
        # From the spawn call onwards compute may exist, so nothing that fails here
        # is ever reported as proof that none was started: that call can hand back
        # a live child and still raise, leaving a handle this controller never
        # registered. Such a child is still bounded — the liveness channel it
        # inherited closes with this process — but it is not a confirmed stop.
        process: worker.WorkerProcess | None = None
        try:
            process = self._spawn_worker(control)
            with self._lock:
                self._runs[job_id] = process
            outcome = self._supervise(job_id, process)
            return self._settle(job_id, record, outcome, control)
        except Exception:
            return _Intent(
                outcome="failed",
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        finally:
            if process is not None:
                with self._lock:
                    self._runs.pop(job_id, None)
                process.close()

    def _supervise(self, job_id: str, process: worker.WorkerProcess) -> str:
        """Wait for the owned process tree, escalating a stop within one bound.

        Four outcomes, and only the first two are terminal:

        ``committed``
            no stop was accepted and the whole owned group is gone;
        ``stopped``
            a stop was accepted and the whole owned group is confirmed gone;
        ``unconfirmed``
            a stop was accepted but the group outlived SIGKILL, so the job stays
            ``stopping``: an unconfirmed stop is never reported as stopped;
        ``abandoned``
            no stop was accepted, yet the group outlived SIGKILL after the
            leader exited. Nothing of that attempt is published, because its
            compute could not be shown to have ended.

        The escalation is TERM, then one wait of ``STOP_GRACE_SECONDS``, then
        KILL, then one wait of ``KILL_GRACE_SECONDS``: the promised bound is
        those two waits and no second grace wait is added elsewhere.
        """
        stop_accepted = False
        term_sent = False
        kill_sent = False
        term_deadline = 0.0
        kill_deadline = 0.0
        while not process.stopped():
            now = time.monotonic()
            if self._stop_requested(job_id):
                stop_accepted = True
            if not term_sent and (stop_accepted or process.leader_reaped()):
                term_sent = True
                if stop_accepted:
                    process.request_stop()
                    term_deadline = now + worker.STOP_GRACE_SECONDS
                else:
                    # The leader is finished but something it started is not: no
                    # compute may outlive its attempt, and nothing asked that
                    # leftover group to linger, so it is killed outright.
                    process.kill()
                    kill_sent = True
                    kill_deadline = now + worker.KILL_GRACE_SECONDS
            elif term_sent and not kill_sent and now >= term_deadline:
                process.kill()
                kill_sent = True
                kill_deadline = now + worker.KILL_GRACE_SECONDS
            elif kill_sent and now >= kill_deadline:
                return "unconfirmed" if stop_accepted else "abandoned"
            process.wait(worker.REAP_SECONDS)
        return "stopped" if stop_accepted else "committed"

    def _settle(self, job_id: str, record: JobRecord, outcome: str, control: Path) -> _Intent:
        """Verify what the worker staged and describe the outcome it earns.

        Nothing here is durable and nothing here is promoted: the returned intent
        is arbitrated against the *current* job — and its artifacts published —
        only inside `_finalize`. That is what keeps the window between "this
        attempt has a verified result" and "this attempt is committed" cancellable.
        """
        if outcome == "unconfirmed":
            return _Intent(
                outcome="unconfirmed",
                error_code="worker_stop_unconfirmed",
                message="the run was told to stop but its process group is not confirmed gone",
            )
        if outcome == "stopped":
            # A stop the supervisor confirmed: the owned group is gone.
            return _Intent(outcome="stopped", compute_ended=True)
        if outcome == "abandoned":
            # No stop was accepted, yet the owned group could not be shown to be
            # gone. Nothing of this attempt is published: a result may only be
            # committed once its compute is confirmed to have ended.
            return _Intent(
                outcome="failed",
                error_code="worker_group_unconfirmed",
                message="the run's process group could not be confirmed stopped",
            )
        # Every branch below is reached only after the supervisor confirmed the
        # owned group is gone, so the attempt's compute is over.
        result = worker.read_result(control)
        if not self._result_is_this_attempt(result, job_id, record):
            return _Intent(
                outcome="failed",
                compute_ended=True,
                error_code="web.internal_failure",
                message=self._worker_failure_message(control),
            )
        return replace(
            self._settled_intent(cast(dict[str, Any], result), record, control),
            compute_ended=True,
        )

    def _settled_intent(self, result: dict[str, Any], record: JobRecord, control: Path) -> _Intent:
        attempt_id = cast(str, record.attempt_id)
        if result["status"] == "failed":
            code = result.get("code")
            staged_attempt = self._staged_attempt(control, attempt_id, result)
            if code == "report_failed" and staged_attempt is not None:
                return _Intent(
                    outcome="failed",
                    engine_run_id=attempt_id,
                    error_code="report_failed",
                    message="the engine succeeded but the formal report was unavailable",
                    attempt_staging=staged_attempt,
                )
            if code in worker.CLASSIFIED_FAILURES:
                return _Intent(
                    outcome="failed",
                    error_code=cast(str, code),
                    message=(
                        "scenario input changed; validate it again"
                        if code == "input_changed"
                        else "the local backtest failed unexpectedly"
                    ),
                    attempt_staging=staged_attempt,
                )
            if result.get("engine_run_id") == attempt_id:
                return _Intent(
                    outcome="failed",
                    engine_run_id=attempt_id,
                    error_code=cast(str, code),
                    message=cast("str | None", result.get("message")),
                    attempt_staging=staged_attempt,
                )
            return _Intent(
                outcome="failed",
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        verified = self._verified_staging(control, result, record)
        if verified is None:
            return _Intent(
                outcome="failed",
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        attempt_staging, report_staging, digests = verified
        return _Intent(
            outcome="succeeded",
            engine_run_id=attempt_id,
            report_sha256=digests["report.json"],
            summary_sha256=digests["summary.txt"],
            equity_path_sha256=digests["equity-path.json"],
            attempt_staging=attempt_staging,
            report_staging=report_staging,
        )

    def _staged_attempt(
        self, control: Path, attempt_id: str, result: dict[str, Any]
    ) -> Path | None:
        """The staged attempt directory, when the worker says it produced one."""
        if not result.get("attempt_staged"):
            return None
        try:
            return worker.staged_path(control, f"{worker.RUNS_NAME}/{attempt_id}")
        except worker.WorkerError:
            return None

    @staticmethod
    def _result_is_this_attempt(
        result: dict[str, Any] | None, job_id: str, record: JobRecord
    ) -> bool:
        return bool(
            result is not None
            and result.get("job_id") == job_id
            and result.get("attempt_id") == record.attempt_id
            and result.get("status") in {"succeeded", "failed"}
        )

    @staticmethod
    def _worker_failure_message(control: Path) -> str:
        phase = worker.last_phase(control)
        if phase is None:
            return "the local backtest failed unexpectedly"
        return f"the local backtest failed unexpectedly (last worker phase: {phase})"

    def _finalize(self, job_id: str, intent: _Intent) -> JobRecord:
        """The one commit gate: arbitrate the current job, then publish.

        The current record is reloaded under the lock, the accepted cancellation
        and the finished attempt are ordered here, every promotion happens here,
        and the durable job record is written here — one operation, one lock, one
        writer. A job that already reached a terminal state is returned
        unchanged, which is why a committed success survives a later stop.
        """
        with self._lock:
            current = self._jobs[job_id]
            if self._writes_frozen(job_id):
                # A previous write to this job could not be confirmed durable and
                # its record could not be re-read, so nothing here may write over
                # the state that failed write may already have committed.
                return current
            # Shutdown is a stop verdict held in memory, so a job whose
            # ``stopping`` mark did not confirm durability is still arbitrated as
            # stopped rather than published.
            stopping = current.status == "stopping" or (
                self._closing and current.status in _LIVE_JOB_STATES
            )
            if stopping:
                if intent.compute_ended:
                    # A stop is recorded as confirmed only against proof that the
                    # attempt's compute is over — the owned group shown gone, or
                    # compute that was never spawned.
                    settled = replace(
                        current,
                        status="stopped",
                        report_sha256=None,
                        summary_sha256=None,
                        equity_path_sha256=None,
                        error_code=None,
                        message="the run was stopped before it published a result",
                    )
                else:
                    # The stop is accepted, not confirmed: nothing proved this
                    # attempt's compute had ended, so the job stays stopping and
                    # its staging is kept, never served. An unproven attempt is
                    # never upgraded to a confirmed stop, by a cancellation or by
                    # shutdown.
                    settled = replace(
                        current,
                        status="stopping",
                        error_code=intent.error_code,
                        message=intent.message,
                    )
            elif current.status != "running":
                return current
            elif intent.outcome == "unconfirmed":
                settled = replace(
                    current,
                    status="stopping",
                    error_code=intent.error_code,
                    message=intent.message,
                )
            else:
                settled = self._publish(current, job_id, intent)
            self._store_adopting(settled)
            if settled.status in {"succeeded", "failed"}:
                # Everything worth keeping is now published; a cancellation keeps
                # its staging as diagnostics, and never serves it.
                self._discard_worker_staging(job_id)
            return settled

    def _publish(self, current: JobRecord, job_id: str, intent: _Intent) -> JobRecord:
        """Promote this attempt's staged artifacts and describe the job they make.

        Called only from `_finalize`, under its lock, and only for a job that is
        still running: a promotion failure becomes a failed job rather than a
        published half-attempt.
        """
        try:
            if intent.attempt_staging is not None:
                self._promote(intent.attempt_staging, self.runs_dir, current.attempt_id)
            if intent.report_staging is not None:
                os.rename(intent.report_staging, self.reports_dir / job_id)
                _fsync_directory(self.reports_dir)
        except Exception:
            return replace(
                current,
                status="failed",
                report_sha256=None,
                summary_sha256=None,
                equity_path_sha256=None,
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        return replace(
            current,
            status="succeeded" if intent.outcome == "succeeded" else "failed",
            engine_run_id=intent.engine_run_id,
            report_sha256=intent.report_sha256,
            summary_sha256=intent.summary_sha256,
            equity_path_sha256=intent.equity_path_sha256,
            error_code=intent.error_code,
            message=intent.message,
        )

    def _promote(self, staged: Path, root: Path, name: str | None) -> None:
        """One atomic rename of a staged path into its served root."""
        if name is None:
            raise WebBoundaryError("published identity is missing")
        target = root / name
        if target.exists():
            raise WebBoundaryError("published identity already exists")
        os.rename(staged, target)
        _fsync_directory(root)

    def _verified_staging(
        self, control: Path, result: dict[str, Any], record: JobRecord
    ) -> tuple[Path, Path, dict[str, str]] | None:
        """The staged attempt and report, only when every identity checks out.

        Each staged artifact is re-hashed and the report is re-read here, so a
        torn, replaced or misaddressed staging directory is refused instead of
        promoted. The report schema is checked against the job record that
        admitted this attempt, never against what the worker claims it produced.
        """
        attempt_id = cast(str, record.attempt_id)
        try:
            if result.get("engine_run_id") != attempt_id:
                return None
            attempt = worker.staged_path(control, result.get("attempt_relative"))
            report = worker.staged_path(control, result.get("report_relative"))
            claimed = result.get("artifacts")
            if type(claimed) is not dict or set(claimed) != set(worker.ARTIFACTS):
                return None
            digests: dict[str, str] = {}
            for name in worker.ARTIFACTS:
                payload = read_regular(report / name, report)
                digest = sha256(payload).hexdigest()
                if claimed.get(name) != digest:
                    return None
                digests[name] = digest
            document = json.loads(read_regular(report / "report.json", report))
        except (OSError, ValueError, worker.WorkerError):
            return None
        if (
            type(document) is not dict
            or document.get("schema") != _REPORT_SCHEMAS.get(record.schema, "ea.backtest-report.v1")
            or document.get("run_id") != attempt_id
        ):
            return None
        return attempt, report, digests

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
        path = self._artifact_root(record) / "report.json"
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
        root = self._artifact_root(record)
        if name == "equity-path.json":
            if (
                record.schema
                not in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}
                or record.equity_path_sha256 is None
            ):
                raise ReportUnavailableError("Path analysis unavailable for this legacy run.")
            try:
                payload = read_regular(root / name, root)
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
        path = root / name
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
