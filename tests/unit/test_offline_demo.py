from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import RFC_4122, UUID

import pytest

import ea.product.offline_demo as offline_demo
from ea.core import ExecutionPolicyId, ExecutionPolicyRef, OutcomeCode, RunId, Sha256Digest
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
    forbidden = ("/tmp/", "hostname", "pid", "traceback", "uuid")
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
    reconciliation_indices = [
        index for index, kind in enumerate(kinds) if kind == "reconciliation.observation_outcome"
    ]
    terminal_records = [
        (index, record)
        for index, record in enumerate(audit)
        if record["header"]["record_kind"] == "run.terminal"
    ]
    assert len(reconciliation_indices) == 2
    assert len(terminal_records) == 1
    terminal_index, terminal_record = terminal_records[0]
    assert terminal_record["payload"]["terminal_kind"] == "success"
    assert max(reconciliation_indices) < terminal_index


def test_accepted_demo_has_one_economic_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    original_planning_authority = offline_demo.create_portfolio_planning_authority
    original_order_authority = offline_demo.create_phase1_order_authority
    original_lifecycle = offline_demo.create_phase1_historical_lifecycle

    def capture_planning_authority(
        *,
        run_id: Any,
        spec_set: Any,
        policy: Any,
        execution_policy: Any,
        ledger: Any,
    ) -> Any:
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
        run_id: Any,
        spec_set: Any,
        execution_policy: Any,
        risk_policy: Any,
        risk_result_verifier: Any,
    ) -> Any:
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


def test_fresh_attempts_have_distinct_uuid4_run_ids(tmp_path: Path) -> None:
    first = run_offline_demo((tmp_path / "one").resolve())
    second = run_offline_demo((tmp_path / "two").resolve())
    first_report = _read_json(first.output_directory / "result.json")
    second_report = _read_json(second.output_directory / "result.json")

    first_run_id = UUID(str(first_report["run_id"]))
    second_run_id = UUID(str(second_report["run_id"]))
    assert first_run_id != second_run_id
    assert first_run_id.version == second_run_id.version == 4
    assert first_run_id.variant == second_run_id.variant == RFC_4122


def test_equivalent_fresh_attempts_share_lineage_and_semantic_outcome(tmp_path: Path) -> None:
    first = run_offline_demo((tmp_path / "one").resolve())
    second = run_offline_demo((tmp_path / "two").resolve())
    first_report = _read_json(first.output_directory / "result.json")
    second_report = _read_json(second.output_directory / "result.json")

    assert first_report["run_id"] != second_report["run_id"]
    assert first_report["lineage_sha256"] == second_report["lineage_sha256"]
    assert first_report["semantic_outcome_sha256"] == second_report["semantic_outcome_sha256"]


def test_equivalent_fresh_attempt_evidence_remains_distinguishable(tmp_path: Path) -> None:
    first = run_offline_demo((tmp_path / "one").resolve())
    second = run_offline_demo((tmp_path / "two").resolve())

    assert (first.output_directory / "result.json").read_bytes() != (
        second.output_directory / "result.json"
    ).read_bytes()
    assert (first.output_directory / "audit.jsonl").read_bytes() != (
        second.output_directory / "audit.jsonl"
    ).read_bytes()
    _assert_stable_artifacts(
        [
            (first.output_directory / "result.json").read_bytes(),
            (first.output_directory / "summary.txt").read_bytes(),
            (first.output_directory / "audit.jsonl").read_bytes(),
        ],
        output_root=(tmp_path / "one").resolve(),
    )


def test_fresh_runner_does_not_accept_caller_supplied_attempt_identity(tmp_path: Path) -> None:
    run_id = RunId("123e4567-e89b-42d3-a456-426614174000")
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        run_offline_demo(  # type: ignore[call-arg]
            (tmp_path / "one").resolve(), attempt_run_id=run_id
        )


def test_lineage_records_explicit_non_random_profile(tmp_path: Path) -> None:
    completed = run_offline_demo((tmp_path / "results").resolve())
    report = _read_json(completed.output_directory / "result.json")

    assert report["randomness"] == {
        "master_seed": "not_applicable",
        "profile": "none",
    }


def test_true_execution_policy_change_changes_lineage_and_semantic_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = run_offline_demo((tmp_path / "baseline").resolve())
    baseline_report = _read_json(baseline.output_directory / "result.json")
    monkeypatch.setattr(
        offline_demo,
        "_EXECUTION_POLICY",
        ExecutionPolicyRef(
            ExecutionPolicyId("phase1.next-bar-close.changed.v1"),
            Sha256Digest("2" * 64),
        ),
    )
    changed = run_offline_demo((tmp_path / "changed").resolve())
    changed_report = _read_json(changed.output_directory / "result.json")

    assert baseline_report["lineage_sha256"] != changed_report["lineage_sha256"]
    assert baseline_report["semantic_outcome_sha256"] != changed_report["semantic_outcome_sha256"]


def test_equivalent_rejected_attempts_share_semantics_but_not_evidence(tmp_path: Path) -> None:
    first_rejected = run_offline_demo(
        (tmp_path / "one-rejected").resolve(), mode=DemoMode.RISK_REJECT
    )
    second_rejected = run_offline_demo(
        (tmp_path / "two-rejected").resolve(), mode=DemoMode.RISK_REJECT
    )
    first_report = _read_json(first_rejected.output_directory / "result.json")
    second_report = _read_json(second_rejected.output_directory / "result.json")

    assert first_report["run_id"] != second_report["run_id"]
    assert first_report["lineage_sha256"] == second_report["lineage_sha256"]
    assert first_report["semantic_outcome_sha256"] == second_report["semantic_outcome_sha256"]
    assert (first_rejected.output_directory / "audit.jsonl").read_bytes() != (
        second_rejected.output_directory / "audit.jsonl"
    ).read_bytes()
    _assert_stable_artifacts(
        [
            (first_rejected.output_directory / "result.json").read_bytes(),
            (first_rejected.output_directory / "summary.txt").read_bytes(),
            (first_rejected.output_directory / "audit.jsonl").read_bytes(),
        ],
        output_root=(tmp_path / "one-rejected").resolve(),
    )


def test_reconciliation_mismatch_output_retains_attempt_identity(tmp_path: Path) -> None:
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
    first_failure = _read_json(first / "failure.json")
    second_failure = _read_json(second / "failure.json")
    assert first_failure["run_id"] != second_failure["run_id"]
    assert first_failure["lineage_sha256"] == second_failure["lineage_sha256"]
    assert first_failure["semantic_outcome_sha256"] == second_failure["semantic_outcome_sha256"]
    assert (first / "audit.jsonl").read_bytes() != (second / "audit.jsonl").read_bytes()
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
    audit_records = [
        json.loads(line) for line in (output_directory / "audit.jsonl").read_bytes().splitlines()
    ]
    assert "run.terminal" not in {record["header"]["record_kind"] for record in audit_records}
    assert (output_directory / "audit.jsonl").is_file()


def test_reconciliation_mismatch_is_detected_by_reconciliation_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = run_offline_demo((tmp_path / "accepted-baseline").resolve())
    _read_json(accepted.output_directory / "result.json")

    original_observation = offline_demo._observation

    def forced_mismatch_observation(
        *, run_id: Any, spec_set: Any, snapshot: Any, position: bool, mismatch: bool
    ) -> Any:
        return original_observation(
            run_id=run_id,
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
    assert not terminal_records
    assert len(completion_records[-1]["payload"]["final_portfolio_snapshot_sha256"]) == 64
    assert completion_records[-1]["payload"]["dispatch_kind"] == "market"
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
