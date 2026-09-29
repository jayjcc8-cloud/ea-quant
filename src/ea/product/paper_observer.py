"""Read-only Claude diagnostic observer for the local Paper runtime (WU-3).

This module lets Claude read a bounded package of *existing* Paper evidence and
return one structured diagnostic assessment. It is an observation surface and
nothing else:

* it never starts, stops, reloads or restarts anything -- launchd and the
  existing product CLI remain the only operators;
* it never places, cancels or resends an order, and it imports no execution,
  risk, portfolio, strategy or reconciliation authority;
* it never writes to the attempt, the backup root or the alert stream -- its
  only side effect is one ``claude`` child process and the document it returns;
* it never evaluates or writes a ``PaperGateVerdict``: the deterministic gate
  is WU-2's pure code, and a PASS / FAIL / INVALID it produces is unchanged by
  whatever Claude says.

The observer input comes from the read models that already exist:
:func:`ea.product.paper_session.read_paper_status` (health projection,
writer lease, reconciliation and risk facts), :func:`ea.product.paper_alerts
.read_alert_stream`, :func:`ea.product.paper_backup.latest_verified_paper_backup`
summarised with WU-2's own :func:`ea.product.paper_evidence.summarize_backup_state`,
and a bounded tail of the attempt's operational log. The evidence document is a
closed schema built by a pure function of those already-read documents, so the
only thing Claude ever sees is the intended evidence.

Claude runs headless (``claude -p``), with no tools, exactly one turn, a fixed
system prompt and a hard wall-clock and output bound. When Claude is missing,
unauthenticated, times out, fails or returns malformed output, the observation
still succeeds: the returned document reports ``observer_status: unavailable``
with a bounded reason, and the Paper runtime itself is never reported as failed
because the observer failed. There is no fallback model and no second verdict
system: unavailable is unavailable.

The assessment document is advisory to a human operator. Suggested actions are
instructions to that operator, never authorizations, and a deterministic FAIL
or INVALID stays FAIL or INVALID regardless of Claude's opinion.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, final

from ea.core.outcomes import OutcomeCode
from ea.core.time import TimeValidationError, require_utc
from ea.product.paper_alerts import (
    PaperAlertStream,
    PaperAlertUnavailable,
    read_alert_stream,
)
from ea.product.paper_backup import (
    PaperBackupError,
    latest_verified_paper_backup,
)
from ea.product.paper_evidence import (
    DEFAULT_BACKUP_FRESHNESS_SECONDS,
    BackupSummary,
    summarize_backup_state,
)
from ea.product.paper_session import read_paper_status

OBSERVER_EVIDENCE_SCHEMA = "ea.observer-evidence.v1"
OBSERVER_ASSESSMENT_SCHEMA = "ea.observer-assessment.v1"
CLAUDE_ASSESSMENT_SCHEMA = "ea.claude-assessment.v1"
OBSERVER_NAME = "claude"

# The observer's own bounds. They are prompt/budget bounds, not safety limits:
# nothing here thresholds what may be traded.
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_LOG_LINES = 200
_MAX_TEXT_BYTES = 512
_MAX_REFERENCE_BYTES = 128
_MAX_LIST_ITEMS = 20
_MAX_CLAUDE_OUTPUT_BYTES = 65_536
_MAX_LOG_BYTES = 16_384
_MAX_OPERATIONAL_LOG_LINE_BYTES = 512
_MAX_REASON_BYTES = 512

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

_STATUS_KEYS = frozenset({"schema", "run_id", "manifest_sha256", "state", "lease_held"})
_ASSESSMENT_KEYS = frozenset(
    {
        "schema",
        "summary",
        "severity",
        "observations",
        "anomalies",
        "evidence_refs",
        "root_causes",
        "operator_actions",
    }
)

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class PaperObserverError(ValueError):
    """Closed contract failure at the observer boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("observer errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


class PaperObserverUnavailable(PaperObserverError):
    """The Claude side could not produce a usable assessment.

    This is an observer degradation, never a Paper runtime failure: the runtime
    state stays exactly as the evidence observed it, and the caller reports the
    reason instead of an assessment.
    """


def _fail(code: OutcomeCode, message: str) -> PaperObserverError:
    return PaperObserverError(code, message)


class ObserverStatus(StrEnum):
    """Whether the observer produced an assessment for this observation."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ObserverSeverity(StrEnum):
    """The bounded severity Claude may report. Closed and total."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


def _bounded_text(value: object, *, field: str, maximum: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str or not value.strip():
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a non-empty str")
    if len(value.encode("utf-8")) > maximum:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} exceeds the bounded size")
    return value


def _reason_text(value: object, *, field: str) -> str:
    """A degradation reason is bounded but may be short; it is still non-empty text."""
    return _bounded_text(value, field=field, maximum=_MAX_REASON_BYTES)


# ---------------------------------------------------------------------------
# evidence inputs (already-read documents, never new readings)
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class AlertEvidence:
    """One alert-stream observation: the read stream, or the explicit unavailable marker.

    A missing or unreadable stream is ``available=False`` with a reason, never
    silently an empty -- and therefore apparently healthy -- alert set.
    """

    available: bool
    stream: PaperAlertStream | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available:
            if type(self.stream) is not PaperAlertStream or self.reason is not None:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "an available alert evidence carries its stream"
                )
        elif self.stream is not None or self.reason is None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "an unavailable alert evidence names its reason"
            )
        if self.reason is not None:
            _reason_text(self.reason, field="alert evidence reason")


