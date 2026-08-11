"""Dormant-then-active historical submission authorization authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, final

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditAppendPort,
    AuditLogicalKey,
    AuditRecord,
    AuditRecordKind,
    AuditRecoveryRecordSource,
    AuditSubjectKind,
    audit_acknowledgement_id,
    audit_append_acknowledgement_digest,
    audit_subject_digest,
    create_audit_append_acknowledgement,
    require_audit_acknowledgement,
)
from ea.core.execution import (
    InstrumentExecutionSpecSet,
    InstrumentSpecSetId,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    ExecutionPolicyRef,
    Order,
    canonical_execution_request_bytes,
    canonical_order_bytes,
    execution_request_digest,
    order_digest,
)
from ea.core.historical_matching import (
    HistoricalPreEffectAuthorizationError,
    HistoricalSubmissionAuthorizationProof,
    HistoricalSubmissionReceipt,
    _create_historical_submission_authorization_proof,
    _require_historical_submission_receipt_causal_root,
    canonical_historical_submission_receipt_bytes,
    historical_market_root_digest,
    runtime_root_key_document,
    runtime_root_order_key_document,
)
from ea.core.lifecycle import (
    CoordinatorPhase,
    GlobalHaltFreshnessPort,
    GlobalHaltSnapshot,
    InstrumentGateFreshnessPort,
    InstrumentGateSnapshot,
    PortfolioFreshnessPort,
    RiskFreshnessPort,
    RuntimeLifecyclePort,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import PortfolioSnapshot
from ea.core.risk import RiskStateSnapshot
from ea.core.run import RunBinding, RunId, Sha256Digest
from ea.core.runtime import RuntimeRootOrderKey, runtime_root_order_key
from ea.core.strategy import causal_market_digest


class _CoordinatorAuthorizationView(Protocol):
    @property
    def binding(self) -> RunBinding: ...

    @property
    def state(self) -> object: ...


class AuthorizationOrderRecoveryResolver(Protocol):
    def resolve_issued_order_by_id(self, order_id: EconomicId) -> Order | None: ...


class AuthorizationSubmissionRecoveryResolver(Protocol):
    def resolve_submission_receipt(
        self,
        *,
        order_id: EconomicId,
        execution_request_sha256: Sha256Digest,
    ) -> HistoricalSubmissionReceipt | None: ...


@dataclass(frozen=True, slots=True)
class _AuthorizationAttempt:
    order: Order
    causal_market_root: MarketDataEnvelope | None
    payload: bytes
    acknowledgement: AuditAppendAcknowledgement
    burned: bool


@dataclass(frozen=True, slots=True)
class _Freshness:
    portfolio: PortfolioSnapshot
    risk: RiskStateSnapshot
    global_halt: GlobalHaltSnapshot
    gate: InstrumentGateSnapshot
    state_version: int


class _PreparationCapability:
    __slots__ = ()


class _ActivationSeal:
    __slots__ = ()


def _deny(code: OutcomeCode, message: str) -> HistoricalPreEffectAuthorizationError:
    return HistoricalPreEffectAuthorizationError(code, message)


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _economic_id_document(value: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": value.owner_kind.value,
        "owner_sequence": value.owner_sequence,
        "run_id": value.run_id.value,
    }


def _economic_id_from_document(value: object, run_id: RunId) -> EconomicId:
    if type(value) is not dict or set(value) != {"owner_kind", "owner_sequence", "run_id"}:
        raise ValueError("economic ID document is invalid")
    if (
        value["owner_kind"] != EconomicOwnerKind.EXECUTION_ORDER.value
        or value["run_id"] != run_id.value
        or type(value["owner_sequence"]) is not int
        or not 1 <= value["owner_sequence"] <= (1 << 64) - 1
    ):
        raise ValueError("economic ID binding is invalid")
    return EconomicId(run_id, EconomicOwnerKind.EXECUTION_ORDER, value["owner_sequence"])


def _authorization_payload_from_receipt(
    receipt: HistoricalSubmissionReceipt,
    acknowledgement: AuditAppendAcknowledgement,
    order: Order,
) -> bytes | None:
    if type(receipt) is not HistoricalSubmissionReceipt:
        return None
    try:
        canonical_historical_submission_receipt_bytes(receipt)
        causal_root = _require_historical_submission_receipt_causal_root(receipt)
        root_key = runtime_root_order_key_document(receipt.causal_root_key)
    except Exception:
        return None
    if (
        receipt.run_id != order.run_id
        or receipt.order_id != order.order_id
        or receipt.order_sha256 != order_digest(order)
        or receipt.execution_request_sha256 != execution_request_digest(order)
        or receipt.client_submission_key != order.client_submission_key
        or receipt.instrument != order.instrument
        or receipt.side is not order.side
        or receipt.quantity_text != order.quantity.text
        or receipt.dispatch_sequence != order.dispatch_sequence
        or receipt.eligible_after_available_at != order.eligible_after_available_at
        or receipt.audit_acknowledgement_id != audit_acknowledgement_id(acknowledgement)
        or receipt.audit_acknowledgement_sha256
        != audit_append_acknowledgement_digest(acknowledgement)
        or receipt.risk_halt_epoch != order.risk_state_version
        or receipt.instrument_spec_set_id != order.instrument_spec_set_id
        or receipt.instrument_spec_set_sha256 != order.instrument_spec_set_sha256
        or receipt.execution_policy != order.execution_policy
    ):
        return None
    return _canonical_json(
        {
            "authorization_state_version": receipt.authorization_state_version,
            "canonicalization": "ea-canonical-json-v1",
            "causal_market_sha256": causal_market_digest(causal_root).value,
            "causal_root_key": root_key,
            "dispatch_sequence": receipt.dispatch_sequence,
            "execution_policy_id": receipt.execution_policy.identifier.value,
            "execution_policy_sha256": receipt.execution_policy.sha256.value,
            "execution_request_sha256": receipt.execution_request_sha256.value,
            "global_halt_epoch": receipt.global_halt_epoch,
            "held_for_order_id": _economic_id_document(receipt.order_id),
            "instrument_gate_id": receipt.instrument_gate_id,
            "instrument_gate_version": receipt.instrument_gate_version,
            "instrument_spec_set_id": receipt.instrument_spec_set_id.value,
            "instrument_spec_set_sha256": receipt.instrument_spec_set_sha256.value,
            "order_id": _economic_id_document(receipt.order_id),
            "order_sha256": receipt.order_sha256.value,
            "portfolio_snapshot_version": order.portfolio_snapshot_version,
            "risk_halt_epoch": receipt.risk_halt_epoch,
            "risk_state_version": order.risk_state_version,
            "run_id": receipt.run_id.value,
            "schema": "ea.audit-submission-authorization.v1",
        }
    )


@final
class HistoricalSubmissionAuthorizationAuthority:
    """Single owner of durable authorization attempts and opaque matcher proofs."""

    __slots__ = (
        "_active",
        "_attempts",
        "_attempt_by_order",
        "_activation_seal",
        "_audit",
        "_binding",
        "_coordinator",
        "_execution_policy",
        "_global_halt",
        "_instrument_gate",
        "_portfolio",
        "_preparation_capability",
        "_risk",
        "_recovery_loaded",
        "_runtime",
        "_spec_set",
        "_spec_sha256",
    )
    _active: bool
    _activation_seal: _ActivationSeal | None
    _attempts: dict[tuple[EconomicId, Sha256Digest], _AuthorizationAttempt]
    _attempt_by_order: dict[EconomicId, Sha256Digest]
    _audit: AuditAppendPort
    _binding: RunBinding
    _coordinator: _CoordinatorAuthorizationView | None
    _execution_policy: ExecutionPolicyRef
    _global_halt: GlobalHaltFreshnessPort
    _instrument_gate: InstrumentGateFreshnessPort
    _portfolio: PortfolioFreshnessPort
    _preparation_capability: _PreparationCapability
    _risk: RiskFreshnessPort
    _recovery_loaded: bool
    _runtime: RuntimeLifecyclePort
    _spec_set: InstrumentExecutionSpecSet
    _spec_sha256: Sha256Digest

    def __init__(self) -> None:
        raise TypeError("authorization authorities are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._binding.reference.run_id

    @property
    def instrument_spec_set_id(self) -> InstrumentSpecSetId:
        return self._spec_set.identifier

    @property
    def instrument_spec_set_sha256(self) -> Sha256Digest:
        return self._spec_sha256

    @property
    def execution_policy(self) -> ExecutionPolicyRef:
        return self._execution_policy

    def activate(
        self,
        coordinator: _CoordinatorAuthorizationView,
        *,
        seal: object,
    ) -> None:
        """Perform the private one-use second phase after coordinator construction."""
        if self._active or self._coordinator is not None:
            raise _deny(OutcomeCode.CONFLICTING_ID, "authorization authority already activated")
        if seal is not self._activation_seal or self._activation_seal is None:
            raise _deny(OutcomeCode.CONFLICTING_ID, "authorization activation seal conflicts")
        self._activation_seal = None
        if coordinator.binding != self._binding:
            raise _deny(OutcomeCode.CONFLICTING_ID, "coordinator binding conflicts")
        self._coordinator = coordinator
        self._active = True
        for key, attempt in tuple(self._attempts.items()):
            causal_root = attempt.causal_market_root
            if causal_root is None:
                continue
            burned = True
            try:
                freshness = self._freshness(
                    attempt.order,
                    causal_market_root=causal_root,
                    dispatch_sequence=attempt.order.dispatch_sequence,
                )
                burned = (
                    self._payload(
                        attempt.order,
                        causal_market_root=causal_root,
                        dispatch_sequence=attempt.order.dispatch_sequence,
                        freshness=freshness,
                    )
                    != attempt.payload
                )
            except Exception:
                burned = True
            self._attempts[key] = _AuthorizationAttempt(
                attempt.order,
                causal_root,
                attempt.payload,
                attempt.acknowledgement,
                burned,
            )

    def recover_attempts(
        self,
        records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
        *,
        orders: AuthorizationOrderRecoveryResolver,
        submissions: AuthorizationSubmissionRecoveryResolver,
        seal: object,
    ) -> None:
        """Rebuild the permanent attempt index before sealed activation."""
        if (
            seal is not self._activation_seal
            or self._activation_seal is None
            or self._active
            or self._coordinator is not None
            or self._recovery_loaded
        ):
            raise _deny(OutcomeCode.CONFLICTING_ID, "authorization recovery seal conflicts")
        if not isinstance(records, tuple):
            try:
                if records.record_count < 1 or records.binding != self._binding:
                    raise _deny(
                        OutcomeCode.CONFLICTING_ID,
                        "authorization recovery record source conflicts",
                    )
            except AttributeError as error:
                raise _deny(
                    OutcomeCode.INVALID_TYPE,
                    "authorization recovery records are invalid",
                ) from error
        attempts: dict[tuple[EconomicId, Sha256Digest], _AuthorizationAttempt] = {}
        by_order: dict[EconomicId, Sha256Digest] = {}
        for record in records:
            if type(record) is not AuditRecord:
                raise _deny(
                    OutcomeCode.INVALID_TYPE,
                    "authorization recovery records are invalid",
                )
            if record.record_kind is not AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
                continue
            if record.binding != self._binding:
                raise _deny(OutcomeCode.CONFLICTING_ID, "authorization record binding conflicts")
            try:
                document = json.loads(record.canonical_payload)
                if type(document) is not dict:
                    raise ValueError("authorization payload is not one object")
                order_id = _economic_id_from_document(document["order_id"], self.run_id)
                held_for_order_id = _economic_id_from_document(
                    document["held_for_order_id"],
                    self.run_id,
                )
                request_sha256 = Sha256Digest(document["execution_request_sha256"])
            except (KeyError, TypeError, ValueError) as error:
                raise _deny(
                    OutcomeCode.CONFLICTING_ID,
                    "authorization recovery payload is invalid",
                ) from error
            if held_for_order_id != order_id:
                raise _deny(OutcomeCode.CONFLICTING_ID, "authorization held order conflicts")
            existing_request = by_order.get(order_id)
            key = (order_id, request_sha256)
            if existing_request is not None or key in attempts:
                raise _deny(OutcomeCode.CONFLICTING_ID, "authorization recovery key is duplicated")
            order = orders.resolve_issued_order_by_id(order_id)
            if (
                type(order) is not Order
                or order.run_id != self.run_id
                or order_digest(order).value != document.get("order_sha256")
                or execution_request_digest(order) != request_sha256
                or order.instrument_spec_set_id != self._spec_set.identifier
                or order.instrument_spec_set_sha256 != self._spec_sha256
                or order.execution_policy != self._execution_policy
                or order.dispatch_sequence != document.get("dispatch_sequence")
            ):
                raise _deny(OutcomeCode.CONFLICTING_ID, "authorization Order evidence conflicts")
            acknowledgement = create_audit_append_acknowledgement(record)
            require_audit_acknowledgement(
                acknowledgement,
                binding=self._binding,
                logical_key=record.logical_key,
                canonical_payload=record.canonical_payload,
            )
            receipt = submissions.resolve_submission_receipt(
                order_id=order_id,
                execution_request_sha256=request_sha256,
            )
            causal_root: MarketDataEnvelope | None = None
            if receipt is not None:
                recovered_payload = _authorization_payload_from_receipt(
                    receipt,
                    acknowledgement,
                    order,
                )
                if recovered_payload != record.canonical_payload:
                    raise _deny(
                        OutcomeCode.CONFLICTING_ID,
                        "authorization receipt evidence conflicts",
                    )
            else:
                active = self._runtime.active_lease
                if (
                    active is not None
                    and type(active.root) is MarketDataEnvelope
                    and active.dispatch_sequence == document.get("dispatch_sequence")
                    and causal_market_digest(active.root).value
                    == document.get("causal_market_sha256")
                    and runtime_root_key_document(active.root) == document.get("causal_root_key")
                ):
                    causal_root = active.root
            attempts[key] = _AuthorizationAttempt(
                order,
                causal_root,
                record.canonical_payload,
                acknowledgement,
                True,
            )
            by_order[order_id] = request_sha256
        self._attempts = attempts
        self._attempt_by_order = by_order
        self._recovery_loaded = True

    def _require_active(self) -> _CoordinatorAuthorizationView:
        if not self._active or self._coordinator is None:
            raise _deny(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "authorization authority is dormant",
            )
        if self._coordinator.binding != self._binding:
            raise _deny(OutcomeCode.CONFLICTING_ID, "coordinator binding changed")
        return self._coordinator

    def _freshness(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
    ) -> _Freshness:
        coordinator = self._require_active()
        active = self._runtime.active_lease
        if (
            active is None
            or active.root is not causal_market_root
            or active.dispatch_sequence != dispatch_sequence
        ):
            raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "causal runtime lease is stale")
        portfolio = self._portfolio.current_snapshot()
        risk = self._risk.current_state()
        global_halt = self._global_halt.current_state()
        gate = self._instrument_gate.current_for(order.instrument)
        if (
            type(portfolio) is not PortfolioSnapshot
            or type(risk) is not RiskStateSnapshot
            or type(global_halt) is not GlobalHaltSnapshot
            or type(gate) is not InstrumentGateSnapshot
        ):
            raise _deny(OutcomeCode.INVALID_TYPE, "freshness ports returned invalid carriers")
        if (
            portfolio.run_id != self.run_id
            or risk.run_id != self.run_id
            or global_halt.run_id != self.run_id
            or gate.run_id != self.run_id
            or gate.instrument != order.instrument
            or portfolio.instrument_spec_set_id != self._spec_set.identifier
            or portfolio.instrument_spec_set_sha256 != self._spec_sha256
            or portfolio.snapshot_version != order.portfolio_snapshot_version
            or risk.risk_state_version != order.risk_state_version
        ):
            raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "portfolio or risk approval is stale")
        if (
            risk.halted
            or global_halt.halted
            or gate.halted
            or gate.held_for_order_id != order.order_id
        ):
            raise _deny(OutcomeCode.SUBMISSION_BLOCKED_BY_HALT, "submission gate is not held")
        state_version = getattr(coordinator.state, "state_version", None)
        phase = getattr(coordinator.state, "phase", None)
        if type(state_version) is not int or phase is not CoordinatorPhase.RUNNING:
            raise _deny(OutcomeCode.CONFLICTING_ID, "coordinator state is invalid")
        return _Freshness(portfolio, risk, global_halt, gate, state_version)

    def _payload(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
        freshness: _Freshness,
    ) -> bytes:
        portfolio = freshness.portfolio
        risk = freshness.risk
        global_halt = freshness.global_halt
        gate = freshness.gate
        held_for_order_id = gate.held_for_order_id
        if held_for_order_id is None:
            raise _deny(OutcomeCode.SUBMISSION_BLOCKED_BY_HALT, "instrument gate is not held")
        return _canonical_json(
            {
                "authorization_state_version": freshness.state_version,
                "canonicalization": "ea-canonical-json-v1",
                "causal_market_sha256": causal_market_digest(causal_market_root).value,
                "causal_root_key": runtime_root_key_document(causal_market_root),
                "dispatch_sequence": dispatch_sequence,
                "execution_policy_id": self._execution_policy.identifier.value,
                "execution_policy_sha256": self._execution_policy.sha256.value,
                "execution_request_sha256": execution_request_digest(order).value,
                "global_halt_epoch": global_halt.global_halt_epoch,
                "held_for_order_id": _economic_id_document(held_for_order_id),
                "instrument_gate_id": gate.instrument_gate_id.value,
                "instrument_gate_version": gate.instrument_gate_version,
                "instrument_spec_set_id": self._spec_set.identifier.value,
                "instrument_spec_set_sha256": self._spec_sha256.value,
                "order_id": _economic_id_document(order.order_id),
                "order_sha256": order_digest(order).value,
                "portfolio_snapshot_version": portfolio.snapshot_version,
                "risk_halt_epoch": risk.risk_state_version,
                "risk_state_version": risk.risk_state_version,
                "run_id": self.run_id.value,
                "schema": "ea.audit-submission-authorization.v1",
            }
        )

    def prepare(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
        capability: object,
    ) -> AuditAppendAcknowledgement:
        """Append at most one durable attempt for an Order/request key."""
        if capability is not self._preparation_capability:
            raise _deny(
                OutcomeCode.CONFLICTING_ID, "authorization preparation capability conflicts"
            )
        if type(order) is not Order or type(causal_market_root) is not MarketDataEnvelope:
            raise _deny(OutcomeCode.INVALID_TYPE, "authorization inputs must be exact")
        if (
            order.run_id != self.run_id
            or order.instrument_spec_set_id != self._spec_set.identifier
            or order.instrument_spec_set_sha256 != self._spec_sha256
            or order.execution_policy != self._execution_policy
            or order.dispatch_sequence != dispatch_sequence
        ):
            raise _deny(OutcomeCode.CONFLICTING_ID, "Order binding conflicts")
        before = self._freshness(
            order,
            causal_market_root=causal_market_root,
            dispatch_sequence=dispatch_sequence,
        )
        payload = self._payload(
            order,
            causal_market_root=causal_market_root,
            dispatch_sequence=dispatch_sequence,
            freshness=before,
        )
        key = (order.order_id, execution_request_digest(order))
        occupied_request = self._attempt_by_order.get(order.order_id)
        if occupied_request is not None and occupied_request != key[1]:
            raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "authorization Order is occupied")
        existing = self._attempts.get(key)
        if existing is not None:
            if existing.payload != payload:
                raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "authorization attempt is occupied")
            if existing.burned:
                raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "authorization attempt is burned")
            return existing.acknowledgement
        subject = audit_subject_digest(
            AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
            payload,
        )
        acknowledgement = self._audit.append(
            record_kind=AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
            subject_kind=AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST,
            subject_sha256=subject,
            canonical_payload=payload,
        )
        require_audit_acknowledgement(
            acknowledgement,
            binding=self._binding,
            logical_key=AuditLogicalKey(
                AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
                AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST,
                subject,
            ),
            canonical_payload=payload,
        )
        burned = False
        try:
            after = self._freshness(
                order,
                causal_market_root=causal_market_root,
                dispatch_sequence=dispatch_sequence,
            )
            if after != before:
                burned = True
        except Exception:
            burned = True
        self._attempts[key] = _AuthorizationAttempt(
            order,
            causal_market_root,
            payload,
            acknowledgement,
            burned,
        )
        self._attempt_by_order[order.order_id] = key[1]
        if burned:
            raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "freshness changed after append")
        return acknowledgement

    def verify_authorized_historical_submission(
        self,
        *,
        order_id: EconomicId,
        canonical_order_bytes: bytes,
        canonical_execution_request_bytes: bytes,
        canonical_causal_market_bytes: bytes,
        causal_market_sha256: Sha256Digest,
        causal_root_key: RuntimeRootOrderKey,
        dispatch_sequence: int,
    ) -> HistoricalSubmissionAuthorizationProof:
        """Return the existing opaque matcher proof only while every binding remains fresh."""
        self._require_active()
        if (
            type(order_id) is not EconomicId
            or type(canonical_order_bytes) is not bytes
            or type(canonical_execution_request_bytes) is not bytes
            or type(canonical_causal_market_bytes) is not bytes
            or type(causal_market_sha256) is not Sha256Digest
            or type(causal_root_key) is not RuntimeRootOrderKey
            or type(dispatch_sequence) is not int
        ):
            raise _deny(OutcomeCode.INVALID_TYPE, "verification inputs must be exact")
        retained_request_digest = self._attempt_by_order.get(order_id)
        attempt_entry = (
            None
            if retained_request_digest is None
            else self._attempts.get((order_id, retained_request_digest))
        )
        order = None if attempt_entry is None else attempt_entry.order
        if order is None:
            raise _deny(OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED, "authorization is missing")
        request_digest = execution_request_digest(order)
        attempt = self._attempts.get((order_id, request_digest))
        if attempt is None or attempt.burned:
            raise _deny(OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED, "authorization is unavailable")
        require_audit_acknowledgement(
            attempt.acknowledgement,
            binding=self._binding,
            logical_key=AuditLogicalKey(
                AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
                AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST,
                audit_subject_digest(
                    AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
                    attempt.payload,
                ),
            ),
            canonical_payload=attempt.payload,
        )
        active = self._runtime.active_lease
        if active is None or type(active.root) is not MarketDataEnvelope:
            raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "causal runtime lease is unavailable")
        causal_root = active.root
        if (
            canonical_order_bytes != canonical_order_bytes_fn(order)
            or canonical_execution_request_bytes != canonical_execution_request_bytes_fn(order)
            or canonical_causal_market_bytes != canonical_market_data_record_bytes(causal_root)
            or causal_market_sha256 != historical_market_root_digest(causal_root)
            or causal_root_key != runtime_root_order_key(causal_root)
        ):
            raise _deny(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH, "authorization evidence mismatches"
            )
        freshness = self._freshness(
            order,
            causal_market_root=causal_root,
            dispatch_sequence=dispatch_sequence,
        )
        if (
            self._payload(
                order,
                causal_market_root=causal_root,
                dispatch_sequence=dispatch_sequence,
                freshness=freshness,
            )
            != attempt.payload
        ):
            self._attempts[(order_id, request_digest)] = _AuthorizationAttempt(
                attempt.order,
                attempt.causal_market_root,
                attempt.payload,
                attempt.acknowledgement,
                True,
            )
            raise _deny(OutcomeCode.RISK_STALE_APPROVAL, "authorization state changed")
        risk = freshness.risk
        global_halt = freshness.global_halt
        gate = freshness.gate
        held_for_order_id = gate.held_for_order_id
        if held_for_order_id is None:
            raise _deny(OutcomeCode.SUBMISSION_BLOCKED_BY_HALT, "instrument gate is not held")
        return _create_historical_submission_authorization_proof(
            run_id=self.run_id,
            spec_set=self._spec_set,
            execution_policy=self._execution_policy,
            order=order,
            order_sha256=order_digest(order),
            execution_request_sha256=request_digest,
            causal_market_sha256=causal_market_sha256,
            causal_root_key=causal_root_key,
            dispatch_sequence=dispatch_sequence,
            audit_acknowledgement_id=audit_acknowledgement_id(attempt.acknowledgement),
            audit_acknowledgement_sha256=audit_append_acknowledgement_digest(
                attempt.acknowledgement
            ),
            global_halt_epoch=global_halt.global_halt_epoch,
            risk_halt_epoch=risk.risk_state_version,
            instrument_gate_id=gate.instrument_gate_id.value,
            instrument_gate_version=gate.instrument_gate_version,
            held_for_order_id=held_for_order_id,
            authorization_state_version=freshness.state_version,
            issuer=self,
        )


# Aliases prevent parameter names in the verifier from shadowing imported codec functions.
canonical_order_bytes_fn = canonical_order_bytes
canonical_execution_request_bytes_fn = canonical_execution_request_bytes


def create_dormant_historical_submission_authorization_authority(
    *,
    binding: RunBinding,
    audit: AuditAppendPort,
    runtime: RuntimeLifecyclePort,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    portfolio: PortfolioFreshnessPort,
    risk: RiskFreshnessPort,
    global_halt: GlobalHaltFreshnessPort,
    instrument_gate: InstrumentGateFreshnessPort,
) -> tuple[HistoricalSubmissionAuthorizationAuthority, object, object]:
    """Create one unpublished dormant verifier for sealed joint construction."""
    if (
        type(binding) is not RunBinding
        or type(spec_set) is not InstrumentExecutionSpecSet
        or type(execution_policy) is not ExecutionPolicyRef
        or audit.binding != binding
        or runtime.run_id != binding.reference.run_id
        or instrument_spec_set_digest(runtime.spec_set) != instrument_spec_set_digest(spec_set)
    ):
        raise _deny(OutcomeCode.CONFLICTING_ID, "authorization static bindings conflict")
    value = object.__new__(HistoricalSubmissionAuthorizationAuthority)
    value._binding = binding
    value._audit = audit
    value._runtime = runtime
    value._spec_set = spec_set
    value._spec_sha256 = instrument_spec_set_digest(spec_set)
    value._execution_policy = execution_policy
    value._portfolio = portfolio
    value._risk = risk
    value._global_halt = global_halt
    value._instrument_gate = instrument_gate
    value._attempts = {}
    value._attempt_by_order = {}
    value._coordinator = None
    value._active = False
    value._recovery_loaded = False
    value._preparation_capability = _PreparationCapability()
    value._activation_seal = _ActivationSeal()
    return value, value._preparation_capability, value._activation_seal
