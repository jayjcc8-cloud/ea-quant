"""Bounded best-effort operational JSONL, separate from economic audit evidence.

Callers pass an explicit logger through one operation. There is no ambient context,
global handler, identity allocation or economic authority. Only deliberately selected
scalar fields belong here; exception messages, credentials and payloads do not.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ea.core.execution_identity import EconomicId, economic_id_digest
from ea.core.run import RunId, Sha256Digest

_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z", re.ASCII)
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_RESERVED = frozenset(
    {
        "schema",
        "schema_version",
        "timestamp",
        "sequence",
        "event",
        "run_id",
        "strategy_id",
        "candidate_id",
        "account_id",
        "operation",
        "dropped_events",
    }
)
_SENSITIVE = frozenset(
    {
        "exception",
        "traceback",
        "password",
        "secret",
        "token",
        "api_key",
        "authorization",
        "payload",
    }
)


def _safe_text(value: object, *, maximum: int = 512) -> str:
    if type(value) is not str:
        raise TypeError("operational text must be an exact string")
    if (
        not value
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("operational text is empty, oversized or contains control characters")
    return value


def operational_identity(value: object) -> str | None:
    """Project known identities to scalars without inventing another economic ID.

    Economic IDs use their existing canonical digest. Client submission keys and other
    digests retain their exact value. No arbitrary object's string or repr is evaluated.
    """
    if value is None:
        return None
    if type(value) is EconomicId:
        return economic_id_digest(value).value
    if type(value) is RunId:
        return _safe_text(value.value, maximum=256)
    if type(value) is Sha256Digest:
        return _safe_text(value.value, maximum=256)
    return _safe_text(value, maximum=256)


@dataclass(frozen=True, slots=True)
class OperationalContext:
    """One immutable operation context; optional absent identities stay absent."""

    run_id: str
    strategy_id: str | None = None
    candidate_id: str | None = None
    account_id: str | None = None
    operation: str = "run"

    def __post_init__(self) -> None:
        RunId(self.run_id)
        for value in (self.strategy_id, self.candidate_id, self.account_id):
            if value is not None:
                _safe_text(value, maximum=256)
        if type(self.operation) is not str or _EVENT.fullmatch(self.operation) is None:
            raise ValueError("operational operation must be a bounded machine label")


class JsonlFileSink:
    """Append to an existing operation directory, never following a final symlink.

    Construction performs no I/O. Opening and writing may fail; OperationalLogger owns
    the best-effort boundary and exposes those failures. The sink does not create parent
    directories, repair logs, rotate files, or modify any authoritative artifact.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, line: str) -> None:
        payload = line.encode("utf-8")
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("operational sink must be an unshared regular file")
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("operational sink write made no progress")
                written += count
        finally:
            os.close(descriptor)


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(1 << 63) <= value < 1 << 64:
            raise ValueError("operational integer is out of bounds")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("operational float must be finite")
        return value
    return _safe_text(value)


class OperationalLogger:
    """Emit one JSON line per call and retain observable best-effort failures.

    Sequence numbers count attempted emissions within this logger, including dropped
    entries. Each successful line reports the prior dropped count. failure_count and
    last_failure remain inspectable even when the sink never becomes writable.
    """

    def __init__(self, context: OperationalContext, sink: Callable[[str], None]) -> None:
        self.context = context
        self._sink = sink
        self._sequence = 0
        self.failure_count = 0
        self.last_failure: str | None = None

    def emit(
        self,
        event: str,
        *,
        correlation_id: object = None,
        client_order_id: object = None,
        order_id: object = None,
        fill_id: object = None,
        market_event_id: object = None,
        outcome: str | None = None,
        **fields: object,
    ) -> bool:
        """Return success; reject unsafe data or sink errors without escaping."""
        self._sequence += 1
        try:
            if type(event) is not str or _EVENT.fullmatch(event) is None:
                raise ValueError("operational event must be a bounded machine label")
            if len(fields) > 32:
                raise ValueError("too many operational fields")
            document: dict[str, object] = {
                "schema": "ea.operational-log.v1",
                "schema_version": 1,
                "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
                "sequence": self._sequence,
                "event": event,
                "run_id": self.context.run_id,
                "strategy_id": self.context.strategy_id,
                "candidate_id": self.context.candidate_id,
                "account_id": self.context.account_id,
                "operation": self.context.operation,
                "dropped_events": self.failure_count,
            }
            for key, identity in (
                ("correlation_id", correlation_id),
                ("client_order_id", client_order_id),
                ("order_id", order_id),
                ("fill_id", fill_id),
                ("market_event_id", market_event_id),
            ):
                if identity is not None:
                    document[key] = operational_identity(identity)
            if outcome is not None:
                document["outcome"] = _safe_text(outcome, maximum=128)
            for key, value in fields.items():
                if _FIELD.fullmatch(key) is None or key in _RESERVED or key in _SENSITIVE:
                    raise ValueError("operational field is reserved or unsafe")
                document[key] = _scalar(value)
            line = (
                json.dumps(
                    document,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        except Exception:
            self.failure_count += 1
            self.last_failure = "serialization"
            return False
        try:
            self._sink(line)
        except Exception:
            self.failure_count += 1
            self.last_failure = "sink"
            return False
        return True


__all__ = ["JsonlFileSink", "OperationalContext", "OperationalLogger", "operational_identity"]
