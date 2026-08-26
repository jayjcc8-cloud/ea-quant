"""Sealed stateful reconciliation authority from Accepted ADR 0022.

This module is the Phase 1 authority that owns reconciliation evidence
admission, comparison outcomes, adjustment-command proposal, and one-use
adjustment-authorization issuance. It is capability confined: the authority
class is the only mutation surface, no ``__all__`` is declared, and every
index is frozen behind a read-only ``MappingProxyType`` after each
copy-on-write state swap. The module never imports ``ea.runtime``,
``ea.composition``, or ``ea.experiments``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, final

from ea.core.economics import CanonicalDecimal
from ea.core.execution import (
    InstrumentExecutionSpecSet,
    SettlementCurrency,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind, SourceNamespace
from ea.core.identity import Instrument
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import PortfolioSnapshot, portfolio_snapshot_digest
from ea.core.reconciliation import (
    _VALUE_SEAL,
    CashReconciliationBalance,
    PositionReconciliationBalance,
    ReconciliationAdjustmentAuthorization,
    ReconciliationAdjustmentCommand,
    ReconciliationAuthorizationDecision,
    ReconciliationAuthorizationPolicyId,
    ReconciliationDiscrepancy,
    ReconciliationObservation,
    ReconciliationOutcome,
    ReconciliationRequestedAction,
    ReconciliationWatermarkComparison,
    _create_reconciliation_adjustment_authorization,
    _create_reconciliation_adjustment_command,
    create_cash_reconciliation_discrepancy,
    create_position_reconciliation_discrepancy,
    create_reconciliation_outcome,
    reconciliation_adjustment_authorization_digest,
    reconciliation_adjustment_command_digest,
    reconciliation_observation_digest,
)
from ea.core.run import RunBinding, RunId, Sha256Digest
from ea.core.runtime import ReconciliationObservationKind

_MAX_UINT64 = (1 << 64) - 1
_PORTFOLIO_LEDGER_WATERMARK_NAMESPACE = SourceNamespace("ledger.portfolio")

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class ReconciliationAuthorityError(ValueError):
    """Closed failure at the stateful reconciliation authority boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError(
                "reconciliation authority errors require an exact supported OutcomeCode"
            )
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> ReconciliationAuthorityError:
    return ReconciliationAuthorityError(code, message)


class SnapshotView(Protocol):
    """Read-only supplier of the current acknowledged local portfolio snapshot.

    Inject a bound method or a simple callable. The authority invokes it for
    every comparison and treats the returned snapshot as the acknowledged
    local frontier at that instant. It must never mutate the ledger: the
    authority reads it for comparison only and never writes back.
    """

    def __call__(self) -> PortfolioSnapshot: ...


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    snapshot_view: SnapshotView
    observation_index: Mapping[tuple[SourceNamespace, int], Sha256Digest]
    outcome_index: Mapping[Sha256Digest, ReconciliationOutcome]
    authorization_index: Mapping[EconomicId, Sha256Digest]
    adjustment_index: Mapping[EconomicId, Sha256Digest]
    command_index: Mapping[Sha256Digest, Sha256Digest]
    next_authorization_sequence: int
    next_adjustment_sequence: int


