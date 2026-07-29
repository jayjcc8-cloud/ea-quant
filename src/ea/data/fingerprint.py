"""Point-in-time market-data selection and canonical fingerprinting."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from ea.core.market_data import MarketDataEnvelope, MarketDataValidationError, order_market_data
from ea.core.market_data_codec import (
    canonical_market_data_record_bytes as canonical_market_data_record_bytes,
)
from ea.core.run import DataFingerprint, ReplayWindow, RunContractError, Sha256Digest

_DATA_HASH_DOMAIN = b"ea.market-data.v1\0"
_MAX_UINT64 = (1 << 64) - 1


def _fingerprint_selected(events: tuple[MarketDataEnvelope, ...]) -> DataFingerprint:
    digest = sha256()
    digest.update(_DATA_HASH_DOMAIN)
    for event in events:
        encoded = canonical_market_data_record_bytes(event)
        if len(encoded) > _MAX_UINT64:
            raise RunContractError("canonical market-data record exceeds uint64 length")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    digest.update(len(events).to_bytes(8, "big"))
    return DataFingerprint(
        sha256=Sha256Digest(digest.hexdigest()),
        record_count=len(events),
    )


@dataclass(frozen=True, slots=True)
class MarketDataSelection:
    """Validated window, exact replay tuple, and recomputed fingerprint as one value."""

    window: ReplayWindow
    events: tuple[MarketDataEnvelope, ...]
    fingerprint: DataFingerprint

    def __post_init__(self) -> None:
        if type(self.window) is not ReplayWindow:
            raise RunContractError("market-data selection window must be a ReplayWindow")
        if type(self.events) is not tuple or not self.events:
            raise RunContractError("market-data selection must contain a non-empty exact tuple")
        if any(type(event) is not MarketDataEnvelope for event in self.events):
            raise RunContractError("market-data selection contains a non-canonical event")
        if type(self.fingerprint) is not DataFingerprint:
            raise RunContractError("market-data selection fingerprint must be a DataFingerprint")
        try:
            ordered = order_market_data(self.events)
        except MarketDataValidationError as exc:
            raise RunContractError("market-data selection is not a canonical batch") from exc
        if ordered != self.events:
            raise RunContractError(
                "market-data selection events must use canonical admission order"
            )
        if any(
            not self.window.start_inclusive <= event.available_at < self.window.end_exclusive
            for event in self.events
        ):
            raise RunContractError("market-data selection contains an event outside its window")
        if self.fingerprint != _fingerprint_selected(self.events):
            raise RunContractError("market-data selection fingerprint does not match its events")


def select_and_fingerprint_market_data(
    events: Iterable[MarketDataEnvelope],
    window: ReplayWindow,
) -> MarketDataSelection:
    """Materialize once, validate/order, select by knowledge time, and fingerprint."""
    if type(window) is not ReplayWindow:
        raise RunContractError("window must be a ReplayWindow")
    try:
        ordered = order_market_data(events)
    except TypeError as exc:
        raise RunContractError("events must be an iterable of MarketDataEnvelope values") from exc
    selected = tuple(
        event
        for event in ordered
        if window.start_inclusive <= event.available_at < window.end_exclusive
    )
    if not selected:
        raise RunContractError("a reproducible Phase 1 backtest requires selected market data")
    if len(selected) > _MAX_UINT64:
        raise RunContractError("selected market-data count exceeds uint64")

    return MarketDataSelection(
        window=window,
        events=selected,
        fingerprint=_fingerprint_selected(selected),
    )