@final
@dataclass(frozen=True, slots=True)
class BackupEvidence:
    """One backup observation: the WU-2 summary of the newest verified backup.

    ``backup_state`` comes from :func:`summarize_backup_state`, the same summary
    the deterministic gate consumes; the observer invents no second freshness
    judgment.
    """

    available: bool
    backup_state: BackupSummary | None = None
    created_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available:
            if type(self.backup_state) is not BackupSummary or self.reason is not None:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "an available backup evidence carries its state"
                )
            if self.created_at is not None:
                try:
                    require_utc(self.created_at, field="backup created_at")
                except TimeValidationError as error:
                    raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        elif self.backup_state is not None or self.created_at is not None or self.reason is None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "an unavailable backup evidence names its reason"
            )
        if self.reason is not None:
            _reason_text(self.reason, field="backup evidence reason")


@final
@dataclass(frozen=True, slots=True)
class OperationalLogEvidence:
    """One bounded operational-log tail, or the explicit unavailable marker.

    A line longer than the per-line bound is elided and counted, never rewritten:
    the observer does not fabricate log content.
    """

    available: bool
    lines: tuple[str, ...] = ()
    truncated: bool = False
    elided_lines: int = 0
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.truncated) is not bool:
            raise _fail(OutcomeCode.INVALID_TYPE, "log truncated must be an exact bool")
        if type(self.elided_lines) is not int or self.elided_lines < 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "log elided_lines must be a non-negative int")
        if self.available:
            if self.reason is not None or any(type(line) is not str for line in self.lines):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "an available log evidence carries plain lines"
                )
            for line in self.lines:
                if not line or len(line.encode("utf-8")) > _MAX_OPERATIONAL_LOG_LINE_BYTES:
                    raise _fail(
                        OutcomeCode.OUT_OF_RANGE, "log lines must be bounded non-empty text"
                    )
            if self.truncated is False and self.elided_lines:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "an elided line marks the tail as truncated"
                )
        else:
            if self.lines or self.truncated or self.elided_lines or self.reason is None:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "an unavailable log evidence names only its reason"
                )
        if self.reason is not None:
            _reason_text(self.reason, field="log evidence reason")


# ---------------------------------------------------------------------------
# evidence document
# ---------------------------------------------------------------------------


def _require_object(value: object, *, field: str, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be one string-keyed object")
    if set(value) != keys:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} carries unknown or missing fields")
    return value


def _evidence_document(observed_at: datetime, evidence: dict[str, Any]) -> dict[str, Any]:
    """The closed evidence document, keyed exactly, with the capture instant."""
    return {"schema": OBSERVER_EVIDENCE_SCHEMA, "observed_at": observed_at.isoformat(), **evidence}


