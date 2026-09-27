"""Truthful audit payloads for the explicitly invoked local Paper profile."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ea.core.execution_messages import (
    ExecutionFactIngress,
    Order,
    OrderIntent,
    RiskDecision,
    canonical_execution_fact_ingress_bytes,
    canonical_order_bytes,
    canonical_order_intent_bytes,
    canonical_risk_decision_bytes,
    execution_request_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.risk import (
    RiskEvaluationEvidence,
    canonical_risk_evaluation_evidence_bytes,
)
from ea.core.run import RunId, Sha256Digest
from ea.core.time import require_utc

_SCHEMAS = {
    "ea.audit-paper-submission.v1": {
        "dispatch_sequence",
        "order",
        "market",
        "portfolio_snapshot_sha256",
        "risk_state_sha256",
    },
    "ea.audit-paper-fact-dispatch.v1": {"dispatch_sequence", "ingress"},
    "ea.audit-paper-submission-result.v1": {
        "dispatch_sequence",
        "order_id",
        "client_submission_key",
        "execution_request_sha256",
        "submitted_at",
        "submission_state",
        "venue_order_id",
    },
    "ea.audit-paper-order-construction.v1": {
        "dispatch_sequence",
        "order",
        "intent",
        "decision",
        "evidence",
    },
    "ea.audit-paper-terminal.v1": {
        "dispatch_sequence",
        "reason",
        "portfolio_snapshot_sha256",
        "risk_state_sha256",
        "pending_facts",
        "pending_orders",
        "fill_count",
    },
}


_UTC_TEXT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _utc_text(value: datetime) -> str:
    return require_utc(value, field="submitted_at").strftime(_UTC_TEXT_FORMAT)


def _require_utc_text(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"Paper {field} is not canonical UTC text")
    try:
        parsed = datetime.strptime(value, _UTC_TEXT_FORMAT).replace(tzinfo=UTC)
    except ValueError as error:
        raise ValueError(f"Paper {field} is not canonical UTC text") from error
    if parsed.strftime(_UTC_TEXT_FORMAT) != value:
        raise ValueError(f"Paper {field} is not canonical UTC text")
    return value


def require_paper_audit_document(document: dict[str, Any], schema: str) -> None:
    fields = _SCHEMAS[schema] | {"schema", "canonicalization", "run_id"}
    if (
        set(document) != fields
        or document.get("schema") != schema
        or document.get("canonicalization") != "ea-canonical-json-v1"
    ):
        raise ValueError("Paper audit payload schema or fields conflict")
    RunId(document["run_id"])
    for field in fields & {"dispatch_sequence", "pending_facts", "pending_orders", "fill_count"}:
        value = document[field]
        minimum = 0 if schema == "ea.audit-paper-terminal.v1" else 1
        if type(value) is not int or not minimum <= value < 2**64:
            raise ValueError("Paper audit count/sequence is outside bounds")
    for field in fields & {
        "portfolio_snapshot_sha256",
        "risk_state_sha256",
        "client_submission_key",
        "execution_request_sha256",
    }:
        Sha256Digest(document[field])
    if schema == "ea.audit-paper-terminal.v1":
        reason = document["reason"]
        if type(reason) is not str or not 1 <= len(reason) <= 128:
            raise ValueError("Paper terminal reason is invalid")
    if schema == "ea.audit-paper-submission-result.v1":
        state = document["submission_state"]
        if state not in {"submitted", "definitely_not_submitted", "uncertain"}:
            raise ValueError("Paper submission result state is invalid")
        venue = document["venue_order_id"]
        if venue is not None and type(venue) is not str:
            raise ValueError("Paper submission result venue id is invalid")
        _require_utc_text(document["submitted_at"], field="submitted_at")
    for field in fields & {
        "order",
        "market",
        "ingress",
        "order_id",
        "intent",
        "decision",
        "evidence",
    }:
        if type(document[field]) is not dict or not document[field]:
            raise ValueError("Paper audit evidence must be a canonical object")


def _payload(schema: str, run_id: RunId, **fields: Any) -> bytes:
    document = {
        "schema": schema,
        "canonicalization": "ea-canonical-json-v1",
        "run_id": run_id.value,
        **fields,
    }
    require_paper_audit_document(document, schema)
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def canonical_paper_submission_payload(
    order: Order,
    market: MarketDataEnvelope,
    dispatch_sequence: int,
    *,
    portfolio_snapshot_sha256: Sha256Digest,
    risk_state_sha256: Sha256Digest,
) -> bytes:
    return _payload(
        "ea.audit-paper-submission.v1",
        order.order_id.run_id,
        dispatch_sequence=dispatch_sequence,
        order=json.loads(canonical_order_bytes(order)),
        market=json.loads(canonical_market_data_record_bytes(market)),
        portfolio_snapshot_sha256=portfolio_snapshot_sha256.value,
        risk_state_sha256=risk_state_sha256.value,
    )


def canonical_paper_fact_dispatch_payload(
    ingress: ExecutionFactIngress,
    dispatch_sequence: int,
    *,
    run_id: RunId,
) -> bytes:
    return _payload(
        "ea.audit-paper-fact-dispatch.v1",
        run_id,
        dispatch_sequence=dispatch_sequence,
        ingress=json.loads(canonical_execution_fact_ingress_bytes(ingress)),
    )


def canonical_paper_submission_result_payload(
    order: Order,
    dispatch_sequence: int,
    *,
    submitted_at: datetime,
    submission_state: str,
    venue_order_id: str | None,
) -> bytes:
    """Durably classify one outbound submission after transport has settled.

    ``submission_state`` is one of ``submitted``, ``definitely_not_submitted``
    or ``uncertain`` and ``submitted_at`` is the canonical UTC instant passed to
    the transport. This record is the restart-visible effect classification
    that prevents an ambiguous or definite no-effect transport from being
    blindly resent, and carries the exact submit time a restarted broker needs
    to rebuild its retained request history without a second effect.
    """
    return _payload(
        "ea.audit-paper-submission-result.v1",
        order.order_id.run_id,
        dispatch_sequence=dispatch_sequence,
        order_id={
            "owner_kind": order.order_id.owner_kind.value,
            "owner_sequence": order.order_id.owner_sequence,
            "run_id": order.order_id.run_id.value,
        },
        client_submission_key=order.client_submission_key.value,
        execution_request_sha256=execution_request_digest(order).value,
        submitted_at=_utc_text(submitted_at),
        submission_state=submission_state,
        venue_order_id=venue_order_id,
    )


def canonical_paper_order_construction_payload(
    order: Order,
    intent: OrderIntent,
    decision: RiskDecision,
    evidence: RiskEvaluationEvidence,
    dispatch_sequence: int,
) -> bytes:
    """Durably retain the order-construction context for restart re-materialization.

    The approved Order alone is insufficient to reconstruct a broker/order
    authority on restart: ``create_order`` must re-prove it against the exact
    ``OrderIntent`` and full ``RiskEvaluationResult`` (decision plus evidence,
    which carries the approval). This record persists all of them so a restarted
    runtime can rebuild the issued Orders without a hidden repair.
    """
    return _payload(
        "ea.audit-paper-order-construction.v1",
        order.order_id.run_id,
        dispatch_sequence=dispatch_sequence,
        order=json.loads(canonical_order_bytes(order)),
        intent=json.loads(canonical_order_intent_bytes(intent)),
        decision=json.loads(canonical_risk_decision_bytes(decision)),
        evidence=json.loads(canonical_risk_evaluation_evidence_bytes(evidence)),
    )


def canonical_paper_terminal_payload(
    run_id: RunId,
    *,
    dispatch_sequence: int,
    reason: str,
    portfolio_snapshot_sha256: Sha256Digest,
    risk_state_sha256: Sha256Digest,
    pending_facts: int,
    pending_orders: int,
    fill_count: int,
) -> bytes:
    return _payload(
        "ea.audit-paper-terminal.v1",
        run_id,
        dispatch_sequence=dispatch_sequence,
        reason=reason,
        portfolio_snapshot_sha256=portfolio_snapshot_sha256.value,
        risk_state_sha256=risk_state_sha256.value,
        pending_facts=pending_facts,
        pending_orders=pending_orders,
        fill_count=fill_count,
    )