@final
class Phase1ReconciliationAuthority:
    """The sole Phase 1 owner of stateful reconciliation evidence.

    The authority is run/specification-bound. Observation identity is
    ``(source_namespace, source_sequence)``; authorization and adjustment
    identities are allocated from monotone one-use sequences that are never
    reused. Every mutation commits one complete immutable candidate state via
    a single pointer swap, so an exception never leaves a partial index.
    """

    _state: _AuthorityState

    __slots__ = ("_state",)

    def __init__(self) -> None:
        raise TypeError("reconciliation authorities are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._state.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._state.spec_set

    @property
    def next_authorization_sequence(self) -> int:
        return self._state.next_authorization_sequence

    @property
    def next_adjustment_sequence(self) -> int:
        return self._state.next_adjustment_sequence

    @property
    def observation_index(self) -> Mapping[tuple[SourceNamespace, int], Sha256Digest]:
        """Read-only observation-identity index (no mutation capability)."""
        return self._state.observation_index

    @property
    def outcome_index(self) -> Mapping[Sha256Digest, ReconciliationOutcome]:
        """Read-only outcome index keyed by observation digest."""
        return self._state.outcome_index

    @property
    def authorization_index(self) -> Mapping[EconomicId, Sha256Digest]:
        """Read-only one-use authorization index, denials included."""
        return self._state.authorization_index

    @property
    def adjustment_index(self) -> Mapping[EconomicId, Sha256Digest]:
        """Read-only adjustment-identity index keyed by command digest."""
        return self._state.adjustment_index

    @property
    def command_index(self) -> Mapping[Sha256Digest, Sha256Digest]:
        """Read-only command-digest index mapping to observation digest."""
        return self._state.command_index

    def admit_observation(
        self,
        observation: ReconciliationObservation,
        *,
        dispatch_sequence: int,
    ) -> ReconciliationOutcome:
        """Admit one rank-20 observation and return its sole comparison outcome.

        Observation identity is ``(source_namespace, source_sequence)``. An
        identical identity with identical canonical bytes is a replay and
        returns the original outcome object without recomputation or a
        snapshot read; the same identity with different bytes is a conflict.
        The comparison never mutates the acknowledged ledger snapshot: the
        outcome is computed from the frozen local snapshot and committed only
        after every validation passes.
        """
        state = self._state
        _require_observation_evidence(observation, state.run_id, state.spec_set)
        checked_dispatch_sequence = _require_uint64(dispatch_sequence, "dispatch_sequence")
        if checked_dispatch_sequence == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch_sequence must be positive")
        observation_sha256 = reconciliation_observation_digest(observation)
        identity = (observation.source_namespace, observation.source_sequence)
        prior = state.observation_index.get(identity)
        if prior is not None:
            if prior != observation_sha256:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "observation identity conflicts with earlier canonical bytes",
                )
            outcome = state.outcome_index.get(observation_sha256)
            if outcome is None:
                raise _fail(OutcomeCode.CONFLICTING_ID, "observation outcome index is inconsistent")
            return outcome
        snapshot = _require_snapshot_view(state.snapshot_view, state.run_id, state.spec_set)
        outcome = _compare_observation(
            observation=observation,
            snapshot=snapshot,
            run_id=state.run_id,
            spec_set=state.spec_set,
            dispatch_sequence=checked_dispatch_sequence,
        )
        next_state = _AuthorityState(
            run_id=state.run_id,
            spec_set=state.spec_set,
            snapshot_view=state.snapshot_view,
            observation_index=_freeze_observation_index(
                {**dict(state.observation_index), identity: observation_sha256}
            ),
            outcome_index=_freeze_outcome_index(
                {**dict(state.outcome_index), observation_sha256: outcome}
            ),
            authorization_index=state.authorization_index,
            adjustment_index=state.adjustment_index,
            command_index=state.command_index,
            next_authorization_sequence=state.next_authorization_sequence,
            next_adjustment_sequence=state.next_adjustment_sequence,
        )
        self._state = next_state
        return outcome

    def propose_adjustment_command(
        self,
        *,
        binding: RunBinding,
        outcome: ReconciliationOutcome,
        outcome_acknowledgement: object,
        observation: ReconciliationObservation,
        local_snapshot: PortfolioSnapshot,
    ) -> ReconciliationAdjustmentCommand:
        """Allocate the next one-use adjustment identity and derive the command.

        Only the exact equal-watermark MISMATCH row with
        ``PROPOSE_SINGLE_TARGET_ADJUSTMENT`` is eligible. Ancestry-resolution
        rows require independently proven ancestry, which Phase 1 never proves
        (no Fill/Order resolution here), so they fail closed. The caller
        supplies the active audit binding so the exact outcome acknowledgement
        is verified against the current lease before any command is derived.
        """
        state = self._state
        if type(binding) is not RunBinding:
            raise _fail(OutcomeCode.INVALID_TYPE, "binding must be exact")
        _require_outcome_evidence(outcome, observation, state.run_id, state.spec_set)
        if type(local_snapshot) is not PortfolioSnapshot:
            raise _fail(OutcomeCode.INVALID_TYPE, "local_snapshot must be exact")
        if (
            local_snapshot.run_id != state.run_id
            or local_snapshot.instrument_spec_set_id != state.spec_set.identifier
            or local_snapshot.instrument_spec_set_sha256
            != instrument_spec_set_digest(state.spec_set)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "local snapshot binding conflicts")
        if outcome.requested_action is ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "Phase 1 reconciliation authority does not derive ancestry commands",
            )
        if (
            outcome.watermark_comparison is not ReconciliationWatermarkComparison.EQUAL
            or outcome.outcome_code is not OutcomeCode.RECONCILIATION_MISMATCH
            or outcome.requested_action
            is not ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT
            or len(outcome.discrepancies) != 1
            or not outcome.halt_requested
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "outcome does not authorize a single-target adjustment command",
            )
        if state.outcome_index.get(outcome.observation_sha256) is not outcome:
            raise _fail(OutcomeCode.CONFLICTING_ID, "outcome was not admitted by this authority")
        adjustment_before = state.next_adjustment_sequence
        adjustment_after = _advance(adjustment_before)
        if adjustment_after is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "reconciliation adjustment sequence is exhausted")
        adjustment_id = EconomicId(
            state.run_id,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            adjustment_before,
        )
        command = _create_reconciliation_adjustment_command(
            binding=binding,
            spec_set=state.spec_set,
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            local_snapshot=local_snapshot,
            adjustment_id=adjustment_id,
        )
        command_sha256 = reconciliation_adjustment_command_digest(command)
        next_state = _AuthorityState(
            run_id=state.run_id,
            spec_set=state.spec_set,
            snapshot_view=state.snapshot_view,
            observation_index=state.observation_index,
            outcome_index=state.outcome_index,
            authorization_index=state.authorization_index,
            adjustment_index=_freeze_adjustment_index(
                {**dict(state.adjustment_index), adjustment_id: command_sha256}
            ),
            command_index=_freeze_command_index(
                {**dict(state.command_index), command_sha256: outcome.observation_sha256}
            ),
            next_authorization_sequence=state.next_authorization_sequence,
            next_adjustment_sequence=adjustment_after,
        )
        self._state = next_state
        return command

    def issue_authorization(
        self,
        *,
        binding: RunBinding,
        outcome: ReconciliationOutcome,
        outcome_acknowledgement: object,
        command: ReconciliationAdjustmentCommand,
        policy: ReconciliationAuthorizationPolicyId,
        policy_version: int,
        policy_sha256: Sha256Digest,
        decision: ReconciliationAuthorizationDecision,
        available_at: datetime,
    ) -> ReconciliationAdjustmentAuthorization:
        """Issue one exact one-use authorization for an authority-proposed command.

        Authorization identities are allocated from the monotone one-use
        sequence and are never reused. Both ALLOWED and DENIED decisions are
        indexed; a denied authorization is permanently burned and can never
        reach the ledger. The adjustment identity is the exact adjustment ID
        already allocated to the proposed command.
        """
        state = self._state
        if (
            type(binding) is not RunBinding
            or type(policy) is not ReconciliationAuthorizationPolicyId
            or type(policy_sha256) is not Sha256Digest
            or type(decision) is not ReconciliationAuthorizationDecision
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "authorization carriers must be exact")
        checked_policy_version = _require_uint64(policy_version, "policy_version")
        if checked_policy_version == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "policy_version must be positive")
        if (
            type(outcome) is not ReconciliationOutcome
            or outcome._seal is not _VALUE_SEAL
            or type(command) is not ReconciliationAdjustmentCommand
            or command._seal is not _VALUE_SEAL
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "authorization evidence must be exact")
        if state.outcome_index.get(outcome.observation_sha256) is not outcome:
            raise _fail(OutcomeCode.CONFLICTING_ID, "outcome was not admitted by this authority")
        command_sha256 = reconciliation_adjustment_command_digest(command)
        if state.adjustment_index.get(command.adjustment_id) != command_sha256:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "adjustment command was not proposed by this authority",
            )
        if state.command_index.get(command_sha256) != outcome.observation_sha256:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "adjustment command does not bind this authority's outcome",
            )
        if (
            outcome.watermark_comparison is not ReconciliationWatermarkComparison.EQUAL
            or outcome.outcome_code is not OutcomeCode.RECONCILIATION_MISMATCH
            or outcome.requested_action
            is not ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "outcome row does not authorize adjustment issuance",
            )
        authorization_before = state.next_authorization_sequence
        authorization_after = _advance(authorization_before)
        if authorization_after is None:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "reconciliation authorization sequence is exhausted",
            )
        authorization_id = EconomicId(
            state.run_id,
            EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
            authorization_before,
        )
        if authorization_id in state.authorization_index:
            raise _fail(OutcomeCode.CONFLICTING_ID, "authorization identity reuse is a conflict")
        authorization = _create_reconciliation_adjustment_authorization(
            binding=binding,
            spec_set=state.spec_set,
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            command=command,
            authorization_id=authorization_id,
            policy_id=policy,
            policy_version=checked_policy_version,
            policy_sha256=policy_sha256,
            decision=decision,
            available_at=available_at,
        )
        next_state = _AuthorityState(
            run_id=state.run_id,
            spec_set=state.spec_set,
            snapshot_view=state.snapshot_view,
            observation_index=state.observation_index,
            outcome_index=state.outcome_index,
            authorization_index=_freeze_authorization_index(
                {
                    **dict(state.authorization_index),
                    authorization_id: reconciliation_adjustment_authorization_digest(authorization),
                }
            ),
            adjustment_index=state.adjustment_index,
            command_index=state.command_index,
            next_authorization_sequence=authorization_after,
            next_adjustment_sequence=state.next_adjustment_sequence,
        )
        self._state = next_state
        return authorization

    def has_issued_authorization(self, authorization_id: EconomicId) -> bool:
        """Return whether this authority has issued (and burned) that identity."""
        if type(authorization_id) is not EconomicId:
            raise _fail(OutcomeCode.INVALID_TYPE, "authorization_id must be an exact EconomicId")
        return authorization_id in self._state.authorization_index


