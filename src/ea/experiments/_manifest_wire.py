"""Canonical manifest wire projection and domain-separated digest primitives."""

from __future__ import annotations

import json
import struct
from datetime import datetime
from hashlib import sha256
from typing import Protocol, cast

from ea.core.run import DataFingerprint, ReplayWindow, RunId, Sha256Digest

CONFIG_DOMAIN = b"ea.config.v1\0"
LOCK_DOMAIN = b"ea.uv-lock.v1\0"
LINEAGE_DOMAIN = b"ea.run-spec.v1\0"
CONFIG_CANONICALIZATION = "ea-settings-v1"
DATA_CANONICALIZATION = "ea-market-data-envelope-v1"


class _ParameterKindLike(Protocol):
    value: str


class _NormalizedConfigurationLike(Protocol):
    schema_version: int
    environment: str
    mode: str


class _CodeLike(Protocol):
    commit: str
    worktree_clean: bool


class _ConfigurationLike(Protocol):
    normalized: _NormalizedConfigurationLike
    sha256: Sha256Digest


class _EffectiveParameterLike(Protocol):
    name: str
    kind: _ParameterKindLike
    value: bool | float | int | str


class _DistributionLike(Protocol):
    name: str
    version: str


class _RandomnessLike(Protocol):
    master_seed: int
    stream_labels: tuple[str, ...]
    generator: str
    stream_derivation: str


class _RuntimeLike(Protocol):
    ea_version: str
    python_implementation: str
    python_version: str
    python_cache_tag: str
    sys_platform: str
    platform_tag: str
    distributions: tuple[_DistributionLike, ...]
    uv_lock_sha256: Sha256Digest
    numeric_policy: str


class _LineageLike(Protocol):
    code: _CodeLike
    configuration: _ConfigurationLike
    data: DataFingerprint
    replay_window: ReplayWindow
    parameters: tuple[_EffectiveParameterLike, ...]
    runtime: _RuntimeLike
    randomness: _RandomnessLike
    lineage_schema_version: int


class _ManifestLike(Protocol):
    run_id: RunId
    lineage_sha256: Sha256Digest
    spec: _LineageLike
    manifest_schema_version: int


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())


def utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normalized_configuration_mapping(
    value: _NormalizedConfigurationLike,
) -> dict[str, object]:
    return {
        "environment": value.environment,
        "run": {"mode": value.mode},
        "schema_version": value.schema_version,
    }


def normalized_configuration_bytes(value: object) -> bytes:
    normalized = cast(_NormalizedConfigurationLike, value)
    return canonical_json_bytes(_normalized_configuration_mapping(normalized))


def _parameter_mapping(value: _EffectiveParameterLike) -> dict[str, object]:
    wire_value: object
    if value.kind.value == "float64":
        wire_value = struct.pack(">d", cast(float, value.value)).hex()
    else:
        wire_value = value.value
    return {"name": value.name, "type": value.kind.value, "value": wire_value}


def _distribution_mapping(value: _DistributionLike) -> dict[str, object]:
    return {"name": value.name, "version": value.version}


def _lineage_mapping(spec: _LineageLike) -> dict[str, object]:
    return {
        "code": {
            "commit": spec.code.commit,
            "worktree_clean": spec.code.worktree_clean,
        },
        "configuration": {
            "canonicalization": CONFIG_CANONICALIZATION,
            "normalized": _normalized_configuration_mapping(spec.configuration.normalized),
            "sha256": spec.configuration.sha256.value,
        },
        "data": {
            "canonicalization": DATA_CANONICALIZATION,
            "record_count": spec.data.record_count,
            "sha256": spec.data.sha256.value,
        },
        "lineage_schema_version": spec.lineage_schema_version,
        "parameters": [_parameter_mapping(item) for item in spec.parameters],
        "randomness": {
            "generator": spec.randomness.generator,
            "master_seed": spec.randomness.master_seed,
            "stream_derivation": spec.randomness.stream_derivation,
            "stream_labels": list(spec.randomness.stream_labels),
        },
        "replay_window": {
            "end_exclusive": utc_text(spec.replay_window.end_exclusive),
            "initial_state": "empty",
            "selection_field": "available_at",
            "start_inclusive": utc_text(spec.replay_window.start_inclusive),
            "timezone": "UTC",
        },
        "runtime": {
            "distributions": [_distribution_mapping(item) for item in spec.runtime.distributions],
            "ea_version": spec.runtime.ea_version,
            "numeric_policy": spec.runtime.numeric_policy,
            "platform_tag": spec.runtime.platform_tag,
            "python_cache_tag": spec.runtime.python_cache_tag,
            "python_implementation": spec.runtime.python_implementation,
            "python_version": spec.runtime.python_version,
            "sys_platform": spec.runtime.sys_platform,
            "uv_lock_sha256": spec.runtime.uv_lock_sha256.value,
        },
    }


def canonical_lineage_bytes(spec: object) -> bytes:
    lineage = cast(_LineageLike, spec)
    return canonical_json_bytes(_lineage_mapping(lineage))


def canonical_manifest_bytes(manifest: object) -> bytes:
    typed_manifest = cast(_ManifestLike, manifest)
    return canonical_json_bytes(
        {
            "lineage_sha256": typed_manifest.lineage_sha256.value,
            "manifest_schema_version": typed_manifest.manifest_schema_version,
            "run_id": typed_manifest.run_id.value,
            "spec": _lineage_mapping(typed_manifest.spec),
        }
    )
