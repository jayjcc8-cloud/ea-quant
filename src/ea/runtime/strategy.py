"""Runtime-owned active market dispatch verification for ADR 0017."""

from __future__ import annotations

from typing import final

from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.runtime import (
    ActiveMarketDispatchProof,
    RuntimeOrderingError,
    _create_active_market_dispatch_proof,
)
from ea.core.strategy import (
    StrategyContractError,
    _causal_market_digest_from_canonical_bytes,
)
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
        try:
            market_bytes = self._runtime._require_active_market_dispatch_bytes(
                market_root,
                dispatch_sequence=dispatch_sequence,
            )
            market_sha256 = _causal_market_digest_from_canonical_bytes(market_bytes)
            confirmed_bytes = self._runtime._require_active_market_dispatch_bytes(
                market_root,
                dispatch_sequence=dispatch_sequence,
            )
        except RuntimeOrderingError as error:
            code = (
                error.code
                if error.code
                in {
                    OutcomeCode.INVALID_TYPE,
                    OutcomeCode.OUT_OF_RANGE,
                    OutcomeCode.CONFLICTING_ID,
                }
                else OutcomeCode.INVALID_TYPE
            )
            raise _fail(code, "active market dispatch verification failed") from error
        except StrategyContractError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "active market root cannot be canonically encoded",
            ) from error
        if confirmed_bytes != market_bytes:
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
