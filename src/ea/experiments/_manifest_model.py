"""Typed run-manifest models and pure lineage builders."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from ea.core.run import (
    DataFingerprint,
    ReplayWindow,
    RunContractError,
    RunId,
    RunReference,
    Sha256Digest,
    validate_stream_label,
)
from ea.experiments._manifest_wire import (
    CONFIG_DOMAIN,
    LINEAGE_DOMAIN,
    LOCK_DOMAIN,
    digest,
    normalized_configuration_bytes,
)
from ea.experiments._manifest_wire import (
    canonical_lineage_bytes as _canonical_lineage_bytes,
)
from ea.experiments._manifest_wire import (
    canonical_manifest_bytes as _canonical_manifest_bytes,
)

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z", flags=re.ASCII)
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z", flags=re.ASCII)
_NORMALIZED_DISTRIBUTION_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?\Z",
    flags=re.ASCII,
)
_PEP503_SEPARATOR_PATTERN = re.compile(r"[-_.]+")
_RUNTIME_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z",
    flags=re.ASCII,
)
_MAX_INT64 = (1 << 63) - 1
_MIN_INT64 = -(1 << 63)
_MAX_UINT64 = (1 << 64) - 1
_NUMERIC_POLICY = "deterministic-ordered-float64-v1"
_GENERATOR = "numpy-pcg64"
_STREAM_DERIVATION = "ea-sha256-component-label-v1"
_ENVIRONMENTS = frozenset({"development", "staging", "production"})


class ManifestError(RunContractError):
    """Base error for manifest construction and verification."""


class ManifestFormatError(ManifestError):
    """Raised when persisted bytes are not the exact canonical closed schema."""


class EvidenceMismatchError(ManifestError):
    """Raised when external evidence does not reproduce the manifest."""


def _require_nfc_text(
    value: object,
    *,
    field: str,
    minimum: int = 1,
    maximum: int = 128,
) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise ManifestError(f"{field} must be a {minimum}..{maximum} character string")
    if unicodedata.normalize("NFC", value) != value:
        raise ManifestError(f"{field} must already be NFC")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise ManifestError(f"{field} cannot contain control or surrogate code points")
    return value


def _require_runtime_token(value: object, *, field: str) -> str:
    if type(value) is not str or _RUNTIME_TOKEN_PATTERN.fullmatch(value) is None:
        raise ManifestError(f"{field} must be a canonical runtime token")
    return value


@dataclass(frozen=True, slots=True)
class NormalizedConfiguration:
    """Closed normalized ADR 0005 values accepted by manifest schema v1."""

    schema_version: int
    environment: str
    mode: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ManifestError("normalized schema_version must be the integer 1")
        if type(self.environment) is not str or self.environment not in _ENVIRONMENTS:
            raise ManifestError("normalized environment is unsupported")
        if type(self.mode) is not str or self.mode != "backtest":
            raise ManifestError("manifest v1 accepts only run.mode backtest")


@dataclass(frozen=True, slots=True)
class CodeEvidence:
    """Successful clean-repository evidence supplied by the Git collector."""

    commit: str

    def __post_init__(self) -> None:
        if type(self.commit) is not str or _COMMIT_PATTERN.fullmatch(self.commit) is None:
            raise ManifestError("Git commit must be 40 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class DistributionIdentity:
    """PEP 503-normalized installed distribution name and exact version."""

    name: str
    version: str

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or _NORMALIZED_DISTRIBUTION_PATTERN.fullmatch(self.name) is None
            or _PEP503_SEPARATOR_PATTERN.sub("-", self.name).lower() != self.name
        ):
            raise ManifestError("distribution name must be PEP 503 normalized")
        _require_nfc_text(self.version, field=f"distribution {self.name} version")


def _validated_distributions(
    values: Sequence[DistributionIdentity],
) -> tuple[DistributionIdentity, ...]:
    if type(values) is not tuple:
        raise ManifestError("distributions must be an immutable tuple")
    if any(type(value) is not DistributionIdentity for value in values):
        raise ManifestError("distributions contain an invalid identity")
    names = [value.name for value in values]
    if len(names) != len(set(names)):
        raise ManifestError("normalized distribution names must be unique")
    return tuple(sorted(values, key=lambda value: value.name))


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    """Collected runtime tokens and exact lock bytes before digest derivation."""

    ea_version: str
    python_implementation: str
    python_version: str
    python_cache_tag: str
    sys_platform: str
    platform_tag: str
    distributions: tuple[DistributionIdentity, ...]
    uv_lock_bytes: bytes

    def __post_init__(self) -> None:
        version = _require_nfc_text(self.ea_version, field="ea_version")
        for field in (
            "python_implementation",
            "python_version",
            "python_cache_tag",
            "sys_platform",
            "platform_tag",
        ):
            _require_runtime_token(getattr(self, field), field=field)
        distributions = _validated_distributions(self.distributions)
        matches = [item for item in distributions if item.name == "ea-quant"]
        if len(matches) != 1 or matches[0].version != version:
            raise ManifestError("runtime must contain one ea-quant matching ea_version")
        if type(self.uv_lock_bytes) is not bytes:
            raise ManifestError("uv_lock_bytes must be exact bytes")
        object.__setattr__(self, "distributions", distributions)


class ParameterKind(StrEnum):
    BOOLEAN = "boolean"
    FLOAT64 = "float64"
    INTEGER = "integer"
    STRING = "string"


@dataclass(frozen=True, slots=True)
class EffectiveParameter:
    """One closed, explicitly tagged effective component parameter."""

    name: str
    kind: ParameterKind
    value: bool | float | int | str

    def __post_init__(self) -> None:
        if type(self.name) is not str or _NAME_PATTERN.fullmatch(self.name) is None:
            raise ManifestError("parameter name must match [a-z][a-z0-9_.-]{0,127}")
        if type(self.kind) is not ParameterKind:
            raise ManifestError("parameter kind must use the closed v1 vocabulary")
        if self.kind is ParameterKind.BOOLEAN:
            if type(self.value) is not bool:
                raise ManifestError("boolean parameter must contain an exact bool")
        elif self.kind is ParameterKind.INTEGER:
            if type(self.value) is not int or self.value < _MIN_INT64 or self.value > _MAX_INT64:
                raise ManifestError("integer parameter must be a signed 64-bit int")
        elif self.kind is ParameterKind.FLOAT64:
            if type(self.value) is not float or not isfinite(self.value):
                raise ManifestError("float64 parameter must be an exact finite float")
        elif self.kind is ParameterKind.STRING:
            _require_nfc_text(self.value, field=f"parameter {self.name}", maximum=256)

    @classmethod
    def from_value(cls, name: str, value: bool | float | int | str) -> EffectiveParameter:
        """Derive the only valid explicit type tag without coercion."""
        if type(value) is bool:
            kind = ParameterKind.BOOLEAN
        elif type(value) is int:
            kind = ParameterKind.INTEGER
        elif type(value) is float:
            kind = ParameterKind.FLOAT64
        elif type(value) is str:
            kind = ParameterKind.STRING
        else:
            raise ManifestError("parameter value type is unsupported")
        return cls(name=name, kind=kind, value=value)


@dataclass(frozen=True, slots=True)
class LineageInputs:
    """Typed, non-derived evidence consumed by the pure lineage builder."""

    code: CodeEvidence
    configuration: NormalizedConfiguration
    data: DataFingerprint
    replay_window: ReplayWindow
    parameters: tuple[EffectiveParameter, ...]
    runtime: RuntimeEvidence
    master_seed: int
    stream_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.code) is not CodeEvidence:
            raise ManifestError("code must be CodeEvidence")
        if type(self.configuration) is not NormalizedConfiguration:
            raise ManifestError("configuration must be NormalizedConfiguration")
        if type(self.data) is not DataFingerprint:
            raise ManifestError("data must be DataFingerprint")
        if type(self.replay_window) is not ReplayWindow:
            raise ManifestError("replay_window must be ReplayWindow")
        if type(self.parameters) is not tuple or any(
            type(item) is not EffectiveParameter for item in self.parameters
        ):
            raise ManifestError("parameters must be an immutable parameter tuple")
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ManifestError("parameter names must be unique")
        if type(self.runtime) is not RuntimeEvidence:
            raise ManifestError("runtime must be RuntimeEvidence")
        if (
            type(self.master_seed) is not int
            or self.master_seed < 0
            or self.master_seed > _MAX_UINT64
        ):
            raise ManifestError("master_seed must be an unsigned 64-bit integer")
        if type(self.stream_labels) is not tuple:
            raise ManifestError("stream_labels must be an immutable tuple")
        labels = [validate_stream_label(label) for label in self.stream_labels]
        if len(labels) != len(set(labels)):
            raise ManifestError("stream labels must be unique")


@dataclass(frozen=True, slots=True)
class CodeSpec:
    commit: str
    worktree_clean: bool = True

    def __post_init__(self) -> None:
        CodeEvidence(self.commit)
        if type(self.worktree_clean) is not bool or not self.worktree_clean:
            raise ManifestError("worktree_clean must be the literal true")


@dataclass(frozen=True, slots=True)
class ConfigurationSpec:
    normalized: NormalizedConfiguration
    sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.normalized) is not NormalizedConfiguration:
            raise ManifestError("configuration normalized value is invalid")
        if type(self.sha256) is not Sha256Digest:
            raise ManifestError("configuration sha256 is invalid")
        expected = digest(CONFIG_DOMAIN, normalized_configuration_bytes(self.normalized))
        if self.sha256 != expected:
            raise ManifestError("configuration digest does not match normalized settings")


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    ea_version: str
    python_implementation: str
    python_version: str
    python_cache_tag: str
    sys_platform: str
    platform_tag: str
    distributions: tuple[DistributionIdentity, ...]
    uv_lock_sha256: Sha256Digest
    numeric_policy: str = _NUMERIC_POLICY

    def __post_init__(self) -> None:
        evidence = RuntimeEvidence(
            ea_version=self.ea_version,
            python_implementation=self.python_implementation,
            python_version=self.python_version,
            python_cache_tag=self.python_cache_tag,
            sys_platform=self.sys_platform,
            platform_tag=self.platform_tag,
            distributions=self.distributions,
            uv_lock_bytes=b"",
        )
        object.__setattr__(self, "distributions", evidence.distributions)
        if type(self.uv_lock_sha256) is not Sha256Digest:
            raise ManifestError("uv_lock_sha256 is invalid")
        if type(self.numeric_policy) is not str or self.numeric_policy != _NUMERIC_POLICY:
            raise ManifestError("numeric_policy is unsupported")


@dataclass(frozen=True, slots=True)
class RandomnessSpec:
    master_seed: int
    stream_labels: tuple[str, ...]
    generator: str = _GENERATOR
    stream_derivation: str = _STREAM_DERIVATION

    def __post_init__(self) -> None:
        if (
            type(self.master_seed) is not int
            or self.master_seed < 0
            or self.master_seed > _MAX_UINT64
        ):
            raise ManifestError("master_seed must be an unsigned 64-bit integer")
        if type(self.stream_labels) is not tuple:
            raise ManifestError("stream_labels must be an immutable tuple")
        labels = tuple(validate_stream_label(label) for label in self.stream_labels)
        if labels != tuple(sorted(labels)) or len(labels) != len(set(labels)):
            raise ManifestError("stream labels must be sorted and unique")
        if type(self.generator) is not str or self.generator != _GENERATOR:
            raise ManifestError("random generator is unsupported")
        if type(self.stream_derivation) is not str or self.stream_derivation != _STREAM_DERIVATION:
            raise ManifestError("stream derivation is unsupported")


@dataclass(frozen=True, slots=True)
class LineageSpec:
    code: CodeSpec
    configuration: ConfigurationSpec
    data: DataFingerprint
    replay_window: ReplayWindow
    parameters: tuple[EffectiveParameter, ...]
    runtime: RuntimeSpec
    randomness: RandomnessSpec
    lineage_schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.lineage_schema_version) is not int or self.lineage_schema_version != 1:
            raise ManifestError("lineage_schema_version must be the integer 1")
        if type(self.code) is not CodeSpec:
            raise ManifestError("lineage code is invalid")
        if type(self.configuration) is not ConfigurationSpec:
            raise ManifestError("lineage configuration is invalid")
        if type(self.data) is not DataFingerprint:
            raise ManifestError("lineage data is invalid")
        if type(self.replay_window) is not ReplayWindow:
            raise ManifestError("lineage replay_window is invalid")
        if type(self.parameters) is not tuple or any(
            type(item) is not EffectiveParameter for item in self.parameters
        ):
            raise ManifestError("lineage parameters are invalid")
        names = tuple(item.name for item in self.parameters)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ManifestError("lineage parameters must be sorted and unique")
        if type(self.runtime) is not RuntimeSpec:
            raise ManifestError("lineage runtime is invalid")
        if type(self.randomness) is not RandomnessSpec:
            raise ManifestError("lineage randomness is invalid")


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Immutable persisted attempt manifest."""

    run_id: RunId
    lineage_sha256: Sha256Digest
    spec: LineageSpec
    manifest_schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.manifest_schema_version) is not int or self.manifest_schema_version != 1:
            raise ManifestError("manifest_schema_version must be the integer 1")
        if type(self.run_id) is not RunId:
            raise ManifestError("manifest run_id is invalid")
        if type(self.lineage_sha256) is not Sha256Digest:
            raise ManifestError("manifest lineage_sha256 is invalid")
        if type(self.spec) is not LineageSpec:
            raise ManifestError("manifest spec is invalid")
        expected = digest(LINEAGE_DOMAIN, canonical_lineage_bytes(self.spec))
        if self.lineage_sha256 != expected:
            raise ManifestError("lineage digest does not match the canonical specification")

    @property
    def reference(self) -> RunReference:
        return RunReference(run_id=self.run_id, lineage_sha256=self.lineage_sha256)


