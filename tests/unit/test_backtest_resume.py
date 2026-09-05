from __future__ import annotations

import json
import os
import stat
from hashlib import sha256
from pathlib import Path

import pytest

import ea.product.backtest as backtest_module
from ea.core import Sha256Digest
from ea.product import (
    BacktestResumeFailure,
    BacktestRunFailure,
    load_backtest_scenario,
    resume_backtest_attempt,
    run_backtest_scenario,
)
from unit.test_backtest_single_run import _report, _scenario


class _AbruptInterruption(BaseException):
    pass


def _interrupt_at(monkeypatch: pytest.MonkeyPatch, selected: str) -> None:
    def interrupt(stage: str) -> None:
        if stage == selected:
            raise _AbruptInterruption(stage)

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", interrupt)


def _attempt(root: Path) -> Path:
    attempts = tuple(path for path in root.iterdir() if path.is_dir())
    assert len(attempts) == 1
    return attempts[0]


def _tree_digest(root: Path) -> str:
    digest = sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_file():
            payload = path.read_bytes()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def _trace_fsync_kind(file_descriptor: int) -> str:
    return "directory" if stat.S_ISDIR(os.fstat(file_descriptor).st_mode) else "file"


def _baseline(tmp_path: Path) -> dict[str, object]:
    scenario = load_backtest_scenario(_scenario(tmp_path / "baseline-input"))
    completed = run_backtest_scenario(scenario, (tmp_path / "baseline-runs").resolve())
    return _report(completed.output_directory)


def test_resume_after_durable_funding_preserves_attempt_and_economics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(tmp_path)
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "funding_durable")

    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)

    attempt = _attempt(runs)
    original_run_id = attempt.name
    assert (attempt / "funding.json").is_file()
    assert not (attempt / "result.json").exists()

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)
    completed = resume_backtest_attempt(attempt)
    resumed = _report(completed.output_directory)

    assert resumed["run_id"] == original_run_id
    assert resumed["lineage_sha256"] == baseline["lineage_sha256"]
    assert resumed["semantic_outcome_sha256"] == baseline["semantic_outcome_sha256"]
    assert resumed["ending_cash"] == baseline["ending_cash"]
    assert resumed["ending_positions"] == baseline["ending_positions"]
    assert resumed["ledger_sequence"] == 2


def test_resume_after_durable_fill_does_not_duplicate_economic_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(tmp_path)
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "dispatch_durable")

    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)

    attempt = _attempt(runs)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)
    resumed = _report(resume_backtest_attempt(attempt).output_directory)

    assert resumed["order"]["quantity"] == baseline["order"]["quantity"]  # type: ignore[index]
    assert resumed["order"]["side"] == baseline["order"]["side"]  # type: ignore[index]
    assert resumed["fill"]["quantity"] == baseline["fill"]["quantity"]  # type: ignore[index]
    assert resumed["fill"]["price"] == baseline["fill"]["price"]  # type: ignore[index]
    assert resumed["ending_cash"] == [{"amount": "9797", "currency": "USD"}]
    assert resumed["ending_positions"] == [{"quantity": "2", "symbol": "AAPL", "venue": "XNAS"}]
    assert resumed["ledger_sequence"] == 2
    assert resumed["semantic_outcome_sha256"] == baseline["semantic_outcome_sha256"]


def test_resume_after_reconciliation_publishes_exactly_one_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "reconciliation_durable")

    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)

    attempt = _attempt(runs)
    assert not (attempt / "result.json").exists()
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)

    first = resume_backtest_attempt(attempt)
    before = _tree_digest(attempt)
    second = resume_backtest_attempt(attempt)

    assert first == second
    assert _tree_digest(attempt) == before
    assert json.loads((attempt / "result.json").read_bytes())["status"] == "success"
    audit = (attempt / "audit.jsonl").read_text(encoding="ascii")
    assert audit.count('"record_kind":"run.terminal"') == 1


def test_funding_is_directory_durable_before_frontier_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    observed: list[str] = []
    at_interrupt: list[str] = []
    real_fsync = os.fsync

    def trace_fsync(file_descriptor: int) -> None:
        observed.append(_trace_fsync_kind(file_descriptor))
        real_fsync(file_descriptor)

    def interrupt(stage: str) -> None:
        if stage == "funding_durable":
            at_interrupt.extend(observed)
            raise _AbruptInterruption(stage)

    monkeypatch.setattr(os, "fsync", trace_fsync)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", interrupt)

    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())

    assert at_interrupt[-2:] == ["file", "directory"]


def test_failure_evidence_is_directory_durable_before_failure_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    observed: list[str] = []
    real_fsync = os.fsync

    def trace_fsync(file_descriptor: int) -> None:
        observed.append(_trace_fsync_kind(file_descriptor))
        real_fsync(file_descriptor)

    def fail(**_kwargs: object) -> object:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(os, "fsync", trace_fsync)
    monkeypatch.setattr(backtest_module, "_execute", fail)

    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())

    assert observed[-2:] == ["file", "directory"]


def test_resume_rejects_result_left_by_failed_directory_sync_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    real_fsync = os.fsync
    failed = False

    def fail_result_directory_sync(file_descriptor: int) -> None:
        nonlocal failed
        is_directory = stat.S_ISDIR(os.fstat(file_descriptor).st_mode)
        result_exists = runs.exists() and any(
            (child / "result.json").exists() for child in runs.iterdir() if child.is_dir()
        )
        if is_directory and result_exists and not failed:
            failed = True
            raise OSError("injected result directory sync failure")
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_result_directory_sync)
    with pytest.raises(BacktestRunFailure, match="atomically published"):
        run_backtest_scenario(scenario, runs)

    attempt = _attempt(runs)
    assert failed
    assert (attempt / "result.json").is_file()
    assert (attempt / "result.publication.json").is_file()
    before = _tree_digest(attempt)
    monkeypatch.setattr(os, "fsync", real_fsync)

    with pytest.raises(BacktestResumeFailure, match="ambiguous"):
        resume_backtest_attempt(attempt)

    assert _tree_digest(attempt) == before


