"""Deterministic Phase 1 historical matcher and simulated venue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Protocol, final

from ea.core.economics import CanonicalDecimal, EconomicValidationError, require_positive
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    PriceDomain,
    SettlementCurrency,
    build_instrument_spec_set,
    canonical_instrument_spec_set_bytes,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import (
    EconomicId,
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
    canonical_order_bytes,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    create_trade_execution_fact,
    execution_fact_digest,
    execution_fact_ingress_digest,
    execution_request_digest,
    order_client_submission_key,
    order_digest,
)
from ea.core.historical_matching import (
    HistoricalDispatchKind,
    HistoricalMatcherConflictEvidence,
    HistoricalMatcherConflictKind,
    HistoricalMatcherDescendantBinding,
    HistoricalMatcherDispatchBatch,
    HistoricalMatcherError,
    HistoricalMatcherState,
    HistoricalPreEffectAuthorizationError,
    HistoricalSubmissionAuthorizationProof,
    HistoricalSubmissionReceipt,
    _create_historical_matcher_conflict,
    _create_historical_matcher_descendant_binding,
    _create_historical_matcher_dispatch_batch,
    _create_historical_matcher_state,
    _create_historical_submission_receipt,
    _require_historical_submission_authorization_proof,
    canonical_end_of_run_root_bytes,
    canonical_historical_matcher_conflict_bytes,
    canonical_historical_matcher_dispatch_batch_bytes,
    canonical_historical_matcher_state_bytes,
    canonical_historical_submission_receipt_bytes,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_conflict_digest,
    historical_matcher_dispatch_batch_digest,
    historical_matcher_observation_digest,
    historical_matcher_state_digest,
    historical_submission_receipt_digest,
)
from ea.core.identity import Instrument, VenueId
from ea.core.market_data import Adjustment, MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.runtime import (
    ActiveEndOfRunDispatchProof,
    ActiveMarketDispatchProof,
    EndOfRunKind,
    EndOfRunRoot,
    RuntimeOrderingError,
    RuntimeRootOrderKey,
    _require_active_end_of_run_dispatch_proof,
    _require_active_market_dispatch_proof,
    runtime_root_order_key,
)
from ea.core.strategy import causal_market_digest

_MAX_UINT64 = (1 << 64) - 1


class _VerifierFailure(Exception):
    """Preserve an unexpected verifier exception across matcher translation."""

    error: Exception

    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(str(error))


class HistoricalOrderIssuanceVerifier(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    @property
    def execution_policy(self) -> ExecutionPolicyRef: ...

    def resolve_issued_order_by_id(self, order_id: EconomicId) -> Order | None: ...


class HistoricalSubmissionAuthorizationVerifier(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def instrument_spec_set_id(self) -> InstrumentSpecSetId: ...

    @property
    def instrument_spec_set_sha256(self) -> Sha256Digest: ...

    @property
    def execution_policy(self) -> ExecutionPolicyRef: ...

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
    ) -> HistoricalSubmissionAuthorizationProof: ...


class HistoricalMatcherDispatchVerifier(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> ActiveMarketDispatchProof: ...

    def verify_active_end_of_run_dispatch(
        self,
        end_root: EndOfRunRoot,
        *,
        dispatch_sequence: int,
    ) -> ActiveEndOfRunDispatchProof: ...


@dataclass(frozen=True, slots=True)
class _SubmissionRecord:
    order: Order
    order_bytes: bytes
    order_sha256: Sha256Digest
    request_bytes: bytes
    request_sha256: Sha256Digest
    client_key: Sha256Digest
    causal_market_bytes: bytes
    causal_market_sha256: Sha256Digest
    causal_root_key: RuntimeRootOrderKey
    dispatch_sequence: int
    receipt: HistoricalSubmissionReceipt
    receipt_bytes: bytes
    receipt_sha256: Sha256Digest


@dataclass(frozen=True, slots=True)
class _DispatchRecord:
    root_bytes: bytes
    root_sha256: Sha256Digest
    root_key: RuntimeRootOrderKey
    batch: HistoricalMatcherDispatchBatch
    batch_bytes: bytes
    batch_sha256: Sha256Digest


@dataclass(frozen=True, slots=True)
class _IssuedRecord:
    ingress: ExecutionFactIngress
    ingress_bytes: bytes
    ingress_sha256: Sha256Digest
    fact_bytes: bytes
    binding: HistoricalMatcherDescendantBinding


@dataclass(frozen=True, slots=True)
class _MatcherState:
    next_submission: int | None
    next_fact: int | None
    submissions: tuple[_SubmissionRecord, ...]
    pending: tuple[_SubmissionRecord, ...]
    submission_by_order: MappingProxyType[EconomicId, _SubmissionRecord]
    submission_by_client: MappingProxyType[Sha256Digest, _SubmissionRecord]
    dispatch_by_sequence: MappingProxyType[int, _DispatchRecord]
    dispatch_by_digest: MappingProxyType[Sha256Digest, _DispatchRecord]
    issued: tuple[_IssuedRecord, ...]
    issued_by_identity: MappingProxyType[IngressIdentity, _IssuedRecord]
    last_dispatch: int | None
    ended: bool
    end_batch_sha256: Sha256Digest | None
    conflict: HistoricalMatcherConflictEvidence | None


def _fail(code: OutcomeCode, message: str) -> HistoricalMatcherError:
    return HistoricalMatcherError(code, message)


def _advance(value: int) -> int | None:
    return None if value == _MAX_UINT64 else value + 1


def _require_dispatch_sequence(value: object) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatch_sequence must be exact int")
    if not 1 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch_sequence must be positive uint64")
    return value


def _clone_instrument(value: Instrument) -> Instrument:
    return Instrument(VenueId(value.venue.code), value.symbol)


def _clone_spec_set(value: InstrumentExecutionSpecSet) -> InstrumentExecutionSpecSet:
    if type(value) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    try:
        owned = build_instrument_spec_set(
            InstrumentSpecSetId(value.identifier.value),
            (
                InstrumentExecutionSpec(
                    instrument=_clone_instrument(spec.instrument),
                    specification_id=InstrumentSpecId(spec.specification_id.value),
                    price_quantum=CanonicalDecimal(spec.price_quantum.text),
                    quantity_quantum=CanonicalDecimal(spec.quantity_quantum.text),
                    settlement_currency=SettlementCurrency(spec.settlement_currency.code),
                    currency_quantum=CanonicalDecimal(spec.currency_quantum.text),
                    contract_multiplier=CanonicalDecimal(spec.contract_multiplier.text),
                    price_domain=PriceDomain(spec.price_domain.value),
                )
                for spec in value.specifications
            ),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set cannot be reconstructed") from error
    if canonical_instrument_spec_set_bytes(owned) != canonical_instrument_spec_set_bytes(
        value
    ) or instrument_spec_set_digest(owned) != instrument_spec_set_digest(value):
        raise _fail(OutcomeCode.CONFLICTING_ID, "spec_set reconstruction conflicts")
    return owned


def _clone_policy(value: ExecutionPolicyRef) -> ExecutionPolicyRef:
    if type(value) is not ExecutionPolicyRef:
        raise _fail(OutcomeCode.INVALID_TYPE, "execution_policy must be exact")
    return ExecutionPolicyRef(
        ExecutionPolicyId(value.identifier.value),
        Sha256Digest(value.sha256.value),
    )


def _clone_order(value: Order) -> Order:
    """Own an immutable scalar reconstruction after authority membership was proved."""
    owned = object.__new__(Order)
    for name in Order.__dataclass_fields__:
        submitted = getattr(value, name)
        if type(submitted) is Instrument:
            submitted = _clone_instrument(submitted)
        elif type(submitted) is CanonicalDecimal:
            submitted = CanonicalDecimal(submitted.text)
        elif type(submitted) is ExecutionPolicyRef:
            submitted = _clone_policy(submitted)
        object.__setattr__(owned, name, submitted)
    if canonical_order_bytes(owned) != canonical_order_bytes(value):
        raise _fail(OutcomeCode.CONFLICTING_ID, "owned Order reconstruction conflicts")
    return owned


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


def _quantized_close(
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


@final
class Phase1HistoricalMatcher:
    __slots__ = (
        "_active_dispatch_verifier",
        "_execution_policy",
        "_order_issuance_verifier",
        "_provenance_id",
        "_run_id",
        "_source_namespace",
        "_spec_bytes",
        "_spec_set",
        "_spec_sha256",
        "_state",
        "_submission_authorization_verifier",
    )
    _active_dispatch_verifier: HistoricalMatcherDispatchVerifier
    _execution_policy: ExecutionPolicyRef
    _order_issuance_verifier: HistoricalOrderIssuanceVerifier
    _provenance_id: FactProvenanceId
    _run_id: RunId
    _source_namespace: SourceNamespace
    _spec_bytes: bytes
    _spec_set: InstrumentExecutionSpecSet
    _spec_sha256: Sha256Digest
    _state: _MatcherState
    _submission_authorization_verifier: HistoricalSubmissionAuthorizationVerifier

    def __init__(self) -> None:
        raise TypeError("matchers are created only by create_phase1_historical_matcher")

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    @property
    def source_namespace(self) -> SourceNamespace:
        return self._source_namespace

    @property
    def execution_policy(self) -> ExecutionPolicyRef:
        return self._execution_policy

    @property
    def provenance_id(self) -> FactProvenanceId:
        return self._provenance_id

    @property
    def state(self) -> HistoricalMatcherState:
        state = self._state
        for submission_record in state.submissions:
            self._require_submission_record(submission_record)
        for _, dispatch_record in sorted(state.dispatch_by_sequence.items()):
            self._require_dispatch_record(dispatch_record)
        for issued_record in state.issued:
            self._require_issued_record(issued_record)
        public = _create_historical_matcher_state(
            run_id=self._run_id,
            source_namespace=self._source_namespace,
            provenance_id=self._provenance_id,
            instrument_spec_set_id=self._spec_set.identifier,
            instrument_spec_set_sha256=self._spec_sha256,
            execution_policy=self._execution_policy,
            next_submission_sequence=state.next_submission,
            next_fact_sequence=state.next_fact,
            receipt_sha256s=tuple(record.receipt_sha256 for record in state.submissions),
            pending_order_ids=tuple(
                record.order.order_id
                for record in sorted(
                    state.pending,
                    key=lambda item: item.receipt.submission_sequence,
                )
            ),
            issued_ingresses=tuple(record.ingress for record in state.issued),
            dispatch_batch_sha256s=tuple(
                record.batch_sha256 for _, record in sorted(state.dispatch_by_sequence.items())
            ),
            last_new_dispatch_sequence=state.last_dispatch,
            ended=state.ended,
            end_batch_sha256=state.end_batch_sha256,
            halted=state.conflict is not None,
            conflict=state.conflict,
        )
        canonical_historical_matcher_state_bytes(public)
        historical_matcher_state_digest(public)
        return public

    def _require_live_bindings(self) -> None:
        if (
            canonical_instrument_spec_set_bytes(self._spec_set) != self._spec_bytes
            or instrument_spec_set_digest(self._spec_set) != self._spec_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "owned spec binding changed")
        for verifier in (
            self._order_issuance_verifier,
            self._submission_authorization_verifier,
        ):
            try:
                run_id = verifier.run_id
                policy = verifier.execution_policy
            except (AttributeError, TypeError) as error:
                raise _fail(OutcomeCode.INVALID_TYPE, "verifier binding failed") from error
            if type(run_id) is not RunId or type(policy) is not ExecutionPolicyRef:
                raise _fail(OutcomeCode.INVALID_TYPE, "verifier bindings must be exact")
            if run_id != self._run_id or policy != self._execution_policy:
                raise _fail(OutcomeCode.CONFLICTING_ID, "verifier binding changed")
        try:
            order_specs = self._order_issuance_verifier.spec_set
            auth_spec_id = self._submission_authorization_verifier.instrument_spec_set_id
            auth_spec_digest = self._submission_authorization_verifier.instrument_spec_set_sha256
            dispatch_run_id = self._active_dispatch_verifier.run_id
            dispatch_specs = self._active_dispatch_verifier.spec_set
        except (AttributeError, TypeError) as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "verifier spec binding failed") from error
        if (
            type(order_specs) is not InstrumentExecutionSpecSet
            or canonical_instrument_spec_set_bytes(order_specs) != self._spec_bytes
            or instrument_spec_set_digest(order_specs) != self._spec_sha256
            or type(auth_spec_id) is not InstrumentSpecSetId
            or type(auth_spec_digest) is not Sha256Digest
            or auth_spec_id != self._spec_set.identifier
            or auth_spec_digest != self._spec_sha256
            or type(dispatch_run_id) is not RunId
            or dispatch_run_id != self._run_id
            or type(dispatch_specs) is not InstrumentExecutionSpecSet
            or canonical_instrument_spec_set_bytes(dispatch_specs) != self._spec_bytes
            or instrument_spec_set_digest(dispatch_specs) != self._spec_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "verifier spec binding changed")

    def _retained_binding_drift(self, *, dispatch_sequence: object = None) -> None:
        sequence = (
            dispatch_sequence
            if type(dispatch_sequence) is int and 1 <= dispatch_sequence <= _MAX_UINT64
            else None
        )
        self._publish_conflict(
            kind=HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT,
            occupied_identity=None,
            existing_sha256=None,
            submitted_sha256=None,
            submitted_dispatch_sequence=sequence,
            trigger_root_sha256=None,
        )

    def _require_submission_record(self, record: _SubmissionRecord) -> None:
        try:
            receipt = record.receipt
            valid = (
                type(record) is _SubmissionRecord
                and type(record.order) is Order
                and canonical_order_bytes(record.order) == record.order_bytes
                and order_digest(record.order) == record.order_sha256
                and canonical_execution_request_bytes(record.order) == record.request_bytes
                and execution_request_digest(record.order) == record.request_sha256
                and order_client_submission_key(record.order) == record.client_key
                and type(receipt) is HistoricalSubmissionReceipt
                and canonical_historical_submission_receipt_bytes(receipt) == record.receipt_bytes
                and historical_submission_receipt_digest(receipt) == record.receipt_sha256
                and receipt.run_id == self._run_id
                and receipt.source_namespace == self._source_namespace
                and receipt.order_id == record.order.order_id
                and receipt.order_sha256 == record.order_sha256
                and receipt.execution_request_sha256 == record.request_sha256
                and receipt.client_submission_key == record.client_key
                and receipt.instrument == record.order.instrument
                and receipt.side is record.order.side
                and receipt.quantity_text == record.order.quantity.text
                and receipt.causal_market_sha256 == record.causal_market_sha256
                and receipt.causal_root_key == record.causal_root_key
                and receipt.dispatch_sequence == record.dispatch_sequence
                and receipt.eligible_after_available_at == record.order.eligible_after_available_at
                and receipt.instrument_spec_set_id == self._spec_set.identifier
                and receipt.instrument_spec_set_sha256 == self._spec_sha256
                and receipt.execution_policy == self._execution_policy
            )
        except Exception:
            valid = False
        if not valid:
            self._retained_binding_drift(dispatch_sequence=record.dispatch_sequence)

    def _require_dispatch_record(self, record: _DispatchRecord) -> None:
        try:
            batch = record.batch
            valid = (
                type(record) is _DispatchRecord
                and type(batch) is HistoricalMatcherDispatchBatch
                and canonical_historical_matcher_dispatch_batch_bytes(batch) == record.batch_bytes
                and historical_matcher_dispatch_batch_digest(batch) == record.batch_sha256
                and batch.run_id == self._run_id
                and batch.source_namespace == self._source_namespace
                and batch.dispatch_sequence in self._state.dispatch_by_sequence
                and self._state.dispatch_by_sequence[batch.dispatch_sequence] is record
                and batch.trigger_root_sha256 == record.root_sha256
                and batch.trigger_root_key == record.root_key
            )
        except Exception:
            valid = False
        if not valid:
            sequence = getattr(record.batch, "dispatch_sequence", None)
            self._retained_binding_drift(dispatch_sequence=sequence)

    def _require_issued_record(self, record: _IssuedRecord) -> None:
        try:
            ingress = record.ingress
            binding = record.binding
            dispatch = self._state.dispatch_by_sequence.get(binding.parent_dispatch_sequence)
            valid = (
                type(record) is _IssuedRecord
                and type(ingress) is ExecutionFactIngress
                and type(binding) is HistoricalMatcherDescendantBinding
                and canonical_execution_fact_ingress_bytes(ingress) == record.ingress_bytes
                and execution_fact_ingress_digest(ingress) == record.ingress_sha256
                and canonical_execution_fact_bytes(ingress.fact) == record.fact_bytes
                and binding.ingress_identity == ingress.identity
                and binding.ingress_sha256 == record.ingress_sha256
                and binding.fact_sha256 == execution_fact_digest(ingress.fact)
                and dispatch is not None
                and binding.batch_sha256 == dispatch.batch_sha256
                and binding.parent_kind is dispatch.batch.dispatch_kind
                and binding.parent_root_sha256 == dispatch.root_sha256
                and binding.parent_root_key == dispatch.root_key
                and binding.parent_dispatch_sequence == dispatch.batch.dispatch_sequence
                and type(binding.batch_index) is int
                and 0 <= binding.batch_index < len(dispatch.batch.ingresses)
                and dispatch.batch.ingresses[binding.batch_index] is ingress
                and dispatch.batch.ingress_sha256s[binding.batch_index] == record.ingress_sha256
                and self._state.issued_by_identity.get(ingress.identity) is record
            )
        except Exception:
            valid = False
        if not valid:
            sequence = getattr(record.binding, "parent_dispatch_sequence", None)
            self._retained_binding_drift(dispatch_sequence=sequence)

    def _publish_conflict(
        self,
        *,
        kind: HistoricalMatcherConflictKind,
        occupied_identity: dict[str, object] | None,
        existing_sha256: Sha256Digest | None,
        submitted_sha256: Sha256Digest | None,
        submitted_dispatch_sequence: int | None,
        trigger_root_sha256: Sha256Digest | None,
    ) -> None:
        if self._state.conflict is None:
            conflict = _create_historical_matcher_conflict(
                run_id=self._run_id,
                conflict_kind=kind,
                occupied_identity=occupied_identity,
                existing_sha256=existing_sha256,
                submitted_sha256=submitted_sha256,
                submitted_dispatch_sequence=submitted_dispatch_sequence,
                last_successful_dispatch_sequence=self._state.last_dispatch,
                pending_count=len(self._state.pending),
                next_submission_sequence=self._state.next_submission,
                next_fact_sequence=self._state.next_fact,
                trigger_root_sha256=trigger_root_sha256,
            )
            canonical_historical_matcher_conflict_bytes(conflict)
            historical_matcher_conflict_digest(conflict)
            self._state = _MatcherState(
                **{
                    name: getattr(self._state, name)
                    for name in _MatcherState.__dataclass_fields__
                    if name != "conflict"
                },
                conflict=conflict,
            )
        raise _fail(OutcomeCode.CONFLICTING_ID, "matcher identity conflict")

    def submit(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
    ) -> HistoricalSubmissionReceipt:
        if type(order) is not Order or type(causal_market_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.INVALID_TYPE, "submission carriers must be exact")
        sequence = _require_dispatch_sequence(dispatch_sequence)
        try:
            submitted_order_bytes = canonical_order_bytes(order)
            submitted_order_sha256 = order_digest(order)
            request_bytes = canonical_execution_request_bytes(order)
            request_sha256 = execution_request_digest(order)
            client_key = order_client_submission_key(order)
            causal_bytes = canonical_market_data_record_bytes(causal_market_root)
            causal_sha256 = historical_market_root_digest(causal_market_root)
            causal_key = runtime_root_order_key(causal_market_root)
        except HistoricalMatcherError:
            raise
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "submission cannot be canonicalized") from error

        existing = self._state.submission_by_order.get(order.order_id)
        if existing is not None:
            self._require_submission_record(existing)
            if (
                existing.order_bytes == submitted_order_bytes
                and existing.causal_market_bytes == causal_bytes
                and existing.dispatch_sequence == sequence
            ):
                return existing.receipt
            self._publish_conflict(
                kind=HistoricalMatcherConflictKind.SUBMISSION_IDENTITY,
                occupied_identity={
                    "kind": "order_id",
                    "order_id": {
                        "owner_kind": order.order_id.owner_kind.value,
                        "owner_sequence": order.order_id.owner_sequence,
                        "run_id": order.order_id.run_id.value,
                    },
                },
                existing_sha256=existing.order_sha256,
                submitted_sha256=submitted_order_sha256,
                submitted_dispatch_sequence=sequence,
                trigger_root_sha256=causal_sha256,
            )
        existing = self._state.submission_by_client.get(client_key)
        if existing is not None:
            self._require_submission_record(existing)
            self._publish_conflict(
                kind=HistoricalMatcherConflictKind.CLIENT_SUBMISSION_KEY,
                occupied_identity={
                    "kind": "client_submission_key",
                    "sha256": client_key.value,
                },
                existing_sha256=existing.order_sha256,
                submitted_sha256=submitted_order_sha256,
                submitted_dispatch_sequence=sequence,
                trigger_root_sha256=causal_sha256,
            )
        if self._state.conflict is not None:
            raise _fail(OutcomeCode.SUBMISSION_BLOCKED_BY_HALT, "matcher is halted")
        if self._state.ended:
            raise _fail(OutcomeCode.CONFLICTING_ID, "matcher already ended")
        if (
            order.order_kind is not OrderKind.MARKET
            or order.time_in_force is not TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
            or order.price_constraint is not None
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "Order is outside Phase 1 profile")
        self._require_live_bindings()
        try:
            try:
                issued = self._order_issuance_verifier.resolve_issued_order_by_id(order.order_id)
            except Exception as error:
                raise _VerifierFailure(error) from error
        except _VerifierFailure as failure:
            raise failure.error from None
        if type(issued) is not Order or canonical_order_bytes(issued) != submitted_order_bytes:
            raise _fail(OutcomeCode.CONFLICTING_ID, "Order was not issued by authority")
        try:
            try:
                proof = self._active_dispatch_verifier.verify_active_market_dispatch(
                    causal_market_root,
                    dispatch_sequence=sequence,
                )
            except RuntimeOrderingError:
                raise
            except Exception as error:
                raise _VerifierFailure(error) from error
            _require_active_market_dispatch_proof(
                proof,
                run_id=self._run_id,
                market_root=causal_market_root,
                canonical_market_bytes=causal_bytes,
                causal_market_sha256=causal_market_digest(causal_market_root),
                dispatch_sequence=sequence,
                issuer=self._active_dispatch_verifier,
            )
        except _VerifierFailure as failure:
            raise failure.error from None
        except HistoricalMatcherError:
            raise
        except RuntimeOrderingError as error:
            raise _fail(error.code, "active market proof is invalid") from error
        except (AttributeError, TypeError) as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "active market proof is invalid") from error
        if (
            order.run_id != self._run_id
            or order.dispatch_sequence != sequence
            or order.eligible_after_available_at != causal_market_root.available_at
            or order.instrument != causal_market_root.payload.instrument
            or order.instrument_spec_set_id != self._spec_set.identifier
            or order.instrument_spec_set_sha256 != self._spec_sha256
            or order.execution_policy != self._execution_policy
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "submission bindings conflict")
        try:
            specification = self._spec_set.require(order.instrument)
            require_positive(order.quantity, field_name="quantity")
            from ea.core.economics import require_quantized

            require_quantized(
                order.quantity,
                specification.quantity_quantum,
                field_name="quantity",
            )
        except EconomicValidationError as error:
            code = (
                error.code
                if error.code
                in {
                    OutcomeCode.INVALID_TYPE,
                    OutcomeCode.OUT_OF_RANGE,
                    OutcomeCode.NOT_QUANTIZED,
                }
                else OutcomeCode.CONFLICTING_ID
            )
            raise _fail(code, "Order quantity is invalid") from error
        submission_sequence = self._state.next_submission
        if submission_sequence is None:
            raise _fail(OutcomeCode.ARITHMETIC_OVERFLOW, "submission sequence exhausted")
        try:
            try:
                auth_proof = (
                    self._submission_authorization_verifier.verify_authorized_historical_submission(
                        order_id=order.order_id,
                        canonical_order_bytes=submitted_order_bytes,
                        canonical_execution_request_bytes=request_bytes,
                        canonical_causal_market_bytes=causal_bytes,
                        causal_market_sha256=causal_sha256,
                        causal_root_key=causal_key,
                        dispatch_sequence=sequence,
                    )
                )
            except HistoricalPreEffectAuthorizationError:
                raise
            except Exception as error:
                raise _VerifierFailure(error) from error
            auth = _require_historical_submission_authorization_proof(
                auth_proof,
                run_id=self._run_id,
                spec_set=self._spec_set,
                execution_policy=self._execution_policy,
                order=order,
                order_sha256=submitted_order_sha256,
                execution_request_sha256=request_sha256,
                causal_market_sha256=causal_sha256,
                causal_root_key=causal_key,
                dispatch_sequence=sequence,
                issuer=self._submission_authorization_verifier,
            )
        except _VerifierFailure as failure:
            raise failure.error from None
        except HistoricalPreEffectAuthorizationError:
            raise
        except HistoricalMatcherError:
            raise
        except (AttributeError, TypeError) as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "authorization proof is invalid") from error
        owned_order = _clone_order(order)
        receipt = _create_historical_submission_receipt(
            run_id=self._run_id,
            source_namespace=self._source_namespace,
            submission_sequence=submission_sequence,
            order_id=owned_order.order_id,
            order_sha256=submitted_order_sha256,
            execution_request_sha256=request_sha256,
            client_submission_key=client_key,
            instrument=owned_order.instrument,
            side=owned_order.side,
            quantity_text=owned_order.quantity.text,
            causal_market_sha256=causal_sha256,
            causal_root_key=causal_key,
            dispatch_sequence=sequence,
            eligible_after_available_at=owned_order.eligible_after_available_at,
            audit_acknowledgement_id=auth.audit_acknowledgement_id,
            audit_acknowledgement_sha256=auth.audit_acknowledgement_sha256,
            global_halt_epoch=auth.global_halt_epoch,
            risk_halt_epoch=auth.risk_halt_epoch,
            instrument_gate_id=auth.instrument_gate_id,
            instrument_gate_version=auth.instrument_gate_version,
            authorization_state_version=auth.authorization_state_version,
            instrument_spec_set_id=self._spec_set.identifier,
            instrument_spec_set_sha256=self._spec_sha256,
            execution_policy=self._execution_policy,
        )
        receipt_bytes = canonical_historical_submission_receipt_bytes(receipt)
        receipt_sha256 = historical_submission_receipt_digest(receipt)
        record = _SubmissionRecord(
            order=owned_order,
            order_bytes=submitted_order_bytes,
            order_sha256=submitted_order_sha256,
            request_bytes=request_bytes,
            request_sha256=request_sha256,
            client_key=client_key,
            causal_market_bytes=causal_bytes,
            causal_market_sha256=causal_sha256,
            causal_root_key=causal_key,
            dispatch_sequence=sequence,
            receipt=receipt,
            receipt_bytes=receipt_bytes,
            receipt_sha256=receipt_sha256,
        )
        by_order = dict(self._state.submission_by_order)
        by_client = dict(self._state.submission_by_client)
        by_order[owned_order.order_id] = record
        by_client[client_key] = record
        self._state = _MatcherState(
            next_submission=_advance(submission_sequence),
            next_fact=self._state.next_fact,
            submissions=(*self._state.submissions, record),
            pending=(*self._state.pending, record),
            submission_by_order=MappingProxyType(by_order),
            submission_by_client=MappingProxyType(by_client),
            dispatch_by_sequence=self._state.dispatch_by_sequence,
            dispatch_by_digest=self._state.dispatch_by_digest,
            issued=self._state.issued,
            issued_by_identity=self._state.issued_by_identity,
            last_dispatch=self._state.last_dispatch,
            ended=False,
            end_batch_sha256=None,
            conflict=None,
        )
        return receipt

    def _dispatch_replay(
        self,
        *,
        sequence: int,
        root_bytes: bytes,
        root_sha256: Sha256Digest,
    ) -> HistoricalMatcherDispatchBatch | None:
        existing = self._state.dispatch_by_sequence.get(sequence)
        if existing is not None:
            self._require_dispatch_record(existing)
            if existing.root_bytes == root_bytes and existing.root_sha256 == root_sha256:
                return existing.batch
            self._publish_conflict(
                kind=HistoricalMatcherConflictKind.DISPATCH_IDENTITY,
                occupied_identity={
                    "dispatch_sequence": sequence,
                    "kind": "dispatch_sequence",
                },
                existing_sha256=existing.root_sha256,
                submitted_sha256=root_sha256,
                submitted_dispatch_sequence=sequence,
                trigger_root_sha256=root_sha256,
            )
        existing = self._state.dispatch_by_digest.get(root_sha256)
        if existing is not None:
            self._require_dispatch_record(existing)
            self._publish_conflict(
                kind=HistoricalMatcherConflictKind.DISPATCH_IDENTITY,
                occupied_identity=None,
                existing_sha256=existing.root_sha256,
                submitted_sha256=root_sha256,
                submitted_dispatch_sequence=sequence,
                trigger_root_sha256=root_sha256,
            )
        return None

    def match_active_market_root(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> HistoricalMatcherDispatchBatch:
        if type(market_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.INVALID_TYPE, "market_root must be exact")
        sequence = _require_dispatch_sequence(dispatch_sequence)
        try:
            root_bytes = canonical_market_data_record_bytes(market_root)
            root_sha256 = historical_market_root_digest(market_root)
            root_key = runtime_root_order_key(market_root)
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "market root cannot be encoded") from error
        replay = self._dispatch_replay(
            sequence=sequence,
            root_bytes=root_bytes,
            root_sha256=root_sha256,
        )
        if replay is not None:
            return replay
        if self._state.conflict is not None or self._state.ended:
            raise _fail(OutcomeCode.CONFLICTING_ID, "matcher is halted or ended")
        if self._state.last_dispatch is not None and sequence <= self._state.last_dispatch:
            self._publish_conflict(
                kind=HistoricalMatcherConflictKind.NON_MONOTONE_DISPATCH,
                occupied_identity={
                    "dispatch_sequence": sequence,
                    "kind": "dispatch_sequence",
                },
                existing_sha256=None,
                submitted_sha256=root_sha256,
                submitted_dispatch_sequence=sequence,
                trigger_root_sha256=root_sha256,
            )
        self._require_live_bindings()
        try:
            try:
                proof = self._active_dispatch_verifier.verify_active_market_dispatch(
                    market_root,
                    dispatch_sequence=sequence,
                )
            except RuntimeOrderingError:
                raise
            except Exception as error:
                raise _VerifierFailure(error) from error
            _require_active_market_dispatch_proof(
                proof,
                run_id=self._run_id,
                market_root=market_root,
                canonical_market_bytes=root_bytes,
                causal_market_sha256=causal_market_digest(market_root),
                dispatch_sequence=sequence,
                issuer=self._active_dispatch_verifier,
            )
        except _VerifierFailure as failure:
            raise failure.error from None
        except RuntimeOrderingError as error:
            raise _fail(error.code, "active market proof is invalid") from error
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "active market verifier failed") from error
        eligible = tuple(
            sorted(
                (
                    record
                    for record in self._state.pending
                    if (
                        record.order.instrument == market_root.payload.instrument
                        and market_root.payload.adjustment is Adjustment.RAW
                        and market_root.revision == 0
                        and root_key > record.causal_root_key
                        and market_root.event_time > record.order.eligible_after_available_at
                    )
                ),
                key=lambda record: record.receipt.submission_sequence,
            )
        )
        return self._publish_batch(
            kind=HistoricalDispatchKind.MARKET,
            sequence=sequence,
            root_key=root_key,
            root_bytes=root_bytes,
            root_sha256=root_sha256,
            records=eligible,
            occurred_at=market_root.event_time,
            available_at=market_root.available_at,
            close=market_root.payload.close,
            end=False,
        )

    def expire_at_active_end(
        self,
        end_root: EndOfRunRoot,
        *,
        dispatch_sequence: int,
    ) -> HistoricalMatcherDispatchBatch:
        if type(end_root) is not EndOfRunRoot:
            raise _fail(OutcomeCode.INVALID_TYPE, "end_root must be exact")
        sequence = _require_dispatch_sequence(dispatch_sequence)
        try:
            root_bytes = canonical_end_of_run_root_bytes(end_root)
            root_sha256 = historical_end_root_digest(end_root)
            root_key = runtime_root_order_key(end_root)
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "end root cannot be encoded") from error
        replay = self._dispatch_replay(
            sequence=sequence,
            root_bytes=root_bytes,
            root_sha256=root_sha256,
        )
        if replay is not None:
            return replay
        if self._state.conflict is not None or self._state.ended:
            raise _fail(OutcomeCode.CONFLICTING_ID, "matcher is halted or ended")
        if end_root.kind is not EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "unsupported terminal root kind")
        if end_root.run_id != self._run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "terminal root run conflicts")
        if self._state.last_dispatch is not None and sequence <= self._state.last_dispatch:
            self._publish_conflict(
                kind=HistoricalMatcherConflictKind.NON_MONOTONE_DISPATCH,
                occupied_identity={
                    "dispatch_sequence": sequence,
                    "kind": "dispatch_sequence",
                },
                existing_sha256=None,
                submitted_sha256=root_sha256,
                submitted_dispatch_sequence=sequence,
                trigger_root_sha256=root_sha256,
            )
        self._require_live_bindings()
        try:
            try:
                proof = self._active_dispatch_verifier.verify_active_end_of_run_dispatch(
                    end_root,
                    dispatch_sequence=sequence,
                )
            except RuntimeOrderingError:
                raise
            except Exception as error:
                raise _VerifierFailure(error) from error
            _require_active_end_of_run_dispatch_proof(
                proof,
                run_id=self._run_id,
                end_root=end_root,
                canonical_end_bytes=root_bytes,
                end_root_sha256=root_sha256,
                dispatch_sequence=sequence,
                issuer=self._active_dispatch_verifier,
            )
        except _VerifierFailure as failure:
            raise failure.error from None
        except RuntimeOrderingError as error:
            raise _fail(error.code, "active end proof is invalid") from error
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "active end verifier failed") from error
        return self._publish_batch(
            kind=HistoricalDispatchKind.END_OF_RUN,
            sequence=sequence,
            root_key=root_key,
            root_bytes=root_bytes,
            root_sha256=root_sha256,
            records=tuple(
                sorted(
                    self._state.pending,
                    key=lambda record: record.receipt.submission_sequence,
                )
            ),
            occurred_at=end_root.available_at,
            available_at=end_root.available_at,
            close=None,
            end=True,
        )

    def _publish_batch(
        self,
        *,
        kind: HistoricalDispatchKind,
        sequence: int,
        root_key: RuntimeRootOrderKey,
        root_bytes: bytes,
        root_sha256: Sha256Digest,
        records: tuple[_SubmissionRecord, ...],
        occurred_at: datetime,
        available_at: datetime,
        close: float | None,
        end: bool,
    ) -> HistoricalMatcherDispatchBatch:
        for record in records:
            self._require_submission_record(record)
        fact_before = self._state.next_fact
        if records and fact_before is None:
            raise _fail(OutcomeCode.ARITHMETIC_OVERFLOW, "fact sequence exhausted")
        if fact_before is not None and len(records) > _MAX_UINT64 - fact_before + 1:
            raise _fail(OutcomeCode.ARITHMETIC_OVERFLOW, "fact batch exceeds capacity")
        ingresses: list[ExecutionFactIngress] = []
        ingress_digests: list[Sha256Digest] = []
        issued_records: list[_IssuedRecord] = []
        fact_sequence = fact_before
        for record in records:
            assert fact_sequence is not None
            price = None
            expiry_code = None
            fact_kind = "expiry" if end else "trade"
            if end:
                expiry_code = OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA
            else:
                assert close is not None
                price = _quantized_close(
                    close,
                    side=record.order.side,
                    specification=self._spec_set.require(record.order.instrument),
                )
            observation_sha256 = historical_matcher_observation_digest(
                fact_sequence=fact_sequence,
                fact_kind=fact_kind,
                source_namespace=self._source_namespace,
                provenance_id=self._provenance_id,
                submission_receipt_sha256=record.receipt_sha256,
                order_sha256=record.order_sha256,
                trigger_root_kind=kind,
                trigger_root_sha256=root_sha256,
                trigger_root_key=root_key,
                trigger_dispatch_sequence=sequence,
                occurred_at=occurred_at,
                available_at=available_at,
                instrument=record.order.instrument,
                side=record.order.side,
                quantity_text=record.order.quantity.text,
                price_text=None if price is None else price.text,
                expiry_outcome_code=expiry_code,
                spec_set=self._spec_set,
                execution_policy=self._execution_policy,
            )
            provenance = FactProvenance(self._provenance_id, observation_sha256)
            if end:
                fact = create_lifecycle_execution_fact(
                    kind=ExecutionFactKind.EXPIRY,
                    outcome_code=expiry_code,
                    source_namespace=self._source_namespace,
                    dedup_identity=SourceNativeSequence(fact_sequence),
                    occurred_at=occurred_at,
                    provenance=provenance,
                    instrument=record.order.instrument,
                    client_submission_key=record.client_key,
                    order_id=record.order.order_id,
                    correlation_id=record.order.correlation_id,
                    causation_id=record.order.order_id,
                )
            else:
                assert price is not None
                fact = create_trade_execution_fact(
                    spec_set=self._spec_set,
                    side=record.order.side,
                    quantity=record.order.quantity,
                    price=price,
                    source_namespace=self._source_namespace,
                    dedup_identity=SourceNativeSequence(fact_sequence),
                    occurred_at=occurred_at,
                    provenance=provenance,
                    instrument=record.order.instrument,
                    client_submission_key=record.client_key,
                    order_id=record.order.order_id,
                    correlation_id=record.order.correlation_id,
                    causation_id=record.order.order_id,
                )
            ingress = create_execution_fact_ingress(
                available_at=available_at,
                source_namespace=self._source_namespace,
                ingress_sequence=fact_sequence,
                fact=fact,
            )
            ingresses.append(ingress)
            ingress_digests.append(execution_fact_ingress_digest(ingress))
            fact_sequence = _advance(fact_sequence)
        batch = _create_historical_matcher_dispatch_batch(
            run_id=self._run_id,
            source_namespace=self._source_namespace,
            dispatch_kind=kind,
            dispatch_sequence=sequence,
            trigger_root_sha256=root_sha256,
            trigger_root_key=root_key,
            next_fact_sequence_before=fact_before,
            next_fact_sequence_after=fact_sequence,
            submission_sequences=tuple(record.receipt.submission_sequence for record in records),
            order_ids=tuple(record.order.order_id for record in records),
            ingresses=tuple(ingresses),
            ingress_sha256s=tuple(ingress_digests),
        )
        batch_bytes = canonical_historical_matcher_dispatch_batch_bytes(batch)
        batch_sha256 = historical_matcher_dispatch_batch_digest(batch)
        for index, (_record, ingress, ingress_sha256) in enumerate(
            zip(records, ingresses, ingress_digests, strict=True)
        ):
            fact_bytes = canonical_execution_fact_bytes(ingress.fact)
            ingress_bytes = canonical_execution_fact_ingress_bytes(ingress)
            binding = _create_historical_matcher_descendant_binding(
                ingress_identity=ingress.identity,
                ingress_sha256=ingress_sha256,
                fact_sha256=execution_fact_digest(ingress.fact),
                batch_sha256=batch_sha256,
                batch_index=index,
                parent_kind=kind,
                parent_root_sha256=root_sha256,
                parent_root_key=root_key,
                parent_dispatch_sequence=sequence,
            )
            issued_records.append(
                _IssuedRecord(
                    ingress=ingress,
                    ingress_bytes=ingress_bytes,
                    ingress_sha256=ingress_sha256,
                    fact_bytes=fact_bytes,
                    binding=binding,
                )
            )
        dispatch_record = _DispatchRecord(
            root_bytes=root_bytes,
            root_sha256=root_sha256,
            root_key=root_key,
            batch=batch,
            batch_bytes=batch_bytes,
            batch_sha256=batch_sha256,
        )
        by_sequence = dict(self._state.dispatch_by_sequence)
        by_digest = dict(self._state.dispatch_by_digest)
        by_sequence[sequence] = dispatch_record
        by_digest[root_sha256] = dispatch_record
        issued_by_identity = dict(self._state.issued_by_identity)
        for issued in issued_records:
            if issued.ingress.identity in issued_by_identity:
                raise _fail(OutcomeCode.CONFLICTING_ID, "issued ingress identity collided")
            issued_by_identity[issued.ingress.identity] = issued
        matched_ids = {record.order.order_id for record in records}
        pending = tuple(
            sorted(
                (
                    record
                    for record in self._state.pending
                    if record.order.order_id not in matched_ids
                ),
                key=lambda record: record.receipt.submission_sequence,
            )
        )
        next_state = _MatcherState(
            next_submission=self._state.next_submission,
            next_fact=fact_sequence,
            submissions=self._state.submissions,
            pending=pending,
            submission_by_order=self._state.submission_by_order,
            submission_by_client=self._state.submission_by_client,
            dispatch_by_sequence=MappingProxyType(by_sequence),
            dispatch_by_digest=MappingProxyType(by_digest),
            issued=(*self._state.issued, *issued_records),
            issued_by_identity=MappingProxyType(issued_by_identity),
            last_dispatch=sequence,
            ended=end,
            end_batch_sha256=batch_sha256 if end else None,
            conflict=None,
        )
        canonical_historical_matcher_state_bytes(
            _create_historical_matcher_state(
                run_id=self._run_id,
                source_namespace=self._source_namespace,
                provenance_id=self._provenance_id,
                instrument_spec_set_id=self._spec_set.identifier,
                instrument_spec_set_sha256=self._spec_sha256,
                execution_policy=self._execution_policy,
                next_submission_sequence=next_state.next_submission,
                next_fact_sequence=next_state.next_fact,
                receipt_sha256s=tuple(item.receipt_sha256 for item in next_state.submissions),
                pending_order_ids=tuple(item.order.order_id for item in next_state.pending),
                issued_ingresses=tuple(item.ingress for item in next_state.issued),
                dispatch_batch_sha256s=tuple(
                    item.batch_sha256 for _, item in sorted(next_state.dispatch_by_sequence.items())
                ),
                last_new_dispatch_sequence=sequence,
                ended=end,
                end_batch_sha256=batch_sha256 if end else None,
                halted=False,
                conflict=None,
            )
        )
        self._state = next_state
        return batch

    def has_issued_ingress(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> bool:
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "issuance lookup inputs must be exact")
        self._require_live_bindings()
        record = self._state.issued_by_identity.get(ingress_identity)
        if record is None:
            return False
        self._require_issued_record(record)
        return bool(
            record.ingress_bytes == canonical_ingress_bytes
            and record.fact_bytes == canonical_fact_bytes
        )

    def resolve_descendant_binding(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> HistoricalMatcherDescendantBinding | None:
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "descendant lookup inputs must be exact")
        self._require_live_bindings()
        record = self._state.issued_by_identity.get(ingress_identity)
        if (
            record is None
            or record.ingress_bytes != canonical_ingress_bytes
            or record.fact_bytes != canonical_fact_bytes
        ):
            return None
        self._require_issued_record(record)
        return record.binding


def create_phase1_historical_matcher(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    source_namespace: SourceNamespace,
    provenance_id: FactProvenanceId,
    order_issuance_verifier: HistoricalOrderIssuanceVerifier,
    submission_authorization_verifier: HistoricalSubmissionAuthorizationVerifier,
    active_dispatch_verifier: HistoricalMatcherDispatchVerifier,
) -> Phase1HistoricalMatcher:
    if (
        type(run_id) is not RunId
        or type(source_namespace) is not SourceNamespace
        or type(provenance_id) is not FactProvenanceId
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "matcher bindings must be exact")
    owned_specs = _clone_spec_set(spec_set)
    owned_policy = _clone_policy(execution_policy)
    for verifier, operations in (
        (order_issuance_verifier, ("resolve_issued_order_by_id",)),
        (
            submission_authorization_verifier,
            ("verify_authorized_historical_submission",),
        ),
        (
            active_dispatch_verifier,
            (
                "verify_active_market_dispatch",
                "verify_active_end_of_run_dispatch",
            ),
        ),
    ):
        if any(not callable(getattr(verifier, operation, None)) for operation in operations):
            raise _fail(OutcomeCode.INVALID_TYPE, "matcher verifier surface is incomplete")
    value = object.__new__(Phase1HistoricalMatcher)
    value._run_id = run_id
    value._spec_set = owned_specs
    value._spec_bytes = canonical_instrument_spec_set_bytes(owned_specs)
    value._spec_sha256 = instrument_spec_set_digest(owned_specs)
    value._execution_policy = owned_policy
    value._source_namespace = source_namespace
    value._provenance_id = provenance_id
    value._order_issuance_verifier = order_issuance_verifier
    value._submission_authorization_verifier = submission_authorization_verifier
    value._active_dispatch_verifier = active_dispatch_verifier
    value._state = _MatcherState(
        next_submission=1,
        next_fact=1,
        submissions=(),
        pending=(),
        submission_by_order=MappingProxyType({}),
        submission_by_client=MappingProxyType({}),
        dispatch_by_sequence=MappingProxyType({}),
        dispatch_by_digest=MappingProxyType({}),
        issued=(),
        issued_by_identity=MappingProxyType({}),
        last_dispatch=None,
        ended=False,
        end_batch_sha256=None,
        conflict=None,
    )
    value._require_live_bindings()
    return value
