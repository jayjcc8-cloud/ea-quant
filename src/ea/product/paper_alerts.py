"""Durable host-scoped Paper alert stream: observation and delivery only (PPV-06).

This module owns exactly one durable artifact -- a single alert stream per host
-- and exactly one authority: none over trading. It observes, it records, and it
delivers to a reader. It cannot repair a ledger, lift a kill switch, alter
reconciliation, restore trading or resend an order, and no component that makes
a trading decision may read it. PPV-15's ``authorize`` never gains an alert
input, directly or transitively.

Three deliberate separations:

* HOST SCOPE. ``backup_stale``, ``backup_failed`` and ``disk_capacity_low`` are
  host-level facts that outlive any individual run. Burying them in one run's
  ``outputs/paper-status.json`` would make them die with that run, so the stream
  is host-scoped and run-scoped alerts carry ``run_id`` as a field instead. This
  is a new *file*, not a new authoritative state store: nothing may decide from
  it.

* ALERTING POLICY IS NOT SAFETY POLICY. ``AlertThresholds`` is this module's own
  alerting policy and is deliberately NOT ``OperationalSafetyLimits``. Reusing
  PPV-15's limits here would re-threshold the safety authority from inside a
  monitoring module and build the second safety engine the plan forbids. The two
  numbers answer different questions: "when should a human be told" versus "what
  may be sent to a venue". They must be allowed to differ, and they are free to
  have different owners and different lifecycles.

* HONESTY OVER QUIET. Every signal carries an explicit ``unavailable``
  condition. A fact this module cannot observe is never silently reported as
  healthy or absent, because an alert stream that reports "no alerts" when it
  cannot see is a lying authority -- the exact failure this unit exists to
  avoid. Two of the nine signals are permanently unavailable today: no durable
  backup-outcome record exists (``backup_failed``), and
  ``PaperBroker.available`` is a hardcoded ``True`` with no provider until
  PPV-17 (``broker_unavailable``). No threshold is built over a constant.

The evaluator that produces these readings lives in
:mod:`ea.product.paper_alert_evaluation`; this module is contracts, dedup and
durable publication, and it never reads PPV-03's projection itself.
"""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, final
from uuid import uuid4

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId
from ea.core.time import TimeValidationError, require_utc
from ea.experiments._manifest_wire import canonical_json_bytes

PAPER_ALERT_STREAM_SCHEMA = "ea.paper-alert-stream.v1"

_MAX_TEXT_BYTES = 512
_MAX_FILE_BYTES = 1 << 20
# Retention bound on resolved alerts. A durable stream that grows without bound
# is a defect, and an alert stream that silently drops an ACTIVE alert is a lie:
# only RESOLVED rows are ever pruned, oldest observation first.
_MAX_RESOLVED_ALERTS = 64

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700

_HOST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_ALERT_ID = re.compile(r"[A-Za-z0-9._|-]{1,192}\Z", re.ASCII)

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
        OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
    }
)

# The stream's own closed key sets. A durable document is decoded against these,
# so an unexpected field is a conflict rather than something silently ignored.
_STREAM_KEYS = frozenset({"schema", "host_id", "evaluated_at", "alerts", "signals"})
_ALERT_KEYS = frozenset(
    {
        "alert_id",
        "type",
        "severity",
        "state",
        "first_seen",
        "last_seen",
        "reason",
        "observation",
        "operator_action",
        "run_id",
        "account_id",
    }
)
_SIGNAL_KEYS = frozenset({"type", "scope", "severity", "condition", "active_since", "observation"})


