"""One-shot local Paper transport commands over issued Orders and observed facts.

The tracker records outbound intent only. The caller owns immediate pre-effect
authorization; execution facts, Fills, and the ledger retain economic authority.
This in-memory state must never be used as crash-recovery evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import final

from ea.core.execution_identity import EconomicId
from ea.core.execution_messages import Order, execution_request_digest, order_digest
from ea.core.execution_state import OrderProjectionSnapshot, OrderProjectionState
from ea.core.outcomes import OutcomeCode
from ea.core.run import Sha256Digest
from ea.execution.authority import Phase1OrderAuthority
from ea.execution.fact_authority import Phase1ExecutionFactAuthority


class OrderCommandError(ValueError):
    """A command conflicts with issued identity or an earlier transport attempt."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class SubmissionAttemptState(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    DEFINITELY_NOT_SUBMITTED = "definitely_not_submitted"
    UNCERTAIN = "uncertain"


class CancellationAttemptState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    UNCERTAIN = "uncertain"


@final
@dataclass(frozen=True, slots=True)
class SubmitCommand:
    order: Order
    client_submission_key: Sha256Digest
    execution_request_sha256: Sha256Digest


@final
@dataclass(frozen=True, slots=True)
class CancelCommand:
    order: Order
    client_submission_key: Sha256Digest
    execution_request_sha256: Sha256Digest


@final
@dataclass(frozen=True, slots=True)
class OrderCommandSnapshot:
    order_id: EconomicId
    client_submission_key: Sha256Digest
    execution_request_sha256: Sha256Digest
    submission_state: SubmissionAttemptState
    cancel_state: CancellationAttemptState | None
    projection: OrderProjectionSnapshot | None


@dataclass(slots=True)
class _Record:
    submit: SubmitCommand
    submission_state: SubmissionAttemptState
    cancel: CancelCommand | None = None
    cancel_state: CancellationAttemptState | None = None


_ACTIVE_PROJECTIONS = frozenset(
    {
        OrderProjectionState.SUBMITTED,
        OrderProjectionState.ACKNOWLEDGED,
        OrderProjectionState.PARTIALLY_FILLED,
    }
)