def build_observer_evidence(
    *,
    observed_at: datetime,
    paper_status: dict[str, Any],
    alerts: AlertEvidence,
    backup: BackupEvidence,
    operational_log: OperationalLogEvidence,
) -> dict[str, Any]:
    """Build the bounded, read-only evidence package Claude will consume.

    This is a pure function: it performs no I/O and re-validates the identity
    keys of the already-read status document, so the package cannot carry more
    or less than the caller's evidence. The health projection is embedded
    exactly as ``read_paper_status`` published it, with liveness and currency
    already re-observed from the live writer lease.
    """
    try:
        observed_at = require_utc(observed_at, field="observed_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    if type(alerts) is not AlertEvidence:
        raise _fail(OutcomeCode.INVALID_TYPE, "alerts must be an exact AlertEvidence")
    if type(backup) is not BackupEvidence:
        raise _fail(OutcomeCode.INVALID_TYPE, "backup must be an exact BackupEvidence")
    if type(operational_log) is not OperationalLogEvidence:
        raise _fail(
            OutcomeCode.INVALID_TYPE, "operational_log must be an exact OperationalLogEvidence"
        )
    if type(paper_status) is not dict or not all(type(key) is str for key in paper_status):
        raise _fail(OutcomeCode.INVALID_TYPE, "paper status must be one string-keyed object")
    for key in _STATUS_KEYS:
        if key not in paper_status:
            raise _fail(OutcomeCode.CONFLICTING_ID, f"paper status carries no {key}")
    if paper_status.get("schema") != "ea.local-paper-status.v1":
        raise _fail(OutcomeCode.CONFLICTING_ID, "paper status schema conflicts")
    if type(paper_status["run_id"]) is not str or not paper_status["run_id"].strip():
        raise _fail(OutcomeCode.INVALID_TYPE, "paper status run_id must be a non-empty str")
    if type(paper_status["lease_held"]) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "paper status lease_held must be an exact bool")
    health = paper_status.get("health")
    if health is not None and type(health) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "paper status health must be an object or null")

    if alerts.available:
        assert alerts.stream is not None
        alert_document: dict[str, Any] = {
            "available": True,
            "host_id": alerts.stream.host_id,
            "evaluated_at": alerts.stream.evaluated_at.isoformat(),
            "active_alerts": [alert.document() for alert in alerts.stream.active_alerts()],
            "resolved_alert_count": len(alerts.stream.alerts) - len(alerts.stream.active_alerts()),
            "signals": [signal.document() for signal in alerts.stream.signals],
        }
    else:
        alert_document = {"available": False, "reason": alerts.reason}

    if backup.available:
        if backup.backup_state is None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "an available backup evidence carries its state"
            )
        backup_document: dict[str, Any] = {
            "available": True,
            "backup_state": backup.backup_state.value,
            "created_at": None if backup.created_at is None else backup.created_at.isoformat(),
        }
    else:
        backup_document = {"available": False, "reason": backup.reason}

    if operational_log.available:
        log_document: dict[str, Any] = {
            "available": True,
            "lines": list(operational_log.lines),
            "truncated": operational_log.truncated,
            "elided_lines": operational_log.elided_lines,
        }
    else:
        log_document = {"available": False, "reason": operational_log.reason}

    return _evidence_document(
        observed_at,
        {
            "paper_status": paper_status,
            "alert_stream": alert_document,
            "backup": backup_document,
            "operational_log": log_document,
        },
    )


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


def _require_bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if type(value) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{key} must be an exact bool")
    return value