class PaperAlertError(ValueError):
    """Closed contract failure at the Paper alert boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("paper-alert errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


class PaperAlertUnavailable(PaperAlertError):
    """The durable alert stream is unreadable or is not usable evidence.

    A reader that cannot obtain the stream must say so and exit, never report an
    empty -- and therefore apparently healthy -- alert set.
    """


class PaperAlertStreamAbsent(PaperAlertUnavailable):
    """No alert stream has been published at this path yet.

    Absent is distinguished from unreadable on purpose: a publisher may start a
    fresh stream where none exists, but must never overwrite a stream it cannot
    read, because that is how an open alert disappears.
    """


def _fail(code: OutcomeCode, message: str) -> PaperAlertError:
    return PaperAlertError(code, message)


def _safe_text(value: object, *, field: str, maximum: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be an exact str")
    if (
        not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is empty, oversized or unsafe")
    return value


def require_host_id(value: object) -> str:
    """Validate one bounded host scope label."""
    if type(value) is not str or _HOST_ID.fullmatch(value) is None:
        raise _fail(OutcomeCode.INVALID_TYPE, "host_id must be one bounded host label")
    return value


class AlertType(StrEnum):
    """The nine required V1 signals. Membership is closed and total."""

    RUNTIME_DOWN = "runtime_down"
    NOT_READY_BEYOND_THRESHOLD = "not_ready_beyond_threshold"
    RECONCILIATION_CONFLICT = "reconciliation_conflict"
    OPERATOR_HALT_ACTIVE = "operator_halt_active"
    UNEXPECTED_RESTART = "unexpected_restart"
    BACKUP_STALE = "backup_stale"
    DISK_CAPACITY_LOW = "disk_capacity_low"
    BACKUP_FAILED = "backup_failed"
    BROKER_UNAVAILABLE = "broker_unavailable"


class AlertScope(StrEnum):
    """Whether one signal's fact outlives a single run."""

    HOST = "host"
    RUN = "run"


class AlertSeverity(StrEnum):
    """How loudly one signal should reach a human when it holds."""

    CRITICAL = "critical"
    WARNING = "warning"


class AlertState(StrEnum):
    """An alert is open, or it was open and the reading recovered."""

    ACTIVE = "active"
    RESOLVED = "resolved"


