from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import hashlib
import json
import os
from datetime import UTC, datetime

from ea.core.run import DataFingerprint, ReplayWindow, RunId, Sha256Digest
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    EffectiveParameter,
    LineageInputs,
    NormalizedConfiguration,
    RuntimeEvidence,
    build_lineage_spec,
    build_manifest,
    canonical_manifest_bytes,
)

_ = os.environ["EA_TEST_RESULT_ROOT"]
inputs = LineageInputs(
    code=CodeEvidence("0123456789abcdef0123456789abcdef01234567"),
    configuration=NormalizedConfiguration(1, "development", "backtest"),
    data=DataFingerprint(
        Sha256Digest(
            "ef22439b2e2fa38e22cea9f544b0c29f1908827d62487815b486163c3c0a1624"
        ),
        1,
    ),
    replay_window=ReplayWindow(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC),
    ),
    parameters=(
        EffectiveParameter.from_value("strategy.threshold", 0.1),
        EffectiveParameter.from_value("matcher.enabled", True),
    ),
    runtime=RuntimeEvidence(
        ea_version="0.1.1",
        python_implementation="cpython",
        python_version="3.12.13",
        python_cache_tag="cpython-312",
        sys_platform="darwin",
        platform_tag="macosx-11.0-arm64",
        distributions=(DistributionIdentity("ea-quant", "0.1.1"),),
        uv_lock_bytes=b"version = 1\\n",
    ),
    master_seed=0,
    stream_labels=("strategy.primary", "matcher.primary"),
)
spec = build_lineage_spec(inputs)
manifest = build_manifest(
    spec,
    RunId("123e4567-e89b-42d3-a456-426614174000"),
)
payload = canonical_manifest_bytes(manifest)
print(
    json.dumps(
        {
            "lineage": manifest.lineage_sha256.value,
            "manifest": hashlib.sha256(payload).hexdigest(),
        },
        sort_keys=True,
    )
)
"""


def _run(
    cwd: Path,
    *,
    hash_seed: str,
    timezone: str,
    locale: str,
    result_root: str,
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_TEST_RESULT_ROOT": result_root,
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


def test_lineage_is_stable_across_process_hash_timezone_locale_cwd_and_paths(
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()

    first = _run(
        first_cwd,
        hash_seed="1",
        timezone="UTC",
        locale="C",
        result_root="/tmp/first-results",
    )
    second = _run(
        second_cwd,
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
        result_root="/var/tmp/other-results",
    )

    assert first == second
