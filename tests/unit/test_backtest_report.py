from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

import ea.product as product
import ea.product.backtest as backtest_module
import ea.product.reporting as reporting_module
from ea.cli.app import app
from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.product import load_backtest_scenario, resume_backtest_attempt, run_backtest_scenario
from ea.product.identity import semantic_outcome_sha256
from unit.test_backtest_single_run import _scenario


class _AbruptInterruption(BaseException):
    pass


def _evidence_tree(attempt: Path) -> dict[str, str]:
    return {
        str(path.relative_to(attempt)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(attempt.rglob("*"))
        if path.is_file()
    }


def _priced_scenario(tmp_path: Path, *, flat: bool = False) -> Path:
    scenario_path = _scenario(
        tmp_path,
        **({"strategy__id": "always-flat-v1", "strategy__target_quantity": None} if flat else {}),
    )
    data_path = tmp_path / "prices.csv"
    later = (
        "1,XNAS,AAPL,2026-01-02T09:32:00.000000Z,2026-01-02T09:33:00.000000Z,"
        "raw,108.0,111.0,107.0,110.0,9.0,fixture.raw,3,0,"
        "2026-01-02T09:33:00.000000Z\n"
    )
    data_path.write_text(data_path.read_text(encoding="utf-8") + later, encoding="utf-8")
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 34, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    document["data"]["end_utc"] = "2026-01-02T09:34:00.000000Z"
    document["data"]["fingerprint"] = {
        "record_count": dataset.selection.fingerprint.record_count,
        "sha256": dataset.selection.fingerprint.sha256.value,
    }
    scenario_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return scenario_path


def _report(attempt: Path, output: Path) -> dict[str, object]:
    product.generate_backtest_report(attempt, output)
    value = json.loads((output / "report.json").read_bytes())
    assert isinstance(value, dict)
    return value


def _economic_projection(report: dict[str, object]) -> dict[str, object]:
    return {
        "economics": report["economics"],
        "lineage_sha256": report["lineage_sha256"],
        "semantic_outcome_sha256": report["completion"]["semantic_outcome_sha256"],  # type: ignore[index]
    }


def _result_semantic_projection(result: dict[str, object]) -> dict[str, object]:
    fill = cast(dict[str, object] | None, result["fill"])
    order = cast(dict[str, object] | None, result["order"])
    initial = cast(dict[str, object], result["initial_funding"])
    return {
        "ending_cash": result["ending_cash"],
        "ending_positions": result["ending_positions"],
        "fill": (
            None if fill is None else {key: fill[key] for key in ("price", "quantity", "side")}
        ),
        "initial_funding": {"amount": initial["amount"], "currency": initial["currency"]},
        "lineage_sha256": result["lineage_sha256"],
        "order": (None if order is None else {key: order[key] for key in ("quantity", "side")}),
        "reconciliation": result["reconciliation"],
        "risk": result["risk"],
        "schema": "ea.backtest-semantic-outcome.v1",
        "strategy": result["strategy"],
        "terminal_state": "completed",
    }


def test_completed_attempt_report_is_canonical_repeatable_and_read_only(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    before = _evidence_tree(attempt)
    output = (tmp_path / "reports" / "accepted").resolve()

    completed = product.generate_backtest_report(attempt, output)
    first_json = (output / "report.json").read_bytes()
    first_text = (output / "summary.txt").read_bytes()
    first_identity = (
        (output / "report.json").stat().st_ino,
        (output / "summary.txt").stat().st_ino,
    )
    repeated = product.generate_backtest_report(attempt, output)

    assert completed.status == repeated.status == "success"
    assert isinstance(completed.report, product.BacktestReportV1)
    assert completed.report.canonical_bytes == first_json
    assert completed.report.summary_bytes == first_text
    assert completed.output_directory == repeated.output_directory == output
    assert first_json.endswith(b"\n")
    assert (
        json.dumps(
            json.loads(first_json),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
        == first_json
    )
    assert (output / "report.json").read_bytes() == first_json
    assert (output / "summary.txt").read_bytes() == first_text
    assert (
        (output / "report.json").stat().st_ino,
        (output / "summary.txt").stat().st_ino,
    ) == first_identity
    assert _evidence_tree(attempt) == before


def test_report_rejects_incomplete_attempt_without_creating_output(tmp_path: Path) -> None:
    attempt = (tmp_path / "00000000-0000-4000-8000-000000000083").resolve()
    attempt.mkdir()
    output = (tmp_path / "reports" / "report").resolve()

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, output)

    assert not output.exists()
    assert not output.parent.exists()


def test_report_field_source_table_is_small_and_versioned() -> None:
    table = product.BACKTEST_REPORT_FIELD_SOURCES

    assert tuple(table[0]) == ("field", "source", "source_version", "rule")
    assert 10 <= len(table) <= 24
    assert any(row[0] == "valuation.last_price" for row in table[1:])
    assert any(row[0] == "economics.total_return" for row in table[1:])


def test_funded_long_uses_last_admitted_price_and_manual_economics(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory

    report = _report(attempt, (tmp_path / "report").resolve())
    economics = cast(dict[str, Any], report["economics"])

    assert economics["ending_cash"] == [{"amount": "9797", "currency": "USD"}]
    assert economics["ending_positions"] == [{"quantity": "2", "symbol": "AAPL", "venue": "XNAS"}]
    assert economics["valuation"] == {
        "adjustment": "raw",
        "available_at": "2026-01-02T09:33:00.000000Z",
        "position_value": "220",
        "price": "110",
        "revision": 0,
        "rule": "last-admitted-close-v1",
        "source": "fixture.raw",
        "source_sequence": 3,
    }
    assert economics["equity"]["amount"] == "10017"
    assert economics["net_pnl"]["amount"] == "17"
    assert economics["total_return"]["value"] == "0.0017"
    assert economics["counts"] == {"fills": 1, "orders": 1}
    assert economics["execution"] == {
        "fill": {"price": "101.5", "quantity": "2", "side": "buy"},
        "order": {"quantity": "2", "side": "buy"},
    }
    assert economics["fees"] == {
        "amount": "0",
        "count": 1,
        "currency": "USD",
        "rule": "phase1-zero-commission-v1",
    }
    source = cast(dict[str, Any], report["source"])
    assert source["runtime"]["python_implementation"] == sys.implementation.name
    assert source["distribution"] == {"name": "ea-quant", "version": "0.2.0"}
    assert report["report_generator"] == {"distribution": "ea-quant", "version": "0.2.0"}


def test_funded_flat_report_has_zero_trade_metrics(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input", flat=True))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory

    report = _report(attempt, (tmp_path / "report").resolve())
    economics = cast(dict[str, Any], report["economics"])

    assert economics["ending_cash"] == [{"amount": "10000", "currency": "USD"}]
    assert economics["ending_positions"] == []
    assert economics["valuation"]["price"] == "110"
    assert economics["valuation"]["position_value"] == "0"
    assert economics["equity"]["amount"] == "10000"
    assert economics["net_pnl"]["amount"] == "0"
    assert economics["total_return"]["value"] == "0"
    assert economics["counts"] == {"fills": 0, "orders": 0}
    assert economics["execution"] == {"fill": None, "order": None}
    assert economics["fees"]["count"] == 0


def test_report_path_never_calls_trading_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("report attempted to execute trading")

    monkeypatch.setattr(backtest_module, "run_backtest_scenario", forbidden)
    monkeypatch.setattr(backtest_module, "resume_backtest_attempt", forbidden)
    monkeypatch.setattr(backtest_module, "_execute", forbidden)

    _report(attempt, (tmp_path / "report").resolve())


def test_equivalent_attempts_keep_distinct_identity_and_equal_economics(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input"))
    root = (tmp_path / "runs").resolve()
    first_attempt = run_backtest_scenario(scenario, root).output_directory
    second_attempt = run_backtest_scenario(scenario, root).output_directory

    first = _report(first_attempt, (tmp_path / "first-report").resolve())
    second = _report(second_attempt, (tmp_path / "second-report").resolve())

    assert first["run_id"] != second["run_id"]
    assert _economic_projection(first) == _economic_projection(second)
    assert (tmp_path / "first-report" / "report.json").read_bytes() != (
        tmp_path / "second-report" / "report.json"
    ).read_bytes()


def test_resumed_and_uninterrupted_attempts_have_equal_economic_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input"))
    root = (tmp_path / "runs").resolve()
    uninterrupted = run_backtest_scenario(scenario, root).output_directory

    def interrupt(stage: str) -> None:
        if stage == "funding_durable":
            raise _AbruptInterruption

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", interrupt)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, root)
    interrupted = next(path for path in root.iterdir() if path != uninterrupted)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)
    resume_backtest_attempt(interrupted)

    first = _report(uninterrupted, (tmp_path / "first-report").resolve())
    second = _report(interrupted, (tmp_path / "second-report").resolve())
    before_completed_resume = _evidence_tree(interrupted)
    resume_backtest_attempt(interrupted)

    assert _economic_projection(first) == _economic_projection(second)
    assert _evidence_tree(interrupted) == before_completed_resume


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-result",
        "result-status",
        "failure",
        "publication",
        "audit-export",
        "journal",
        "funding",
        "data",
    ],
)
def test_invalid_or_ambiguous_source_never_creates_report(tmp_path: Path, mutation: str) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    if mutation == "missing-result":
        (attempt / "result.json").unlink()
    elif mutation == "result-status":
        result = json.loads((attempt / "result.json").read_bytes())
        result["status"] = "failed"
        (attempt / "result.json").write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii"
        )
    elif mutation == "failure":
        (attempt / "failure.json").write_text("{}\n", encoding="ascii")
    elif mutation == "publication":
        (attempt / "result.publication.json").write_text("{}\n", encoding="ascii")
    elif mutation == "audit-export":
        (attempt / "audit.jsonl").write_bytes(b"{}\n")
    elif mutation == "funding":
        funding = json.loads((attempt / "funding.json").read_bytes())
        funding["amount"] = "9999"
        (attempt / "funding.json").write_text(
            json.dumps(funding, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
    elif mutation == "data":
        scenario.data_path.write_bytes(scenario.data_path.read_bytes() + b"\n")
    else:
        journal = attempt / "audit" / "audit-v1.journal"
        payload = bytearray(journal.read_bytes())
        payload[-1] ^= 1
        journal.write_bytes(payload)
    output = (tmp_path / "report").resolve()

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, output)

    assert not output.exists()


@pytest.mark.parametrize("mutate_fill", [True, False], ids=["fill-and-cash", "cash-only"])
def test_report_rejects_result_economics_not_bound_to_committed_fill(
    tmp_path: Path, *, mutate_fill: bool
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    result_path = attempt / "result.json"
    result = cast(dict[str, object], json.loads(result_path.read_bytes()))
    fill = cast(dict[str, object], result["fill"])
    cash = cast(list[dict[str, object]], result["ending_cash"])
    if mutate_fill:
        fill["price"] = "100"
    cash[0]["amount"] = "9800"
    result["semantic_outcome_sha256"] = semantic_outcome_sha256(
        _result_semantic_projection(result)
    ).value
    result_path.write_bytes(
        json.dumps(
            result,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    before = _evidence_tree(attempt)
    output = (tmp_path / "report").resolve()

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, output)

    assert not output.exists()
    assert _evidence_tree(attempt) == before


def test_real_incomplete_and_failed_attempts_cannot_emit_success_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    root = (tmp_path / "runs").resolve()

    def interrupt(stage: str) -> None:
        if stage == "funding_durable":
            raise _AbruptInterruption

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", interrupt)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, root)
    incomplete = next(root.iterdir())
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)
    incomplete_before = _evidence_tree(incomplete)

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(incomplete, (tmp_path / "incomplete-report").resolve())

    assert _evidence_tree(incomplete) == incomplete_before
    rejected = load_backtest_scenario(_scenario(tmp_path / "rejected", funding__initial_cash="50"))
    with pytest.raises(product.BacktestRunFailure) as captured:
        run_backtest_scenario(rejected, root)
    failed = captured.value.output_directory
    failed_before = _evidence_tree(failed)

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(failed, (tmp_path / "failed-report").resolve())

    assert _evidence_tree(failed) == failed_before
    assert not (tmp_path / "incomplete-report").exists()
    assert not (tmp_path / "failed-report").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"ea.backtest-single-run-result.v1","schema":"duplicate"}\n',
        b'{"schema":"ea.backtest-single-run-result.v1","value":NaN}\n',
        b'{"schema":"ea.backtest-single-run-result.v1","value":Infinity}\n',
    ],
)
def test_noncanonical_json_numbers_and_duplicate_keys_fail_closed(
    tmp_path: Path, payload: bytes
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    (attempt / "result.json").write_bytes(payload)

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, (tmp_path / "report").resolve())