def _require_object_list(value: object, *, field: str) -> None:
    if type(value) is not list or any(
        type(item) is not dict or any(type(key) is not str for key in item) for item in value
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be one list of string-keyed objects")


def decode_observer_evidence(document: object) -> dict[str, Any]:
    """Validate one evidence document against the closed schema; malformed input fails closed.

    The alert and backup entries are bounded projections, so this decoder
    validates their shape and values exactly rather than rebuilding the live
    objects they were projected from.
    """
    payload = _require_object(
        document,
        field="observer evidence",
        keys=frozenset(
            {"schema", "observed_at", "paper_status", "alert_stream", "backup", "operational_log"}
        ),
    )
    if payload["schema"] != OBSERVER_EVIDENCE_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observer evidence schema conflicts")
    _require_stamp(payload.get("observed_at"), field="evidence observed_at")

    status = payload.get("paper_status")
    if type(status) is not dict or not all(key in status for key in _STATUS_KEYS):
        raise _fail(
            OutcomeCode.CONFLICTING_ID, "paper status carries unknown or missing identity fields"
        )
    if status["schema"] != "ea.local-paper-status.v1":
        raise _fail(OutcomeCode.CONFLICTING_ID, "paper status schema conflicts")
    if type(status.get("run_id")) is not str or not status["run_id"].strip():
        raise _fail(OutcomeCode.INVALID_TYPE, "paper status run_id must be a non-empty str")
    _require_bool(status, "lease_held")
    health = status.get("health")
    if health is not None and type(health) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "paper status health must be an object or null")

    alerts = payload.get("alert_stream")
    if type(alerts) is not dict or "available" not in alerts:
        raise _fail(OutcomeCode.INVALID_TYPE, "alert evidence must be one object with availability")
    if _require_bool(alerts, "available"):
        _require_object(
            alerts,
            field="alert evidence",
            keys=frozenset(
                {
                    "available",
                    "host_id",
                    "evaluated_at",
                    "active_alerts",
                    "resolved_alert_count",
                    "signals",
                }
            ),
        )
        _bounded_text(alerts.get("host_id"), field="alert evidence host_id")
        _require_stamp(alerts.get("evaluated_at"), field="alert evidence evaluated_at")
        _require_object_list(alerts.get("active_alerts"), field="alert evidence active_alerts")
        _require_object_list(alerts.get("signals"), field="alert evidence signals")
        if (
            type(alerts.get("resolved_alert_count")) is not int
            or alerts["resolved_alert_count"] < 0
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "resolved_alert_count must be a non-negative int")
    else:
        _require_object(alerts, field="alert evidence", keys=frozenset({"available", "reason"}))
        _reason_text(alerts.get("reason"), field="alert evidence reason")

    backup = payload.get("backup")
    if type(backup) is not dict or "available" not in backup:
        raise _fail(
            OutcomeCode.INVALID_TYPE, "backup evidence must be one object with availability"
        )
    if _require_bool(backup, "available"):
        _require_object(
            backup,
            field="backup evidence",
            keys=frozenset({"available", "backup_state", "created_at"}),
        )
        if type(backup.get("backup_state")) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "backup_state must be a str")
        try:
            BackupSummary(backup["backup_state"])
        except ValueError:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "backup_state is not a known value") from None
        if backup.get("created_at") is not None:
            _require_stamp(backup.get("created_at"), field="backup created_at")
    else:
        _require_object(backup, field="backup evidence", keys=frozenset({"available", "reason"}))
        _reason_text(backup.get("reason"), field="backup evidence reason")

    log = payload.get("operational_log")
    if type(log) is not dict or "available" not in log:
        raise _fail(OutcomeCode.INVALID_TYPE, "log evidence must be one object with availability")
    if _require_bool(log, "available"):
        _require_object(
            log,
            field="log evidence",
            keys=frozenset({"available", "lines", "truncated", "elided_lines"}),
        )
        lines = log.get("lines")
        if type(lines) is not list or any(type(line) is not str for line in lines):
            raise _fail(OutcomeCode.INVALID_TYPE, "log lines must be one list of strings")
        for line in lines:
            if not line or len(line.encode("utf-8")) > _MAX_OPERATIONAL_LOG_LINE_BYTES:
                raise _fail(OutcomeCode.OUT_OF_RANGE, "log lines must be bounded non-empty text")
        _require_bool(log, "truncated")
        if type(log.get("elided_lines")) is not int or log["elided_lines"] < 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "elided_lines must be a non-negative int")
        if log["truncated"] is False and log["elided_lines"]:
            raise _fail(OutcomeCode.CONFLICTING_ID, "an elided line marks the tail as truncated")
    else:
        _require_object(log, field="log evidence", keys=frozenset({"available", "reason"}))
        _reason_text(log.get("reason"), field="log evidence reason")
    return payload


# ---------------------------------------------------------------------------
# Claude prompt and invocation
# ---------------------------------------------------------------------------

# The fixed role boundary. It is a code constant, so an operator or a hostile
# prompt cannot move it, and it is the whole instruction Claude receives apart
# from the evidence document itself.
_CLAUDE_SYSTEM_PROMPT = """\
You are the read-only diagnostic observer of a Mac-local simulated Paper \
trading runtime ("EA").

You have no tools, no filesystem access and no ability to act. You receive one \
bounded evidence document produced by the runtime's own read-only surfaces. \
Your only job is to return one JSON diagnostic assessment about that evidence.

Rules:
1. Respond with exactly one JSON object and nothing else (no markdown, no \
prose around it), with schema "ea.claude-assessment.v1" and exactly these \
fields: "schema", "summary", "severity", "observations", "anomalies", \
"evidence_refs", "root_causes", "operator_actions".
2. "schema" is exactly "ea.claude-assessment.v1".
3. "summary" is one short sentence: the current state of the observed runtime \
in plain language.
4. "severity" is one of "info", "warning", "critical".
5. "observations", "anomalies", "evidence_refs", "root_causes" and \
"operator_actions" are arrays of strings, each at most 20 items, each string \
at most 512 characters. Use empty arrays when you have nothing to say.
6. Deterministic states in the evidence are authoritative: health reason \
codes, trade_permitted, the kill switch, reconciliation state, writer lease \
and any PASS / FAIL / INVALID verdict are facts. You may explain them; you \
must never reinterpret, soften or contradict them.
7. Every suggested action in "operator_actions" is advisory to a human \
operator. Phrase actions as instructions to that operator (for example \
"Confirm the halt is intended..."), never as things you or any program may do. \
You cannot start, stop, restart, place orders, change limits, repair state, \
restore backups or change any verdict.
8. If the evidence is missing a source (available: false), say so rather than \
assuming it is healthy. Do not report the Paper runtime as failed merely \
because a source or the observer is limited."""


