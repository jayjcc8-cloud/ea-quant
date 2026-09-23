"""Accepted research bindings and explicit offline loading through the existing engine.

Acceptance is evidence admission, never broker or live execution permission. Inspection
does not execute Python. Runtime loading checks frozen configuration before importing
only the accepted package bytes; normal research entrypoints remain unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ea import __version__
from ea.core.run import RunId
from ea.product.backtest import BacktestRunResult, run_backtest_scenario
from ea.product.offline_demo import _package_code_digest
from ea.product.scenario import (
    BacktestScenarioV2,
    BacktestScenarioV3,
    BacktestScenarioV4,
    BacktestScenarioV5,
    LoadedBacktestScenario,
    _load_document,
    _load_scenario_document,
    _ScenarioInput,
)
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import StrategyPackage, read_regular, validate_package
from ea.strategy.registry import project_parameters
from ea.web.candidate_evidence import CandidateEvidenceReader
from ea.web.candidates import _digest as require_digest
from ea.web.candidates import _uuid, canonical

_BINDING_SEAL = object()
_CONFIGURATION_FIELDS = (
    "schema_version",
    "strategy",
    "instrument",
    "funding",
    "risk",
    "execution",
    "randomness_profile",
)


def _configuration(document: dict[str, Any]) -> bytes:
    return canonical({field: document[field] for field in _CONFIGURATION_FIELDS})


def _digest(domain: str, payload: bytes) -> str:
    return sha256(domain.encode("ascii") + b"\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class AcceptedCandidateBinding:
    """Immutable evidence-admitted identity; construction belongs to this loader."""

    workspace: Path
    candidate_id: str
    fingerprint: str
    strategy_id: str
    artifact_sha256: str | None
    parameters_sha256: str
    configuration_sha256: str
    strategy_package: StrategyPackage | None
    _record_bytes: bytes
    _configuration_bytes: bytes
    _artifact_bytes: bytes | None
    _seal: object

    def __init__(self) -> None:
        raise TypeError("accepted bindings are created only by verified candidate loading")

    def document(self) -> dict[str, Any]:
        record = json.loads(self._record_bytes)
        return {
            "schema": "ea.accepted-candidate-binding.v1",
            "candidate_id": self.candidate_id,
            "fingerprint": self.fingerprint,
            "strategy_id": self.strategy_id,
            "strategy_version": record["projection"]["strategy"]["version"],
            "artifact_sha256": self.artifact_sha256,
            "parameters_sha256": self.parameters_sha256,
            "configuration_sha256": self.configuration_sha256,
            "source_scenario_sha256": record["projection"]["source"]["scenario_sha256"],
            "source_data_sha256": record["projection"]["source"]["data_sha256"],
            "implementation": record["projection"]["strategy"]["implementation"],
            "execution_scope": "offline-backtest-only",
        }


def _binding(
    workspace: Path,
    record: dict[str, Any],
    scenario: dict[str, Any],
    artifact: bytes | None,
    package: StrategyPackage | None = None,
) -> AcceptedCandidateBinding:
    strategy = record["projection"]["strategy"]
    configuration = _configuration(scenario)
    values = {
        "workspace": workspace,
        "candidate_id": record["candidate_id"],
        "fingerprint": record["fingerprint"],
        "strategy_id": strategy["id"],
        "artifact_sha256": strategy["implementation"].get("artifact_sha256"),
        "parameters_sha256": _digest(
            "ea.candidate-parameters.v1", canonical(strategy["parameters"])
        ),
        "configuration_sha256": _digest("ea.candidate-configuration.v1", configuration),
        "strategy_package": package,
        "_record_bytes": canonical(record),
        "_configuration_bytes": configuration,
        "_artifact_bytes": artifact,
        "_seal": _BINDING_SEAL,
    }
    result = object.__new__(AcceptedCandidateBinding)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


def inspect_candidate_binding(workspace: Path, candidate_id: str) -> AcceptedCandidateBinding:
    """Freshly verify ACCEPTED evidence and frozen bytes without executing strategy code."""
    reader = CandidateEvidenceReader(workspace)
    record = reader.record(candidate_id)
    if record["status"] != "ACCEPTED":
        raise ValueError("candidate must have an explicit ACCEPTED research decision")
    source_id = record["projection"]["source"]["job_id"]
    evidence, strategy, scenario = reader.job(source_id)
    if evidence != record["projection"]["source"] or strategy != record["projection"]["strategy"]:
        raise ValueError("candidate source changed during inspection")
    artifact = None
    if record["projection"]["strategy"]["implementation"]["kind"] == "local":
        artifact = reader.read(reader.inputs_dir / f"{source_id}.eastrategy")
        if (
            sha256(artifact).hexdigest()
            != record["projection"]["strategy"]["implementation"]["artifact_sha256"]
        ):
            raise ValueError("candidate artifact changed during inspection")
    return _binding(workspace, record, scenario, artifact)


def load_accepted_candidate(
    workspace: Path,
    candidate_id: str,
    *,
    expected_artifact_sha256: str | None = None,
    expected_configuration_sha256: str | None = None,
    expected_parameters_sha256: str | None = None,
) -> AcceptedCandidateBinding:
    """Check every requested identity before executable accepted package validation."""
    binding = inspect_candidate_binding(workspace, candidate_id)
    for expected, actual in (
        (expected_artifact_sha256, binding.artifact_sha256),
        (expected_configuration_sha256, binding.configuration_sha256),
        (expected_parameters_sha256, binding.parameters_sha256),
    ):
        if expected is not None and expected != actual:
            raise ValueError("accepted candidate loading identity conflicts")
    implementation = binding.document()["implementation"]
    if implementation["code_sha256"] != _package_code_digest().value or implementation[
        "distribution"
    ] != {"name": "ea-quant", "version": __version__}:
        raise ValueError("accepted candidate requires its recorded EA code and distribution")
    if binding._artifact_bytes is not None:
        package = validate_package(binding._artifact_bytes, expected_sha256=binding.artifact_sha256)
        record = json.loads(binding._record_bytes)
        strategy = record["projection"]["strategy"]
        if (
            package.identity.package_id != strategy["implementation"]["package_id"]
            or package.descriptor.strategy_id != strategy["id"]
            or package.descriptor.strategy_version != strategy["version"]
        ):
            raise ValueError("accepted package descriptor conflicts with candidate")
        return _binding(
            workspace,
            record,
            json.loads(binding._configuration_bytes),
            binding._artifact_bytes,
            package,
        )
    return binding


def _require_binding(binding: AcceptedCandidateBinding) -> None:
    if type(binding) is not AcceptedCandidateBinding or binding._seal is not _BINDING_SEAL:
        raise ValueError("runtime requires a verified accepted candidate binding")
    fresh = inspect_candidate_binding(binding.workspace, binding.candidate_id)
    if fresh._record_bytes != binding._record_bytes or fresh.document() != binding.document():
        raise ValueError("accepted candidate changed before runtime loading")


def _preflight(path: Path) -> dict[str, Any]:
    """Decode strict schema/defaults without consulting or executing any package."""
    raw = _load_document(path)
    models: dict[int, Any] = {
        1: _ScenarioInput,
        2: BacktestScenarioV2,
        3: BacktestScenarioV3,
        4: BacktestScenarioV4,
        5: BacktestScenarioV5,
    }
    version = raw.get("schema_version")
    if type(version) is not int or version not in models:
        raise ValueError("runtime scenario version is unsupported")
    document: dict[str, Any] = models[version].model_validate(raw).model_dump(mode="json")
    for field in ("commission", "slippage", "latency"):
        if document["execution"][field] is None:
            document["execution"].pop(field)
    if version in (4, 5) and document["strategy"].get("source") is None:
        document["strategy"].pop("source")
    return document


def run_accepted_candidate(
    binding: AcceptedCandidateBinding, scenario_path: Path, output_root: Path
) -> BacktestRunResult:
    """Run one explicit offline scenario using only its accepted immutable configuration."""
    _require_binding(binding)
    path = scenario_path.resolve(strict=True)
    document = _preflight(path)
    if _configuration(document) != binding._configuration_bytes:
        raise ValueError("runtime scenario differs from accepted normalized configuration")
    loaded = load_accepted_candidate(
        binding.workspace,
        binding.candidate_id,
        expected_artifact_sha256=binding.artifact_sha256,
        expected_configuration_sha256=binding.configuration_sha256,
        expected_parameters_sha256=binding.parameters_sha256,
    )
    catalog = ResearchStrategyCatalogV1(
        () if loaded.strategy_package is None else (loaded.strategy_package,)
    )
    scenario = _load_scenario_document(path, document, catalog=catalog)
    return run_backtest_scenario(scenario, output_root, _candidate_binding=loaded)


def candidate_run_document(
    binding: AcceptedCandidateBinding, scenario: LoadedBacktestScenario, run_id: RunId
) -> bytes:
    """Recheck immutable attribution before the existing engine creates economic evidence."""
    _require_binding(binding)
    if _configuration(json.loads(scenario.canonical_bytes)) != binding._configuration_bytes:
        raise ValueError("loaded scenario configuration differs from accepted candidate")
    artifact = (
        None if scenario.strategy_package is None else scenario.strategy_package.artifact_bytes
    )
    if artifact != binding._artifact_bytes:
        raise ValueError("loaded scenario artifact differs from accepted candidate")
    return canonical(
        {
            **binding.document(),
            "schema": "ea.candidate-run-binding.v1",
            "run_id": run_id.value,
            "scenario_sha256": scenario.scenario_sha256.value,
            "data_sha256": scenario.dataset.selection.fingerprint.sha256.value,
        }
    )


def retained_candidate_id(
    attempt: Path, scenario: LoadedBacktestScenario, run_id: RunId
) -> str | None:
    """Validate optional recorded attribution for resume without changing any evidence."""
    path = attempt / "candidate-binding.json"
    if not path.exists() and not path.is_symlink():
        return None
    payload = read_regular(path, attempt)
    document = json.loads(payload)
    if (
        type(document) is not dict
        or set(document)
        != {
            "schema",
            "candidate_id",
            "fingerprint",
            "strategy_id",
            "strategy_version",
            "artifact_sha256",
            "parameters_sha256",
            "configuration_sha256",
            "source_scenario_sha256",
            "source_data_sha256",
            "implementation",
            "execution_scope",
            "run_id",
            "scenario_sha256",
            "data_sha256",
        }
        or canonical(document) != payload
    ):
        raise ValueError("candidate runtime attribution is invalid")
    _uuid(document["candidate_id"])
    for key in (
        "fingerprint",
        "parameters_sha256",
        "configuration_sha256",
        "source_scenario_sha256",
        "source_data_sha256",
        "scenario_sha256",
        "data_sha256",
    ):
        require_digest(document[key])
    scenario_document = json.loads(scenario.canonical_bytes)
    strategy = scenario_document["strategy"]
    parameters = strategy.get("parameters")
    if parameters is None:
        parameters = project_parameters(strategy)
    artifact = (
        None
        if scenario.strategy_package is None
        else scenario.strategy_package.identity.artifact_sha256
    )
    if (
        document["schema"] != "ea.candidate-run-binding.v1"
        or document["execution_scope"] != "offline-backtest-only"
        or document["run_id"] != run_id.value
        or document["scenario_sha256"] != scenario.scenario_sha256.value
        or document["data_sha256"] != scenario.dataset.selection.fingerprint.sha256.value
        or document["strategy_id"] != strategy["id"]
        or document["strategy_version"] != strategy.get("version", 1)
        or document["artifact_sha256"] != artifact
        or document["parameters_sha256"]
        != _digest("ea.candidate-parameters.v1", canonical(parameters))
        or document["configuration_sha256"]
        != _digest("ea.candidate-configuration.v1", _configuration(scenario_document))
    ):
        raise ValueError("candidate runtime attribution conflicts with the persisted scenario")
    candidate_id: str = document["candidate_id"]
    return candidate_id