def test_existing_partial_or_conflicting_report_is_not_overwritten(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    output = (tmp_path / "report").resolve()
    output.mkdir()
    original = b"do-not-overwrite\n"
    (output / "report.json").write_bytes(original)

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, output)

    assert (output / "report.json").read_bytes() == original
    assert not (output / "summary.txt").exists()


def test_selected_output_failure_is_not_published_and_source_stays_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    before = _evidence_tree(attempt)
    real_write = reporting_module._write_report_file
    calls = 0

    def fail_second(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated summary failure")
        real_write(path, payload)

    monkeypatch.setattr(reporting_module, "_write_report_file", fail_second)
    output = (tmp_path / "report").resolve()

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, output)

    assert not output.exists()
    assert _evidence_tree(attempt) == before


def test_report_output_must_be_separate_from_attempt(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, attempt / "report")

    assert not (attempt / "report").exists()

    with pytest.raises(product.BacktestReportError):
        product.generate_backtest_report(attempt, attempt / "nested" / "report")

    assert not (attempt / "nested").exists()


def test_cross_process_locale_and_timezone_do_not_change_report_bytes(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; from ea.product import generate_backtest_report; "
            f"generate_backtest_report(Path({str(attempt)!r}), Path(__import__('sys').argv[1]))"
        ),
    ]
    first = (tmp_path / "first").resolve()
    second = (tmp_path / "second").resolve()
    first_env = {**os.environ, "LANG": "C", "TZ": "UTC"}
    second_env = {**os.environ, "LANG": "C", "TZ": "Pacific/Honolulu"}

    one = subprocess.run(command + [str(first)], cwd=tmp_path, env=first_env, check=False)
    two = subprocess.run(command + [str(second)], cwd=tmp_path, env=second_env, check=False)

    assert (one.returncode, two.returncode) == (0, 0)
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    assert (first / "summary.txt").read_bytes() == (second / "summary.txt").read_bytes()


def test_backtest_report_cli_writes_both_outputs_without_traceback(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, (tmp_path / "runs").resolve()).output_directory
    output = (tmp_path / "report").resolve()

    result = CliRunner().invoke(
        app,
        ["backtest", "report", "--run-dir", str(attempt), "--output-dir", str(output)],
    )

    assert result.exit_code == 0
    assert "backtest report: success" in result.output
    assert f"report: {output / 'report.json'}" in result.output
    assert f"summary: {output / 'summary.txt'}" in result.output
    assert "source attempt: read-only" in result.output
    assert "Traceback" not in result.output


def test_backtest_report_cli_fails_closed_without_internal_details(tmp_path: Path) -> None:
    attempt = (tmp_path / "00000000-0000-4000-8000-000000000083").resolve()
    attempt.mkdir()
    output = (tmp_path / "report").resolve()

    result = CliRunner().invoke(
        app,
        ["backtest", "report", "--run-dir", str(attempt), "--output-dir", str(output)],
    )

    assert result.exit_code == 3
    assert result.output.strip() == "backtest report failed closed"
    assert "Traceback" not in result.output
    assert not output.exists()
