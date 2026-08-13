from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import json
from ea.core import (
    EconomicId, EconomicOwnerKind, IngressIdentity, LedgerHandoffAction,
    RunId, Sha256Digest, SourceNamespace,
    canonical_ledger_application_command_bytes,
    canonical_ledger_handoff_outcome_bytes,
    create_ledger_handoff_outcome,
    ledger_application_command_digest,
    ledger_handoff_outcome_digest,
)
from ea.core.ledger_integration import _create_ledger_application_command

run_id = RunId("12345678-1234-4234-8234-123456789abc")
fill_id = EconomicId(run_id, EconomicOwnerKind.EXECUTION_FILL, 7)
command = _create_ledger_application_command(
    run_id=run_id,
    dispatch_sequence=2,
    audited_handoff_sha256=Sha256Digest("11" * 32),
    processing_outcome_sha256=Sha256Digest("22" * 32),
    fill_id=fill_id,
    fill_sha256=Sha256Digest("33" * 32),
    requires_reconciliation=False,
)
outcome = create_ledger_handoff_outcome(
    run_id=run_id,
    dispatch_sequence=2,
    ingress_identity=IngressIdentity(SourceNamespace("sim.execution"), 3),
    audited_handoff_sha256=Sha256Digest("11" * 32),
    processing_outcome_sha256=Sha256Digest("22" * 32),
    processing_outcome_ack_sha256=Sha256Digest("44" * 32),
    fill_id=None,
    fill_sha256=None,
    action=LedgerHandoffAction.NOT_APPLICABLE,
    original_ledger_apply_outcome=None,
    original_ledger_apply_outcome_sha256=None,
    before_snapshot_version=4,
    before_snapshot_sha256=Sha256Digest("55" * 32),
    after_snapshot_version=4,
    after_snapshot_sha256=Sha256Digest("55" * 32),
    requires_reconciliation=False,
    halt_requested=False,
    failure=None,
)
print(json.dumps({
    "command": canonical_ledger_application_command_bytes(command).hex(),
    "command_sha256": ledger_application_command_digest(command).value,
    "outcome": canonical_ledger_handoff_outcome_bytes(outcome).hex(),
    "outcome_sha256": ledger_handoff_outcome_digest(outcome).value,
}, sort_keys=True, separators=(",", ":")))
"""


def _run(cwd: Path, *, hash_seed: str, timezone: str, locale: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
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


def test_ledger_integration_vectors_ignore_process_environment(tmp_path: Path) -> None:
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
