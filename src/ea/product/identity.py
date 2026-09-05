"""Backtest attempt, reproducibility, and semantic outcome projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

from ea.core.run import DataFingerprint, ReplayWindow, Sha256Digest

_LINEAGE_DOMAIN = b"ea.backtest-lineage.v1\0"
_SEMANTIC_OUTCOME_DOMAIN = b"ea.backtest-semantic-outcome.v1\0"
_ATTEMPT_ONLY_KEYS = frozenset(
    {
        "audit_chain_head_sha256",
        "available_at",
        "causation_id",
        "correlation_id",
        "fill_id",
        "occurred_at",
        "order_id",
        "output_directory",
        "output_root",
        "record_sha256",
        "run_id",
        "signal_id",
        "timestamp",
    }
)


class BacktestIdentityError(ValueError):
    """Raised when a backtest identity projection is not canonical."""


class RandomnessProfile(StrEnum):
    """Supported backtest randomness ownership profiles."""

    NONE = "none"


def _canonical_json(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise BacktestIdentityError("identity projection must be canonical JSON") from error


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise BacktestIdentityError(f"{field} must be non-empty canonical text")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise BacktestIdentityError(f"{field} must contain printable ASCII only")
    return value


@dataclass(frozen=True, slots=True)
class BacktestRandomness:
    """Explicit randomness ownership for one reproducibility lineage."""

    profile: RandomnessProfile = RandomnessProfile.NONE
    master_seed: int | None = None

    def __post_init__(self) -> None:
        if type(self.profile) is not RandomnessProfile:
            raise BacktestIdentityError("randomness profile must use the closed vocabulary")
        if self.profile is RandomnessProfile.NONE and self.master_seed is not None:
            raise BacktestIdentityError("a non-random profile cannot declare a master seed")

    def document(self) -> dict[str, object]:
        return {
            "master_seed": ("not_applicable" if self.master_seed is None else self.master_seed),
            "profile": self.profile.value,
        }


@dataclass(frozen=True, slots=True)
class BacktestLineageInputs:
    """Typed inputs for the installed backtest reproducibility projection."""

    data: DataFingerprint
    replay_window: ReplayWindow
    scenario_sha256: Sha256Digest
    instrument_spec_set_sha256: Sha256Digest
    strategy_id: str
    strategy_parameters_sha256: Sha256Digest
    risk_policy_id: str
    risk_limits_sha256: Sha256Digest
    execution_policy_id: str
    execution_policy_sha256: Sha256Digest
    code_sha256: Sha256Digest
    distribution_name: str
    distribution_version: str
    python_implementation: str
    python_version: str
    python_cache_tag: str
    sys_platform: str
    platform_tag: str
    randomness: BacktestRandomness

    def __post_init__(self) -> None:
        if type(self.data) is not DataFingerprint:
            raise BacktestIdentityError("data must be a DataFingerprint")
        if type(self.replay_window) is not ReplayWindow:
            raise BacktestIdentityError("replay_window must be a ReplayWindow")
        for field in (
            "scenario_sha256",
            "instrument_spec_set_sha256",
            "strategy_parameters_sha256",
            "risk_limits_sha256",
            "execution_policy_sha256",
            "code_sha256",
        ):
            if type(getattr(self, field)) is not Sha256Digest:
                raise BacktestIdentityError(f"{field} must be a Sha256Digest")
        for field in (
            "strategy_id",
            "risk_policy_id",
            "execution_policy_id",
            "distribution_name",
            "distribution_version",
            "python_implementation",
            "python_version",
            "python_cache_tag",
            "sys_platform",
            "platform_tag",
        ):
            _require_text(getattr(self, field), field=field)
        if type(self.randomness) is not BacktestRandomness:
            raise BacktestIdentityError("randomness must be BacktestRandomness")


def canonical_backtest_lineage_bytes(inputs: BacktestLineageInputs) -> bytes:
    """Return the attempt-independent canonical reproducibility projection."""
    if type(inputs) is not BacktestLineageInputs:
        raise BacktestIdentityError("lineage inputs must be BacktestLineageInputs")
    document = {
        "code": {
            "distribution": {
                "name": inputs.distribution_name,
                "version": inputs.distribution_version,
            },
            "package_sha256": inputs.code_sha256.value,
        },
        "data": {
            "canonicalization": "ea-market-data-envelope-v1",
            "record_count": inputs.data.record_count,
            "sha256": inputs.data.sha256.value,
        },
        "execution_policy": {
            "id": inputs.execution_policy_id,
            "sha256": inputs.execution_policy_sha256.value,
        },
        "instrument": {
            "spec_set_sha256": inputs.instrument_spec_set_sha256.value,
        },
        "lineage_schema_version": 1,
        "randomness": inputs.randomness.document(),
        "replay_window": {
            "end_exclusive": _utc_text(inputs.replay_window.end_exclusive),
            "selection_field": "available_at",
            "start_inclusive": _utc_text(inputs.replay_window.start_inclusive),
            "timezone": "UTC",
        },
        "risk": {
            "limits_sha256": inputs.risk_limits_sha256.value,
            "policy_id": inputs.risk_policy_id,
        },
        "runtime": {
            "platform_tag": inputs.platform_tag,
            "python_cache_tag": inputs.python_cache_tag,
            "python_implementation": inputs.python_implementation,
            "python_version": inputs.python_version,
            "sys_platform": inputs.sys_platform,
        },
        "scenario": {"sha256": inputs.scenario_sha256.value},
        "strategy": {
            "id": inputs.strategy_id,
            "parameters_sha256": inputs.strategy_parameters_sha256.value,
        },
    }
    return _canonical_json(document)


def build_backtest_lineage(inputs: BacktestLineageInputs) -> Sha256Digest:
    """Hash one complete canonical reproducibility projection."""
    return Sha256Digest(
        sha256(_LINEAGE_DOMAIN + canonical_backtest_lineage_bytes(inputs)).hexdigest()
    )


def _reject_attempt_only_fields(value: object) -> None:
    if type(value) is dict:
        for key, nested in value.items():
            if type(key) is not str:
                raise BacktestIdentityError("semantic outcome keys must be strings")
            if key in _ATTEMPT_ONLY_KEYS:
                raise BacktestIdentityError(
                    f"attempt-only field {key!r} cannot enter semantic outcome"
                )
            _reject_attempt_only_fields(nested)
    elif type(value) is list:
        for nested in value:
            _reject_attempt_only_fields(nested)


def semantic_outcome_sha256(projection: Mapping[str, object]) -> Sha256Digest:
    """Hash explicit economic semantics after rejecting attempt-only evidence."""
    if not isinstance(projection, Mapping):
        raise BacktestIdentityError("semantic outcome must be a mapping")
    document = dict(projection)
    _reject_attempt_only_fields(document)
    return Sha256Digest(sha256(_SEMANTIC_OUTCOME_DOMAIN + _canonical_json(document)).hexdigest())
