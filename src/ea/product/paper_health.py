"""Read-only operational health projection for the local Paper runtime (PPV-03).

This module separates three questions that must never be conflated:

* LIVENESS -- is the Paper process responsive? The crash-correct owner is the
  store's writer lease: the kernel releases an ``flock`` on process death, so a
  held lease is truthful liveness and a released lease is truthful non-liveness,
  never an operational fault. A released lease is read as *not alive*, not as an
  error, and it is never inferred from reconciliation, market or halt state.
* READINESS -- are the runtime's dependencies and authoritative state complete
  and consistent enough to continue working? It is a conjunction of judgments
  other owners have already made: restart admission, broker reconciliation,
  transport availability, market freshness, strategy liveness and durable
  storage. Nothing here recomputes or re-thresholds those judgments.
* TRADE_PERMISSION -- what PPV-15's ``OperationalSafetyAuthority`` actually
  permits for an outbound effect at this instant. It is obtained by calling
  ``authorize`` with a zero-effect probe and is never derived here.

  A permitted ``trade_permitted`` is a statement about SYSTEM STATE, not about
  any particular order. The probe proposes nothing and prices it exactly at the
  market reference, so it structurally cannot exercise the ``price_deviation``
  guard, and its ``exposure`` and ``order_rate`` outcomes can only reflect the
  headroom that already exists. A zero-effect probe therefore answers "do the
  proposal-independent operational gates currently permit an outbound effect",
  and NOT "will this specific order be accepted": a real order priced away from
  the reference can still return ``DENY``/``price_deviation`` at submission
  while the projection reads permitted. Both verdicts are correct. The document
  states its own scope in ``trade_permission_basis`` and names the guards the
  probe could not exercise in ``trade_permission_unexercised_guards``.

This is an observation surface, never a second safety engine. It owns no halt,
no threshold, no portfolio and no order registry, and it can only report what
the owning authorities already decided. Operator halt is therefore visible as
``process_alive``/``runtime_ready`` true with ``trade_permitted`` false -- the
separation of the three concepts is the point of the projection.

``reason_codes`` is honest about the authority's semantics: ``authorize``
short-circuits and returns only the FIRST non-ALLOW guard, so a blocked
``trade_permitted`` names exactly one guard (``trade_blocking_guard``) and never
claims to enumerate every guard. Readiness codes are this projection's own, one
per dependency it could not find healthy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from typing import Any, final

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId
from ea.core.time import TimeValidationError, require_utc
from ea.risk.operational_safety import (
    OperationalSafetyAuthority,
    OperationalSafetyInput,
    OperationalSafetyVerdict,
)
from ea.runtime.streaming import StreamPhase, StreamStatus, StreamStopReason

PAPER_HEALTH_SCHEMA = "ea.paper-health.v1"
PAPER_HEALTH_PROBE_IDENTITY = "ea.paper-health.zero-effect-probe.v1"

# One code per dependency the readiness conjunction could not find healthy, plus
# the projection's own liveness and trade-permission codes.
LIVENESS_NOT_ALIVE = "liveness.not_alive"
READINESS_RECOVERY = "readiness.recovery"
READINESS_RECONCILIATION = "readiness.reconciliation"
READINESS_MARKET_FRESHNESS = "readiness.market_freshness"
READINESS_BROKER_HEALTH = "readiness.broker_health"
READINESS_STRATEGY_HEARTBEAT = "readiness.strategy_heartbeat"
READINESS_STORAGE = "readiness.storage"
RUNTIME_STOPPED = "runtime.stopped"
RUNTIME_FAULTED = "runtime.faulted"
PROJECTION_STALE = "projection.stale"
TRADE_NO_MARKET_REFERENCE = "trade_permission.no_market_reference"
_TRADE_PERMISSION_PREFIX = "trade_permission."

# The guards a zero-effect probe structurally cannot exercise. The probe prices
# its (nonexistent) effect exactly at the market reference, so the deviation it
# presents is always zero and ``price_deviation`` can never fire on it. The
# ``exposure`` and ``order_rate`` guards are still evaluated -- they simply can
# only reflect headroom that already exists. This is a statement about the
# probe, not a threshold: the authority keeps its own limits untouched.
PROBE_UNEXERCISED_GUARDS: tuple[str, ...] = ("price_deviation",)

# A health slot is either one full projection or one explicit "no projection was
# produced" marker. The marker exists so a failed observation is published as
# unavailable instead of as a healthy-looking absent field.
PAPER_HEALTH_UNAVAILABLE_SCHEMA = "ea.paper-health-unavailable.v1"
_MAX_DETAIL_BYTES = 200

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class PaperHealthError(ValueError):
    """Closed failure at the read-only Paper health projection boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("paper-health errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> PaperHealthError:
    return PaperHealthError(code, message)


class RuntimeState(StrEnum):
    """The runtime's own dispatch phase, resolved with its stop reason."""

    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAULTED = "faulted"
    UNKNOWN = "unknown"


class RecoveryState(StrEnum):
    """The restart-admission verdict (PPV-12) for this attempt."""

    NOT_REQUIRED = "not_required"
    ADMITTED = "admitted"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class ReconciliationState(StrEnum):
    """Whether the ledger retains unresolved broker reconciliation references."""

    CLEAN = "clean"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


class MarketState(StrEnum):
    """The runtime's judgment about the freshness of its market input."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class BrokerState(StrEnum):
    """Whether the transport accepts a new outbound effect."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class StrategyHeartbeatState(StrEnum):
    """Strategy liveness: the runtime is dispatching on a live feed."""

    LIVE = "live"
    NOT_LIVE = "not_live"
    UNKNOWN = "unknown"


class StorageState(StrEnum):
    """Whether durable storage attests it can still record this attempt."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class KillSwitchProjectionState(StrEnum):
    """The operator control as the outbound path sees it.

    ``NOT_CONFIGURED`` mirrors PPV-15, which reads an absent operator authority
    as not halted rather than as healthy.
    """

    ACTIVE = "active"
    HALTED = "halted"
    NOT_CONFIGURED = "not_configured"


class TradePermissionBasis(StrEnum):
    """How the reported ``trade_permitted`` was obtained.

    ``ZERO_EFFECT_PROBE`` is a statement about system state, never about a
    particular order: the probe proposes nothing, so it cannot exercise
    ``price_deviation`` and its exposure and rate outcomes only reflect the
    headroom that already exists. ``NO_MARKET_REFERENCE`` means no probe could
    be formed at all, which always reports ``trade_permitted`` false.
    """

    ZERO_EFFECT_PROBE = "zero_effect_probe"
    NO_MARKET_REFERENCE = "no_market_reference"


@final
@dataclass(frozen=True, slots=True)
class PaperSafetyObservation:
    """The operational state PPV-15 consumes, with no proposed effect.

    Every field is observed from an owning authority; none is computed here.
    The zero-effect probe and a proposed effect are both built from this one
    observation so the two callers cannot drift apart.
    """

    kill_switch_halted: bool
    reconciliation_healthy: bool
    market_age_seconds: float
    broker_available: bool
    strategy_live: bool
    daily_loss: Decimal
    current_exposure: Decimal
    outstanding_order_exposure: Decimal
    open_order_count: int
    reference_price: Decimal
    now_monotonic: float

    def __post_init__(self) -> None:
        for name in (
            "kill_switch_halted",
            "reconciliation_healthy",
            "broker_available",
            "strategy_live",
        ):
            if type(getattr(self, name)) is not bool:
                raise _fail(OutcomeCode.INVALID_TYPE, f"{name} must be an exact bool")
        for name in ("market_age_seconds", "now_monotonic"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value):
                raise _fail(OutcomeCode.INVALID_TYPE, f"{name} must be a finite float")
        for name in (
            "daily_loss",
            "current_exposure",
            "outstanding_order_exposure",
            "reference_price",
        ):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite():
                raise _fail(OutcomeCode.INVALID_TYPE, f"{name} must be a finite Decimal")
        if type(self.open_order_count) is not int or self.open_order_count < 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "open_order_count must be a non-negative int")

    def probe_input(self) -> OperationalSafetyInput:
        """One zero-effect probe: no proposed exposure, priced at the reference.

        The probe proposes nothing, so it can only reveal the guards that are
        already blocking regardless of the effect being proposed. The probe
        identity is a constant marker, never an Order identity.

        Because it proposes nothing and prices at the reference, the probe
        structurally cannot exercise ``price_deviation``: the deviation it
        presents is always exactly zero. Its ``exposure`` and ``order_rate``
        outcomes likewise reflect only the headroom that already exists, since
        neither can see a proposal that is not there. A verdict from this input
        is therefore a statement about system state, not about any particular
        order -- an order priced away from the reference can still be denied
        while a probe on the same state is permitted.
        """
        return OperationalSafetyInput(
            kill_switch_halted=self.kill_switch_halted,
            reconciliation_healthy=self.reconciliation_healthy,
            market_age_seconds=self.market_age_seconds,
            broker_available=self.broker_available,
            strategy_live=self.strategy_live,
            daily_loss=self.daily_loss,
            current_exposure=self.current_exposure,
            outstanding_order_exposure=self.outstanding_order_exposure,
            proposed_order_exposure=Decimal("0"),
            open_order_count=self.open_order_count,
            proposed_effective_price=self.reference_price,
            reference_price=self.reference_price,
            order_identity=PAPER_HEALTH_PROBE_IDENTITY,
            now_monotonic=self.now_monotonic,
        )

    def proposed_input(
        self,
        *,
        proposed_order_exposure: Decimal,
        proposed_effective_price: Decimal,
        order_identity: str,
    ) -> OperationalSafetyInput:
        """The same observed state carrying one real proposed outbound effect."""
        return OperationalSafetyInput(
            kill_switch_halted=self.kill_switch_halted,
            reconciliation_healthy=self.reconciliation_healthy,
            market_age_seconds=self.market_age_seconds,
            broker_available=self.broker_available,
            strategy_live=self.strategy_live,
            daily_loss=self.daily_loss,
            current_exposure=self.current_exposure,
            outstanding_order_exposure=self.outstanding_order_exposure,
            proposed_order_exposure=proposed_order_exposure,
            open_order_count=self.open_order_count,
            proposed_effective_price=proposed_effective_price,
            reference_price=self.reference_price,
            order_identity=order_identity,
            now_monotonic=self.now_monotonic,
        )


@final
@dataclass(frozen=True, slots=True)
class PaperHealthObservation:
    """Already-extracted authority judgments for one health projection.

    ``safety`` is ``None`` when the session has no market reference at all, so
    no zero-effect probe can be formed; the projection then reports a blocked
    trade permission without asking PPV-15 anything, which is fail-closed and
    cannot permit an effect. The authority is never asked with a fabricated
    reference price.
    """

    process_alive: bool
    runtime_state: RuntimeState
    recovery_state: RecoveryState
    reconciliation_state: ReconciliationState
    market_state: MarketState
    broker_state: BrokerState
    strategy_heartbeat_state: StrategyHeartbeatState
    storage_state: StorageState
    kill_switch_state: KillSwitchProjectionState
    safety: PaperSafetyObservation | None
    observed_at: datetime
    run_id: RunId
    candidate_id: str | None

    def __post_init__(self) -> None:
        if type(self.process_alive) is not bool:
            raise _fail(OutcomeCode.INVALID_TYPE, "process_alive must be an exact bool")
        for name, expected in (
            ("runtime_state", RuntimeState),
            ("recovery_state", RecoveryState),
            ("reconciliation_state", ReconciliationState),
            ("market_state", MarketState),
            ("broker_state", BrokerState),
            ("strategy_heartbeat_state", StrategyHeartbeatState),
            ("storage_state", StorageState),
            ("kill_switch_state", KillSwitchProjectionState),
        ):
            if type(getattr(self, name)) is not expected:
                raise _fail(
                    OutcomeCode.INVALID_TYPE, f"{name} must be an exact {expected.__name__}"
                )
        if self.safety is not None and type(self.safety) is not PaperSafetyObservation:
            raise _fail(OutcomeCode.INVALID_TYPE, "safety must be exact or None")
        if type(self.run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
        if self.candidate_id is not None and (
            type(self.candidate_id) is not str or not self.candidate_id.strip()
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "candidate_id must be a non-empty str or None")
        try:
            require_utc(self.observed_at, field="observed_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


@final
@dataclass(frozen=True, slots=True)
class PaperHealthSnapshot:
    """One read-only projection of liveness, readiness and trade permission.

    ``trade_permitted`` states what the proposal-independent operational gates
    permit right now, which is not the same as "this order will be accepted".
    ``trade_permission_basis`` records how it was obtained and
    ``trade_permission_unexercised_guards`` names the guards the probe could not
    exercise, so an operator can tell the two questions apart without reading
    this source.
    """

    process_alive: bool
    runtime_ready: bool
    trade_permitted: bool
    runtime_state: RuntimeState
    recovery_state: RecoveryState
    reconciliation_state: ReconciliationState
    market_state: MarketState
    broker_state: BrokerState
    strategy_heartbeat_state: StrategyHeartbeatState
    storage_state: StorageState
    kill_switch_state: KillSwitchProjectionState
    operator_halt: bool
    reason_codes: tuple[str, ...]
    trade_blocking_guard: str | None
    trade_reason: str | None
    trade_permission_basis: TradePermissionBasis
    trade_permission_unexercised_guards: tuple[str, ...]
    observed_at: datetime
    run_id: RunId
    candidate_id: str | None
    schema: str = PAPER_HEALTH_SCHEMA

    def __post_init__(self) -> None:
        for name in ("process_alive", "runtime_ready", "trade_permitted", "operator_halt"):
            if type(getattr(self, name)) is not bool:
                raise _fail(OutcomeCode.INVALID_TYPE, f"{name} must be an exact bool")
        for name, expected in (
            ("runtime_state", RuntimeState),
            ("recovery_state", RecoveryState),
            ("reconciliation_state", ReconciliationState),
            ("market_state", MarketState),
            ("broker_state", BrokerState),
            ("strategy_heartbeat_state", StrategyHeartbeatState),
            ("storage_state", StorageState),
            ("kill_switch_state", KillSwitchProjectionState),
            ("trade_permission_basis", TradePermissionBasis),
        ):
            if type(getattr(self, name)) is not expected:
                raise _fail(
                    OutcomeCode.INVALID_TYPE, f"{name} must be an exact {expected.__name__}"
                )
        # The probe's blind spot is part of the contract, not a build detail.
        expected_guards = (
            PROBE_UNEXERCISED_GUARDS
            if self.trade_permission_basis is TradePermissionBasis.ZERO_EFFECT_PROBE
            else ()
        )
        if self.trade_permission_unexercised_guards != expected_guards:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "unexercised guards must follow the trade permission basis",
            )
        if self.trade_permitted and (
            self.trade_permission_basis is TradePermissionBasis.NO_MARKET_REFERENCE
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "a probe-less basis never permits a trade")
        if self.schema != PAPER_HEALTH_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "health schema conflicts")
        if type(self.reason_codes) is not tuple or any(
            type(code) is not str or not code.strip() for code in self.reason_codes
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "reason_codes must be non-empty strings")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise _fail(OutcomeCode.CONFLICTING_ID, "reason_codes must not repeat")
        for name in ("trade_blocking_guard", "trade_reason"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value.strip()):
                raise _fail(OutcomeCode.OUT_OF_RANGE, f"{name} must be a non-empty str or None")
        if self.trade_permitted and (
            self.trade_blocking_guard is not None or self.trade_reason is not None
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "a permitted trade carries no blocking reason")
        if self.operator_halt != (self.kill_switch_state is KillSwitchProjectionState.HALTED):
            raise _fail(OutcomeCode.CONFLICTING_ID, "operator_halt must follow the kill switch")
        if type(self.run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
        if self.candidate_id is not None and (
            type(self.candidate_id) is not str or not self.candidate_id.strip()
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "candidate_id must be a non-empty str or None")
        try:
            require_utc(self.observed_at, field="observed_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error

    def document(self) -> dict[str, Any]:
        """The canonical JSON-safe projection, as published and as re-read.

        ``process_alive`` and ``projection_stale`` are recorded here as the
        values true at publication. Both are observations of the live writer
        lease, so an offline reader MUST overwrite them from its own lease
        observation -- see :func:`observed_paper_health`.
        """
        return {
            "schema": self.schema,
            "run_id": self.run_id.value,
            "candidate_id": self.candidate_id,
            "process_alive": self.process_alive,
            "projection_stale": False,
            "runtime_ready": self.runtime_ready,
            "trade_permitted": self.trade_permitted,
            "operator_halt": self.operator_halt,
            "runtime_state": self.runtime_state.value,
            "recovery_state": self.recovery_state.value,
            "reconciliation_state": self.reconciliation_state.value,
            "market_state": self.market_state.value,
            "broker_state": self.broker_state.value,
            "strategy_heartbeat_state": self.strategy_heartbeat_state.value,
            "storage_state": self.storage_state.value,
            "kill_switch_state": self.kill_switch_state.value,
            "reason_codes": list(self.reason_codes),
            "trade_blocking_guard": self.trade_blocking_guard,
            "trade_reason": self.trade_reason,
            "trade_permission_basis": self.trade_permission_basis.value,
            "trade_permission_unexercised_guards": list(self.trade_permission_unexercised_guards),
            "observed_at": self.observed_at.isoformat(),
        }


def _recovery_ready(state: RecoveryState) -> bool:
    """A terminal attempt is complete, not ready to continue working."""
    return state in (RecoveryState.NOT_REQUIRED, RecoveryState.ADMITTED)


def runtime_state_from_stream(status: StreamStatus) -> RuntimeState:
    """Project the runtime's own phase and stop reason onto one closed state."""
    if type(status) is not StreamStatus:
        raise _fail(OutcomeCode.INVALID_TYPE, "stream status must be exact")
    if status.phase is StreamPhase.RUNNING:
        return RuntimeState.RUNNING
    if status.phase is StreamPhase.DRAINING:
        return RuntimeState.DRAINING
    reason = status.reason
    if reason in (
        StreamStopReason.STOP_REQUESTED,
        StreamStopReason.SOURCE_EXHAUSTED,
        None,
    ):
        return RuntimeState.STOPPED
    return RuntimeState.FAULTED


def market_state_from_stream(
    status: StreamStatus,
    *,
    market_age_seconds: float | None,
    max_market_age_seconds: float,
) -> MarketState:
    """Project market freshness without inventing a threshold.

    The runtime's own stop reason is authoritative when it already refused its
    input, and otherwise the comparison reuses the configured market-age limit
    the PPV-15 market guard itself applies. A session that has not observed any
    market yet reports ``UNKNOWN`` rather than a fabricated age.
    """
    if type(status) is not StreamStatus:
        raise _fail(OutcomeCode.INVALID_TYPE, "stream status must be exact")
    if status.reason in (StreamStopReason.STALE_MARKET, StreamStopReason.SOURCE_STALLED):
        return MarketState.STALE
    if market_age_seconds is None:
        return MarketState.UNKNOWN
    if type(market_age_seconds) is not float or not isfinite(market_age_seconds):
        raise _fail(OutcomeCode.INVALID_TYPE, "market_age_seconds must be a finite float")
    if type(max_market_age_seconds) is not float or not isfinite(max_market_age_seconds):
        raise _fail(OutcomeCode.INVALID_TYPE, "max_market_age_seconds must be a finite float")
    return MarketState.FRESH if market_age_seconds <= max_market_age_seconds else MarketState.STALE


def build_paper_health(
    observation: PaperHealthObservation,
    *,
    authority: OperationalSafetyAuthority,
) -> PaperHealthSnapshot:
    """Project one health snapshot from judgments other authorities already made.

    The only authority consulted is PPV-15, and only through the zero-effect
    probe. Readiness is a conjunction of the dependency verdicts carried by the
    observation; nothing here thresholds, repairs, halts or resends.

    ``authorize`` prunes its own expired rate-window entries through
    ``_rate_count`` before counting. That prune is decision-neutral -- it drops
    only submissions that have already aged out of the window and cannot change
    any subsequent verdict -- but the probe does touch that internal window, so
    a snapshot is not a perfectly pure read of the authority.
    """
    if type(observation) is not PaperHealthObservation:
        raise _fail(OutcomeCode.INVALID_TYPE, "health observation must be exact")
    if type(authority) is not OperationalSafetyAuthority:
        raise _fail(OutcomeCode.INVALID_TYPE, "authority must be an exact safety authority")

    reasons: set[str] = set()
    if observation.process_alive:
        ready = True
    else:
        ready = False
        reasons.add(LIVENESS_NOT_ALIVE)
    for code, healthy in (
        (READINESS_RECOVERY, _recovery_ready(observation.recovery_state)),
        (
            READINESS_RECONCILIATION,
            observation.reconciliation_state is ReconciliationState.CLEAN,
        ),
        (READINESS_MARKET_FRESHNESS, observation.market_state is MarketState.FRESH),
        (READINESS_BROKER_HEALTH, observation.broker_state is BrokerState.AVAILABLE),
        (
            READINESS_STRATEGY_HEARTBEAT,
            observation.strategy_heartbeat_state is StrategyHeartbeatState.LIVE,
        ),
        (READINESS_STORAGE, observation.storage_state is StorageState.AVAILABLE),
    ):
        if not healthy:
            ready = False
            reasons.add(code)
    if observation.runtime_state is RuntimeState.FAULTED:
        reasons.add(RUNTIME_FAULTED)
    elif observation.runtime_state is RuntimeState.STOPPED:
        reasons.add(RUNTIME_STOPPED)

    blocking_guard: str | None = None
    trade_reason: str | None = None
    if observation.safety is None:
        trade_permitted = False
        basis = TradePermissionBasis.NO_MARKET_REFERENCE
        reasons.add(TRADE_NO_MARKET_REFERENCE)
    else:
        decision = authority.authorize(observation.safety.probe_input())
        basis = TradePermissionBasis.ZERO_EFFECT_PROBE
        trade_permitted = decision.verdict is OperationalSafetyVerdict.ALLOW
        if not trade_permitted:
            blocking_guard = decision.guard
            trade_reason = decision.reason
            reasons.add(f"{_TRADE_PERMISSION_PREFIX}{decision.guard}")

    return PaperHealthSnapshot(
        process_alive=observation.process_alive,
        runtime_ready=ready,
        trade_permitted=trade_permitted,
        runtime_state=observation.runtime_state,
        recovery_state=observation.recovery_state,
        reconciliation_state=observation.reconciliation_state,
        market_state=observation.market_state,
        broker_state=observation.broker_state,
        strategy_heartbeat_state=observation.strategy_heartbeat_state,
        storage_state=observation.storage_state,
        kill_switch_state=observation.kill_switch_state,
        operator_halt=observation.kill_switch_state is KillSwitchProjectionState.HALTED,
        reason_codes=tuple(sorted(reasons)),
        trade_blocking_guard=blocking_guard,
        trade_reason=trade_reason,
        trade_permission_basis=basis,
        trade_permission_unexercised_guards=(
            PROBE_UNEXERCISED_GUARDS if basis is TradePermissionBasis.ZERO_EFFECT_PROBE else ()
        ),
        observed_at=observation.observed_at,
        run_id=observation.run_id,
        candidate_id=observation.candidate_id,
    )


def canonical_paper_health_bytes(snapshot: PaperHealthSnapshot) -> bytes:
    """Return the canonical durable document for one health projection."""
    if type(snapshot) is not PaperHealthSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot must be an exact health snapshot")
    return json.dumps(
        snapshot.document(),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _require_str(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if type(value) is not str or not value.strip():
        raise _fail(OutcomeCode.INVALID_TYPE, f"health {key} must be a non-empty str")
    return value


def _require_bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if type(value) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, f"health {key} must be an exact bool")
    return value


def _require_enum(document: dict[str, Any], key: str, expected: type[StrEnum]) -> Any:
    value = document.get(key)
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"health {key} must be a str")
    try:
        return expected(value)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"health {key} is not a known state") from error