def create_phase1_reconciliation_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    snapshot_view: SnapshotView,
) -> Phase1ReconciliationAuthority:
    """Create one run/specification-bound authority over the acknowledged snapshot.

    The injected ``snapshot_view`` is a read-only callable returning the
    current acknowledged local ``PortfolioSnapshot``; inject a bound method or
    a simple callable. It is probed once at construction so a wrong
    run/specification binding fails immediately, and re-read and re-validated
    on every admission so a frontier drift cannot be compared against.
    """
    if type(run_id) is not RunId or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "authority binding must be exact")
    if not callable(snapshot_view):
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot_view must be callable")
    _require_snapshot_view(snapshot_view, run_id, spec_set)
    value = object.__new__(Phase1ReconciliationAuthority)
    object.__setattr__(
        value,
        "_state",
        _AuthorityState(
            run_id=run_id,
            spec_set=spec_set,
            snapshot_view=snapshot_view,
            observation_index=_freeze_observation_index({}),
            outcome_index=_freeze_outcome_index({}),
            authorization_index=_freeze_authorization_index({}),
            adjustment_index=_freeze_adjustment_index({}),
            command_index=_freeze_command_index({}),
            next_authorization_sequence=1,
            next_adjustment_sequence=1,
        ),
    )
    return value


