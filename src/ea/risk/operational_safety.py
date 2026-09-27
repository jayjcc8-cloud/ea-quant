"""Operational safety authority for the Paper outbound boundary (PPV-15).

This is the single, non-bypassable gate every broker outbound effect must pass
immediately before transport. It consumes authoritative state it does not own:
the operator kill switch (PPV-14), broker reconciliation health (PPV-13), market
freshness and runtime liveness (PPV-10), the ledger/portfolio snapshot, and the
authoritative Order projection. It owns no second portfolio, order registry,
reconciliation, halt, or market-health authority.

A decision is one closed result:

* ``ALLOW`` -- every guard permits the effect.
* ``DENY(reason)`` -- this order is refused; no broker effect, no halt.
* ``HALT(reason)`` -- the operational state is unsafe; the runtime must engage
  its authoritative halt and produce no further broker effects.

HALT is fail-closed and monotone at the runtime boundary: it is resolved through
the existing risk authority ``EXTERNAL_SAFETY_HALT``, never a PPV-15-specific
halt. DENY is per-order and does not mutate safety state.

The same contract may be invoked twice around a durable pre-effect audit: once to
record the authorization and once to re-check volatile state immediately before
transport. There is exactly one guard implementation, never two.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from math import isfinite
from typing import final

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId

_LIMITS_SCHEMA = "ea.operational-safety-limits.v1"

_ALLOW_DECISION: OperationalSafetyDecision

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class OperationalSafetyError(ValueError):
    """Closed failure at the operational safety boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("operational-safety errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> OperationalSafetyError:
    return OperationalSafetyError(code, message)


class OperationalSafetyVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    HALT = "halt"


@final
@dataclass(frozen=True, slots=True)
class OperationalSafetyDecision:
    """One closed guard outcome with a minimal explainable reason."""

    verdict: OperationalSafetyVerdict
    guard: str
    reason: str
    limit: str | None = None
    observed: str | None = None
    identity: str | None = None

    def __post_init__(self) -> None:
        if type(self.verdict) is not OperationalSafetyVerdict:
            raise _fail(OutcomeCode.INVALID_TYPE, "decision verdict must be exact")
        if type(self.guard) is not str or not self.guard.strip():
            raise _fail(OutcomeCode.OUT_OF_RANGE, "decision guard must be non-empty")
        if type(self.reason) is not str or not self.reason.strip():
            raise _fail(OutcomeCode.OUT_OF_RANGE, "decision reason must be non-empty")


@final
@dataclass(frozen=True, slots=True)
class OperationalSafetyLimits:
    """Configured operational limits for one run-bound safety authority.

    ``max_daily_loss`` and ``max_total_exposure`` are currency units;
    ``max_open_orders`` and ``max_order_rate`` are counts;
    ``max_price_deviation_bps`` is in basis points.
    """

    max_market_age_seconds: float
    max_daily_loss: Decimal
    max_total_exposure: Decimal
    max_open_orders: int
    max_order_rate: int
    order_rate_window_seconds: float
    max_price_deviation_bps: int

    def __post_init__(self) -> None:
        for name in ("max_market_age_seconds", "order_rate_window_seconds"):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value <= 0:
                raise _fail(OutcomeCode.OUT_OF_RANGE, f"{name} must be a positive finite float")
        for name in ("max_daily_loss", "max_total_exposure"):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise _fail(
                    OutcomeCode.OUT_OF_RANGE, f"{name} must be a non-negative finite Decimal"
                )
        for name in ("max_open_orders", "max_order_rate", "max_price_deviation_bps"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise _fail(OutcomeCode.OUT_OF_RANGE, f"{name} must be a positive int")


@final
@dataclass(frozen=True, slots=True)
class OperationalSafetyInput:
    """Authoritative observed state plus the one proposed outbound effect."""

    kill_switch_halted: bool
    reconciliation_healthy: bool
    market_age_seconds: float
    broker_available: bool
    strategy_live: bool
    daily_loss: Decimal
    current_exposure: Decimal
    outstanding_order_exposure: Decimal
    proposed_order_exposure: Decimal
    open_order_count: int
    proposed_effective_price: Decimal
    reference_price: Decimal
    order_identity: str
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
        if type(self.market_age_seconds) is not float or not isfinite(self.market_age_seconds):
            raise _fail(OutcomeCode.INVALID_TYPE, "market_age_seconds must be a finite float")
        if type(self.now_monotonic) is not float or not isfinite(self.now_monotonic):
            raise _fail(OutcomeCode.INVALID_TYPE, "now_monotonic must be a finite float")
        for name in (
            "daily_loss",
            "current_exposure",
            "outstanding_order_exposure",
            "proposed_order_exposure",
            "proposed_effective_price",
            "reference_price",
        ):
            if type(getattr(self, name)) is not Decimal or not getattr(self, name).is_finite():
                raise _fail(OutcomeCode.INVALID_TYPE, f"{name} must be a finite Decimal")
        if type(self.open_order_count) is not int or self.open_order_count < 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "open_order_count must be a non-negative int")
        if type(self.order_identity) is not str or not self.order_identity.strip():
            raise _fail(OutcomeCode.OUT_OF_RANGE, "order_identity must be non-empty")


def _decision(
    verdict: OperationalSafetyVerdict,
    guard: str,
    reason: str,
    *,
    limit: object | None = None,
    observed: object | None = None,
    identity: str | None = None,
) -> OperationalSafetyDecision:
    return OperationalSafetyDecision(
        verdict,
        guard,
        reason,
        None if limit is None else str(limit),
        None if observed is None else str(observed),
        identity,
    )


@final
class OperationalSafetyAuthority:
    """One run-bound authority that gates every outbound broker effect.

    ``authorize`` is read-only over the recorded submission window and never
    mutates it; ``record_submission`` advances the window only after a real
    broker effect. Recovery seeds the window from durable submission ages so a
    restart never clears the rate state.
    """

    _limits: OperationalSafetyLimits
    _submissions: deque[float]

    __slots__ = ("_limits", "_run_id", "_submissions")

    def __init__(self, *, run_id: RunId, limits: OperationalSafetyLimits) -> None:
        if type(run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
        if type(limits) is not OperationalSafetyLimits:
            raise _fail(OutcomeCode.INVALID_TYPE, "limits must be an exact OperationalSafetyLimits")
        self._run_id = run_id
        self._limits = limits
        self._submissions: deque[float] = deque()

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def limits(self) -> OperationalSafetyLimits:
        return self._limits

    def authorize(self, safety_input: OperationalSafetyInput) -> OperationalSafetyDecision:
        """Return the first non-ALLOW guard outcome, else ALLOW."""
        if type(safety_input) is not OperationalSafetyInput:
            raise _fail(OutcomeCode.INVALID_TYPE, "safety input must be exact")
        identity = safety_input.order_identity
        limits = self._limits

        if safety_input.kill_switch_halted:
            return _decision(
                OperationalSafetyVerdict.HALT,
                "kill_switch",
                "operator kill switch is halted",
                identity=identity,
            )
        if not safety_input.reconciliation_healthy:
            return _decision(
                OperationalSafetyVerdict.HALT,
                "reconciliation",
                "unresolved reconciliation state",
                identity=identity,
            )
        if safety_input.market_age_seconds > limits.max_market_age_seconds:
            return _decision(
                OperationalSafetyVerdict.HALT,
                "market_freshness",
                "market is stale",
                limit=f"{limits.max_market_age_seconds}s",
                observed=f"{safety_input.market_age_seconds}s",
                identity=identity,
            )
        if not safety_input.broker_available:
            return _decision(
                OperationalSafetyVerdict.HALT,
                "broker_health",
                "broker is unavailable",
                identity=identity,
            )
        if not safety_input.strategy_live:
            return _decision(
                OperationalSafetyVerdict.HALT,
                "strategy_heartbeat",
                "strategy liveness deadline exceeded",
                identity=identity,
            )
        if safety_input.daily_loss > limits.max_daily_loss:
            return _decision(
                OperationalSafetyVerdict.HALT,
                "daily_loss",
                "maximum daily loss exceeded",
                limit=str(limits.max_daily_loss),
                observed=str(safety_input.daily_loss),
                identity=identity,
            )
        total_exposure = (
            safety_input.current_exposure
            + safety_input.outstanding_order_exposure
            + safety_input.proposed_order_exposure
        )
        if total_exposure > limits.max_total_exposure:
            return _decision(
                OperationalSafetyVerdict.DENY,
                "exposure",
                "maximum total exposure exceeded",
                limit=str(limits.max_total_exposure),
                observed=str(total_exposure),
                identity=identity,
            )
        if safety_input.open_order_count >= limits.max_open_orders:
            return _decision(
                OperationalSafetyVerdict.DENY,
                "open_orders",
                "maximum open orders reached",
                limit=str(limits.max_open_orders),
                observed=str(safety_input.open_order_count),
                identity=identity,
            )
        if self._rate_count(safety_input.now_monotonic) + 1 > limits.max_order_rate:
            return _decision(
                OperationalSafetyVerdict.DENY,
                "order_rate",
                "order rate limit exceeded",
                limit=f"{limits.max_order_rate} per {limits.order_rate_window_seconds}s",
                observed=str(self._rate_count(safety_input.now_monotonic) + 1),
                identity=identity,
            )
        deviation_bps = _price_deviation_bps(
            safety_input.proposed_effective_price, safety_input.reference_price
        )
        if deviation_bps > Decimal(limits.max_price_deviation_bps):
            return _decision(
                OperationalSafetyVerdict.DENY,
                "price_deviation",
                "proposed price deviates from market reference",
                limit=f"{limits.max_price_deviation_bps}bps",
                observed=f"{deviation_bps}bps",
                identity=identity,
            )
        return _ALLOW_DECISION

    def _rate_count(self, now_monotonic: float) -> int:
        window = self._limits.order_rate_window_seconds
        floor = now_monotonic - window
        while self._submissions and self._submissions[0] <= floor:
            self._submissions.popleft()
        return len(self._submissions)

    def record_submission(self, now_monotonic: float) -> None:
        """Record one accepted outbound effect into the rate window."""
        if type(now_monotonic) is not float or not isfinite(now_monotonic):
            raise _fail(OutcomeCode.INVALID_TYPE, "now_monotonic must be a finite float")
        self._rate_count(now_monotonic)
        self._submissions.append(now_monotonic)

    def seed_submissions(self, ages_seconds: tuple[float, ...], *, now_monotonic: float) -> None:
        """Seed the rate window from recovered submission ages (recovery only).

        Each age is ``now - submitted_at`` in seconds. Seeding maps the durable
        submission history onto the fresh monotonic origin so a restart does not
        clear the order-rate state.
        """
        if type(now_monotonic) is not float or not isfinite(now_monotonic):
            raise _fail(OutcomeCode.INVALID_TYPE, "now_monotonic must be a finite float")
        if type(ages_seconds) is not tuple or any(
            type(value) is not float or not isfinite(value) or value < 0 for value in ages_seconds
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE, "submission ages must be non-negative finite floats"
            )
        if self._submissions:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "submission window is already seeded")
        window = self._limits.order_rate_window_seconds
        seeded = [now_monotonic - age for age in ages_seconds if age <= window]
        seeded.sort()
        self._submissions.extend(seeded)


def _price_deviation_bps(proposed: Decimal, reference: Decimal) -> Decimal:
    if reference == 0:
        # A zero reference price has no meaningful deviation; fail closed.
        return Decimal("Infinity") if proposed != 0 else Decimal("0")
    with localcontext() as context:
        context.prec = 100
        return (abs(proposed - reference) / reference) * Decimal(10000)


def canonical_operational_safety_limits_bytes(limits: OperationalSafetyLimits) -> bytes:
    """Return the canonical durable document for the configured limits."""
    if type(limits) is not OperationalSafetyLimits:
        raise _fail(OutcomeCode.INVALID_TYPE, "limits must be an exact OperationalSafetyLimits")
    document = {
        "schema": _LIMITS_SCHEMA,
        "max_market_age_seconds": limits.max_market_age_seconds,
        "max_daily_loss": str(limits.max_daily_loss),
        "max_total_exposure": str(limits.max_total_exposure),
        "max_open_orders": limits.max_open_orders,
        "max_order_rate": limits.max_order_rate,
        "order_rate_window_seconds": limits.order_rate_window_seconds,
        "max_price_deviation_bps": limits.max_price_deviation_bps,
    }
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def restore_operational_safety_limits(payload: bytes) -> OperationalSafetyLimits:
    """Rebuild configured limits; a malformed document fails closed."""
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "limits payload must be exact bytes")
    try:
        document = json.loads(payload)
    except (RecursionError, UnicodeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "limits payload is malformed JSON") from error
    if type(document) is not dict or document.get("schema") != _LIMITS_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "limits schema conflicts")
    try:
        return OperationalSafetyLimits(
            max_market_age_seconds=document["max_market_age_seconds"],
            max_daily_loss=Decimal(document["max_daily_loss"]),
            max_total_exposure=Decimal(document["max_total_exposure"]),
            max_open_orders=document["max_open_orders"],
            max_order_rate=document["max_order_rate"],
            order_rate_window_seconds=document["order_rate_window_seconds"],
            max_price_deviation_bps=document["max_price_deviation_bps"],
        )
    except (KeyError, TypeError, ValueError, OperationalSafetyError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "limits payload conflicts") from error


_ALLOW_DECISION = OperationalSafetyDecision(
    OperationalSafetyVerdict.ALLOW,
    "none",
    "all guards permit the outbound effect",
)
