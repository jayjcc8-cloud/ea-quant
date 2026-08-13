from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from ea.core.historical_matching import HistoricalDispatchKind
from ea.core.lifecycle import (
    _create_active_dispatch_window,
    active_dispatch_window_digest,
    canonical_active_dispatch_window_bytes,
)
from ea.core.run import Sha256Digest
from ea.core.runtime import runtime_root_order_key

_HELPER_PATH = Path(__file__).parents[1] / "unit" / "test_lifecycle_window.py"
_SPEC = importlib.util.spec_from_file_location("lifecycle_window_property_helper", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HELPER = importlib.util.module_from_spec(_SPEC)
sys.modules["lifecycle_window_property_helper"] = _HELPER
_SPEC.loader.exec_module(_HELPER)
_binding_and_order = cast(Callable[[], tuple[Any, ...]], _HELPER.__dict__["_binding_and_order"])

_UINT64 = st.integers(min_value=1, max_value=(1 << 64) - 1)
_SHA256 = st.binary(min_size=32, max_size=32).map(lambda value: Sha256Digest(value.hex()))


@settings(deadline=None, max_examples=80)
@given(
    state_version=_UINT64,
    dispatch_sequence=_UINT64,
    trigger_sha256=_SHA256,
    batch_sha256=_SHA256,
    batch_ack_sha256=_SHA256,
    handoff_sha256s=st.lists(_SHA256, min_size=0, max_size=8).map(tuple),
    chain_head_sha256=_SHA256,
    authorization_allowed=st.booleans(),
)
def test_active_window_canonical_evidence_is_factory_input_deterministic(
    state_version: int,
    dispatch_sequence: int,
    trigger_sha256: Sha256Digest,
    batch_sha256: Sha256Digest,
    batch_ack_sha256: Sha256Digest,
    handoff_sha256s: tuple[Sha256Digest, ...],
    chain_head_sha256: Sha256Digest,
    authorization_allowed: bool,
) -> None:
    binding, _order, causal = _binding_and_order()
    values = {
        "binding": binding,
        "coordinator_state_version": state_version,
        "dispatch_kind": HistoricalDispatchKind.MARKET,
        "dispatch_sequence": dispatch_sequence,
        "trigger_root_key": runtime_root_order_key(causal),
        "trigger_root_sha256": trigger_sha256,
        "batch_sha256": batch_sha256,
        "batch_ack_sha256": batch_ack_sha256,
        "handoff_sha256s": handoff_sha256s,
        "audited_handoff_chain_head_sha256": chain_head_sha256,
        "authorization_allowed": authorization_allowed,
    }
    first = _create_active_dispatch_window(**values)
    second = _create_active_dispatch_window(**values)

    first_bytes = canonical_active_dispatch_window_bytes(first)
    document = json.loads(first_bytes)
    assert first is not second
    assert first_bytes == canonical_active_dispatch_window_bytes(second)
    assert active_dispatch_window_digest(first) == active_dispatch_window_digest(second)
    assert document["coordinator_state_version"] == state_version
    assert document["dispatch_sequence"] == dispatch_sequence
    assert document["handoff_count"] == len(handoff_sha256s)
    assert document["authorization_allowed"] is authorization_allowed
