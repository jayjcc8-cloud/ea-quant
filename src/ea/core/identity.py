"""Canonical venue and instrument identities."""

from __future__ import annotations

import re
from dataclasses import dataclass

_VENUE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z", flags=re.ASCII)
_SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z", flags=re.ASCII)


class IdentityValidationError(ValueError):
    """Raised when a canonical identity is invalid or ambiguous."""


@dataclass(frozen=True, slots=True)
class VenueId:
    """Project-canonical trading venue namespace."""

    code: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _VENUE_PATTERN.fullmatch(self.code) is None:
            raise IdentityValidationError("venue code must match [A-Z0-9][A-Z0-9._-]{0,31}")


@dataclass(frozen=True, slots=True)
class Instrument:
    """A venue-local canonical instrument identity."""

    venue: VenueId
    symbol: str

    def __post_init__(self) -> None:
        if type(self.venue) is not VenueId:
            raise IdentityValidationError("venue must be a VenueId")
        if type(self.symbol) is not str or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise IdentityValidationError(
                "instrument symbol must match [A-Za-z0-9][A-Za-z0-9._/-]{0,63}"
            )

    @property
    def key(self) -> tuple[str, str]:
        """Return the structured identity used for equality and grouping."""
        return (self.venue.code, self.symbol)

    @property
    def id(self) -> str:
        """Return an unambiguous human-readable identity, not a wire schema."""
        return f"{self.venue.code}:{self.symbol}"