def canonical_lineage_bytes(spec: LineageSpec) -> bytes:
    """Return exact canonical bytes for the complete lineage specification."""
    if type(spec) is not LineageSpec:
        raise ManifestError("spec must be a LineageSpec")
    return _canonical_lineage_bytes(spec)


def canonical_manifest_bytes(manifest: RunManifest) -> bytes:
    """Return exact persisted manifest bytes with no trailing newline."""
    if type(manifest) is not RunManifest:
        raise ManifestError("manifest must be a RunManifest")
    return _canonical_manifest_bytes(manifest)


def build_lineage_spec(inputs: LineageInputs) -> LineageSpec:
    """Derive all hashes and canonical ordering from typed raw evidence."""
    if type(inputs) is not LineageInputs:
        raise ManifestError("inputs must be LineageInputs")
    normalized_bytes = normalized_configuration_bytes(inputs.configuration)
    configuration = ConfigurationSpec(
        normalized=inputs.configuration,
        sha256=digest(CONFIG_DOMAIN, normalized_bytes),
    )
    runtime = RuntimeSpec(
        ea_version=inputs.runtime.ea_version,
        python_implementation=inputs.runtime.python_implementation,
        python_version=inputs.runtime.python_version,
        python_cache_tag=inputs.runtime.python_cache_tag,
        sys_platform=inputs.runtime.sys_platform,
        platform_tag=inputs.runtime.platform_tag,
        distributions=inputs.runtime.distributions,
        uv_lock_sha256=digest(LOCK_DOMAIN, inputs.runtime.uv_lock_bytes),
    )
    return LineageSpec(
        code=CodeSpec(commit=inputs.code.commit),
        configuration=configuration,
        data=inputs.data,
        replay_window=inputs.replay_window,
        parameters=tuple(sorted(inputs.parameters, key=lambda item: item.name)),
        runtime=runtime,
        randomness=RandomnessSpec(
            master_seed=inputs.master_seed,
            stream_labels=tuple(sorted(inputs.stream_labels)),
        ),
    )


def build_manifest(spec: LineageSpec, run_id: RunId) -> RunManifest:
    """Bind a validated UUID attempt to deterministic lineage."""
    if type(spec) is not LineageSpec:
        raise ManifestError("spec must be a LineageSpec")
    if type(run_id) is not RunId:
        raise ManifestError("run_id must be a RunId")
    return RunManifest(
        run_id=run_id,
        lineage_sha256=digest(LINEAGE_DOMAIN, canonical_lineage_bytes(spec)),
        spec=spec,
    )
