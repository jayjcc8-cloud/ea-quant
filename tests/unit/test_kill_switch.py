"""Durable scoped operator kill-switch authority contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId
from ea.risk.kill_switch import (
    KillSwitchScope,
    KillSwitchState,
    OperatorKillSwitchError,
    canonical_kill_switch_bytes,
    create_operator_kill_switch_authority,
    kill_switch_digest,
    restore_operator_kill_switch_authority,
)
from unit.test_paper_runtime import drive, session

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")


def _time(second: int) -> datetime:
    return datetime(2026, 9, 27, 12, 0, second, tzinfo=UTC)


def test_fresh_authority_is_active_across_all_scopes() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    assert authority.revision == 0
    assert not authority.effective_halted()
    assert authority.halted_scopes() == ()
    for scope in KillSwitchScope:
        assert not authority.is_halted(scope)


def test_halt_before_submit_blocks_outbound_effect() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    authority.halt(KillSwitchScope.STRATEGY, reason="operator_requested_stop", changed_at=_time(1))
    assert authority.effective_halted()
    assert authority.is_halted(KillSwitchScope.STRATEGY)


def test_halt_is_monotone_and_retains_first_reason() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    first = authority.halt(KillSwitchScope.ACCOUNT, reason="first_reason", changed_at=_time(1))
    second = authority.halt(KillSwitchScope.ACCOUNT, reason="second_reason", changed_at=_time(2))
    assert second is first
    assert first.reason == "first_reason"
    assert authority.revision == 1


def test_halt_does_not_liquidate_or_change_other_scopes() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    authority.halt(KillSwitchScope.GLOBAL, reason="global_stop", changed_at=_time(1))
    # Halt is a pure control transition; it never mutates any scope beyond the target.
    assert authority.is_halted(KillSwitchScope.GLOBAL)
    assert not authority.is_halted(KillSwitchScope.ACCOUNT)
    assert not authority.is_halted(KillSwitchScope.STRATEGY)
    assert authority.effective_halted()


def test_resume_requires_an_explicit_operator_action_and_a_halted_scope() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    with pytest.raises(OperatorKillSwitchError) as error:
        authority.resume(KillSwitchScope.STRATEGY, reason="resume", changed_at=_time(1))
    assert error.value.code is OutcomeCode.CONFLICTING_ID
    authority.halt(KillSwitchScope.STRATEGY, reason="stop", changed_at=_time(2))
    control = authority.resume(KillSwitchScope.STRATEGY, reason="resume", changed_at=_time(3))
    assert control.state is KillSwitchState.ACTIVE
    assert not authority.effective_halted()


def test_restart_while_halted_stays_halted() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    authority.halt(KillSwitchScope.GLOBAL, reason="incident", changed_at=_time(1))
    payload = canonical_kill_switch_bytes(authority)
    restored = restore_operator_kill_switch_authority(RUN_ID, payload)
    assert restored.effective_halted()
    assert restored.is_halted(KillSwitchScope.GLOBAL)
    assert restored.halted_scopes() == (KillSwitchScope.GLOBAL,)
    assert kill_switch_digest(restored) == kill_switch_digest(authority)


def test_restore_rejects_identity_conflict() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    payload = canonical_kill_switch_bytes(authority)
    with pytest.raises(OperatorKillSwitchError) as error:
        restore_operator_kill_switch_authority(OTHER_RUN_ID, payload)
    assert error.value.code is OutcomeCode.CONFLICTING_ID


def test_restore_rejects_halted_scope_without_change_time() -> None:
    payload = (
        b'{"schema":"ea.paper-kill-switch.v1","run_id":"12345678-1234-4234-8234-123456789abc",'
        b'"revision":0,"scopes":{"strategy":{"state":"halted","reason":"x","changed_at":null},'
        b'"account":{"state":"active","reason":"","changed_at":null},'
        b'"global":{"state":"active","reason":"","changed_at":null}}}'
    )
    with pytest.raises(OperatorKillSwitchError) as error:
        restore_operator_kill_switch_authority(RUN_ID, payload)
    assert error.value.code is OutcomeCode.CONFLICTING_ID


def test_canonical_bytes_are_stable_and_bound() -> None:
    authority = create_operator_kill_switch_authority(RUN_ID)
    assert canonical_kill_switch_bytes(authority) == canonical_kill_switch_bytes(authority)
    authority.halt(KillSwitchScope.ACCOUNT, reason="stop", changed_at=_time(1))
    assert canonical_kill_switch_bytes(authority) == canonical_kill_switch_bytes(authority)


def test_halt_before_submit_produces_no_broker_order(tmp_path: Path) -> None:
    kill_switch = create_operator_kill_switch_authority(RUN_ID)
    kill_switch.halt(KillSwitchScope.GLOBAL, reason="operator_stop", changed_at=_time(1))
    engine, clock = session(tmp_path, kill_switch=kill_switch)
    drive(engine, clock)
    assert engine.status()["kill_switch_halted"] is True
    assert engine.status()["fills"] == 0
    assert not engine.broker._records
    assert engine.reconcile() == "match"


def test_resume_after_halt_restores_trading(tmp_path: Path) -> None:
    kill_switch = create_operator_kill_switch_authority(RUN_ID)
    kill_switch.halt(KillSwitchScope.STRATEGY, reason="stop", changed_at=_time(1))
    kill_switch.resume(KillSwitchScope.STRATEGY, reason="resume", changed_at=_time(2))
    engine, clock = session(tmp_path, kill_switch=kill_switch)
    drive(engine, clock)
    assert engine.status()["kill_switch_halted"] is False
    assert engine.status()["fills"] == 6
