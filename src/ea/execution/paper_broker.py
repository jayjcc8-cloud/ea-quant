"""Bounded, in-process Paper transport for issued Orders.

This adapter owns simulated request and fact identities, never Fill, cash, or
position state. Its ingresses must pass the existing source, dispatch, fact,
audit, and ledger boundaries when a runtime composes the Paper path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, final

from ea.core.commission import (
    commission_bps_from_identity,
    execution_latency_allows,
    slippage_bps_from_identity,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_identity import (
    IngressIdentity,
    SourceNamespace,
    SourceNativeSequence,
)
from ea.core.execution_messages import (
    ExecutionFact,
    ExecutionFactIngress,
    ExecutionFactKind,
    FactProvenance,
    FactProvenanceId,
    Order,
    OrderKind,
    TimeInForce,
    VenueOrderId,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_execution_request_bytes,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    create_trade_execution_fact,
    execution_request_digest,
    order_digest,
)
from ea.core.historical_matching import _quantized_historical_close
from ea.core.market_data import Adjustment, MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.runtime import RuntimeRootOrderKey, runtime_root_order_key
from ea.core.time import require_utc
from ea.execution.authority import Phase1OrderAuthority
from ea.execution.order_lifecycle import (
    CancelCommand,
    CancellationAttemptState,
    SubmissionAttemptState,
    SubmitCommand,
)


class PaperBrokerError(ValueError):
    """Malformed or conflicting local transport input with no new effect."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@final
@dataclass(frozen=True, slots=True)
class PaperSubmitResult:
    state: SubmissionAttemptState
    venue_order_id: VenueOrderId | None
    ingresses: tuple[ExecutionFactIngress, ...]


@final
@dataclass(frozen=True, slots=True)
class PaperCancelResult:
    state: CancellationAttemptState
    ingresses: tuple[ExecutionFactIngress, ...]


@final
@dataclass(frozen=True, slots=True)
class PaperQueryResult:
    code: OutcomeCode
    ingress: ExecutionFactIngress | None


class PaperBrokerPort(Protocol):
    def submit(self, command: SubmitCommand, *, submitted_at: datetime) -> PaperSubmitResult: ...

    def cancel(self, command: CancelCommand, *, requested_at: datetime) -> PaperCancelResult: ...

    def query(
        self, client_submission_key: Sha256Digest, *, observed_at: datetime
    ) -> PaperQueryResult: ...

    def on_market(self, event: MarketDataEnvelope) -> tuple[ExecutionFactIngress, ...]: ...


