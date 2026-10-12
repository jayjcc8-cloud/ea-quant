"""The stateless Rust-core domain adapter, proven against a real engine run.

These tests cover only the adapter's own bounded contract: it freezes the
admitted input identity, it refuses an altered freeze binding, it independently
regenerates and compares the formal artifacts of one real worker attempt, and it
refuses a staged artifact that is not the one the evidence produces.
"""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from ea.core.encoding import canonical_json_bytes
from ea.web import rust_domain, worker

SCENARIO = """schema_version: 1
data:
  path: prices.csv
  start_utc: '2026-01-02T09:31:00.000000Z'
  end_utc: '2026-01-02T09:34:00.000000Z'
  fingerprint:
    record_count: 4
    sha256: c693dd2db0a06f2736b8b4a54967cb91398dee11e72aae11c7bfba98a1e9f6a4
instrument:
  venue: XNAS
  symbol: AAPL
  specification_id: xnas.aapl.v1
  specification_set_id: scenario.xnas.aapl.v1
  settlement_currency: USD
  price_quantum: '0.01'
  quantity_quantum: '1'
  currency_quantum: '0.01'
  contract_multiplier: '1'
strategy:
  id: bounded-long-v1
  target_quantity: '2'
funding:
  currency: USD
  initial_cash: '10000'
risk:
  max_order_quantity: '5'
  max_position_quantity: '5'
  max_notional: '1000'
execution:
  policy: phase1.next-bar-close.v1
randomness_profile: none
"""

_PRICE_COLUMNS = (
    "schema_version",
    "venue",
    "symbol",
    "interval_start",
    "interval_end",
    "adjustment",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "source_sequence",
    "revision",
    "available_at",
)

_PRICE_ROWS = (
    (
        "1",
        "XNAS",
        "AAPL",
        "2026-01-02T09:31:00.000000Z",
        "2026-01-02T09:32:00.000000Z",
        "raw",
        "100.5",
        "102.0",
        "100.0",
        "101.5",
        "12.0",
        "fixture.raw",
        "2",
        "0",
        "2026-01-02T09:32:00.000000Z",
    ),
    (
        "1",
        "XNAS",
        "AAPL",
        "2026-01-02T09:30:00.000000Z",
        "2026-01-02T09:31:00.000000Z",
        "raw",
        "100.0",
        "101.0",
        "99.0",
        "100.5",
        "10.0",
        "fixture.raw",
        "0",
        "0",
        "2026-01-02T09:31:00.000000Z",
    ),
    (
        "1",
        "XNAS",
        "AAPL",
        "2026-01-02T09:30:00.000000Z",
        "2026-01-02T09:31:00.000000Z",
        "raw",
        "100.0",
        "101.5",
        "99.0",
        "101.0",
        "11.0",
        "fixture.raw",
        "1",
        "1",
        "2026-01-02T09:31:30.000000Z",
    ),
    (
        "1",
        "XNAS",
        "AAPL",
        "2026-01-02T09:32:00.000000Z",
        "2026-01-02T09:33:00.000000Z",
        "raw",
        "108.0",
        "111.0",
        "107.0",
        "110.0",
        "9.0",
        "fixture.raw",
        "3",
        "0",
        "2026-01-02T09:33:00.000000Z",
    ),
)

PRICES = "\n".join([",".join(_PRICE_COLUMNS), *(",".join(row) for row in _PRICE_ROWS)]) + "\n"

#: One registered local research input with its own economics: a different
#: entry and a much larger exit move than the scenario's own bundled data.
_RESEARCH_ROWS = tuple(
    row[:11] + (close,) + row[12:]
    for row, close in zip(_PRICE_ROWS, ("121.5", "100.5", "101.0", "130.0"), strict=True)
)
RESEARCH_PRICES = (
    "\n".join([",".join(_PRICE_COLUMNS), *(",".join(row) for row in _RESEARCH_ROWS)]) + "\n"
)

