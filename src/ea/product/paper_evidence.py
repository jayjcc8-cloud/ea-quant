"""Deterministic M4 evidence contract for the local Paper runtime (WU-2).

This module is the single evidence standard the 72-hour and 7-day gates judge
against. It does not add a second runtime, scheduler, or control plane: it
*collects* the state the existing owners already publish and reduces it to a
deterministic ``PASS`` / ``FAIL`` / ``INVALID`` verdict that plain code -- never
an LLM, never a human reading logs -- can reproduce.

Three closed objects form the contract:

* ``PaperSnapshot`` -- one read-only observation of the runtime and host at an
  instant. It reuses ``ea paper status``'s health projection, the writer lease,
  the launchd supervisor, the alert stream, the verified backup and the git
  HEAD, and it owns no new reading of any of them.
* ``PaperGateIdentity`` -- the small set of facts whose drift would invalidate a
  gate: the repo SHA, the runtime profile, the operator config and the launchd
  definitions. This is the only place a digest is taken.
* ``PaperGateVerdict`` -- the outcome of applying the hard rules. Drift produces
  ``INVALID`` (restart the gate), a runtime violation produces ``FAIL`` (the
  runtime is not in a passing state), and only a fully clean, unchanged gate
  produces ``PASS``.

The evaluator is a pure function of its inputs: the same snapshot and identity
always produce the same verdict, so a gate can be re-judged offline from durable
evidence without any observer in the loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from typing import Any

from ea.core import RunId, Sha256Digest
from ea.core.outcomes import OutcomeCode
from ea.core.time import TimeValidationError, require_utc
from ea.product.paper_alerts import AlertSeverity, AlertState, PaperAlertStream
from ea.product.paper_health import (
    KillSwitchProjectionState,
    PaperHealthSnapshot,
    ReconciliationState,
    RuntimeState,
    decode_paper_health,
)

SNAPSHOT_SCHEMA = "ea.paper-snapshot.v1"
GATE_IDENTITY_SCHEMA = "ea.paper-gate-identity.v1"
GATE_VERDICT_SCHEMA = "ea.paper-gate-verdict.v1"

# The evidence schema and runtime profile are code constants: they only change
# when this module changes, so a drift in either is already a repo SHA drift.
EVIDENCE_SCHEMA_VERSION = "1"
RUNTIME_PROFILE = "local-simulated-paper-v1"

# A backup is "fresh" for a gate when its verified creation is within this
# window. It matches PPV-06's default stale threshold, not a new safety rule.
DEFAULT_BACKUP_FRESHNESS_SECONDS = 129_600.0

_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z", flags=re.ASCII)
_GATE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", flags=re.ASCII)
_PROFILE_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z", flags=re.ASCII)
_SCHEMA_VERSION_PATTERN = re.compile(r"[0-9]{1,8}\Z", flags=re.ASCII)


class PaperEvidenceError(ValueError):
    """A snapshot, identity or verdict violates the evidence contract."""

    def __init__(self, code: OutcomeCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: OutcomeCode, message: str) -> PaperEvidenceError:
    return PaperEvidenceError(code, message)


class SupervisorState(StrEnum):
    """The launchd supervisor's report for the supervised Paper job."""

    RUNNING = "running"
    NOT_RUNNING = "not_running"
    UNKNOWN = "unknown"


class BrokerMode(StrEnum):
    """The broker boundary a Paper run exercises. Closed: only local Paper exists."""

    LOCAL_SIMULATED_PAPER = "local_simulated_paper"


class RiskGateState(StrEnum):
    """PPV-15's outbound boundary as the zero-effect probe left it."""

    HEALTHY = "healthy"
    BLOCKED = "blocked"
    HALTED = "halted"
    UNKNOWN = "unknown"


class AlertSummary(StrEnum):
    """Whether the host alert stream holds an unresolved critical alert."""

    CLEAN = "clean"
    CRITICAL_ACTIVE = "critical_active"
    UNAVAILABLE = "unavailable"


class BackupSummary(StrEnum):
    """Whether a usable verified backup exists and how old it is."""

    FRESH = "fresh"
    STALE = "stale"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


class GateType(StrEnum):
    """The two long-soak gates the contract judges."""

    T72H = "72h"
    T7D = "7d"


