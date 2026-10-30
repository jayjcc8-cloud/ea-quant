"""Real owner observations -> sealed boundary -> settled backup -> existing evaluator."""

import json

# These composition tests run the actual foreground Paper process, with fresh
# synthetic Candidate evidence. They do not touch launchd or user runtime data.
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ea.cli.app import app
from ea.product.paper_backup import create_paper_backup
from ea.product.paper_evidence import GateType, decode_paper_snapshot
from ea.product.paper_gate_history import decode_paper_interval_history
from ea.product.paper_interval_capture import (
    PaperIntervalError,
    begin_interval,
    evaluate_interval,
    observe_interval,
    settle_interval,
)
from ea.product.paper_session import PaperSessionError, read_paper_status, request_paper_stop
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_candidate_runtime import _create_candidate_evidence
from unit.test_paper_gate_prep import REPO, Live, _operator_surface


def test_cli_seals_real_boundary_and_reads_it_after_stop(tmp_path: Path) -> None:
    live = Live(tmp_path, at=datetime.now(UTC))
    config, launchd = _operator_surface(tmp_path)
    interval = tmp_path / "interval"
    runner = CliRunner()
    begin = runner.invoke(
        app,
        [
            "paper",
            "gate",
            "interval",
            "begin",
            "--interval",
            str(interval),
            "--run-dir",
            str(live.run_dir),
            "--repo",
            str(REPO),
            "--config",
            str(config),
            "--launchd-dir",
            str(launchd),
            "--gate-id",
            "short-real",
            "--gate-type",
            "72h",
        ],
    )
    try:
        assert begin.exit_code == 0, begin.output
        end = runner.invoke(app, ["paper", "gate", "interval", "end", "--interval", str(interval)])
        assert end.exit_code == 0, end.output
    finally:
        live.paper.quiesce()
    backup = create_paper_backup(live.run_dir, tmp_path / "backups").backup_dir
    settle = runner.invoke(
        app,
        [
            "paper",
            "gate",
            "interval",
            "settle",
            "--interval",
            str(interval),
            "--backup",
            str(backup),
        ],
    )
    assert settle.exit_code == 0, settle.output
    result = runner.invoke(
        app, ["paper", "gate", "interval", "evaluate", "--interval", str(interval)]
    )
    assert result.exit_code == 3, result.output
    document = json.loads(result.stdout)
    history = decode_paper_interval_history(document["history"])
    snapshot = decode_paper_snapshot(document["boundary_snapshot"])
    assert snapshot.writer_lease_held is True
    assert document["historical_verdict"]["evaluated_at"] == document["ended_at"]
    assert (
        snapshot.captured_at == history.coverage_end == datetime.fromisoformat(document["ended_at"])
    )
    assert document["current_status"]["lease_held"] is False
    assert "coverage_gap" in document["historical_verdict"]["reasons"]
    assert document["historical_verdict"]["verdict"] != "pass"


@pytest.fixture(scope="module")
def accepted(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, Path]:
    root = tmp_path_factory.mktemp("interval-candidate")
    scenario = bounded_scenario(root / "scenarios")
    _create_candidate_evidence(root, scenario, None)
    selection = json.loads((root / "selection.json").read_text())
    return root / "workspace", selection["candidate_id"], scenario


@contextmanager
def running(
    accepted: tuple[Path, str, Path], root: Path
) -> Iterator[tuple[Path, subprocess.Popen[bytes]]]:
    workspace, candidate, scenario = accepted
    run_dir = root / str(uuid4())
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ea.cli.app",
            "paper",
            "start",
            "--workspace",
            str(workspace),
            "--candidate-id",
            candidate,
            "--scenario",
            str(scenario),
            "--output-root",
            str(root),
            "--run-id",
            run_dir.name,
            "--prices",
            "100",
            "--interval",
            "0.02",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1])
            try:
                status = read_paper_status(run_dir)
                if (
                    status.get("health", {}).get("runtime_ready") is True
                    and status.get("fills") == 6
                ):
                    break
            except PaperSessionError:
                pass
            time.sleep(0.02)
        else:
            raise AssertionError("isolated Paper did not become ready")
        yield run_dir, process
    finally:
        if process.poll() is None:
            request_paper_stop(run_dir)
        process.wait(timeout=10)
        if process.stderr:
            process.stderr.close()


