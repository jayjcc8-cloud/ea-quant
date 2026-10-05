"""Contract for the shared canonical JSON primitive."""

from __future__ import annotations

import pytest

from ea.core.encoding import canonical_json_bytes


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"b": 1, "a": 2}, b'{"a":2,"b":1}'),
        ({"name": "café"}, b'{"name":"caf\\u00e9"}'),
        ({"z": 1, "a": 2, "m": 3}, b'{"a":2,"m":3,"z":1}'),
        ([3, 1, 2], b"[3,1,2]"),
    ],
)
def test_canonical_json_encodes_ascii_with_sorted_keys(document: object, expected: bytes) -> None:
    assert canonical_json_bytes(document) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": value})


@pytest.mark.parametrize("document", [{1, 2, 3}, object(), b"raw bytes"])
def test_canonical_json_rejects_unsupported_objects(document: object) -> None:
    with pytest.raises(TypeError):
        canonical_json_bytes(document)
