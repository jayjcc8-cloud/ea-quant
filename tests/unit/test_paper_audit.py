from __future__ import annotations

import pytest

from ea.core import (
    AuditAppendAcknowledgement,
    AuditRecordKind,
    RunBinding,
    RunReference,
    Sha256Digest,
    audit_subject_digest,
    canonical_execution_fact_processing_outcome_bytes,
    canonical_run_prepared_audit_payload,
    create_audited_execution_fact_handoff,
)
from ea.core.audit import AUDIT_SUBJECT_BY_RECORD_KIND
from ea.core.lifecycle import create_paper_audited_execution_fact_handoff
from ea.core.paper import canonical_paper_fact_dispatch_payload
from ea.execution.order_lifecycle import OrderCommandTracker
from ea.execution.paper_broker import PaperBroker
from ea.product.offline_demo import _DemoAudit
from unit.test_execution_fact_authority import TIME, _orders, _process_next
from unit.test_paper_broker import _paper_runtime


def test_paper_handoff_requires_exact_ingress_dispatch_and_durable_outcome() -> None:
    specs, orders, (order,) = _orders(quantity="2")
    broker = PaperBroker(orders)
    command = OrderCommandTracker(orders).begin_submit(order)
    assert command is not None
    ingress = broker.submit(command, submitted_at=TIME).ingresses[0]
    queue, facts = _paper_runtime(specs, orders, broker, (ingress,))
    _, outcome = _process_next(queue, facts)
    binding = RunBinding(
        RunReference(order.order_id.run_id, Sha256Digest("1" * 64)), Sha256Digest("2" * 64)
    )
    audit = _DemoAudit(binding, specs)

    def append(kind: AuditRecordKind, payload: bytes) -> AuditAppendAcknowledgement:
        return audit.append(
            record_kind=kind,
            subject_kind=AUDIT_SUBJECT_BY_RECORD_KIND[kind],
            subject_sha256=audit_subject_digest(kind, payload),
            canonical_payload=payload,
        )

    append(AuditRecordKind.RUN_PREPARED, canonical_run_prepared_audit_payload(binding))
    payload = canonical_paper_fact_dispatch_payload(
        ingress, outcome.runtime_dispatch_sequence, run_id=outcome.run_id
    )
    dispatch_ack = append(AuditRecordKind.PAPER_FACT_DISPATCH, payload)
    outcome_ack = append(
        AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
        canonical_execution_fact_processing_outcome_bytes(outcome),
    )
    handoff = create_paper_audited_execution_fact_handoff(
        ingress=ingress,
        outcome=outcome,
        dispatch_acknowledgement=dispatch_ack,
        outcome_acknowledgement=outcome_ack,
    )
    assert handoff.ingress_identity == ingress.identity
    assert handoff.dispatch_sequence == outcome.runtime_dispatch_sequence
    wrong_ack = append(
        AuditRecordKind.PAPER_FACT_DISPATCH,
        canonical_paper_fact_dispatch_payload(
            ingress, outcome.runtime_dispatch_sequence + 1, run_id=outcome.run_id
        ),
    )
    with pytest.raises(ValueError):
        create_paper_audited_execution_fact_handoff(
            ingress=ingress,
            outcome=outcome,
            dispatch_acknowledgement=wrong_ack,
            outcome_acknowledgement=outcome_ack,
        )
    with pytest.raises(ValueError):
        create_audited_execution_fact_handoff(
            outcome=outcome,
            batch_acknowledgement=dispatch_ack,
            outcome_acknowledgement=outcome_ack,
        )
