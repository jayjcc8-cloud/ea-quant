"""Immutable deterministic historical-matcher contracts from ADR 0018."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Any, cast, final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecSetId,
    PriceDomain,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    IngressIdentity,
    SourceNamespace,
    SourceNativeSequence,
)
from ea.core.execution_messages import (
    ExecutionFactIngress,
    ExecutionFactKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    FactProvenance,
    FactProvenanceId,
    Order,
    OrderKind,
    OrderSide,
    TimeInForce,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_execution_request_bytes,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    create_trade_execution_fact,
    execution_fact_ingress_digest,
    execution_request_digest,
    order_client_submission_key,
    order_digest,
)
from ea.core.identity import Instrument, VenueId
from ea.core.market_data import Adjustment, MarketDataEnvelope, SourceId
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.runtime import (
    END_OF_RUN_KIND_RANKS,
    MARKET_DATA_KIND_RANKS,
    RUNTIME_ROOT_DOMAIN_RANKS,
    EndOfRunKind,
    EndOfRunRoot,
    RuntimeRootDomain,
    RuntimeRootOrderKey,
    _EndOfRunSuffix,
    _MarketDataSuffix,
    runtime_root_order_key,
)

HISTORICAL_SUBMISSION_RECEIPT_CANONICALIZATION = "ea-phase1-historical-submission-receipt-v1"
HISTORICAL_SUBMISSION_RECEIPT_DIGEST_DOMAIN = b"ea.phase1-historical-submission-receipt.v1\0"
HISTORICAL_MATCHER_DISPATCH_BATCH_CANONICALIZATION = (
    "ea-phase1-historical-matcher-dispatch-batch-v1"
)
HISTORICAL_MATCHER_DISPATCH_BATCH_DIGEST_DOMAIN = (
    b"ea.phase1-historical-matcher-dispatch-batch.v1\0"
)
_HISTORICAL_MATCHER_DISPATCH_HISTORY_DIGEST_DOMAIN = (
    b"ea.phase1-historical-matcher-dispatch-history.v1\0"
)
_HISTORICAL_MATCHER_DISPATCH_INGRESS_HISTORY_DIGEST_DOMAIN = (
    b"ea.phase1-historical-matcher-dispatch-ingress-history.v1\0"
)
HISTORICAL_MATCHER_STATE_CANONICALIZATION = "ea-phase1-historical-matcher-state-v1"
HISTORICAL_MATCHER_STATE_DIGEST_DOMAIN = b"ea.phase1-historical-matcher-state.v1\0"
HISTORICAL_MATCHER_CONFLICT_CANONICALIZATION = "ea-phase1-historical-matcher-conflict-v1"
HISTORICAL_MATCHER_CONFLICT_DIGEST_DOMAIN = b"ea.phase1-historical-matcher-conflict.v1\0"
HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN = b"ea.phase1-historical-matcher-market-root.v1\0"
HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN = b"ea.phase1-historical-matcher-end-root.v1\0"
HISTORICAL_MATCHER_OBSERVATION_CANONICALIZATION = "ea-phase1-historical-matcher-observation-v1"
HISTORICAL_MATCHER_OBSERVATION_DIGEST_DOMAIN = b"ea.phase1-historical-matcher-observation.v1\0"
HISTORICAL_MATCHER_SCHEMA_VERSION = 1
_MAX_UINT64 = (1 << 64) - 1
_AUTHORIZATION_PROOF_SEAL = object()

_MATCHER_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.ARITHMETIC_OVERFLOW,
        OutcomeCode.CONFLICTING_ID,
        OutcomeCode.SUBMISSION_BLOCKED_BY_HALT,
        OutcomeCode.RISK_STALE_APPROVAL,
        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
        OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
    }
)
_AUTHORIZATION_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.CONFLICTING_ID,
        OutcomeCode.SUBMISSION_BLOCKED_BY_HALT,
        OutcomeCode.RISK_STALE_APPROVAL,
        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
        OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
    }
)


class HistoricalMatcherError(ValueError):
    """Closed structured failure for the Phase 1 historical matcher."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _MATCHER_ERROR_CODES:
            raise TypeError("historical matcher errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


class HistoricalPreEffectAuthorizationError(ValueError):
    """Normal fail-closed denial returned by the coordinator-owned authorization port."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _AUTHORIZATION_ERROR_CODES:
            raise TypeError("historical authorization errors require an exact permitted code")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> HistoricalMatcherError:
    return HistoricalMatcherError(code, message)


def _utc_text(value: object) -> str:
    from datetime import datetime

    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical time must be exact datetime")
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "canonical time must be UTC")
    if offset.total_seconds() != 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "canonical time must be UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_json(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "value cannot be canonically encoded") from error


def _framed_digest(domain: bytes, payload: bytes) -> Sha256Digest:
    if type(domain) is not bytes or type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "digest inputs must be exact bytes")
    return Sha256Digest(sha256(domain + len(payload).to_bytes(8, "big") + payload).hexdigest())


def _economic_id_document(value: EconomicId) -> dict[str, object]:
    if type(value) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "economic ID must be exact")
    return {
        "owner_kind": value.owner_kind.value,
        "owner_sequence": value.owner_sequence,
        "run_id": value.run_id.value,
    }


def _instrument_document(value: Instrument) -> dict[str, str]:
    if type(value) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument must be exact")
    return {"symbol": value.symbol, "venue": value.venue.code}


def _ingress_identity_document(value: IngressIdentity) -> dict[str, object]:
    if type(value) is not IngressIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "ingress identity must be exact")
    return {
        "ingress_sequence": value.ingress_sequence,
        "source_namespace": value.source_namespace.value,
    }


def canonical_end_of_run_root_bytes(root: EndOfRunRoot) -> bytes:
    """Encode the ADR 0018 terminal-root preimage."""
    if type(root) is not EndOfRunRoot:
        raise _fail(OutcomeCode.INVALID_TYPE, "end root must be exact")
    return _canonical_json(
        {
            "available_at": _utc_text(root.available_at),
            "kind": root.kind.value,
            "producer_namespace": root.producer_namespace.value,
            "producer_sequence": root.producer_sequence,
            "run_id": root.run_id.value,
            "type": "end_of_run",
        }
    )


def historical_market_root_digest(root: MarketDataEnvelope) -> Sha256Digest:
    if type(root) is not MarketDataEnvelope:
        raise _fail(OutcomeCode.INVALID_TYPE, "market root must be exact")
    try:
        payload = canonical_market_data_record_bytes(root)
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "market root cannot be encoded") from error
    return _framed_digest(HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN, payload)


def historical_end_root_digest(root: EndOfRunRoot) -> Sha256Digest:
    return _framed_digest(
        HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN,
        canonical_end_of_run_root_bytes(root),
    )


def _historical_root_digest_from_bytes(
    *,
    kind: HistoricalDispatchKind,
    canonical_root_bytes: bytes,
) -> Sha256Digest:
    if type(kind) is not HistoricalDispatchKind or type(canonical_root_bytes) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "historical root digest inputs are invalid")
    domain = (
        HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN
        if kind is HistoricalDispatchKind.MARKET
        else HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN
    )
    return _framed_digest(domain, canonical_root_bytes)


def runtime_root_key_document(
    root: MarketDataEnvelope | EndOfRunRoot,
) -> dict[str, object]:
    """Return the closed tagged matcher root-key projection."""
    if type(root) is MarketDataEnvelope:
        payload = root.payload
        return {
            "adjustment": payload.adjustment.value,
            "available_at": _utc_text(root.available_at),
            "domain_rank": RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.MARKET_DATA],
            "event_time": _utc_text(root.event_time),
            "instrument": _instrument_document(payload.instrument),
            "interval_end": _utc_text(payload.interval_end),
            "interval_start": _utc_text(payload.interval_start),
            "kind_rank": MARKET_DATA_KIND_RANKS[root.kind],
            "revision": root.revision,
            "root_domain": RuntimeRootDomain.MARKET_DATA.value,
            "source": root.source.code,
            "source_sequence": root.source_sequence,
        }
    if type(root) is EndOfRunRoot:
        return {
            "available_at": _utc_text(root.available_at),
            "domain_rank": RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.END_OF_RUN],
            "kind": root.kind.value,
            "kind_rank": END_OF_RUN_KIND_RANKS[root.kind],
            "producer_namespace": root.producer_namespace.value,
            "producer_sequence": root.producer_sequence,
            "root_domain": RuntimeRootDomain.END_OF_RUN.value,
            "run_id": root.run_id.value,
        }
    raise _fail(OutcomeCode.INVALID_TYPE, "matcher root key requires market or end root")


def runtime_root_key_from_document(document: object) -> RuntimeRootOrderKey:
    """Strictly reconstruct one tagged matcher root key."""
    from datetime import UTC, datetime

    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "root key must be an object")

    def time(field: str) -> datetime:
        raw = document.get(field)
        if type(raw) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact str")
        try:
            value = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is invalid") from error
        if _utc_text(value) != raw:
            raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is not canonical")
        return value

    domain = document.get("root_domain")
    if domain == RuntimeRootDomain.MARKET_DATA.value:
        expected = {
            "adjustment",
            "available_at",
            "domain_rank",
            "event_time",
            "instrument",
            "interval_end",
            "interval_start",
            "kind_rank",
            "revision",
            "root_domain",
            "source",
            "source_sequence",
        }
        if set(document) != expected:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "market root key fields conflict")
        instrument = document["instrument"]
        if type(instrument) is not dict or set(instrument) != {"symbol", "venue"}:
            raise _fail(OutcomeCode.INVALID_TYPE, "market root instrument is invalid")
        values = (
            document["domain_rank"],
            document["kind_rank"],
            document["source_sequence"],
            document["revision"],
        )
        if any(type(value) is not int for value in values):
            raise _fail(OutcomeCode.INVALID_TYPE, "market root ranks must be exact int")
        if (
            document["domain_rank"] != RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.MARKET_DATA]
            or document["kind_rank"] != MARKET_DATA_KIND_RANKS[next(iter(MARKET_DATA_KIND_RANKS))]
            or document["source_sequence"] < 0
            or document["revision"] < 0
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "market root ranks conflict")
        for field in ("adjustment", "source"):
            if type(document[field]) is not str:
                raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact str")
        if any(type(instrument[field]) is not str for field in ("symbol", "venue")):
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument fields must be exact str")
        available_at = time("available_at")
        event_time = time("event_time")
        interval_start = time("interval_start")
        interval_end = time("interval_end")
        try:
            Adjustment(document["adjustment"])
            SourceId(document["source"])
            Instrument(VenueId(instrument["venue"]), instrument["symbol"])
        except (TypeError, ValueError) as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "market root identity is invalid") from error
        if (
            interval_start >= interval_end
            or event_time != interval_end
            or available_at < event_time
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "market root temporal fields conflict")
        return RuntimeRootOrderKey(
            available_at=available_at,
            domain_rank=document["domain_rank"],
            _suffix=_MarketDataSuffix(
                event_time=event_time,
                kind_rank=document["kind_rank"],
                source_code=document["source"],
                source_sequence=document["source_sequence"],
                instrument_venue=instrument["venue"],
                instrument_symbol=instrument["symbol"],
                interval_start=interval_start,
                interval_end=interval_end,
                adjustment=document["adjustment"],
                revision=document["revision"],
            ),
        )
    if domain == RuntimeRootDomain.END_OF_RUN.value:
        expected = {
            "available_at",
            "domain_rank",
            "kind",
            "kind_rank",
            "producer_namespace",
            "producer_sequence",
            "root_domain",
            "run_id",
        }
        if set(document) != expected:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "end root key fields conflict")
        for field in ("kind", "producer_namespace", "run_id"):
            if type(document[field]) is not str:
                raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact str")
        if any(
            type(document[field]) is not int
            for field in ("domain_rank", "kind_rank", "producer_sequence")
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "end root ranks must be exact int")
        try:
            kind = EndOfRunKind(document["kind"])
        except ValueError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "end kind is invalid") from error
        if (
            document["domain_rank"] != RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.END_OF_RUN]
            or document["kind_rank"] != END_OF_RUN_KIND_RANKS[kind]
            or document["producer_sequence"] < 0
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "end root ranks conflict")
        try:
            SourceNamespace(document["producer_namespace"])
            RunId(document["run_id"])
        except (TypeError, ValueError) as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "end root identity is invalid") from error
        return RuntimeRootOrderKey(
            available_at=time("available_at"),
            domain_rank=document["domain_rank"],
            _suffix=_EndOfRunSuffix(
                kind_rank=document["kind_rank"],
                producer_namespace=document["producer_namespace"],
                producer_sequence=document["producer_sequence"],
                run_id=document["run_id"],
            ),
        )
    raise _fail(OutcomeCode.OUT_OF_RANGE, "root key domain is invalid")


class HistoricalDispatchKind(StrEnum):
    MARKET = "market"
    END_OF_RUN = "end_of_run"


class HistoricalMatcherConflictKind(StrEnum):
    SUBMISSION_IDENTITY = "submission_identity"
    CLIENT_SUBMISSION_KEY = "client_submission_key"
    DISPATCH_IDENTITY = "dispatch_identity"
    NON_MONOTONE_DISPATCH = "non_monotone_dispatch"
    RETAINED_BINDING_DRIFT = "retained_binding_drift"


@final
@dataclass(frozen=True, slots=True, init=False)
class HistoricalSubmissionAuthorizationProof:
    _run_id: RunId
    _instrument_spec_set_id: InstrumentSpecSetId
    _instrument_spec_set_sha256: Sha256Digest
    _execution_policy: ExecutionPolicyRef
    _order_id: EconomicId
    _order_sha256: Sha256Digest
    _execution_request_sha256: Sha256Digest
    _causal_market_sha256: Sha256Digest
    _causal_root_key: RuntimeRootOrderKey
    _dispatch_sequence: int
    _audit_acknowledgement_id: str
    _audit_acknowledgement_sha256: Sha256Digest
    _portfolio_snapshot_version: int
    _risk_state_version: int
    _global_halt_epoch: int
    _risk_halt_epoch: int
    _instrument_gate_id: str
    _instrument_gate_version: int
    _held_for_order_id: EconomicId
    _authorization_state_version: int
    _issuer: object
    _seal: object

    def __init__(self) -> None:
        raise TypeError("authorization proofs are created only by the sealed factory")

    @property
    def audit_acknowledgement_id(self) -> str:
        return self._audit_acknowledgement_id

    @property
    def audit_acknowledgement_sha256(self) -> Sha256Digest:
        return self._audit_acknowledgement_sha256

    @property
    def global_halt_epoch(self) -> int:
        return self._global_halt_epoch

    @property
    def risk_halt_epoch(self) -> int:
        return self._risk_halt_epoch

    @property
    def instrument_gate_id(self) -> str:
        return self._instrument_gate_id

    @property
    def instrument_gate_version(self) -> int:
        return self._instrument_gate_version

    @property
    def authorization_state_version(self) -> int:
        return self._authorization_state_version


def _create_historical_submission_authorization_proof(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    order: Order,
    order_sha256: Sha256Digest,
    execution_request_sha256: Sha256Digest,
    causal_market_sha256: Sha256Digest,
    causal_root_key: RuntimeRootOrderKey,
    dispatch_sequence: int,
    audit_acknowledgement_id: str,
    audit_acknowledgement_sha256: Sha256Digest,
    global_halt_epoch: int,
    risk_halt_epoch: int,
    instrument_gate_id: str,
    instrument_gate_version: int,
    held_for_order_id: EconomicId,
    authorization_state_version: int,
    issuer: object,
) -> HistoricalSubmissionAuthorizationProof:
    if (
        type(run_id) is not RunId
        or type(spec_set) is not InstrumentExecutionSpecSet
        or type(execution_policy) is not ExecutionPolicyRef
        or type(order) is not Order
        or type(order_sha256) is not Sha256Digest
        or type(execution_request_sha256) is not Sha256Digest
        or type(causal_market_sha256) is not Sha256Digest
        or type(causal_root_key) is not RuntimeRootOrderKey
        or type(audit_acknowledgement_id) is not str
        or not audit_acknowledgement_id
        or type(audit_acknowledgement_sha256) is not Sha256Digest
        or type(instrument_gate_id) is not str
        or not instrument_gate_id
        or type(held_for_order_id) is not EconomicId
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization proof inputs are invalid")
    for value in (
        dispatch_sequence,
        order.portfolio_snapshot_version,
        order.risk_state_version,
        global_halt_epoch,
        risk_halt_epoch,
        instrument_gate_version,
        authorization_state_version,
    ):
        if type(value) is not int:
            raise _fail(OutcomeCode.INVALID_TYPE, "authorization versions must be exact int")
    if (
        not 1 <= dispatch_sequence <= _MAX_UINT64
        or not 0 <= order.portfolio_snapshot_version <= _MAX_UINT64
        or not 0 <= order.risk_state_version <= _MAX_UINT64
        or not 0 <= global_halt_epoch <= _MAX_UINT64
        or not 0 <= risk_halt_epoch <= _MAX_UINT64
        or not 1 <= instrument_gate_version <= _MAX_UINT64
        or not 0 <= authorization_state_version <= _MAX_UINT64
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "authorization versions are outside range")
    proof = object.__new__(HistoricalSubmissionAuthorizationProof)
    values: dict[str, object] = {
        "_run_id": run_id,
        "_instrument_spec_set_id": spec_set.identifier,
        "_instrument_spec_set_sha256": instrument_spec_set_digest(spec_set),
        "_execution_policy": execution_policy,
        "_order_id": order.order_id,
        "_order_sha256": order_sha256,
        "_execution_request_sha256": execution_request_sha256,
        "_causal_market_sha256": causal_market_sha256,
        "_causal_root_key": causal_root_key,
        "_dispatch_sequence": dispatch_sequence,
        "_audit_acknowledgement_id": audit_acknowledgement_id,
        "_audit_acknowledgement_sha256": audit_acknowledgement_sha256,
        "_portfolio_snapshot_version": order.portfolio_snapshot_version,
        "_risk_state_version": order.risk_state_version,
        "_global_halt_epoch": global_halt_epoch,
        "_risk_halt_epoch": risk_halt_epoch,
        "_instrument_gate_id": instrument_gate_id,
        "_instrument_gate_version": instrument_gate_version,
        "_held_for_order_id": held_for_order_id,
        "_authorization_state_version": authorization_state_version,
        "_issuer": issuer,
        "_seal": _AUTHORIZATION_PROOF_SEAL,
    }
    for name, proof_value in values.items():
        object.__setattr__(proof, name, proof_value)
    return proof


def _require_historical_submission_authorization_proof(
    proof: object,
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    order: Order,
    order_sha256: Sha256Digest,
    execution_request_sha256: Sha256Digest,
    causal_market_sha256: Sha256Digest,
    causal_root_key: RuntimeRootOrderKey,
    dispatch_sequence: int,
    issuer: object,
) -> HistoricalSubmissionAuthorizationProof:
    if type(proof) is not HistoricalSubmissionAuthorizationProof:
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization verifier returned non-exact proof")
    try:
        if (
            type(proof._run_id) is not RunId
            or type(proof._instrument_spec_set_id) is not InstrumentSpecSetId
            or type(proof._instrument_spec_set_sha256) is not Sha256Digest
            or type(proof._execution_policy) is not ExecutionPolicyRef
            or type(proof._order_id) is not EconomicId
            or type(proof._order_sha256) is not Sha256Digest
            or type(proof._execution_request_sha256) is not Sha256Digest
            or type(proof._causal_market_sha256) is not Sha256Digest
            or type(proof._causal_root_key) is not RuntimeRootOrderKey
            or type(proof._dispatch_sequence) is not int
            or type(proof._portfolio_snapshot_version) is not int
            or type(proof._risk_state_version) is not int
            or type(proof._held_for_order_id) is not EconomicId
            or type(proof._audit_acknowledgement_id) is not str
            or type(proof._audit_acknowledgement_sha256) is not Sha256Digest
            or type(proof._global_halt_epoch) is not int
            or type(proof._risk_halt_epoch) is not int
            or type(proof._instrument_gate_id) is not str
            or type(proof._instrument_gate_version) is not int
            or type(proof._authorization_state_version) is not int
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "authorization proof carriers must be exact")
        _validate_economic_id(
            proof._order_id,
            run_id=run_id,
            owner_kind=EconomicOwnerKind.EXECUTION_ORDER,
        )
        _validate_economic_id(
            proof._held_for_order_id,
            run_id=run_id,
            owner_kind=EconomicOwnerKind.EXECUTION_ORDER,
        )
        root_key_document = _runtime_key_document_from_key(proof._causal_root_key)
        if runtime_root_key_from_document(root_key_document) != proof._causal_root_key:
            raise _fail(OutcomeCode.CONFLICTING_ID, "authorization root key conflicts")
        matches = (
            proof._seal is _AUTHORIZATION_PROOF_SEAL
            and proof._issuer is issuer
            and proof._run_id == run_id
            and proof._instrument_spec_set_id == spec_set.identifier
            and proof._instrument_spec_set_sha256 == instrument_spec_set_digest(spec_set)
            and proof._execution_policy == execution_policy
            and proof._order_id == order.order_id
            and proof._order_sha256 == order_sha256
            and proof._execution_request_sha256 == execution_request_sha256
            and proof._causal_market_sha256 == causal_market_sha256
            and proof._causal_root_key == causal_root_key
            and proof._dispatch_sequence == dispatch_sequence
            and 1 <= proof._dispatch_sequence <= _MAX_UINT64
            and proof._portfolio_snapshot_version == order.portfolio_snapshot_version
            and 0 <= proof._portfolio_snapshot_version <= _MAX_UINT64
            and proof._risk_state_version == order.risk_state_version
            and 0 <= proof._risk_state_version <= _MAX_UINT64
            and proof._held_for_order_id == order.order_id
            and bool(proof._audit_acknowledgement_id)
            and 0 <= proof._global_halt_epoch <= _MAX_UINT64
            and 0 <= proof._risk_halt_epoch <= _MAX_UINT64
            and bool(proof._instrument_gate_id)
            and 1 <= proof._instrument_gate_version <= _MAX_UINT64
            and 0 <= proof._authorization_state_version <= _MAX_UINT64
        )
    except HistoricalMatcherError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization proof carriers are invalid") from error
    if not matches:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization proof conflicts")
    return proof


@final
@dataclass(frozen=True, slots=True, init=False)
class HistoricalSubmissionReceipt:
    run_id: RunId
    source_namespace: SourceNamespace
    submission_sequence: int
    order_id: EconomicId
    order_sha256: Sha256Digest
    execution_request_sha256: Sha256Digest
    client_submission_key: Sha256Digest
    instrument: Instrument
    side: OrderSide
    quantity_text: str
    causal_market_sha256: Sha256Digest
    causal_root_key: RuntimeRootOrderKey
    dispatch_sequence: int
    eligible_after_available_at: datetime
    audit_acknowledgement_id: str
    audit_acknowledgement_sha256: Sha256Digest
    global_halt_epoch: int
    risk_halt_epoch: int
    instrument_gate_id: str
    instrument_gate_version: int
    authorization_state_version: int
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    execution_policy: ExecutionPolicyRef

    def __init__(self) -> None:
        raise TypeError("submission receipts are created only by the matcher or decoder")

    @property
    def outcome_code(self) -> OutcomeCode:
        return OutcomeCode.SUBMISSION_SUBMITTED


@final
@dataclass(frozen=True, slots=True, init=False)
class HistoricalMatcherDispatchBatch:
    run_id: RunId
    source_namespace: SourceNamespace
    dispatch_kind: HistoricalDispatchKind
    dispatch_sequence: int
    trigger_root_sha256: Sha256Digest
    trigger_root_key: RuntimeRootOrderKey
    next_fact_sequence_before: int | None
    next_fact_sequence_after: int | None
    submission_sequences: tuple[int, ...]
    order_ids: tuple[EconomicId, ...]
    ingresses: tuple[ExecutionFactIngress, ...]
    ingress_sha256s: tuple[Sha256Digest, ...]

    def __init__(self) -> None:
        raise TypeError("dispatch batches are created only by the matcher or decoder")

    @property
    def ingress_identities(self) -> tuple[IngressIdentity, ...]:
        return tuple(ingress.identity for ingress in self.ingresses)


@final
@dataclass(frozen=True, slots=True, init=False)
class HistoricalMatcherDescendantBinding:
    ingress_identity: IngressIdentity
    ingress_sha256: Sha256Digest
    fact_sha256: Sha256Digest
    batch_sha256: Sha256Digest
    batch_index: int
    parent_kind: HistoricalDispatchKind
    parent_root_sha256: Sha256Digest
    parent_root_key: RuntimeRootOrderKey
    parent_dispatch_sequence: int

    def __init__(self) -> None:
        raise TypeError("descendant bindings are created only by the matcher")


@final
@dataclass(frozen=True, slots=True, init=False)
class HistoricalMatcherConflictEvidence:
    run_id: RunId
    conflict_kind: HistoricalMatcherConflictKind
    occupied_identity: Mapping[str, object] | None
    existing_sha256: Sha256Digest | None
    submitted_sha256: Sha256Digest | None
    submitted_dispatch_sequence: int | None
    last_successful_dispatch_sequence: int | None
    pending_count: int
    next_submission_sequence: int | None
    next_fact_sequence: int | None
    trigger_root_sha256: Sha256Digest | None

    def __init__(self) -> None:
        raise TypeError("matcher conflicts are created only by the matcher or decoder")


@final
@dataclass(frozen=True, slots=True, init=False)
class HistoricalMatcherState:
    run_id: RunId
    source_namespace: SourceNamespace
    provenance_id: FactProvenanceId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    execution_policy: ExecutionPolicyRef
    next_submission_sequence: int | None
    next_fact_sequence: int | None
    _submission_receipts: tuple[HistoricalSubmissionReceipt, ...] = field(repr=False)
    receipt_sha256s: tuple[Sha256Digest, ...]
    pending_order_ids: tuple[EconomicId, ...]
    issued_ingresses: tuple[ExecutionFactIngress, ...]
    _dispatch_batch_history_sha256: Sha256Digest = field(repr=False)
    _dispatch_ingress_history_sha256: Sha256Digest = field(repr=False)
    _last_dispatch_batch: HistoricalMatcherDispatchBatch | None = field(repr=False)
    dispatch_batch_sha256s: tuple[Sha256Digest, ...]
    last_new_dispatch_sequence: int | None
    ended: bool
    end_batch_sha256: Sha256Digest | None
    halted: bool
    conflict: HistoricalMatcherConflictEvidence | None

    def __init__(self) -> None:
        raise TypeError("matcher states are created only by the matcher or decoder")


def _require_positive_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact int")
    if not 1 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is outside positive uint64")
    return value


def _require_non_negative_uint64(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact int")
    if not 0 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is outside non-negative uint64")
    return value


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str")
    if not value:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must not be empty")
    return value


def _root_key_dispatch_kind(key: RuntimeRootOrderKey) -> HistoricalDispatchKind:
    if type(key) is not RuntimeRootOrderKey:
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime root key must be exact")
    if type(key._suffix) is _MarketDataSuffix:
        return HistoricalDispatchKind.MARKET
    if type(key._suffix) is _EndOfRunSuffix:
        return HistoricalDispatchKind.END_OF_RUN
    raise _fail(OutcomeCode.CONFLICTING_ID, "runtime root key suffix is unsupported")


def _validate_economic_id(
    value: object,
    *,
    run_id: RunId,
    owner_kind: EconomicOwnerKind | None = None,
) -> EconomicId:
    if (
        type(value) is not EconomicId
        or type(value.run_id) is not RunId
        or type(value.owner_kind) is not EconomicOwnerKind
        or type(value.owner_sequence) is not int
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "economic identity carriers must be exact")
    if (
        value.run_id != run_id
        or not 1 <= value.owner_sequence <= _MAX_UINT64
        or (owner_kind is not None and value.owner_kind is not owner_kind)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "economic identity binding conflicts")
    return value


def _validate_run_id(value: object) -> RunId:
    if type(value) is not RunId or type(value.value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "run ID carriers must be exact")
    try:
        owned = RunId(value.value)
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "run ID is invalid") from error
    if owned != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "run ID reconstruction conflicts")
    return value


def _validate_source_namespace(value: object) -> SourceNamespace:
    if type(value) is not SourceNamespace or type(value.value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "source namespace carriers must be exact")
    try:
        owned = SourceNamespace(value.value)
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "source namespace is invalid") from error
    if owned != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "source namespace reconstruction conflicts")
    return value


def _validate_digest(value: object, *, field_name: str) -> Sha256Digest:
    if type(value) is not Sha256Digest or type(value.value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} carriers must be exact")
    try:
        owned = Sha256Digest(value.value)
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is invalid") from error
    if owned != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} reconstruction conflicts")
    return value


def _validate_instrument(value: object) -> Instrument:
    if (
        type(value) is not Instrument
        or type(value.venue) is not VenueId
        or type(value.venue.code) is not str
        or type(value.symbol) is not str
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument carriers must be exact")
    try:
        owned = Instrument(VenueId(value.venue.code), value.symbol)
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "instrument is invalid") from error
    if owned != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument reconstruction conflicts")
    return value


def _validate_execution_policy(value: object) -> ExecutionPolicyRef:
    if (
        type(value) is not ExecutionPolicyRef
        or type(value.identifier) is not ExecutionPolicyId
        or type(value.identifier.value) is not str
        or type(value.sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "execution policy carriers must be exact")
    _validate_digest(value.sha256, field_name="execution policy digest")
    try:
        owned = ExecutionPolicyRef(
            ExecutionPolicyId(value.identifier.value),
            Sha256Digest(value.sha256.value),
        )
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "execution policy is invalid") from error
    if owned != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "execution policy reconstruction conflicts")
    return value


def _canonical_decimal_from_coefficient(coefficient: int, scale: int) -> CanonicalDecimal:
    negative = coefficient < 0
    digits = str(abs(coefficient))
    if coefficient == 0:
        return CanonicalDecimal("0")
    if scale:
        digits = digits.rjust(scale + 1, "0")
        text = f"{digits[:-scale]}.{digits[-scale:]}"
        while text.endswith("0"):
            text = text[:-1]
        if text.endswith("."):
            text = text[:-1]
    else:
        text = digits
    return CanonicalDecimal(("-" if negative else "") + text)


def _quantized_historical_close(
    close: object,
    *,
    side: OrderSide,
    specification: InstrumentExecutionSpec,
) -> CanonicalDecimal:
    if type(close) is not float:
        raise _fail(OutcomeCode.INVALID_TYPE, "Bar close must be exact float")
    if not isfinite(close):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "Bar close must be finite")
    c = specification.price_quantum.coefficient
    s = specification.price_quantum.scale
    if c <= 0:
        raise _fail(OutcomeCode.CONFLICTING_ID, "price quantum is invalid")
    p, q = close.as_integer_ratio()
    numerator = p * (10**s)
    denominator = q * c
    ticks, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and side is OrderSide.BUY):
        ticks += 1
    try:
        price = _canonical_decimal_from_coefficient(ticks * c, s)
        if specification.price_domain is PriceDomain.POSITIVE and price.coefficient <= 0:
            raise _fail(OutcomeCode.PRICE_DOMAIN, "quantized price must be positive")
        if specification.price_domain is PriceDomain.NON_NEGATIVE and price.coefficient < 0:
            raise _fail(OutcomeCode.PRICE_DOMAIN, "quantized price must be non-negative")
    except HistoricalMatcherError:
        raise
    except EconomicValidationError as error:
        code = (
            OutcomeCode.ARITHMETIC_OVERFLOW
            if error.code is OutcomeCode.OUT_OF_RANGE
            else error.code
        )
        if code not in {
            OutcomeCode.INVALID_TYPE,
            OutcomeCode.NOT_QUANTIZED,
            OutcomeCode.PRICE_DOMAIN,
            OutcomeCode.ARITHMETIC_OVERFLOW,
        }:
            code = OutcomeCode.CONFLICTING_ID
        raise _fail(code, "quantized close is invalid") from error
    return price


def _validate_runtime_root_key(
    value: object,
    *,
    expected_kind: HistoricalDispatchKind | None = None,
) -> RuntimeRootOrderKey:
    if type(value) is not RuntimeRootOrderKey:
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime root key must be exact")
    try:
        document = _runtime_key_document_from_key(value)
        owned = runtime_root_key_from_document(document)
    except HistoricalMatcherError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime root key carriers are invalid") from error
    if owned != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "runtime root key reconstruction conflicts")
    if expected_kind is not None and _root_key_dispatch_kind(owned) is not expected_kind:
        raise _fail(OutcomeCode.CONFLICTING_ID, "runtime root key kind conflicts")
    return value


def _next_sequence_after(before: int | None, count: int) -> int | None:
    _require_non_negative_uint64(count, field_name="sequence count")
    if count == 0:
        return before
    if before is None:
        raise _fail(OutcomeCode.ARITHMETIC_OVERFLOW, "sequence capacity is exhausted")
    _require_positive_uint64(before, field_name="sequence pointer")
    if count > _MAX_UINT64 - before + 1:
        raise _fail(OutcomeCode.ARITHMETIC_OVERFLOW, "sequence capacity is insufficient")
    candidate = before + count
    return None if candidate == _MAX_UINT64 + 1 else candidate


def _validate_receipt(receipt: HistoricalSubmissionReceipt) -> None:
    if (
        type(receipt.run_id) is not RunId
        or type(receipt.source_namespace) is not SourceNamespace
        or type(receipt.order_id) is not EconomicId
        or type(receipt.order_sha256) is not Sha256Digest
        or type(receipt.execution_request_sha256) is not Sha256Digest
        or type(receipt.client_submission_key) is not Sha256Digest
        or type(receipt.instrument) is not Instrument
        or type(receipt.side) is not OrderSide
        or type(receipt.quantity_text) is not str
        or type(receipt.causal_market_sha256) is not Sha256Digest
        or type(receipt.causal_root_key) is not RuntimeRootOrderKey
        or type(receipt.audit_acknowledgement_sha256) is not Sha256Digest
        or type(receipt.instrument_spec_set_id) is not InstrumentSpecSetId
        or type(receipt.instrument_spec_set_sha256) is not Sha256Digest
        or type(receipt.execution_policy) is not ExecutionPolicyRef
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "submission receipt carriers must be exact")
    _validate_run_id(receipt.run_id)
    _validate_source_namespace(receipt.source_namespace)
    for field_name, digest in (
        ("order digest", receipt.order_sha256),
        ("execution request digest", receipt.execution_request_sha256),
        ("client submission key", receipt.client_submission_key),
        ("causal market digest", receipt.causal_market_sha256),
        ("audit acknowledgement digest", receipt.audit_acknowledgement_sha256),
        ("instrument spec-set digest", receipt.instrument_spec_set_sha256),
    ):
        _validate_digest(digest, field_name=field_name)
    _validate_instrument(receipt.instrument)
    _validate_execution_policy(receipt.execution_policy)
    try:
        owned_spec_set_id = InstrumentSpecSetId(receipt.instrument_spec_set_id.value)
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "instrument spec-set ID is invalid") from error
    if owned_spec_set_id != receipt.instrument_spec_set_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument spec-set ID conflicts")
    _require_positive_uint64(receipt.submission_sequence, field_name="submission_sequence")
    _require_positive_uint64(receipt.dispatch_sequence, field_name="dispatch_sequence")
    _require_non_negative_uint64(receipt.global_halt_epoch, field_name="global_halt_epoch")
    _require_non_negative_uint64(receipt.risk_halt_epoch, field_name="risk_halt_epoch")
    _require_positive_uint64(
        receipt.instrument_gate_version,
        field_name="instrument_gate_version",
    )
    _require_non_negative_uint64(
        receipt.authorization_state_version,
        field_name="authorization_state_version",
    )
    _require_non_empty_string(
        receipt.audit_acknowledgement_id,
        field_name="audit_acknowledgement_id",
    )
    _require_non_empty_string(receipt.instrument_gate_id, field_name="instrument_gate_id")
    _utc_text(receipt.eligible_after_available_at)
    try:
        quantity = CanonicalDecimal(receipt.quantity_text)
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "receipt quantity is not canonical") from error
    if quantity.coefficient <= 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "receipt quantity must be positive")
    _validate_economic_id(
        receipt.order_id,
        run_id=receipt.run_id,
        owner_kind=EconomicOwnerKind.EXECUTION_ORDER,
    )
    _validate_runtime_root_key(
        receipt.causal_root_key,
        expected_kind=HistoricalDispatchKind.MARKET,
    )
    suffix = receipt.causal_root_key._suffix
    if (
        type(suffix) is not _MarketDataSuffix
        or receipt.causal_root_key.available_at != receipt.eligible_after_available_at
        or suffix.instrument_venue != receipt.instrument.venue.code
        or suffix.instrument_symbol != receipt.instrument.symbol
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "submission receipt bindings conflict")


def _validate_batch(batch: HistoricalMatcherDispatchBatch) -> None:
    if (
        type(batch.run_id) is not RunId
        or type(batch.source_namespace) is not SourceNamespace
        or type(batch.dispatch_kind) is not HistoricalDispatchKind
        or type(batch.trigger_root_sha256) is not Sha256Digest
        or type(batch.trigger_root_key) is not RuntimeRootOrderKey
        or type(batch.submission_sequences) is not tuple
        or type(batch.order_ids) is not tuple
        or type(batch.ingresses) is not tuple
        or type(batch.ingress_sha256s) is not tuple
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatch batch carriers must be exact")
    _validate_run_id(batch.run_id)
    _validate_source_namespace(batch.source_namespace)
    _validate_digest(batch.trigger_root_sha256, field_name="trigger root digest")
    _validate_runtime_root_key(batch.trigger_root_key, expected_kind=batch.dispatch_kind)
    _require_positive_uint64(batch.dispatch_sequence, field_name="dispatch_sequence")
    before = (
        None
        if batch.next_fact_sequence_before is None
        else _require_positive_uint64(
            batch.next_fact_sequence_before,
            field_name="next_fact_sequence_before",
        )
    )
    after = (
        None
        if batch.next_fact_sequence_after is None
        else _require_positive_uint64(
            batch.next_fact_sequence_after,
            field_name="next_fact_sequence_after",
        )
    )
    if not (
        len(batch.ingresses)
        == len(batch.ingress_sha256s)
        == len(batch.order_ids)
        == len(batch.submission_sequences)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch arrays are not aligned")
    if _next_sequence_after(before, len(batch.ingresses)) != after:
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch fact sequence pointers conflict")
    if len(set(batch.order_ids)) != len(batch.order_ids):
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch Order IDs duplicate")
    expected_kind = (
        ExecutionFactKind.TRADE
        if batch.dispatch_kind is HistoricalDispatchKind.MARKET
        else ExecutionFactKind.EXPIRY
    )
    previous_submission = 0
    for index, (submission, order_id, ingress, digest) in enumerate(
        zip(
            batch.submission_sequences,
            batch.order_ids,
            batch.ingresses,
            batch.ingress_sha256s,
            strict=True,
        )
    ):
        _require_positive_uint64(submission, field_name="submission_sequence")
        if submission <= previous_submission:
            raise _fail(OutcomeCode.CONFLICTING_ID, "batch submissions are not ordered")
        previous_submission = submission
        _validate_economic_id(
            order_id,
            run_id=batch.run_id,
            owner_kind=EconomicOwnerKind.EXECUTION_ORDER,
        )
        if type(ingress) is not ExecutionFactIngress or type(digest) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "batch entry carriers must be exact")
        _validate_digest(digest, field_name="ingress digest")
        expected_sequence = cast(int, before) + index
        fact = ingress.fact
        if (
            ingress.source_namespace != batch.source_namespace
            or ingress.ingress_sequence != expected_sequence
            or execution_fact_ingress_digest(ingress) != digest
            or fact.source_namespace != batch.source_namespace
            or type(fact.dedup_identity) is not SourceNativeSequence
            or fact.dedup_identity.value != expected_sequence
            or fact.kind is not expected_kind
            or fact.order_id != order_id
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "batch entry evidence conflicts")
        canonical_execution_fact_ingress_bytes(ingress)
        canonical_execution_fact_bytes(fact)


def _validate_descendant_binding(binding: HistoricalMatcherDescendantBinding) -> None:
    if (
        type(binding.ingress_identity) is not IngressIdentity
        or type(binding.ingress_sha256) is not Sha256Digest
        or type(binding.fact_sha256) is not Sha256Digest
        or type(binding.batch_sha256) is not Sha256Digest
        or type(binding.parent_kind) is not HistoricalDispatchKind
        or type(binding.parent_root_sha256) is not Sha256Digest
        or type(binding.parent_root_key) is not RuntimeRootOrderKey
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "descendant binding carriers must be exact")
    _validate_source_namespace(binding.ingress_identity.source_namespace)
    for field_name, digest in (
        ("ingress digest", binding.ingress_sha256),
        ("fact digest", binding.fact_sha256),
        ("batch digest", binding.batch_sha256),
        ("parent root digest", binding.parent_root_sha256),
    ):
        _validate_digest(digest, field_name=field_name)
    _require_non_negative_uint64(
        binding.ingress_identity.ingress_sequence,
        field_name="ingress_sequence",
    )
    _require_non_negative_uint64(binding.batch_index, field_name="batch_index")
    _require_positive_uint64(
        binding.parent_dispatch_sequence,
        field_name="parent_dispatch_sequence",
    )
    _validate_runtime_root_key(binding.parent_root_key, expected_kind=binding.parent_kind)


def _validate_conflict(conflict: HistoricalMatcherConflictEvidence) -> None:
    if (
        type(conflict.run_id) is not RunId
        or type(conflict.conflict_kind) is not HistoricalMatcherConflictKind
        or (
            conflict.existing_sha256 is not None
            and type(conflict.existing_sha256) is not Sha256Digest
        )
        or (
            conflict.submitted_sha256 is not None
            and type(conflict.submitted_sha256) is not Sha256Digest
        )
        or (
            conflict.trigger_root_sha256 is not None
            and type(conflict.trigger_root_sha256) is not Sha256Digest
        )
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict carriers must be exact")
    _validate_run_id(conflict.run_id)
    for field_name, digest in (
        ("existing digest", conflict.existing_sha256),
        ("submitted digest", conflict.submitted_sha256),
        ("trigger root digest", conflict.trigger_root_sha256),
    ):
        if digest is not None:
            _validate_digest(digest, field_name=field_name)
    for field_name, value in (
        ("submitted_dispatch_sequence", conflict.submitted_dispatch_sequence),
        ("last_successful_dispatch_sequence", conflict.last_successful_dispatch_sequence),
        ("next_submission_sequence", conflict.next_submission_sequence),
        ("next_fact_sequence", conflict.next_fact_sequence),
    ):
        if value is not None:
            _require_positive_uint64(value, field_name=field_name)
    _require_non_negative_uint64(conflict.pending_count, field_name="pending_count")
    immutable_occupied_identity(
        conflict.occupied_identity,
        conflict_kind=conflict.conflict_kind,
    )


def _validate_state(state: HistoricalMatcherState) -> None:
    if (
        type(state.run_id) is not RunId
        or type(state.source_namespace) is not SourceNamespace
        or type(state.provenance_id) is not FactProvenanceId
        or type(state.instrument_spec_set_id) is not InstrumentSpecSetId
        or type(state.instrument_spec_set_sha256) is not Sha256Digest
        or type(state.execution_policy) is not ExecutionPolicyRef
        or type(state._submission_receipts) is not tuple
        or type(state.receipt_sha256s) is not tuple
        or type(state.pending_order_ids) is not tuple
        or type(state.issued_ingresses) is not tuple
        or type(state._dispatch_batch_history_sha256) is not Sha256Digest
        or type(state._dispatch_ingress_history_sha256) is not Sha256Digest
        or type(state.dispatch_batch_sha256s) is not tuple
        or type(state.ended) is not bool
        or type(state.halted) is not bool
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "matcher state carriers must be exact")
    _validate_run_id(state.run_id)
    _validate_source_namespace(state.source_namespace)
    _validate_execution_policy(state.execution_policy)
    _validate_digest(state.instrument_spec_set_sha256, field_name="instrument spec-set digest")
    try:
        FactProvenanceId(state.provenance_id.value)
        InstrumentSpecSetId(state.instrument_spec_set_id.value)
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "state lineage binding is invalid") from error
    for field_name, value in (
        ("next_submission_sequence", state.next_submission_sequence),
        ("next_fact_sequence", state.next_fact_sequence),
        ("last_new_dispatch_sequence", state.last_new_dispatch_sequence),
    ):
        if value is not None:
            _require_positive_uint64(value, field_name=field_name)
    if any(type(value) is not Sha256Digest for value in state.receipt_sha256s):
        raise _fail(OutcomeCode.INVALID_TYPE, "state receipt digests must be exact")
    if any(type(value) is not Sha256Digest for value in state.dispatch_batch_sha256s):
        raise _fail(OutcomeCode.INVALID_TYPE, "state batch digests must be exact")
    for digest in (*state.receipt_sha256s, *state.dispatch_batch_sha256s):
        _validate_digest(digest, field_name="state digest")
    _validate_digest(
        state._dispatch_batch_history_sha256,
        field_name="state batch-history digest",
    )
    _validate_digest(
        state._dispatch_ingress_history_sha256,
        field_name="state batch/ingress-history digest",
    )
    if len(state._submission_receipts) != len(state.receipt_sha256s):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt evidence is not aligned")
    receipt_order_ids: list[EconomicId] = []
    for sequence, (receipt, digest) in enumerate(
        zip(state._submission_receipts, state.receipt_sha256s, strict=True),
        start=1,
    ):
        if type(receipt) is not HistoricalSubmissionReceipt:
            raise _fail(OutcomeCode.INVALID_TYPE, "state receipt evidence must be exact")
        _validate_receipt(receipt)
        if (
            receipt.run_id != state.run_id
            or receipt.source_namespace != state.source_namespace
            or receipt.submission_sequence != sequence
            or historical_submission_receipt_digest(receipt) != digest
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt evidence conflicts")
        receipt_order_ids.append(receipt.order_id)
    if len(set(receipt_order_ids)) != len(receipt_order_ids):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt Order IDs duplicate")
    for order_id in state.pending_order_ids:
        _validate_economic_id(
            order_id,
            run_id=state.run_id,
            owner_kind=EconomicOwnerKind.EXECUTION_ORDER,
        )
    if any(type(value) is not ExecutionFactIngress for value in state.issued_ingresses):
        raise _fail(OutcomeCode.INVALID_TYPE, "state ingresses must be exact")
    if len(set(state.receipt_sha256s)) != len(state.receipt_sha256s):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt digests duplicate")
    if len(set(state.dispatch_batch_sha256s)) != len(state.dispatch_batch_sha256s):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state batch digests duplicate")
    if (
        _dispatch_batch_history_digest(state.dispatch_batch_sha256s)
        != state._dispatch_batch_history_sha256
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state batch digest history conflicts")
    if (
        _dispatch_ingress_history_digest(
            state.dispatch_batch_sha256s,
            state.issued_ingresses,
        )
        != state._dispatch_ingress_history_sha256
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state batch/ingress history conflicts")
    if state._last_dispatch_batch is not None:
        if type(state._last_dispatch_batch) is not HistoricalMatcherDispatchBatch:
            raise _fail(OutcomeCode.INVALID_TYPE, "state final batch evidence must be exact")
        _validate_batch(state._last_dispatch_batch)
        if (
            not state.dispatch_batch_sha256s
            or state._last_dispatch_batch.run_id != state.run_id
            or state._last_dispatch_batch.source_namespace != state.source_namespace
            or historical_matcher_dispatch_batch_digest(state._last_dispatch_batch)
            != state.dispatch_batch_sha256s[-1]
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state final batch evidence conflicts")
    if len(set(state.pending_order_ids)) != len(state.pending_order_ids):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state pending Order IDs duplicate")
    if _next_sequence_after(1, len(state.receipt_sha256s)) != state.next_submission_sequence:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state submission sequence conflicts")
    if _next_sequence_after(1, len(state.issued_ingresses)) != state.next_fact_sequence:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state fact sequence conflicts")
    issued_order_ids: list[EconomicId] = []
    for sequence, ingress in enumerate(state.issued_ingresses, start=1):
        if (
            ingress.identity != IngressIdentity(state.source_namespace, sequence)
            or type(ingress.fact.dedup_identity) is not SourceNativeSequence
            or ingress.fact.dedup_identity.value != sequence
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state ingress sequence conflicts")
        issued_order_id = ingress.fact.order_id
        if type(issued_order_id) is not EconomicId:
            raise _fail(OutcomeCode.CONFLICTING_ID, "state ingress Order identity conflicts")
        _validate_economic_id(
            issued_order_id,
            run_id=state.run_id,
            owner_kind=EconomicOwnerKind.EXECUTION_ORDER,
        )
        issued_order_ids.append(issued_order_id)
        canonical_execution_fact_ingress_bytes(ingress)
        canonical_execution_fact_bytes(ingress.fact)
    if len(state.receipt_sha256s) != len(state.pending_order_ids) + len(issued_order_ids):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt partition conflicts")
    if len(set(issued_order_ids)) != len(issued_order_ids):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state issued Order IDs duplicate")
    if set(state.pending_order_ids).intersection(issued_order_ids):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state Order is both pending and issued")
    receipt_order_id_set = set(receipt_order_ids)
    if set(state.pending_order_ids).union(issued_order_ids) != receipt_order_id_set:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt membership conflicts")
    issued_order_id_set = set(issued_order_ids)
    expected_pending_order_ids = tuple(
        order_id for order_id in receipt_order_ids if order_id not in issued_order_id_set
    )
    if state.pending_order_ids != expected_pending_order_ids:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state pending Order order conflicts")
    if state.halted != (state.conflict is not None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state halt/conflict relationship conflicts")
    if state.conflict is not None:
        _validate_conflict(state.conflict)
        if state.conflict.run_id != state.run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "state conflict run binding conflicts")
        if (
            state.conflict.pending_count != len(state.pending_order_ids)
            or state.conflict.next_submission_sequence != state.next_submission_sequence
            or state.conflict.next_fact_sequence != state.next_fact_sequence
            or state.conflict.last_successful_dispatch_sequence != state.last_new_dispatch_sequence
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state conflict snapshot conflicts")
    if state.ended != (state.end_batch_sha256 is not None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state end relationship conflicts")
    if state.ended and state.pending_order_ids:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ended state retains pending Orders")
    if state.end_batch_sha256 is not None and (
        type(state.end_batch_sha256) is not Sha256Digest
        or not state.dispatch_batch_sha256s
        or state.end_batch_sha256 != state.dispatch_batch_sha256s[-1]
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state end batch conflicts")
    if bool(state.dispatch_batch_sha256s) != (state.last_new_dispatch_sequence is not None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state last dispatch relationship conflicts")
    expected_last_dispatch = (
        None if state._last_dispatch_batch is None else state._last_dispatch_batch.dispatch_sequence
    )
    if state.last_new_dispatch_sequence != expected_last_dispatch:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state last dispatch conflicts")


def _new_immutable(value_type: type[object], values: Mapping[str, object]) -> object:
    fields = getattr(value_type, "__dataclass_fields__", None)
    if type(fields) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "immutable matcher value type is invalid")
    expected = set(fields)
    if set(values) != expected:
        raise _fail(OutcomeCode.CONFLICTING_ID, "immutable matcher value fields conflict")
    value = object.__new__(value_type)
    for name, item in values.items():
        object.__setattr__(value, name, item)
    return value


def _create_historical_submission_receipt(
    **values: object,
) -> HistoricalSubmissionReceipt:
    receipt = cast(
        HistoricalSubmissionReceipt,
        _new_immutable(HistoricalSubmissionReceipt, values),
    )
    _validate_receipt(receipt)
    return receipt


def _create_historical_matcher_dispatch_batch(
    **values: object,
) -> HistoricalMatcherDispatchBatch:
    batch = cast(
        HistoricalMatcherDispatchBatch,
        _new_immutable(HistoricalMatcherDispatchBatch, values),
    )
    _validate_batch(batch)
    return batch


def _create_historical_matcher_descendant_binding(
    **values: object,
) -> HistoricalMatcherDescendantBinding:
    binding = cast(
        HistoricalMatcherDescendantBinding,
        _new_immutable(HistoricalMatcherDescendantBinding, values),
    )
    _validate_descendant_binding(binding)
    return binding


def _create_historical_matcher_conflict(
    **values: object,
) -> HistoricalMatcherConflictEvidence:
    kind = values.get("conflict_kind")
    if type(kind) is not HistoricalMatcherConflictKind:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict kind must be exact")
    values = dict(values)
    values["occupied_identity"] = immutable_occupied_identity(
        cast(Mapping[str, object] | None, values.get("occupied_identity")),
        conflict_kind=kind,
    )
    conflict = cast(
        HistoricalMatcherConflictEvidence,
        _new_immutable(HistoricalMatcherConflictEvidence, values),
    )
    _validate_conflict(conflict)
    return conflict


def _create_historical_matcher_state(**values: object) -> HistoricalMatcherState:
    state = cast(
        HistoricalMatcherState,
        _new_immutable(HistoricalMatcherState, values),
    )
    _validate_state(state)
    return state


@final
@dataclass(frozen=True, slots=True)
class HistoricalMatcherDecodeContext:
    """Closed run/binding context and canonical nested-value registries."""

    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    execution_policy: ExecutionPolicyRef
    source_namespace: SourceNamespace
    provenance_id: FactProvenanceId
    orders_by_sha256: Mapping[Sha256Digest, Order] = field(default_factory=dict)
    receipts_by_sha256: Mapping[Sha256Digest, HistoricalSubmissionReceipt] = field(
        default_factory=dict
    )
    batches_by_sha256: Mapping[Sha256Digest, HistoricalMatcherDispatchBatch] = field(
        default_factory=dict
    )
    ingresses_by_sha256: Mapping[Sha256Digest, ExecutionFactIngress] = field(default_factory=dict)
    conflicts_by_sha256: Mapping[Sha256Digest, HistoricalMatcherConflictEvidence] = field(
        default_factory=dict
    )
    market_roots_by_sha256: Mapping[Sha256Digest, MarketDataEnvelope] = field(default_factory=dict)
    end_roots_by_sha256: Mapping[Sha256Digest, EndOfRunRoot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not RunId
            or type(self.spec_set) is not InstrumentExecutionSpecSet
            or type(self.execution_policy) is not ExecutionPolicyRef
            or type(self.source_namespace) is not SourceNamespace
            or type(self.provenance_id) is not FactProvenanceId
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "matcher decode bindings must be exact")
        registries = (
            ("orders_by_sha256", self.orders_by_sha256, Order),
            (
                "receipts_by_sha256",
                self.receipts_by_sha256,
                HistoricalSubmissionReceipt,
            ),
            (
                "batches_by_sha256",
                self.batches_by_sha256,
                HistoricalMatcherDispatchBatch,
            ),
            ("ingresses_by_sha256", self.ingresses_by_sha256, ExecutionFactIngress),
            (
                "conflicts_by_sha256",
                self.conflicts_by_sha256,
                HistoricalMatcherConflictEvidence,
            ),
            (
                "market_roots_by_sha256",
                self.market_roots_by_sha256,
                MarketDataEnvelope,
            ),
            ("end_roots_by_sha256", self.end_roots_by_sha256, EndOfRunRoot),
        )
        for name, registry, value_type in registries:
            if not isinstance(registry, Mapping):
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "matcher decode registries must be read-only mappings",
                )
            if any(
                type(key) is not Sha256Digest or type(value) is not value_type
                for key, value in registry.items()
            ):
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "matcher decode registry entries must have exact types",
                )
            object.__setattr__(self, name, MappingProxyType(dict(registry)))
        digest_functions: tuple[
            tuple[Mapping[Sha256Digest, Any], Callable[[Any], Sha256Digest]],
            ...,
        ] = (
            (self.orders_by_sha256, cast(Callable[[Any], Sha256Digest], order_digest)),
            (
                self.receipts_by_sha256,
                cast(Callable[[Any], Sha256Digest], historical_submission_receipt_digest),
            ),
            (
                self.batches_by_sha256,
                cast(Callable[[Any], Sha256Digest], historical_matcher_dispatch_batch_digest),
            ),
            (
                self.ingresses_by_sha256,
                cast(Callable[[Any], Sha256Digest], _execution_fact_ingress_digest),
            ),
            (
                self.conflicts_by_sha256,
                cast(Callable[[Any], Sha256Digest], historical_matcher_conflict_digest),
            ),
            (
                self.market_roots_by_sha256,
                cast(Callable[[Any], Sha256Digest], historical_market_root_digest),
            ),
            (
                self.end_roots_by_sha256,
                cast(Callable[[Any], Sha256Digest], historical_end_root_digest),
            ),
        )
        for registry, digest_function in digest_functions:
            try:
                matches = all(digest_function(value) == key for key, value in registry.items())
            except (AttributeError, TypeError, ValueError) as error:
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "matcher decode registry value cannot be canonicalized",
                ) from error
            if not matches:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "matcher decode registry digest conflicts",
                )


def _receipt_document(receipt: HistoricalSubmissionReceipt) -> dict[str, object]:
    if type(receipt) is not HistoricalSubmissionReceipt:
        raise _fail(OutcomeCode.INVALID_TYPE, "receipt must be exact")
    _validate_receipt(receipt)
    return {
        "audit_acknowledgement_id": receipt.audit_acknowledgement_id,
        "audit_acknowledgement_sha256": receipt.audit_acknowledgement_sha256.value,
        "authorization_state_version": receipt.authorization_state_version,
        "canonicalization": HISTORICAL_SUBMISSION_RECEIPT_CANONICALIZATION,
        "causal_market_sha256": receipt.causal_market_sha256.value,
        "causal_root_key": _runtime_key_document_from_key(receipt.causal_root_key),
        "client_submission_key": receipt.client_submission_key.value,
        "dispatch_sequence": receipt.dispatch_sequence,
        "eligible_after_available_at": _utc_text(receipt.eligible_after_available_at),
        "execution_policy_id": receipt.execution_policy.identifier.value,
        "execution_policy_sha256": receipt.execution_policy.sha256.value,
        "execution_request_sha256": receipt.execution_request_sha256.value,
        "global_halt_epoch": receipt.global_halt_epoch,
        "instrument": _instrument_document(receipt.instrument),
        "instrument_gate_id": receipt.instrument_gate_id,
        "instrument_gate_version": receipt.instrument_gate_version,
        "instrument_spec_set_id": receipt.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": receipt.instrument_spec_set_sha256.value,
        "message_type": "historical_submission_receipt",
        "order_id": _economic_id_document(receipt.order_id),
        "order_sha256": receipt.order_sha256.value,
        "outcome_code": receipt.outcome_code.value,
        "quantity": receipt.quantity_text,
        "risk_halt_epoch": receipt.risk_halt_epoch,
        "run_id": receipt.run_id.value,
        "schema_version": HISTORICAL_MATCHER_SCHEMA_VERSION,
        "side": receipt.side.value,
        "source_namespace": receipt.source_namespace.value,
        "submission_sequence": receipt.submission_sequence,
    }


def _runtime_key_document_from_key(key: RuntimeRootOrderKey) -> dict[str, object]:
    if type(key) is not RuntimeRootOrderKey:
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime key must be exact")
    suffix = key._suffix
    if type(suffix) is _MarketDataSuffix:
        return {
            "adjustment": suffix.adjustment,
            "available_at": _utc_text(key.available_at),
            "domain_rank": key.domain_rank,
            "event_time": _utc_text(suffix.event_time),
            "instrument": {
                "symbol": suffix.instrument_symbol,
                "venue": suffix.instrument_venue,
            },
            "interval_end": _utc_text(suffix.interval_end),
            "interval_start": _utc_text(suffix.interval_start),
            "kind_rank": suffix.kind_rank,
            "revision": suffix.revision,
            "root_domain": RuntimeRootDomain.MARKET_DATA.value,
            "source": suffix.source_code,
            "source_sequence": suffix.source_sequence,
        }
    if type(suffix) is _EndOfRunSuffix:
        return {
            "available_at": _utc_text(key.available_at),
            "domain_rank": key.domain_rank,
            "kind": next(
                kind.value
                for kind, rank in END_OF_RUN_KIND_RANKS.items()
                if rank == suffix.kind_rank
            ),
            "kind_rank": suffix.kind_rank,
            "producer_namespace": suffix.producer_namespace,
            "producer_sequence": suffix.producer_sequence,
            "root_domain": RuntimeRootDomain.END_OF_RUN.value,
            "run_id": suffix.run_id,
        }
    raise _fail(OutcomeCode.CONFLICTING_ID, "matcher key has unsupported suffix")


def canonical_historical_submission_receipt_bytes(
    receipt: HistoricalSubmissionReceipt,
) -> bytes:
    return _canonical_json(_receipt_document(receipt))


def historical_submission_receipt_digest(
    receipt: HistoricalSubmissionReceipt,
) -> Sha256Digest:
    return _framed_digest(
        HISTORICAL_SUBMISSION_RECEIPT_DIGEST_DOMAIN,
        canonical_historical_submission_receipt_bytes(receipt),
    )


def _batch_document(batch: HistoricalMatcherDispatchBatch) -> dict[str, object]:
    if type(batch) is not HistoricalMatcherDispatchBatch:
        raise _fail(OutcomeCode.INVALID_TYPE, "batch must be exact")
    _validate_batch(batch)
    return {
        "canonicalization": HISTORICAL_MATCHER_DISPATCH_BATCH_CANONICALIZATION,
        "dispatch_kind": batch.dispatch_kind.value,
        "dispatch_sequence": batch.dispatch_sequence,
        "ingresses": [
            {
                "ingress_identity": _ingress_identity_document(ingress.identity),
                "ingress_sha256": digest.value,
            }
            for ingress, digest in zip(
                batch.ingresses,
                batch.ingress_sha256s,
                strict=True,
            )
        ],
        "message_type": "historical_matcher_dispatch_batch",
        "next_fact_sequence_after": batch.next_fact_sequence_after,
        "next_fact_sequence_before": batch.next_fact_sequence_before,
        "order_ids": [_economic_id_document(value) for value in batch.order_ids],
        "run_id": batch.run_id.value,
        "schema_version": HISTORICAL_MATCHER_SCHEMA_VERSION,
        "source_namespace": batch.source_namespace.value,
        "submission_sequences": list(batch.submission_sequences),
        "trigger_root_key": _runtime_key_document_from_key(batch.trigger_root_key),
        "trigger_root_sha256": batch.trigger_root_sha256.value,
    }


def canonical_historical_matcher_dispatch_batch_bytes(
    batch: HistoricalMatcherDispatchBatch,
) -> bytes:
    return _canonical_json(_batch_document(batch))


def historical_matcher_dispatch_batch_digest(
    batch: HistoricalMatcherDispatchBatch,
) -> Sha256Digest:
    return _framed_digest(
        HISTORICAL_MATCHER_DISPATCH_BATCH_DIGEST_DOMAIN,
        canonical_historical_matcher_dispatch_batch_bytes(batch),
    )


def _dispatch_batch_history_digest(
    digests: tuple[Sha256Digest, ...],
) -> Sha256Digest:
    if type(digests) is not tuple or any(type(value) is not Sha256Digest for value in digests):
        raise _fail(OutcomeCode.INVALID_TYPE, "batch-history digests must be exact")
    for digest in digests:
        _validate_digest(digest, field_name="batch-history digest")
    return _framed_digest(
        _HISTORICAL_MATCHER_DISPATCH_HISTORY_DIGEST_DOMAIN,
        _canonical_json([digest.value for digest in digests]),
    )


def _dispatch_ingress_history_digest(
    batch_digests: tuple[Sha256Digest, ...],
    ingresses: tuple[ExecutionFactIngress, ...],
) -> Sha256Digest:
    if type(batch_digests) is not tuple or any(
        type(value) is not Sha256Digest for value in batch_digests
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "batch/ingress-history digests must be exact")
    if type(ingresses) is not tuple or any(
        type(value) is not ExecutionFactIngress for value in ingresses
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "batch/ingress-history ingresses must be exact")
    for digest in batch_digests:
        _validate_digest(digest, field_name="batch/ingress-history batch digest")
    ingress_digests = tuple(_execution_fact_ingress_digest(ingress) for ingress in ingresses)
    return _framed_digest(
        _HISTORICAL_MATCHER_DISPATCH_INGRESS_HISTORY_DIGEST_DOMAIN,
        _canonical_json(
            {
                "dispatch_batch_sha256s": [digest.value for digest in batch_digests],
                "issued_ingress_sha256s": [digest.value for digest in ingress_digests],
            }
        ),
    )


def canonical_historical_matcher_state_bytes(state: HistoricalMatcherState) -> bytes:
    if type(state) is not HistoricalMatcherState:
        raise _fail(OutcomeCode.INVALID_TYPE, "state must be exact")
    _validate_state(state)
    document = {
        "canonicalization": HISTORICAL_MATCHER_STATE_CANONICALIZATION,
        "conflict_sha256": (
            None
            if state.conflict is None
            else historical_matcher_conflict_digest(state.conflict).value
        ),
        "dispatch_batch_sha256s": [digest.value for digest in state.dispatch_batch_sha256s],
        "end_batch_sha256": (
            None if state.end_batch_sha256 is None else state.end_batch_sha256.value
        ),
        "ended": state.ended,
        "execution_policy_id": state.execution_policy.identifier.value,
        "execution_policy_sha256": state.execution_policy.sha256.value,
        "halted": state.halted,
        "instrument_spec_set_id": state.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": state.instrument_spec_set_sha256.value,
        "issued_ingresses": [
            {
                "ingress_identity": _ingress_identity_document(ingress.identity),
                "ingress_sha256": _execution_fact_ingress_digest(ingress).value,
            }
            for ingress in state.issued_ingresses
        ],
        "last_new_dispatch_sequence": state.last_new_dispatch_sequence,
        "message_type": "historical_matcher_state",
        "next_fact_sequence": state.next_fact_sequence,
        "next_submission_sequence": state.next_submission_sequence,
        "pending_order_ids": [_economic_id_document(value) for value in state.pending_order_ids],
        "provenance_id": state.provenance_id.value,
        "receipt_sha256s": [digest.value for digest in state.receipt_sha256s],
        "run_id": state.run_id.value,
        "schema_version": HISTORICAL_MATCHER_SCHEMA_VERSION,
        "source_namespace": state.source_namespace.value,
    }
    return _canonical_json(document)


def historical_matcher_state_digest(state: HistoricalMatcherState) -> Sha256Digest:
    return _framed_digest(
        HISTORICAL_MATCHER_STATE_DIGEST_DOMAIN,
        canonical_historical_matcher_state_bytes(state),
    )


def _execution_fact_ingress_digest(ingress: ExecutionFactIngress) -> Sha256Digest:
    from ea.core.execution_messages import execution_fact_ingress_digest

    return execution_fact_ingress_digest(ingress)


def canonical_historical_matcher_conflict_bytes(
    conflict: HistoricalMatcherConflictEvidence,
) -> bytes:
    if type(conflict) is not HistoricalMatcherConflictEvidence:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict must be exact")
    _validate_conflict(conflict)
    document = {
        "canonicalization": HISTORICAL_MATCHER_CONFLICT_CANONICALIZATION,
        "conflict_kind": conflict.conflict_kind.value,
        "existing_sha256": (
            None if conflict.existing_sha256 is None else conflict.existing_sha256.value
        ),
        "last_successful_dispatch_sequence": conflict.last_successful_dispatch_sequence,
        "message_type": "historical_matcher_conflict",
        "next_fact_sequence": conflict.next_fact_sequence,
        "next_submission_sequence": conflict.next_submission_sequence,
        "occupied_identity": _occupied_identity_document(
            conflict.occupied_identity,
            conflict_kind=conflict.conflict_kind,
        ),
        "pending_count": conflict.pending_count,
        "run_id": conflict.run_id.value,
        "schema_version": HISTORICAL_MATCHER_SCHEMA_VERSION,
        "submitted_dispatch_sequence": conflict.submitted_dispatch_sequence,
        "submitted_sha256": (
            None if conflict.submitted_sha256 is None else conflict.submitted_sha256.value
        ),
        "trigger_root_sha256": (
            None if conflict.trigger_root_sha256 is None else conflict.trigger_root_sha256.value
        ),
    }
    return _canonical_json(document)


def historical_matcher_conflict_digest(
    conflict: HistoricalMatcherConflictEvidence,
) -> Sha256Digest:
    return _framed_digest(
        HISTORICAL_MATCHER_CONFLICT_DIGEST_DOMAIN,
        canonical_historical_matcher_conflict_bytes(conflict),
    )


def canonical_historical_matcher_observation_bytes(
    *,
    fact_sequence: int,
    fact_kind: str,
    source_namespace: SourceNamespace,
    provenance_id: FactProvenanceId,
    submission_receipt_sha256: Sha256Digest,
    submission_receipt: HistoricalSubmissionReceipt,
    order_sha256: Sha256Digest,
    order: Order,
    trigger_root_kind: HistoricalDispatchKind,
    trigger_root_sha256: Sha256Digest,
    trigger_root_key: RuntimeRootOrderKey,
    trigger_root: MarketDataEnvelope | EndOfRunRoot,
    trigger_dispatch_sequence: int,
    occurred_at: object,
    available_at: object,
    instrument: Instrument,
    side: OrderSide,
    quantity_text: str,
    price_text: str | None,
    expiry_outcome_code: OutcomeCode | None,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
) -> bytes:
    if (
        type(fact_sequence) is not int
        or not 1 <= fact_sequence <= _MAX_UINT64
        or type(fact_kind) is not str
        or type(source_namespace) is not SourceNamespace
        or type(provenance_id) is not FactProvenanceId
        or type(submission_receipt_sha256) is not Sha256Digest
        or type(submission_receipt) is not HistoricalSubmissionReceipt
        or type(order_sha256) is not Sha256Digest
        or type(order) is not Order
        or type(trigger_root_kind) is not HistoricalDispatchKind
        or type(trigger_root_sha256) is not Sha256Digest
        or type(trigger_root_key) is not RuntimeRootOrderKey
        or type(trigger_dispatch_sequence) is not int
        or not 1 <= trigger_dispatch_sequence <= _MAX_UINT64
        or type(instrument) is not Instrument
        or type(side) is not OrderSide
        or type(quantity_text) is not str
        or type(spec_set) is not InstrumentExecutionSpecSet
        or type(execution_policy) is not ExecutionPolicyRef
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "matcher observation inputs are invalid")
    _validate_source_namespace(source_namespace)
    try:
        if FactProvenanceId(provenance_id.value) != provenance_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "observation provenance conflicts")
    except HistoricalMatcherError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "observation provenance is invalid") from error
    _validate_digest(submission_receipt_sha256, field_name="submission receipt digest")
    _validate_digest(order_sha256, field_name="order digest")
    _validate_digest(trigger_root_sha256, field_name="trigger root digest")
    _validate_instrument(instrument)
    _validate_execution_policy(execution_policy)
    _validate_receipt(submission_receipt)
    _validate_runtime_root_key(trigger_root_key, expected_kind=trigger_root_kind)
    if trigger_root_kind is HistoricalDispatchKind.MARKET:
        if (
            type(trigger_root) is not MarketDataEnvelope
            or historical_market_root_digest(trigger_root) != trigger_root_sha256
            or runtime_root_order_key(trigger_root) != trigger_root_key
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "trade trigger root conflicts")
    elif (
        type(trigger_root) is not EndOfRunRoot
        or historical_end_root_digest(trigger_root) != trigger_root_sha256
        or runtime_root_order_key(trigger_root) != trigger_root_key
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "expiry trigger root conflicts")
    try:
        specification = spec_set.require(instrument)
        quantity = CanonicalDecimal(quantity_text)
        require_positive(quantity, field_name="quantity")
        require_quantized(
            quantity,
            specification.quantity_quantum,
            field_name="quantity",
        )
        price = None if price_text is None else CanonicalDecimal(price_text)
        if price is not None:
            require_quantized(
                price,
                specification.price_quantum,
                field_name="price",
            )
            if specification.price_domain is PriceDomain.POSITIVE and price.coefficient <= 0:
                raise _fail(OutcomeCode.PRICE_DOMAIN, "observation price must be positive")
            if specification.price_domain is PriceDomain.NON_NEGATIVE and price.coefficient < 0:
                raise _fail(OutcomeCode.PRICE_DOMAIN, "observation price must be non-negative")
    except HistoricalMatcherError:
        raise
    except EconomicValidationError as error:
        raise _fail(error.code, "observation economics are invalid") from error
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "observation economics are invalid") from error
    try:
        order_sha256_matches = order_digest(order) == order_sha256
        request_sha256_matches = (
            execution_request_digest(order) == submission_receipt.execution_request_sha256
        )
        client_key_matches = (
            order_client_submission_key(order) == submission_receipt.client_submission_key
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "observation Order evidence is invalid") from error
    if (
        historical_submission_receipt_digest(submission_receipt) != submission_receipt_sha256
        or not order_sha256_matches
        or submission_receipt.order_sha256 != order_sha256
        or not request_sha256_matches
        or not client_key_matches
        or order.run_id != submission_receipt.run_id
        or submission_receipt.source_namespace != source_namespace
        or order.order_id != submission_receipt.order_id
        or order.instrument != submission_receipt.instrument
        or order.instrument != instrument
        or order.side is not submission_receipt.side
        or order.side is not side
        or order.quantity.text != submission_receipt.quantity_text
        or order.quantity.text != quantity_text
        or order.dispatch_sequence != submission_receipt.dispatch_sequence
        or order.eligible_after_available_at != submission_receipt.eligible_after_available_at
        or order.order_kind is not OrderKind.MARKET
        or order.time_in_force is not TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
        or order.price_constraint is not None
        or order.instrument_specification_id != specification.specification_id
        or order.instrument_spec_set_id != spec_set.identifier
        or order.instrument_spec_set_id != submission_receipt.instrument_spec_set_id
        or order.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or order.instrument_spec_set_sha256 != submission_receipt.instrument_spec_set_sha256
        or order.execution_policy != execution_policy
        or order.execution_policy != submission_receipt.execution_policy
        or trigger_dispatch_sequence <= order.dispatch_sequence
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation receipt Order bindings conflict")
    occurred_text = _utc_text(occurred_at)
    available_text = _utc_text(available_at)
    document = {
        "available_at": available_text,
        "canonicalization": HISTORICAL_MATCHER_OBSERVATION_CANONICALIZATION,
        "execution_policy_id": execution_policy.identifier.value,
        "execution_policy_sha256": execution_policy.sha256.value,
        "expiry_outcome_code": (None if expiry_outcome_code is None else expiry_outcome_code.value),
        "fact_kind": fact_kind,
        "fact_sequence": fact_sequence,
        "instrument": _instrument_document(instrument),
        "instrument_spec_set_id": spec_set.identifier.value,
        "instrument_spec_set_sha256": instrument_spec_set_digest(spec_set).value,
        "occurred_at": occurred_text,
        "order_sha256": order_sha256.value,
        "price": price_text,
        "provenance_id": provenance_id.value,
        "quantity": quantity_text,
        "schema_version": HISTORICAL_MATCHER_SCHEMA_VERSION,
        "side": side.value,
        "source_namespace": source_namespace.value,
        "submission_receipt_sha256": submission_receipt_sha256.value,
        "trigger_dispatch_sequence": trigger_dispatch_sequence,
        "trigger_root_key": _runtime_key_document_from_key(trigger_root_key),
        "trigger_root_kind": trigger_root_kind.value,
        "trigger_root_sha256": trigger_root_sha256.value,
    }
    if fact_kind == "trade":
        suffix = trigger_root_key._suffix
        expected_price = (
            None
            if type(trigger_root) is not MarketDataEnvelope
            else _quantized_historical_close(
                trigger_root.payload.close,
                side=side,
                specification=specification,
            )
        )
        if (
            price_text is None
            or price != expected_price
            or expiry_outcome_code is not None
            or trigger_root_kind is not HistoricalDispatchKind.MARKET
            or type(suffix) is not _MarketDataSuffix
            or occurred_text != _utc_text(suffix.event_time)
            or available_text != _utc_text(trigger_root_key.available_at)
            or instrument.venue.code != suffix.instrument_venue
            or instrument.symbol != suffix.instrument_symbol
            or suffix.adjustment != Adjustment.RAW.value
            or suffix.revision != 0
            or trigger_root_key <= submission_receipt.causal_root_key
            or cast(MarketDataEnvelope, trigger_root).event_time
            <= order.eligible_after_available_at
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "trade observation fields conflict")
    elif fact_kind == "expiry":
        suffix = trigger_root_key._suffix
        if (
            price_text is not None
            or expiry_outcome_code is not OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA
            or trigger_root_kind is not HistoricalDispatchKind.END_OF_RUN
            or type(suffix) is not _EndOfRunSuffix
            or suffix.kind_rank != END_OF_RUN_KIND_RANKS[EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED]
            or type(trigger_root) is not EndOfRunRoot
            or trigger_root.run_id != submission_receipt.run_id
            or trigger_root_key <= submission_receipt.causal_root_key
            or occurred_text != available_text
            or available_text != _utc_text(trigger_root_key.available_at)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "expiry observation fields conflict")
    else:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "observation fact kind is invalid")
    return _canonical_json(document)


def historical_matcher_observation_digest(**kwargs: object) -> Sha256Digest:
    encoder = cast(
        Callable[..., bytes],
        cast(Any, canonical_historical_matcher_observation_bytes),
    )
    return _framed_digest(
        HISTORICAL_MATCHER_OBSERVATION_DIGEST_DOMAIN,
        encoder(**kwargs),
    )


def immutable_occupied_identity(
    value: Mapping[str, object] | None,
    *,
    conflict_kind: HistoricalMatcherConflictKind,
) -> Mapping[str, object] | None:
    if type(conflict_kind) is not HistoricalMatcherConflictKind:
        raise _fail(OutcomeCode.INVALID_TYPE, "conflict kind must be exact")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _fail(OutcomeCode.INVALID_TYPE, "occupied identity must be a mapping or null")
    tag = value.get("kind")
    if type(tag) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "occupied identity kind must be exact str")
    if conflict_kind is HistoricalMatcherConflictKind.SUBMISSION_IDENTITY:
        if tag != "order_id" or set(value) != {"kind", "order_id"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "submission conflict identity conflicts")
        raw_order_id = value["order_id"]
        if not isinstance(raw_order_id, Mapping):
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "occupied Order identity must be a mapping",
            )
        order_id = _parse_economic_id(
            dict(raw_order_id),
            field_name="occupied_identity.order_id",
        )
        if (
            order_id.owner_kind is not EconomicOwnerKind.EXECUTION_ORDER
            or order_id.owner_sequence < 1
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "occupied Order identity conflicts")
        return MappingProxyType(
            {
                "kind": "order_id",
                "order_id": MappingProxyType(_economic_id_document(order_id)),
            }
        )
    if conflict_kind is HistoricalMatcherConflictKind.CLIENT_SUBMISSION_KEY:
        if tag != "client_submission_key" or set(value) != {"kind", "sha256"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "client-key conflict identity conflicts")
        digest = _parse_digest(value["sha256"], field_name="occupied_identity.sha256")
        return MappingProxyType(
            {
                "kind": "client_submission_key",
                "sha256": digest.value,
            }
        )
    if conflict_kind in {
        HistoricalMatcherConflictKind.DISPATCH_IDENTITY,
        HistoricalMatcherConflictKind.NON_MONOTONE_DISPATCH,
    }:
        if tag != "dispatch_sequence" or set(value) != {
            "dispatch_sequence",
            "kind",
        }:
            raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch conflict identity conflicts")
        sequence = _require_positive_uint64(
            value["dispatch_sequence"],
            field_name="occupied_identity.dispatch_sequence",
        )
        return MappingProxyType(
            {
                "dispatch_sequence": sequence,
                "kind": "dispatch_sequence",
            }
        )
    if conflict_kind is HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "retained-binding drift has no occupied identity",
        )
    raise AssertionError("unreachable conflict kind")


def _occupied_identity_document(
    value: Mapping[str, object] | None,
    *,
    conflict_kind: HistoricalMatcherConflictKind,
) -> dict[str, object] | None:
    immutable = immutable_occupied_identity(value, conflict_kind=conflict_kind)
    if immutable is None:
        return None
    if immutable["kind"] == "order_id":
        return {
            "kind": "order_id",
            "order_id": dict(cast(Mapping[str, object], immutable["order_id"])),
        }
    return dict(immutable)


def _decode_document(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical payload must be exact bytes")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise _fail(
                    OutcomeCode.OUT_OF_RANGE,
                    "canonical payload has duplicate or invalid object keys",
                )
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)
    except HistoricalMatcherError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical payload is invalid JSON") from error
    if type(value) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical payload must be an object")
    if _canonical_json(value) != payload:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "canonical payload bytes are not canonical")
    return value


def _require_decode_context(value: object) -> HistoricalMatcherDecodeContext:
    if type(value) is not HistoricalMatcherDecodeContext:
        raise _fail(OutcomeCode.INVALID_TYPE, "decode context must be exact")
    instrument_spec_set_digest(value.spec_set)
    return value


def _parse_digest(value: object, *, field_name: str) -> Sha256Digest:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str")
    try:
        return Sha256Digest(value)
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is invalid") from error


def _parse_optional_digest(
    value: object,
    *,
    field_name: str,
) -> Sha256Digest | None:
    return None if value is None else _parse_digest(value, field_name=field_name)


def _parse_uint64_or_none(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact int or null")
    if not 1 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is outside positive uint64")
    return value


def _parse_non_negative(value: object, *, field_name: str) -> int:
    return _require_non_negative_uint64(value, field_name=field_name)


def _parse_time(value: object, *, field_name: str) -> datetime:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is invalid") from error
    if _utc_text(parsed) != value:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is not canonical")
    return parsed


def _parse_economic_id(value: object, *, field_name: str) -> EconomicId:
    if type(value) is not dict or set(value) != {
        "owner_kind",
        "owner_sequence",
        "run_id",
    }:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} has invalid fields")
    if type(value["owner_kind"]) is not str or type(value["run_id"]) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} strings are invalid")
    sequence = _parse_non_negative(
        value["owner_sequence"],
        field_name=f"{field_name}.owner_sequence",
    )
    try:
        return EconomicId(
            RunId(value["run_id"]),
            EconomicOwnerKind(value["owner_kind"]),
            sequence,
        )
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is invalid") from error


def _parse_instrument(value: object) -> Instrument:
    if type(value) is not dict or set(value) != {"symbol", "venue"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "instrument fields are invalid")
    if type(value["symbol"]) is not str or type(value["venue"]) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument strings must be exact")
    try:
        return Instrument(VenueId(value["venue"]), value["symbol"])
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "instrument is invalid") from error


def _parse_policy(
    document: Mapping[str, object],
    *,
    context: HistoricalMatcherDecodeContext,
) -> ExecutionPolicyRef:
    identifier = document.get("execution_policy_id")
    digest = document.get("execution_policy_sha256")
    if type(identifier) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "execution policy ID must be exact str")
    try:
        policy = ExecutionPolicyRef(
            ExecutionPolicyId(identifier),
            _parse_digest(digest, field_name="execution_policy_sha256"),
        )
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "execution policy is invalid") from error
    if policy != context.execution_policy:
        raise _fail(OutcomeCode.CONFLICTING_ID, "execution policy context conflicts")
    return policy


def _require_common_binding_document(
    document: Mapping[str, object],
    *,
    context: HistoricalMatcherDecodeContext,
    require_provenance: bool = False,
) -> None:
    if document.get("run_id") != context.run_id.value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "run context conflicts")
    if document.get("source_namespace") != context.source_namespace.value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "source context conflicts")
    if require_provenance and document.get("provenance_id") != context.provenance_id.value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "provenance context conflicts")


def _require_receipt_order_and_causal_root_bindings(
    *,
    context: HistoricalMatcherDecodeContext,
    receipt: HistoricalSubmissionReceipt,
    receipt_sha256: Sha256Digest,
    order: Order,
) -> MarketDataEnvelope:
    try:
        specification = context.spec_set.require(order.instrument)
        require_positive(order.quantity, field_name="Order quantity")
        require_quantized(
            order.quantity,
            specification.quantity_quantum,
            field_name="Order quantity",
        )
    except EconomicValidationError as error:
        raise _fail(error.code, "receipt Order quantity conflicts") from error
    except (KeyError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt Order specification conflicts") from error
    if (
        historical_submission_receipt_digest(receipt) != receipt_sha256
        or order_digest(order) != receipt.order_sha256
        or execution_request_digest(order) != receipt.execution_request_sha256
        or order_client_submission_key(order) != receipt.client_submission_key
        or order.run_id != context.run_id
        or receipt.run_id != context.run_id
        or receipt.source_namespace != context.source_namespace
        or order.order_id != receipt.order_id
        or order.instrument != receipt.instrument
        or order.side is not receipt.side
        or order.quantity.text != receipt.quantity_text
        or order.dispatch_sequence != receipt.dispatch_sequence
        or order.eligible_after_available_at != receipt.eligible_after_available_at
        or order.order_kind is not OrderKind.MARKET
        or order.time_in_force is not TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
        or order.price_constraint is not None
        or order.instrument_specification_id != specification.specification_id
        or order.instrument_spec_set_id != context.spec_set.identifier
        or order.instrument_spec_set_id != receipt.instrument_spec_set_id
        or order.instrument_spec_set_sha256 != instrument_spec_set_digest(context.spec_set)
        or order.instrument_spec_set_sha256 != receipt.instrument_spec_set_sha256
        or order.execution_policy != context.execution_policy
        or order.execution_policy != receipt.execution_policy
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt Order bindings conflict")
    causal_root = context.market_roots_by_sha256.get(receipt.causal_market_sha256)
    if (
        type(causal_root) is not MarketDataEnvelope
        or historical_market_root_digest(causal_root) != receipt.causal_market_sha256
        or runtime_root_order_key(causal_root) != receipt.causal_root_key
        or causal_root.payload.instrument != order.instrument
        or causal_root.available_at != order.eligible_after_available_at
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt causal market evidence conflicts")
    return causal_root


def decode_historical_submission_receipt(
    payload: bytes,
    *,
    context: HistoricalMatcherDecodeContext,
) -> HistoricalSubmissionReceipt:
    context = _require_decode_context(context)
    document = _decode_document(payload)
    expected_fields = {
        "audit_acknowledgement_id",
        "audit_acknowledgement_sha256",
        "authorization_state_version",
        "canonicalization",
        "causal_market_sha256",
        "causal_root_key",
        "client_submission_key",
        "dispatch_sequence",
        "eligible_after_available_at",
        "execution_policy_id",
        "execution_policy_sha256",
        "execution_request_sha256",
        "global_halt_epoch",
        "instrument",
        "instrument_gate_id",
        "instrument_gate_version",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "message_type",
        "order_id",
        "order_sha256",
        "outcome_code",
        "quantity",
        "risk_halt_epoch",
        "run_id",
        "schema_version",
        "side",
        "source_namespace",
        "submission_sequence",
    }
    if set(document) != expected_fields:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "receipt fields conflict")
    if (
        document["schema_version"] != HISTORICAL_MATCHER_SCHEMA_VERSION
        or document["canonicalization"] != HISTORICAL_SUBMISSION_RECEIPT_CANONICALIZATION
        or document["message_type"] != "historical_submission_receipt"
        or document["outcome_code"] != OutcomeCode.SUBMISSION_SUBMITTED.value
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "receipt schema markers conflict")
    _require_common_binding_document(document, context=context)
    order_sha256 = _parse_digest(document["order_sha256"], field_name="order_sha256")
    order = context.orders_by_sha256.get(order_sha256)
    if type(order) is not Order or order_digest(order) != order_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt Order lookup failed")
    request_sha256 = _parse_digest(
        document["execution_request_sha256"],
        field_name="execution_request_sha256",
    )
    if (
        execution_request_digest(order) != request_sha256
        or type(canonical_execution_request_bytes(order)) is not bytes
        or order_client_submission_key(order)
        != _parse_digest(
            document["client_submission_key"],
            field_name="client_submission_key",
        )
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt Order evidence conflicts")
    policy = _parse_policy(document, context=context)
    spec_id = document["instrument_spec_set_id"]
    if spec_id != context.spec_set.identifier.value or _parse_digest(
        document["instrument_spec_set_sha256"],
        field_name="instrument_spec_set_sha256",
    ) != instrument_spec_set_digest(context.spec_set):
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt spec context conflicts")
    try:
        side = OrderSide(_require_string(document["side"], field_name="side"))
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "receipt side is invalid") from error
    receipt = _create_historical_submission_receipt(
        run_id=context.run_id,
        source_namespace=context.source_namespace,
        submission_sequence=_require_positive_uint64(
            document["submission_sequence"],
            field_name="submission_sequence",
        ),
        order_id=_parse_economic_id(document["order_id"], field_name="order_id"),
        order_sha256=order_sha256,
        execution_request_sha256=request_sha256,
        client_submission_key=order_client_submission_key(order),
        instrument=_parse_instrument(document["instrument"]),
        side=side,
        quantity_text=_require_string(document["quantity"], field_name="quantity"),
        causal_market_sha256=_parse_digest(
            document["causal_market_sha256"],
            field_name="causal_market_sha256",
        ),
        causal_root_key=runtime_root_key_from_document(document["causal_root_key"]),
        dispatch_sequence=_require_positive_uint64(
            document["dispatch_sequence"],
            field_name="dispatch_sequence",
        ),
        eligible_after_available_at=_parse_time(
            document["eligible_after_available_at"],
            field_name="eligible_after_available_at",
        ),
        audit_acknowledgement_id=_require_non_empty_string(
            document["audit_acknowledgement_id"],
            field_name="audit_acknowledgement_id",
        ),
        audit_acknowledgement_sha256=_parse_digest(
            document["audit_acknowledgement_sha256"],
            field_name="audit_acknowledgement_sha256",
        ),
        global_halt_epoch=_require_non_negative_uint64(
            document["global_halt_epoch"],
            field_name="global_halt_epoch",
        ),
        risk_halt_epoch=_require_non_negative_uint64(
            document["risk_halt_epoch"],
            field_name="risk_halt_epoch",
        ),
        instrument_gate_id=_require_non_empty_string(
            document["instrument_gate_id"],
            field_name="instrument_gate_id",
        ),
        instrument_gate_version=_require_positive_uint64(
            document["instrument_gate_version"],
            field_name="instrument_gate_version",
        ),
        authorization_state_version=_require_non_negative_uint64(
            document["authorization_state_version"],
            field_name="authorization_state_version",
        ),
        instrument_spec_set_id=context.spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(context.spec_set),
        execution_policy=policy,
    )
    if (
        receipt.order_id != order.order_id
        or receipt.instrument != order.instrument
        or receipt.side is not order.side
        or receipt.quantity_text != order.quantity.text
        or receipt.eligible_after_available_at != order.eligible_after_available_at
        or canonical_historical_submission_receipt_bytes(receipt) != payload
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "receipt reconstruction conflicts")
    _require_receipt_order_and_causal_root_bindings(
        context=context,
        receipt=receipt,
        receipt_sha256=historical_submission_receipt_digest(receipt),
        order=order,
    )
    return receipt


def _require_string(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str")
    return value


def _expected_decoded_batch_ingress(
    *,
    context: HistoricalMatcherDecodeContext,
    batch: HistoricalMatcherDispatchBatch,
    receipt_sha256: Sha256Digest,
    receipt: HistoricalSubmissionReceipt,
    order: Order,
    fact_sequence: int,
    trigger_root: MarketDataEnvelope | EndOfRunRoot,
) -> ExecutionFactIngress:
    causal_root = _require_receipt_order_and_causal_root_bindings(
        context=context,
        receipt=receipt,
        receipt_sha256=receipt_sha256,
        order=order,
    )
    if batch.dispatch_sequence <= order.dispatch_sequence:
        raise _fail(OutcomeCode.CONFLICTING_ID, "batch receipt bindings conflict")

    fact_kind: str
    price: CanonicalDecimal | None
    expiry_code: OutcomeCode | None
    occurred_at: datetime
    available_at: datetime
    if batch.dispatch_kind is HistoricalDispatchKind.MARKET:
        if type(trigger_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.CONFLICTING_ID, "batch market root conflicts")
        root_key = runtime_root_order_key(trigger_root)
        if (
            trigger_root.payload.instrument != order.instrument
            or trigger_root.payload.adjustment is not Adjustment.RAW
            or trigger_root.revision != 0
            or root_key <= runtime_root_order_key(causal_root)
            or trigger_root.event_time <= order.eligible_after_available_at
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "batch market eligibility conflicts")
        fact_kind = "trade"
        price = _quantized_historical_close(
            trigger_root.payload.close,
            side=order.side,
            specification=context.spec_set.require(order.instrument),
        )
        expiry_code = None
        occurred_at = trigger_root.event_time
        available_at = trigger_root.available_at
    else:
        root_key = runtime_root_order_key(trigger_root)
        if (
            type(trigger_root) is not EndOfRunRoot
            or trigger_root.kind is not EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED
            or trigger_root.run_id != context.run_id
            or root_key != batch.trigger_root_key
            or root_key <= receipt.causal_root_key
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "batch end root conflicts")
        fact_kind = "expiry"
        price = None
        expiry_code = OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA
        occurred_at = trigger_root.available_at
        available_at = trigger_root.available_at

    observation_sha256 = historical_matcher_observation_digest(
        fact_sequence=fact_sequence,
        fact_kind=fact_kind,
        source_namespace=context.source_namespace,
        provenance_id=context.provenance_id,
        submission_receipt_sha256=receipt_sha256,
        submission_receipt=receipt,
        order_sha256=receipt.order_sha256,
        order=order,
        trigger_root_kind=batch.dispatch_kind,
        trigger_root_sha256=batch.trigger_root_sha256,
        trigger_root_key=batch.trigger_root_key,
        trigger_root=trigger_root,
        trigger_dispatch_sequence=batch.dispatch_sequence,
        occurred_at=occurred_at,
        available_at=available_at,
        instrument=receipt.instrument,
        side=order.side,
        quantity_text=order.quantity.text,
        price_text=None if price is None else price.text,
        expiry_outcome_code=expiry_code,
        spec_set=context.spec_set,
        execution_policy=context.execution_policy,
    )
    provenance = FactProvenance(context.provenance_id, observation_sha256)
    if batch.dispatch_kind is HistoricalDispatchKind.MARKET:
        assert price is not None
        fact = create_trade_execution_fact(
            spec_set=context.spec_set,
            side=order.side,
            quantity=CanonicalDecimal(order.quantity.text),
            price=price,
            source_namespace=context.source_namespace,
            dedup_identity=SourceNativeSequence(fact_sequence),
            occurred_at=occurred_at,
            provenance=provenance,
            instrument=order.instrument,
            client_submission_key=order_client_submission_key(order),
            order_id=order.order_id,
            correlation_id=order.correlation_id,
            causation_id=order.order_id,
        )
    else:
        fact = create_lifecycle_execution_fact(
            kind=ExecutionFactKind.EXPIRY,
            outcome_code=expiry_code,
            source_namespace=context.source_namespace,
            dedup_identity=SourceNativeSequence(fact_sequence),
            occurred_at=occurred_at,
            provenance=provenance,
            instrument=order.instrument,
            client_submission_key=order_client_submission_key(order),
            order_id=order.order_id,
            correlation_id=order.correlation_id,
            causation_id=order.order_id,
        )
    return create_execution_fact_ingress(
        available_at=available_at,
        source_namespace=context.source_namespace,
        ingress_sequence=fact_sequence,
        fact=fact,
    )


def decode_historical_matcher_dispatch_batch(
    payload: bytes,
    *,
    context: HistoricalMatcherDecodeContext,
) -> HistoricalMatcherDispatchBatch:
    context = _require_decode_context(context)
    document = _decode_document(payload)
    if set(document) != {
        "canonicalization",
        "dispatch_kind",
        "dispatch_sequence",
        "ingresses",
        "message_type",
        "next_fact_sequence_after",
        "next_fact_sequence_before",
        "order_ids",
        "run_id",
        "schema_version",
        "source_namespace",
        "submission_sequences",
        "trigger_root_key",
        "trigger_root_sha256",
    }:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch batch fields conflict")
    if (
        document["schema_version"] != HISTORICAL_MATCHER_SCHEMA_VERSION
        or document["canonicalization"] != HISTORICAL_MATCHER_DISPATCH_BATCH_CANONICALIZATION
        or document["message_type"] != "historical_matcher_dispatch_batch"
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch batch schema markers conflict")
    _require_common_binding_document(document, context=context)
    try:
        kind = HistoricalDispatchKind(
            _require_string(document["dispatch_kind"], field_name="dispatch_kind")
        )
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch kind is invalid") from error
    raw_ingresses = document["ingresses"]
    raw_orders = document["order_ids"]
    raw_submissions = document["submission_sequences"]
    if (
        type(raw_ingresses) is not list
        or type(raw_orders) is not list
        or type(raw_submissions) is not list
        or not len(raw_ingresses) == len(raw_orders) == len(raw_submissions)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatch batch arrays are invalid")
    ingresses: list[ExecutionFactIngress] = []
    ingress_sha256s: list[Sha256Digest] = []
    for index, raw in enumerate(raw_ingresses):
        if type(raw) is not dict or set(raw) != {
            "ingress_identity",
            "ingress_sha256",
        }:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "batch ingress entry fields conflict")
        digest = _parse_digest(
            raw["ingress_sha256"],
            field_name=f"ingresses[{index}].ingress_sha256",
        )
        ingress = context.ingresses_by_sha256.get(digest)
        if (
            type(ingress) is not ExecutionFactIngress
            or _execution_fact_ingress_digest(ingress) != digest
            or raw["ingress_identity"] != _ingress_identity_document(ingress.identity)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "batch ingress lookup conflicts")
        ingresses.append(ingress)
        ingress_sha256s.append(digest)
    batch = _create_historical_matcher_dispatch_batch(
        run_id=context.run_id,
        source_namespace=context.source_namespace,
        dispatch_kind=kind,
        dispatch_sequence=_require_positive_uint64(
            document["dispatch_sequence"],
            field_name="dispatch_sequence",
        ),
        trigger_root_sha256=_parse_digest(
            document["trigger_root_sha256"],
            field_name="trigger_root_sha256",
        ),
        trigger_root_key=runtime_root_key_from_document(document["trigger_root_key"]),
        next_fact_sequence_before=_parse_uint64_or_none(
            document["next_fact_sequence_before"],
            field_name="next_fact_sequence_before",
        ),
        next_fact_sequence_after=_parse_uint64_or_none(
            document["next_fact_sequence_after"],
            field_name="next_fact_sequence_after",
        ),
        submission_sequences=tuple(
            _require_positive_uint64(value, field_name="submission_sequence")
            for value in raw_submissions
        ),
        order_ids=tuple(_parse_economic_id(value, field_name="order_id") for value in raw_orders),
        ingresses=tuple(ingresses),
        ingress_sha256s=tuple(ingress_sha256s),
    )
    if batch.dispatch_kind is HistoricalDispatchKind.MARKET:
        trigger_root: MarketDataEnvelope | EndOfRunRoot | None = context.market_roots_by_sha256.get(
            batch.trigger_root_sha256
        )
    else:
        trigger_root = context.end_roots_by_sha256.get(batch.trigger_root_sha256)
    if trigger_root is None:
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch root evidence conflicts")
    root_digest_matches = (
        type(trigger_root) is MarketDataEnvelope
        and historical_market_root_digest(trigger_root) == batch.trigger_root_sha256
    ) or (
        type(trigger_root) is EndOfRunRoot
        and historical_end_root_digest(trigger_root) == batch.trigger_root_sha256
    )
    if not root_digest_matches or runtime_root_order_key(trigger_root) != batch.trigger_root_key:
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch root evidence conflicts")
    if batch.dispatch_kind is HistoricalDispatchKind.END_OF_RUN and (
        type(trigger_root) is not EndOfRunRoot
        or trigger_root.kind is not EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED
        or trigger_root.run_id != context.run_id
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch end root conflicts")

    receipts = tuple(context.receipts_by_sha256.items())
    for index, (submission_sequence, order_id, ingress, ingress_sha256) in enumerate(
        zip(
            batch.submission_sequences,
            batch.order_ids,
            batch.ingresses,
            batch.ingress_sha256s,
            strict=True,
        )
    ):
        matching_receipts = tuple(
            (digest, receipt)
            for digest, receipt in receipts
            if receipt.submission_sequence == submission_sequence and receipt.order_id == order_id
        )
        if len(matching_receipts) != 1:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "dispatch batch submission evidence conflicts",
            )
        receipt_sha256, receipt = matching_receipts[0]
        order = context.orders_by_sha256.get(receipt.order_sha256)
        if type(order) is not Order:
            raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch Order evidence conflicts")
        expected = _expected_decoded_batch_ingress(
            context=context,
            batch=batch,
            receipt_sha256=receipt_sha256,
            receipt=receipt,
            order=order,
            fact_sequence=cast(int, batch.next_fact_sequence_before) + index,
            trigger_root=trigger_root,
        )
        if canonical_execution_fact_ingress_bytes(
            ingress
        ) != canonical_execution_fact_ingress_bytes(
            expected
        ) or ingress_sha256 != execution_fact_ingress_digest(expected):
            raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch fact evidence conflicts")
    if canonical_historical_matcher_dispatch_batch_bytes(batch) != payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch batch reconstruction conflicts")
    return batch


def decode_historical_matcher_conflict(
    payload: bytes,
    *,
    context: HistoricalMatcherDecodeContext,
) -> HistoricalMatcherConflictEvidence:
    context = _require_decode_context(context)
    document = _decode_document(payload)
    if set(document) != {
        "canonicalization",
        "conflict_kind",
        "existing_sha256",
        "last_successful_dispatch_sequence",
        "message_type",
        "next_fact_sequence",
        "next_submission_sequence",
        "occupied_identity",
        "pending_count",
        "run_id",
        "schema_version",
        "submitted_dispatch_sequence",
        "submitted_sha256",
        "trigger_root_sha256",
    }:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "conflict fields conflict")
    if (
        document["schema_version"] != HISTORICAL_MATCHER_SCHEMA_VERSION
        or document["canonicalization"] != HISTORICAL_MATCHER_CONFLICT_CANONICALIZATION
        or document["message_type"] != "historical_matcher_conflict"
        or document["run_id"] != context.run_id.value
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "conflict schema or run binding conflicts")
    try:
        kind = HistoricalMatcherConflictKind(
            _require_string(document["conflict_kind"], field_name="conflict_kind")
        )
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "conflict kind is invalid") from error
    occupied = document["occupied_identity"]
    if occupied is not None and type(occupied) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "occupied identity must be object or null")
    conflict = _create_historical_matcher_conflict(
        run_id=context.run_id,
        conflict_kind=kind,
        occupied_identity=cast(Mapping[str, object] | None, occupied),
        existing_sha256=_parse_optional_digest(
            document["existing_sha256"],
            field_name="existing_sha256",
        ),
        submitted_sha256=_parse_optional_digest(
            document["submitted_sha256"],
            field_name="submitted_sha256",
        ),
        submitted_dispatch_sequence=_parse_uint64_or_none(
            document["submitted_dispatch_sequence"],
            field_name="submitted_dispatch_sequence",
        ),
        last_successful_dispatch_sequence=_parse_uint64_or_none(
            document["last_successful_dispatch_sequence"],
            field_name="last_successful_dispatch_sequence",
        ),
        pending_count=_parse_non_negative(
            document["pending_count"],
            field_name="pending_count",
        ),
        next_submission_sequence=_parse_uint64_or_none(
            document["next_submission_sequence"],
            field_name="next_submission_sequence",
        ),
        next_fact_sequence=_parse_uint64_or_none(
            document["next_fact_sequence"],
            field_name="next_fact_sequence",
        ),
        trigger_root_sha256=_parse_optional_digest(
            document["trigger_root_sha256"],
            field_name="trigger_root_sha256",
        ),
    )
    if canonical_historical_matcher_conflict_bytes(conflict) != payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "conflict reconstruction conflicts")
    return conflict


def decode_historical_matcher_state(
    payload: bytes,
    *,
    context: HistoricalMatcherDecodeContext,
) -> HistoricalMatcherState:
    context = _require_decode_context(context)
    document = _decode_document(payload)
    if set(document) != {
        "canonicalization",
        "conflict_sha256",
        "dispatch_batch_sha256s",
        "end_batch_sha256",
        "ended",
        "execution_policy_id",
        "execution_policy_sha256",
        "halted",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "issued_ingresses",
        "last_new_dispatch_sequence",
        "message_type",
        "next_fact_sequence",
        "next_submission_sequence",
        "pending_order_ids",
        "provenance_id",
        "receipt_sha256s",
        "run_id",
        "schema_version",
        "source_namespace",
    }:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "matcher state fields conflict")
    if (
        document["schema_version"] != HISTORICAL_MATCHER_SCHEMA_VERSION
        or document["canonicalization"] != HISTORICAL_MATCHER_STATE_CANONICALIZATION
        or document["message_type"] != "historical_matcher_state"
        or type(document["ended"]) is not bool
        or type(document["halted"]) is not bool
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "matcher state schema markers conflict")
    _require_common_binding_document(
        document,
        context=context,
        require_provenance=True,
    )
    _parse_policy(document, context=context)
    if document["instrument_spec_set_id"] != context.spec_set.identifier.value or _parse_digest(
        document["instrument_spec_set_sha256"],
        field_name="instrument_spec_set_sha256",
    ) != instrument_spec_set_digest(context.spec_set):
        raise _fail(OutcomeCode.CONFLICTING_ID, "matcher state spec context conflicts")
    raw_receipts = document["receipt_sha256s"]
    raw_batches = document["dispatch_batch_sha256s"]
    raw_ingresses = document["issued_ingresses"]
    raw_pending = document["pending_order_ids"]
    if any(
        type(value) is not list for value in (raw_receipts, raw_batches, raw_ingresses, raw_pending)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "matcher state arrays are invalid")
    raw_receipts = cast(list[object], raw_receipts)
    raw_batches = cast(list[object], raw_batches)
    raw_ingresses = cast(list[object], raw_ingresses)
    raw_pending = cast(list[object], raw_pending)
    receipt_digests: list[Sha256Digest] = []
    receipts: list[HistoricalSubmissionReceipt] = []
    for value in raw_receipts:
        digest = _parse_digest(value, field_name="receipt_sha256")
        receipt = context.receipts_by_sha256.get(digest)
        if (
            type(receipt) is not HistoricalSubmissionReceipt
            or historical_submission_receipt_digest(receipt) != digest
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt lookup conflicts")
        receipt = decode_historical_submission_receipt(
            canonical_historical_submission_receipt_bytes(receipt),
            context=context,
        )
        receipt_digests.append(digest)
        receipts.append(receipt)
    batch_digests: list[Sha256Digest] = []
    batches: list[HistoricalMatcherDispatchBatch] = []
    for value in raw_batches:
        digest = _parse_digest(value, field_name="batch_sha256")
        batch = context.batches_by_sha256.get(digest)
        if (
            type(batch) is not HistoricalMatcherDispatchBatch
            or historical_matcher_dispatch_batch_digest(batch) != digest
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state batch lookup conflicts")
        batch = decode_historical_matcher_dispatch_batch(
            canonical_historical_matcher_dispatch_batch_bytes(batch),
            context=context,
        )
        batch_digests.append(digest)
        batches.append(batch)
    issued: list[ExecutionFactIngress] = []
    for raw in raw_ingresses:
        if type(raw) is not dict or set(raw) != {
            "ingress_identity",
            "ingress_sha256",
        }:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "state ingress fields conflict")
        digest = _parse_digest(raw["ingress_sha256"], field_name="ingress_sha256")
        ingress = context.ingresses_by_sha256.get(digest)
        if (
            type(ingress) is not ExecutionFactIngress
            or _execution_fact_ingress_digest(ingress) != digest
            or raw["ingress_identity"] != _ingress_identity_document(ingress.identity)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "state ingress lookup conflicts")
        issued.append(ingress)
    conflict_digest = _parse_optional_digest(
        document["conflict_sha256"],
        field_name="conflict_sha256",
    )
    conflict = None if conflict_digest is None else context.conflicts_by_sha256.get(conflict_digest)
    if conflict_digest is not None and (
        type(conflict) is not HistoricalMatcherConflictEvidence
        or historical_matcher_conflict_digest(conflict) != conflict_digest
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state conflict lookup conflicts")
    state = _create_historical_matcher_state(
        run_id=context.run_id,
        source_namespace=context.source_namespace,
        provenance_id=context.provenance_id,
        instrument_spec_set_id=context.spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(context.spec_set),
        execution_policy=context.execution_policy,
        next_submission_sequence=_parse_uint64_or_none(
            document["next_submission_sequence"],
            field_name="next_submission_sequence",
        ),
        next_fact_sequence=_parse_uint64_or_none(
            document["next_fact_sequence"],
            field_name="next_fact_sequence",
        ),
        _submission_receipts=tuple(receipts),
        receipt_sha256s=tuple(receipt_digests),
        pending_order_ids=tuple(
            _parse_economic_id(value, field_name="pending_order_id") for value in raw_pending
        ),
        issued_ingresses=tuple(issued),
        _dispatch_batch_history_sha256=_dispatch_batch_history_digest(tuple(batch_digests)),
        _dispatch_ingress_history_sha256=_dispatch_ingress_history_digest(
            tuple(batch_digests),
            tuple(issued),
        ),
        _last_dispatch_batch=None if not batches else batches[-1],
        dispatch_batch_sha256s=tuple(batch_digests),
        last_new_dispatch_sequence=_parse_uint64_or_none(
            document["last_new_dispatch_sequence"],
            field_name="last_new_dispatch_sequence",
        ),
        ended=document["ended"],
        end_batch_sha256=_parse_optional_digest(
            document["end_batch_sha256"],
            field_name="end_batch_sha256",
        ),
        halted=document["halted"],
        conflict=conflict,
    )
    if tuple(receipt.submission_sequence for receipt in receipts) != tuple(
        range(1, len(receipts) + 1)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt append order conflicts")
    submitted_entries = tuple(
        (receipt.submission_sequence, receipt.order_id) for receipt in receipts
    )
    if len(set(submitted_entries)) != len(submitted_entries) or len(
        {receipt.order_id for receipt in receipts}
    ) != len(receipts):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt identity history conflicts")
    if any(
        receipt.run_id != context.run_id or receipt.source_namespace != context.source_namespace
        for receipt in receipts
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt bindings conflict")
    if any(
        batch.run_id != context.run_id or batch.source_namespace != context.source_namespace
        for batch in batches
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state batch bindings conflict")
    if tuple(receipt.dispatch_sequence for receipt in receipts) != tuple(
        sorted(receipt.dispatch_sequence for receipt in receipts)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt dispatch order conflicts")
    if tuple(batch.dispatch_sequence for batch in batches) != tuple(
        sorted(batch.dispatch_sequence for batch in batches)
    ) or len({batch.dispatch_sequence for batch in batches}) != len(batches):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state dispatch append order conflicts")
    receipt_records: list[tuple[HistoricalSubmissionReceipt, Order]] = []
    for receipt in receipts:
        order = context.orders_by_sha256.get(receipt.order_sha256)
        if type(order) is not Order:
            raise _fail(OutcomeCode.CONFLICTING_ID, "state receipt Order lookup conflicts")
        receipt_records.append((receipt, order))
    pending_records: list[tuple[HistoricalSubmissionReceipt, Order]] = []
    next_receipt_index = 0
    expected_fact_pointer: int | None = 1
    for index, batch in enumerate(batches):
        while (
            next_receipt_index < len(receipt_records)
            and receipt_records[next_receipt_index][0].dispatch_sequence < batch.dispatch_sequence
        ):
            pending_records.append(receipt_records[next_receipt_index])
            next_receipt_index += 1
        if batch.dispatch_kind is HistoricalDispatchKind.MARKET:
            trigger_root = context.market_roots_by_sha256.get(batch.trigger_root_sha256)
            if type(trigger_root) is not MarketDataEnvelope:
                raise _fail(OutcomeCode.CONFLICTING_ID, "state market root lookup conflicts")
            expected_records = tuple(
                (receipt, order)
                for receipt, order in pending_records
                if (
                    trigger_root.payload.instrument == order.instrument
                    and trigger_root.payload.adjustment is Adjustment.RAW
                    and trigger_root.revision == 0
                    and batch.trigger_root_key > receipt.causal_root_key
                    and trigger_root.event_time > order.eligible_after_available_at
                )
            )
        else:
            expected_records = tuple(pending_records)
        expected_entries = tuple(
            (receipt.submission_sequence, receipt.order_id) for receipt, _ in expected_records
        )
        actual_entries = tuple(zip(batch.submission_sequences, batch.order_ids, strict=True))
        if actual_entries != expected_entries:
            raise _fail(OutcomeCode.CONFLICTING_ID, "state batch eligibility history conflicts")
        emitted_order_ids = {receipt.order_id for receipt, _ in expected_records}
        pending_records = [
            record for record in pending_records if record[0].order_id not in emitted_order_ids
        ]
        if batch.next_fact_sequence_before != expected_fact_pointer:
            raise _fail(OutcomeCode.CONFLICTING_ID, "state batch fact chain conflicts")
        expected_fact_pointer = batch.next_fact_sequence_after
        if batch.dispatch_kind is HistoricalDispatchKind.END_OF_RUN and index != len(batches) - 1:
            raise _fail(OutcomeCode.CONFLICTING_ID, "state has dispatch after terminal batch")
    pending_records.extend(receipt_records[next_receipt_index:])
    if state.next_fact_sequence != expected_fact_pointer:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state final fact pointer conflicts")
    expected_last = None if not batches else batches[-1].dispatch_sequence
    if state.last_new_dispatch_sequence != expected_last:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state last dispatch conflicts")
    emitted_entries = tuple(
        zip(batch.submission_sequences, batch.order_ids, strict=True) for batch in batches
    )
    flattened_emitted_entries = tuple(entry for entries in emitted_entries for entry in entries)
    submitted_entry_set = set(submitted_entries)
    if any(entry not in submitted_entry_set for entry in flattened_emitted_entries):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state batch references external receipt")
    emitted_order_ids_in_order = tuple(order_id for _, order_id in flattened_emitted_entries)
    if len(set(emitted_order_ids_in_order)) != len(emitted_order_ids_in_order):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state Order is emitted more than once")
    if state.ended and (
        not batches
        or batches[-1].dispatch_kind is not HistoricalDispatchKind.END_OF_RUN
        or historical_matcher_dispatch_batch_digest(batches[-1]) != state.end_batch_sha256
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state terminal batch conflicts")
    if not state.ended and any(
        batch.dispatch_kind is HistoricalDispatchKind.END_OF_RUN for batch in batches
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "non-ended state contains terminal batch")
    flattened_ingresses = tuple(ingress for batch in batches for ingress in batch.ingresses)
    if flattened_ingresses != tuple(issued):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state issued ingress append order conflicts")
    expected_pending = tuple(receipt.order_id for receipt, _ in pending_records)
    if state.pending_order_ids != expected_pending:
        raise _fail(OutcomeCode.CONFLICTING_ID, "state pending membership conflicts")
    if state.ended and state.pending_order_ids:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ended state retains pending Orders")
    if conflict is not None and (
        conflict.pending_count != len(state.pending_order_ids)
        or conflict.next_submission_sequence != state.next_submission_sequence
        or conflict.next_fact_sequence != state.next_fact_sequence
        or conflict.last_successful_dispatch_sequence != state.last_new_dispatch_sequence
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "state conflict snapshot conflicts")
    if canonical_historical_matcher_state_bytes(state) != payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "matcher state reconstruction conflicts")
    return state