class GateVerdict(StrEnum):
    """The deterministic outcome. Only ``PASS`` certifies the gate."""

    PASS = "pass"
    FAIL = "fail"
    INVALID = "invalid"


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _require_str(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if type(value) is not str or not value.strip():
        raise _fail(OutcomeCode.INVALID_TYPE, f"{key} must be a non-empty str")
    return value


def _require_bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if type(value) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{key} must be an exact bool")
    return value


def _require_enum(document: dict[str, Any], key: str, expected: type[StrEnum]) -> Any:
    value = document.get(key)
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{key} must be a str")
    try:
        return expected(value)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{key} is not a known value") from error


def _require_sha(value: object, *, field: str) -> str:
    if type(value) is not str or _GIT_SHA_PATTERN.fullmatch(value) is None:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be a 40-hex git SHA")
    return value


def _require_stamp(value: object, *, field: str) -> datetime:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a str")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is not a timestamp") from error
    try:
        return require_utc(parsed, field=field)
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _require_digest(value: object, *, field: str) -> Sha256Digest:
    if type(value) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a Sha256Digest")
    return value


def _dedup(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


# ---------------------------------------------------------------------------
# identity digests
# ---------------------------------------------------------------------------


def config_identity_digest(config_bytes: bytes) -> Sha256Digest:
    """Digest the operator config whose drift would invalidate a gate.

    The config is the launchd operator's ``run-config.env``: a checked data file
    whose bytes are the operator's only configurable identity. Nothing here
    parses it; any byte change is drift.
    """
    if type(config_bytes) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "config bytes must be exact bytes")
    return Sha256Digest(sha256(b"ea.config-identity.v1\0" + config_bytes).hexdigest())


def launchd_identity_digest(launchd_bytes: bytes) -> Sha256Digest:
    """Digest the rendered launchd definitions whose drift would invalidate a gate."""
    if type(launchd_bytes) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "launchd bytes must be exact bytes")
    return Sha256Digest(sha256(b"ea.launchd-identity.v1\0" + launchd_bytes).hexdigest())


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def _risk_gate_state(health: PaperHealthSnapshot | None) -> RiskGateState:
    if health is None:
        return RiskGateState.UNKNOWN
    if health.operator_halt or health.kill_switch_state is KillSwitchProjectionState.HALTED:
        return RiskGateState.HALTED
    if health.trade_blocking_guard is not None:
        return RiskGateState.BLOCKED
    return RiskGateState.HEALTHY


def summarize_alert_state(stream: PaperAlertStream | None) -> AlertSummary:
    """Summarize the durable alert stream into one gate-relevant fact.

    A missing stream is ``UNAVAILABLE``, never ``CLEAN``: an unobserved alert
    is not evidence that nothing is wrong.
    """
    if stream is None:
        return AlertSummary.UNAVAILABLE
    if any(
        alert.state is AlertState.ACTIVE and alert.severity is AlertSeverity.CRITICAL
        for alert in stream.alerts
    ):
        return AlertSummary.CRITICAL_ACTIVE
    return AlertSummary.CLEAN