def decode_paper_health(document: object) -> PaperHealthSnapshot:
    """Rebuild one health projection from its durable document; malformed input fails closed."""
    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "health document must be one object")
    if document.get("schema") != PAPER_HEALTH_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "health schema conflicts")
    candidate_id = document.get("candidate_id")
    if candidate_id is not None and (type(candidate_id) is not str or not candidate_id.strip()):
        raise _fail(OutcomeCode.INVALID_TYPE, "health candidate_id must be a non-empty str or None")
    blocking_guard = document.get("trade_blocking_guard")
    trade_reason = document.get("trade_reason")
    for name, value in (("trade_blocking_guard", blocking_guard), ("trade_reason", trade_reason)):
        if value is not None and (type(value) is not str or not value.strip()):
            raise _fail(OutcomeCode.INVALID_TYPE, f"health {name} must be a non-empty str or None")
    codes = document.get("reason_codes")
    if type(codes) is not list or any(type(code) is not str or not code.strip() for code in codes):
        raise _fail(OutcomeCode.INVALID_TYPE, "health reason_codes must be one list of strings")
    unexercised = document.get("trade_permission_unexercised_guards")
    if type(unexercised) is not list or any(
        type(guard) is not str or not guard.strip() for guard in unexercised
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "health trade_permission_unexercised_guards must be one list of strings",
        )
    try:
        run_id = RunId(_require_str(document, "run_id"))
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "health run_id conflicts") from error
    observed_at = _require_str(document, "observed_at")
    try:
        parsed = datetime.fromisoformat(observed_at)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "health observed_at is not a timestamp") from error
    try:
        return PaperHealthSnapshot(
            process_alive=_require_bool(document, "process_alive"),
            runtime_ready=_require_bool(document, "runtime_ready"),
            trade_permitted=_require_bool(document, "trade_permitted"),
            runtime_state=_require_enum(document, "runtime_state", RuntimeState),
            recovery_state=_require_enum(document, "recovery_state", RecoveryState),
            reconciliation_state=_require_enum(
                document, "reconciliation_state", ReconciliationState
            ),
            market_state=_require_enum(document, "market_state", MarketState),
            broker_state=_require_enum(document, "broker_state", BrokerState),
            strategy_heartbeat_state=_require_enum(
                document, "strategy_heartbeat_state", StrategyHeartbeatState
            ),
            storage_state=_require_enum(document, "storage_state", StorageState),
            kill_switch_state=_require_enum(
                document, "kill_switch_state", KillSwitchProjectionState
            ),
            operator_halt=_require_bool(document, "operator_halt"),
            reason_codes=tuple(codes),
            trade_blocking_guard=blocking_guard,
            trade_reason=trade_reason,
            trade_permission_basis=_require_enum(
                document, "trade_permission_basis", TradePermissionBasis
            ),
            trade_permission_unexercised_guards=tuple(unexercised),
            observed_at=parsed,
            run_id=run_id,
            candidate_id=candidate_id,
        )
    except PaperHealthError:
        raise
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "health document conflicts") from error


