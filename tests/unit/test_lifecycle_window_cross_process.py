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
context.prec = int(os.environ["EA_WINDOW_DECIMAL_PRECISION"])
context.rounding = getattr(decimal, os.environ["EA_WINDOW_DECIMAL_ROUNDING"])

helper_path = Path(os.environ["EA_WINDOW_TEST_HELPER"])
sys.path.insert(0, str(helper_path.parents[1]))
spec = importlib.util.spec_from_file_location("lifecycle_window_fixture_helper", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
sys.modules["lifecycle_window_fixture_helper"] = helper
spec.loader.exec_module(helper)

from ea.core.historical_matching import HistoricalDispatchKind
from ea.core.lifecycle import (
    _create_active_dispatch_window,
    active_dispatch_window_digest,
    canonical_active_dispatch_window_bytes,
)
from ea.core.run import Sha256Digest
from ea.core.runtime import runtime_root_order_key

binding, _order, causal = helper._binding_and_order()
window = _create_active_dispatch_window(
    binding=binding,
    coordinator_state_version=3,
    dispatch_kind=HistoricalDispatchKind.MARKET,
    dispatch_sequence=7,
    trigger_root_key=runtime_root_order_key(causal),
    trigger_root_sha256=Sha256Digest("33" * 32),
    batch_sha256=Sha256Digest("44" * 32),
    batch_ack_sha256=Sha256Digest("55" * 32),
    handoff_sha256s=(Sha256Digest("66" * 32), Sha256Digest("77" * 32)),
    audited_handoff_chain_head_sha256=Sha256Digest("88" * 32),
    authorization_allowed=True,
)
print(json.dumps(
    {
        "canonical_hex": canonical_active_dispatch_window_bytes(window).hex(),
        "digest": active_dispatch_window_digest(window).value,
    },
    ensure_ascii=True,
    sort_keys=True,
    separators=(",", ":"),
))
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
            "EA_WINDOW_DECIMAL_PRECISION": precision,
            "EA_WINDOW_DECIMAL_ROUNDING": rounding,
            "EA_WINDOW_TEST_HELPER": str(Path(__file__).with_name("test_lifecycle_window.py")),
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


def test_active_window_evidence_ignores_process_ambient_context(tmp_path: Path) -> None:
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
