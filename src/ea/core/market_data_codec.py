"""Canonical dependency-neutral market-data record encoding."""

from __future__ import annotations

import json
import struct
from datetime import datetime

from ea.core.market_data import MarketDataEnvelope
from ea.core.run import RunContractError


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _float_bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def canonical_market_data_record_bytes(event: MarketDataEnvelope) -> bytes:
    """Serialize one ADR 0004 envelope using the closed ADR 0006 record schema."""
    if type(event) is not MarketDataEnvelope:
        raise RunContractError("event must be a MarketDataEnvelope")
    payload = event.payload
    record = {
        "adjustment": payload.adjustment.value,
        "available_at": _utc_text(event.available_at),
        "close_bits": _float_bits(payload.close),
        "high_bits": _float_bits(payload.high),
        "interval_end": _utc_text(payload.interval_end),
        "interval_start": _utc_text(payload.interval_start),
        "kind": payload.kind.value,
        "low_bits": _float_bits(payload.low),
        "open_bits": _float_bits(payload.open),
        "revision": event.revision,
        "source": event.source.code,
        "source_sequence": event.source_sequence,
        "symbol": payload.instrument.symbol,
        "venue": payload.instrument.venue.code,
        "volume_bits": _float_bits(payload.volume),
    }
    return json.dumps(
        record,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
