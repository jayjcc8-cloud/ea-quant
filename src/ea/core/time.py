"""Canonical time validation and injected clock contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class TimeValidationError(ValueError):
    """Raised when a value violates the canonical UTC contract."""


class Clock(Protocol):
    """Injected time capability; implementations must be monotonic within a run."""

    def now(self) -> datetime:
        """Return the current canonical UTC instant."""
        ...


def require_utc(value: datetime, *, field: str) -> datetime:
    """Validate an aware zero-offset datetime and normalize its tzinfo to ``UTC``."""
    if type(value) is not datetime:
        raise TimeValidationError(f"{field} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise TimeValidationError(f"{field} must be timezone-aware UTC")
    return value.astimezone(UTC)
