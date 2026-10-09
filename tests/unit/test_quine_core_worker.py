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


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "scenarios"
    root.mkdir()
    (root / "bounded-long.yaml").write_text(SCENARIO, encoding="utf-8")
    (root / "prices.csv").write_text(PRICES, encoding="utf-8")
    return root


def _snapshot(root: Path, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"scenario_path": str(root / "bounded-long.yaml")}
    if parameters is not None:
        request["parameters"] = parameters
    reply = rust_domain.run("snapshot", request)
    assert reply["status"] == "ok", f"{reply['code']}: {reply['message']}"
    return cast(dict[str, Any], reply["result"]["frozen"])


def _run_worker(
    control: Path, frozen: dict[str, Any], attempt_id: str, job_id: str
) -> dict[str, Any]:
    control.mkdir(parents=True, exist_ok=True)
    scenario_path = control / "frozen.json"
    scenario_path.write_bytes(bytes.fromhex(frozen["scenario_file"]))
    request = worker.build_request(
        job_id=job_id,
        attempt_id=attempt_id,
        scenario_path=scenario_path,
        scenario_sha256=frozen["scenario_file_sha256"],
        scenario_identity_sha256=frozen["scenario_identity_sha256"],
        research_input_bytes=None,
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