def summarize_backup_state(
    created_at: datetime | None,
    *,
    captured_at: datetime,
    freshness_seconds: float,
) -> BackupSummary:
    """Summarize the newest verified backup into one gate-relevant fact.

    ``created_at`` is the creation instant of the newest verified backup (the
    caller supplies it from ``latest_verified_paper_backup``), or ``None`` when
    no verified backup exists. No verified backup is ``ABSENT``; a verified one
    is ``FRESH`` while its creation is within the freshness window and
    ``STALE`` otherwise.
    """
    if (
        type(freshness_seconds) is not float
        or not isfinite(freshness_seconds)
        or freshness_seconds <= 0
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "backup freshness must be a positive finite float")
    try:
        captured_at = require_utc(captured_at, field="captured_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    if created_at is None:
        return BackupSummary.ABSENT
    try:
        created_at = require_utc(created_at, field="created_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    age_seconds = (captured_at - created_at).total_seconds()
    return BackupSummary.FRESH if age_seconds <= freshness_seconds else BackupSummary.STALE


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    """One read-only observation of the Paper runtime and its gate inputs."""

    captured_at: datetime
    repo_sha: str
    run_id: RunId
    runtime_state: RuntimeState
    supervisor_state: SupervisorState
    writer_lease_held: bool
    broker_mode: BrokerMode
    live_enabled: bool
    reconciliation_state: ReconciliationState
    risk_gate_state: RiskGateState
    readiness: bool
    health: PaperHealthSnapshot | None
    alert_state: AlertSummary
    backup_state: BackupSummary
    schema: str = SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SNAPSHOT_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "snapshot schema conflicts")
        try:
            require_utc(self.captured_at, field="captured_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        _require_sha(self.repo_sha, field="repo_sha")
        if type(self.run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
        if type(self.writer_lease_held) is not bool:
            raise _fail(OutcomeCode.INVALID_TYPE, "writer_lease_held must be an exact bool")
        if type(self.live_enabled) is not bool:
            raise _fail(OutcomeCode.INVALID_TYPE, "live_enabled must be an exact bool")
        if type(self.readiness) is not bool:
            raise _fail(OutcomeCode.INVALID_TYPE, "readiness must be an exact bool")
        for name, expected in (
            ("runtime_state", RuntimeState),
            ("supervisor_state", SupervisorState),
            ("broker_mode", BrokerMode),
            ("reconciliation_state", ReconciliationState),
            ("risk_gate_state", RiskGateState),
            ("alert_state", AlertSummary),
            ("backup_state", BackupSummary),
        ):
            if type(getattr(self, name)) is not expected:
                raise _fail(
                    OutcomeCode.INVALID_TYPE, f"{name} must be an exact {expected.__name__}"
                )
        if self.health is not None and type(self.health) is not PaperHealthSnapshot:
            raise _fail(OutcomeCode.INVALID_TYPE, "health must be a health snapshot or None")

    def document(self) -> dict[str, Any]:
        """The canonical JSON-safe snapshot, as published and re-read."""
        return {
            "schema": self.schema,
            "captured_at": self.captured_at.isoformat(),
            "repo_sha": self.repo_sha,
            "run_id": self.run_id.value,
            "runtime_state": self.runtime_state.value,
            "supervisor_state": self.supervisor_state.value,
            "writer_lease_held": self.writer_lease_held,
            "broker_mode": self.broker_mode.value,
            "live_enabled": self.live_enabled,
            "reconciliation_state": self.reconciliation_state.value,
            "risk_gate_state": self.risk_gate_state.value,
            "readiness": self.readiness,
            "health": None if self.health is None else self.health.document(),
            "alert_state": self.alert_state.value,
            "backup_state": self.backup_state.value,
        }


def canonical_paper_snapshot_bytes(snapshot: PaperSnapshot) -> bytes:
    """Return the canonical durable document for one snapshot."""
    if type(snapshot) is not PaperSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot must be an exact PaperSnapshot")
    return _canonical(snapshot.document())


def build_paper_snapshot(
    *,
    captured_at: datetime,
    repo_sha: str,
    run_id: RunId,
    supervisor_state: SupervisorState,
    writer_lease_held: bool,
    broker_mode: BrokerMode,
    live_enabled: bool,
    health: PaperHealthSnapshot | None,
    alert_state: AlertSummary,
    backup_state: BackupSummary,
) -> PaperSnapshot:
    """Build one snapshot; runtime/readiness/risk state are read from ``health``.

    The summary fields that already live inside the health projection are
    derived here, not re-entered, so the snapshot cannot disagree with the
    projection it embeds. A ``None`` health (the explicit unavailable marker)
    is preserved as ``UNKNOWN`` state and ``readiness`` false, never read as
    healthy.
    """
    if health is None:
        runtime_state = RuntimeState.UNKNOWN
        reconciliation_state = ReconciliationState.UNKNOWN
        risk_gate_state = RiskGateState.UNKNOWN
        readiness = False
    else:
        runtime_state = health.runtime_state
        reconciliation_state = health.reconciliation_state
        risk_gate_state = _risk_gate_state(health)
        readiness = health.runtime_ready
    return PaperSnapshot(
        captured_at=captured_at,
        repo_sha=repo_sha,
        run_id=run_id,
        runtime_state=runtime_state,
        supervisor_state=supervisor_state,
        writer_lease_held=writer_lease_held,
        broker_mode=broker_mode,
        live_enabled=live_enabled,
        reconciliation_state=reconciliation_state,
        risk_gate_state=risk_gate_state,
        readiness=readiness,
        health=health,
        alert_state=alert_state,
        backup_state=backup_state,
    )


def decode_paper_snapshot(document: object) -> PaperSnapshot:
    """Rebuild one snapshot from its durable document; malformed input fails closed."""
    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot document must be one object")
    if document.get("schema") != SNAPSHOT_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "snapshot schema conflicts")
    health = document.get("health")
    if health is None:
        health_snapshot: PaperHealthSnapshot | None = None
    else:
        health_snapshot = decode_paper_health(health)
    try:
        return PaperSnapshot(
            captured_at=_require_stamp(document.get("captured_at"), field="captured_at"),
            repo_sha=_require_sha(document.get("repo_sha"), field="repo_sha"),
            run_id=RunId(_require_str(document, "run_id")),
            runtime_state=_require_enum(document, "runtime_state", RuntimeState),
            supervisor_state=_require_enum(document, "supervisor_state", SupervisorState),
            writer_lease_held=_require_bool(document, "writer_lease_held"),
            broker_mode=_require_enum(document, "broker_mode", BrokerMode),
            live_enabled=_require_bool(document, "live_enabled"),
            reconciliation_state=_require_enum(
                document, "reconciliation_state", ReconciliationState
            ),
            risk_gate_state=_require_enum(document, "risk_gate_state", RiskGateState),
            readiness=_require_bool(document, "readiness"),
            health=health_snapshot,
            alert_state=_require_enum(document, "alert_state", AlertSummary),
            backup_state=_require_enum(document, "backup_state", BackupSummary),
        )
    except (PaperEvidenceError, ValueError) as error:
        if isinstance(error, PaperEvidenceError):
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "snapshot document conflicts") from error


# ---------------------------------------------------------------------------
# gate identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperGateIdentity:
    """The locked identity a gate certifies against.

    Every field except ``started_at`` is drift-sensitive: a change means the
    gate's evidentiary basis changed and the verdict is ``INVALID``, not a
    continued clock.
    """

    gate_id: str
    gate_type: GateType
    repo_sha: str
    runtime_profile: str
    config_identity: Sha256Digest
    launchd_identity: Sha256Digest
    evidence_schema_version: str
    started_at: datetime
    schema: str = GATE_IDENTITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GATE_IDENTITY_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "gate identity schema conflicts")
        if _GATE_ID_PATTERN.fullmatch(self.gate_id) is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "gate_id must be a bounded label")
        if type(self.gate_type) is not GateType:
            raise _fail(OutcomeCode.INVALID_TYPE, "gate_type must be an exact GateType")
        _require_sha(self.repo_sha, field="repo_sha")
        if _PROFILE_PATTERN.fullmatch(self.runtime_profile) is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "runtime_profile must be a bounded label")
        _require_digest(self.config_identity, field="config_identity")
        _require_digest(self.launchd_identity, field="launchd_identity")
        if _SCHEMA_VERSION_PATTERN.fullmatch(self.evidence_schema_version) is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "evidence_schema_version must be numeric")
        try:
            require_utc(self.started_at, field="started_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error

    def document(self) -> dict[str, Any]:
        """The canonical JSON-safe identity, as locked and re-read."""
        return {
            "schema": self.schema,
            "gate_id": self.gate_id,
            "gate_type": self.gate_type.value,
            "repo_sha": self.repo_sha,
            "runtime_profile": self.runtime_profile,
            "config_identity": self.config_identity.value,
            "launchd_identity": self.launchd_identity.value,
            "evidence_schema_version": self.evidence_schema_version,
            "started_at": self.started_at.isoformat(),
        }


def canonical_gate_identity_bytes(identity: PaperGateIdentity) -> bytes:
    """Return the canonical durable document for one locked gate identity."""
    if type(identity) is not PaperGateIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "identity must be an exact PaperGateIdentity")
    return _canonical(identity.document())