class _PaperOrderState(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class _PaperOrder:
    command: SubmitCommand
    submitted_at: datetime
    venue_order_id: VenueOrderId
    submit_result: PaperSubmitResult
    state: _PaperOrderState = _PaperOrderState.OPEN
    cancel_command: CancelCommand | None = None
    cancel_result: PaperCancelResult | None = None
    terminal_fact: ExecutionFact | None = None


@final
class PaperBroker:
    """One run-bound Paper adapter with finite retained request history.

    The caller supplies only newly admitted market envelopes. An unrecognized
    client key remains unknown because another process/history may have sent it.
    No state here is durable restart or pre-effect authorization evidence.
    """

    _DEFAULT_SOURCE = SourceNamespace("paper.local")

    def __init__(
        self,
        orders: Phase1OrderAuthority,
        *,
        source_namespace: SourceNamespace = _DEFAULT_SOURCE,
        max_orders: int = 1024,
    ) -> None:
        if (
            type(orders) is not Phase1OrderAuthority
            or type(source_namespace) is not SourceNamespace
        ):
            raise PaperBrokerError(OutcomeCode.INVALID_TYPE, "Paper bindings must be exact")
        if type(max_orders) is not int or not 1 <= max_orders <= 1024:
            raise PaperBrokerError(OutcomeCode.OUT_OF_RANGE, "max_orders must be in 1..1024")
        self._orders = orders
        self._source = source_namespace
        self._max_orders = max_orders
        self._max_ingresses = max_orders * 8
        self._records: dict[Sha256Digest, _PaperOrder] = {}
        self._unknown_cancels: dict[Sha256Digest, tuple[CancelCommand, PaperCancelResult]] = {}
        self._issued: dict[IngressIdentity, tuple[bytes, bytes]] = {}
        self._fact_next = 1
        self._ingress_next = 1
        self._last_market_key: RuntimeRootOrderKey | None = None
        self._last_market_bytes: bytes | None = None

    @property
    def source_namespace(self) -> SourceNamespace:
        return self._source

    @property
    def run_id(self) -> RunId:
        return self._orders.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._orders.spec_set

    def has_issued_ingress(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> bool:
        """Prove exact source-issued bytes without issuing or changing state."""
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            return False
        return self._issued.get(ingress_identity) == (
            canonical_ingress_bytes,
            canonical_fact_bytes,
        )

    def submit(self, command: SubmitCommand, *, submitted_at: datetime) -> PaperSubmitResult:
        """Retain one exact request and its ACK before returning the local effect."""
        order = self._require_command(command, SubmitCommand)
        submitted = require_utc(submitted_at, field="submitted_at")
        if submitted < order.eligible_after_available_at:
            raise PaperBrokerError(OutcomeCode.OUT_OF_RANGE, "submit precedes Order eligibility")
        record = self._records.get(order.client_submission_key)
        if record is not None:
            if record.command != command:
                raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "request key is occupied")
            return record.submit_result
        if len(self._records) >= self._max_orders:
            return PaperSubmitResult(SubmissionAttemptState.DEFINITELY_NOT_SUBMITTED, None, ())
        if (
            order.order_kind is not OrderKind.MARKET
            or order.time_in_force is not TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
        ):
            return PaperSubmitResult(SubmissionAttemptState.DEFINITELY_NOT_SUBMITTED, None, ())
        venue_id = VenueOrderId("paper-" + order.client_submission_key.value)
        fact = create_lifecycle_execution_fact(
            kind=ExecutionFactKind.ACKNOWLEDGEMENT,
            source_namespace=self._source,
            dedup_identity=SourceNativeSequence(self._fact_next),
            occurred_at=submitted,
            provenance=self._provenance(
                b"ack", canonical_execution_request_bytes(order), submitted.isoformat().encode()
            ),
            instrument=order.instrument,
            client_submission_key=order.client_submission_key,
            venue_order_id=venue_id,
            order_id=order.order_id,
            correlation_id=order.correlation_id,
            causation_id=order.order_id,
        )
        ingress = self._ingress(fact, submitted, self._ingress_next)
        result = PaperSubmitResult(SubmissionAttemptState.SUBMITTED, venue_id, (ingress,))
        self._retain((ingress,))
        self._records[order.client_submission_key] = _PaperOrder(
            command, submitted, venue_id, result
        )
        self._fact_next += 1
        self._ingress_next += 1
        return result

    def cancel(self, command: CancelCommand, *, requested_at: datetime) -> PaperCancelResult:
        """Cancel a pending local match and emit a separate confirmation fact."""
        order = self._require_command(command, CancelCommand)
        requested = require_utc(requested_at, field="requested_at")
        retained_unknown = self._unknown_cancels.get(order.client_submission_key)
        if retained_unknown is not None:
            if retained_unknown[0] != command:
                raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "cancel request differs")
            return retained_unknown[1]
        record = self._records.get(order.client_submission_key)
        if record is None:
            if len(self._unknown_cancels) >= self._max_orders:
                raise PaperBrokerError(OutcomeCode.OUT_OF_RANGE, "Paper cancel history is full")
            uncertain = PaperCancelResult(CancellationAttemptState.UNCERTAIN, ())
            self._unknown_cancels[order.client_submission_key] = (command, uncertain)
            return uncertain
        if record.cancel_command is not None:
            if record.cancel_command != command:
                raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "cancel request differs")
            assert record.cancel_result is not None
            return record.cancel_result
        if requested < record.submitted_at:
            raise PaperBrokerError(OutcomeCode.OUT_OF_RANGE, "cancel precedes submission")
        if record.state is not _PaperOrderState.OPEN:
            result = PaperCancelResult(CancellationAttemptState.DEFINITELY_NOT_SENT, ())
            record.cancel_command = command
            record.cancel_result = result
            return result
        fact = create_lifecycle_execution_fact(
            kind=ExecutionFactKind.CANCELLATION,
            source_namespace=self._source,
            dedup_identity=SourceNativeSequence(self._fact_next),
            occurred_at=requested,
            provenance=self._provenance(
                b"cancel", canonical_execution_request_bytes(order), requested.isoformat().encode()
            ),
            instrument=order.instrument,
            client_submission_key=order.client_submission_key,
            venue_order_id=record.venue_order_id,
            order_id=order.order_id,
            correlation_id=order.correlation_id,
            causation_id=order.order_id,
        )
        ingress = self._ingress(fact, requested, self._ingress_next)
        result = PaperCancelResult(CancellationAttemptState.ACCEPTED, (ingress,))
        self._retain((ingress,))
        record.cancel_command = command
        record.cancel_result = result
        record.terminal_fact = fact
        record.state = _PaperOrderState.CANCELLED
        self._fact_next += 1
        self._ingress_next += 1
        return result

    def query(
        self, client_submission_key: Sha256Digest, *, observed_at: datetime
    ) -> PaperQueryResult:
        """Return known current-run evidence; absence is never proof of no send."""
        if type(client_submission_key) is not Sha256Digest:
            raise PaperBrokerError(OutcomeCode.INVALID_TYPE, "client key must be exact")
        observed = require_utc(observed_at, field="observed_at")
        record = self._records.get(client_submission_key)
        if record is None:
            return PaperQueryResult(OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN, None)
        if observed < record.submitted_at:
            raise PaperBrokerError(OutcomeCode.OUT_OF_RANGE, "query precedes submission")
        fact: ExecutionFact | None
        if record.state is _PaperOrderState.CANCELLED:
            fact = record.terminal_fact
            code = OutcomeCode.ORDER_CANCELLED
        elif record.state is _PaperOrderState.FILLED:
            fact = record.terminal_fact
            code = OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED
        else:
            code = OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED
            # A query reports the retained ACK, including after a lost submit
            # reply. A new SUBMITTED fact would regress an ACKNOWLEDGED Order.
            fact = record.submit_result.ingresses[0].fact
        if fact is None:
            raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "terminal fact is missing")
        ingress = self._ingress(fact, observed, self._ingress_next)
        self._retain((ingress,))
        self._ingress_next += 1
        return PaperQueryResult(code, ingress)

    def on_market(self, event: MarketDataEnvelope) -> tuple[ExecutionFactIngress, ...]:
        """Match each open Order at its first newly admitted eligible raw event."""
        if type(event) is not MarketDataEnvelope:
            raise PaperBrokerError(OutcomeCode.INVALID_TYPE, "market envelope must be exact")
        key = runtime_root_order_key(event)
        market_bytes = canonical_market_data_record_bytes(event)
        if self._last_market_key is not None:
            if key == self._last_market_key and market_bytes == self._last_market_bytes:
                return ()
            if key <= self._last_market_key:
                raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "market order regressed")
        fact_next = self._fact_next
        ingress_next = self._ingress_next
        staged: list[tuple[_PaperOrder, ExecutionFact, ExecutionFactIngress]] = []
        if event.revision == 0 and event.payload.adjustment is Adjustment.RAW:
            for record in self._records.values():
                if record.state is not _PaperOrderState.OPEN:
                    continue
                order = record.command.order
                policy = order.execution_policy
                if event.payload.instrument != order.instrument or not execution_latency_allows(
                    event.event_time,
                    record.submitted_at,
                    policy.identifier.value,
                    policy.sha256.value,
                ):
                    continue
                price = _quantized_historical_close(
                    event.payload.close,
                    side=order.side,
                    specification=self._orders.spec_set.require(order.instrument),
                    slippage_bps=slippage_bps_from_identity(
                        policy.identifier.value, policy.sha256.value
                    ),
                )
                fact = create_trade_execution_fact(
                    spec_set=self._orders.spec_set,
                    side=order.side,
                    quantity=order.quantity,
                    price=price,
                    commission_bps=commission_bps_from_identity(
                        policy.identifier.value, policy.sha256.value
                    ),
                    source_namespace=self._source,
                    dedup_identity=SourceNativeSequence(fact_next),
                    occurred_at=event.event_time,
                    provenance=self._provenance(
                        b"trade", canonical_execution_request_bytes(order), market_bytes
                    ),
                    instrument=order.instrument,
                    client_submission_key=order.client_submission_key,
                    venue_order_id=record.venue_order_id,
                    order_id=order.order_id,
                    correlation_id=order.correlation_id,
                    causation_id=order.order_id,
                )
                ingress = self._ingress(fact, event.available_at, ingress_next)
                staged.append((record, fact, ingress))
                fact_next += 1
                ingress_next += 1
        self._retain(tuple(ingress for _, _, ingress in staged))
        for record, fact, _ingress in staged:
            record.state = _PaperOrderState.FILLED
            record.terminal_fact = fact
        self._fact_next = fact_next
        self._ingress_next = ingress_next
        self._last_market_key = key
        self._last_market_bytes = market_bytes
        return tuple(ingress for _, _, ingress in staged)

    def _require_command(
        self,
        command: SubmitCommand | CancelCommand,
        expected: type[SubmitCommand] | type[CancelCommand],
    ) -> Order:
        if type(command) is not expected or type(command.order) is not Order:
            raise PaperBrokerError(OutcomeCode.INVALID_TYPE, "command must be exact")
        order = command.order
        issued_by_id = self._orders.resolve_issued_order_by_id(order.order_id)
        issued_by_key = self._orders.resolve_issued_order_by_client_submission_key(
            order.client_submission_key
        )
        if (
            issued_by_id is None
            or issued_by_id is not issued_by_key
            or order_digest(issued_by_id) != order_digest(order)
            or command.client_submission_key != order.client_submission_key
            or command.execution_request_sha256 != execution_request_digest(order)
        ):
            raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "command Order identity differs")
        return order

    def _ingress(
        self, fact: ExecutionFact, available_at: datetime, sequence: int
    ) -> ExecutionFactIngress:
        return create_execution_fact_ingress(
            available_at=available_at,
            source_namespace=self._source,
            ingress_sequence=sequence,
            fact=fact,
        )

    def _retain(self, ingresses: tuple[ExecutionFactIngress, ...]) -> None:
        """Publish a bounded, exact batch of source issuance evidence."""
        if len(self._issued) + len(ingresses) > self._max_ingresses:
            raise PaperBrokerError(OutcomeCode.OUT_OF_RANGE, "Paper ingress history is full")
        staged: dict[IngressIdentity, tuple[bytes, bytes]] = {}
        for ingress in ingresses:
            if ingress.identity in self._issued or ingress.identity in staged:
                raise PaperBrokerError(OutcomeCode.CONFLICTING_ID, "Paper ingress ID is occupied")
            staged[ingress.identity] = (
                canonical_execution_fact_ingress_bytes(ingress),
                canonical_execution_fact_bytes(ingress.fact),
            )
        self._issued.update(staged)

    @staticmethod
    def _provenance(kind: bytes, request: bytes, observation: bytes) -> FactProvenance:
        digest = sha256(b"ea.paper-local.v1\0" + kind + b"\0" + request + b"\0" + observation)
        return FactProvenance(FactProvenanceId("paper.local.v1"), Sha256Digest(digest.hexdigest()))
