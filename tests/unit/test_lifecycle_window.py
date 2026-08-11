from __future__ import annotations

import json
from copy import copy, deepcopy
from pickle import dumps

import pytest

from ea.core.audit import AuditLogicalKey, AuditRecordKind, AuditSubjectKind
from ea.core.execution_messages import Order
from ea.core.historical_matching import HistoricalDispatchKind
from ea.core.lifecycle import (
    ActiveDispatchWindow,
    LifecycleError,
    SubmissionAuthorizationAttemptStatus,
    _create_active_dispatch_window,
    active_dispatch_window_digest,
    canonical_active_dispatch_window_bytes,
    canonical_submission_authorization_attempt_outcome_bytes,
    create_submission_authorization_attempt_outcome,
    submission_authorization_attempt_outcome_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.core.runtime import runtime_root_order_key
from unit.test_historical_matcher import _system


def _binding_and_order() -> tuple[RunBinding, Order, MarketDataEnvelope]:
    _fixture, matcher, orders, causal, _delayed, _end = _system()
    return (
        RunBinding(
            RunReference(matcher.run_id, Sha256Digest("11" * 32)),
            Sha256Digest("22" * 32),
        ),
        orders[0],
        causal,
    )


def test_active_dispatch_window_is_canonical_and_nontransferable() -> None:
    binding, _order, causal = _binding_and_order()
    window = _create_active_dispatch_window(
        binding=binding,
        coordinator_state_version=3,
        dispatch_kind=HistoricalDispatchKind.MARKET,
        dispatch_sequence=7,
        trigger_root_key=runtime_root_order_key(causal),
        trigger_root_sha256=Sha256Digest("33" * 32),
        batch_sha256=Sha256Digest("44" * 32),
        batch_ack_sha256=Sha256Digest("55" * 32),
        handoff_sha256s=(Sha256Digest("66" * 32), Sha256Digest("77" * 32)),
        audited_handoff_chain_head_sha256=Sha256Digest("88" * 32),
        authorization_allowed=True,
    )

    document = json.loads(canonical_active_dispatch_window_bytes(window))
    assert document["schema"] == "ea.coordinator-active-dispatch-window.v1"
    assert document["run_id"] == binding.reference.run_id.value
    assert document["coordinator_state_version"] == 3
    assert document["dispatch_sequence"] == 7
    assert document["handoff_count"] == 2
    assert document["audited_handoff_chain_head_sha256"] == "88" * 32
    assert document["authorization_allowed"] is True
    assert active_dispatch_window_digest(window) == active_dispatch_window_digest(window)

    with pytest.raises(TypeError, match="coordinator"):
        ActiveDispatchWindow()
    for operation in (copy, deepcopy, dumps):
        with pytest.raises(TypeError):
            operation(window)


def test_bounded_end_window_cannot_authorize() -> None:
    binding, _order, causal = _binding_and_order()
    with pytest.raises(LifecycleError) as raised:
        _create_active_dispatch_window(
            binding=binding,
            coordinator_state_version=1,
            dispatch_kind=HistoricalDispatchKind.END_OF_RUN,
            dispatch_sequence=1,
            trigger_root_key=runtime_root_order_key(causal),
            trigger_root_sha256=Sha256Digest("33" * 32),
            batch_sha256=Sha256Digest("44" * 32),
            batch_ack_sha256=Sha256Digest("55" * 32),
            handoff_sha256s=(),
            audited_handoff_chain_head_sha256=Sha256Digest("66" * 32),
            authorization_allowed=True,
        )
    assert raised.value.code is OutcomeCode.CONFLICTING_ID


@pytest.mark.parametrize(
    ("status", "has_key", "has_ack", "error_code"),
    [
        (SubmissionAuthorizationAttemptStatus.DENIED, False, False, OutcomeCode.RISK_REJECTED),
        (
            SubmissionAuthorizationAttemptStatus.UNRESOLVED,
            True,
            False,
            OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
        ),
        (SubmissionAuthorizationAttemptStatus.AUTHORIZED, True, True, None),
        (
            SubmissionAuthorizationAttemptStatus.BURNED,
            True,
            True,
            OutcomeCode.RISK_STALE_APPROVAL,
        ),
        (
            SubmissionAuthorizationAttemptStatus.FAILED,
            True,
            False,
            OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
        ),
    ],
)
def test_authorization_attempt_outcome_has_closed_status_evidence(
    status: SubmissionAuthorizationAttemptStatus,
    has_key: bool,
    has_ack: bool,
    error_code: OutcomeCode | None,
) -> None:
    binding, order, _causal = _binding_and_order()
    logical_key = (
        AuditLogicalKey(
            AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
            AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST,
            Sha256Digest("66" * 32),
        )
        if has_key
        else None
    )
    outcome = create_submission_authorization_attempt_outcome(
        binding=binding,
        dispatch_sequence=order.dispatch_sequence,
        trigger_root_sha256=Sha256Digest("33" * 32),
        order_id=order.order_id,
        execution_request_sha256=Sha256Digest("44" * 32),
        authorization_payload_sha256=Sha256Digest("55" * 32),
        status=status,
        logical_key=logical_key,
        acknowledgement_sha256=Sha256Digest("77" * 32) if has_ack else None,
        error_code=error_code,
    )

    document = json.loads(canonical_submission_authorization_attempt_outcome_bytes(outcome))
    assert document["status"] == status.value
    assert (document["logical_key"] is not None) is has_key
    assert (document["acknowledgement_sha256"] is not None) is has_ack
    assert document["error_code"] == (None if error_code is None else error_code.value)
    assert submission_authorization_attempt_outcome_digest(outcome) == (
        submission_authorization_attempt_outcome_digest(outcome)
    )


def test_authorization_attempt_outcome_rejects_status_evidence_mismatch() -> None:
    binding, order, _causal = _binding_and_order()
    with pytest.raises(LifecycleError) as raised:
        create_submission_authorization_attempt_outcome(
            binding=binding,
            dispatch_sequence=order.dispatch_sequence,
            trigger_root_sha256=Sha256Digest("33" * 32),
            order_id=order.order_id,
            execution_request_sha256=Sha256Digest("44" * 32),
            authorization_payload_sha256=Sha256Digest("55" * 32),
            status=SubmissionAuthorizationAttemptStatus.AUTHORIZED,
            logical_key=None,
            acknowledgement_sha256=None,
            error_code=None,
        )
    assert raised.value.code is OutcomeCode.CONFLICTING_ID