def build_observer_prompt(evidence: dict[str, Any]) -> str:
    """The whole prompt: the fixed system boundary plus exactly the evidence document.

    This is a pure function, so tests can prove Claude receives only the
    intended evidence and nothing else.
    """
    payload = canonical_evidence_bytes(evidence)
    return (
        _CLAUDE_SYSTEM_PROMPT
        + "\n\nThe bounded read-only evidence document follows.\n\n"
        + payload.decode("ascii")
    )


def canonical_evidence_bytes(evidence: dict[str, Any]) -> bytes:
    """Return the canonical durable form of one evidence document."""
    if type(evidence) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "evidence must be one object")
    if evidence.get("schema") != OBSERVER_EVIDENCE_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "evidence schema conflicts")
    try:
        return json.dumps(
            evidence,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError, UnicodeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "evidence is not serializable") from error


def invoke_claude(
    prompt: str,
    *,
    claude_bin: Path,
    timeout_seconds: float,
) -> str:
    """Run one headless, tool-less Claude turn and return its raw result text.

    The argv is a fixed list with no shell, so nothing in the evidence can
    become a command. The child runs with the caller's environment (that is
    where Claude authentication lives) and gets exactly one turn, no tools, and
    a hard wall-clock and output bound. Every failure mode -- missing binary,
    authentication failure, non-zero exit, timeout, oversized output -- is a
    bounded :class:`PaperObserverUnavailable`, never a Paper failure.
    """
    if not isinstance(claude_bin, Path):
        raise _fail(OutcomeCode.INVALID_TYPE, "claude_bin must be an exact Path")
    if type(timeout_seconds) is not float or not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "claude timeout must be a positive finite float")
    if type(prompt) is not str or not prompt:
        raise _fail(OutcomeCode.INVALID_TYPE, "prompt must be non-empty text")
    try:
        completed = subprocess.run(
            [str(claude_bin), "-p", "--output-format", "json", "--max-turns", "1"],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PaperObserverUnavailable(
            OutcomeCode.OUT_OF_RANGE, "claude did not answer within the observer timeout"
        ) from None
    except (OSError, ValueError) as error:
        raise PaperObserverUnavailable(
            OutcomeCode.OUT_OF_RANGE, f"claude could not be run: {type(error).__name__}"
        ) from error
    if len(completed.stdout.encode("utf-8")) > _MAX_CLAUDE_OUTPUT_BYTES:
        raise PaperObserverUnavailable(
            OutcomeCode.OUT_OF_RANGE, "claude output exceeded the bounded observer size"
        )
    if completed.returncode != 0:
        detail = (completed.stderr or "no detail").strip()
        raise PaperObserverUnavailable(
            OutcomeCode.OUT_OF_RANGE,
            f"claude exited {completed.returncode}: {_bounded_reason(detail)}",
        )
    envelope = _decode_envelope(completed.stdout)
    if envelope.get("is_error") is True:
        raw_detail = envelope.get("result")
        detail = raw_detail if type(raw_detail) is str else "no detail"
        raise PaperObserverUnavailable(
            OutcomeCode.CONFLICTING_ID, f"claude reported an error: {_bounded_reason(detail)}"
        )
    result = envelope.get("result")
    if type(result) is not str or not result.strip():
        raise PaperObserverUnavailable(
            OutcomeCode.CONFLICTING_ID, "claude returned no assessment text"
        )
    return result


def _bounded_reason(text: str) -> str:
    """A bounded copy of arbitrary failure text; never lets it inflate the document."""
    encoded = text.encode("utf-8")[:_MAX_REASON_BYTES]
    return encoded.decode("utf-8", "ignore")


def _decode_envelope(stdout: str) -> dict[str, Any]:
    """Decode the ``claude --output-format json`` envelope, failing closed."""
    try:
        document = json.loads(
            stdout,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (RecursionError, UnicodeError, ValueError) as error:
        raise PaperObserverUnavailable(
            OutcomeCode.CONFLICTING_ID, "claude output is not a usable result envelope"
        ) from error
    if type(document) is not dict or document.get("type") != "result":
        raise PaperObserverUnavailable(
            OutcomeCode.CONFLICTING_ID, "claude output is not a result envelope"
        )
    if type(document.get("is_error")) is not bool:
        raise PaperObserverUnavailable(
            OutcomeCode.CONFLICTING_ID, "claude result envelope carries no error flag"
        )
    return document


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


# ---------------------------------------------------------------------------
# Claude assessment decode
# ---------------------------------------------------------------------------


def decode_claude_assessment(text: str) -> dict[str, Any]:
    """Decode Claude's JSON assessment strictly; malformed or oversized fails closed."""
    if type(text) is not str or not text:
        raise PaperObserverUnavailable(OutcomeCode.INVALID_TYPE, "claude returned no assessment")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (RecursionError, UnicodeError, ValueError) as error:
        raise PaperObserverUnavailable(
            OutcomeCode.CONFLICTING_ID, "claude assessment is not usable JSON"
        ) from error
    try:
        payload = _require_object(document, field="claude assessment", keys=_ASSESSMENT_KEYS)
        if payload["schema"] != CLAUDE_ASSESSMENT_SCHEMA:
            raise PaperObserverUnavailable(
                OutcomeCode.CONFLICTING_ID, "claude assessment schema conflicts"
            )
        _bounded_text(payload["summary"], field="assessment summary")
        if type(payload["severity"]) is not str:
            raise PaperObserverUnavailable(
                OutcomeCode.INVALID_TYPE, "claude assessment severity must be a str"
            )
        try:
            ObserverSeverity(payload["severity"])
        except ValueError:
            raise PaperObserverUnavailable(
                OutcomeCode.OUT_OF_RANGE, "claude assessment severity is not a known value"
            ) from None
        for field in (
            "observations",
            "anomalies",
            "evidence_refs",
            "root_causes",
            "operator_actions",
        ):
            items = payload[field]
            if type(items) is not list or any(type(item) is not str for item in items):
                raise PaperObserverUnavailable(
                    OutcomeCode.INVALID_TYPE,
                    f"claude assessment {field} must be one list of strings",
                )
            if len(items) > _MAX_LIST_ITEMS:
                raise PaperObserverUnavailable(
                    OutcomeCode.OUT_OF_RANGE,
                    f"claude assessment {field} exceeds the bounded item count",
                )
            for item in items:
                _bounded_text(item, field=f"assessment {field} item", maximum=_MAX_TEXT_BYTES)
            if field == "evidence_refs":
                for item in items:
                    if len(item.encode("utf-8")) > _MAX_REFERENCE_BYTES:
                        raise PaperObserverUnavailable(
                            OutcomeCode.OUT_OF_RANGE,
                            "claude assessment evidence_refs exceed the bounded size",
                        )
        return payload
    except PaperObserverUnavailable:
        raise
    except PaperObserverError as error:
        raise PaperObserverUnavailable(
            error.code, f"claude assessment is not usable: {error}"
        ) from error


# ---------------------------------------------------------------------------
# assessment document
# ---------------------------------------------------------------------------


def _evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    """The bounded human-readable digest of what the observer saw.

    Every value is copied from the evidence document, never re-read or
    re-judged, so the summary cannot drift from what Claude was shown.
    """
    status = evidence["paper_status"]
    health = status.get("health")
    alerts = evidence["alert_stream"]
    backup = evidence["backup"]
    log = evidence["operational_log"]
    summary: dict[str, Any] = {
        "run_id": status["run_id"],
        "state": status["state"],
        "lease_held": status["lease_held"],
        "runtime_ready": None if not isinstance(health, dict) else health.get("runtime_ready"),
        "reconciliation_state": None
        if not isinstance(health, dict)
        else health.get("reconciliation_state"),
        "kill_switch_state": None
        if not isinstance(health, dict)
        else health.get("kill_switch_state"),
        "trade_permitted": None if not isinstance(health, dict) else health.get("trade_permitted"),
        "active_alerts": 0 if not alerts["available"] else len(alerts["active_alerts"]),
        "backup_state": None if not backup["available"] else backup["backup_state"],
        "operational_log_lines": 0 if not log["available"] else len(log["lines"]),
        "sources": {
            "paper_status": True,
            "alert_stream": alerts["available"],
            "backup": backup["available"],
            "operational_log": log["available"],
        },
    }
    return summary


def assess_observation(
    evidence: dict[str, Any],
    *,
    claude_assessment: dict[str, Any] | None,
    unavailable_reason: str | None,
) -> dict[str, Any]:
    """Fold one evidence package and one Claude assessment into the output document.

    The deterministic gate verdict is deliberately not a field here: the
    observer carries no verdict and can therefore never contradict one. When
    Claude produced nothing usable, the document reports
    ``observer_status: unavailable`` with the bounded reason and the evidence
    summary still stands on its own.
    """
    if type(evidence) is not dict or evidence.get("schema") != OBSERVER_EVIDENCE_SCHEMA:
        raise _fail(OutcomeCode.INVALID_TYPE, "evidence must be one observer evidence document")
    observed_at = evidence["observed_at"]
    if claude_assessment is None and unavailable_reason is None:
        raise _fail(OutcomeCode.CONFLICTING_ID, "an unavailable observer names its reason")
    if claude_assessment is not None and unavailable_reason is not None:
        raise _fail(OutcomeCode.CONFLICTING_ID, "an available observer carries no reason")
    if claude_assessment is not None:
        if type(claude_assessment) is not dict:
            raise _fail(OutcomeCode.INVALID_TYPE, "claude assessment must be one object")
        if claude_assessment.get("schema") != CLAUDE_ASSESSMENT_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "claude assessment schema conflicts")
        status = ObserverStatus.AVAILABLE
        reason: str | None = None
    else:
        status = ObserverStatus.UNAVAILABLE
        reason = _reason_text(unavailable_reason, field="observer unavailable reason")
    return {
        "schema": OBSERVER_ASSESSMENT_SCHEMA,
        "observed_at": observed_at,
        "observer": OBSERVER_NAME,
        "observer_status": status.value,
        "unavailable_reason": reason,
        "evidence": _evidence_summary(evidence),
        "assessment": claude_assessment,
    }


def decode_observer_assessment(document: object) -> dict[str, Any]:
    """Rebuild one observer assessment from its document; malformed input fails closed."""
    payload = _require_object(
        document,
        field="observer assessment",
        keys=frozenset(
            {
                "schema",
                "observed_at",
                "observer",
                "observer_status",
                "unavailable_reason",
                "evidence",
                "assessment",
            }
        ),
    )
    if payload["schema"] != OBSERVER_ASSESSMENT_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observer assessment schema conflicts")
    _bounded_text(payload.get("observed_at"), field="assessment observed_at")
    if payload.get("observer") != OBSERVER_NAME:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observer name conflicts")
    if type(payload.get("observer_status")) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "observer_status must be a str")
    try:
        status = ObserverStatus(payload["observer_status"])
    except ValueError:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "observer_status is not a known value") from None
    if payload.get("assessment") is None:
        if status is not ObserverStatus.UNAVAILABLE:
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "only an unavailable observer lacks an assessment"
            )
        if type(payload.get("unavailable_reason")) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "an unavailable observer carries a reason")
        _reason_text(payload["unavailable_reason"], field="observer unavailable reason")
    else:
        if status is not ObserverStatus.AVAILABLE:
            raise _fail(OutcomeCode.CONFLICTING_ID, "an available observer carries an assessment")
        if payload.get("unavailable_reason") is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "an available observer carries no reason")
        if type(payload["assessment"]) is not dict:
            raise _fail(OutcomeCode.INVALID_TYPE, "assessment must be one object")
    if type(payload.get("evidence")) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "evidence summary must be one object")
    return payload