@final
class _ObservationOnlyReconciliationAuthority:
    """Private sealed comparison port for the read-only observation vertical."""

    _state: _AuthorityState

    __slots__ = ("_state",)

    def __init__(self) -> None:
        raise TypeError("observation-only authorities are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._state.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._state.spec_set

    @property
    def observation_index(self) -> Mapping[tuple[SourceNamespace, int], Sha256Digest]:
        return self._state.observation_index

    @property
    def outcome_index(self) -> Mapping[Sha256Digest, ReconciliationOutcome]:
        return self._state.outcome_index

    def admit_observation(
        self,
        observation: ReconciliationObservation,
        *,
        dispatch_sequence: int,
    ) -> ReconciliationOutcome:
        """Retain only a deterministic comparison outcome, never an effect capability."""
        state = self._state
        _require_observation_evidence(observation, state.run_id, state.spec_set)
        if observation.kind is ReconciliationObservationKind.TRADE_DETAIL:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "trade detail is outside observation-only scope")
        checked_dispatch_sequence = _require_uint64(dispatch_sequence, "dispatch_sequence")
        if checked_dispatch_sequence == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch_sequence must be positive")
        observation_sha256 = reconciliation_observation_digest(observation)
        identity = (observation.source_namespace, observation.source_sequence)
        prior = state.observation_index.get(identity)
        if prior is not None:
            if prior != observation_sha256:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "observation identity conflicts with earlier canonical bytes",
                )
            outcome = state.outcome_index.get(observation_sha256)
            if outcome is None:
                raise _fail(OutcomeCode.CONFLICTING_ID, "observation outcome index is inconsistent")
            return outcome
        snapshot = _require_snapshot_view(state.snapshot_view, state.run_id, state.spec_set)
        outcome = _compare_observation(
            observation=observation,
            snapshot=snapshot,
            run_id=state.run_id,
            spec_set=state.spec_set,
            dispatch_sequence=checked_dispatch_sequence,
        )
        self._state = _AuthorityState(
            run_id=state.run_id,
            spec_set=state.spec_set,
            snapshot_view=state.snapshot_view,
            observation_index=_freeze_observation_index(
                {**dict(state.observation_index), identity: observation_sha256}
            ),
            outcome_index=_freeze_outcome_index(
                {**dict(state.outcome_index), observation_sha256: outcome}
            ),
            authorization_index=state.authorization_index,
            adjustment_index=state.adjustment_index,
            command_index=state.command_index,
            next_authorization_sequence=state.next_authorization_sequence,
            next_adjustment_sequence=state.next_adjustment_sequence,
        )
        return outcome

    def resolve_outcome(self, observation: ReconciliationObservation) -> ReconciliationOutcome:
        """Return the one retained canonical outcome for exactly one observation."""
        state = self._state
        _require_observation_evidence(observation, state.run_id, state.spec_set)
        digest = reconciliation_observation_digest(observation)
        outcome = state.outcome_index.get(digest)
        if outcome is None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "observation outcome is not retained")
        return outcome