def start_capture(tmp_path: Path, run: Path) -> Path:
    config, launchd = _operator_surface(tmp_path)
    pointer = tmp_path / "current-run"
    pointer.write_text(str(run))
    interval = tmp_path / "interval"
    begin_interval(
        interval,
        run_dir=run,
        repository=REPO,
        config=config,
        launchd_dir=launchd,
        gate_id="observed",
        gate_type=GateType.T72H,
        current_run=pointer,
    )
    return interval


def backup(run: Path, tmp_path: Path) -> Path:
    return create_paper_backup(run, tmp_path / "backups", keep=20).backup_dir


def test_real_clean_stop_after_seal_does_not_interrupt_the_window(
    tmp_path: Path, accepted: tuple[Path, str, Path]
) -> None:
    with running(accepted, tmp_path / "runs") as (run, _):
        interval = start_capture(tmp_path, run)
        observe_interval(interval)
        sealed = observe_interval(interval, end=True)
    captured = backup(run, tmp_path)
    settle_interval(interval, [captured])
    result = evaluate_interval(interval)
    assert result["boundary_snapshot"] == sealed["snapshot"]
    assert result["boundary_snapshot"]["runtime_state"] == "running"
    assert result["current_status"]["state"] == "stopped"
    assert result["current_status"]["ended_at"] > result["ended_at"]
    assert "attempt_interrupted" not in result["historical_verdict"]["reasons"]
    assert "coverage_gap" in result["historical_verdict"]["reasons"]
    assert result["history"]["associations"][0]["end"] == result["ended_at"]
    saved = (interval / "observations.jsonl").read_bytes()
    with pytest.raises(PaperIntervalError, match="already sealed"):
        observe_interval(interval, end=True)
    assert (interval / "observations.jsonl").read_bytes() == saved
    with pytest.raises(FileExistsError):
        settle_interval(interval, [captured])


@pytest.mark.parametrize("crash", [False, True])
def test_real_interruption_is_retained_when_replacement_is_healthy(
    tmp_path: Path, accepted: tuple[Path, str, Path], crash: bool
) -> None:
    with running(accepted, tmp_path / "runs") as (old, process):
        interval = start_capture(tmp_path, old)
        if crash:
            process.kill()
            process.wait(timeout=10)
        else:
            request_paper_stop(old)
    with running(accepted, tmp_path / "runs") as (new, _):
        (tmp_path / "current-run").write_text(str(new))
        observe_interval(interval)
        observe_interval(interval, end=True)
    settle_interval(interval, [backup(old, tmp_path), backup(new, tmp_path)])
    result = evaluate_interval(interval)
    assert {item["binding"]["run_id"] for item in result["history"]["attempts"]} == {
        old.name,
        new.name,
    }
    assert "attempt_interrupted" in result["historical_verdict"]["reasons"]
    assert "coverage_gap" in result["historical_verdict"]["reasons"]
    assert result["boundary_snapshot"]["run_id"] == new.name
    assert result["historical_verdict"]["verdict"] == "fail"


def test_observed_drift_and_unavailable_reads_survive_restoration(
    tmp_path: Path, accepted: tuple[Path, str, Path]
) -> None:
    with running(accepted, tmp_path / "runs") as (run, _):
        interval = start_capture(tmp_path, run)
        config = tmp_path / "run-config.env"
        original = config.read_bytes()
        config.write_bytes(original + b"BACKUP_KEEP=10\n")
        observe_interval(interval)
        config.unlink()
        with pytest.raises(PaperIntervalError, match="failure retained"):
            observe_interval(interval, end=True)
        config.write_bytes(original)
        observe_interval(interval, end=True)
    settle_interval(interval, [backup(run, tmp_path)])
    result = evaluate_interval(interval)
    assert result["historical_verdict"]["verdict"] == "invalid"
    assert "config_drift" in result["historical_verdict"]["reasons"]
    assert result["inventory_errors"]
    assert result["boundary_identity"] == result["history"]["identity"]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "omitted"])
