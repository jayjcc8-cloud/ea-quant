from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

import ea.product.offline_demo as offline_demo
from ea.core import OutcomeCode
from ea.product.offline_demo import (
    DemoMode,
    OfflineDemoFailure,
    OfflineDemoInputError,
    run_offline_demo,
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _assert_stable_artifacts(payloads: list[bytes], *, output_root: Path) -> None:
    forbidden = ("/tmp/", "hostname", "pid", "traceback", "random", "uuid")
    root_value = str(output_root)
    for payload in payloads:
        text = payload.decode("utf-8")
        assert root_value not in text
        lowered = text.lower()
        for token in forbidden:
            assert token not in lowered


def test_bundled_demo_runs_the_existing_economic_path_end_to_end(tmp_path: Path) -> None:
    completed = run_offline_demo((tmp_path / "results").resolve())
    report = _read_json(completed.output_directory / "result.json")

    assert completed.status == "success"
    assert report["schema"] == "ea.offline-demo-result.v1"
    assert report["status"] == "success"
    assert report["run_outcome"] == "filled"
    assert report["trade_outcome"] == "filled"
    assert report["run_status"] == "completed"
    assert report["market_event_count"] == 3
    assert report["signal"]["direction"] == "long"  # type: ignore[index]
    assert report["risk"]["decision"] in {"allow", "resize"}  # type: ignore[index]
    assert report["order_detail"]["side"] == "buy"  # type: ignore[index]
    assert report["fill_detail"]["quantity"] == "2"  # type: ignore[index]
    assert report["cash_snapshot"] == [{"amount": "-203", "currency": "USD"}]
    assert report["position_snapshot"] == [{"quantity": "2", "symbol": "AAPL", "venue": "XNAS"}]
    assert report["reconciliation"] == {
        "cash": "reconciliation_match",
        "position": "reconciliation_match",
    }
    assert report["internal_snapshot_sha256"] == report["replayed_snapshot_sha256"]
    portfolio = report["portfolio"]
    assert isinstance(portfolio, dict)
    assert portfolio["authoritative_snapshot_sha256"] == report["internal_snapshot_sha256"]
    replay_verification = portfolio["replay_verification"]
    assert isinstance(replay_verification, dict)
    assert replay_verification["status"] == "matched"
    assert replay_verification["snapshot_sha256"] == report["replayed_snapshot_sha256"]
    assert len(report["audit_chain_head_sha256"]) == 64  # type: ignore[arg-type]
    assert len(report["semantic_outcome_sha256"]) == 64  # type: ignore[arg-type]

    audit_lines = (completed.output_directory / "audit.jsonl").read_bytes().splitlines()
    assert audit_lines
    audit = [json.loads(line) for line in audit_lines]
    kinds = [record["header"]["record_kind"] for record in audit]
    assert kinds[0] == "run.prepared"
    assert "submission.pre_effect_authorization" in kinds
    assert "portfolio.ledger_handoff_outcome" in kinds
    assert kinds.count("reconciliation.observation_outcome") == 2
    assert kinds[-1] == "reconciliation.observation_outcome"


def test_accepted_demo_has_one_economic_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    original_planning_authority = offline_demo.create_portfolio_planning_authority
    original_order_authority = offline_demo.create_phase1_order_authority
    original_lifecycle = offline_demo.create_phase1_historical_lifecycle

    def capture_planning_authority(*, run_id, spec_set, policy, execution_policy, ledger):
        captured["planning_ledger"] = ledger
        return original_planning_authority(
            run_id=run_id,
            spec_set=spec_set,
            policy=policy,
            execution_policy=execution_policy,
            ledger=ledger,
        )

    def capture_order_authority(
        *,
        run_id,
        spec_set,
        execution_policy,
        risk_policy,
        risk_result_verifier,
    ):
        captured["risk_verifier"] = risk_result_verifier
        return original_order_authority(
            run_id=run_id,
            spec_set=spec_set,
            execution_policy=execution_policy,
            risk_policy=risk_policy,
            risk_result_verifier=risk_result_verifier,
        )

    def capture_lifecycle(*, economic_gate: Any, **kwargs: Any) -> Any:
        captured["economic_gate"] = economic_gate
        return original_lifecycle(economic_gate=economic_gate, **kwargs)

    monkeypatch.setattr(
        offline_demo,
        "create_portfolio_planning_authority",
        capture_planning_authority,
    )
    monkeypatch.setattr(
        offline_demo,
        "create_phase1_order_authority",
        capture_order_authority,
    )
    monkeypatch.setattr(
        offline_demo,
        "create_phase1_historical_lifecycle",
        capture_lifecycle,
    )

    completed = run_offline_demo((tmp_path / "lineage").resolve())
    report = _read_json(completed.output_directory / "result.json")

    gate = captured["economic_gate"]
    assert captured["planning_ledger"] is gate.ledger
    assert captured["risk_verifier"] is gate.risk_authority
    assert report["run_outcome"] == "filled"
    assert report["trade_outcome"] == "filled"
    assert report["planning_snapshot_sha256"] == report["risk_snapshot_sha256"]
    assert report["risk_snapshot_sha256"] == report["pre_fill_ledger_snapshot_sha256"]
    assert (
        report["authoritative_final_snapshot_sha256"]
        == report["reconciliation_snapshot_sha256"]
        == report["report_snapshot_sha256"]
    )


def test_fixed_demo_is_byte_deterministic_across_fresh_output_roots(tmp_path: Path) -> None:
    first = run_offline_demo((tmp_path / "one").resolve())
    second = run_offline_demo((tmp_path / "two").resolve())

    for name in ("result.json", "summary.txt", "audit.jsonl"):
        assert (first.output_directory / name).read_bytes() == (
            second.output_directory / name
        ).read_bytes()
    _assert_stable_artifacts(
        [
            (first.output_directory / "result.json").read_bytes(),
            (first.output_directory / "summary.txt").read_bytes(),
            (first.output_directory / "audit.jsonl").read_bytes(),
        ],
        output_root=(tmp_path / "one").resolve(),
    )
    first_rejected = run_offline_demo(
        (tmp_path / "one-rejected").resolve(),
        mode=DemoMode.RISK_REJECT,
    )
    second_rejected = run_offline_demo(
        (tmp_path / "two-rejected").resolve(),
        mode=DemoMode.RISK_REJECT,
    )
    for name in ("result.json", "summary.txt", "audit.jsonl"):
        assert (first_rejected.output_directory / name).read_bytes() == (
            second_rejected.output_directory / name
        ).read_bytes()
    _assert_stable_artifacts(
        [
            (first_rejected.output_directory / "result.json").read_bytes(),
            (first_rejected.output_directory / "summary.txt").read_bytes(),
            (first_rejected.output_directory / "audit.jsonl").read_bytes(),
        ],
        output_root=(tmp_path / "one-rejected").resolve(),
    )


def test_reconciliation_mismatch_output_is_deterministic_for_byte_equality(tmp_path: Path) -> None:
    with pytest.raises(OfflineDemoFailure):
        run_offline_demo(
            (tmp_path / "mismatch-one").resolve(),
            mode=DemoMode.RECONCILIATION_MISMATCH,
        )
    with pytest.raises(OfflineDemoFailure):
        run_offline_demo(
            (tmp_path / "mismatch-two").resolve(),
            mode=DemoMode.RECONCILIATION_MISMATCH,
        )

    first = (tmp_path / "mismatch-one" / "phase1-demo-v1").resolve()
    second = (tmp_path / "mismatch-two" / "phase1-demo-v1").resolve()
    assert (first / "failure.json").read_bytes() == (second / "failure.json").read_bytes()
    assert (first / "audit.jsonl").read_bytes() == (second / "audit.jsonl").read_bytes()
    _assert_stable_artifacts(
        [
            (first / "failure.json").read_bytes(),
            (first / "audit.jsonl").read_bytes(),
        ],
        output_root=(tmp_path / "mismatch-one").resolve(),
    )


def test_risk_rejection_creates_no_order_fill_cash_or_position_mutation(tmp_path: Path) -> None:
    completed = run_offline_demo(
        (tmp_path / "rejected").resolve(),
        mode=DemoMode.RISK_REJECT,
    )
    report = _read_json(completed.output_directory / "result.json")

    assert report["status"] == "success"
    assert report["run_outcome"] == "risk_rejected"
    assert report["risk"]["decision"] == "reject"  # type: ignore[index]
    assert report["order_detail"] is None
    assert report["fill_detail"] is None
    assert report["cash_snapshot"] == []
    assert report["position_snapshot"] == []
    assert report["run_status"] == "completed"
    assert report["trade_outcome"] == "risk_rejected"
    portfolio = report["portfolio"]
    assert isinstance(portfolio, dict)
    assert (
        portfolio["authoritative_snapshot_ledger_sequence"] == portfolio["pre_fill_ledger_sequence"]
    )
    assert report["pre_fill_ledger_snapshot_sha256"] == report["internal_snapshot_sha256"]
    assert report["internal_snapshot_sha256"] == report["replayed_snapshot_sha256"]
    assert report["reconciliation"] == {
        "cash": "not_required_empty",
        "position": "not_required_empty",
    }


def test_replay_verifier_diverges_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_ledger = offline_demo.create_portfolio_ledger

    class _ReplayLedger:
        def __init__(self, run_id: Any, spec_set: Any) -> None:
            ledger = original_ledger(run_id, spec_set)
            self._delegate = ledger
            self._snapshot = ledger.snapshot

        @property
        def snapshot(self) -> Any:
            return self._snapshot

        def apply_fill(self, fill: Any) -> Any:
            return self._delegate.apply_fill(fill)

    monkeypatch.setattr(offline_demo, "create_portfolio_ledger", _ReplayLedger)
    with pytest.raises(
        RuntimeError, match="audited internal ledger and public Fill replay diverged"
    ):
        run_offline_demo((tmp_path / "divergent-replay").resolve())
    monkeypatch.setattr(offline_demo, "create_portfolio_ledger", original_ledger)


def test_reconciliation_mismatch_fails_closed_without_success_report(tmp_path: Path) -> None:
    output_root = (tmp_path / "mismatch").resolve()

    with pytest.raises(OfflineDemoFailure) as captured:
        run_offline_demo(output_root, mode=DemoMode.RECONCILIATION_MISMATCH)

    assert captured.value.code is OutcomeCode.RECONCILIATION_MISMATCH
    output_directory = output_root / "phase1-demo-v1"
    assert not (output_directory / "result.json").exists()
    assert not (output_directory / "summary.txt").exists()
    failure = _read_json(output_directory / "failure.json")
    assert failure["code"] == "reconciliation.mismatch"
    assert failure["message"] == "offline demo reconciliation did not match"
    assert failure["schema"] == "ea.offline-demo-failure.v1"
    assert failure["status"] == "failed"
    assert failure["run_status"] == "failed"
    assert failure["trade_outcome"] == "reconciliation_failed"
    assert (output_directory / "audit.jsonl").is_file()


def test_reconciliation_mismatch_is_detected_by_reconciliation_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = run_offline_demo((tmp_path / "accepted-baseline").resolve())
    accepted_report = _read_json(accepted.output_directory / "result.json")
    expected_snapshot = accepted_report["internal_snapshot_sha256"]

    original_observation = offline_demo._observation

    def forced_mismatch_observation(
        *, spec_set: Any, snapshot: Any, position: bool, mismatch: bool
    ) -> Any:
        return original_observation(
            spec_set=spec_set,
            snapshot=snapshot,
            position=position,
            mismatch=True,
        )

    monkeypatch.setattr(offline_demo, "_observation", forced_mismatch_observation)
    output_root = (tmp_path / "mismatch-authority").resolve()

    with pytest.raises(OfflineDemoFailure) as captured:
        run_offline_demo(output_root, mode=DemoMode.ACCEPT)
    assert captured.value.code is OutcomeCode.RECONCILIATION_MISMATCH
    output_directory = output_root / "phase1-demo-v1"
    failure = _read_json(output_directory / "failure.json")
    assert failure["code"] == "reconciliation.mismatch"
    audit_lines = (output_directory / "audit.jsonl").read_bytes().splitlines()
    audit = [json.loads(line) for line in audit_lines]
    kinds = [record["header"]["record_kind"] for record in audit]
    assert kinds[-1] == "reconciliation.observation_outcome"
    reconciliation_outcomes = [
        record
        for record in audit
        if record["header"]["record_kind"] == "reconciliation.observation_outcome"
    ]
    assert len(reconciliation_outcomes) == 1
    outcome_payload = reconciliation_outcomes[0]["payload"]
    assert outcome_payload["outcome_code"] == "reconciliation.mismatch"
    completion_records = [
        record
        for record in audit
        if record["header"]["record_kind"] == "runtime.dispatch_completed"
    ]
    terminal_records = [
        record for record in audit if record["header"]["record_kind"] == "run.terminal"
    ]
    assert completion_records
    assert terminal_records
    assert completion_records[-1]["payload"]["final_portfolio_snapshot_sha256"] == expected_snapshot
    assert not (output_directory / "result.json").exists()
    assert not (output_directory / "summary.txt").exists()


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    output_root = (tmp_path / "results").resolve()
    completed = run_offline_demo(output_root)
    before = {
        path.name: path.read_bytes()
        for path in completed.output_directory.iterdir()
        if path.is_file()
    }

    with pytest.raises(OfflineDemoInputError, match="already exists"):
        run_offline_demo(output_root)

    assert {
        path.name: path.read_bytes()
        for path in completed.output_directory.iterdir()
        if path.is_file()
    } == before


def test_demo_market_data_is_a_real_installed_package_resource() -> None:
    sample = resources.files("ea.product").joinpath("phase1_demo_ohlcv_v1.csv")

    assert sample.is_file()
    payload = sample.read_bytes()
    assert payload.startswith(b"schema_version,venue,symbol")
    assert payload.count(b"\n") == 4
