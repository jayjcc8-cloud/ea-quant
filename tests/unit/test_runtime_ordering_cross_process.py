from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import json
import locale
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, getcontext
from pathlib import Path
from threading import Event

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

decimal_context = getcontext()
decimal_context.prec = int(os.environ["DECIMAL_PRECISION"])
decimal_context.rounding = {
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
}[os.environ["DECIMAL_ROUNDING"]]
assert decimal_context.prec == int(os.environ["DECIMAL_PRECISION"])
assert decimal_context.rounding == os.environ["DECIMAL_ROUNDING"]
assert locale.setlocale(locale.LC_ALL, "") == os.environ["EXPECTED_LOCALE"]
assert Path.cwd().name == os.environ["EXPECTED_CWD_NAME"]
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

completion_order = {
    "forward": (0, 1, 2, 3, 4),
    "reverse": (4, 3, 2, 1, 0),
    "rotate": (2, 3, 4, 0, 1),
}[os.environ["COMPLETION_ORDER"]]
gates = [Event() for _ in roots]


def calculate_after_release(index, root):
    gates[index].wait()
    runtime_root_order_key(root)
    return index, root


with ThreadPoolExecutor(max_workers=len(roots)) as pool:
    futures = [
        pool.submit(calculate_after_release, index, root)
        for index, root in enumerate(roots)
    ]
    completed_roots = []
    for expected_index in completion_order:
        gates[expected_index].set()
        completed_index, completed_root = futures[expected_index].result(timeout=5)
        assert completed_index == expected_index
        completed_roots.append(completed_root)

assert completed_roots != roots or completion_order == tuple(range(len(roots)))
plan = prepare_bounded_runtime_roots(completed_roots)
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


def _run(
    working_directory: Path,
    *,
    seed: str,
    timezone: str,
    locale_name: str,
    decimal_precision: int,
    decimal_rounding: str,
    root_order: str,
    completion_order: str,
) -> bytes:
    working_directory.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": seed,
            "TZ": timezone,
            "LC_ALL": locale_name,
            "LANG": locale_name,
            "EXPECTED_LOCALE": locale_name,
            "EXPECTED_CWD_NAME": working_directory.name,
            "DECIMAL_PRECISION": str(decimal_precision),
            "DECIMAL_ROUNDING": decimal_rounding,
            "ROOT_ORDER": root_order,
            "COMPLETION_ORDER": completion_order,
        }
    )
    return subprocess.check_output(
        [sys.executable, "-I", "-c", SCRIPT],
        cwd=working_directory,
        env=environment,
    )


def test_runtime_order_trace_is_cross_process_invariant(tmp_path: Path) -> None:
    expected = _run(
        tmp_path / "forward-cwd",
        seed="1",
        timezone="UTC",
        locale_name="C",
        decimal_precision=7,
        decimal_rounding="ROUND_DOWN",
        root_order="forward",
        completion_order="forward",
    )

    assert (
        _run(
            tmp_path / "reverse-cwd",
            seed="987654",
            timezone="Asia/Shanghai",
            locale_name="C.UTF-8",
            decimal_precision=41,
            decimal_rounding="ROUND_HALF_EVEN",
            root_order="reverse",
            completion_order="reverse",
        )
        == expected
    )
    assert (
        _run(
            tmp_path / "rotate-cwd",
            seed="0",
            timezone="America/New_York",
            locale_name="C",
            decimal_precision=19,
            decimal_rounding="ROUND_HALF_EVEN",
            root_order="rotate",
            completion_order="rotate",
        )
        == expected
    )
    assert expected == (
        b'["SafetyRoot:halt","fact:acknowledgement","market:bar",'
        b'"TimerRoot:strategy_timer","EndOfRunRoot:bounded_source_exhausted"]\n'
    )
