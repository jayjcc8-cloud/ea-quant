"""Strict closed-schema manifest decoder."""

from __future__ import annotations

import json
import re
import struct
from datetime import UTC, datetime
from typing import NoReturn, TypedDict, cast

from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency
from ea.core.execution_messages import ExecutionPolicyId, ExecutionPolicyRef
from ea.core.initial_funding import InitialFundingSpec
from ea.core.risk import RiskPolicyId
from ea.core.run import (
    DataFingerprint,
    ReplayWindow,
    RunContractError,
    RunId,
    Sha256Digest,
)
from ea.experiments._manifest_model import (
    CodeSpec,
    ConfigurationSpec,
    DistributionIdentity,
    EffectiveParameter,
    InstalledRuntimeSpecV2,
    LineageSpec,
    LineageSpecV2,
    ManifestError,
    ManifestFormatError,
    NormalizedConfiguration,
    ParameterKind,
    RandomnessSpec,
    RunManifest,
    RunManifestV2,
    RuntimeSpec,
    canonical_manifest_bytes,
)
from ea.experiments._manifest_wire import (
    CONFIG_CANONICALIZATION,
    DATA_CANONICALIZATION,
    utc_text,
)

_FLOAT_BITS_PATTERN = re.compile(r"[0-9a-f]{16}\Z", flags=re.ASCII)
_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z",
    flags=re.ASCII,
)


class _NormalizedRunWire(TypedDict):
    mode: str


class _NormalizedConfigurationWire(TypedDict):
    environment: str
    run: object
    schema_version: int


class _ParameterWire(TypedDict):
    name: str
    type: str
    value: bool | float | int | str


class _DistributionWire(TypedDict):
    name: str
    version: str


class _LineageWire(TypedDict):
    code: object
    configuration: object
    data: object
    lineage_schema_version: int
    parameters: object
    randomness: object
    replay_window: object
    runtime: object


class _CodeWire(TypedDict):
    commit: str
    worktree_clean: bool


class _ConfigurationWire(TypedDict):
    canonicalization: str
    normalized: object
    sha256: str


class _DataWire(TypedDict):
    canonicalization: str
    record_count: int
    sha256: str


class _ReplayWindowWire(TypedDict):
    end_exclusive: str
    initial_state: str
    selection_field: str
    start_inclusive: str
    timezone: str


class _RandomnessWire(TypedDict):
    generator: str
    master_seed: int
    stream_derivation: str
    stream_labels: object


class _RuntimeWire(TypedDict):
    distributions: object
    ea_version: str
    numeric_policy: str
    platform_tag: str
    python_cache_tag: str
    python_implementation: str
    python_version: str
    sys_platform: str
    uv_lock_sha256: str


class _ManifestWire(TypedDict):
    lineage_sha256: str
    manifest_schema_version: int
    run_id: str
    spec: object


class _LineageV2Wire(TypedDict):
    configuration: object
    data: object
    execution_policy: object
    initial_funding: object
    lineage_schema_version: int
    parameters: object
    randomness: object
    replay_window: object
    risk_policy: object
    runtime: object
    scenario_sha256: str


class _InstalledRuntimeV2Wire(TypedDict):
    distributions: object
    ea_distribution: object
    ea_installed_files_sha256: str
    numeric_policy: str
    platform_tag: str
    provenance_kind: str
    python_cache_tag: str
    python_implementation: str
    python_version: str
    sys_platform: str


class _InitialFundingWire(TypedDict):
    amount: str
    canonicalization: str
    currency_quantum: str
    instrument_spec_set_id: str
    instrument_spec_set_sha256: str
    schema_version: int
    settlement_currency: str


class _ExecutionPolicyWire(TypedDict):
    identifier: str
    sha256: str


class _RiskPolicyWire(TypedDict):
    identifier: str
    sha256: str


