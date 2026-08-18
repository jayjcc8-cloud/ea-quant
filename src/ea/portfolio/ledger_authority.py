"""Sealed audited-handoff-to-ledger authority from Accepted ADR 0022."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_messages import Fill, fill_digest
from ea.core.execution_state import (
    ExecutionFactAction,
    ExecutionFactProcessingOutcome,
    execution_fact_processing_outcome_digest,
)
from ea.core.ledger_integration import (
    LedgerHandoffAction,
    LedgerHandoffFailure,
    LedgerHandoffOutcome,
    _create_ledger_application_command,
    create_ledger_handoff_outcome,
)
from ea.core.lifecycle import (
    _VALUE_SEAL as _HANDOFF_SEAL,
)
from ea.core.lifecycle import (
    AuditedExecutionFactHandoff,
    audited_execution_fact_handoff_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    LedgerConflictKind,
    ledger_apply_outcome_digest,
    portfolio_snapshot_digest,
)
from ea.core.run import RunId, Sha256Digest
from ea.portfolio.ledger import PortfolioLedger

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class LedgerHandoffAuthorityError(ValueError):
    """Closed failure at the audited-handoff-to-ledger boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("ledger handoff errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> LedgerHandoffAuthorityError:
    return LedgerHandoffAuthorityError(code, message)


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    ledger: PortfolioLedger


@final
class Phase1LedgerHandoffAuthority:
    """The sole eligible audited-handoff-to-ledger integration authority."""

    _state: _AuthorityState

    __slots__ = ("_state",)

    def __init__(self) -> None:
        raise TypeError("ledger handoff authorities are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._state.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._state.spec_set

    @property
    def snapshot(self) -> object:
        return self._state.ledger.snapshot

    def apply_handoff(
        self,
        *,
        handoff: AuditedExecutionFactHandoff,
        outcome: ExecutionFactProcessingOutcome,
        fill: Fill | None,
    ) -> LedgerHandoffOutcome:
        """Apply one audited handoff exactly once and return its sole result."""
        state = self._state
        _require_handoff_evidence(handoff, outcome, fill, state.run_id)
        ledger = state.ledger
        run_id = state.run_id
        handoff_sha256 = audited_execution_fact_handoff_digest(handoff)
        unchanged_version = ledger.snapshot.snapshot_version
        unchanged_sha256 = portfolio_snapshot_digest(ledger.snapshot)

        if fill is None:
            return create_ledger_handoff_outcome(
                run_id=run_id,
                dispatch_sequence=handoff.dispatch_sequence,
                ingress_identity=handoff.ingress_identity,
                audited_handoff_sha256=handoff_sha256,
                processing_outcome_sha256=handoff.outcome_sha256,
                processing_outcome_ack_sha256=handoff.outcome_ack_sha256,
                fill_id=None,
                fill_sha256=None,
                action=LedgerHandoffAction.NOT_APPLICABLE,
                original_ledger_apply_outcome=None,
                original_ledger_apply_outcome_sha256=None,
                before_snapshot_version=unchanged_version,
                before_snapshot_sha256=unchanged_sha256,
                after_snapshot_version=unchanged_version,
                after_snapshot_sha256=unchanged_sha256,
                requires_reconciliation=False,
                halt_requested=False,
                failure=None,
            )
        if outcome.action not in {
            ExecutionFactAction.ACCEPTED,
            ExecutionFactAction.UNRESOLVED,
        }:
            return _evidence_mismatch_handoff(
                run_id=run_id,
                handoff=handoff,
                handoff_sha256=handoff_sha256,
                version=unchanged_version,
                snapshot_sha256=unchanged_sha256,
            )
        fill_sha256 = fill_digest(fill)
        missing_ancestry = (
            fill.order_id is None or fill.correlation_id is None or fill.causation_id is None
        )
        requires_reconciliation = outcome.requires_reconciliation or missing_ancestry
        command = _create_ledger_application_command(
            run_id=run_id,
            dispatch_sequence=handoff.dispatch_sequence,
            audited_handoff_sha256=handoff_sha256,
            processing_outcome_sha256=handoff.outcome_sha256,
            fill_id=fill.fill_id,
            fill_sha256=fill_sha256,
            requires_reconciliation=requires_reconciliation,
        )
        result = ledger.apply_ledger_application_command(command, fill)
        if result.code is OutcomeCode.LEDGER_APPLIED:
            binding = ledger._resolve_handoff_binding(command.audited_handoff_sha256)
            assert binding is not None
            return create_ledger_handoff_outcome(
                run_id=run_id,
                dispatch_sequence=handoff.dispatch_sequence,
                ingress_identity=handoff.ingress_identity,
                audited_handoff_sha256=handoff_sha256,
                processing_outcome_sha256=handoff.outcome_sha256,
                processing_outcome_ack_sha256=handoff.outcome_ack_sha256,
                fill_id=fill.fill_id,
                fill_sha256=fill_sha256,
                action=LedgerHandoffAction.EFFECT_COMMITTED,
                original_ledger_apply_outcome=binding.original_outcome_bytes,
                original_ledger_apply_outcome_sha256=ledger_apply_outcome_digest(
                    binding.original_outcome
                ),
                before_snapshot_version=result.before_snapshot_version,
                before_snapshot_sha256=binding.before_snapshot_sha256,
                after_snapshot_version=result.after_snapshot_version,
                after_snapshot_sha256=binding.after_snapshot_sha256,
                requires_reconciliation=requires_reconciliation,
                halt_requested=requires_reconciliation,
                failure=None,
            )
        if (
            result.code is OutcomeCode.LEDGER_CONFLICT
            and result.conflict_kind is LedgerConflictKind.UNBOUND_EXISTING_FILL
        ):
            failure = LedgerHandoffFailure.UNBOUND_EXISTING_FILL
        elif result.code is OutcomeCode.LEDGER_CONFLICT:
            failure = LedgerHandoffFailure.LEDGER_CONFLICT
        elif result.code is OutcomeCode.LEDGER_UNBALANCED:
            failure = LedgerHandoffFailure.UNBALANCED
        elif result.code in {
            OutcomeCode.ARITHMETIC_OVERFLOW,
            OutcomeCode.ROUNDING_UNREPRESENTABLE,
        }:
            failure = LedgerHandoffFailure.ARITHMETIC_FAILURE
        else:
            failure = LedgerHandoffFailure.STRUCTURAL_ERROR
        # A conflict or failure is audited as its own evidence; the closed
        # handoff matrix retains original apply-outcome bytes only for the
        # first application (ADR 0022 L92-104).
        return create_ledger_handoff_outcome(
            run_id=run_id,
            dispatch_sequence=handoff.dispatch_sequence,
            ingress_identity=handoff.ingress_identity,
            audited_handoff_sha256=handoff_sha256,
            processing_outcome_sha256=handoff.outcome_sha256,
            processing_outcome_ack_sha256=handoff.outcome_ack_sha256,
            fill_id=fill.fill_id,
            fill_sha256=fill_sha256,
            action=LedgerHandoffAction.FAILED,
            original_ledger_apply_outcome=None,
            original_ledger_apply_outcome_sha256=None,
            before_snapshot_version=result.before_snapshot_version,
            before_snapshot_sha256=unchanged_sha256,
            after_snapshot_version=result.after_snapshot_version,
            after_snapshot_sha256=result.snapshot_sha256,
            requires_reconciliation=requires_reconciliation,
            halt_requested=True,
            failure=failure,
        )


def _evidence_mismatch_handoff(
    *,
    run_id: RunId,
    handoff: AuditedExecutionFactHandoff,
    handoff_sha256: Sha256Digest,
    version: int,
    snapshot_sha256: Sha256Digest,
) -> LedgerHandoffOutcome:
    return create_ledger_handoff_outcome(
        run_id=run_id,
        dispatch_sequence=handoff.dispatch_sequence,
        ingress_identity=handoff.ingress_identity,
        audited_handoff_sha256=handoff_sha256,
        processing_outcome_sha256=handoff.outcome_sha256,
        processing_outcome_ack_sha256=handoff.outcome_ack_sha256,
        fill_id=None,
        fill_sha256=None,
        action=LedgerHandoffAction.FAILED,
        original_ledger_apply_outcome=None,
        original_ledger_apply_outcome_sha256=None,
        before_snapshot_version=version,
        before_snapshot_sha256=snapshot_sha256,
        after_snapshot_version=version,
        after_snapshot_sha256=snapshot_sha256,
        requires_reconciliation=False,
        halt_requested=True,
        failure=LedgerHandoffFailure.EVIDENCE_MISMATCH,
    )


def _require_handoff_evidence(
    handoff: AuditedExecutionFactHandoff,
    outcome: ExecutionFactProcessingOutcome,
    fill: Fill | None,
    run_id: RunId,
) -> None:
    if (
        type(handoff) is not AuditedExecutionFactHandoff
        or handoff._seal is not _HANDOFF_SEAL
        or type(outcome) is not ExecutionFactProcessingOutcome
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "handoff evidence must be exact")
    if execution_fact_processing_outcome_digest(outcome) != handoff.outcome_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "handoff outcome digest conflicts")
    has_id = handoff.fill_id is not None
    has_sha = handoff.fill_sha256 is not None
    if has_id != has_sha:
        raise _fail(OutcomeCode.CONFLICTING_ID, "handoff fill evidence is incomplete")
    if fill is None:
        if has_id or has_sha:
            raise _fail(OutcomeCode.CONFLICTING_ID, "handoff fill evidence is incomplete")
        return
    if type(fill) is not Fill or fill.run_id != run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "resolved fill conflicts with the run")
    if handoff.fill_id != fill.fill_id or handoff.fill_sha256 != fill_digest(fill):
        raise _fail(OutcomeCode.CONFLICTING_ID, "handoff fill digests conflict")


def create_phase1_ledger_handoff_authority(
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    ledger: PortfolioLedger,
) -> Phase1LedgerHandoffAuthority:
    """Create one run/specification-bound authority over the exact ledger."""
    if type(run_id) is not RunId or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "authority binding must be exact")
    if type(ledger) is not PortfolioLedger or ledger.snapshot.run_id != run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger binding conflicts")
    if (
        ledger.snapshot.instrument_spec_set_id != spec_set.identifier
        or ledger.snapshot.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger specification binding conflicts")
    value = object.__new__(Phase1LedgerHandoffAuthority)
    object.__setattr__(value, "_state", _AuthorityState(run_id, spec_set, ledger))
    return value