RESEARCH_PARAMETERS = {
    "initial_cash": "25000.5",
    "strategy_parameters": {"target_quantity": "3", "entry_delay_bars": 1},
}


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "scenarios"
    root.mkdir()
    (root / "bounded-long.yaml").write_text(SCENARIO, encoding="utf-8")
    (root / "prices.csv").write_text(PRICES, encoding="utf-8")
    return root


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    (root / "full.csv").write_text(RESEARCH_PRICES, encoding="utf-8")
    return root


def _snapshot(root: Path, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"scenario_path": str(root / "bounded-long.yaml")}
    if parameters is not None:
        request["parameters"] = parameters
    reply = rust_domain.run("snapshot", request)
    assert reply["status"] == "ok", f"{reply['code']}: {reply['message']}"
    return cast(dict[str, Any], reply["result"]["frozen"])


def _research_snapshot(tmp_path: Path) -> dict[str, Any]:
    """One frozen input admitted over an explicitly registered research input."""
    scenarios = _workspace(tmp_path)
    reply = rust_domain.run(
        "snapshot",
        {
            "scenario_path": str(scenarios / "bounded-long.yaml"),
            "data_root": str(_data_root(tmp_path)),
            "dataset_id": "full.csv",
            "parameters": RESEARCH_PARAMETERS,
        },
    )
    assert reply["status"] == "ok", f"{reply['code']}: {reply['message']}"
    return cast(dict[str, Any], reply["result"]["frozen"])


def _run_worker(
    control: Path, frozen: dict[str, Any], attempt_id: str, job_id: str
) -> dict[str, Any]:
    control.mkdir(parents=True, exist_ok=True)
    scenario_path = control / "frozen.json"
    scenario_path.write_bytes(bytes.fromhex(frozen["scenario_file"]))
    # Exactly what the Rust core carries from the frozen envelope: the admitted
    # research evidence value and the digest of its canonical bytes.
    evidence = frozen.get("research_input")
    request = worker.build_request(
        job_id=job_id,
        attempt_id=attempt_id,
        scenario_path=scenario_path,
        scenario_sha256=frozen["scenario_file_sha256"],
        scenario_identity_sha256=frozen["scenario_identity_sha256"],
        research_input_bytes=(None if evidence is None else canonical_json_bytes(evidence)),
        package_path=None,
        package_sha256=None,
    )
    worker.write_request(control, request)
    process = worker.spawn(control, executable=sys.executable)
    try:
        assert process.wait(120), "the worker never finished"
        assert process.stopped(), "the worker group is not confirmed gone"
    finally:
        process.close()
    result = worker.read_result(control)
    assert result is not None, "the worker wrote no result document"
    return result


