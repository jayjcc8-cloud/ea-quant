"""Truthful audit payloads for the explicitly invoked local Paper profile."""

from __future__ import annotations

import json
from typing import Any

from ea.core.execution_messages import (
    ExecutionFactIngress,
    Order,
    canonical_execution_fact_ingress_bytes,
    canonical_order_bytes,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.run import RunId, Sha256Digest

_SCHEMAS = {
    "ea.audit-paper-submission.v1": {
        "dispatch_sequence",
        "order",
        "market",
        "portfolio_snapshot_sha256",
        "risk_state_sha256",
    },
    "ea.audit-paper-fact-dispatch.v1": {"dispatch_sequence", "ingress"},
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
    for field in fields & {"portfolio_snapshot_sha256", "risk_state_sha256"}:
        Sha256Digest(document[field])
    if schema == "ea.audit-paper-terminal.v1":
        reason = document["reason"]
        if type(reason) is not str or not 1 <= len(reason) <= 128:
            raise ValueError("Paper terminal reason is invalid")
    for field in fields & {"order", "market", "ingress"}:
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