def _create_observation_only_reconciliation_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    snapshot_view: SnapshotView,
) -> _ObservationOnlyReconciliationAuthority:
    """Create the private narrow port used only by lifecycle composition."""
    if type(run_id) is not RunId or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "authority binding must be exact")
    if not callable(snapshot_view):
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot_view must be callable")
    _require_snapshot_view(snapshot_view, run_id, spec_set)
    value = object.__new__(_ObservationOnlyReconciliationAuthority)
    object.__setattr__(
        value,
        "_state",
        _AuthorityState(
            run_id=run_id,
            spec_set=spec_set,
            snapshot_view=snapshot_view,
            observation_index=_freeze_observation_index({}),
            outcome_index=_freeze_outcome_index({}),
            authorization_index=_freeze_authorization_index({}),
            adjustment_index=_freeze_adjustment_index({}),
            command_index=_freeze_command_index({}),
            next_authorization_sequence=1,
            next_adjustment_sequence=1,
        ),
    )
    return value


def _require_observation_evidence(
    observation: ReconciliationObservation,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
) -> None:
    if type(observation) is not ReconciliationObservation or observation._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation observation must be factory-issued")
    if (
        observation.run_id != run_id
        or observation.instrument_spec_set_id != spec_set.identifier
        or observation.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation observation binding conflicts")


def _require_outcome_evidence(
    outcome: ReconciliationOutcome,
    observation: ReconciliationObservation,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
) -> None:
    if (
        type(outcome) is not ReconciliationOutcome
        or outcome._seal is not _VALUE_SEAL
        or type(observation) is not ReconciliationObservation
        or observation._seal is not _VALUE_SEAL
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "adjustment evidence must be exact")
    if (
        outcome.run_id != run_id
        or observation.run_id != run_id
        or observation.instrument_spec_set_id != spec_set.identifier
        or observation.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or outcome.observation_sha256 != reconciliation_observation_digest(observation)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment evidence bindings conflict")


def _require_snapshot_view(
    view: SnapshotView,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
) -> PortfolioSnapshot:
    snapshot = view()
    if type(snapshot) is not PortfolioSnapshot:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "snapshot_view must return an exact PortfolioSnapshot",
        )
    if (
        snapshot.run_id != run_id
        or snapshot.instrument_spec_set_id != spec_set.identifier
        or snapshot.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "acknowledged snapshot binding conflicts")
    return snapshot


def _compare_observation(
    *,
    observation: ReconciliationObservation,
    snapshot: PortfolioSnapshot,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    dispatch_sequence: int,
) -> ReconciliationOutcome:
    """Derive the closed comparison outcome for one admitted observation."""
    observation_sha256 = reconciliation_observation_digest(observation)
    kind = observation.kind
    if kind is ReconciliationObservationKind.ORDER_DETAIL:
        # Phase 1 never proves ancestry (no Fill/Order resolution here), so
        # every order-detail root is unresolved-correlation evidence. The
        # closed outcome matrix requires the EQUAL watermark row even though
        # this is correlation evidence, not a balance comparison.
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.EQUAL,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_UNRESOLVED_CORRELATION,
            requested_action=ReconciliationRequestedAction.RETAIN_AND_HALT,
            halt_requested=True,
        )
    if kind is ReconciliationObservationKind.TRADE_DETAIL:
        # Trade detail is normalized through the execution-fact path, not by
        # this balance authority; with no comparable balances it is
        # incomparable evidence (ADR 0022 comparison table).
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.INCOMPARABLE,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_INVALID,
            requested_action=ReconciliationRequestedAction.RETAIN_AND_HALT,
            halt_requested=True,
        )
    if observation.watermark_namespace != _PORTFOLIO_LEDGER_WATERMARK_NAMESPACE:
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.INCOMPARABLE,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_INVALID,
            requested_action=ReconciliationRequestedAction.RETAIN_AND_HALT,
            halt_requested=True,
        )
    ledger_sequence = snapshot.ledger_sequence
    if observation.watermark_sequence < ledger_sequence:
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.REMOTE_LOWER,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_LOCAL_AHEAD_STALE,
            requested_action=ReconciliationRequestedAction.RETAIN_AND_HALT,
            halt_requested=True,
        )
    if observation.watermark_sequence > ledger_sequence:
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.REMOTE_HIGHER,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_REMOTE_AHEAD,
            requested_action=ReconciliationRequestedAction.REQUEST_MISSING_TRADE_FACTS,
            halt_requested=True,
        )
    if not _observation_scope_is_complete(observation, snapshot):
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.INCOMPARABLE,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_INVALID,
            requested_action=ReconciliationRequestedAction.RETAIN_AND_HALT,
            halt_requested=True,
        )
    discrepancies = _compare_balances(observation, snapshot, spec_set)
    if not discrepancies:
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.EQUAL,
            discrepancies=(),
            outcome_code=OutcomeCode.RECONCILIATION_MATCH,
            requested_action=ReconciliationRequestedAction.NONE,
            halt_requested=False,
        )
    if len(discrepancies) == 1:
        return _comparison_outcome(
            observation_sha256=observation_sha256,
            snapshot=snapshot,
            run_id=run_id,
            dispatch_sequence=dispatch_sequence,
            comparison=ReconciliationWatermarkComparison.EQUAL,
            discrepancies=discrepancies,
            outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
            requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
            halt_requested=True,
        )
    return _comparison_outcome(
        observation_sha256=observation_sha256,
        snapshot=snapshot,
        run_id=run_id,
        dispatch_sequence=dispatch_sequence,
        comparison=ReconciliationWatermarkComparison.EQUAL,
        discrepancies=discrepancies,
        outcome_code=OutcomeCode.RECONCILIATION_QUARANTINED,
        requested_action=ReconciliationRequestedAction.MANUAL_EVIDENCE_DECOMPOSITION,
        halt_requested=True,
    )