# ---------------------------------------------------------------------------
# operational log tail (read-only)
# ---------------------------------------------------------------------------


def _read_operational_log_tail(
    run_dir: Path,
    *,
    max_lines: int,
) -> OperationalLogEvidence:
    """Read a bounded tail of the attempt's operational log, without mutation.

    Only the last ``max_lines`` lines and the last ``_MAX_LOG_BYTES`` bytes are
    ever read. A missing file is an explicit unavailable marker, not an error
    and never an "empty log" claim. Oversized lines are elided and counted,
    never rewritten, and a partial first line (from tailing mid-file) is
    dropped.
    """
    if type(max_lines) is not int or max_lines < 1:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "max_lines must be a positive int")
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise _fail(OutcomeCode.INVALID_TYPE, "run_dir must be an absolute Path")
    path = run_dir / "outputs" / "operational.jsonl"
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, _READ_FLAGS)
        except FileNotFoundError:
            return OperationalLogEvidence(
                available=False,
                reason="no operational log has been published at outputs/operational.jsonl",
            )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return OperationalLogEvidence(
                available=False, reason="operational log is not one usable regular file"
            )
        if info.st_size == 0:
            # A published but empty log is honest evidence: zero lines observed.
            return OperationalLogEvidence(available=True)
        tail_bytes = min(info.st_size, _MAX_LOG_BYTES)
        offset = info.st_size - tail_bytes
        payload = os.pread(descriptor, tail_bytes, offset)
        if len(payload) != tail_bytes:
            return OperationalLogEvidence(
                available=False, reason="operational log changed while being read"
            )
    except OSError as error:
        return OperationalLogEvidence(
            available=False, reason=f"operational log is unavailable: {type(error).__name__}"
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return OperationalLogEvidence(
            available=False, reason="operational log is not decodable text"
        )
    lines = text.split("\n")
    if offset > 0 and lines:
        lines = lines[1:]
    truncated = offset > 0
    kept: list[str] = []
    elided = 0
    for line in lines:
        if not line:
            continue
        if len(line.encode("utf-8")) > _MAX_OPERATIONAL_LOG_LINE_BYTES:
            elided += 1
            continue
        kept.append(line)
    if elided:
        truncated = True
    if len(kept) > max_lines:
        truncated = True
        kept = kept[-max_lines:]
    return OperationalLogEvidence(
        available=True,
        lines=tuple(kept),
        truncated=truncated,
        elided_lines=elided,
    )


# ---------------------------------------------------------------------------
# one observation
# ---------------------------------------------------------------------------


def run_observer(
    *,
    run_dir: Path,
    alert_stream_path: Path | None = None,
    backup_root: Path | None = None,
    claude_bin: Path = Path("claude"),
    timeout_seconds: float = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    max_log_lines: int = DEFAULT_MAX_LOG_LINES,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Run one on-demand observation: read evidence, ask Claude, fold the result.

    Only existing read models are called; nothing here mutates any runtime,
    attempt, backup or alert state. A Paper status that cannot be read fails
    the observation (the caller reports it the way ``ea paper status`` does),
    while every Claude-side failure degrades only the observer and still
    returns the evidence summary.
    """
    observed_at = datetime.now(UTC) if observed_at is None else observed_at
    try:
        observed_at = require_utc(observed_at, field="observed_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    # The status read is pinned to the observation instant, so the embedded
    # health overlay (which ages the projection against "now") is coherent
    # with the evidence document's own captured_at.
    status = read_paper_status(run_dir, now=observed_at)

    if alert_stream_path is None:
        alerts = AlertEvidence(available=False, reason="no alert stream path was supplied")
    else:
        try:
            stream = read_alert_stream(alert_stream_path)
        except PaperAlertUnavailable as error:
            alerts = AlertEvidence(available=False, reason=str(error))
        else:
            alerts = AlertEvidence(available=True, stream=stream)

    if backup_root is None:
        backup: BackupEvidence = BackupEvidence(
            available=False, reason="no backup root was supplied"
        )
    else:
        try:
            verified = latest_verified_paper_backup(backup_root)
        except PaperBackupError as error:
            backup = BackupEvidence(available=False, reason=str(error))
        else:
            backup = BackupEvidence(
                available=True,
                backup_state=summarize_backup_state(
                    None if verified is None else verified.created_at,
                    captured_at=observed_at,
                    freshness_seconds=DEFAULT_BACKUP_FRESHNESS_SECONDS,
                ),
                created_at=None if verified is None else verified.created_at,
            )

    operational_log = _read_operational_log_tail(run_dir, max_lines=max_log_lines)
    evidence = build_observer_evidence(
        observed_at=observed_at,
        paper_status=status,
        alerts=alerts,
        backup=backup,
        operational_log=operational_log,
    )
    prompt = build_observer_prompt(evidence)
    try:
        claude_text = invoke_claude(prompt, claude_bin=claude_bin, timeout_seconds=timeout_seconds)
        assessment: dict[str, Any] | None = decode_claude_assessment(claude_text)
        reason: str | None = None
    except PaperObserverUnavailable as error:
        assessment = None
        reason = _bounded_reason(str(error))
    return assess_observation(
        evidence,
        claude_assessment=assessment,
        unavailable_reason=reason,
    )


__all__ = [
    "CLAUDE_ASSESSMENT_SCHEMA",
    "DEFAULT_CLAUDE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_LOG_LINES",
    "OBSERVER_ASSESSMENT_SCHEMA",
    "OBSERVER_EVIDENCE_SCHEMA",
    "OBSERVER_NAME",
    "AlertEvidence",
    "BackupEvidence",
    "ObserverSeverity",
    "ObserverStatus",
    "OperationalLogEvidence",
    "PaperObserverError",
    "PaperObserverUnavailable",
    "assess_observation",
    "build_observer_evidence",
    "build_observer_prompt",
    "canonical_evidence_bytes",
    "decode_claude_assessment",
    "decode_observer_assessment",
    "decode_observer_evidence",
    "invoke_claude",
    "run_observer",
]