def test_completed_attempt_resume_is_validated_non_mutating_no_op(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    before = _tree_digest(completed.output_directory)

    resumed = resume_backtest_attempt(completed.output_directory)

    assert resumed == completed
    assert _tree_digest(completed.output_directory) == before


def test_resume_rejects_noncanonical_attempt_directory_name_without_mutation(
    tmp_path: Path,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    canonical = completed.output_directory
    alias = canonical.parent / f"{{{canonical.name}}}"
    alias.mkdir()
    (alias / "manifest.json").write_bytes((canonical / "manifest.json").read_bytes())
    before_canonical = _tree_digest(canonical)
    before_alias = _tree_digest(alias)

    with pytest.raises(BacktestResumeFailure, match="directory identity"):
        resume_backtest_attempt(alias)

    assert _tree_digest(canonical) == before_canonical
    assert _tree_digest(alias) == before_alias


def test_failed_attempt_resume_rejects_without_mutation(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input", funding__initial_cash="50"))
    with pytest.raises(BacktestRunFailure) as captured:
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    attempt = captured.value.output_directory
    before = _tree_digest(attempt)

    with pytest.raises(BacktestResumeFailure, match="failed attempt"):
        resume_backtest_attempt(attempt)

    assert _tree_digest(attempt) == before


@pytest.mark.parametrize("conflict", ["scenario", "data", "distribution"])
def test_resume_identity_conflict_fails_closed_without_attempt_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
) -> None:
    scenario_path = _scenario(tmp_path / "input")
    scenario = load_backtest_scenario(scenario_path)
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "funding_durable")
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)
    attempt = _attempt(runs)
    before = _tree_digest(attempt)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)
    if conflict == "scenario":
        scenario_path.write_text(scenario_path.read_text() + "\n", encoding="utf-8")
    elif conflict == "data":
        scenario.data_path.write_bytes(scenario.data_path.read_bytes() + b"\n")
    else:
        monkeypatch.setattr(
            backtest_module,
            "_package_code_digest",
            lambda: Sha256Digest("f" * 64),
        )

    with pytest.raises(BacktestResumeFailure, match="identity"):
        resume_backtest_attempt(attempt)

    assert _tree_digest(attempt) == before


def test_resume_rejects_committed_journal_corruption_without_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "funding_durable")
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)
    attempt = _attempt(runs)
    journal = attempt / "audit" / "audit-v1.journal"
    corrupted = bytearray(journal.read_bytes())
    corrupted[-1] ^= 1
    journal.write_bytes(corrupted)
    before = _tree_digest(attempt)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)

    with pytest.raises(BacktestResumeFailure, match="audit"):
        resume_backtest_attempt(attempt)

    assert _tree_digest(attempt) == before
    assert not (attempt / "result.json").exists()


def test_resume_rejects_partial_success_artifact_before_terminal_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "funding_durable")
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)
    attempt = _attempt(runs)
    (attempt / "result.pending").write_bytes(b'{"status":"success"}\n')
    before = _tree_digest(attempt)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)

    with pytest.raises(BacktestResumeFailure, match="ambiguous"):
        resume_backtest_attempt(attempt)

    assert _tree_digest(attempt) == before
    assert not (attempt / "result.json").exists()


def test_handled_internal_failure_retains_classified_durable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))

    def fail(**_kwargs: object) -> object:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(backtest_module, "_execute", fail)

    with pytest.raises(BacktestRunFailure) as captured:
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())

    attempt = captured.value.output_directory
    failure = json.loads((attempt / "failure.json").read_bytes())
    assert failure["classification"] == "backtest.internal_failure"
    assert failure["run_id"] == attempt.name
    assert failure["lineage_sha256"]
    assert failure["last_durable_frontier"] == "attempt_prepared"
    assert failure["terminal_state"] == "failed"
    assert "sensitive internal detail" not in (attempt / "failure.json").read_text()
    assert not (attempt / "result.json").exists()


@pytest.mark.parametrize(
    ("stage", "expected_frontier"),
    [
        ("dispatch_durable", "dispatch_durable"),
        ("reconciliation_durable", "reconciliation_durable"),
    ],
)
def test_fresh_handled_failure_reports_actual_last_durable_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_frontier: str,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))

    def fail(selected: str) -> None:
        if selected == stage:
            raise RuntimeError("injected handled failure")

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", fail)
    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())

    failure = json.loads((_attempt(tmp_path / "runs") / "failure.json").read_bytes())
    assert failure["last_durable_frontier"] == expected_frontier


@pytest.mark.parametrize(
    ("stage", "expected_frontier"),
    [
        ("dispatch_durable", "dispatch_durable"),
        ("reconciliation_durable", "reconciliation_durable"),
    ],
)
def test_resume_handled_failure_reports_actual_last_durable_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_frontier: str,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    runs = (tmp_path / "runs").resolve()
    _interrupt_at(monkeypatch, "funding_durable")
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, runs)
    attempt = _attempt(runs)

    def fail(selected: str) -> None:
        if selected == stage:
            raise RuntimeError("injected resume failure")

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", fail)
    with pytest.raises(BacktestResumeFailure, match="internal failure"):
        resume_backtest_attempt(attempt)

    failure = json.loads((attempt / "failure.json").read_bytes())
    assert failure["last_durable_frontier"] == expected_frontier