def build_gate_identity(
    *,
    gate_id: str,
    gate_type: GateType,
    repo_sha: str,
    config_bytes: bytes,
    launchd_bytes: bytes,
    started_at: datetime,
    runtime_profile: str = RUNTIME_PROFILE,
    evidence_schema_version: str = EVIDENCE_SCHEMA_VERSION,
) -> PaperGateIdentity:
    """Build a gate identity from raw config and launchd bytes, hashing the two."""
    return PaperGateIdentity(
        gate_id=gate_id,
        gate_type=gate_type,
        repo_sha=repo_sha,
        runtime_profile=runtime_profile,
        config_identity=config_identity_digest(config_bytes),
        launchd_identity=launchd_identity_digest(launchd_bytes),
        evidence_schema_version=evidence_schema_version,
        started_at=started_at,
    )


def decode_gate_identity(document: object) -> PaperGateIdentity:
    """Rebuild one gate identity from its durable document; malformed input fails closed."""
    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "gate identity document must be one object")
    if document.get("schema") != GATE_IDENTITY_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "gate identity schema conflicts")
    config_identity = document.get("config_identity")
    launchd_identity = document.get("launchd_identity")
    if type(config_identity) is not str or type(launchd_identity) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "identity digests must be str")
    try:
        return PaperGateIdentity(
            gate_id=_require_str(document, "gate_id"),
            gate_type=_require_enum(document, "gate_type", GateType),
            repo_sha=_require_sha(document.get("repo_sha"), field="repo_sha"),
            runtime_profile=_require_str(document, "runtime_profile"),
            config_identity=Sha256Digest(config_identity),
            launchd_identity=Sha256Digest(launchd_identity),
            evidence_schema_version=_require_str(document, "evidence_schema_version"),
            started_at=_require_stamp(document.get("started_at"), field="started_at"),
        )
    except (PaperEvidenceError, ValueError) as error:
        if isinstance(error, PaperEvidenceError):
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "gate identity document conflicts") from error


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperGateVerdict:
    """The deterministic verdict of one gate evaluation."""

    verdict: GateVerdict
    gate_id: str
    gate_type: GateType
    evaluated_at: datetime
    reasons: tuple[str, ...]
    schema: str = GATE_VERDICT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GATE_VERDICT_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "verdict schema conflicts")
        if type(self.verdict) is not GateVerdict:
            raise _fail(OutcomeCode.INVALID_TYPE, "verdict must be an exact GateVerdict")
        if _GATE_ID_PATTERN.fullmatch(self.gate_id) is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "gate_id must be a bounded label")
        if type(self.gate_type) is not GateType:
            raise _fail(OutcomeCode.INVALID_TYPE, "gate_type must be an exact GateType")
        try:
            require_utc(self.evaluated_at, field="evaluated_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        if type(self.reasons) is not tuple or any(
            type(reason) is not str or not reason.strip() for reason in self.reasons
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "reasons must be non-empty strings")
        if len(set(self.reasons)) != len(self.reasons):
            raise _fail(OutcomeCode.CONFLICTING_ID, "reasons must not repeat")
        if self.verdict is GateVerdict.PASS and self.reasons:
            raise _fail(OutcomeCode.CONFLICTING_ID, "a passing verdict carries no reasons")
        if self.verdict is not GateVerdict.PASS and not self.reasons:
            raise _fail(OutcomeCode.CONFLICTING_ID, "a non-passing verdict names its reasons")

    def document(self) -> dict[str, Any]:
        """The canonical JSON-safe verdict."""
        return {
            "schema": self.schema,
            "verdict": self.verdict.value,
            "gate_id": self.gate_id,
            "gate_type": self.gate_type.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "reasons": list(self.reasons),
        }


