from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import json
import os
import sys

sys.path.insert(0, os.environ["EA_TEST_SOURCE_ROOT"])
from ea.core import (
    EconomicId,
    EconomicOwnerKind,
    ExternalFactId,
    FactDedupKey,
    RunId,
    SourceNamespace,
    canonical_economic_id_bytes,
    canonical_fact_dedup_key_bytes,
    economic_id_digest,
    fact_dedup_key_digest,
)

run_id = RunId("12345678-1234-4234-8234-123456789abc")
economic = EconomicId(run_id, EconomicOwnerKind.PORTFOLIO_INTENT, 18446744073709551615)
sources = {"sim.secondary", "sim.primary"}
source = sorted(sources)[0]
fact_key = FactDedupKey(SourceNamespace(source), ExternalFactId('trade"\\\\42'))
print(
    json.dumps(
        {
            "economic_bytes": canonical_economic_id_bytes(economic).hex(),
            "economic_digest": economic_id_digest(economic).value,
            "fact_bytes": canonical_fact_dedup_key_bytes(fact_key).hex(),
            "fact_digest": fact_dedup_key_digest(fact_key).value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
"""


def _run(cwd: Path, *, hash_seed: str, timezone: str, locale: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_TEST_SOURCE_ROOT": str(Path(__file__).resolve().parents[2] / "src"),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _SCRIPT],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_execution_identity_vectors_ignore_process_environment_and_cwd(
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    first = _run(first_cwd, hash_seed="1", timezone="UTC", locale="C")
    second = _run(
        second_cwd,
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
    )

    assert first == second
