"""Bounded, single-dispatch live market ingress with ephemeral authority proofs.

The runtime owns ordering and dispatch visibility, not market production, orders,
facts, or economic state. Callers supply both clocks and all effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import final

from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import IngressIdentity
from ea.core.execution_messages import (
    ExecutionFactIngress,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
)
from ea.core.market_data import (
    MarketDataEnvelope,
    SourceId,
    admission_order_key,
    is_visible_as_of,
)
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId
from ea.core.runtime import (
    ActiveMarketDispatchProof,
    RuntimeRootOrderKey,
    _create_active_market_dispatch_proof,
    runtime_root_order_key,
)
from ea.core.strategy import StrategyContractError, causal_market_digest
from ea.core.time import Clock, require_utc
from ea.runtime.queue import ExecutionFactIssuanceVerifier


class StreamingRuntimeError(ValueError):
    """Invalid input, bound breach, or dispatch operation."""


class StreamPhase(StrEnum):
    RUNNING = "running"
    DRAINING = "draining"
    CLOSED = "closed"


class StreamStopReason(StrEnum):
    STOP_REQUESTED = "stop_requested"
    SOURCE_EXHAUSTED = "source_exhausted"
    SOURCE_STALLED = "source_stalled"
    STALE_MARKET = "stale_market"
    CALLBACK_ERROR = "callback_error"
    SOURCE_ERROR = "source_error"


@final
@dataclass(frozen=True, slots=True)
class StreamStatus:
    phase: StreamPhase
    reason: StreamStopReason | None
    dispatch_sequence: int
    market_dispatches: int
    fact_dispatches: int
    pending_facts: int
    pending_market: bool
    heartbeat_count: int
    failed_dispatch_kind: str | None


@final
class StreamingMarketRuntime:
    """One-source incremental admission and globally sequenced active dispatch."""

    def __init__(
        self,
        *,
        run_id: RunId,
        spec_set: InstrumentExecutionSpecSet,
        source_id: SourceId,
        clock: Clock,
        monotonic: Callable[[], float],
        max_market_age_seconds: float,
        stall_timeout_seconds: float,
        fact_source: ExecutionFactIssuanceVerifier | None = None,
        max_pending_facts: int = 64,
    ) -> None:
        if type(run_id) is not RunId or type(spec_set) is not InstrumentExecutionSpecSet:
            raise StreamingRuntimeError("run/spec bindings must be exact")
        if type(source_id) is not SourceId or not callable(clock.now) or not callable(monotonic):
            raise StreamingRuntimeError("source and injected clocks are required")
        for value, name in (
            (max_market_age_seconds, "max_market_age_seconds"),
            (stall_timeout_seconds, "stall_timeout_seconds"),
        ):
            if type(value) is not float or not isfinite(value) or value <= 0:
                raise StreamingRuntimeError(f"{name} must be a positive finite float")
        if type(max_pending_facts) is not int or max_pending_facts < 1:
            raise StreamingRuntimeError("max_pending_facts must be a positive int")
        if fact_source is not None:
            try:
                binding_ok = (
                    fact_source.run_id == run_id
                    and fact_source.spec_set.identifier == spec_set.identifier
                    and instrument_spec_set_digest(fact_source.spec_set)
                    == instrument_spec_set_digest(spec_set)
                    and callable(fact_source.has_issued_ingress)
                )
            except (AttributeError, TypeError, ValueError) as error:
                raise StreamingRuntimeError("fact source binding is incomplete") from error
            if not binding_ok:
                raise StreamingRuntimeError("fact source binding conflicts")
        self.run_id = run_id
        self.spec_set = spec_set
        self.source_id = source_id
        self._clock = clock
        self._monotonic = monotonic
        self._max_age = max_market_age_seconds
        self._stall_timeout = stall_timeout_seconds
        self._fact_source = fact_source
        self._max_pending_facts = max_pending_facts
        self._phase = StreamPhase.RUNNING
        self._reason: StreamStopReason | None = None
        self._pending_market: MarketDataEnvelope | None = None
        self._pending_facts: list[ExecutionFactIngress] = []
        self._last_market: MarketDataEnvelope | None = None
        self._last_dispatch_key: RuntimeRootOrderKey | None = None
        self._active_market: tuple[MarketDataEnvelope, bytes, int] | None = None
        self._active_fact: tuple[IngressIdentity, bytes, bytes, int] | None = None
        self._dispatching = False
        self._dispatch_sequence = 0
        self._market_dispatches = 0
        self._fact_dispatches = 0
        self._heartbeat_count = 0
        self._failed_dispatch_kind: str | None = None
        self._last_utc = require_utc(clock.now(), field="clock.now()")
        self._last_mono = self._read_monotonic()
        self._last_market_mono = self._last_mono

    @property
    def phase(self) -> StreamPhase:
        return self._phase

    @property
    def reason(self) -> StreamStopReason | None:
        return self._reason

    def _read_monotonic(self) -> float:
        value = self._monotonic()
        if type(value) is not float or not isfinite(value) or value < 0:
            raise StreamingRuntimeError("monotonic clock must return a nonnegative finite float")
        return value

    def _sample(self) -> tuple[datetime, float]:
        now = require_utc(self._clock.now(), field="clock.now()")
        mono = self._read_monotonic()
        if now < self._last_utc or mono < self._last_mono:
            raise StreamingRuntimeError("injected clocks regressed")
        self._last_utc, self._last_mono = now, mono
        return now, mono

    def status(self) -> StreamStatus:
        return StreamStatus(
            self._phase,
            self._reason,
            self._dispatch_sequence,
            self._market_dispatches,
            self._fact_dispatches,
            len(self._pending_facts),
            self._pending_market is not None,
            self._heartbeat_count,
            self._failed_dispatch_kind,
        )

    def heartbeat(self) -> StreamStatus:
        if self._phase is StreamPhase.CLOSED:
            return self.status()
        _, mono = self._sample()
        self._heartbeat_count += 1
        if (
            self._phase is StreamPhase.RUNNING
            and mono - self._last_market_mono > self._stall_timeout
        ):
            self._drain(StreamStopReason.SOURCE_STALLED)
        return self.status()

    def _drain(self, reason: StreamStopReason) -> None:
        if self._phase is StreamPhase.RUNNING:
            self._phase = StreamPhase.DRAINING
            self._reason = reason
            self._pending_market = None

    def fail_source(self) -> None:
        """Cut off market dispatch after a source/clock/lifetime failure."""
        if self._phase is not StreamPhase.CLOSED:
            self._phase = StreamPhase.DRAINING
            self._reason = StreamStopReason.SOURCE_ERROR
            self._pending_market = None

    def fail_heartbeat_callback(self) -> None:
        """Cut off market dispatch after the outer heartbeat observer fails."""
        if self._phase is not StreamPhase.CLOSED:
            self._phase = StreamPhase.DRAINING
            self._reason = StreamStopReason.CALLBACK_ERROR
            self._failed_dispatch_kind = "heartbeat"
            self._pending_market = None

    def receive_market(self, event: MarketDataEnvelope) -> None:
        if self._phase is not StreamPhase.RUNNING or self._dispatching:
            raise StreamingRuntimeError("market input is stopped or dispatch is active")
        if type(event) is not MarketDataEnvelope or event.source != self.source_id:
            raise StreamingRuntimeError("market input has wrong type or source")
        if self._pending_market is not None:
            raise StreamingRuntimeError("one pending market event is already buffered")
        prior = self._last_market
        if prior is not None:
            if event.source_sequence == prior.source_sequence:
                label = "duplicate" if event == prior else "conflicting"
                raise StreamingRuntimeError(f"{label} source emission")
            if event.record_key == prior.record_key:
                label = "duplicate" if event == prior else "conflicting"
                raise StreamingRuntimeError(f"{label} record version")
            if event.source_sequence < prior.source_sequence or admission_order_key(
                event
            ) <= admission_order_key(prior):
                raise StreamingRuntimeError("out-of-order market input")
            if event.logical_key == prior.logical_key and event.revision <= prior.revision:
                raise StreamingRuntimeError("conflicting market revision")
            # This local streaming profile accepts only forward event time.
            # Historical corrections/backfills need a separate retained-history
            # contract; never accept an old record again with fresh availability.
            if event.event_time <= prior.event_time:
                raise StreamingRuntimeError("market event time must strictly advance")
        key = runtime_root_order_key(event)
        if self._last_dispatch_key is not None and key <= self._last_dispatch_key:
            raise StreamingRuntimeError("out-of-order market input after dispatch")
        self._pending_market = event
        self._last_market = event

    def enqueue_fact(self, ingress: ExecutionFactIngress) -> None:
        if self._phase is StreamPhase.CLOSED:
            raise StreamingRuntimeError("runtime is closed")
        source = self._fact_source
        if source is None:
            raise StreamingRuntimeError("no fact source is bound")
        if type(ingress) is not ExecutionFactIngress:
            raise StreamingRuntimeError("fact ingress must be exact")
        if len(self._pending_facts) >= self._max_pending_facts:
            raise StreamingRuntimeError("pending fact capacity exceeded")
        ingress_bytes = canonical_execution_fact_ingress_bytes(ingress)
        fact_bytes = canonical_execution_fact_bytes(ingress.fact)
        if ingress.source_namespace != source.source_namespace or not source.has_issued_ingress(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=ingress_bytes,
            canonical_fact_bytes=fact_bytes,
        ):
            raise StreamingRuntimeError("fact ingress was not issued by the bound source")
        if any(existing.identity == ingress.identity for existing in self._pending_facts):
            raise StreamingRuntimeError("duplicate or conflicting pending fact ingress")
        self._pending_facts.append(ingress)

    def request_stop(self) -> None:
        self._drain(StreamStopReason.STOP_REQUESTED)

    def mark_source_exhausted(self) -> None:
        self._drain(StreamStopReason.SOURCE_EXHAUSTED)

    def close(self) -> None:
        if self._dispatching:
            raise StreamingRuntimeError("cannot close during active dispatch")
        self._drain(StreamStopReason.STOP_REQUESTED)
        if self._pending_facts:
            raise StreamingRuntimeError("pending facts must drain before close")
        self._phase = StreamPhase.CLOSED

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> ActiveMarketDispatchProof:
        active = self._active_market
        if (
            active is None
            or market_root is not active[0]
            or type(dispatch_sequence) is not int
            or dispatch_sequence != active[2]
            or canonical_market_data_record_bytes(market_root) != active[1]
        ):
            raise StrategyContractError(
                OutcomeCode.CONFLICTING_ID, "no exact active market dispatch"
            )
        return _create_active_market_dispatch_proof(
            run_id=self.run_id,
            market_root=market_root,
            canonical_market_bytes=active[1],
            causal_market_sha256=causal_market_digest(market_root),
            dispatch_sequence=dispatch_sequence,
            issuer=self,
        )

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None:
        active = self._active_fact
        if (
            active is None
            or type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
            or active[:3]
            != (
                ingress_identity,
                canonical_ingress_bytes,
                canonical_fact_bytes,
            )
        ):
            return None
        return active[3]

    def poll(
        self,
        *,
        on_market: Callable[[MarketDataEnvelope, int], None],
        on_fact: Callable[[ExecutionFactIngress, int], None],
    ) -> bool:
        """Dispatch at most one visible root; callbacks execute in one serial lease."""
        if self._phase is StreamPhase.CLOSED:
            return False
        if self._dispatching:
            raise StreamingRuntimeError("dispatch cannot be re-entered")
        if not callable(on_market) or not callable(on_fact):
            raise StreamingRuntimeError("dispatch callbacks must be callable")
        self.heartbeat()
        now = self._last_utc
        eligible: list[tuple[RuntimeRootOrderKey, MarketDataEnvelope | ExecutionFactIngress]] = []
        market = self._pending_market if self._phase is StreamPhase.RUNNING else None
        if market is not None and is_visible_as_of(market, now):
            if (now - market.event_time).total_seconds() > self._max_age:
                self._drain(StreamStopReason.STALE_MARKET)
            else:
                eligible.append((runtime_root_order_key(market), market))
        for ingress in self._pending_facts:
            if ingress.available_at <= now:
                eligible.append((runtime_root_order_key(ingress), ingress))
        if not eligible:
            return False
        key, root = min(eligible, key=lambda item: item[0])
        if (
            type(root) is MarketDataEnvelope
            and self._last_dispatch_key is not None
            and key <= self._last_dispatch_key
        ):
            self._drain(StreamStopReason.CALLBACK_ERROR)
            raise StreamingRuntimeError("runtime root arrived out of dispatch order")
        if self._dispatch_sequence >= (1 << 64) - 1:
            self._drain(StreamStopReason.CALLBACK_ERROR)
            raise StreamingRuntimeError("dispatch sequence exhausted")
        sequence = self._dispatch_sequence + 1
        # Reserve before invoking user code: a callback can publish a signal or
        # enqueue an issued fact and then fail. That sequence must never repeat.
        self._dispatch_sequence = sequence
        self._last_dispatch_key = key
        self._dispatching = True
        try:
            if isinstance(root, MarketDataEnvelope):
                market_bytes = canonical_market_data_record_bytes(root)
                self._active_market = (root, market_bytes, sequence)
                on_market(root, sequence)
                self._pending_market = None
                self._market_dispatches += 1
                self._last_market_mono = self._last_mono
            else:
                ingress_bytes = canonical_execution_fact_ingress_bytes(root)
                fact_bytes = canonical_execution_fact_bytes(root.fact)
                self._active_fact = (root.identity, ingress_bytes, fact_bytes, sequence)
                on_fact(root, sequence)
                self._pending_facts.remove(root)
                self._fact_dispatches += 1
            return True
        except BaseException:
            self._phase = StreamPhase.DRAINING
            self._reason = StreamStopReason.CALLBACK_ERROR
            self._pending_market = None
            self._failed_dispatch_kind = (
                "market" if isinstance(root, MarketDataEnvelope) else "fact"
            )
            raise
        finally:
            self._active_market = None
            self._active_fact = None
            self._dispatching = False