class SignalCondition(StrEnum):
    """What one evaluation could honestly say about one signal.

    ``UNAVAILABLE`` is a first-class outcome, not an absence of data: it records
    that the fact could not be observed and must never be read as healthy.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNAVAILABLE = "unavailable"


@final
@dataclass(frozen=True, slots=True)
class AlertSignalSpec:
    """The closed, per-signal policy this module owns.

    ``reason`` is the stable statement of what the signal means -- it does not
    change between evaluations. The changing detail lives on the reading.
    """

    type: AlertType
    scope: AlertScope
    severity: AlertSeverity
    reason: str
    operator_action: str

    def __post_init__(self) -> None:
        if type(self.type) is not AlertType:
            raise _fail(OutcomeCode.INVALID_TYPE, "signal type must be an exact AlertType")
        if type(self.scope) is not AlertScope:
            raise _fail(OutcomeCode.INVALID_TYPE, "signal scope must be an exact AlertScope")
        if type(self.severity) is not AlertSeverity:
            raise _fail(OutcomeCode.INVALID_TYPE, "signal severity must be an exact AlertSeverity")
        _safe_text(self.reason, field="signal reason")
        _safe_text(self.operator_action, field="signal operator action")


# The nine signals, in canonical order. Order is part of the durable contract:
# every evaluation publishes every signal, so a reader can never confuse "no
# alerts" with "nothing was looked at".
ALERT_SIGNALS: tuple[AlertSignalSpec, ...] = (
    AlertSignalSpec(
        AlertType.RUNTIME_DOWN,
        AlertScope.RUN,
        AlertSeverity.CRITICAL,
        "the Paper runtime is not alive while the attempt has not terminated",
        "Find out why the runtime stopped, then restart it and confirm the writer lease "
        "is reacquired. Do not clear anything from this stream.",
    ),
    AlertSignalSpec(
        AlertType.NOT_READY_BEYOND_THRESHOLD,
        AlertScope.RUN,
        AlertSeverity.WARNING,
        "the Paper runtime is alive but has not been ready for longer than the alerting "
        "policy allows",
        "Read the readiness reason codes in the run's health projection and fix the "
        "dependency they name. This stream reports readiness; it never decides it.",
    ),
    AlertSignalSpec(
        AlertType.RECONCILIATION_CONFLICT,
        AlertScope.RUN,
        AlertSeverity.CRITICAL,
        "the ledger retains unresolved broker reconciliation references",
        "Inspect the broker reconciliation evidence and resolve it through the owning "
        "recovery path. This stream never repairs a ledger.",
    ),
    AlertSignalSpec(
        AlertType.OPERATOR_HALT_ACTIVE,
        AlertScope.RUN,
        AlertSeverity.CRITICAL,
        "an operator halt is active, so outbound effects are refused while the runtime "
        "may still be alive and ready",
        "Confirm the halt is intended. Lifting a halt is an operator decision taken "
        "through the kill-switch authority; this stream never lifts it.",
    ),
    AlertSignalSpec(
        AlertType.UNEXPECTED_RESTART,
        AlertScope.RUN,
        AlertSeverity.CRITICAL,
        "the attempt ended without a durable terminal record, so the last journal record "
        "is not run.terminal",
        "Resume the attempt through the recovery path and complete its reconciliation "
        "before it trades again. This stream never resumes or repairs it.",
    ),
    AlertSignalSpec(
        AlertType.BACKUP_STALE,
        AlertScope.HOST,
        AlertSeverity.WARNING,
        "the newest verified Paper backup is older than the alerting policy allows",
        "Run a quiesced backup and verify it. A stale backup is also the observable proxy "
        "for backups that are failing outright.",
    ),
    AlertSignalSpec(
        AlertType.DISK_CAPACITY_LOW,
        AlertScope.HOST,
        AlertSeverity.WARNING,
        "the filesystem holding Paper state has less free space than the alerting policy allows",
        "Free space or move Paper state before the next run. This stream never deletes "
        "or migrates anything.",
    ),
    AlertSignalSpec(
        AlertType.BACKUP_FAILED,
        AlertScope.HOST,
        AlertSeverity.WARNING,
        "a Paper backup attempt failed",
        "Nothing can be actioned from this stream: no durable per-attempt backup outcome "
        "record exists to read. Read backup_stale instead.",
    ),
    AlertSignalSpec(
        AlertType.BROKER_UNAVAILABLE,
        AlertScope.RUN,
        AlertSeverity.CRITICAL,
        "the Paper transport does not accept a new outbound effect",
        "Nothing can be actioned from this stream: PaperBroker.available is a constant "
        "until a PPV-17 provider exists, so no threshold is built over it.",
    ),
)

_SIGNAL_BY_TYPE = {spec.type: spec for spec in ALERT_SIGNALS}

if len(_SIGNAL_BY_TYPE) != len(ALERT_SIGNALS):  # pragma: no cover - import-time invariant
    raise RuntimeError("alert signal catalog repeats a signal type")


def alert_signal_spec(alert_type: AlertType) -> AlertSignalSpec:
    """Return the closed policy for one signal type."""
    if type(alert_type) is not AlertType:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal type must be an exact AlertType")
    return _SIGNAL_BY_TYPE[alert_type]


def alert_id_for(alert_type: AlertType, *, host_id: str, run_id: RunId | None) -> str:
    """Derive the stable dedup key for one signal from identity alone.

    The key is a function of the signal type and the run/host identity it belongs
    to -- never of a timestamp, a counter or an arrival order. That is what makes
    a repeated observation continue one alert instead of opening another, and
    what makes re-occurrence after resolution a genuinely new alert.
    """
    spec = alert_signal_spec(alert_type)
    require_host_id(host_id)
    if spec.scope is AlertScope.HOST:
        if run_id is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "a host-scoped signal carries no run id")
        return f"{host_id}|{alert_type.value}"
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "a run-scoped signal requires an exact RunId")
    return f"{host_id}|{run_id.value}|{alert_type.value}"


@final
@dataclass(frozen=True, slots=True)
class AlertSignalReading:
    """One evaluation's honest statement about one signal.

    ``observation`` is bounded, control-character-free text describing what was
    actually seen. It is never a claim the evaluator could not support.
    """

    type: AlertType
    condition: SignalCondition
    observation: str

    def __post_init__(self) -> None:
        if type(self.type) is not AlertType:
            raise _fail(OutcomeCode.INVALID_TYPE, "reading type must be an exact AlertType")
        if type(self.condition) is not SignalCondition:
            raise _fail(
                OutcomeCode.INVALID_TYPE, "reading condition must be an exact SignalCondition"
            )
        _safe_text(self.observation, field="reading observation")


@final
@dataclass(frozen=True, slots=True)
class AlertThresholds:
    """PPV-06's own alerting policy. Deliberately not PPV-15's safety limits.

    These numbers decide when a human is told; they decide nothing about what may
    be sent to a venue. ``OperationalSafetyLimits`` is not imported, not reused
    and not re-derived here, because re-thresholding the safety authority from a
    monitoring module would create the second safety engine the plan forbids.
    """

    not_ready_seconds: float = 300.0
    backup_stale_seconds: float = 129_600.0
    disk_free_floor_bytes: int = 5 * 1024**3

    def __post_init__(self) -> None:
        for name in ("not_ready_seconds", "backup_stale_seconds"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value <= 0.0:
                raise _fail(OutcomeCode.OUT_OF_RANGE, f"{name} must be a positive finite float")
        if type(self.disk_free_floor_bytes) is not int or self.disk_free_floor_bytes < 0:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE, "disk_free_floor_bytes must be a non-negative int"
            )


DEFAULT_ALERT_THRESHOLDS = AlertThresholds()


@final
@dataclass(frozen=True, slots=True)
class PaperAlert:
    """One open or recovered alert, deduplicated by its stable identity key."""

    alert_id: str
    type: AlertType
    severity: AlertSeverity
    state: AlertState
    first_seen: datetime
    last_seen: datetime
    reason: str
    observation: str
    operator_action: str
    run_id: RunId | None
    account_id: str | None

    def __post_init__(self) -> None:
        if type(self.alert_id) is not str or _ALERT_ID.fullmatch(self.alert_id) is None:
            raise _fail(OutcomeCode.INVALID_TYPE, "alert id must be one bounded identity key")
        if type(self.type) is not AlertType:
            raise _fail(OutcomeCode.INVALID_TYPE, "alert type must be an exact AlertType")
        if type(self.severity) is not AlertSeverity:
            raise _fail(OutcomeCode.INVALID_TYPE, "alert severity must be an exact AlertSeverity")
        if type(self.state) is not AlertState:
            raise _fail(OutcomeCode.INVALID_TYPE, "alert state must be an exact AlertState")
        for name in ("reason", "observation", "operator_action"):
            _safe_text(getattr(self, name), field=f"alert {name}")
        for name in ("first_seen", "last_seen"):
            try:
                require_utc(getattr(self, name), field=f"alert {name}")
            except TimeValidationError as error:
                raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        if self.last_seen < self.first_seen:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "alert last_seen precedes first_seen")
        spec = alert_signal_spec(self.type)
        if self.severity is not spec.severity:
            raise _fail(OutcomeCode.CONFLICTING_ID, "alert severity must follow its signal")
        if self.reason != spec.reason:
            raise _fail(OutcomeCode.CONFLICTING_ID, "alert reason must follow its signal")
        if self.operator_action != spec.operator_action:
            raise _fail(OutcomeCode.CONFLICTING_ID, "alert operator action must follow its signal")
        if spec.scope is AlertScope.HOST:
            if self.run_id is not None or self.account_id is not None:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "a host-scoped alert carries no run identity"
                )
        elif type(self.run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "a run-scoped alert requires an exact RunId")
        if self.account_id is not None:
            _safe_text(self.account_id, field="alert account_id")

    def document(self) -> dict[str, Any]:
        """The canonical JSON-safe projection, as published and as re-read."""
        return {
            "alert_id": self.alert_id,
            "type": self.type.value,
            "severity": self.severity.value,
            "state": self.state.value,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "reason": self.reason,
            "observation": self.observation,
            "operator_action": self.operator_action,
            "run_id": None if self.run_id is None else self.run_id.value,
            "account_id": self.account_id,
        }


@final
@dataclass(frozen=True, slots=True)
class AlertSignalState:
    """The published state of one signal in one evaluation.

    ``condition_active_since`` is carried across evaluations so a threshold can
    be applied to a duration this module observed itself, rather than to a
    guess. It is ``None`` whenever the condition does not currently hold.
    """

    type: AlertType
    scope: AlertScope
    severity: AlertSeverity
    condition: SignalCondition
    active_since: datetime | None
    observation: str

    def __post_init__(self) -> None:
        spec = alert_signal_spec(self.type)
        if type(self.scope) is not AlertScope or self.scope is not spec.scope:
            raise _fail(OutcomeCode.CONFLICTING_ID, "signal state scope must follow its type")
        if type(self.severity) is not AlertSeverity or self.severity is not spec.severity:
            raise _fail(OutcomeCode.CONFLICTING_ID, "signal state severity must follow its type")
        if type(self.condition) is not SignalCondition:
            raise _fail(
                OutcomeCode.INVALID_TYPE, "signal condition must be an exact SignalCondition"
            )
        _safe_text(self.observation, field="signal observation")
        if self.condition is SignalCondition.ACTIVE:
            if self.active_since is None:
                raise _fail(OutcomeCode.CONFLICTING_ID, "an active condition carries when it began")
        elif self.active_since is not None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "only an active condition carries a start time; unavailable is not active",
            )
        if self.active_since is not None:
            try:
                require_utc(self.active_since, field="signal active_since")
            except TimeValidationError as error:
                raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error

    def document(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "scope": self.scope.value,
            "severity": self.severity.value,
            "condition": self.condition.value,
            "active_since": None if self.active_since is None else self.active_since.isoformat(),
            "observation": self.observation,
        }


@final
@dataclass(frozen=True, slots=True)
class PaperAlertStream:
    """One host's durable alert stream: open alerts, recovered alerts, readings.

    Every evaluation publishes a state for all nine signals, so the document
    itself distinguishes "nothing is wrong" from "nothing was observed".
    """

    host_id: str
    evaluated_at: datetime
    alerts: tuple[PaperAlert, ...]
    signals: tuple[AlertSignalState, ...]
    schema: str = PAPER_ALERT_STREAM_SCHEMA

    def __post_init__(self) -> None:
        require_host_id(self.host_id)
        if self.schema != PAPER_ALERT_STREAM_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "alert stream schema conflicts")
        try:
            require_utc(self.evaluated_at, field="evaluated_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        if type(self.alerts) is not tuple or any(
            type(alert) is not PaperAlert for alert in self.alerts
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "alerts must be one tuple of exact alerts")
        if type(self.signals) is not tuple or any(
            type(signal) is not AlertSignalState for signal in self.signals
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "signals must be one tuple of exact states")
        identifiers = tuple(alert.alert_id for alert in self.alerts)
        if identifiers != tuple(sorted(identifiers)):
            raise _fail(OutcomeCode.CONFLICTING_ID, "alerts must be in canonical key order")
        if len(set(identifiers)) != len(identifiers):
            raise _fail(OutcomeCode.CONFLICTING_ID, "each alert key must appear at most once")
        for alert in self.alerts:
            if alert.alert_id != alert_id_for(
                alert.type, host_id=self.host_id, run_id=alert.run_id
            ):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "an alert key must bind to its host and run"
                )
            if alert.last_seen > self.evaluated_at:
                raise _fail(OutcomeCode.OUT_OF_RANGE, "an alert was last seen after this reading")
        types = tuple(signal.type for signal in self.signals)
        if types != tuple(spec.type for spec in ALERT_SIGNALS):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "every evaluation must publish every signal in canonical order",
            )

    def document(self) -> dict[str, Any]:
        """The canonical JSON-safe projection, as published and as re-read."""
        return {
            "schema": self.schema,
            "host_id": self.host_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "alerts": [alert.document() for alert in self.alerts],
            "signals": [signal.document() for signal in self.signals],
        }

    def active_alerts(self) -> tuple[PaperAlert, ...]:
        """Only the currently open alerts; a recovered alert is history."""
        return tuple(alert for alert in self.alerts if alert.state is AlertState.ACTIVE)


def canonical_alert_stream_bytes(stream: PaperAlertStream) -> bytes:
    """Return the canonical durable document for one alert stream."""
    if type(stream) is not PaperAlertStream:
        raise _fail(OutcomeCode.INVALID_TYPE, "stream must be an exact alert stream")
    payload = canonical_json_bytes(stream.document())
    if not 1 <= len(payload) <= _MAX_FILE_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "alert stream exceeds the bounded file size")
    return payload


def _decode_stamp(document: dict[str, Any], key: str) -> datetime:
    text = document.get(key)
    if type(text) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"alert {key} must be a str")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"alert {key} is not a timestamp") from error
    try:
        return require_utc(parsed, field=key)
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _require_object(value: object, *, field: str, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be one string-keyed object")
    if set(value) != keys:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} carries unknown or missing fields")
    return value


def _decode_enum(value: object, expected: type[StrEnum], *, field: str) -> Any:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a str")
    try:
        return expected(value)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is not a known state") from error


def _decode_alert(value: object) -> PaperAlert:
    document = _require_object(value, field="alert", keys=_ALERT_KEYS)
    run_id = document["run_id"]
    if run_id is not None:
        try:
            run_id = RunId(run_id)
        except (TypeError, ValueError) as error:
            raise _fail(OutcomeCode.CONFLICTING_ID, "alert run_id conflicts") from error
    account_id = document["account_id"]
    if account_id is not None and type(account_id) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "alert account_id must be a str or null")
    for key in ("alert_id", "reason", "observation", "operator_action"):
        if type(document[key]) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, f"alert {key} must be a str")
    return PaperAlert(
        alert_id=document["alert_id"],
        type=_decode_enum(document["type"], AlertType, field="alert type"),
        severity=_decode_enum(document["severity"], AlertSeverity, field="alert severity"),
        state=_decode_enum(document["state"], AlertState, field="alert state"),
        first_seen=_decode_stamp(document, "first_seen"),
        last_seen=_decode_stamp(document, "last_seen"),
        reason=document["reason"],
        observation=document["observation"],
        operator_action=document["operator_action"],
        run_id=run_id,
        account_id=account_id,
    )


def _decode_signal(value: object) -> AlertSignalState:
    document = _require_object(value, field="signal", keys=_SIGNAL_KEYS)
    active_since = document["active_since"]
    if active_since is not None:
        if type(active_since) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "signal active_since must be a str or null")
        try:
            parsed = datetime.fromisoformat(active_since)
        except ValueError as error:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE, "signal active_since is not a timestamp"
            ) from error
        try:
            active_since = require_utc(parsed, field="active_since")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    if type(document["observation"]) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "signal observation must be a str")
    return AlertSignalState(
        type=_decode_enum(document["type"], AlertType, field="signal type"),
        scope=_decode_enum(document["scope"], AlertScope, field="signal scope"),
        severity=_decode_enum(document["severity"], AlertSeverity, field="signal severity"),
        condition=_decode_enum(document["condition"], SignalCondition, field="signal condition"),
        active_since=active_since,
        observation=document["observation"],
    )


def decode_alert_stream(document: object) -> PaperAlertStream:
    """Rebuild one alert stream from its durable document; malformed input fails closed."""
    payload = _require_object(document, field="alert stream", keys=_STREAM_KEYS)
    if payload["schema"] != PAPER_ALERT_STREAM_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "alert stream schema conflicts")
    alerts = payload["alerts"]
    signals = payload["signals"]
    if type(alerts) is not list or type(signals) is not list:
        raise _fail(OutcomeCode.INVALID_TYPE, "alert stream lists must be JSON arrays")
    return PaperAlertStream(
        host_id=require_host_id(payload["host_id"]),
        evaluated_at=_decode_stamp(payload, "evaluated_at"),
        alerts=tuple(_decode_alert(item) for item in alerts),
        signals=tuple(_decode_signal(item) for item in signals),
    )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def read_alert_stream(path: Path) -> PaperAlertStream:
    """Read one durable alert stream without mutation, following the status pattern.

    A missing, unreadable, non-canonical or malformed stream is reported as
    unavailable rather than as an empty alert set, because an empty set is a
    positive claim that nothing is wrong.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise PaperAlertUnavailable(
            OutcomeCode.INVALID_TYPE, "alert stream path must be an absolute Path"
        )
    descriptor: int | None = None
    try:
        if path.is_symlink():
            raise PaperAlertUnavailable(
                OutcomeCode.CONFLICTING_ID, "alert stream cannot be a symlink"
            )
        try:
            descriptor = os.open(path, _READ_FLAGS)
        except FileNotFoundError as error:
            raise PaperAlertStreamAbsent(
                OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
                "no alert stream has been published at this path",
            ) from error
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != _FILE_MODE
            or info.st_nlink != 1
            or not 1 <= info.st_size <= _MAX_FILE_BYTES
        ):
            raise PaperAlertUnavailable(
                OutcomeCode.CONFLICTING_ID, "alert stream must be one bounded private file"
            )
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != info.st_size
            or after.st_size != info.st_size
            or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise PaperAlertUnavailable(
                OutcomeCode.CONFLICTING_ID, "alert stream changed while being read"
            )
    except PaperAlertUnavailable:
        raise
    except OSError as error:
        raise PaperAlertUnavailable(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "alert stream is unavailable or unsafe",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        document = json.loads(
            payload, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
        stream = decode_alert_stream(document)
    except PaperAlertError as error:
        raise PaperAlertUnavailable(
            error.code, f"alert stream is not usable evidence: {error}"
        ) from error
    except (RecursionError, UnicodeError, ValueError) as error:
        raise PaperAlertUnavailable(
            OutcomeCode.CONFLICTING_ID, "alert stream is malformed JSON"
        ) from error
    if canonical_alert_stream_bytes(stream) != payload:
        raise PaperAlertUnavailable(
            OutcomeCode.CONFLICTING_ID, "alert stream is not canonical evidence"
        )
    return stream


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise _fail(
                OutcomeCode.DURABILITY_RESULT_WRITE_FAILED, "alert stream write made no progress"
            )
        remaining = remaining[written:]


def write_alert_stream(path: Path, stream: PaperAlertStream) -> None:
    """Publish one alert stream atomically: temp, fsync, rename, fsync parent.

    A reader therefore only ever sees a complete previous stream or a complete
    new one, never a torn document that would be read as "no alerts".
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise _fail(OutcomeCode.INVALID_TYPE, "alert stream path must be an absolute Path")
    if type(stream) is not PaperAlertStream:
        raise _fail(OutcomeCode.INVALID_TYPE, "stream must be an exact alert stream")
    payload = canonical_alert_stream_bytes(stream)
    parent = path.parent
    if parent.is_symlink():
        raise _fail(OutcomeCode.CONFLICTING_ID, "alert stream directory cannot be a symlink")
    try:
        parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    except OSError as error:
        raise _fail(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "alert stream directory could not be created",
        ) from error
    directory: int | None = None
    descriptor: int | None = None
    temp_name = f".{path.name}.{uuid4().hex}.tmp"
    try:
        directory = os.open(parent, _DIR_FLAGS)
        descriptor = os.open(temp_name, _CREATE_FLAGS, _FILE_MODE, dir_fd=directory)
        os.fchmod(descriptor, _FILE_MODE)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temp_name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except PaperAlertError:
        raise
    except OSError as error:
        raise _fail(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED, "alert stream could not be published"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            with suppress(OSError):
                os.unlink(temp_name, dir_fd=directory)
            os.close(directory)


def _previous_signal(
    previous: PaperAlertStream | None, alert_type: AlertType
) -> AlertSignalState | None:
    if previous is None:
        return None
    for signal in previous.signals:
        if signal.type is alert_type:
            return signal
    return None


def _prune_resolved(alerts: list[PaperAlert]) -> tuple[PaperAlert, ...]:
    """Drop the oldest recovered alerts only; an open alert is never discarded."""
    resolved = [alert for alert in alerts if alert.state is AlertState.RESOLVED]
    if len(resolved) <= _MAX_RESOLVED_ALERTS:
        return tuple(sorted(alerts, key=lambda alert: alert.alert_id))
    ordered = sorted(resolved, key=lambda alert: (alert.last_seen, alert.alert_id))
    keep = {alert.alert_id for alert in ordered[len(ordered) - _MAX_RESOLVED_ALERTS :]}
    return tuple(
        sorted(
            (
                alert
                for alert in alerts
                if alert.state is AlertState.ACTIVE or alert.alert_id in keep
            ),
            key=lambda alert: alert.alert_id,
        )
    )


def project_alert_stream(
    *,
    host_id: str,
    readings: tuple[AlertSignalReading, ...],
    observed_at: datetime,
    run_id: RunId | None,
    account_id: str | None,
    previous: PaperAlertStream | None = None,
    thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
) -> PaperAlertStream:
    """Fold one evaluation's readings into the durable stream.

    Dedup rules, exactly:

    * first occurrence of an alerting condition opens an ``ACTIVE`` alert with
      ``first_seen == last_seen == observed_at``;
    * a continued condition updates ``last_seen`` and the observation and leaves
      ``first_seen`` untouched;
    * a recovered condition becomes ``RESOLVED``, and
    * re-occurrence after resolution opens a *new* ``ACTIVE`` alert rather than
      silently flipping the resolved one back, so the history of one incident is
      never rewritten.

    An ``UNAVAILABLE`` reading opens nothing and resolves nothing: the signal is
    published as unavailable and whatever alert it had is left exactly as it was,
    because resolving an alert claims a recovery nobody observed.

    Alerts this evaluation does not speak for -- most importantly those belonging
    to a different run -- are carried forward untouched. Dropping an open alert
    because some other run was observed would be the same lie as never opening it.
    """
    require_host_id(host_id)
    try:
        observed_at = require_utc(observed_at, field="observed_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    if type(thresholds) is not AlertThresholds:
        raise _fail(OutcomeCode.INVALID_TYPE, "thresholds must be exact alerting policy")
    if type(readings) is not tuple or any(
        type(reading) is not AlertSignalReading for reading in readings
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "readings must be one tuple of exact readings")
    if tuple(reading.type for reading in readings) != tuple(spec.type for spec in ALERT_SIGNALS):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "an evaluation must read every signal exactly once, in canonical order",
        )
    if previous is not None:
        if type(previous) is not PaperAlertStream:
            raise _fail(OutcomeCode.INVALID_TYPE, "previous stream must be exact or None")
        if previous.host_id != host_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "the alert stream belongs to another host")
        if observed_at < previous.evaluated_at:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "an evaluation cannot travel backwards")
    if run_id is not None and type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId or None")

    by_id: dict[str, PaperAlert] = (
        {} if previous is None else {alert.alert_id: alert for alert in previous.alerts}
    )
    signals: list[AlertSignalState] = []

    for reading in readings:
        spec = alert_signal_spec(reading.type)
        # A host-scoped alert is one per host: it carries no run identity, and its
        # key must not vary with whichever run happened to be observed.
        scoped_run_id = run_id if spec.scope is AlertScope.RUN else None
        scoped_account_id = account_id if spec.scope is AlertScope.RUN else None
        run_known = spec.scope is AlertScope.HOST or run_id is not None
        alert_id = (
            alert_id_for(reading.type, host_id=host_id, run_id=scoped_run_id) if run_known else None
        )
        existing = None if alert_id is None else by_id.get(alert_id)
        prior = _previous_signal(previous, reading.type)

        if reading.condition is SignalCondition.UNAVAILABLE:
            # Unobservable is neither healthy nor active. Hold the alert exactly as
            # it was: resolving it would claim a recovery nobody observed.
            signals.append(
                AlertSignalState(
                    type=reading.type,
                    scope=spec.scope,
                    severity=spec.severity,
                    condition=SignalCondition.UNAVAILABLE,
                    active_since=None,
                    observation=reading.observation,
                )
            )
            continue

        active = reading.condition is SignalCondition.ACTIVE
        if not active:
            if (
                alert_id is not None
                and existing is not None
                and existing.state is AlertState.ACTIVE
            ):
                by_id[alert_id] = PaperAlert(
                    alert_id=alert_id,
                    type=reading.type,
                    severity=spec.severity,
                    state=AlertState.RESOLVED,
                    first_seen=existing.first_seen,
                    last_seen=observed_at,
                    reason=spec.reason,
                    observation=reading.observation,
                    operator_action=spec.operator_action,
                    run_id=scoped_run_id,
                    account_id=scoped_account_id,
                )
            signals.append(
                AlertSignalState(
                    type=reading.type,
                    scope=spec.scope,
                    severity=spec.severity,
                    condition=SignalCondition.INACTIVE,
                    active_since=None,
                    observation=reading.observation,
                )
            )
            continue

        active_since = (
            prior.active_since
            if prior is not None
            and prior.condition is SignalCondition.ACTIVE
            and prior.active_since is not None
            else observed_at
        )
        signals.append(
            AlertSignalState(
                type=reading.type,
                scope=spec.scope,
                severity=spec.severity,
                condition=SignalCondition.ACTIVE,
                active_since=active_since,
                observation=reading.observation,
            )
        )
        if alert_id is None:
            # The condition holds for a run this evaluation could not identify, so
            # no alert key can be derived and none is invented.
            continue
        if (
            reading.type is AlertType.NOT_READY_BEYOND_THRESHOLD
            and (observed_at - active_since).total_seconds() < thresholds.not_ready_seconds
        ):
            # The condition holds but has not held for long enough to be an alert.
            # Opening one below its own threshold would be the false alarm the
            # threshold exists to prevent; the active-since time is still published,
            # so the next evaluation can measure the same duration honestly.
            continue
        first_seen = (
            existing.first_seen
            if existing is not None and existing.state is AlertState.ACTIVE
            else observed_at
        )
        by_id[alert_id] = PaperAlert(
            alert_id=alert_id,
            type=reading.type,
            severity=spec.severity,
            state=AlertState.ACTIVE,
            first_seen=first_seen,
            last_seen=observed_at,
            reason=spec.reason,
            observation=reading.observation,
            operator_action=spec.operator_action,
            run_id=scoped_run_id,
            account_id=scoped_account_id,
        )

    return PaperAlertStream(
        host_id=host_id,
        evaluated_at=observed_at,
        alerts=_prune_resolved(list(by_id.values())),
        signals=tuple(signals),
    )


__all__ = [
    "ALERT_SIGNALS",
    "DEFAULT_ALERT_THRESHOLDS",
    "PAPER_ALERT_STREAM_SCHEMA",
    "AlertScope",
    "AlertSeverity",
    "AlertSignalReading",
    "AlertSignalSpec",
    "AlertSignalState",
    "AlertState",
    "AlertThresholds",
    "AlertType",
    "PaperAlert",
    "PaperAlertError",
    "PaperAlertStream",
    "PaperAlertStreamAbsent",
    "PaperAlertUnavailable",
    "SignalCondition",
    "alert_id_for",
    "alert_signal_spec",
    "canonical_alert_stream_bytes",
    "decode_alert_stream",
    "project_alert_stream",
    "read_alert_stream",
    "require_host_id",
    "write_alert_stream",
]
