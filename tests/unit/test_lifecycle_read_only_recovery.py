from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from ea.core import (
    AuditRecordKind,
    ReconciliationObservationKind,
    RunBinding,
    RunReference,
    Sha256Digest,
    create_reconciliation_observation_root,
)
from ea.core.lifecycle import LifecycleError
from ea.core.runtime import runtime_root_order_key
from ea.runtime._coordinator_read_only_recovery import _require_read_only_recovery_order
from ea.runtime._coordinator_recovery import _require_runtime_trace
from ea.runtime.historical import historical_runtime_trace_digest
from unit.test_historical_matcher import _system
from unit.test_reconciliation_authority import _observation


def _trace_order_key(root: object) -> list[str | int]:
    return [
        value.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if type(value) is datetime else value
        for value in runtime_root_order_key(root).as_tuple()
    ]


def test_recovery_accepts_one_canonical_v2_read_only_trace_root() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    root = create_reconciliation_observation_root(
        _observation(
            kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
            spec_set=matcher.spec_set,
        )
    )
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)), Sha256Digest("22" * 32)
    )
    document = {
        "clock_now": root.available_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "committed_cursor_as_of": None,
        "committed_event_count": 0,
        "data_sha256": "00" * 32,
        "dispatch_sequence": 1,
        "root": json.loads(root.canonical_observation_bytes),
        "root_order_key": _trace_order_key(root),
        "run_id": matcher.run_id.value,
        "schema": "ea.phase1-historical-runtime-trace.v2",
        "terminal_acknowledged": False,
    }
    record = json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    runtime = SimpleNamespace(
        spec_set=matcher.spec_set,
        trace_records=(record,),
        trace_digest=historical_runtime_trace_digest((record,)),
    )

    trace = _require_runtime_trace(runtime, binding)

    assert trace[1][1] == root.observation_sha256


def test_read_only_recovery_rejects_completion_without_mandatory_refresh() -> None:
    outcome = SimpleNamespace(record_kind=AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME)
    completion = SimpleNamespace(record_kind=AuditRecordKind.RUNTIME_DISPATCH_COMPLETED)
    group = SimpleNamespace(
        read_only_outcome_record=(2, outcome, None),
        batch_record=None,
        outcome_records={},
        authorization_records=[],
        ledger_records=[],
        failing_record=None,
        refresh_record=None,
        completion_record=(3, completion, None),
    )

    with pytest.raises(LifecycleError, match="prefix"):
        _require_read_only_recovery_order(group)