def _require_exact_keys(
    value: object,
    expected: frozenset[str],
    *,
    field: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ManifestFormatError(f"{field} must be an object")
    mapping = value
    if frozenset(mapping) != expected:
        raise ManifestFormatError(f"{field} has missing or unknown fields")
    return mapping


def _parse_utc_text(value: object, *, field: str) -> datetime:
    if type(value) is not str or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ManifestFormatError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ManifestFormatError(f"{field} must be a valid UTC timestamp") from exc
    if utc_text(parsed) != value:
        raise ManifestFormatError(f"{field} must be a canonical UTC timestamp")
    return parsed


def _reject_number(value: str) -> NoReturn:
    raise ManifestFormatError(f"non-integer JSON number is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestFormatError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(data: bytes) -> object:
    if type(data) is not bytes:
        raise ManifestFormatError("manifest input must be exact bytes")
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestFormatError("manifest is not valid strict UTF-8 JSON") from exc


def _parse_normalized(value: object) -> NormalizedConfiguration:
    mapping = cast(
        _NormalizedConfigurationWire,
        _require_exact_keys(
            value,
            frozenset({"environment", "run", "schema_version"}),
            field="spec.configuration.normalized",
        ),
    )
    run = cast(
        _NormalizedRunWire,
        _require_exact_keys(
            mapping["run"],
            frozenset({"mode"}),
            field="spec.configuration.normalized.run",
        ),
    )
    try:
        return NormalizedConfiguration(
            schema_version=mapping["schema_version"],
            environment=mapping["environment"],
            mode=run["mode"],
        )
    except ManifestError as exc:
        raise ManifestFormatError(str(exc)) from exc


def _parse_parameter(value: object, *, index: int) -> EffectiveParameter:
    mapping = cast(
        _ParameterWire,
        _require_exact_keys(
            value,
            frozenset({"name", "type", "value"}),
            field=f"spec.parameters[{index}]",
        ),
    )
    raw_kind = mapping["type"]
    if type(raw_kind) is not str:
        raise ManifestFormatError(f"spec.parameters[{index}].type must be a string")
    try:
        kind = ParameterKind(raw_kind)
    except ValueError as exc:
        raise ManifestFormatError(f"spec.parameters[{index}].type is unsupported") from exc
    raw_value = mapping["value"]
    if kind is ParameterKind.FLOAT64:
        if type(raw_value) is not str or _FLOAT_BITS_PATTERN.fullmatch(raw_value) is None:
            raise ManifestFormatError("float64 parameter value must be 16 lowercase hex digits")
        raw_value = struct.unpack(">d", bytes.fromhex(raw_value))[0]
    try:
        return EffectiveParameter(name=mapping["name"], kind=kind, value=raw_value)
    except ManifestError as exc:
        raise ManifestFormatError(str(exc)) from exc


def _parse_distribution(value: object, *, index: int) -> DistributionIdentity:
    mapping = cast(
        _DistributionWire,
        _require_exact_keys(
            value,
            frozenset({"name", "version"}),
            field=f"spec.runtime.distributions[{index}]",
        ),
    )
    try:
        return DistributionIdentity(name=mapping["name"], version=mapping["version"])
    except ManifestError as exc:
        raise ManifestFormatError(str(exc)) from exc


def _parse_lineage(value: object) -> LineageSpec:
    mapping = cast(
        _LineageWire,
        _require_exact_keys(
            value,
            frozenset(
                {
                    "code",
                    "configuration",
                    "data",
                    "lineage_schema_version",
                    "parameters",
                    "randomness",
                    "replay_window",
                    "runtime",
                }
            ),
            field="spec",
        ),
    )
    code = cast(
        _CodeWire,
        _require_exact_keys(
            mapping["code"],
            frozenset({"commit", "worktree_clean"}),
            field="spec.code",
        ),
    )
    configuration = cast(
        _ConfigurationWire,
        _require_exact_keys(
            mapping["configuration"],
            frozenset({"canonicalization", "normalized", "sha256"}),
            field="spec.configuration",
        ),
    )
    data = cast(
        _DataWire,
        _require_exact_keys(
            mapping["data"],
            frozenset({"canonicalization", "record_count", "sha256"}),
            field="spec.data",
        ),
    )
    replay = cast(
        _ReplayWindowWire,
        _require_exact_keys(
            mapping["replay_window"],
            frozenset(
                {
                    "end_exclusive",
                    "initial_state",
                    "selection_field",
                    "start_inclusive",
                    "timezone",
                }
            ),
            field="spec.replay_window",
        ),
    )
    randomness = cast(
        _RandomnessWire,
        _require_exact_keys(
            mapping["randomness"],
            frozenset({"generator", "master_seed", "stream_derivation", "stream_labels"}),
            field="spec.randomness",
        ),
    )
    runtime = cast(
        _RuntimeWire,
        _require_exact_keys(
            mapping["runtime"],
            frozenset(
                {
                    "distributions",
                    "ea_version",
                    "numeric_policy",
                    "platform_tag",
                    "python_cache_tag",
                    "python_implementation",
                    "python_version",
                    "sys_platform",
                    "uv_lock_sha256",
                }
            ),
            field="spec.runtime",
        ),
    )
    if configuration["canonicalization"] != CONFIG_CANONICALIZATION:
        raise ManifestFormatError("configuration canonicalization is unsupported")
    if data["canonicalization"] != DATA_CANONICALIZATION:
        raise ManifestFormatError("data canonicalization is unsupported")
    if (
        replay["timezone"] != "UTC"
        or replay["selection_field"] != "available_at"
        or replay["initial_state"] != "empty"
    ):
        raise ManifestFormatError("replay window fixed vocabulary is invalid")
    raw_parameters = mapping["parameters"]
    raw_labels = randomness["stream_labels"]
    raw_distributions = runtime["distributions"]
    if type(raw_parameters) is not list:
        raise ManifestFormatError("spec.parameters must be an array")
    if type(raw_labels) is not list or any(type(label) is not str for label in raw_labels):
        raise ManifestFormatError("spec.randomness.stream_labels must be a string array")
    if type(raw_distributions) is not list:
        raise ManifestFormatError("spec.runtime.distributions must be an array")
    parameters = cast(list[object], raw_parameters)
    labels = cast(list[str], raw_labels)
    distributions = cast(list[object], raw_distributions)
    try:
        spec = LineageSpec(
            lineage_schema_version=mapping["lineage_schema_version"],
            code=CodeSpec(
                commit=code["commit"],
                worktree_clean=code["worktree_clean"],
            ),
            configuration=ConfigurationSpec(
                normalized=_parse_normalized(configuration["normalized"]),
                sha256=Sha256Digest(configuration["sha256"]),
            ),
            data=DataFingerprint(
                sha256=Sha256Digest(data["sha256"]),
                record_count=data["record_count"],
            ),
            replay_window=ReplayWindow(
                start_inclusive=_parse_utc_text(
                    replay["start_inclusive"],
                    field="spec.replay_window.start_inclusive",
                ),
                end_exclusive=_parse_utc_text(
                    replay["end_exclusive"],
                    field="spec.replay_window.end_exclusive",
                ),
            ),
            parameters=tuple(
                _parse_parameter(item, index=index) for index, item in enumerate(parameters)
            ),
            randomness=RandomnessSpec(
                master_seed=randomness["master_seed"],
                stream_labels=tuple(labels),
                generator=randomness["generator"],
                stream_derivation=randomness["stream_derivation"],
            ),
            runtime=RuntimeSpec(
                ea_version=runtime["ea_version"],
                python_implementation=runtime["python_implementation"],
                python_version=runtime["python_version"],
                python_cache_tag=runtime["python_cache_tag"],
                sys_platform=runtime["sys_platform"],
                platform_tag=runtime["platform_tag"],
                distributions=tuple(
                    _parse_distribution(item, index=index)
                    for index, item in enumerate(distributions)
                ),
                uv_lock_sha256=Sha256Digest(runtime["uv_lock_sha256"]),
                numeric_policy=runtime["numeric_policy"],
            ),
        )
    except RunContractError as exc:
        raise ManifestFormatError(str(exc)) from exc
    return spec


def _parse_lineage_v2(value: object) -> LineageSpecV2:
    mapping = cast(
        _LineageV2Wire,
        _require_exact_keys(
            value,
            frozenset(
                {
                    "configuration",
                    "data",
                    "execution_policy",
                    "initial_funding",
                    "lineage_schema_version",
                    "parameters",
                    "randomness",
                    "replay_window",
                    "risk_policy",
                    "runtime",
                    "scenario_sha256",
                }
            ),
            field="spec",
        ),
    )
    configuration = cast(
        _ConfigurationWire,
        _require_exact_keys(
            mapping["configuration"],
            frozenset({"canonicalization", "normalized", "sha256"}),
            field="spec.configuration",
        ),
    )
    data = cast(
        _DataWire,
        _require_exact_keys(
            mapping["data"],
            frozenset({"canonicalization", "record_count", "sha256"}),
            field="spec.data",
        ),
    )
    replay = cast(
        _ReplayWindowWire,
        _require_exact_keys(
            mapping["replay_window"],
            frozenset(
                {"end_exclusive", "initial_state", "selection_field", "start_inclusive", "timezone"}
            ),
            field="spec.replay_window",
        ),
    )
    randomness = cast(
        _RandomnessWire,
        _require_exact_keys(
            mapping["randomness"],
            frozenset({"generator", "master_seed", "stream_derivation", "stream_labels"}),
            field="spec.randomness",
        ),
    )
    runtime = cast(
        _InstalledRuntimeV2Wire,
        _require_exact_keys(
            mapping["runtime"],
            frozenset(
                {
                    "distributions",
                    "ea_distribution",
                    "ea_installed_files_sha256",
                    "numeric_policy",
                    "platform_tag",
                    "provenance_kind",
                    "python_cache_tag",
                    "python_implementation",
                    "python_version",
                    "sys_platform",
                }
            ),
            field="spec.runtime",
        ),
    )
    funding = cast(
        _InitialFundingWire,
        _require_exact_keys(
            mapping["initial_funding"],
            frozenset(
                {
                    "amount",
                    "canonicalization",
                    "currency_quantum",
                    "instrument_spec_set_id",
                    "instrument_spec_set_sha256",
                    "schema_version",
                    "settlement_currency",
                }
            ),
            field="spec.initial_funding",
        ),
    )
    execution = cast(
        _ExecutionPolicyWire,
        _require_exact_keys(
            mapping["execution_policy"],
            frozenset({"identifier", "sha256"}),
            field="spec.execution_policy",
        ),
    )
    risk = cast(
        _RiskPolicyWire,
        _require_exact_keys(
            mapping["risk_policy"],
            frozenset({"identifier", "sha256"}),
            field="spec.risk_policy",
        ),
    )
    raw_parameters = mapping["parameters"]
    raw_labels = randomness["stream_labels"]
    raw_distributions = runtime["distributions"]
    if type(raw_parameters) is not list:
        raise ManifestFormatError("spec.parameters must be an array")
    if type(raw_labels) is not list or any(type(label) is not str for label in raw_labels):
        raise ManifestFormatError("spec.randomness.stream_labels must be a string array")
    if type(raw_distributions) is not list:
        raise ManifestFormatError("spec.runtime.distributions must be an array")
    if configuration["canonicalization"] != CONFIG_CANONICALIZATION:
        raise ManifestFormatError("configuration canonicalization is unsupported")
    if data["canonicalization"] != DATA_CANONICALIZATION:
        raise ManifestFormatError("data canonicalization is unsupported")
    if (
        replay["timezone"] != "UTC"
        or replay["selection_field"] != "available_at"
        or replay["initial_state"] != "empty"
    ):
        raise ManifestFormatError("replay window fixed vocabulary is invalid")
    if (
        funding["canonicalization"] != "ea-initial-funding-spec-v1"
        or funding["schema_version"] != 1
    ):
        raise ManifestFormatError("initial funding canonicalization is unsupported")
    try:
        return LineageSpecV2(
            configuration=ConfigurationSpec(
                normalized=_parse_normalized(configuration["normalized"]),
                sha256=Sha256Digest(configuration["sha256"]),
            ),
            data=DataFingerprint(Sha256Digest(data["sha256"]), data["record_count"]),
            replay_window=ReplayWindow(
                _parse_utc_text(
                    replay["start_inclusive"], field="spec.replay_window.start_inclusive"
                ),
                _parse_utc_text(replay["end_exclusive"], field="spec.replay_window.end_exclusive"),
            ),
            parameters=tuple(
                _parse_parameter(item, index=index)
                for index, item in enumerate(cast(list[object], raw_parameters))
            ),
            runtime=InstalledRuntimeSpecV2(
                ea_distribution=_parse_distribution(runtime["ea_distribution"], index=0),
                ea_installed_files_sha256=Sha256Digest(runtime["ea_installed_files_sha256"]),
                python_implementation=runtime["python_implementation"],
                python_version=runtime["python_version"],
                python_cache_tag=runtime["python_cache_tag"],
                sys_platform=runtime["sys_platform"],
                platform_tag=runtime["platform_tag"],
                distributions=tuple(
                    _parse_distribution(item, index=index)
                    for index, item in enumerate(cast(list[object], raw_distributions))
                ),
                numeric_policy=runtime["numeric_policy"],
                provenance_kind=runtime["provenance_kind"],
            ),
            randomness=RandomnessSpec(
                randomness["master_seed"],
                tuple(cast(list[str], raw_labels)),
                generator=randomness["generator"],
                stream_derivation=randomness["stream_derivation"],
            ),
            scenario_sha256=Sha256Digest(mapping["scenario_sha256"]),
            initial_funding=InitialFundingSpec(
                InstrumentSpecSetId(funding["instrument_spec_set_id"]),
                Sha256Digest(funding["instrument_spec_set_sha256"]),
                SettlementCurrency(funding["settlement_currency"]),
                CanonicalDecimal(funding["currency_quantum"]),
                CanonicalDecimal(funding["amount"]),
            ),
            execution_policy=ExecutionPolicyRef(
                ExecutionPolicyId(execution["identifier"]), Sha256Digest(execution["sha256"])
            ),
            risk_policy_id=RiskPolicyId(risk["identifier"]),
            risk_policy_sha256=Sha256Digest(risk["sha256"]),
        )
    except (RunContractError, ValueError) as exc:
        raise ManifestFormatError(str(exc)) from exc


def read_manifest_v1(data: bytes) -> RunManifest:
    """Strictly parse, validate hashes, and require canonical byte-for-byte form."""
    root = cast(
        _ManifestWire,
        _require_exact_keys(
            _strict_json_loads(data),
            frozenset({"lineage_sha256", "manifest_schema_version", "run_id", "spec"}),
            field="manifest",
        ),
    )
    try:
        manifest = RunManifest(
            manifest_schema_version=root["manifest_schema_version"],
            run_id=RunId(root["run_id"]),
            lineage_sha256=Sha256Digest(root["lineage_sha256"]),
            spec=_parse_lineage(root["spec"]),
        )
    except RunContractError as exc:
        raise ManifestFormatError(str(exc)) from exc
    if canonical_manifest_bytes(manifest) != data:
        raise ManifestFormatError("manifest bytes are valid JSON but not canonical")
    return manifest


def read_manifest_v2(data: bytes) -> RunManifestV2:
    root = cast(
        _ManifestWire,
        _require_exact_keys(
            _strict_json_loads(data),
            frozenset({"lineage_sha256", "manifest_schema_version", "run_id", "spec"}),
            field="manifest",
        ),
    )
    try:
        manifest = RunManifestV2(
            manifest_schema_version=root["manifest_schema_version"],
            run_id=RunId(root["run_id"]),
            lineage_sha256=Sha256Digest(root["lineage_sha256"]),
            spec=_parse_lineage_v2(root["spec"]),
        )
    except RunContractError as exc:
        raise ManifestFormatError(str(exc)) from exc
    if canonical_manifest_bytes(manifest) != data:
        raise ManifestFormatError("manifest bytes are valid JSON but not canonical")
    return manifest


def read_manifest(data: bytes) -> RunManifest | RunManifestV2:
    root = _require_exact_keys(
        _strict_json_loads(data),
        frozenset({"lineage_sha256", "manifest_schema_version", "run_id", "spec"}),
        field="manifest",
    )
    if root["manifest_schema_version"] == 1:
        return read_manifest_v1(data)
    if root["manifest_schema_version"] == 2:
        return read_manifest_v2(data)
    raise ManifestFormatError("manifest_schema_version is unsupported")
