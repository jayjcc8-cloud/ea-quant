from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, getcontext

from ea.core import (
    Adjustment,
    Bar,
    EndOfRunKind,
    EndOfRunRoot,
    ExecutionFactKind,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    MarketDataEnvelope,
    RuntimeIdentifier,
    SafetyKind,
    SafetyRoot,
    Sha256Digest,
    SourceId,
    SourceNamespace,
    TimerKind,
    TimerRoot,
    VenueId,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    prepare_bounded_runtime_roots,
    runtime_root_order_key,
)
from ea.core.run import RunId

getcontext().prec = 7
getcontext().rounding = ROUND_DOWN
time = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
source = SourceNamespace("sim.primary")
fact = create_lifecycle_execution_fact(
    kind=ExecutionFactKind.ACKNOWLEDGEMENT,
    source_namespace=source,
    dedup_identity=ExternalFactId("ack-1"),
    occurred_at=time,
    provenance=FactProvenance(
        FactProvenanceId("phase1.simulator.v1"),
        Sha256Digest("3" * 64),
    ),
)
ingress = create_execution_fact_ingress(
    available_at=time,
    source_namespace=source,
    ingress_sequence=4,
    fact=fact,
)
market = MarketDataEnvelope(
    payload=Bar(
        instrument=Instrument(VenueId("XNAS"), "AAPL"),
        interval_start=time - timedelta(minutes=1),
        interval_end=time,
        adjustment=Adjustment.RAW,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
    ),
    source=SourceId("primary.raw"),
    available_at=time,
    source_sequence=7,
    revision=0,
)
roots = [
    SafetyRoot(
        time,
        SafetyKind.HALT,
        SourceNamespace("runtime.safety"),
        1,
    ),
    ingress,
    market,
    TimerRoot(
        time,
        TimerKind.STRATEGY_TIMER,
        SourceNamespace("runtime.timer"),
        RuntimeIdentifier("strategy.primary"),
        2,
    ),
    EndOfRunRoot(
        time,
        EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED,
        SourceNamespace("runtime.end"),
        3,
        RunId("12345678-1234-4234-8234-123456789abc"),
    ),
]
mode = os.environ["ROOT_ORDER"]
if mode == "reverse":
    roots.reverse()
elif mode == "rotate":
    roots = roots[2:] + roots[:2]

with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(runtime_root_order_key, reversed(roots)))

plan = prepare_bounded_runtime_roots(roots)
labels = []
for root in plan.roots:
    if type(root).__name__ == "ExecutionFactIngress":
        labels.append("fact:" + root.fact.kind.value)
    elif type(root).__name__ == "MarketDataEnvelope":
        labels.append("market:" + root.kind.value)
    else:
        labels.append(type(root).__name__ + ":" + root.kind.value)
print(json.dumps(labels, separators=(",", ":")))
"""


def _run(tmp_path: Path, *, seed: str, timezone: str, order: str) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": seed,
            "TZ": timezone,
            "LC_ALL": "C",
            "LANG": "C",
            "ROOT_ORDER": order,
        }
    )
    return subprocess.check_output(
        [sys.executable, "-I", "-c", SCRIPT],
        cwd=tmp_path,
        env=environment,
    )


def test_runtime_order_trace_is_cross_process_invariant(tmp_path: Path) -> None:
    expected = _run(tmp_path, seed="1", timezone="UTC", order="forward")

    assert _run(tmp_path, seed="987654", timezone="Asia/Shanghai", order="reverse") == expected
    assert _run(tmp_path, seed="0", timezone="America/New_York", order="rotate") == expected
    assert expected == (
        b'["SafetyRoot:halt","fact:acknowledgement","market:bar",'
        b'"TimerRoot:strategy_timer","EndOfRunRoot:bounded_source_exhausted"]\n'
    )
