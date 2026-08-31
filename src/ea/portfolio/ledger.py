"""Single in-memory Fill ledger authority from Accepted ADR 0010."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_quantized,
)
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    SettlementCurrency,
    instrument_spec_set_digest,
    settle_execution,
    validate_execution_inputs,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind, FactDedupKey
from ea.core.execution_messages import FeeCode, FeeEntry, Fill, fill_digest
from ea.core.identity import Instrument
from ea.core.initial_funding import (
    InitialFundingConflictKind,
    InitialFundingOutcome,
    InitialFundingResult,
    InitialFundingSpec,
    InitialFundingTransaction,
    initial_funding_spec_digest,
    initial_funding_transaction_digest,
    validate_initial_funding_spec,
)
from ea.core.ledger_integration import (
    _VALUE_SEAL as _LEDGER_INTEGRATION_SEAL,
)
from ea.core.ledger_integration import (
    LedgerApplicationCommand,
    ledger_application_command_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    CashBalance,
    CurrencyCommodity,
    ExistingLedgerBinding,
    InstrumentCommodity,
    LedgerAccountKind,
    LedgerApplyOutcome,
    LedgerConflictKind,
    LedgerFailureStage,
    LedgerPosting,
    LedgerTransaction,
    OpenReconciliationRef,
    PortfolioLedgerError,
    PortfolioSnapshot,
    PositionBalance,
    RoundingBalance,
    UnresolvedFillRef,
    _create_ledger_apply_outcome,
    canonical_ledger_apply_outcome_bytes,
    canonical_ledger_transaction_bytes,
    canonical_portfolio_snapshot_bytes,
    ledger_apply_outcome_digest,
    ledger_transaction_digest,
    portfolio_snapshot_digest,
)
from ea.core.reconciliation import (
    _VALUE_SEAL as _RECONCILIATION_SEAL,
)
from ea.core.reconciliation import (
    AuditedReconciliationAdjustmentAuthorization,
    CanonicalPortfolioTransaction,
    ReconciliationAdjustmentAuthorization,
    ReconciliationAdjustmentCommand,
    ReconciliationAdjustmentConflictKind,
    ReconciliationAdjustmentFailureKind,
    ReconciliationAdjustmentOutcome,
    ReconciliationAdjustmentResult,
    ReconciliationAdjustmentTargetKind,
    ReconciliationAdjustmentVariant,
    ReconciliationAuthorizationDecision,
    ReconciliationTransaction,
    _create_reconciliation_adjustment_outcome,
    _create_reconciliation_transaction,
    canonical_reconciliation_adjustment_outcome_bytes,
    canonical_reconciliation_transaction_bytes,
    reconciliation_adjustment_command_digest,
    reconciliation_adjustment_outcome_digest,
    reconciliation_transaction_digest,
)
from ea.core.run import RunBinding, RunId, Sha256Digest

_MAX_UINT64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class _HandoffBinding:
    """One non-evicting audited-handoff integration binding (ADR 0022 L118-124)."""

    command_sha256: Sha256Digest
    processing_outcome_sha256: Sha256Digest
    fill_sha256: Sha256Digest
    original_outcome: LedgerApplyOutcome
    original_outcome_bytes: bytes
    transaction_sha256: Sha256Digest
    before_snapshot_sha256: Sha256Digest
    after_snapshot_sha256: Sha256Digest


@dataclass(frozen=True, slots=True)
class _LedgerState:
    cash: Mapping[SettlementCurrency, CanonicalDecimal]
    positions: Mapping[Instrument, CanonicalDecimal]
    rounding: Mapping[SettlementCurrency, CanonicalDecimal]
    unresolved: Mapping[EconomicId, UnresolvedFillRef]
    fill_index: Mapping[EconomicId, ExistingLedgerBinding]
    fact_index: Mapping[FactDedupKey, ExistingLedgerBinding]
    entry_index: Mapping[EconomicId, CanonicalPortfolioTransaction]
    transactions: tuple[CanonicalPortfolioTransaction, ...]
    open_reconciliation_bindings: Mapping[EconomicId, ExistingLedgerBinding]
    open_reconciliation_refs: Mapping[EconomicId, OpenReconciliationRef]
    handoff_index: Mapping[Sha256Digest, _HandoffBinding]
    authorization_index: Mapping[Sha256Digest, Sha256Digest]
    adjustment_index: Mapping[EconomicId, Sha256Digest]
    observation_index: Mapping[Sha256Digest, EconomicId]
    command_index: Mapping[Sha256Digest, ReconciliationAdjustmentOutcome]
    adjustment_outcomes: Mapping[Sha256Digest, ReconciliationAdjustmentOutcome]
    initial_funding_outcome: InitialFundingOutcome | None
    snapshot: PortfolioSnapshot


@final
class PortfolioLedger:
    """The only mutable owner of Fill-derived transactions and balances."""

    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet
    _spec_set_sha256: Sha256Digest
    _currency_quanta: Mapping[SettlementCurrency, CanonicalDecimal]
    _spec_by_instrument: Mapping[Instrument, InstrumentExecutionSpec]
    _state: _LedgerState

    __slots__ = (
        "_currency_quanta",
        "_run_id",
        "_spec_by_instrument",
        "_spec_set",
        "_spec_set_sha256",
        "_state",
    )

    def __init__(self) -> None:
        raise TypeError("PortfolioLedger values are created only by create_portfolio_ledger")

    @property
    def snapshot(self) -> PortfolioSnapshot:
        return self._state.snapshot

    @property
    def transactions(self) -> tuple[CanonicalPortfolioTransaction, ...]:
        return self._state.transactions

    def _resolve_handoff_binding(
        self, audited_handoff_sha256: Sha256Digest
    ) -> _HandoffBinding | None:
        """Return the retained original integration binding or None."""
        return self._state.handoff_index.get(audited_handoff_sha256)

    def apply_fill(self, fill: Fill) -> LedgerApplyOutcome:
        """Apply one canonical Fill once or return exact immutable replay evidence."""
        if type(fill) is not Fill:
            raise PortfolioLedgerError(
                OutcomeCode.INVALID_TYPE,
                "fill must be an exact Fill",
            )
        if fill.run_id != self._run_id:
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "fill run conflicts with ledger run",
            )
        submitted_digest = fill_digest(fill)
        fill_binding = self._state.fill_index.get(fill.fill_id)
        fact_binding = self._state.fact_index.get(fill.fact_key)
        replay = self._classify_replay(
            fill=fill,
            submitted_digest=submitted_digest,
            fill_binding=fill_binding,
            fact_binding=fact_binding,
        )
        if replay is not None:
            return replay
        return self._apply_new_fill(fill=fill, submitted_digest=submitted_digest)

    def apply_initial_funding(
        self,
        spec: InitialFundingSpec,
        *,
        binding: RunBinding,
        prepared_acknowledgement: Sha256Digest,
    ) -> InitialFundingOutcome:
        """Apply the immutable ledger genesis once or retain its exact result."""
        if (
            type(spec) is not InitialFundingSpec
            or type(binding) is not RunBinding
            or type(prepared_acknowledgement) is not Sha256Digest
        ):
            raise PortfolioLedgerError(OutcomeCode.INVALID_TYPE, "funding evidence must be exact")
        if binding.reference.run_id != self._run_id:
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID, "funding binding run conflicts with ledger run"
            )
        validate_initial_funding_spec(spec, self._spec_set)
        submitted_spec_sha256 = initial_funding_spec_digest(spec)
        retained = self._state.initial_funding_outcome
        if retained is not None:
            if (
                retained.submitted_funding_spec_sha256 == submitted_spec_sha256
                and retained.manifest_sha256 == binding.manifest_sha256
                and retained.transaction is not None
                and retained.transaction.lineage_sha256 == binding.reference.lineage_sha256
                and retained.prepared_audit_acknowledgement_sha256 == prepared_acknowledgement
            ):
                return retained
            kind = (
                InitialFundingConflictKind.FUNDING_BINDING_CONFLICT
                if retained.submitted_funding_spec_sha256 != submitted_spec_sha256
                else InitialFundingConflictKind.MANIFEST_BINDING_CONFLICT
                if retained.manifest_sha256 != binding.manifest_sha256
                or retained.transaction is None
                or retained.transaction.lineage_sha256 != binding.reference.lineage_sha256
                else InitialFundingConflictKind.AUDIT_PREDECESSOR_CONFLICT
            )
            return self._initial_funding_conflict(
                spec_sha256=submitted_spec_sha256,
                binding=binding,
                acknowledgement=prepared_acknowledgement,
                kind=kind,
                existing=retained.transaction,
            )
        entry_id = EconomicId(self._run_id, EconomicOwnerKind.LEDGER_ENTRY, 1)
        occupied = self._state.entry_index.get(entry_id)
        if occupied is not None or self._state.snapshot.ledger_sequence != 0:
            return self._initial_funding_conflict(
                spec_sha256=submitted_spec_sha256,
                binding=binding,
                acknowledgement=prepared_acknowledgement,
                kind=InitialFundingConflictKind.ENTRY_ID_OCCUPIED,
                existing=occupied,
            )
        currency = spec.settlement_currency
        postings = (
            LedgerPosting(
                LedgerAccountKind.PORTFOLIO_CASH, CurrencyCommodity(currency), spec.amount
            ),
            LedgerPosting(
                LedgerAccountKind.EXTERNAL_SETTLEMENT,
                CurrencyCommodity(currency),
                _signed(spec.amount, -1),
            ),
        )
        transaction = InitialFundingTransaction(
            run_id=self._run_id,
            entry_id=entry_id,
            ledger_sequence=1,
            manifest_sha256=binding.manifest_sha256,
            lineage_sha256=binding.reference.lineage_sha256,
            funding_spec_sha256=submitted_spec_sha256,
            instrument_spec_set_id=spec.instrument_spec_set_id,
            instrument_spec_set_sha256=spec.instrument_spec_set_sha256,
            settlement_currency=currency,
            currency_quantum=spec.currency_quantum,
            amount=spec.amount,
            prepared_audit_acknowledgement_sha256=prepared_acknowledgement,
            previous_transaction_sha256=None,
            postings=postings,
        )
        digest = initial_funding_transaction_digest(transaction)
        next_cash = dict(self._state.cash)
        _assign_nonzero(next_cash, currency, spec.amount)
        next_entry_index = dict(self._state.entry_index)
        next_entry_index[entry_id] = transaction
        next_transactions = (*self._state.transactions, transaction)
        next_snapshot = _snapshot(
            ledger=self,
            sequence=1,
            last_entry_id=entry_id,
            last_transaction_sha256=digest,
            cash=next_cash,
            positions=self._state.positions,
            rounding=self._state.rounding,
            unresolved=self._state.unresolved,
            open_reconciliation_bindings=self._state.open_reconciliation_bindings,
            open_reconciliation_refs=self._state.open_reconciliation_refs,
        )
        outcome = InitialFundingOutcome(
            run_id=self._run_id,
            result=InitialFundingResult.APPLIED,
            manifest_sha256=binding.manifest_sha256,
            submitted_funding_spec_sha256=submitted_spec_sha256,
            prepared_audit_acknowledgement_sha256=prepared_acknowledgement,
            before_snapshot_version=0,
            after_snapshot_version=1,
            snapshot=next_snapshot,
            transaction=transaction,
            existing_transaction_sha256=None,
            conflict_kind=None,
        )
        self._state = _freeze_state(
            cash=next_cash,
            positions=self._state.positions,
            rounding=self._state.rounding,
            unresolved=self._state.unresolved,
            fill_index=self._state.fill_index,
            fact_index=self._state.fact_index,
            entry_index=next_entry_index,
            transactions=next_transactions,
            open_reconciliation_bindings=self._state.open_reconciliation_bindings,
            open_reconciliation_refs=self._state.open_reconciliation_refs,
            handoff_index=self._state.handoff_index,
            authorization_index=self._state.authorization_index,
            adjustment_index=self._state.adjustment_index,
            observation_index=self._state.observation_index,
            command_index=self._state.command_index,
            adjustment_outcomes=self._state.adjustment_outcomes,
            initial_funding_outcome=outcome,
            snapshot=next_snapshot,
        )
        return outcome

    def _initial_funding_conflict(
        self,
        *,
        spec_sha256: Sha256Digest,
        binding: RunBinding,
        acknowledgement: Sha256Digest,
        kind: InitialFundingConflictKind,
        existing: CanonicalPortfolioTransaction | None,
    ) -> InitialFundingOutcome:
        version = self._state.snapshot.snapshot_version
        return InitialFundingOutcome(
            run_id=self._run_id,
            result=InitialFundingResult.CONFLICT,
            manifest_sha256=binding.manifest_sha256,
            submitted_funding_spec_sha256=spec_sha256,
            prepared_audit_acknowledgement_sha256=acknowledgement,
            before_snapshot_version=version,
            after_snapshot_version=version,
            snapshot=self._state.snapshot,
            transaction=None,
            existing_transaction_sha256=None if existing is None else _transaction_digest(existing),
            conflict_kind=kind,
        )

    def apply_ledger_application_command(
        self,
        command: LedgerApplicationCommand,
        fill: Fill,
    ) -> LedgerApplyOutcome:
        """Apply one audited command exactly once through the sole integration path."""
        if (
            type(command) is not LedgerApplicationCommand
            or command._seal is not _LEDGER_INTEGRATION_SEAL
            or type(fill) is not Fill
        ):
            raise PortfolioLedgerError(
                OutcomeCode.INVALID_TYPE,
                "command integration requires exact factory-issued evidence",
            )
        if command.run_id != self._run_id or fill.run_id != self._run_id:
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "command or fill run conflicts with ledger run",
            )
        submitted_digest = fill_digest(fill)
        if command.fill_id != fill.fill_id or command.fill_sha256 != submitted_digest:
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "command fill evidence conflicts",
            )
        existing = self._state.handoff_index.get(command.audited_handoff_sha256)
        if existing is not None:
            if existing.command_sha256 != ledger_application_command_digest(command):
                raise PortfolioLedgerError(
                    OutcomeCode.CONFLICTING_ID,
                    "handoff command replay conflicts with its original binding",
                )
            return existing.original_outcome
        fill_binding = self._state.fill_index.get(fill.fill_id)
        fact_binding = self._state.fact_index.get(fill.fact_key)
        if fill_binding is not None or fact_binding is not None:
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=LedgerConflictKind.UNBOUND_EXISTING_FILL,
                fill_binding=fill_binding,
                fact_binding=fact_binding,
            )
        return self._apply_new_fill(
            fill=fill,
            submitted_digest=submitted_digest,
            requires_reconciliation=command.requires_reconciliation,
            processing_outcome_sha256=command.processing_outcome_sha256,
            audited_handoff_sha256=command.audited_handoff_sha256,
            command_sha256=ledger_application_command_digest(command),
        )

    def apply_reconciliation_adjustment(
        self,
        audited: AuditedReconciliationAdjustmentAuthorization,
        command: ReconciliationAdjustmentCommand,
    ) -> ReconciliationAdjustmentOutcome:
        """Apply one exact acknowledged adjustment once through the shared entry chain."""
        if (
            type(audited) is not AuditedReconciliationAdjustmentAuthorization
            or audited._seal is not _RECONCILIATION_SEAL
            or type(command) is not ReconciliationAdjustmentCommand
            or command._seal is not _RECONCILIATION_SEAL
        ):
            raise PortfolioLedgerError(
                OutcomeCode.INVALID_TYPE,
                "adjustment requires exact factory-issued evidence",
            )
        authorization = audited.authorization
        if (
            authorization.run_id != self._run_id
            or command.run_id != self._run_id
            or command.adjustment_id.run_id != self._run_id
        ):
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "adjustment evidence runs conflict with the ledger run",
            )
        command_sha256 = reconciliation_adjustment_command_digest(command)
        if (
            authorization.command_sha256 != command_sha256
            or authorization.adjustment_id != command.adjustment_id
            or authorization.observation_sha256 != command.observation_sha256
            or authorization.reconciliation_outcome_sha256 != command.reconciliation_outcome_sha256
            or authorization.ledger_sequence != command.ledger_sequence
            or authorization.local_snapshot_sha256 != command.local_snapshot_sha256
            or authorization.dispatch_sequence != command.dispatch_sequence
        ):
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "adjustment authorization conflicts with its command",
            )
        authorization_sha256 = audited.authorization_sha256
        if authorization.decision is not ReconciliationAuthorizationDecision.ALLOWED:
            return self._adjustment_failed(
                command=command,
                authorization_sha256=authorization_sha256,
                failure=ReconciliationAdjustmentFailureKind.INVALID_COMMAND,
            )
        replay = self._state.adjustment_outcomes.get(authorization_sha256)
        if replay is not None:
            if (
                replay.adjustment_id == command.adjustment_id
                and replay.adjustment_command_sha256 == command_sha256
            ):
                return replay
            return self._adjustment_conflict(
                command=command,
                authorization_sha256=authorization_sha256,
                conflict=ReconciliationAdjustmentConflictKind.AUTHORIZATION_ID_COLLISION,
            )
        if self._state.adjustment_index.get(command.adjustment_id) is not None:
            return self._adjustment_conflict(
                command=command,
                authorization_sha256=authorization_sha256,
                conflict=ReconciliationAdjustmentConflictKind.ADJUSTMENT_ID_COLLISION,
            )
        if self._state.observation_index.get(command.observation_sha256) is not None:
            return self._adjustment_conflict(
                command=command,
                authorization_sha256=authorization_sha256,
                conflict=ReconciliationAdjustmentConflictKind.OBSERVATION_ALREADY_CONSUMED,
            )
        snapshot = self._state.snapshot
        if (
            command.ledger_sequence != snapshot.ledger_sequence
            or command.local_snapshot_sha256 != portfolio_snapshot_digest(snapshot)
        ):
            return self._adjustment_failed(
                command=command,
                authorization_sha256=authorization_sha256,
                failure=ReconciliationAdjustmentFailureKind.STALE_FRONTIER,
            )
        before_version = snapshot.snapshot_version
        if before_version == _MAX_UINT64:
            return self._adjustment_failed(
                command=command,
                authorization_sha256=authorization_sha256,
                failure=ReconciliationAdjustmentFailureKind.STALE_FRONTIER,
            )
        entry_id = EconomicId(
            self._run_id,
            EconomicOwnerKind.LEDGER_ENTRY,
            before_version + 1,
        )
        if self._state.entry_index.get(entry_id) is not None:
            return self._adjustment_conflict(
                command=command,
                authorization_sha256=authorization_sha256,
                conflict=ReconciliationAdjustmentConflictKind.INDEX_INCONSISTENT,
            )
        if command.variant is ReconciliationAdjustmentVariant.BALANCE_CORRECTION:
            try:
                derived = self._derive_correction(
                    authorization=authorization,
                    command=command,
                    entry_id=entry_id,
                    before_version=before_version,
                    authorization_sha256=authorization_sha256,
                )
            except PortfolioLedgerError as error:
                return self._adjustment_failed(
                    command=command,
                    authorization_sha256=authorization_sha256,
                    failure=_correction_failure_kind(error),
                )
        elif command.variant is ReconciliationAdjustmentVariant.ANCESTRY_RESOLUTION:
            if (
                command.target_kind
                is not ReconciliationAdjustmentTargetKind.OPEN_RECONCILIATION_REF
                or command.open_reconciliation_ref is None
            ):
                return self._adjustment_failed(
                    command=command,
                    authorization_sha256=authorization_sha256,
                    failure=ReconciliationAdjustmentFailureKind.INVALID_COMMAND,
                )
            reference = command.open_reconciliation_ref
            if self._state.open_reconciliation_refs.get(reference.fill_id) != reference:
                return self._adjustment_failed(
                    command=command,
                    authorization_sha256=authorization_sha256,
                    failure=ReconciliationAdjustmentFailureKind.STALE_FRONTIER,
                )
            derived = self._derive_ancestry_resolution(
                authorization=authorization,
                command=command,
                entry_id=entry_id,
                before_version=before_version,
                authorization_sha256=authorization_sha256,
            )
        else:  # pragma: no cover - closed enum
            raise AssertionError("adjustment variant is outside the closed enum")
        before_sha256 = portfolio_snapshot_digest(self._state.snapshot)
        outcome = _create_reconciliation_adjustment_outcome(
            run_id=self._run_id,
            adjustment_id=command.adjustment_id,
            authorization_sha256=authorization_sha256,
            observation_sha256=command.observation_sha256,
            reconciliation_outcome_sha256=command.reconciliation_outcome_sha256,
            adjustment_command_sha256=command_sha256,
            result=ReconciliationAdjustmentResult.APPLIED,
            transaction=derived.transaction,
            failure_kind=None,
            conflict_kind=None,
            before_snapshot_sha256=before_sha256,
            after_snapshot_sha256=portfolio_snapshot_digest(derived.next_snapshot),
        )
        next_authorization_index = dict(self._state.authorization_index)
        next_authorization_index[authorization_sha256] = command_sha256
        next_adjustment_index = dict(self._state.adjustment_index)
        next_adjustment_index[command.adjustment_id] = command_sha256
        next_observation_index = dict(self._state.observation_index)
        next_observation_index[command.observation_sha256] = command.adjustment_id
        next_command_index = dict(self._state.command_index)
        next_command_index[command_sha256] = outcome
        next_adjustment_outcomes = dict(self._state.adjustment_outcomes)
        next_adjustment_outcomes[authorization_sha256] = outcome
        next_state = _freeze_state(
            cash=derived.next_cash,
            positions=derived.next_positions,
            rounding=self._state.rounding,
            unresolved=derived.next_unresolved,
            fill_index=self._state.fill_index,
            fact_index=self._state.fact_index,
            entry_index=derived.next_entry_index,
            transactions=derived.next_transactions,
            open_reconciliation_bindings=derived.next_open_bindings,
            open_reconciliation_refs=derived.next_open_refs,
            handoff_index=self._state.handoff_index,
            authorization_index=next_authorization_index,
            adjustment_index=next_adjustment_index,
            observation_index=next_observation_index,
            command_index=next_command_index,
            adjustment_outcomes=next_adjustment_outcomes,
            initial_funding_outcome=self._state.initial_funding_outcome,
            snapshot=derived.next_snapshot,
        )
        _preflight_adjustment_evidence(derived, outcome)
        self._state = next_state
        return outcome

    def _derive_correction(
        self,
        *,
        authorization: ReconciliationAdjustmentAuthorization,
        command: ReconciliationAdjustmentCommand,
        entry_id: EconomicId,
        before_version: int,
        authorization_sha256: Sha256Digest,
    ) -> _AdjustmentDerivation:
        if command.local_amount is None or command.observed_amount is None:
            raise PortfolioLedgerError(OutcomeCode.INVALID_TYPE, "correction amounts are missing")
        delta = command.delta
        if delta is None:
            raise PortfolioLedgerError(OutcomeCode.INVALID_TYPE, "correction delta is missing")
        is_position = command.target_kind is ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION
        if is_position:
            if command.instrument is None:
                raise PortfolioLedgerError(
                    OutcomeCode.INVALID_TYPE, "position correction instrument is missing"
                )
            try:
                quantum = self._spec_set.require(command.instrument).quantity_quantum
            except EconomicValidationError as error:
                raise _structural_error(error) from error
            commodity: InstrumentCommodity | CurrencyCommodity = InstrumentCommodity(
                command.instrument
            )
            accounts = (LedgerAccountKind.PORTFOLIO_POSITION, LedgerAccountKind.EXTERNAL_INVENTORY)
            current = self._state.positions.get(command.instrument)
        else:
            if command.currency is None:
                raise PortfolioLedgerError(
                    OutcomeCode.INVALID_TYPE, "cash correction currency is missing"
                )
            quantums = {
                specification.currency_quantum
                for specification in self._spec_set.specifications
                if specification.settlement_currency == command.currency
            }
            if len(quantums) != 1:
                raise PortfolioLedgerError(
                    OutcomeCode.CONFLICTING_ID,
                    "cash correction currency binding conflicts",
                )
            quantum = next(iter(quantums))
            commodity = CurrencyCommodity(command.currency)
            accounts = (LedgerAccountKind.PORTFOLIO_CASH, LedgerAccountKind.EXTERNAL_SETTLEMENT)
            current = self._state.cash.get(command.currency)
        try:
            require_quantized(delta, quantum, field_name="correction delta")
        except EconomicValidationError as error:
            raise PortfolioLedgerError(
                (
                    OutcomeCode.NOT_QUANTIZED
                    if error.code is OutcomeCode.NOT_QUANTIZED
                    else OutcomeCode.OUT_OF_RANGE
                ),
                "correction delta is outside the target grid",
            ) from error
        try:
            next_balance = _next_balance(current, delta)
        except EconomicValidationError:
            raise PortfolioLedgerError(
                OutcomeCode.ARITHMETIC_OVERFLOW,
                "correction balance overflow",
            ) from None
        postings = (
            LedgerPosting(accounts[0], commodity, delta),
            LedgerPosting(accounts[1], commodity, _negated_decimal_value(delta)),
        )
        next_cash = dict(self._state.cash)
        next_positions = dict(self._state.positions)
        if is_position:
            assert command.instrument is not None
            _assign_nonzero(next_positions, command.instrument, next_balance)
        else:
            assert command.currency is not None
            _assign_nonzero(next_cash, command.currency, next_balance)
        transaction = self._build_adjustment_transaction(
            authorization=authorization,
            command=command,
            entry_id=entry_id,
            before_version=before_version,
            authorization_sha256=authorization_sha256,
            postings=postings,
        )
        return self._finish_adjustment_derivation(
            command=command,
            transaction=transaction,
            entry_id=entry_id,
            next_cash=next_cash,
            next_positions=next_positions,
            next_unresolved=dict(self._state.unresolved),
            next_open_bindings=dict(self._state.open_reconciliation_bindings),
            next_open_refs=dict(self._state.open_reconciliation_refs),
        )

    def _derive_ancestry_resolution(
        self,
        *,
        authorization: ReconciliationAdjustmentAuthorization,
        command: ReconciliationAdjustmentCommand,
        entry_id: EconomicId,
        before_version: int,
        authorization_sha256: Sha256Digest,
    ) -> _AdjustmentDerivation:
        assert command.open_reconciliation_ref is not None
        reference = command.open_reconciliation_ref
        next_open_refs = dict(self._state.open_reconciliation_refs)
        next_open_refs.pop(reference.fill_id, None)
        next_open_bindings = dict(self._state.open_reconciliation_bindings)
        next_open_bindings.pop(reference.fill_id, None)
        next_unresolved = dict(self._state.unresolved)
        next_unresolved.pop(reference.fill_id, None)
        transaction = self._build_adjustment_transaction(
            authorization=authorization,
            command=command,
            entry_id=entry_id,
            before_version=before_version,
            authorization_sha256=authorization_sha256,
            postings=(),
        )
        return self._finish_adjustment_derivation(
            command=command,
            transaction=transaction,
            entry_id=entry_id,
            next_cash=dict(self._state.cash),
            next_positions=dict(self._state.positions),
            next_unresolved=next_unresolved,
            next_open_bindings=next_open_bindings,
            next_open_refs=next_open_refs,
        )

    def _build_adjustment_transaction(
        self,
        *,
        authorization: ReconciliationAdjustmentAuthorization,
        command: ReconciliationAdjustmentCommand,
        entry_id: EconomicId,
        before_version: int,
        authorization_sha256: Sha256Digest,
        postings: tuple[LedgerPosting, ...],
    ) -> ReconciliationTransaction:
        previous_digest = (
            None
            if not self._state.transactions
            else _transaction_digest(self._state.transactions[-1])
        )
        return _create_reconciliation_transaction(
            run_id=self._run_id,
            spec_set=self._spec_set,
            entry_id=entry_id,
            ledger_sequence=before_version + 1,
            adjustment_id=command.adjustment_id,
            authorization_sha256=authorization_sha256,
            observation_sha256=command.observation_sha256,
            reconciliation_outcome_sha256=command.reconciliation_outcome_sha256,
            adjustment_command_sha256=reconciliation_adjustment_command_digest(command),
            variant=command.variant,
            target_kind=command.target_kind,
            instrument=command.instrument,
            currency=command.currency,
            local_amount=command.local_amount,
            observed_amount=command.observed_amount,
            delta=command.delta,
            open_reconciliation_ref=command.open_reconciliation_ref,
            previous_transaction_sha256=previous_digest,
            postings=postings,
            occurred_at=authorization.available_at,
            available_at=authorization.available_at,
        )

    def _finish_adjustment_derivation(
        self,
        *,
        command: ReconciliationAdjustmentCommand,
        transaction: ReconciliationTransaction,
        entry_id: EconomicId,
        next_cash: dict[SettlementCurrency, CanonicalDecimal],
        next_positions: dict[Instrument, CanonicalDecimal],
        next_unresolved: Mapping[EconomicId, UnresolvedFillRef] | None = None,
        next_open_bindings: dict[EconomicId, ExistingLedgerBinding],
        next_open_refs: dict[EconomicId, OpenReconciliationRef],
    ) -> _AdjustmentDerivation:
        transaction_sha256 = reconciliation_transaction_digest(transaction)
        next_entry_index = dict(self._state.entry_index)
        next_entry_index[entry_id] = transaction
        next_transactions = (*self._state.transactions, transaction)
        next_snapshot = _snapshot(
            ledger=self,
            sequence=entry_id.owner_sequence,
            last_entry_id=entry_id,
            last_transaction_sha256=transaction_sha256,
            cash=next_cash,
            positions=next_positions,
            rounding=self._state.rounding,
            unresolved=(self._state.unresolved if next_unresolved is None else next_unresolved),
            open_reconciliation_bindings=next_open_bindings,
            open_reconciliation_refs=next_open_refs,
        )
        return _AdjustmentDerivation(
            transaction=transaction,
            next_cash=next_cash,
            next_positions=next_positions,
            next_unresolved=(
                self._state.unresolved if next_unresolved is None else next_unresolved
            ),
            next_open_bindings=next_open_bindings,
            next_open_refs=next_open_refs,
            next_entry_index=next_entry_index,
            next_transactions=next_transactions,
            next_snapshot=next_snapshot,
        )

    def _adjustment_failed(
        self,
        *,
        command: ReconciliationAdjustmentCommand,
        authorization_sha256: Sha256Digest,
        failure: ReconciliationAdjustmentFailureKind,
    ) -> ReconciliationAdjustmentOutcome:
        digest = portfolio_snapshot_digest(self._state.snapshot)
        return _create_reconciliation_adjustment_outcome(
            run_id=self._run_id,
            adjustment_id=command.adjustment_id,
            authorization_sha256=authorization_sha256,
            observation_sha256=command.observation_sha256,
            reconciliation_outcome_sha256=command.reconciliation_outcome_sha256,
            adjustment_command_sha256=reconciliation_adjustment_command_digest(command),
            result=ReconciliationAdjustmentResult.FAILED,
            transaction=None,
            failure_kind=failure,
            conflict_kind=None,
            before_snapshot_sha256=digest,
            after_snapshot_sha256=digest,
        )

    def _adjustment_conflict(
        self,
        *,
        command: ReconciliationAdjustmentCommand,
        authorization_sha256: Sha256Digest,
        conflict: ReconciliationAdjustmentConflictKind,
    ) -> ReconciliationAdjustmentOutcome:
        digest = portfolio_snapshot_digest(self._state.snapshot)
        return _create_reconciliation_adjustment_outcome(
            run_id=self._run_id,
            adjustment_id=command.adjustment_id,
            authorization_sha256=authorization_sha256,
            observation_sha256=command.observation_sha256,
            reconciliation_outcome_sha256=command.reconciliation_outcome_sha256,
            adjustment_command_sha256=reconciliation_adjustment_command_digest(command),
            result=ReconciliationAdjustmentResult.CONFLICT,
            transaction=None,
            failure_kind=None,
            conflict_kind=conflict,
            before_snapshot_sha256=digest,
            after_snapshot_sha256=digest,
        )

    def _apply_new_fill(
        self,
        *,
        fill: Fill,
        submitted_digest: Sha256Digest,
        requires_reconciliation: bool | None = None,
        processing_outcome_sha256: Sha256Digest | None = None,
        audited_handoff_sha256: Sha256Digest | None = None,
        command_sha256: Sha256Digest | None = None,
    ) -> LedgerApplyOutcome:
        if audited_handoff_sha256 is not None and (
            processing_outcome_sha256 is None
            or command_sha256 is None
            or requires_reconciliation is None
        ):
            raise AssertionError("command integration evidence must be complete")
        self._require_specification(fill)
        before_version = self._state.snapshot.snapshot_version
        if before_version == _MAX_UINT64:
            return self._failure(
                fill=fill,
                fill_sha256=submitted_digest,
                code=OutcomeCode.OUT_OF_RANGE,
                stage=LedgerFailureStage.LEDGER_SEQUENCE_EXHAUSTED,
            )
        entry_id = EconomicId(
            self._run_id,
            EconomicOwnerKind.LEDGER_ENTRY,
            before_version + 1,
        )
        occupied = self._state.entry_index.get(entry_id)
        if occupied is not None:
            if type(occupied) is not LedgerTransaction:
                return self._conflict(
                    fill=fill,
                    fill_sha256=submitted_digest,
                    kind=LedgerConflictKind.INDEX_INCONSISTENT,
                )
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=LedgerConflictKind.ENTRY_ID_OCCUPIED,
                entry_binding=_binding_for(occupied),
            )

        try:
            settlement = settle_execution(
                self._spec_set,
                fill.instrument,
                fill.price,
                fill.quantity,
            )
        except EconomicValidationError as error:
            if error.code is OutcomeCode.ARITHMETIC_OVERFLOW:
                return self._failure(
                    fill=fill,
                    fill_sha256=submitted_digest,
                    code=OutcomeCode.ARITHMETIC_OVERFLOW,
                    stage=LedgerFailureStage.SETTLEMENT_ARITHMETIC_OVERFLOW,
                )
            if error.code is OutcomeCode.ROUNDING_UNREPRESENTABLE:
                return self._failure(
                    fill=fill,
                    fill_sha256=submitted_digest,
                    code=OutcomeCode.ROUNDING_UNREPRESENTABLE,
                    stage=LedgerFailureStage.SETTLEMENT_ROUNDING_UNREPRESENTABLE,
                )
            raise _structural_error(error) from error

        try:
            exact_notional = _add_decimal(settlement.amount, settlement.rounding_residual)
        except EconomicValidationError:
            return self._failure(
                fill=fill,
                fill_sha256=submitted_digest,
                code=OutcomeCode.ARITHMETIC_OVERFLOW,
                stage=LedgerFailureStage.EXACT_NOTIONAL_OVERFLOW,
            )

        side_sign = 1 if fill.side.value == "buy" else -1
        position_delta = _signed(fill.quantity, side_sign)
        cash_delta = _signed(settlement.amount, -side_sign)
        rounding_delta = _signed(settlement.rounding_residual, -side_sign)
        postings = _postings(
            instrument=fill.instrument,
            currency=settlement.settlement_currency,
            position_delta=position_delta,
            cash_delta=cash_delta,
            external_notional=_signed(exact_notional, side_sign),
            rounding_delta=rounding_delta,
        )

        try:
            next_cash = _next_balance(
                self._state.cash.get(settlement.settlement_currency),
                cash_delta,
            )
        except EconomicValidationError:
            return self._failure(
                fill=fill,
                fill_sha256=submitted_digest,
                code=OutcomeCode.ARITHMETIC_OVERFLOW,
                stage=LedgerFailureStage.CASH_BALANCE_OVERFLOW,
            )
        try:
            next_position = _next_balance(
                self._state.positions.get(fill.instrument),
                position_delta,
            )
        except EconomicValidationError:
            return self._failure(
                fill=fill,
                fill_sha256=submitted_digest,
                code=OutcomeCode.ARITHMETIC_OVERFLOW,
                stage=LedgerFailureStage.POSITION_BALANCE_OVERFLOW,
            )
        try:
            next_rounding = _next_balance(
                self._state.rounding.get(settlement.settlement_currency),
                rounding_delta,
            )
        except EconomicValidationError:
            return self._failure(
                fill=fill,
                fill_sha256=submitted_digest,
                code=OutcomeCode.ROUNDING_UNREPRESENTABLE,
                stage=LedgerFailureStage.ROUNDING_BALANCE_OVERFLOW,
            )
        if not _postings_balance(postings):
            return self._failure(
                fill=fill,
                fill_sha256=submitted_digest,
                code=OutcomeCode.LEDGER_UNBALANCED,
                stage=LedgerFailureStage.COMMODITY_UNBALANCED,
            )

        next_sequence = before_version + 1

        next_cash_map = dict(self._state.cash)
        _assign_nonzero(next_cash_map, settlement.settlement_currency, next_cash)
        next_position_map = dict(self._state.positions)
        _assign_nonzero(next_position_map, fill.instrument, next_position)
        next_rounding_map = dict(self._state.rounding)
        _assign_nonzero(next_rounding_map, settlement.settlement_currency, next_rounding)

        derived_reconciliation = (
            fill.order_id is None or fill.correlation_id is None or fill.causation_id is None
        )
        if requires_reconciliation is None:
            requires_reconciliation = derived_reconciliation
        elif derived_reconciliation and not requires_reconciliation:
            raise AssertionError("missing Fill ancestry cannot clear reconciliation")

        previous_digest = (
            None
            if not self._state.transactions
            else _transaction_digest(self._state.transactions[-1])
        )
        transaction = LedgerTransaction(
            run_id=self._run_id,
            entry_id=entry_id,
            ledger_sequence=next_sequence,
            fill_id=fill.fill_id,
            fill_sha256=submitted_digest,
            fact_key=fill.fact_key,
            provenance=fill.provenance,
            occurred_at=fill.occurred_at,
            order_id=fill.order_id,
            correlation_id=fill.correlation_id,
            causation_id=fill.causation_id,
            client_submission_key=fill.client_submission_key,
            venue_order_id=fill.venue_order_id,
            instrument_specification_id=fill.instrument_specification_id,
            instrument_spec_set_id=fill.instrument_spec_set_id,
            instrument_spec_set_sha256=fill.instrument_spec_set_sha256,
            previous_transaction_sha256=previous_digest,
            postings=postings,
            requires_reconciliation=requires_reconciliation,
        )
        transaction_sha256 = ledger_transaction_digest(transaction)
        next_unresolved = dict(self._state.unresolved)
        if derived_reconciliation:
            # ADR 0024 L41-42: a missing-ancestry Fill carries BOTH the
            # UnresolvedFillRef and (for the audited command path) the
            # OpenReconciliationRef; the outcome-flag-only case adds the open
            # reference without a legacy unresolved reference.
            next_unresolved[fill.fill_id] = UnresolvedFillRef(
                fill_id=fill.fill_id,
                fill_sha256=submitted_digest,
            )
        next_open_bindings = dict(self._state.open_reconciliation_bindings)
        next_open_refs = dict(self._state.open_reconciliation_refs)
        if requires_reconciliation and audited_handoff_sha256 is not None:
            assert processing_outcome_sha256 is not None
            next_open_bindings[fill.fill_id] = ExistingLedgerBinding(
                entry_id=entry_id,
                fill_id=fill.fill_id,
                fill_sha256=submitted_digest,
                transaction_sha256=transaction_sha256,
            )
            next_open_refs[fill.fill_id] = OpenReconciliationRef(
                fill_id=fill.fill_id,
                fill_sha256=submitted_digest,
                processing_outcome_sha256=processing_outcome_sha256,
            )

        next_snapshot = _snapshot(
            ledger=self,
            sequence=next_sequence,
            last_entry_id=entry_id,
            last_transaction_sha256=transaction_sha256,
            cash=next_cash_map,
            positions=next_position_map,
            rounding=next_rounding_map,
            unresolved=next_unresolved,
            open_reconciliation_bindings=next_open_bindings,
            open_reconciliation_refs=next_open_refs,
        )
        binding = ExistingLedgerBinding(
            entry_id=entry_id,
            fill_id=fill.fill_id,
            fill_sha256=submitted_digest,
            transaction_sha256=transaction_sha256,
        )
        next_fill_index = dict(self._state.fill_index)
        next_fill_index[fill.fill_id] = binding
        next_fact_index = dict(self._state.fact_index)
        next_fact_index[fill.fact_key] = binding
        next_entry_index = dict(self._state.entry_index)
        next_entry_index[entry_id] = transaction
        next_transactions = (*self._state.transactions, transaction)
        next_handoff_index = dict(self._state.handoff_index)
        outcome = _create_ledger_apply_outcome(
            run_id=self._run_id,
            code=OutcomeCode.LEDGER_APPLIED,
            before_snapshot_version=before_version,
            after_snapshot_version=next_sequence,
            snapshot=next_snapshot,
            transaction=transaction,
            submitted_fill_id=fill.fill_id,
            submitted_fill_sha256=submitted_digest,
        )
        if audited_handoff_sha256 is not None:
            assert command_sha256 is not None and processing_outcome_sha256 is not None
            next_handoff_index[audited_handoff_sha256] = _HandoffBinding(
                command_sha256=command_sha256,
                processing_outcome_sha256=processing_outcome_sha256,
                fill_sha256=submitted_digest,
                original_outcome=outcome,
                original_outcome_bytes=canonical_ledger_apply_outcome_bytes(outcome),
                transaction_sha256=transaction_sha256,
                before_snapshot_sha256=portfolio_snapshot_digest(self._state.snapshot),
                after_snapshot_sha256=portfolio_snapshot_digest(next_snapshot),
            )
        next_state = _freeze_state(
            cash=next_cash_map,
            positions=next_position_map,
            rounding=next_rounding_map,
            unresolved=next_unresolved,
            fill_index=next_fill_index,
            fact_index=next_fact_index,
            entry_index=next_entry_index,
            transactions=next_transactions,
            open_reconciliation_bindings=next_open_bindings,
            open_reconciliation_refs=next_open_refs,
            handoff_index=next_handoff_index,
            authorization_index=self._state.authorization_index,
            adjustment_index=self._state.adjustment_index,
            observation_index=self._state.observation_index,
            command_index=self._state.command_index,
            adjustment_outcomes=self._state.adjustment_outcomes,
            initial_funding_outcome=self._state.initial_funding_outcome,
            snapshot=next_snapshot,
        )
        _preflight_canonical_evidence(
            transaction=transaction,
            snapshot=next_snapshot,
            outcome=outcome,
        )
        self._state = next_state
        return outcome

    def _require_specification(self, fill: Fill) -> InstrumentExecutionSpec:
        try:
            specification = self._spec_set.require(fill.instrument)
        except EconomicValidationError as error:
            raise _structural_error(error) from error
        if (
            fill.instrument_specification_id != specification.specification_id
            or fill.instrument_spec_set_id != self._spec_set.identifier
            or fill.instrument_spec_set_sha256 != self._spec_set_sha256
        ):
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "fill specification lineage conflicts with ledger",
            )
        try:
            validated = validate_execution_inputs(
                self._spec_set,
                fill.instrument,
                fill.price,
                fill.quantity,
            )
        except EconomicValidationError as error:
            raise _structural_error(error) from error
        if validated is not specification:
            raise AssertionError("execution validation changed the canonical specification")
        if (
            type(fill.fees) is not tuple
            or len(fill.fees) != 1
            or type(fill.fees[0]) is not FeeEntry
        ):
            raise PortfolioLedgerError(
                OutcomeCode.INVALID_TYPE,
                "Phase 1 fill fees must be one exact FeeEntry tuple",
            )
        fee = fill.fees[0]
        if (
            fee.fee_code is not FeeCode.COMMISSION
            or fee.currency != specification.settlement_currency
            or fee.amount.text != "0"
        ):
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "Phase 1 fill fee conflicts with ledger specification",
            )
        try:
            require_quantized(
                fee.amount,
                specification.currency_quantum,
                field_name="fee",
            )
        except EconomicValidationError as error:
            raise _structural_error(error) from error
        return specification

    def _classify_replay(
        self,
        *,
        fill: Fill,
        submitted_digest: Sha256Digest,
        fill_binding: ExistingLedgerBinding | None,
        fact_binding: ExistingLedgerBinding | None,
    ) -> LedgerApplyOutcome | None:
        if fill_binding is None and fact_binding is None:
            return None
        if fill_binding is not None and fact_binding is None:
            kind = (
                LedgerConflictKind.FILL_ID_COLLISION
                if fill_binding.fill_sha256 != submitted_digest
                else LedgerConflictKind.INDEX_INCONSISTENT
            )
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=kind,
                fill_binding=fill_binding,
            )
        if fill_binding is None and fact_binding is not None:
            kind = (
                LedgerConflictKind.FACT_KEY_COLLISION
                if fact_binding.fill_sha256 != submitted_digest
                else LedgerConflictKind.INDEX_INCONSISTENT
            )
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=kind,
                fact_binding=fact_binding,
            )
        if fill_binding is None or fact_binding is None:
            raise AssertionError("replay bindings must be jointly narrowed")
        if fill_binding.entry_id != fact_binding.entry_id:
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=LedgerConflictKind.CROSS_INDEX_COLLISION,
                fill_binding=fill_binding,
                fact_binding=fact_binding,
            )
        if fill_binding != fact_binding:
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=LedgerConflictKind.INDEX_INCONSISTENT,
                fill_binding=fill_binding,
                fact_binding=fact_binding,
            )
        transaction = self._state.entry_index.get(fill_binding.entry_id)
        if (
            transaction is None
            or type(transaction) is not LedgerTransaction
            or _binding_for(transaction) != fill_binding
        ):
            return self._conflict(
                fill=fill,
                fill_sha256=submitted_digest,
                kind=LedgerConflictKind.INDEX_INCONSISTENT,
                fill_binding=fill_binding,
                fact_binding=fact_binding,
            )
        if fill_binding.fill_sha256 == submitted_digest:
            version = self._state.snapshot.snapshot_version
            return _create_ledger_apply_outcome(
                run_id=self._run_id,
                code=OutcomeCode.LEDGER_DUPLICATE,
                before_snapshot_version=version,
                after_snapshot_version=version,
                snapshot=self._state.snapshot,
                transaction=transaction,
                submitted_fill_id=fill.fill_id,
                submitted_fill_sha256=submitted_digest,
            )
        return self._conflict(
            fill=fill,
            fill_sha256=submitted_digest,
            kind=LedgerConflictKind.FILL_AND_FACT_COLLISION,
            fill_binding=fill_binding,
            fact_binding=fact_binding,
        )

    def _conflict(
        self,
        *,
        fill: Fill,
        fill_sha256: Sha256Digest,
        kind: LedgerConflictKind,
        entry_binding: ExistingLedgerBinding | None = None,
        fill_binding: ExistingLedgerBinding | None = None,
        fact_binding: ExistingLedgerBinding | None = None,
    ) -> LedgerApplyOutcome:
        version = self._state.snapshot.snapshot_version
        return _create_ledger_apply_outcome(
            run_id=self._run_id,
            code=OutcomeCode.LEDGER_CONFLICT,
            before_snapshot_version=version,
            after_snapshot_version=version,
            snapshot=self._state.snapshot,
            transaction=None,
            submitted_fill_id=fill.fill_id,
            submitted_fill_sha256=fill_sha256,
            conflict_kind=kind,
            entry_index_binding=entry_binding,
            fill_index_binding=fill_binding,
            fact_index_binding=fact_binding,
        )

    def _failure(
        self,
        *,
        fill: Fill,
        fill_sha256: Sha256Digest,
        code: OutcomeCode,
        stage: LedgerFailureStage,
    ) -> LedgerApplyOutcome:
        version = self._state.snapshot.snapshot_version
        return _create_ledger_apply_outcome(
            run_id=self._run_id,
            code=code,
            before_snapshot_version=version,
            after_snapshot_version=version,
            snapshot=self._state.snapshot,
            transaction=None,
            submitted_fill_id=fill.fill_id,
            submitted_fill_sha256=fill_sha256,
            failure_stage=stage,
        )


def create_portfolio_ledger(
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
) -> PortfolioLedger:
    """Create one empty, run- and specification-bound ledger."""
    if type(run_id) is not RunId:
        raise PortfolioLedgerError(OutcomeCode.INVALID_TYPE, "run_id must be exact")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise PortfolioLedgerError(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    currency_quanta: dict[SettlementCurrency, CanonicalDecimal] = {}
    spec_by_instrument: dict[Instrument, InstrumentExecutionSpec] = {}
    for specification in spec_set.specifications:
        existing = currency_quanta.get(specification.settlement_currency)
        if existing is not None and existing != specification.currency_quantum:
            raise PortfolioLedgerError(
                OutcomeCode.CONFLICTING_ID,
                "one currency has conflicting settlement quanta",
            )
        currency_quanta[specification.settlement_currency] = specification.currency_quantum
        spec_by_instrument[specification.instrument] = specification
    spec_set_sha256 = instrument_spec_set_digest(spec_set)
    initial = PortfolioSnapshot(
        run_id=run_id,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=spec_set_sha256,
        snapshot_version=0,
        ledger_sequence=0,
        last_entry_id=None,
        last_transaction_sha256=None,
        cash_balances=(),
        position_balances=(),
        rounding_balances=(),
        unresolved_fills=(),
    )
    ledger = object.__new__(PortfolioLedger)
    ledger._run_id = run_id
    ledger._spec_set = spec_set
    ledger._spec_set_sha256 = spec_set_sha256
    ledger._currency_quanta = MappingProxyType(dict(currency_quanta))
    ledger._spec_by_instrument = MappingProxyType(dict(spec_by_instrument))
    ledger._state = _freeze_state(
        cash={},
        positions={},
        rounding={},
        unresolved={},
        fill_index={},
        fact_index={},
        entry_index={},
        transactions=(),
        open_reconciliation_bindings={},
        open_reconciliation_refs={},
        handoff_index={},
        authorization_index={},
        adjustment_index={},
        observation_index={},
        command_index={},
        adjustment_outcomes={},
        initial_funding_outcome=None,
        snapshot=initial,
    )
    return ledger


def _freeze_state(
    *,
    cash: Mapping[SettlementCurrency, CanonicalDecimal],
    positions: Mapping[Instrument, CanonicalDecimal],
    rounding: Mapping[SettlementCurrency, CanonicalDecimal],
    unresolved: Mapping[EconomicId, UnresolvedFillRef],
    fill_index: Mapping[EconomicId, ExistingLedgerBinding],
    fact_index: Mapping[FactDedupKey, ExistingLedgerBinding],
    entry_index: Mapping[EconomicId, CanonicalPortfolioTransaction],
    transactions: tuple[CanonicalPortfolioTransaction, ...],
    open_reconciliation_bindings: Mapping[EconomicId, ExistingLedgerBinding],
    open_reconciliation_refs: Mapping[EconomicId, OpenReconciliationRef],
    handoff_index: Mapping[Sha256Digest, _HandoffBinding],
    authorization_index: Mapping[Sha256Digest, Sha256Digest],
    adjustment_index: Mapping[EconomicId, Sha256Digest],
    observation_index: Mapping[Sha256Digest, EconomicId],
    command_index: Mapping[Sha256Digest, ReconciliationAdjustmentOutcome],
    adjustment_outcomes: Mapping[Sha256Digest, ReconciliationAdjustmentOutcome],
    initial_funding_outcome: InitialFundingOutcome | None,
    snapshot: PortfolioSnapshot,
) -> _LedgerState:
    return _LedgerState(
        cash=MappingProxyType(dict(cash)),
        positions=MappingProxyType(dict(positions)),
        rounding=MappingProxyType(dict(rounding)),
        unresolved=MappingProxyType(dict(unresolved)),
        fill_index=MappingProxyType(dict(fill_index)),
        fact_index=MappingProxyType(dict(fact_index)),
        entry_index=MappingProxyType(dict(entry_index)),
        transactions=transactions,
        open_reconciliation_bindings=MappingProxyType(dict(open_reconciliation_bindings)),
        open_reconciliation_refs=MappingProxyType(dict(open_reconciliation_refs)),
        handoff_index=MappingProxyType(dict(handoff_index)),
        authorization_index=MappingProxyType(dict(authorization_index)),
        adjustment_index=MappingProxyType(dict(adjustment_index)),
        observation_index=MappingProxyType(dict(observation_index)),
        command_index=MappingProxyType(dict(command_index)),
        adjustment_outcomes=MappingProxyType(dict(adjustment_outcomes)),
        initial_funding_outcome=initial_funding_outcome,
        snapshot=snapshot,
    )


def _preflight_canonical_evidence(
    *,
    transaction: LedgerTransaction,
    snapshot: PortfolioSnapshot,
    outcome: LedgerApplyOutcome,
) -> None:
    canonical_ledger_transaction_bytes(transaction)
    ledger_transaction_digest(transaction)
    canonical_portfolio_snapshot_bytes(snapshot)
    portfolio_snapshot_digest(snapshot)
    canonical_ledger_apply_outcome_bytes(outcome)
    ledger_apply_outcome_digest(outcome)


def _snapshot(
    *,
    ledger: PortfolioLedger,
    sequence: int,
    last_entry_id: EconomicId,
    last_transaction_sha256: Sha256Digest,
    cash: Mapping[SettlementCurrency, CanonicalDecimal],
    positions: Mapping[Instrument, CanonicalDecimal],
    rounding: Mapping[SettlementCurrency, CanonicalDecimal],
    unresolved: Mapping[EconomicId, UnresolvedFillRef],
    open_reconciliation_bindings: Mapping[EconomicId, ExistingLedgerBinding],
    open_reconciliation_refs: Mapping[EconomicId, OpenReconciliationRef],
) -> PortfolioSnapshot:
    cash_balances = tuple(
        CashBalance(
            currency=currency,
            currency_quantum=ledger._currency_quanta[currency],
            amount=amount,
        )
        for currency, amount in sorted(cash.items(), key=lambda item: item[0].code)
    )
    position_balances = tuple(
        PositionBalance(
            instrument=instrument,
            quantity_quantum=ledger._spec_by_instrument[instrument].quantity_quantum,
            quantity=quantity,
        )
        for instrument, quantity in sorted(
            positions.items(),
            key=lambda item: item[0].key,
        )
    )
    rounding_balances = tuple(
        RoundingBalance(currency=currency, amount=amount)
        for currency, amount in sorted(rounding.items(), key=lambda item: item[0].code)
    )
    unresolved_fills = tuple(
        unresolved[key]
        for key in sorted(
            unresolved,
            key=lambda identity: (
                identity.run_id.value,
                identity.owner_kind.value,
                identity.owner_sequence,
            ),
        )
    )
    reconciliation_bindings = tuple(
        open_reconciliation_bindings[key]
        for key in sorted(
            open_reconciliation_bindings,
            key=lambda identity: (
                identity.run_id.value,
                identity.owner_kind.value,
                identity.owner_sequence,
            ),
        )
    )
    reconciliation_refs = tuple(
        open_reconciliation_refs[key]
        for key in sorted(
            open_reconciliation_refs,
            key=lambda identity: (
                identity.run_id.value,
                identity.owner_kind.value,
                identity.owner_sequence,
            ),
        )
    )
    return PortfolioSnapshot(
        run_id=ledger._run_id,
        instrument_spec_set_id=ledger._spec_set.identifier,
        instrument_spec_set_sha256=ledger._spec_set_sha256,
        snapshot_version=sequence,
        ledger_sequence=sequence,
        last_entry_id=last_entry_id,
        last_transaction_sha256=last_transaction_sha256,
        cash_balances=cash_balances,
        position_balances=position_balances,
        rounding_balances=rounding_balances,
        unresolved_fills=unresolved_fills,
        open_reconciliation_bindings=reconciliation_bindings,
        open_reconciliation_refs=reconciliation_refs,
    )


def _postings(
    *,
    instrument: Instrument,
    currency: SettlementCurrency,
    position_delta: CanonicalDecimal,
    cash_delta: CanonicalDecimal,
    external_notional: CanonicalDecimal,
    rounding_delta: CanonicalDecimal,
) -> tuple[LedgerPosting, ...]:
    instrument_commodity = InstrumentCommodity(instrument)
    currency_commodity = CurrencyCommodity(currency)
    values = [
        LedgerPosting(
            LedgerAccountKind.PORTFOLIO_POSITION,
            instrument_commodity,
            position_delta,
        ),
        LedgerPosting(
            LedgerAccountKind.EXTERNAL_INVENTORY,
            instrument_commodity,
            _signed(position_delta, -1),
        ),
    ]
    if cash_delta.coefficient != 0:
        values.append(
            LedgerPosting(
                LedgerAccountKind.PORTFOLIO_CASH,
                currency_commodity,
                cash_delta,
            )
        )
    if external_notional.coefficient != 0:
        values.append(
            LedgerPosting(
                LedgerAccountKind.EXTERNAL_SETTLEMENT,
                currency_commodity,
                external_notional,
            )
        )
    if rounding_delta.coefficient != 0:
        values.append(
            LedgerPosting(
                LedgerAccountKind.PORTFOLIO_ROUNDING,
                currency_commodity,
                rounding_delta,
            )
        )
    return tuple(values)


def _postings_balance(postings: tuple[LedgerPosting, ...]) -> bool:
    sums: dict[object, tuple[int, int]] = {}
    for posting in postings:
        existing = sums.get(posting.commodity)
        if existing is None:
            sums[posting.commodity] = (posting.amount.coefficient, posting.amount.scale)
        else:
            left_coefficient, left_scale = existing
            scale = max(left_scale, posting.amount.scale)
            coefficient = left_coefficient * (10 ** (scale - left_scale))
            coefficient += posting.amount.coefficient * (10 ** (scale - posting.amount.scale))
            sums[posting.commodity] = (coefficient, scale)
    return all(coefficient == 0 for coefficient, _ in sums.values())


def _transaction_digest(transaction: CanonicalPortfolioTransaction) -> Sha256Digest:
    if type(transaction) is InitialFundingTransaction:
        return initial_funding_transaction_digest(transaction)
    if type(transaction) is LedgerTransaction:
        return ledger_transaction_digest(transaction)
    if type(transaction) is ReconciliationTransaction:
        return reconciliation_transaction_digest(transaction)
    raise AssertionError("canonical portfolio transaction is outside the closed union")


def _binding_for(transaction: LedgerTransaction) -> ExistingLedgerBinding:
    return ExistingLedgerBinding(
        entry_id=transaction.entry_id,
        fill_id=transaction.fill_id,
        fill_sha256=transaction.fill_sha256,
        transaction_sha256=ledger_transaction_digest(transaction),
    )


def _assign_nonzero[Key](
    mapping: dict[Key, CanonicalDecimal],
    key: Key,
    value: CanonicalDecimal,
) -> None:
    if value.coefficient == 0:
        mapping.pop(key, None)
    else:
        mapping[key] = value


def _next_balance(
    current: CanonicalDecimal | None,
    delta: CanonicalDecimal,
) -> CanonicalDecimal:
    return delta if current is None else _add_decimal(current, delta)


def _add_decimal(left: CanonicalDecimal, right: CanonicalDecimal) -> CanonicalDecimal:
    scale = max(left.scale, right.scale)
    coefficient = left.coefficient * (10 ** (scale - left.scale))
    coefficient += right.coefficient * (10 ** (scale - right.scale))
    return CanonicalDecimal(_scaled_text(coefficient, scale))


def _signed(value: CanonicalDecimal, sign: int) -> CanonicalDecimal:
    if sign not in (-1, 1):
        raise AssertionError("internal decimal sign must be -1 or 1")
    return CanonicalDecimal(_scaled_text(value.coefficient * sign, value.scale))


def _scaled_text(coefficient: int, scale: int) -> str:
    if coefficient == 0:
        return "0"
    while scale > 0 and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return sign + digits
    if len(digits) <= scale:
        digits = ("0" * (scale + 1 - len(digits))) + digits
    split = len(digits) - scale
    return f"{sign}{digits[:split]}.{digits[split:]}"


def _structural_error(error: EconomicValidationError) -> PortfolioLedgerError:
    if error.code not in {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.CONFLICTING_ID,
    }:
        raise AssertionError("unexpected economic result code at structural boundary")
    return PortfolioLedgerError(error.code, str(error))


@dataclass(frozen=True, slots=True)
class _AdjustmentDerivation:
    transaction: ReconciliationTransaction
    next_cash: Mapping[SettlementCurrency, CanonicalDecimal]
    next_positions: Mapping[Instrument, CanonicalDecimal]
    next_unresolved: Mapping[EconomicId, UnresolvedFillRef]
    next_open_bindings: Mapping[EconomicId, ExistingLedgerBinding]
    next_open_refs: Mapping[EconomicId, OpenReconciliationRef]
    next_entry_index: Mapping[EconomicId, CanonicalPortfolioTransaction]
    next_transactions: tuple[CanonicalPortfolioTransaction, ...]
    next_snapshot: PortfolioSnapshot


def _negated_decimal_value(value: CanonicalDecimal) -> CanonicalDecimal:
    if value.coefficient == 0:
        return CanonicalDecimal("0")
    if value.text.startswith("-"):
        return CanonicalDecimal(value.text[1:])
    return CanonicalDecimal(f"-{value.text}")


def _correction_failure_kind(error: PortfolioLedgerError) -> ReconciliationAdjustmentFailureKind:
    if error.code in {OutcomeCode.ARITHMETIC_OVERFLOW, OutcomeCode.ROUNDING_UNREPRESENTABLE}:
        return ReconciliationAdjustmentFailureKind.ARITHMETIC_FAILURE
    if error.code is OutcomeCode.LEDGER_UNBALANCED:
        return ReconciliationAdjustmentFailureKind.UNBALANCED
    return ReconciliationAdjustmentFailureKind.INVALID_COMMAND


def _preflight_adjustment_evidence(
    derived: _AdjustmentDerivation,
    outcome: ReconciliationAdjustmentOutcome,
) -> None:
    canonical_reconciliation_transaction_bytes(derived.transaction)
    reconciliation_transaction_digest(derived.transaction)
    canonical_portfolio_snapshot_bytes(derived.next_snapshot)
    portfolio_snapshot_digest(derived.next_snapshot)
    canonical_reconciliation_adjustment_outcome_bytes(outcome)
    reconciliation_adjustment_outcome_digest(outcome)
