"""Runtime-owned active market dispatch verification for ADR 0017."""

from __future__ import annotations

from typing import final

from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.runtime import (
    ActiveMarketDispatchProof,
    _create_active_market_dispatch_proof,
)
from ea.core.strategy import StrategyContractError, causal_market_digest
from ea.runtime.historical import Phase1HistoricalMarketRuntime


def _fail(code: OutcomeCode, message: str) -> StrategyContractError:
    return StrategyContractError(code, message)


@final
class _ActiveMarketDispatchVerifier:
    __slots__ = ("_runtime",)

    _runtime: Phase1HistoricalMarketRuntime

    def __init__(self) -> None:
        raise TypeError(
            "active market dispatch verifiers are created only by their runtime factory"
        )

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> ActiveMarketDispatchProof:
        if type(market_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.INVALID_TYPE, "market root must be exact")
        if type(dispatch_sequence) is not int:
            raise _fail(OutcomeCode.INVALID_TYPE, "dispatch sequence must be exact int")
        if dispatch_sequence < 1 or dispatch_sequence > (1 << 64) - 1:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence must be positive uint64")
        active = self._runtime.active_lease
        if (
            active is None
            or active.root is not market_root
            or active.dispatch_sequence != dispatch_sequence
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "market root is not the exact active runtime dispatch",
            )
        try:
            market_bytes = canonical_market_data_record_bytes(market_root)
            market_sha256 = causal_market_digest(market_root)
        except StrategyContractError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "active market root cannot be canonically encoded",
            ) from error
        if (
            self._runtime.active_lease is not active
            or active.root is not market_root
            or active.dispatch_sequence != dispatch_sequence
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "active market dispatch changed")
        return _create_active_market_dispatch_proof(
            run_id=self._runtime.run_id,
            market_root=market_root,
            canonical_market_bytes=market_bytes,
            causal_market_sha256=market_sha256,
            dispatch_sequence=dispatch_sequence,
            issuer=self,
        )


def create_active_market_dispatch_verifier(
    runtime: Phase1HistoricalMarketRuntime,
) -> _ActiveMarketDispatchVerifier:
    """Bind a sealed read-only verifier to one exact historical runtime."""
    if type(runtime) is not Phase1HistoricalMarketRuntime:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "runtime must be an exact Phase1HistoricalMarketRuntime",
        )
    value = object.__new__(_ActiveMarketDispatchVerifier)
    value._runtime = runtime
    return value
