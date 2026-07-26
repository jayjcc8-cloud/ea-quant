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
from ea.core.run import RunId, Sha256Digest

_MAX_UINT64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class _LedgerState:
    cash: Mapping[SettlementCurrency, CanonicalDecimal]
    positions: Mapping[Instrument, CanonicalDecimal]
    rounding: Mapping[SettlementCurrency, CanonicalDecimal]
    unresolved: Mapping[EconomicId, UnresolvedFillRef]
    fill_index: Mapping[EconomicId, ExistingLedgerBinding]
    fact_index: Mapping[FactDedupKey, ExistingLedgerBinding]
    entry_index: Mapping[EconomicId, LedgerTransaction]
    transactions: tuple[LedgerTransaction, ...]
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
    def transactions(self) -> tuple[LedgerTransaction, ...]:
        return self._state.transactions

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

        requires_reconciliation = (
            fill.order_id is None or fill.correlation_id is None or fill.causation_id is None
        )
        next_unresolved = dict(self._state.unresolved)
        if requires_reconciliation:
            next_unresolved[fill.fill_id] = UnresolvedFillRef(
                fill_id=fill.fill_id,
                fill_sha256=submitted_digest,
            )

        previous_digest = (
            None
            if not self._state.transactions
            else ledger_transaction_digest(self._state.transactions[-1])
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
        next_snapshot = _snapshot(
            ledger=self,
            sequence=next_sequence,
            last_entry_id=entry_id,
            last_transaction_sha256=transaction_sha256,
            cash=next_cash_map,
            positions=next_position_map,
            rounding=next_rounding_map,
            unresolved=next_unresolved,
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
        next_state = _freeze_state(
            cash=next_cash_map,
            positions=next_position_map,
            rounding=next_rounding_map,
            unresolved=next_unresolved,
            fill_index=next_fill_index,
            fact_index=next_fact_index,
            entry_index=next_entry_index,
            transactions=next_transactions,
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
        if transaction is None or _binding_for(transaction) != fill_binding:
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
    entry_index: Mapping[EconomicId, LedgerTransaction],
    transactions: tuple[LedgerTransaction, ...],
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