@final
class OrderCommandTracker:
    """Remember one submit and at most one cancel request for each issued Order.

    A command is recorded before a caller invokes transport. Repeating begin_* never
    returns a command to send again. An exception or timeout after begin_submit
    must be recorded as UNCERTAIN, and a restart must halt/query externally.
    """

    def __init__(self, orders: Phase1OrderAuthority) -> None:
        if type(orders) is not Phase1OrderAuthority:
            raise OrderCommandError(OutcomeCode.INVALID_TYPE, "issued Order authority is required")
        self._orders = orders
        self._records: dict[EconomicId, _Record] = {}
        self._lock = RLock()

    def begin_submit(self, order: Order) -> SubmitCommand | None:
        """Reserve the sole send attempt before calling any transport port."""
        self._require_issued_order(order)
        with self._lock:
            if order.order_id in self._records:
                return None
            command = SubmitCommand(
                order=order,
                client_submission_key=order.client_submission_key,
                execution_request_sha256=execution_request_digest(order),
            )
            self._records[order.order_id] = _Record(command, SubmissionAttemptState.PENDING)
            return command

    def record_submit_result(
        self, command: SubmitCommand, state: SubmissionAttemptState
    ) -> SubmissionAttemptState:
        """Classify a transport return; timeout or possible effect means UNCERTAIN."""
        if type(command) is not SubmitCommand or type(state) is not SubmissionAttemptState:
            raise OrderCommandError(OutcomeCode.INVALID_TYPE, "submit result has invalid type")
        if state is SubmissionAttemptState.PENDING:
            raise OrderCommandError(OutcomeCode.OUT_OF_RANGE, "pending is not a transport result")
        with self._lock:
            record = self._require_record(command.order)
            if record.submit != command:
                raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "submit command differs")
            if record.submission_state is SubmissionAttemptState.PENDING:
                record.submission_state = state
            elif record.submission_state is not state:
                raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "submit result conflicts")
            return state

    def begin_cancel(
        self, order: Order, facts: Phase1ExecutionFactAuthority
    ) -> CancelCommand | None:
        """Reserve one cancellation request; only a fact can confirm cancellation."""
        self._require_issued_order(order)
        with self._lock:
            record = self._require_record(order)
            if record.cancel is not None:
                return None
            projection = self._require_facts(facts, order)
            if projection is not None and projection.is_terminal:
                raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "Order is terminal")
            if record.submission_state is not SubmissionAttemptState.SUBMITTED and (
                projection is None or projection.projection_state not in _ACTIVE_PROJECTIONS
            ):
                raise OrderCommandError(
                    OutcomeCode.CONFLICTING_ID,
                    "submission has no positive evidence for cancellation",
                )
            command = CancelCommand(
                order=order,
                client_submission_key=order.client_submission_key,
                execution_request_sha256=record.submit.execution_request_sha256,
            )
            record.cancel = command
            record.cancel_state = CancellationAttemptState.PENDING
            return command

    def record_cancel_result(
        self, command: CancelCommand, state: CancellationAttemptState
    ) -> CancellationAttemptState:
        """Record only cancel transport status, never a terminal Order state."""
        if type(command) is not CancelCommand or type(state) is not CancellationAttemptState:
            raise OrderCommandError(OutcomeCode.INVALID_TYPE, "cancel result has invalid type")
        if state is CancellationAttemptState.PENDING:
            raise OrderCommandError(OutcomeCode.OUT_OF_RANGE, "pending is not a transport result")
        with self._lock:
            record = self._require_record(command.order)
            if record.cancel != command:
                raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "cancel command differs")
            if record.cancel_state is CancellationAttemptState.PENDING:
                record.cancel_state = state
            elif record.cancel_state is not state:
                raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "cancel result conflicts")
            return state

    def snapshot(self, order: Order, facts: Phase1ExecutionFactAuthority) -> OrderCommandSnapshot:
        """Combine local command evidence with the existing fact-owned projection."""
        self._require_issued_order(order)
        with self._lock:
            record = self._require_record(order)
            return self._snapshot(record, self._require_facts(facts, order))

    def _require_issued_order(self, order: Order) -> None:
        if type(order) is not Order:
            raise OrderCommandError(OutcomeCode.INVALID_TYPE, "Order must be exact")
        issued_by_id = self._orders.resolve_issued_order_by_id(order.order_id)
        issued_by_key = self._orders.resolve_issued_order_by_client_submission_key(
            order.client_submission_key
        )
        if (
            issued_by_id is None
            or issued_by_key is None
            or issued_by_id is not issued_by_key
            or order_digest(issued_by_id) != order_digest(order)
            or execution_request_digest(issued_by_id) != execution_request_digest(order)
        ):
            raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "Order was not issued here")

    def _require_record(self, order: Order) -> _Record:
        if type(order) is not Order:
            raise OrderCommandError(OutcomeCode.INVALID_TYPE, "Order must be exact")
        record = self._records.get(order.order_id)
        if record is None or record.submit.order != order:
            raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "no matching submit attempt")
        return record

    def _require_facts(
        self, facts: Phase1ExecutionFactAuthority, order: Order
    ) -> OrderProjectionSnapshot | None:
        if type(facts) is not Phase1ExecutionFactAuthority:
            raise OrderCommandError(OutcomeCode.INVALID_TYPE, "fact authority must be exact")
        if facts.run_id != self._orders.run_id or facts.spec_set != self._orders.spec_set:
            raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "fact authority binding differs")
        projection = facts.projection_for_order(order.order_id)
        if projection is not None and (
            projection.order_sha256 != order_digest(order)
            or projection.client_submission_key != order.client_submission_key
        ):
            raise OrderCommandError(OutcomeCode.CONFLICTING_ID, "projection binding differs")
        return projection

    @staticmethod
    def _snapshot(
        record: _Record, projection: OrderProjectionSnapshot | None
    ) -> OrderCommandSnapshot:
        return OrderCommandSnapshot(
            order_id=record.submit.order.order_id,
            client_submission_key=record.submit.client_submission_key,
            execution_request_sha256=record.submit.execution_request_sha256,
            submission_state=record.submission_state,
            cancel_state=record.cancel_state,
            projection=projection,
        )
