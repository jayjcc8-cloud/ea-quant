"""Side-effect-free canonical encoding primitives."""

from __future__ import annotations

import json


def canonical_json_bytes(document: object) -> bytes:
    """Encode one value as compact, sorted-key ASCII JSON that rejects non-finite numbers."""
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