def observed_paper_health(
    document: object,
    *,
    process_alive: bool,
    now: datetime,
    max_projection_age_seconds: float,
) -> dict[str, Any]:
    """Overlay live observations on one durable health projection.

    ``process_alive`` is the live ``flock`` observation of the writer lease, not
    the value recorded when the projection was published: the kernel releases
    the lease when the writer dies, so a released lease is truthful
    non-liveness. Liveness therefore comes only from the lease.

    ``max_projection_age_seconds`` is a reader-side freshness bound on the
    durable bytes, not a readiness or safety threshold. A projection no held
    lease can corroborate, or one older than that bound, is marked
    ``projection_stale`` and its readiness is demoted: readiness read from
    frozen bytes is not a current readiness verdict.
    """
    if type(process_alive) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "process_alive must be an exact bool")
    if type(max_projection_age_seconds) is not float or not isfinite(max_projection_age_seconds):
        raise _fail(OutcomeCode.INVALID_TYPE, "max_projection_age_seconds must be a finite float")
    try:
        now = require_utc(now, field="now")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error

    snapshot = decode_paper_health(document)
    age_seconds = (now - snapshot.observed_at).total_seconds()
    reasons = set(snapshot.reason_codes)
    if not process_alive:
        reasons.add(LIVENESS_NOT_ALIVE)
    stale = not process_alive or age_seconds > max_projection_age_seconds
    if stale:
        reasons.add(PROJECTION_STALE)

    result = snapshot.document()
    result["process_alive"] = process_alive
    result["projection_stale"] = stale
    result["age_seconds"] = age_seconds
    result["runtime_ready"] = snapshot.runtime_ready and not stale
    result["reason_codes"] = sorted(reasons)
    return result


def paper_health_unavailable_document(*, detail: str) -> dict[str, Any]:
    """One explicit "the projection could not be produced" marker.

    A failed observation must never fail the run that publishes it, and must
    never be published as if it were a healthy projection. This marker is a
    closed, bounded alternative to the full projection.
    """
    if type(detail) is not str or not detail.strip():
        raise _fail(OutcomeCode.OUT_OF_RANGE, "unavailable detail must be non-empty text")
    bounded = detail[:_MAX_DETAIL_BYTES]
    if len(bounded.encode("utf-8")) > _MAX_DETAIL_BYTES:
        bounded = bounded.encode("utf-8")[:_MAX_DETAIL_BYTES].decode("utf-8", "ignore")
    return {
        "schema": PAPER_HEALTH_UNAVAILABLE_SCHEMA,
        "available": False,
        "detail": bounded,
    }


def is_paper_health_unavailable(document: object) -> bool:
    """True for the explicit unavailable marker, never for a real projection."""
    return (
        type(document) is dict
        and document.get("schema") == PAPER_HEALTH_UNAVAILABLE_SCHEMA
        and document.get("available") is False
        and type(document.get("detail")) is str
    )
