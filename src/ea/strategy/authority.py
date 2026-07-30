"""Single deterministic strategy-signal authority from Accepted ADR 0017."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn, final

from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.market_data import MarketDataEnvelope, MarketDataKind
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.runtime import RuntimeOrderingError, _require_active_market_dispatch_proof
from ea.core.strategy import (
    ActiveMarketDispatchVerifierPort,
    AuthorityConflictKind,
    AuthorityKind,
    SignalDirection,
    StrategyContractError,
    StrategySignal,
    StrategySignalAuthorityState,
    _create_authority_conflict_evidence,
    _create_strategy_signal,
    _create_strategy_signal_authority_state,
    canonical_strategy_signal_authority_state_bytes,
    canonical_strategy_signal_bytes,
    causal_market_digest,
    strategy_signal_authority_state_digest,
    strategy_signal_digest,
    strategy_signal_submission_digest,
)

_MAX_UINT64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class _ReplayRecord:
    market_root_bytes: bytes
    direction: SignalDirection
    submitted_sha256: Sha256Digest
    signal: StrategySignal
    signal_bytes: bytes
    signal_sha256: Sha256Digest


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    public: StrategySignalAuthorityState
    replay_by_dispatch: MappingProxyType[int, _ReplayRecord]


def _fail(code: OutcomeCode, message: str) -> StrategyContractError:
    return StrategyContractError(code, message)


def _advance(value: int) -> int | None:
    return None if value == _MAX_UINT64 else value + 1


@final
class StrategySignalAuthority:
    """The sole mutable owner of canonical StrategySignal issuance."""

    __slots__ = ("_run_id", "_state", "_verifier")

    _run_id: RunId
    _verifier: ActiveMarketDispatchVerifierPort
    _state: _AuthorityState

    def __init__(self) -> None:
        raise TypeError(
            "StrategySignalAuthority values are created only by create_strategy_signal_authority"
        )

    @property
    def state(self) -> StrategySignalAuthorityState:
        return self._state.public

    def lookup_by_dispatch_sequence(self, sequence: int) -> StrategySignal | None:
        if type(sequence) is not int:
            raise _fail(OutcomeCode.INVALID_TYPE, "dispatch sequence must be exact int")
        if sequence < 1 or sequence > _MAX_UINT64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence must be positive uint64")
        record = self._state.replay_by_dispatch.get(sequence)
        return None if record is None else record.signal

    def issue(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
        direction: SignalDirection,
    ) -> StrategySignal:
        """Issue one new canonical signal or return its exact retained replay."""
        if type(market_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.INVALID_TYPE, "market root must be exact")
        if type(direction) is not SignalDirection:
            raise _fail(OutcomeCode.INVALID_TYPE, "direction must be exact")
        if type(dispatch_sequence) is not int:
            raise _fail(OutcomeCode.INVALID_TYPE, "dispatch sequence must be exact int")
        if dispatch_sequence < 1 or dispatch_sequence > _MAX_UINT64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence must be positive uint64")
        try:
            market_bytes = canonical_market_data_record_bytes(market_root)
            submitted_sha256 = strategy_signal_submission_digest(
                market_root,
                dispatch_sequence=dispatch_sequence,
                direction=direction,
            )
        except StrategyContractError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "market root cannot be canonically encoded",
            ) from error

        state = self._state
        existing = state.replay_by_dispatch.get(dispatch_sequence)
        if existing is not None:
            if existing.market_root_bytes == market_bytes and existing.direction is direction:
                return existing.signal
            self._publish_conflict(
                kind=AuthorityConflictKind.IDENTITY_REUSE,
                dispatch_sequence=dispatch_sequence,
                occupied_owner_id=existing.signal.signal_id,
                existing_sha256=existing.signal_sha256,
                submitted_sha256=submitted_sha256,
            )

        last_sequence = state.public.last_new_dispatch_sequence
        if last_sequence is not None and dispatch_sequence <= last_sequence:
            self._publish_conflict(
                kind=AuthorityConflictKind.NON_MONOTONE_DISPATCH,
                dispatch_sequence=dispatch_sequence,
                occupied_owner_id=None,
                existing_sha256=None,
                submitted_sha256=submitted_sha256,
            )
        if state.public.halted:
            raise _fail(OutcomeCode.CONFLICTING_ID, "strategy signal authority is halted")
        if market_root.kind is not MarketDataKind.BAR:
            raise _fail(OutcomeCode.INVALID_TYPE, "signal cause must be a market Bar")
        signal_next = state.public.signal_next
        if signal_next is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "strategy signal sequence is exhausted")

        market_sha256 = causal_market_digest(market_root)
        try:
            verify = self._verifier.verify_active_market_dispatch
            if not callable(verify):
                raise AttributeError("verifier operation is not callable")
            proof = verify(market_root, dispatch_sequence=dispatch_sequence)
        except StrategyContractError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "active market verifier operation contract failed",
            ) from error
        try:
            _require_active_market_dispatch_proof(
                proof,
                run_id=self._run_id,
                market_root=market_root,
                canonical_market_bytes=market_bytes,
                causal_market_sha256=market_sha256,
                dispatch_sequence=dispatch_sequence,
                issuer=self._verifier,
            )
        except RuntimeOrderingError as error:
            code = (
                error.code
                if error.code in {OutcomeCode.INVALID_TYPE, OutcomeCode.CONFLICTING_ID}
                else OutcomeCode.INVALID_TYPE
            )
            raise _fail(code, "active market dispatch proof is invalid") from error

        try:
            signal = _create_strategy_signal(
                run_id=self._run_id,
                signal_id=EconomicId(
                    self._run_id,
                    EconomicOwnerKind.STRATEGY_SIGNAL,
                    signal_next,
                ),
                market_root=market_root,
                direction=direction,
                causal_market_sha256=market_sha256,
                dispatch_sequence=dispatch_sequence,
            )
            signal_bytes = canonical_strategy_signal_bytes(signal)
            signal_sha256 = strategy_signal_digest(signal)
        except StrategyContractError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "strategy signal publication preflight failed",
            ) from error
        replay = dict(state.replay_by_dispatch)
        replay[dispatch_sequence] = _ReplayRecord(
            market_root_bytes=market_bytes,
            direction=direction,
            submitted_sha256=submitted_sha256,
            signal=signal,
            signal_bytes=signal_bytes,
            signal_sha256=signal_sha256,
        )
        try:
            public = _create_strategy_signal_authority_state(
                run_id=self._run_id,
                halted=False,
                signal_next=_advance(signal_next),
                last_new_dispatch_sequence=dispatch_sequence,
                issuance_count=state.public.issuance_count + 1,
                conflict=None,
            )
            canonical_strategy_signal_authority_state_bytes(public)
            strategy_signal_authority_state_digest(public)
        except StrategyContractError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "strategy signal state publication preflight failed",
            ) from error
        self._state = _AuthorityState(
            public=public,
            replay_by_dispatch=MappingProxyType(replay),
        )
        return signal

    def _publish_conflict(
        self,
        *,
        kind: AuthorityConflictKind,
        dispatch_sequence: int,
        occupied_owner_id: EconomicId | None,
        existing_sha256: Sha256Digest | None,
        submitted_sha256: Sha256Digest,
    ) -> NoReturn:
        state = self._state
        if state.public.conflict is None:
            try:
                conflict = _create_authority_conflict_evidence(
                    authority_kind=AuthorityKind.STRATEGY_SIGNAL,
                    conflict_kind=kind,
                    run_id=self._run_id,
                    occupied_owner_id=occupied_owner_id,
                    dispatch_sequence=dispatch_sequence,
                    last_new_dispatch_sequence=state.public.last_new_dispatch_sequence,
                    existing_sha256=existing_sha256,
                    submitted_sha256=submitted_sha256,
                )
                public = _create_strategy_signal_authority_state(
                    run_id=self._run_id,
                    halted=True,
                    signal_next=state.public.signal_next,
                    last_new_dispatch_sequence=state.public.last_new_dispatch_sequence,
                    issuance_count=state.public.issuance_count,
                    conflict=conflict,
                )
                canonical_strategy_signal_authority_state_bytes(public)
                strategy_signal_authority_state_digest(public)
            except StrategyContractError:
                raise
            except Exception as error:
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "strategy conflict publication preflight failed",
                ) from error
            self._state = _AuthorityState(
                public=public,
                replay_by_dispatch=state.replay_by_dispatch,
            )
        raise _fail(OutcomeCode.CONFLICTING_ID, "strategy signal replay conflicts")


def create_strategy_signal_authority(
    *,
    run_id: RunId,
    verifier: ActiveMarketDispatchVerifierPort,
) -> StrategySignalAuthority:
    """Create one single-run signal authority."""
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
    try:
        operation = verifier.verify_active_market_dispatch
    except (AttributeError, TypeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "active market verifier is incomplete") from error
    if not callable(operation):
        raise _fail(OutcomeCode.INVALID_TYPE, "active market verifier operation must be callable")
    public = _create_strategy_signal_authority_state(
        run_id=run_id,
        halted=False,
        signal_next=1,
        last_new_dispatch_sequence=None,
        issuance_count=0,
        conflict=None,
    )
    value = object.__new__(StrategySignalAuthority)
    value._run_id = run_id
    value._verifier = verifier
    value._state = _AuthorityState(
        public=public,
        replay_by_dispatch=MappingProxyType({}),
    )
    return value