def _comparison_outcome(
    *,
    observation_sha256: Sha256Digest,
    snapshot: PortfolioSnapshot,
    run_id: RunId,
    dispatch_sequence: int,
    comparison: ReconciliationWatermarkComparison,
    discrepancies: tuple[ReconciliationDiscrepancy, ...],
    outcome_code: OutcomeCode,
    requested_action: ReconciliationRequestedAction,
    halt_requested: bool,
) -> ReconciliationOutcome:
    return create_reconciliation_outcome(
        run_id=run_id,
        dispatch_sequence=dispatch_sequence,
        observation_sha256=observation_sha256,
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        watermark_comparison=comparison,
        discrepancies=discrepancies,
        outcome_code=outcome_code,
        requested_action=requested_action,
        halt_requested=halt_requested,
    )


def _compare_balances(
    observation: ReconciliationObservation,
    snapshot: PortfolioSnapshot,
    spec_set: InstrumentExecutionSpecSet,
) -> tuple[ReconciliationDiscrepancy, ...]:
    """Compare one kind-complete balance snapshot against the local snapshot."""
    if observation.kind is ReconciliationObservationKind.POSITION_SNAPSHOT:
        return _compare_position_balances(observation, snapshot, spec_set)
    return _compare_cash_balances(observation, snapshot, spec_set)