def test_backup_gaps_remain_visible_on_readback(
    tmp_path: Path, accepted: tuple[Path, str, Path], damage: str
) -> None:
    with running(accepted, tmp_path / "runs") as (run, _):
        interval = start_capture(tmp_path, run)
        observe_interval(interval, end=True)
    captured = backup(run, tmp_path)
    settle_interval(interval, [] if damage == "omitted" else [captured])
    if damage == "missing":
        captured.rename(tmp_path / "moved-backup")
    elif damage == "corrupt":
        (captured / "files" / "manifest.json").write_text("broken")
    result = evaluate_interval(interval)
    assert ("corrupt_evidence" if damage == "corrupt" else "missing_evidence") in result[
        "historical_verdict"
    ]["reasons"]
    assert result["historical_verdict"]["verdict"] != "pass"


def test_unobserved_middle_attempt_and_missing_inventory_cannot_be_cropped(
    tmp_path: Path, accepted: tuple[Path, str, Path]
) -> None:
    with running(accepted, tmp_path / "runs") as (run, _):
        interval = start_capture(tmp_path, run)
        with running(accepted, tmp_path / "runs") as (middle, _):
            pass
        observe_interval(interval)
        middle_backup = backup(middle, tmp_path)
        middle.rename(tmp_path / "retained-middle")
        observe_interval(interval, end=True)
    settle_interval(interval, [backup(run, tmp_path), middle_backup])
    result = evaluate_interval(interval)
    assert "unassociated_evidence" in result["historical_verdict"]["reasons"]
    assert result["inventory_errors"] == ["attempt inventory disappeared during the interval"]
    path = interval / "history.json"
    history = json.loads(path.read_bytes())
    history["attempts"] = [
        item for item in history["attempts"] if item["binding"]["run_id"] != middle.name
    ]
    path.write_text(json.dumps(history))
    with pytest.raises(PaperIntervalError, match="conflicts with the collected"):
        evaluate_interval(interval)


def test_launch_inventory_gaps_and_truncation_survive_later_reads(
    tmp_path: Path, accepted: tuple[Path, str, Path]
) -> None:
    with running(accepted, tmp_path / "runs") as (run, _):
        config, launchd = _operator_surface(tmp_path)
        launches = tmp_path / "launch-history.jsonl"
        # An old, already pruned launch is before the observed window.
        old = str(uuid4())
        launches.write_text(
            json.dumps({"event": "launch", "run_id": old, "run_dir": str(run.parent / old)}) + "\n"
        )
        interval = tmp_path / "interval"
        begin_interval(
            interval,
            run_dir=run,
            repository=REPO,
            config=config,
            launchd_dir=launchd,
            gate_id="inventory",
            gate_type=GateType.T72H,
            launch_history=launches,
        )
        missing = str(uuid4())
        original = launches.read_text()
        launches.write_text(
            original
            + json.dumps(
                {"event": "launch", "run_id": missing, "run_dir": str(run.parent / missing)}
            )
            + "\n"
        )
        observe_interval(interval)
        launches.write_text(original)
        observe_interval(interval, end=True)
    settle_interval(interval, [backup(run, tmp_path)])
    result = evaluate_interval(interval)
    assert f"launch inventory missing: {missing}" in result["inventory_errors"]
    assert "launch inventory was truncated or replaced" in result["inventory_errors"]
    assert all(old not in error for error in result["inventory_errors"])
    assert "coverage_gap" in result["historical_verdict"]["reasons"]


def test_collection_has_no_manual_clock_or_health_override() -> None:
    runner = CliRunner()
    for command in ("begin", "end", "observe"):
        help_text = runner.invoke(app, ["paper", "gate", "interval", command, "--help"]).stdout
        assert "--started-at" not in help_text
        assert "--ended-at" not in help_text
        assert "--supervisor-state" not in help_text
