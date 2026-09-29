"""WU-3 boundary tests: the read-only Claude observer cannot become authority.

These tests prove the four boundaries the acceptance criteria name, using the
real product contracts:

1. the observer receives only the intended evidence;
2. the deterministic gate verdict wins over contradictory Claude output;
3. observer failure is isolated from the Paper runtime;
4. no mutation or control capability is reachable from the observer path.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ea.cli.app import app
from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product import paper_observer as observer_module
from ea.product.paper_alerts import (
    ALERT_SIGNALS,
    AlertSignalReading,
    SignalCondition,
    project_alert_stream,
    write_alert_stream,
)
from ea.product.paper_evidence import (
    AlertSummary,
    BackupSummary,
    BrokerMode,
    GateType,
    SupervisorState,
    build_gate_identity,
    build_paper_snapshot,
    canonical_gate_verdict_bytes,
    evaluate_gate,
)
from ea.product.paper_observer import (
    OBSERVER_ASSESSMENT_SCHEMA,
    OBSERVER_EVIDENCE_SCHEMA,
    AlertEvidence,
    BackupEvidence,
    OperationalLogEvidence,
    assess_observation,
    build_observer_evidence,
    build_observer_prompt,
    decode_claude_assessment,
    decode_observer_assessment,
    decode_observer_evidence,
    invoke_claude,
    run_observer,
)
from ea.product.paper_session import PaperSessionWriter, read_paper_status
from unit.test_paper_session import _health_document

RUN_ID = RunId("123e4567-e89b-42d3-a456-426614174000")
LINEAGE = Sha256Digest("1" * 64)
OBSERVED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
REPO_SHA = "a" * 40


@pytest.fixture
def attempt(tmp_path: Path) -> Iterator[tuple[LocalResultStore, Path, RunBinding]]:
    """One real prepared attempt with the store holding the writer lease."""
    root = tmp_path / "results"
    root.mkdir()
    store = LocalResultStore(root.resolve())
    reference = RunReference(RUN_ID, LINEAGE)
    manifest = {
        "schema": "ea.local-paper-attempt.v1",
        "run_id": RUN_ID.value,
        "lineage_sha256": LINEAGE.value,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    prepared = store.prepare_canonical_attempt(CanonicalAttemptManifest(reference, payload))
    try:
        yield store, root / RUN_ID.value, prepared.audit.binding
    finally:
        store.close()


# A stub `claude` that answers with one valid assessment envelope. The `result`
# string is the assessment text with its quotes escaped inside the envelope.
_SUCCESS_ASSESSMENT = (
    '{"type":"result","subtype":"success","is_error":false,"result":"'
    '{\\"schema\\":\\"ea.claude-assessment.v1\\",'
    '\\"summary\\":\\"The Paper runtime is running cleanly within its operational gates.\\",'
    '\\"severity\\":\\"info\\",'
    '\\"observations\\":[\\"The writer lease is held by one live writer.\\"],'
    '\\"anomalies\\":[],'
    '\\"evidence_refs\\":[\\"paper_status\\"],'
    '\\"root_causes\\":[],'
    '\\"operator_actions\\":[\\"No operator action is required.\\"]}"}'
)


def _stub_claude(tmp_path: Path, *, body: str, exit_code: int = 0, capture: bool = False) -> Path:
    """A stand-in for the real `claude` CLI: no tools, just the observer contract."""
    script = tmp_path / "claude-stub"
    lines = ["#!/bin/sh", "cat >/dev/null"]
    if capture:
        lines[1] = 'cat >"$OBSERVER_CAPTURE"'
    if exit_code != 0:
        lines.append("printf '%s\\n' 'stub failure' >&2")
        lines.append(f"exit {exit_code}")
    else:
        lines.append(f"printf '%s\\n' '{body}'")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def _alert_stream_file(tmp_path: Path) -> Path:
    """One real, canonical host alert stream with every signal inactive."""
    path = tmp_path / "alerts" / "paper-alerts.json"
    path.parent.mkdir()
    readings = tuple(
        AlertSignalReading(
            type=spec.type, condition=SignalCondition.INACTIVE, observation="no condition observed"
        )
        for spec in ALERT_SIGNALS
    )
    stream = project_alert_stream(
        host_id="observer-test-host",
        readings=readings,
        observed_at=OBSERVED_AT,
        run_id=None,
        account_id=None,
    )
    write_alert_stream(path, stream)
    return path


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """(relative path) -> (mode, size, sha256) for every file and directory."""

    def record(path: Path) -> tuple[int, int, str]:
        info = path.stat()
        if path.is_file():
            return (
                stat.S_IMODE(info.st_mode),
                info.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return (stat.S_IMODE(info.st_mode), 0, "")

    return {str(path.relative_to(root)): record(path) for path in sorted(root.rglob("*"))}


# ---------------------------------------------------------------------------
# acceptance 1: the observer receives only the intended evidence
# ---------------------------------------------------------------------------


def test_evidence_package_is_a_closed_document_of_the_read_models(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish(
        {
            "state": "running",
            "health": _health_document(OBSERVED_AT),
        }
    )
    status = read_paper_status(run_dir)
    alerts = AlertEvidence(available=False, reason="no alert stream path was supplied")
    backup = BackupEvidence(available=False, reason="no backup root was supplied")
    log = OperationalLogEvidence(available=True, lines=("one", "two"), truncated=False)

    evidence = build_observer_evidence(
        observed_at=OBSERVED_AT,
        paper_status=status,
        alerts=alerts,
        backup=backup,
        operational_log=log,
    )

    assert evidence["schema"] == OBSERVER_EVIDENCE_SCHEMA
    assert set(evidence) == {
        "schema",
        "observed_at",
        "paper_status",
        "alert_stream",
        "backup",
        "operational_log",
    }
    assert evidence["paper_status"] == status
    assert evidence["alert_stream"] == {
        "available": False,
        "reason": "no alert stream path was supplied",
    }
    assert evidence["backup"] == {"available": False, "reason": "no backup root was supplied"}
    assert evidence["operational_log"] == {
        "available": True,
        "lines": ["one", "two"],
        "truncated": False,
        "elided_lines": 0,
    }
    # The closed document round-trips through the strict decoder unchanged.
    assert decode_observer_evidence(evidence) == evidence
    assert decode_observer_evidence(json.loads(json.dumps(evidence))) == evidence


def test_prompt_is_the_fixed_boundary_plus_exactly_the_evidence(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "health": _health_document(OBSERVED_AT)})
    evidence = build_observer_evidence(
        observed_at=OBSERVED_AT,
        paper_status=read_paper_status(run_dir),
        alerts=AlertEvidence(available=False, reason="no alert stream path was supplied"),
        backup=BackupEvidence(available=False, reason="no backup root was supplied"),
        operational_log=OperationalLogEvidence(available=True, lines=("one",), truncated=False),
    )

    prompt = build_observer_prompt(evidence)

    assert prompt.startswith(observer_module._CLAUDE_SYSTEM_PROMPT)
    assert prompt.count(OBSERVER_EVIDENCE_SCHEMA) == 1
    suffix = prompt[len(observer_module._CLAUDE_SYSTEM_PROMPT) :]
    assert suffix == "\n\nThe bounded read-only evidence document follows.\n\n" + json.dumps(
        evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    # The system boundary itself states the role and the advisory-only output.
    assert "ea.claude-assessment.v1" in observer_module._CLAUDE_SYSTEM_PROMPT
    assert "advisory to a human" in observer_module._CLAUDE_SYSTEM_PROMPT
    assert "must never reinterpret" in observer_module._CLAUDE_SYSTEM_PROMPT


def test_end_to_end_claude_receives_only_the_intended_evidence(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "health": _health_document(OBSERVED_AT)})
    (run_dir / "outputs" / "operational.jsonl").write_text("line one\nline two\n")
    alert_stream = _alert_stream_file(tmp_path)
    capture = tmp_path / "captured-prompt.txt"
    monkeypatch.setenv("OBSERVER_CAPTURE", str(capture))

    document = run_observer(
        run_dir=run_dir,
        alert_stream_path=alert_stream,
        backup_root=None,
        claude_bin=_stub_claude(tmp_path, body=_SUCCESS_ASSESSMENT, capture=True),
        timeout_seconds=10.0,
        observed_at=OBSERVED_AT,
    )

    assert document["observer_status"] == "available"
    captured = capture.read_text()
    # The child saw the fixed boundary, then exactly one evidence document.
    assert captured.startswith(observer_module._CLAUDE_SYSTEM_PROMPT)
    assert captured.count(OBSERVER_EVIDENCE_SCHEMA) == 1
    received = decode_observer_evidence(
        json.loads(captured[len(observer_module._CLAUDE_SYSTEM_PROMPT) :].split("\n\n", 2)[-1])
    )
    assert received["paper_status"] == read_paper_status(run_dir, now=OBSERVED_AT)
    assert received["operational_log"]["lines"] == ["line one", "line two"]
    assert received["alert_stream"]["available"] is True
    assert received["alert_stream"]["host_id"] == "observer-test-host"
    assert received["backup"]["available"] is False


def test_evidence_builder_rejects_documents_that_are_not_the_read_models(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "health": _health_document(OBSERVED_AT)})
    status = read_paper_status(run_dir)
    alerts = AlertEvidence(available=False, reason="no alert stream path was supplied")
    backup = BackupEvidence(available=False, reason="no backup root was supplied")
    log = OperationalLogEvidence(available=True, lines=("one",), truncated=False)
    with pytest.raises(observer_module.PaperObserverError):
        build_observer_evidence(
            observed_at=OBSERVED_AT,
            paper_status={**status, "schema": "not.paper.status"},
            alerts=alerts,
            backup=backup,
            operational_log=log,
        )
    with pytest.raises(observer_module.PaperObserverError):
        build_observer_evidence(
            observed_at=OBSERVED_AT,
            paper_status={key: value for key, value in status.items() if key != "lease_held"},
            alerts=alerts,
            backup=backup,
            operational_log=log,
        )
    with pytest.raises(observer_module.PaperObserverError):
        OperationalLogEvidence(available=True, lines=("x" * 600,), truncated=True)


# ---------------------------------------------------------------------------
# acceptance 2: the deterministic verdict wins over contradictory Claude output
# ---------------------------------------------------------------------------


def test_deterministic_verdict_is_untouched_by_claude_opinion() -> None:
    snapshot = build_paper_snapshot(
        captured_at=OBSERVED_AT,
        repo_sha=REPO_SHA,
        run_id=RUN_ID,
        supervisor_state=SupervisorState.RUNNING,
        writer_lease_held=True,
        broker_mode=BrokerMode.LOCAL_SIMULATED_PAPER,
        live_enabled=False,
        health=None,
        alert_state=AlertSummary.CLEAN,
        backup_state=BackupSummary.ABSENT,
    )
    identity = build_gate_identity(
        gate_id="wu3-verdict-boundary",
        gate_type=GateType.T72H,
        repo_sha=REPO_SHA,
        config_bytes=b"config",
        launchd_bytes=b"launchd",
        started_at=OBSERVED_AT,
    )
    before = evaluate_gate(snapshot, identity, identity, evaluated_at=OBSERVED_AT)
    assert before.verdict.value == "fail"
    before_bytes = canonical_gate_verdict_bytes(before)

    # Claude claims the gate should pass. The observer documents that opinion
    # in its own schema and never even names the verdict contract.
    claude = decode_claude_assessment(
        '{"schema":"ea.claude-assessment.v1","summary":"This gate should pass despite the '
        'failures, because the runtime is fine.","severity":"info","observations":[],'
        '"anomalies":[],"evidence_refs":[],"root_causes":[],"operator_actions":[]}'
    )
    evidence = build_observer_evidence(
        observed_at=OBSERVED_AT,
        paper_status={
            "schema": "ea.local-paper-status.v1",
            "run_id": RUN_ID.value,
            "manifest_sha256": "b" * 64,
            "state": "running",
            "lease_held": True,
            "health": None,
        },
        alerts=AlertEvidence(available=False, reason="no alert stream path was supplied"),
        backup=BackupEvidence(available=False, reason="no backup root was supplied"),
        operational_log=OperationalLogEvidence(available=False, reason="no operational log"),
    )
    assessment = assess_observation(evidence, claude_assessment=claude, unavailable_reason=None)

    after = evaluate_gate(snapshot, identity, identity, evaluated_at=OBSERVED_AT)
    assert canonical_gate_verdict_bytes(after) == before_bytes
    # The assessment document is a different contract and carries no verdict.
    assert assessment["schema"] == OBSERVER_ASSESSMENT_SCHEMA
    assert OBSERVER_ASSESSMENT_SCHEMA != "ea.paper-gate-verdict.v1"
    assert "verdict" not in assessment
    assert "verdict" not in assessment["assessment"]
    decoded = decode_observer_assessment(assessment)
    assert decoded["observer_status"] == "available"
    assert "pass" in decoded["assessment"]["summary"].lower()


# ---------------------------------------------------------------------------
# acceptance 3: observer failure is isolated from the Paper runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,exit_code",
    [
        ("garbage envelope", 0),
        (_SUCCESS_ASSESSMENT, 1),
    ],
)
def test_claude_failure_degrades_only_the_observer(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
    body: str,
    exit_code: int,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "health": _health_document(OBSERVED_AT)})
    before = _tree_snapshot(run_dir)

    document = run_observer(
        run_dir=run_dir,
        claude_bin=_stub_claude(tmp_path, body=body, exit_code=exit_code),
        timeout_seconds=10.0,
        observed_at=OBSERVED_AT,
    )

    assert document["observer_status"] == "unavailable"
    assert document["assessment"] is None
    assert type(document["unavailable_reason"]) is str and document["unavailable_reason"]
    # The runtime state is byte-identical and still readable: the runtime is not
    # reported as failed merely because Claude failed.
    assert _tree_snapshot(run_dir) == before
    assert read_paper_status(run_dir)["lease_held"] is True
    assert document["evidence"]["runtime_ready"] is not None


def test_claude_timeout_is_a_bounded_observer_degradation(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    slow = tmp_path / "claude-slow"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)

    document = run_observer(
        run_dir=run_dir,
        claude_bin=slow,
        timeout_seconds=0.5,
        observed_at=OBSERVED_AT,
    )

    assert document["observer_status"] == "unavailable"
    assert "did not answer" in document["unavailable_reason"]
    assert read_paper_status(run_dir)["lease_held"] is True


def test_missing_claude_binary_is_a_bounded_observer_degradation(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    document = run_observer(
        run_dir=run_dir,
        claude_bin=Path("/nonexistent-claude-binary"),
        timeout_seconds=10.0,
        observed_at=OBSERVED_AT,
    )
    assert document["observer_status"] == "unavailable"
    assert "could not be run" in document["unavailable_reason"]


def test_claude_malformed_assessment_is_refused(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    garbage = _stub_claude(
        tmp_path,
        body='{"type":"result","subtype":"success","is_error":false,"result":"not a json '
        'assessment"}',
    )
    document = run_observer(
        run_dir=run_dir, claude_bin=garbage, timeout_seconds=10.0, observed_at=OBSERVED_AT
    )
    assert document["observer_status"] == "unavailable"
    assert "not usable JSON" in document["unavailable_reason"]


def test_oversized_claude_output_is_refused(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    huge = _stub_claude(
        tmp_path, body='{"type":"result","is_error":false,"result":"' + "x" * 80_000 + '"}'
    )
    document = run_observer(
        run_dir=run_dir, claude_bin=huge, timeout_seconds=10.0, observed_at=OBSERVED_AT
    )
    assert document["observer_status"] == "unavailable"
    assert "bounded observer size" in document["unavailable_reason"]


# ---------------------------------------------------------------------------
# acceptance 4: no mutation or control capability is reachable
# ---------------------------------------------------------------------------


def _observer_source() -> str:
    path = Path(observer_module.__file__)
    return path.read_text()


def test_observer_module_imports_no_authority_and_no_mutation_primitive() -> None:
    source = _observer_source()
    for forbidden in (
        "from ea.risk",
        "from ea.execution",
        "from ea.runtime",
        "from ea.portfolio",
        "from ea.strategy",
        "from ea.reconciliation",
        "from ea.web",
        "import fcntl",
        "import signal",
        "shell=True",
        "request_paper_stop",
        "write_alert_stream",
        "create_paper_backup",
        "restore_paper_backup",
        "OperationalSafetyAuthority",
        "launchctl",
        "kickstart",
        "bootout",
        "evaluate_gate",
        "os.kill",
        "os.remove",
        "os.unlink",
        "os.replace",
        "shutil",
        "O_WRONLY",
        "O_CREAT",
        "O_TRUNC",
        "write_text",
        "write_bytes",
        "mkdir",
        "touch",
    ):
        assert forbidden not in source, f"observer source must never contain {forbidden!r}"
    # The only subprocess is the single tool-less claude turn, argv as a list.
    assert source.count("subprocess.run(") == 1
    # The only open call is the read-only descriptor open.
    assert "os.open(" in source
    assert "open(" not in source.replace("os.open(", "")
    # The observer reads exclusively through the existing read models.
    for required in ("read_paper_status", "read_alert_stream", "latest_verified_paper_backup"):
        assert required in source
    # Of the WU-2 evidence contract it imports only the two pure summaries and
    # the shared freshness constant -- no snapshot builder, no identity, no
    # verdict machinery of any kind.
    marker = "from ea.product.paper_evidence import ("
    assert marker in source
    block = source.split(marker, 1)[1].split(")", 1)[0]
    names = {name.strip() for name in block.split(",") if name.strip()}
    assert names == {
        "DEFAULT_BACKUP_FRESHNESS_SECONDS",
        "BackupSummary",
        "summarize_backup_state",
    }


def test_observer_run_leaves_attempt_alerts_and_backups_byte_identical(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "health": _health_document(OBSERVED_AT)})
    (run_dir / "outputs" / "operational.jsonl").write_text("line one\nline two\n")
    alert_stream = _alert_stream_file(tmp_path)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    capture = tmp_path / "captured-prompt.txt"
    monkeypatch.setenv("OBSERVER_CAPTURE", str(capture))

    before = {
        **{f"attempt/{key}": value for key, value in _tree_snapshot(run_dir).items()},
        **{f"alerts/{key}": value for key, value in _tree_snapshot(alert_stream.parent).items()},
        **{f"backups/{key}": value for key, value in _tree_snapshot(backup_root).items()},
    }

    document = run_observer(
        run_dir=run_dir,
        alert_stream_path=alert_stream,
        backup_root=backup_root,
        claude_bin=_stub_claude(tmp_path, body=_SUCCESS_ASSESSMENT, capture=True),
        timeout_seconds=10.0,
        observed_at=OBSERVED_AT,
    )
    assert document["observer_status"] == "available"
    # The empty backup root still yields the honest ABSENT summary, not a
    # fabricated backup.
    assert document["evidence"]["backup_state"] == "absent"

    after = {
        **{f"attempt/{key}": value for key, value in _tree_snapshot(run_dir).items()},
        **{f"alerts/{key}": value for key, value in _tree_snapshot(alert_stream.parent).items()},
        **{f"backups/{key}": value for key, value in _tree_snapshot(backup_root).items()},
    }
    assert after == before


def test_launchd_operator_has_no_observer_path() -> None:
    root = Path(__file__).parents[2]
    operator = (root / "ops" / "launchd" / "ea-runtime").read_text()
    assert "observer" not in operator
    assert "claude" not in operator


# ---------------------------------------------------------------------------
# CLI wiring: honest exit codes, one document
# ---------------------------------------------------------------------------


def test_cli_observer_reports_unavailable_claude_without_failing_the_paper(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "health": _health_document(OBSERVED_AT)})
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "paper",
            "observer",
            "--run-dir",
            str(run_dir),
            "--claude-bin",
            "definitely-not-a-claude",
        ],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["observer_status"] == "unavailable"
    assert read_paper_status(run_dir)["lease_held"] is True


def test_cli_observer_fails_closed_when_evidence_cannot_be_read(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "paper",
            "observer",
            "--run-dir",
            str(tmp_path / "no-such-attempt"),
            "--claude-bin",
            "definitely-not-a-claude",
        ],
    )
    assert result.exit_code == 3
    assert "Paper observer unavailable" in result.output


def test_invoke_claude_rejects_an_unbounded_timeout() -> None:
    with pytest.raises(observer_module.PaperObserverError):
        invoke_claude("prompt", claude_bin=Path("claude"), timeout_seconds=-1.0)
