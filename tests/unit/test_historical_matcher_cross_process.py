from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import decimal
import importlib.util
import json
import os
import sys
from pathlib import Path

context = decimal.getcontext()
context.prec = int(os.environ["EA_MATCHER_DECIMAL_PRECISION"])
context.rounding = getattr(decimal, os.environ["EA_MATCHER_DECIMAL_ROUNDING"])

helper_path = Path(os.environ["EA_MATCHER_TEST_HELPER"])
spec = importlib.util.spec_from_file_location("matcher_fixture_helper", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
sys.modules["matcher_fixture_helper"] = helper
spec.loader.exec_module(helper)

from ea.core import (
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_historical_matcher_dispatch_batch_bytes,
    canonical_historical_matcher_state_bytes,
    canonical_historical_submission_receipt_bytes,
    execution_fact_digest,
    execution_fact_ingress_digest,
    historical_matcher_dispatch_batch_digest,
    historical_matcher_state_digest,
    historical_submission_receipt_digest,
)

fixture, matcher, orders, causal, delayed, end = helper._system()
trade_receipt = matcher.submit(
    orders[0],
    causal_market_root=causal,
    dispatch_sequence=7,
)
trade_batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
expiry_receipt = matcher.submit(
    orders[1],
    causal_market_root=delayed,
    dispatch_sequence=8,
)
end_batch = matcher.expire_at_active_end(end, dispatch_sequence=9)
state = matcher.state

values = {
    "trade_receipt": [
        canonical_historical_submission_receipt_bytes(trade_receipt).hex(),
        historical_submission_receipt_digest(trade_receipt).value,
    ],
    "trade_batch": [
        canonical_historical_matcher_dispatch_batch_bytes(trade_batch).hex(),
        historical_matcher_dispatch_batch_digest(trade_batch).value,
    ],
    "trade_fact": [
        canonical_execution_fact_bytes(trade_batch.ingresses[0].fact).hex(),
        execution_fact_digest(trade_batch.ingresses[0].fact).value,
    ],
    "trade_ingress": [
        canonical_execution_fact_ingress_bytes(trade_batch.ingresses[0]).hex(),
        execution_fact_ingress_digest(trade_batch.ingresses[0]).value,
    ],
    "expiry_receipt": [
        canonical_historical_submission_receipt_bytes(expiry_receipt).hex(),
        historical_submission_receipt_digest(expiry_receipt).value,
    ],
    "end_batch": [
        canonical_historical_matcher_dispatch_batch_bytes(end_batch).hex(),
        historical_matcher_dispatch_batch_digest(end_batch).value,
    ],
    "expiry_fact": [
        canonical_execution_fact_bytes(end_batch.ingresses[0].fact).hex(),
        execution_fact_digest(end_batch.ingresses[0].fact).value,
    ],
    "expiry_ingress": [
        canonical_execution_fact_ingress_bytes(end_batch.ingresses[0]).hex(),
        execution_fact_ingress_digest(end_batch.ingresses[0]).value,
    ],
    "state": [
        canonical_historical_matcher_state_bytes(state).hex(),
        historical_matcher_state_digest(state).value,
    ],
}
print(json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
"""


def _run(
    cwd: Path,
    *,
    hash_seed: str,
    timezone: str,
    locale: str,
    precision: str,
    rounding: str,
) -> str:
    cwd.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_MATCHER_DECIMAL_PRECISION": precision,
            "EA_MATCHER_DECIMAL_ROUNDING": rounding,
            "EA_MATCHER_TEST_HELPER": str(Path(__file__).with_name("test_historical_matcher.py")),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", SCRIPT],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_historical_matcher_vectors_ignore_process_environment(tmp_path: Path) -> None:
    first = _run(
        tmp_path / "first",
        hash_seed="1",
        timezone="UTC",
        locale="C",
        precision="7",
        rounding="ROUND_DOWN",
    )
    second = _run(
        tmp_path / "second",
        hash_seed="987654",
        timezone="Asia/Shanghai",
        locale="POSIX",
        precision="31",
        rounding="ROUND_CEILING",
    )
    assert first == second