def _compare_position_balances(
    observation: ReconciliationObservation,
    snapshot: PortfolioSnapshot,
    spec_set: InstrumentExecutionSpecSet,
) -> tuple[ReconciliationDiscrepancy, ...]:
    observed: dict[Instrument, CanonicalDecimal] = {}
    for balance in observation.balances:
        if type(balance) is PositionReconciliationBalance:
            observed[balance.instrument] = balance.quantity
    local = {balance.instrument: balance.quantity for balance in snapshot.position_balances}
    targets = sorted(set(observed) | set(local), key=lambda value: value.key)
    discrepancies: list[ReconciliationDiscrepancy] = []
    for instrument in targets:
        local_amount = local.get(instrument, CanonicalDecimal("0"))
        observed_amount = observed.get(instrument, CanonicalDecimal("0"))
        if local_amount == observed_amount:
            continue
        discrepancies.append(
            create_position_reconciliation_discrepancy(
                spec_set=spec_set,
                instrument=instrument,
                local_amount=local_amount,
                observed_amount=observed_amount,
            )
        )
    return tuple(discrepancies)


def _compare_cash_balances(
    observation: ReconciliationObservation,
    snapshot: PortfolioSnapshot,
    spec_set: InstrumentExecutionSpecSet,
) -> tuple[ReconciliationDiscrepancy, ...]:
    observed: dict[SettlementCurrency, CanonicalDecimal] = {}
    for balance in observation.balances:
        if type(balance) is CashReconciliationBalance:
            observed[balance.currency] = balance.amount
    local = {balance.currency: balance.amount for balance in snapshot.cash_balances}
    targets = sorted(set(observed) | set(local), key=lambda value: value.code)
    discrepancies: list[ReconciliationDiscrepancy] = []
    for currency in targets:
        local_amount = local.get(currency, CanonicalDecimal("0"))
        observed_amount = observed.get(currency, CanonicalDecimal("0"))
        if local_amount == observed_amount:
            continue
        discrepancies.append(
            create_cash_reconciliation_discrepancy(
                spec_set=spec_set,
                currency=currency,
                local_amount=local_amount,
                observed_amount=observed_amount,
            )
        )
    return tuple(discrepancies)


def _observation_scope_is_complete(
    observation: ReconciliationObservation,
    snapshot: PortfolioSnapshot,
) -> bool:
    """ADR 0022 L148-149: omission means invalid evidence, not a zero balance."""
    if observation.kind is ReconciliationObservationKind.POSITION_SNAPSHOT:
        observed = {
            balance.instrument
            for balance in observation.balances
            if type(balance) is PositionReconciliationBalance
        }
        local = {balance.instrument for balance in snapshot.position_balances}
        return local <= observed
    cash_observed = {
        balance.currency
        for balance in observation.balances
        if type(balance) is CashReconciliationBalance
    }
    cash_local = {balance.currency for balance in snapshot.cash_balances}
    return cash_local <= cash_observed


def _require_uint64(value: object, field: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact int")
    if not 0 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} must be uint64")
    return value


def _advance(value: int) -> int | None:
    if type(value) is not int or not 1 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "authority sequence must be uint64 and positive")
    return None if value == _MAX_UINT64 else value + 1


def _freeze_observation_index(
    index: dict[tuple[SourceNamespace, int], Sha256Digest],
) -> Mapping[tuple[SourceNamespace, int], Sha256Digest]:
    return MappingProxyType(dict(index))


def _freeze_outcome_index(
    index: dict[Sha256Digest, ReconciliationOutcome],
) -> Mapping[Sha256Digest, ReconciliationOutcome]:
    return MappingProxyType(dict(index))


def _freeze_authorization_index(
    index: dict[EconomicId, Sha256Digest],
) -> Mapping[EconomicId, Sha256Digest]:
    return MappingProxyType(dict(index))


def _freeze_adjustment_index(
    index: dict[EconomicId, Sha256Digest],
) -> Mapping[EconomicId, Sha256Digest]:
    return MappingProxyType(dict(index))


def _freeze_command_index(
    index: dict[Sha256Digest, Sha256Digest],
) -> Mapping[Sha256Digest, Sha256Digest]:
    return MappingProxyType(dict(index))