def canonical_gate_verdict_bytes(verdict: PaperGateVerdict) -> bytes:
    """Return the canonical durable document for one verdict."""
    if type(verdict) is not PaperGateVerdict:
        raise _fail(OutcomeCode.INVALID_TYPE, "verdict must be an exact PaperGateVerdict")
    return _canonical(verdict.document())


def _identity_drift_reasons(
    snapshot: PaperSnapshot,
    locked: PaperGateIdentity,
    current: PaperGateIdentity,
) -> list[str]:
    """Yield the identity-relevant drift reasons, in a stable order."""
    reasons: list[str] = []
    if snapshot.repo_sha != locked.repo_sha:
        reasons.append("repo_sha_drift")
    if current.gate_id != locked.gate_id:
        reasons.append("gate_id_drift")
    if current.gate_type is not locked.gate_type:
        reasons.append("gate_type_drift")
    if current.repo_sha != locked.repo_sha:
        reasons.append("repo_sha_drift")
    if current.runtime_profile != locked.runtime_profile:
        reasons.append("runtime_profile_drift")
    if current.config_identity != locked.config_identity:
        reasons.append("config_drift")
    if current.launchd_identity != locked.launchd_identity:
        reasons.append("launchd_drift")
    if current.evidence_schema_version != locked.evidence_schema_version:
        reasons.append("evidence_schema_drift")
    # The Paper/Live boundary is part of the locked identity, so an unexpected
    # mode is drift, not a runtime fault.
    if snapshot.broker_mode is not BrokerMode.LOCAL_SIMULATED_PAPER:
        reasons.append("broker_mode_drift")
    if snapshot.live_enabled:
        reasons.append("unexpected_live_capability")
    return reasons


