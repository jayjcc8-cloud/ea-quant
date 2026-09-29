"""WU-2 deterministic evidence contract: snapshot, gate identity and verdict."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ea.core import RunId, Sha256Digest
from ea.core.outcomes import OutcomeCode
from ea.product.paper_alerts import (
    AlertSeverity,
    AlertSignalReading,
    AlertType,
    SignalCondition,
    project_alert_stream,
)
from ea.product.paper_evidence import (
    DEFAULT_BACKUP_FRESHNESS_SECONDS,
    EVIDENCE_SCHEMA_VERSION,
    GATE_IDENTITY_SCHEMA,
    GATE_VERDICT_SCHEMA,
    RUNTIME_PROFILE,
    SNAPSHOT_SCHEMA,
    AlertSummary,
    BackupSummary,
    BrokerMode,
    GateType,
    GateVerdict,
    PaperEvidenceError,
    PaperGateIdentity,
    PaperGateVerdict,
    PaperSnapshot,
    SupervisorState,
    build_gate_identity,
    build_paper_snapshot,
    canonical_gate_identity_bytes,
    canonical_gate_verdict_bytes,
    canonical_paper_snapshot_bytes,
    config_identity_digest,
    decode_gate_identity,
    decode_gate_verdict,
    decode_paper_snapshot,
    evaluate_gate,
    launchd_identity_digest,
    summarize_alert_state,
    summarize_backup_state,
)
from ea.product.paper_health import (
    BrokerState,
    KillSwitchProjectionState,
    MarketState,
    PaperHealthSnapshot,
    ReconciliationState,
    RecoveryState,
    RuntimeState,
    StorageState,
    StrategyHeartbeatState,
    TradePermissionBasis,
)

RUN_ID = RunId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REPO_SHA = "a" * 40
OTHER_SHA = "b" * 40
UNDERWAY = datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
CONFIG_BYTES = (
    b"EA_BIN=/Users/mo/Documents/EA/venv/bin/ea\n"
    b"WORKSPACE=/Users/mo/EA/workspace\n"
    b"CANDIDATE_ID=00000000-0000-4000-8000-000000000000\n"
)
OTHER_CONFIG_BYTES = CONFIG_BYTES + b"PRICES=99\n"
LAUNCHD_BYTES = b"<plist><key>Label</key><string>com.ea.paper</string></plist>\n"
OTHER_LAUNCHD_BYTES = LAUNCHD_BYTES + b"<extra/>\n"
HOST_ID = "test-host"


def _healthy_health(**overrides: Any) -> PaperHealthSnapshot:
    values: dict[str, Any] = {
        "process_alive": True,
        "runtime_ready": True,
        "trade_permitted": True,
        "runtime_state": RuntimeState.RUNNING,
        "recovery_state": RecoveryState.NOT_REQUIRED,
        "reconciliation_state": ReconciliationState.CLEAN,
        "market_state": MarketState.FRESH,
        "broker_state": BrokerState.AVAILABLE,
        "strategy_heartbeat_state": StrategyHeartbeatState.LIVE,
        "storage_state": StorageState.AVAILABLE,
        "kill_switch_state": KillSwitchProjectionState.ACTIVE,
        "operator_halt": False,
        "reason_codes": (),
        "trade_blocking_guard": None,
        "trade_reason": None,
        "trade_permission_basis": TradePermissionBasis.ZERO_EFFECT_PROBE,
        "trade_permission_unexercised_guards": ("price_deviation",),
        "observed_at": UNDERWAY,
        "run_id": RUN_ID,
        "candidate_id": "candidate-1",
    }
    values.update(overrides)
    return PaperHealthSnapshot(**values)


def _healthy_snapshot(**overrides: Any) -> PaperSnapshot:
    values: dict[str, Any] = {
        "captured_at": UNDERWAY,
        "repo_sha": REPO_SHA,
        "run_id": RUN_ID,
        "supervisor_state": SupervisorState.RUNNING,
        "writer_lease_held": True,
        "broker_mode": BrokerMode.LOCAL_SIMULATED_PAPER,
        "live_enabled": False,
        "alert_state": AlertSummary.CLEAN,
        "backup_state": BackupSummary.FRESH,
    }
    if "health" not in values and "health" not in overrides:
        values["health"] = _healthy_health()
    values.update(overrides)
    return build_paper_snapshot(**values)


def _locked_identity(**overrides: Any) -> PaperGateIdentity:
    values: dict[str, Any] = {
        "gate_id": "M4-72H-001",
        "gate_type": GateType.T72H,
        "repo_sha": REPO_SHA,
        "config_bytes": CONFIG_BYTES,
        "launchd_bytes": LAUNCHD_BYTES,
        "started_at": UNDERWAY,
    }
    values.update(overrides)
    return build_gate_identity(**values)


def _alert_stream(*, runtime_down_active: bool) -> Any:
    """A real projected stream; RUNTIME_DOWN is a critical run-scoped signal."""
    readings = tuple(
        AlertSignalReading(
            alert_type,
            (
                SignalCondition.ACTIVE
                if runtime_down_active and alert_type is AlertType.RUNTIME_DOWN
                else SignalCondition.INACTIVE
            ),
            f"observed {alert_type.value}",
        )
        for alert_type in AlertType
    )
    return project_alert_stream(
        host_id=HOST_ID,
        readings=readings,
        observed_at=UNDERWAY,
        run_id=RUN_ID,
        account_id="paper.local",
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_collection_round_trips() -> None:
    """A collected snapshot is a valid, decodable, canonical document."""
    snapshot = _healthy_snapshot()
    assert snapshot.schema == SNAPSHOT_SCHEMA
    assert snapshot.runtime_state is RuntimeState.RUNNING
    assert snapshot.reconciliation_state is ReconciliationState.CLEAN
    assert snapshot.risk_gate_state.value == "healthy"
    assert snapshot.readiness is True

    document = snapshot.document()
    assert document["writer_lease_held"] is True
    assert document["live_enabled"] is False
    assert document["broker_mode"] == "local_simulated_paper"

    decoded = decode_paper_snapshot(document)
    assert decoded == snapshot
    assert canonical_paper_snapshot_bytes(snapshot) == canonical_paper_snapshot_bytes(decoded)


def test_snapshot_collection_is_side_effect_free() -> None:
    """Collection is a pure function of its inputs: byte-stable, no mutation."""
    health = _healthy_health()
    first = _healthy_snapshot(health=health)
    second = _healthy_snapshot(health=health)
    assert first == second
    assert canonical_paper_snapshot_bytes(first) == canonical_paper_snapshot_bytes(second)
    # The embedded projection is preserved, never replaced or re-derived.
    assert first.health == health


def test_snapshot_unavailable_health_is_never_read_as_healthy() -> None:
    """A missing health projection demotes to UNKNOWN, not healthy."""
    snapshot = _healthy_snapshot(health=None)
    assert snapshot.runtime_state is RuntimeState.UNKNOWN
    assert snapshot.reconciliation_state is ReconciliationState.UNKNOWN
    assert snapshot.risk_gate_state.value == "unknown"
    assert snapshot.readiness is False
    assert snapshot.health is None


def test_snapshot_decodes_malformed_input_fails_closed() -> None:
    document = _healthy_snapshot().document()
    document["repo_sha"] = "not-a-sha"
    with pytest.raises(PaperEvidenceError) as error:
        decode_paper_snapshot(document)
    assert error.value.code is OutcomeCode.CONFLICTING_ID


# ---------------------------------------------------------------------------
# alert and backup summaries
# ---------------------------------------------------------------------------


def test_alert_summary_distinguishes_clean_critical_and_unavailable() -> None:
    assert summarize_alert_state(None) is AlertSummary.UNAVAILABLE
    assert summarize_alert_state(_alert_stream(runtime_down_active=False)) is AlertSummary.CLEAN
    assert (
        summarize_alert_state(_alert_stream(runtime_down_active=True))
        is AlertSummary.CRITICAL_ACTIVE
    )


def test_backup_summary_ages_against_the_freshness_window() -> None:
    assert summarize_backup_state(None, captured_at=UNDERWAY, freshness_seconds=3600.0) is (
        BackupSummary.ABSENT
    )
    recent = UNDERWAY - timedelta(seconds=1000)
    assert (
        summarize_backup_state(recent, captured_at=UNDERWAY, freshness_seconds=3600.0)
        is BackupSummary.FRESH
    )
    assert (
        summarize_backup_state(recent, captured_at=UNDERWAY, freshness_seconds=60.0)
        is BackupSummary.STALE
    )


# ---------------------------------------------------------------------------
# gate identity
# ---------------------------------------------------------------------------


def test_gate_identity_hashes_config_and_launchd_and_round_trips() -> None:
    identity = _locked_identity()
    assert identity.config_identity == config_identity_digest(CONFIG_BYTES)
    assert identity.launchd_identity == launchd_identity_digest(LAUNCHD_BYTES)
    assert identity.runtime_profile == RUNTIME_PROFILE
    assert identity.evidence_schema_version == EVIDENCE_SCHEMA_VERSION

    decoded = decode_gate_identity(identity.document())
    assert decoded == identity
    assert canonical_gate_identity_bytes(identity) == canonical_gate_identity_bytes(decoded)


def test_gate_identity_rejects_drift_prone_fields() -> None:
    with pytest.raises(PaperEvidenceError):
        _locked_identity(repo_sha="short")


# ---------------------------------------------------------------------------
# verdict and hard rules
# ---------------------------------------------------------------------------


def _evaluate(snapshot: PaperSnapshot, locked: PaperGateIdentity) -> PaperGateVerdict:
    return evaluate_gate(
        snapshot,
        locked,
        current_identity=build_gate_identity(
            gate_id=locked.gate_id,
            gate_type=locked.gate_type,
            repo_sha=locked.repo_sha,
            config_bytes=CONFIG_BYTES,
            launchd_bytes=LAUNCHD_BYTES,
            started_at=UNDERWAY,
        ),
        evaluated_at=UNDERWAY,
    )


def test_healthy_gate_passes() -> None:
    verdict = _evaluate(_healthy_snapshot(), _locked_identity())
    assert verdict.verdict is GateVerdict.PASS
    assert verdict.reasons == ()


def test_same_input_same_verdict() -> None:
    """The evaluator is pure: identical inputs, identical canonical verdicts."""
    snapshot = _healthy_snapshot()
    locked = _locked_identity()
    first = _evaluate(snapshot, locked)
    second = _evaluate(snapshot, locked)
    assert first == second
    assert canonical_gate_verdict_bytes(first) == canonical_gate_verdict_bytes(second)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"writer_lease_held": False}, "writer_not_exclusive"),
        (
            {"health": _healthy_health(reconciliation_state=ReconciliationState.UNRESOLVED)},
            "reconciliation_not_clean",
        ),
        (
            {
                "health": _healthy_health(
                    trade_blocking_guard="price_deviation", trade_permitted=False
                )
            },
            "risk_gate_not_healthy",
        ),
        ({"health": _healthy_health(runtime_state=RuntimeState.STOPPED)}, "runtime_not_running"),
        ({"health": _healthy_health(runtime_ready=False)}, "runtime_not_ready"),
        ({"backup_state": BackupSummary.STALE}, "backup_not_fresh"),
        ({"backup_state": BackupSummary.ABSENT}, "backup_not_fresh"),
        ({"alert_state": AlertSummary.CRITICAL_ACTIVE}, "critical_alert_active"),
        ({"alert_state": AlertSummary.UNAVAILABLE}, "critical_alert_active"),
    ],
)
def test_hard_rules_are_deterministic_and_independent(
    overrides: dict[str, Any], reason: str
) -> None:
    """Each violated hard rule flips a healthy gate to FAIL with exactly its reason."""
    snapshot = _healthy_snapshot(**overrides)
    verdict = _evaluate(snapshot, _locked_identity())
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (reason,)


# ---------------------------------------------------------------------------
# drift -> INVALID
# ---------------------------------------------------------------------------


def test_repo_sha_drift_invalidates() -> None:
    locked = _locked_identity(repo_sha=OTHER_SHA)
    verdict = _evaluate(_healthy_snapshot(), locked)
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("repo_sha_drift",)


def test_config_drift_invalidates() -> None:
    snapshot = _healthy_snapshot()
    locked = _locked_identity()
    verdict = evaluate_gate(
        snapshot,
        locked,
        current_identity=build_gate_identity(
            gate_id=locked.gate_id,
            gate_type=locked.gate_type,
            repo_sha=locked.repo_sha,
            config_bytes=OTHER_CONFIG_BYTES,
            launchd_bytes=LAUNCHD_BYTES,
            started_at=UNDERWAY,
        ),
        evaluated_at=UNDERWAY,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("config_drift",)


def test_launchd_drift_invalidates() -> None:
    snapshot = _healthy_snapshot()
    locked = _locked_identity()
    verdict = evaluate_gate(
        snapshot,
        locked,
        current_identity=build_gate_identity(
            gate_id=locked.gate_id,
            gate_type=locked.gate_type,
            repo_sha=locked.repo_sha,
            config_bytes=CONFIG_BYTES,
            launchd_bytes=OTHER_LAUNCHD_BYTES,
            started_at=UNDERWAY,
        ),
        evaluated_at=UNDERWAY,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("launchd_drift",)


def test_mutating_one_locked_identity_field_invalidates() -> None:
    """The identity is locked, not merely recorded: a single changed field -> INVALID."""
    snapshot = _healthy_snapshot()
    locked = _locked_identity()
    assert _evaluate(snapshot, locked).verdict is GateVerdict.PASS

    tampered = replace(locked, config_identity=Sha256Digest("f" * 64))
    verdict = _evaluate(snapshot, tampered)
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("config_drift",)


def test_live_boundary_fails_closed() -> None:
    """An unexpected Live capability can never pass: it is drift, and it invalidates."""
    snapshot = _healthy_snapshot(live_enabled=True)
    verdict = _evaluate(snapshot, _locked_identity())
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("unexpected_live_capability",)


def test_drift_takes_precedence_over_hard_rule_failure() -> None:
    """Once identity drift exists, the verdict is INVALID even if the runtime is also sick."""
    snapshot = _healthy_snapshot(writer_lease_held=False)
    locked = _locked_identity(repo_sha=OTHER_SHA)
    verdict = _evaluate(snapshot, locked)
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("repo_sha_drift",)


# ---------------------------------------------------------------------------
# verdict document
# ---------------------------------------------------------------------------


def test_verdict_round_trips_and_validates() -> None:
    verdict = _evaluate(_healthy_snapshot(), _locked_identity())
    decoded = decode_gate_verdict(verdict.document())
    assert decoded == verdict
    assert verdict.schema == GATE_VERDICT_SCHEMA
    assert canonical_gate_verdict_bytes(verdict) == canonical_gate_verdict_bytes(decoded)


def test_pass_verdict_rejects_a_reason() -> None:
    with pytest.raises(PaperEvidenceError):
        PaperGateVerdict(
            verdict=GateVerdict.PASS,
            gate_id="M4-72H-001",
            gate_type=GateType.T72H,
            evaluated_at=UNDERWAY,
            reasons=("spurious",),
        )


def test_gate_identity_schema_constant() -> None:
    assert _locked_identity().schema == GATE_IDENTITY_SCHEMA


def test_freshness_default_is_positive() -> None:
    assert DEFAULT_BACKUP_FRESHNESS_SECONDS > 0


def test_alert_severity_reference_is_importable() -> None:
    # Guards against a silent redefinition of the critical severity the gate keys on.
    assert AlertSeverity.CRITICAL.value == "critical"