def test_snapshot_freezes_the_admitted_identity(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    assert frozen["schema"] == rust_domain.FROZEN_SCHEMA
    assert frozen["snapshot_sha256"] == frozen["snapshot_sha256"].lower()
    assert (
        frozen["scenario_file_sha256"] == sha256(bytes.fromhex(frozen["scenario_file"])).hexdigest()
    )
    verified = rust_domain.run("freeze", {"frozen": frozen})
    assert verified["status"] == "ok", verified
    assert verified["result"]["scenario_identity_sha256"] == frozen["scenario_identity_sha256"]


def test_parameters_are_normalized_into_the_frozen_input(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(
        root,
        {
            # ea-decimal-v1: an exact decimal string with no trailing zero.
            "initial_cash": "25000.5",
            "strategy_parameters": {"target_quantity": "3", "entry_delay_bars": 1},
        },
    )
    document = json.loads(bytes.fromhex(frozen["scenario_file"]))
    assert document["funding"]["initial_cash"] == "25000.5"
    assert document["strategy"]["target_quantity"] == "3"
    assert document["strategy"]["entry_delay_bars"] == 1
    assert rust_domain.run("freeze", {"frozen": frozen})["status"] == "ok"


def test_freeze_refuses_an_altered_snapshot(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    frozen["snapshot"]["scenario"]["funding"]["initial_cash"] = "999999"
    refused = rust_domain.run("freeze", {"frozen": frozen})
    assert refused["status"] == "refused"
    assert refused["code"] == "digest_mismatch"


def test_freeze_refuses_altered_scenario_bytes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    payload = bytearray(bytes.fromhex(frozen["scenario_file"]))
    payload[-2] ^= 0x01
    frozen["scenario_file"] = bytes(payload).hex()
    refused = rust_domain.run("freeze", {"frozen": frozen})
    assert refused["status"] == "refused"
    assert refused["code"] == "digest_mismatch"


def _validate(
    frozen: dict[str, Any],
    control: Path,
    result: dict[str, Any],
    *,
    attempt_id: str,
    job_id: str,
    attempt_dir: Path | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    return rust_domain.run(
        "validate",
        {
            "frozen": frozen,
            "attempt_id": attempt_id,
            "job_id": job_id,
            "attempt_dir": str(attempt_dir or control / result["attempt_relative"]),
            "report_dir": str(report_dir or control / result["report_relative"]),
        },
    )


def _direct_engine_report(work: Path, frozen: dict[str, Any]) -> dict[str, Any]:
    """One attempt of the same frozen input, run by the existing engine directly.

    Nothing here belongs to the Rust core: the scenario bytes are the frozen
    ones, the research evidence is the confirmed one, and the attempt gets its
    own fresh identity.
    """
    from dataclasses import replace

    from ea.core import RunId
    from ea.product import generate_backtest_report, run_backtest_scenario
    from ea.product.scenario import load_backtest_scenario_bytes

    work.mkdir(parents=True, exist_ok=True)
    scenario_path = work / "frozen.json"
    scenario_path.write_bytes(bytes.fromhex(frozen["scenario_file"]))
    scenario = load_backtest_scenario_bytes(scenario_path.read_bytes(), scenario_path=scenario_path)
    evidence = frozen.get("research_input")
    if evidence is not None:
        scenario = replace(scenario, research_input_bytes=canonical_json_bytes(evidence))
    attempt = run_backtest_scenario(scenario, work / "runs", run_id=RunId(str(uuid4())))
    report = generate_backtest_report(attempt.output_directory, work / "report").report
    return cast(dict[str, Any], json.loads(report.canonical_bytes))


def test_real_worker_attempt_is_validated_byte_for_byte(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    result = _run_worker(control, frozen, attempt_id, job_id)
    assert result["status"] == "succeeded", result

    validated = _validate(frozen, control, result, attempt_id=attempt_id, job_id=job_id)
    assert validated["status"] == "ok", validated
    produced = validated["result"]
    assert produced["run_id"] == attempt_id
    # The digests are of the exact staged bytes the adapter regenerated.
    assert produced["artifacts"] == result["artifacts"]
    facts = {item["path"]: item["value"] for item in produced["facts"]}
    # A count stays an integer; a decimal stays the report's own canonical string.
    assert type(facts["economics.counts.fills"]) is int
    assert type(facts["economics.counts.orders"]) is int
    assert isinstance(facts["economics.net_pnl.amount"], str)
    assert isinstance(facts["economics.total_return.value"], str)


def test_validate_refuses_a_tampered_artifact(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    result = _run_worker(control, frozen, attempt_id, job_id)
    report_dir = control / result["report_relative"]
    summary = report_dir / "summary.txt"
    summary.write_bytes(summary.read_bytes() + b"tampered\n")

    refused = _validate(
        frozen, control, result, attempt_id=attempt_id, job_id=job_id, report_dir=report_dir
    )
    assert refused["status"] == "refused"
    assert refused["code"] == "artifact_mismatch"


def test_validate_refuses_a_mismatched_attempt_identity(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    result = _run_worker(control, frozen, attempt_id, job_id)
    refused = _validate(frozen, control, result, attempt_id=str(uuid4()), job_id=job_id)
    assert refused["status"] == "refused"
    assert refused["code"] == "identity_mismatch"


def test_validate_refuses_evidence_from_another_frozen_input(tmp_path: Path) -> None:
    """A valid run of a different frozen input cannot be registered as this one.

    Both frozen inputs are internally consistent — each reproduces its own
    snapshot digest, its own scenario-file digest and its own admitted identity.
    Only the binding between the staged engine evidence and *this* input's
    scenario identity separates them.
    """
    root = _workspace(tmp_path)
    frozen = _snapshot(root)
    other = _snapshot(
        root,
        {
            "initial_cash": "25000.5",
            "strategy_parameters": {"target_quantity": "3", "entry_delay_bars": 0},
        },
    )
    assert other["scenario_identity_sha256"] != frozen["scenario_identity_sha256"]
    assert rust_domain.run("freeze", {"frozen": other})["status"] == "ok"
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    result = _run_worker(control, frozen, attempt_id, job_id)
    refused = _validate(other, control, result, attempt_id=attempt_id, job_id=job_id)
    assert refused["status"] == "refused"
    assert refused["code"] == "identity_mismatch"


# --- registered research input ---------------------------------------------


def test_registered_research_input_is_frozen_with_its_own_evidence(tmp_path: Path) -> None:
    frozen = _research_snapshot(tmp_path)
    snapshot = frozen["snapshot"]
    assert snapshot["schema"] == "ea.local-web-input.v2"
    evidence = snapshot["research_input"]
    assert evidence["dataset_id"] == "full.csv"
    assert evidence["replay_start_utc"] and evidence["source_sha256"]
    assert frozen["research_input"] == evidence

    canonical = canonical_json_bytes(evidence)
    assert frozen["research_input_canonical"] == canonical.hex()
    assert frozen["research_input_sha256"] == sha256(canonical).hexdigest()
    assert snapshot["identity"]["dataset_id"] == "full.csv"
    assert snapshot["identity"]["source_sha256"] == evidence["source_sha256"]

    # The frozen scenario file is the rebound document: its data block names the
    # registered input, not the scenario's own bundled file.
    document = json.loads(bytes.fromhex(frozen["scenario_file"]))
    assert document["data"]["path"].endswith("full.csv")
    assert document["data"]["fingerprint"]["sha256"] == evidence["data_sha256"]
    assert document["funding"]["initial_cash"] == "25000.5"
    assert document["strategy"]["target_quantity"] == "3"
    assert document["strategy"]["entry_delay_bars"] == 1
    assert rust_domain.run("freeze", {"frozen": frozen})["status"] == "ok"


def test_real_worker_consumes_the_registered_research_evidence(tmp_path: Path) -> None:
    """The worker re-reads and re-digests exactly the admitted evidence."""
    frozen = _research_snapshot(tmp_path)
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    result = _run_worker(control, frozen, attempt_id, job_id)
    assert result["status"] == "succeeded", result

    validated = _validate(frozen, control, result, attempt_id=attempt_id, job_id=job_id)
    assert validated["status"] == "ok", validated
    assert validated["result"]["artifacts"] == result["artifacts"]

    # The worker verified the evidence it was given: a request carrying other
    # evidence for the same frozen scenario is refused before the engine.
    request = json.loads((control / "request.json").read_bytes())
    assert request["scenario"]["research_input_sha256"] == frozen["research_input_sha256"]
    other = dict(request)
    other["scenario"] = {**request["scenario"], "research_input_sha256": "0" * 64}
    second = tmp_path / "second"
    second.mkdir()
    (second / "frozen.json").write_bytes((control / "frozen.json").read_bytes())
    worker.write_request(second, other)
    process = worker.spawn(second, executable=sys.executable)
    try:
        assert process.wait(120)
    finally:
        process.close()
    drifted = worker.read_result(second)
    assert drifted is not None and drifted["status"] == "failed"
    assert drifted["code"] == "input_changed"


def test_freeze_refuses_research_evidence_that_is_not_the_snapshots(tmp_path: Path) -> None:
    frozen = _research_snapshot(tmp_path)
    replaced = {**frozen["research_input"], "dataset_id": "other.csv"}
    canonical = canonical_json_bytes(replaced)
    frozen["research_input"] = replaced
    frozen["research_input_canonical"] = canonical.hex()
    frozen["research_input_sha256"] = sha256(canonical).hexdigest()
    refused = rust_domain.run("freeze", {"frozen": frozen})
    assert refused["status"] == "refused"
    assert refused["code"] == "identity_mismatch"


def test_freeze_refuses_research_evidence_with_a_broken_digest(tmp_path: Path) -> None:
    frozen = _research_snapshot(tmp_path)
    frozen["research_input_canonical"] = canonical_json_bytes(
        {**frozen["research_input"], "dataset_id": "other.csv"}
    ).hex()
    refused = rust_domain.run("freeze", {"frozen": frozen})
    assert refused["status"] == "refused"
    assert refused["code"] == "digest_mismatch"


def _drift_the_source_bytes(tmp_path: Path) -> Path:
    """Vary the registered source's bytes without changing what it selects.

    A line-ending rewrite parses to exactly the same rows, so the selected
    fingerprint, the record count and the scenario identity are unchanged while
    the source bytes are no longer the confirmed ones.
    """
    source = tmp_path / "data" / "full.csv"
    raw = source.read_bytes()
    source.write_bytes(raw.replace(b"\n", b"\r\n"))
    assert source.read_bytes() != raw
    return source


def test_source_drift_that_keeps_the_selected_fingerprint_is_refused(tmp_path: Path) -> None:
    """Only the source bytes changed: the admitted evidence no longer holds."""
    frozen = _research_snapshot(tmp_path)
    _drift_the_source_bytes(tmp_path)
    refused = rust_domain.run("freeze", {"frozen": frozen})
    assert refused["status"] == "refused", refused
    assert refused["code"] == "identity_mismatch"


def test_real_worker_refuses_a_source_that_is_not_the_confirmed_one(tmp_path: Path) -> None:
    """The worker's own consumption seam holds the evidence to its real source."""
    frozen = _research_snapshot(tmp_path)
    _drift_the_source_bytes(tmp_path)
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    result = _run_worker(control, frozen, attempt_id, job_id)
    assert result["status"] == "failed", result
    assert result["code"] == "input_changed", result


def test_direct_engine_result_matches_the_core_worker_result(tmp_path: Path) -> None:
    """The same frozen input, run directly through the existing engine.

    The worker's attempt and a fresh independent direct attempt must agree on
    every economic and source reading while keeping their own attempt identity.
    """
    frozen = _research_snapshot(tmp_path)
    attempt_id, job_id = str(uuid4()), str(uuid4())
    control = tmp_path / "control"
    worker_result = _run_worker(control, frozen, attempt_id, job_id)
    assert worker_result["status"] == "succeeded", worker_result
    staged = json.loads((control / worker_result["report_relative"] / "report.json").read_bytes())

    direct = _direct_engine_report(tmp_path / "direct", frozen)
    # A fresh attempt and run identity, with the same input.
    assert direct["run_id"] != staged["run_id"]
    assert direct["run_id"] != attempt_id
    # Identical economics and source evidence, read from the engine's own work.
    assert direct["economics"] == staged["economics"]
    assert direct["source"]["scenario_sha256"] == staged["source"]["scenario_sha256"]
    assert direct["source"]["data"] == staged["source"]["data"]
    assert direct["source"]["strategy"] == staged["source"]["strategy"]
    assert direct["source"]["instrument"] == staged["source"]["instrument"]
    assert direct["source"]["risk"] == staged["source"]["risk"]
    assert direct["source"]["execution"] == staged["source"]["execution"]
    assert direct["schema"] == staged["schema"]