def _hard_rule_reasons(snapshot: PaperSnapshot) -> list[str]:
    """Apply the hard rules as plain code; one reason per violated rule."""
    reasons: list[str] = []
    if not snapshot.writer_lease_held:
        reasons.append("writer_not_exclusive")
    if snapshot.reconciliation_state is not ReconciliationState.CLEAN:
        reasons.append("reconciliation_not_clean")
    if snapshot.risk_gate_state is not RiskGateState.HEALTHY:
        reasons.append("risk_gate_not_healthy")
    if snapshot.runtime_state is not RuntimeState.RUNNING:
        reasons.append("runtime_not_running")
    if not snapshot.readiness:
        reasons.append("runtime_not_ready")
    if snapshot.backup_state is not BackupSummary.FRESH:
        reasons.append("backup_not_fresh")
    if snapshot.alert_state is not AlertSummary.CLEAN:
        reasons.append("critical_alert_active")
    return reasons


def evaluate_gate(
    snapshot: PaperSnapshot,
    locked_identity: PaperGateIdentity,
    current_identity: PaperGateIdentity,
    *,
    evaluated_at: datetime,
) -> PaperGateVerdict:
    """Judge one snapshot against a locked gate identity, deterministically.

    Drift is evaluated first and produces ``INVALID``: once the gate's
    evidentiary basis has moved, no runtime state can rescue it and the gate
    must be restarted from a fresh identity. Only then are the hard rules
    applied; any violation produces ``FAIL``. A clean, unchanged gate produces
    ``PASS``.

    This function is pure: it performs no I/O and reads no clock, so the same
    snapshot and identities always produce the same verdict.
    """
    if type(snapshot) is not PaperSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot must be an exact PaperSnapshot")
    if type(locked_identity) is not PaperGateIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "locked_identity must be an exact identity")
    if type(current_identity) is not PaperGateIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "current_identity must be an exact identity")
    try:
        require_utc(evaluated_at, field="evaluated_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error

    drift = _dedup(_identity_drift_reasons(snapshot, locked_identity, current_identity))
    if drift:
        return PaperGateVerdict(
            verdict=GateVerdict.INVALID,
            gate_id=locked_identity.gate_id,
            gate_type=locked_identity.gate_type,
            evaluated_at=evaluated_at,
            reasons=drift,
        )

    failures = _dedup(_hard_rule_reasons(snapshot))
    if failures:
        return PaperGateVerdict(
            verdict=GateVerdict.FAIL,
            gate_id=locked_identity.gate_id,
            gate_type=locked_identity.gate_type,
            evaluated_at=evaluated_at,
            reasons=failures,
        )

    return PaperGateVerdict(
        verdict=GateVerdict.PASS,
        gate_id=locked_identity.gate_id,
        gate_type=locked_identity.gate_type,
        evaluated_at=evaluated_at,
        reasons=(),
    )


def decode_gate_verdict(document: object) -> PaperGateVerdict:
    """Rebuild one verdict from its durable document; malformed input fails closed."""
    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "verdict document must be one object")
    if document.get("schema") != GATE_VERDICT_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "verdict schema conflicts")
    reasons = document.get("reasons")
    if type(reasons) is not list or any(type(reason) is not str for reason in reasons):
        raise _fail(OutcomeCode.INVALID_TYPE, "reasons must be one list of strings")
    try:
        return PaperGateVerdict(
            verdict=_require_enum(document, "verdict", GateVerdict),
            gate_id=_require_str(document, "gate_id"),
            gate_type=_require_enum(document, "gate_type", GateType),
            evaluated_at=_require_stamp(document.get("evaluated_at"), field="evaluated_at"),
            reasons=tuple(reasons),
        )
    except PaperEvidenceError:
        raise
    except ValueError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "verdict document conflicts") from error
