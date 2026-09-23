from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ea.cli.app import app
from unit.test_backtest_single_run import _scenario


def test_strict_cli_emits_run_logs_and_readers_leave_attempt_unchanged(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path / "inputs")
    runs = (tmp_path / "runs").resolve()
    runner = CliRunner()

    executed = runner.invoke(
        app, ["backtest", "run", "--scenario", str(scenario), "--output-root", str(runs)]
    )
    assert executed.exit_code == 0, executed.output
    attempt = next(path for path in runs.iterdir() if path.is_dir())
    events = [json.loads(line) for line in (attempt / "operational.jsonl").read_bytes().splitlines()]
    assert events
    assert {event["run_id"] for event in events} == {attempt.name}
    assert {event["strategy_id"] for event in events} == {"bounded-long-v1"}
    assert any(event.get("order_id") and event.get("client_order_id") for event in events)
    assert any(event.get("fill_id") for event in events)

    def snapshot() -> dict[str, bytes]:
        return {
            str(path.relative_to(attempt)): path.read_bytes()
            for path in attempt.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    reported = runner.invoke(
        app,
        ["backtest", "report", "--run-dir", str(attempt), "--output-dir", str(tmp_path / "report")],
    )
    assert reported.exit_code == 0, reported.output
    resumed = runner.invoke(app, ["backtest", "resume", "--run-dir", str(attempt)])
    assert resumed.exit_code == 0, resumed.output
    assert snapshot() == before
