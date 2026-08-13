"""Canonical portfolio-ledger values from Accepted ADR 0010."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import (
    InstrumentSpecId,
    InstrumentSpecSetId,
    SettlementCurrency,
)
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    ExternalFactId,
    FactDedupKey,
    SourceNativeSequence,
)
from ea.core.execution_messages import FactProvenance, VenueOrderId
from ea.core.identity import Instrument
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

LEDGER_TRANSACTION_SCHEMA_VERSION = 1
LEDGER_TRANSACTION_CANONICALIZATION = "ea-ledger-transaction-v1"
LEDGER_TRANSACTION_DIGEST_DOMAIN = b"ea.ledger-transaction.v1\0"

PORTFOLIO_SNAPSHOT_SCHEMA_VERSION = 2
PORTFOLIO_SNAPSHOT_CANONICALIZATION = "ea-portfolio-snapshot-v2"
PORTFOLIO_SNAPSHOT_DIGEST_DOMAIN = b"ea.portfolio-snapshot.v2\0"

LEDGER_APPLY_OUTCOME_SCHEMA_VERSION = 1
LEDGER_APPLY_OUTCOME_CANONICALIZATION = "ea-ledger-apply-outcome-v1"
LEDGER_APPLY_OUTCOME_DIGEST_DOMAIN = b"ea.ledger-apply-outcome.v1\0"

_LEDGER_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.CONFLICTING_ID,
    }
)
_LEDGER_RESULT_CODES = frozenset(
    {
        OutcomeCode.LEDGER_APPLIED,
        OutcomeCode.LEDGER_DUPLICATE,
        OutcomeCode.LEDGER_CONFLICT,
        OutcomeCode.LEDGER_UNBALANCED,
        OutcomeCode.ROUNDING_UNREPRESENTABLE,
        OutcomeCode.ARITHMETIC_OVERFLOW,
        OutcomeCode.OUT_OF_RANGE,
    }
)


class PortfolioLedgerError(ValueError):
    """Structured validation failure at the canonical ledger boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _LEDGER_ERROR_CODES:
            raise TypeError("portfolio ledger errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> PortfolioLedgerError:
    return PortfolioLedgerError(code, message)


def _translate_economic_error(error: EconomicValidationError) -> PortfolioLedgerError:
    if error.code not in _LEDGER_ERROR_CODES:
        raise AssertionError("economic result code cannot be translated to a structural error")
    return _fail(error.code, str(error))


class LedgerAccountKind(StrEnum):
    """Closed accounts for Fill-derived Phase 1 entries."""

    PORTFOLIO_POSITION = "portfolio.position"
    EXTERNAL_INVENTORY = "external.inventory"
    PORTFOLIO_CASH = "portfolio.cash"
    EXTERNAL_SETTLEMENT = "external.settlement"
    PORTFOLIO_ROUNDING = "portfolio.rounding"


class LedgerConflictKind(StrEnum):
    """Closed replay and internal-index conflicts."""

    FILL_ID_COLLISION = "fill_id_collision"
    FACT_KEY_COLLISION = "fact_key_collision"
    FILL_AND_FACT_COLLISION = "fill_and_fact_collision"
    CROSS_INDEX_COLLISION = "cross_index_collision"
    INDEX_INCONSISTENT = "index_inconsistent"
    ENTRY_ID_OCCUPIED = "entry_id_occupied"


class LedgerFailureStage(StrEnum):
    """Closed ordered failure stages from Accepted ADR 0010."""

    LEDGER_SEQUENCE_EXHAUSTED = "ledger_sequence_exhausted"
    SETTLEMENT_ARITHMETIC_OVERFLOW = "settlement_arithmetic_overflow"
    SETTLEMENT_ROUNDING_UNREPRESENTABLE = "settlement_rounding_unrepresentable"
    EXACT_NOTIONAL_OVERFLOW = "exact_notional_overflow"
    CASH_BALANCE_OVERFLOW = "cash_balance_overflow"
    POSITION_BALANCE_OVERFLOW = "position_balance_overflow"
    ROUNDING_BALANCE_OVERFLOW = "rounding_balance_overflow"
    COMMODITY_UNBALANCED = "commodity_unbalanced"


@final
@dataclass(frozen=True, slots=True)
class InstrumentCommodity:
    instrument: Instrument

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "instrument commodity requires an exact Instrument",
            )


@final
@dataclass(frozen=True, slots=True)
class CurrencyCommodity:
    currency: SettlementCurrency

    def __post_init__(self) -> None:
        if type(self.currency) is not SettlementCurrency:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "currency commodity requires an exact SettlementCurrency",
            )


type LedgerCommodity = InstrumentCommodity | CurrencyCommodity


@final
@dataclass(frozen=True, slots=True)
class LedgerPosting:
    account: LedgerAccountKind
    commodity: LedgerCommodity
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.account) is not LedgerAccountKind:
            raise _fail(OutcomeCode.INVALID_TYPE, "ledger account must be exact")
        if type(self.commodity) not in (InstrumentCommodity, CurrencyCommodity):
            raise _fail(OutcomeCode.INVALID_TYPE, "ledger commodity must be exact")
        if type(self.amount) is not CanonicalDecimal:
            raise _fail(OutcomeCode.INVALID_TYPE, "ledger amount must be exact")
        if self.amount.coefficient == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "zero ledger postings are forbidden")
        instrument_account = self.account in (
            LedgerAccountKind.PORTFOLIO_POSITION,
            LedgerAccountKind.EXTERNAL_INVENTORY,
        )
        if instrument_account != (type(self.commodity) is InstrumentCommodity):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "ledger account and commodity kinds conflict",
            )


@final
@dataclass(frozen=True, slots=True)
class CashBalance:
    currency: SettlementCurrency
    currency_quantum: CanonicalDecimal
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.currency) is not SettlementCurrency:
            raise _fail(OutcomeCode.INVALID_TYPE, "cash currency must be exact")
        try:
            require_positive(self.currency_quantum, field_name="currency_quantum")
            require_quantized(self.amount, self.currency_quantum, field_name="cash_amount")
        except EconomicValidationError as error:
            raise _translate_economic_error(error) from error
        if self.amount.coefficient == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "zero cash balances are omitted")


@final
@dataclass(frozen=True, slots=True)
class PositionBalance:
    instrument: Instrument
    quantity_quantum: CanonicalDecimal
    quantity: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise _fail(OutcomeCode.INVALID_TYPE, "position instrument must be exact")
        try:
            require_positive(self.quantity_quantum, field_name="quantity_quantum")
            require_quantized(self.quantity, self.quantity_quantum, field_name="position_quantity")
        except EconomicValidationError as error:
            raise _translate_economic_error(error) from error
        if self.quantity.coefficient == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "zero position balances are omitted")


@final
@dataclass(frozen=True, slots=True)
class RoundingBalance:
    currency: SettlementCurrency
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.currency) is not SettlementCurrency:
            raise _fail(OutcomeCode.INVALID_TYPE, "rounding currency must be exact")
        if type(self.amount) is not CanonicalDecimal:
            raise _fail(OutcomeCode.INVALID_TYPE, "rounding amount must be exact")
        if self.amount.coefficient == 0:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "zero rounding balances are omitted")


@final
@dataclass(frozen=True, slots=True)
class UnresolvedFillRef:
    fill_id: EconomicId
    fill_sha256: Sha256Digest

    def __post_init__(self) -> None:
        _require_economic_id(
            self.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            field_name="fill_id",
        )
        if type(self.fill_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "fill_sha256 must be exact")


@final
@dataclass(frozen=True, slots=True)
class OpenReconciliationRef:
    """Outcome-bound reconciliation evidence retained for one applied Fill."""

    fill_id: EconomicId
    fill_sha256: Sha256Digest
    processing_outcome_sha256: Sha256Digest

    def __post_init__(self) -> None:
        _require_economic_id(
            self.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            field_name="fill_id",
        )
        if (
            type(self.fill_sha256) is not Sha256Digest
            or type(self.processing_outcome_sha256) is not Sha256Digest
        ):
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "open reconciliation reference digests must be exact",
            )


@final
@dataclass(frozen=True, slots=True)
class ExistingLedgerBinding:
    entry_id: EconomicId
    fill_id: EconomicId
    fill_sha256: Sha256Digest
    transaction_sha256: Sha256Digest

    def __post_init__(self) -> None:
        _require_economic_id(
            self.entry_id,
            owner=EconomicOwnerKind.LEDGER_ENTRY,
            field_name="entry_id",
        )
        _require_economic_id(
            self.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            field_name="fill_id",
        )
        if self.entry_id.run_id != self.fill_id.run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "ledger binding runs conflict")
        if (
            type(self.fill_sha256) is not Sha256Digest
            or type(self.transaction_sha256) is not Sha256Digest
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "ledger binding digests must be exact")


_POSTING_ORDER = {
    LedgerAccountKind.PORTFOLIO_POSITION: 0,
    LedgerAccountKind.EXTERNAL_INVENTORY: 1,
    LedgerAccountKind.PORTFOLIO_CASH: 2,
    LedgerAccountKind.EXTERNAL_SETTLEMENT: 3,
    LedgerAccountKind.PORTFOLIO_ROUNDING: 4,
}


@final
@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    run_id: RunId
    entry_id: EconomicId
    ledger_sequence: int
    fill_id: EconomicId
    fill_sha256: Sha256Digest
    fact_key: FactDedupKey
    provenance: FactProvenance
    occurred_at: datetime
    order_id: EconomicId | None
    correlation_id: EconomicId | None
    causation_id: EconomicId | None
    client_submission_key: Sha256Digest | None
    venue_order_id: VenueOrderId | None
    instrument_specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    previous_transaction_sha256: Sha256Digest | None
    postings: tuple[LedgerPosting, ...]
    requires_reconciliation: bool

    def __post_init__(self) -> None:
        _require_run(self.run_id)
        _require_economic_id(
            self.entry_id,
            owner=EconomicOwnerKind.LEDGER_ENTRY,
            run_id=self.run_id,
            field_name="entry_id",
        )
        if type(self.ledger_sequence) is not int or not 1 <= self.ledger_sequence < 1 << 64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "ledger_sequence must be uint64 and positive")
        if self.entry_id.owner_sequence != self.ledger_sequence:
            raise _fail(OutcomeCode.CONFLICTING_ID, "entry identity and sequence conflict")
        _require_economic_id(
            self.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            run_id=self.run_id,
            field_name="fill_id",
        )
        if type(self.fill_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "fill_sha256 must be exact")
        if type(self.fact_key) is not FactDedupKey:
            raise _fail(OutcomeCode.INVALID_TYPE, "fact_key must be exact")
        if type(self.provenance) is not FactProvenance:
            raise _fail(OutcomeCode.INVALID_TYPE, "provenance must be exact")
        _require_time(self.occurred_at)
        for field_name, identity in (
            ("order_id", self.order_id),
            ("correlation_id", self.correlation_id),
            ("causation_id", self.causation_id),
        ):
            if identity is not None:
                _require_economic_id(identity, run_id=self.run_id, field_name=field_name)
        if self.client_submission_key is not None and (
            type(self.client_submission_key) is not Sha256Digest
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "client_submission_key must be exact or None")
        if self.venue_order_id is not None and type(self.venue_order_id) is not VenueOrderId:
            raise _fail(OutcomeCode.INVALID_TYPE, "venue_order_id must be exact or None")
        if type(self.instrument_specification_id) is not InstrumentSpecId:
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument specification ID must be exact")
        if type(self.instrument_spec_set_id) is not InstrumentSpecSetId:
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument spec-set ID must be exact")
        if type(self.instrument_spec_set_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument spec-set digest must be exact")
        if self.ledger_sequence == 1:
            if self.previous_transaction_sha256 is not None:
                raise _fail(OutcomeCode.CONFLICTING_ID, "first entry cannot name a predecessor")
        elif type(self.previous_transaction_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "later entry requires an exact predecessor")
        _require_postings(self.postings)
        if type(self.requires_reconciliation) is not bool:
            raise _fail(OutcomeCode.INVALID_TYPE, "requires_reconciliation must be exact bool")
        expected_reconciliation = (
            self.order_id is None or self.correlation_id is None or self.causation_id is None
        )
        if self.requires_reconciliation is not expected_reconciliation:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "requires_reconciliation conflicts with Fill ancestry",
            )


@final
@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    run_id: RunId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    snapshot_version: int
    ledger_sequence: int
    last_entry_id: EconomicId | None
    last_transaction_sha256: Sha256Digest | None
    cash_balances: tuple[CashBalance, ...]
    position_balances: tuple[PositionBalance, ...]
    rounding_balances: tuple[RoundingBalance, ...]
    unresolved_fills: tuple[UnresolvedFillRef, ...]
    open_reconciliation_bindings: tuple[ExistingLedgerBinding, ...] = ()
    open_reconciliation_refs: tuple[OpenReconciliationRef, ...] = ()

    def __post_init__(self) -> None:
        _require_run(self.run_id)
        if type(self.instrument_spec_set_id) is not InstrumentSpecSetId:
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument spec-set ID must be exact")
        if type(self.instrument_spec_set_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument spec-set digest must be exact")
        _require_version(self.snapshot_version, field_name="snapshot_version")
        _require_version(self.ledger_sequence, field_name="ledger_sequence")
        if self.snapshot_version != self.ledger_sequence:
            raise _fail(OutcomeCode.CONFLICTING_ID, "snapshot version and sequence conflict")
        _require_snapshot_tuples(self)
        if self.ledger_sequence == 0:
            if self.last_entry_id is not None or self.last_transaction_sha256 is not None:
                raise _fail(OutcomeCode.CONFLICTING_ID, "version zero cannot name a last entry")
            if any(
                (
                    self.cash_balances,
                    self.position_balances,
                    self.rounding_balances,
                    self.unresolved_fills,
                    self.open_reconciliation_bindings,
                    self.open_reconciliation_refs,
                )
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "version zero balances must be empty")
        else:
            last_entry = _require_economic_id(
                self.last_entry_id,
                owner=EconomicOwnerKind.LEDGER_ENTRY,
                run_id=self.run_id,
                field_name="last_entry_id",
            )
            if last_entry.owner_sequence != self.ledger_sequence:
                raise _fail(OutcomeCode.CONFLICTING_ID, "last entry and sequence conflict")
            if type(self.last_transaction_sha256) is not Sha256Digest:
                raise _fail(OutcomeCode.INVALID_TYPE, "last transaction digest must be exact")


@final
@dataclass(frozen=True, slots=True, init=False)
class LedgerApplyOutcome:
    run_id: RunId
    code: OutcomeCode
    before_snapshot_version: int
    after_snapshot_version: int
    snapshot: PortfolioSnapshot
    transaction: LedgerTransaction | None
    submitted_fill_id: EconomicId
    submitted_fill_sha256: Sha256Digest
    conflict_kind: LedgerConflictKind | None
    entry_index_binding: ExistingLedgerBinding | None
    fill_index_binding: ExistingLedgerBinding | None
    fact_index_binding: ExistingLedgerBinding | None
    failure_stage: LedgerFailureStage | None

    def __init__(self) -> None:
        raise TypeError("LedgerApplyOutcome values are created only by the portfolio ledger")

    @property
    def snapshot_sha256(self) -> Sha256Digest:
        return portfolio_snapshot_digest(self.snapshot)

    @property
    def transaction_entry_id(self) -> EconomicId | None:
        return None if self.transaction is None else self.transaction.entry_id

    @property
    def transaction_sha256(self) -> Sha256Digest | None:
        return None if self.transaction is None else ledger_transaction_digest(self.transaction)


def canonical_ledger_transaction_bytes(transaction: LedgerTransaction) -> bytes:
    """Return the exact canonical transaction document."""
    if type(transaction) is not LedgerTransaction:
        raise _fail(OutcomeCode.INVALID_TYPE, "transaction must be exact")
    return _encode_json(_transaction_document(transaction))


def ledger_transaction_digest(transaction: LedgerTransaction) -> Sha256Digest:
    return _digest(
        LEDGER_TRANSACTION_DIGEST_DOMAIN,
        canonical_ledger_transaction_bytes(transaction),
    )


def canonical_portfolio_snapshot_bytes(snapshot: PortfolioSnapshot) -> bytes:
    """Return the exact canonical snapshot document."""
    if type(snapshot) is not PortfolioSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot must be exact")
    return _encode_json(_snapshot_document(snapshot))


def portfolio_snapshot_digest(snapshot: PortfolioSnapshot) -> Sha256Digest:
    return _digest(
        PORTFOLIO_SNAPSHOT_DIGEST_DOMAIN,
        canonical_portfolio_snapshot_bytes(snapshot),
    )


def canonical_ledger_apply_outcome_bytes(outcome: LedgerApplyOutcome) -> bytes:
    """Return the exact canonical ledger-application result document."""
    if type(outcome) is not LedgerApplyOutcome:
        raise _fail(OutcomeCode.INVALID_TYPE, "outcome must be exact")
    return _encode_json(_outcome_document(outcome))


def ledger_apply_outcome_digest(outcome: LedgerApplyOutcome) -> Sha256Digest:
    return _digest(
        LEDGER_APPLY_OUTCOME_DIGEST_DOMAIN,
        canonical_ledger_apply_outcome_bytes(outcome),
    )


def _create_ledger_apply_outcome(
    *,
    run_id: RunId,
    code: OutcomeCode,
    before_snapshot_version: int,
    after_snapshot_version: int,
    snapshot: PortfolioSnapshot,
    transaction: LedgerTransaction | None,
    submitted_fill_id: EconomicId,
    submitted_fill_sha256: Sha256Digest,
    conflict_kind: LedgerConflictKind | None = None,
    entry_index_binding: ExistingLedgerBinding | None = None,
    fill_index_binding: ExistingLedgerBinding | None = None,
    fact_index_binding: ExistingLedgerBinding | None = None,
    failure_stage: LedgerFailureStage | None = None,
) -> LedgerApplyOutcome:
    values = (
        (run_id, RunId),
        (code, OutcomeCode),
        (before_snapshot_version, int),
        (after_snapshot_version, int),
        (snapshot, PortfolioSnapshot),
        (submitted_fill_id, EconomicId),
        (submitted_fill_sha256, Sha256Digest),
    )
    if any(type(value) is not expected for value, expected in values):
        raise AssertionError("internal ledger outcome inputs must be canonical")
    if code not in _LEDGER_RESULT_CODES:
        raise AssertionError("internal ledger outcome code is not permitted")
    if transaction is not None and type(transaction) is not LedgerTransaction:
        raise AssertionError("internal transaction must be canonical")
    for binding in (entry_index_binding, fill_index_binding, fact_index_binding):
        if binding is not None and type(binding) is not ExistingLedgerBinding:
            raise AssertionError("internal replay binding must be canonical")
    if conflict_kind is not None and type(conflict_kind) is not LedgerConflictKind:
        raise AssertionError("internal conflict kind must be canonical")
    if failure_stage is not None and type(failure_stage) is not LedgerFailureStage:
        raise AssertionError("internal failure stage must be canonical")
    if snapshot.run_id != run_id or submitted_fill_id.run_id != run_id:
        raise AssertionError("internal outcome run binding must be consistent")
    _require_version(before_snapshot_version, field_name="before_snapshot_version")
    _require_version(after_snapshot_version, field_name="after_snapshot_version")
    if snapshot.snapshot_version != after_snapshot_version:
        raise AssertionError("internal outcome snapshot version must match")

    is_applied = code is OutcomeCode.LEDGER_APPLIED
    is_duplicate = code is OutcomeCode.LEDGER_DUPLICATE
    is_conflict = code is OutcomeCode.LEDGER_CONFLICT
    if is_applied:
        if transaction is None or after_snapshot_version != before_snapshot_version + 1:
            raise AssertionError("applied outcome fields are inconsistent")
    elif is_duplicate:
        if transaction is None or after_snapshot_version != before_snapshot_version:
            raise AssertionError("duplicate outcome fields are inconsistent")
    elif after_snapshot_version != before_snapshot_version or transaction is not None:
        raise AssertionError("non-success outcome fields are inconsistent")
    if is_conflict:
        if conflict_kind is None or failure_stage is not None:
            raise AssertionError("conflict outcome evidence is inconsistent")
    elif (
        any(
            binding is not None
            for binding in (entry_index_binding, fill_index_binding, fact_index_binding)
        )
        or conflict_kind is not None
    ):
        raise AssertionError("non-conflict outcome cannot carry replay bindings")
    is_failure = not (is_applied or is_duplicate or is_conflict)
    if is_failure != (failure_stage is not None):
        raise AssertionError("failure outcome stage is inconsistent")

    result = object.__new__(LedgerApplyOutcome)
    for name, value in (
        ("run_id", run_id),
        ("code", code),
        ("before_snapshot_version", before_snapshot_version),
        ("after_snapshot_version", after_snapshot_version),
        ("snapshot", snapshot),
        ("transaction", transaction),
        ("submitted_fill_id", submitted_fill_id),
        ("submitted_fill_sha256", submitted_fill_sha256),
        ("conflict_kind", conflict_kind),
        ("entry_index_binding", entry_index_binding),
        ("fill_index_binding", fill_index_binding),
        ("fact_index_binding", fact_index_binding),
        ("failure_stage", failure_stage),
    ):
        object.__setattr__(result, name, value)
    return result


def _require_run(run_id: object) -> RunId:
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be exact")
    return run_id


def _require_economic_id(
    identity: object,
    *,
    owner: EconomicOwnerKind | None = None,
    run_id: RunId | None = None,
    field_name: str,
) -> EconomicId:
    if type(identity) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an exact EconomicId")
    if owner is not None and identity.owner_kind is not owner:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} owner kind conflicts")
    if run_id is not None and identity.run_id != run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} run conflicts")
    return identity


def _require_time(value: object) -> datetime:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, "occurred_at must be an exact datetime")
    try:
        return require_utc(value, field="occurred_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _require_version(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact int")
    if value < 0 or value >= 1 << 64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be uint64")
    return value


def _require_postings(postings: object) -> tuple[LedgerPosting, ...]:
    if type(postings) is not tuple or any(type(item) is not LedgerPosting for item in postings):
        raise _fail(OutcomeCode.INVALID_TYPE, "postings must be an exact LedgerPosting tuple")
    if len(postings) < 2 or len(postings) > 5:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "ledger transaction requires 2..5 postings")
    order = tuple(_POSTING_ORDER[posting.account] for posting in postings)
    if order != tuple(sorted(order)) or len(order) != len(set(order)):
        raise _fail(OutcomeCode.CONFLICTING_ID, "posting order or account uniqueness conflicts")
    if order[:2] != (0, 1):
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument posting pair is required")
    first = postings[0]
    second = postings[1]
    if (
        first.commodity != second.commodity
        or first.amount.scale != second.amount.scale
        or first.amount.coefficient != -second.amount.coefficient
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument posting pair is not balanced")
    currency_commodities = tuple(
        posting.commodity
        for posting in postings[2:]
        if type(posting.commodity) is CurrencyCommodity
    )
    if currency_commodities and any(
        commodity != currency_commodities[0] for commodity in currency_commodities[1:]
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "currency postings use conflicting commodities")
    if not _postings_sum_to_zero(postings):
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger postings are not commodity-balanced")
    return postings


def _postings_sum_to_zero(postings: tuple[LedgerPosting, ...]) -> bool:
    sums: dict[LedgerCommodity, tuple[int, int]] = {}
    for posting in postings:
        existing = sums.get(posting.commodity)
        if existing is None:
            sums[posting.commodity] = (posting.amount.coefficient, posting.amount.scale)
            continue
        coefficient, scale = existing
        target_scale = max(scale, posting.amount.scale)
        total = coefficient * (10 ** (target_scale - scale))
        total += posting.amount.coefficient * (10 ** (target_scale - posting.amount.scale))
        sums[posting.commodity] = (total, target_scale)
    return all(coefficient == 0 for coefficient, _ in sums.values())


def _require_snapshot_tuples(snapshot: PortfolioSnapshot) -> None:
    tuples_and_types = (
        (snapshot.cash_balances, CashBalance, "cash_balances"),
        (snapshot.position_balances, PositionBalance, "position_balances"),
        (snapshot.rounding_balances, RoundingBalance, "rounding_balances"),
        (snapshot.unresolved_fills, UnresolvedFillRef, "unresolved_fills"),
        (
            snapshot.open_reconciliation_bindings,
            ExistingLedgerBinding,
            "open_reconciliation_bindings",
        ),
        (
            snapshot.open_reconciliation_refs,
            OpenReconciliationRef,
            "open_reconciliation_refs",
        ),
    )
    for values, expected, field_name in tuples_and_types:
        if type(values) is not tuple or any(type(value) is not expected for value in values):
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an exact tuple")
    cash_keys = tuple(value.currency.code for value in snapshot.cash_balances)
    position_keys = tuple(value.instrument.key for value in snapshot.position_balances)
    rounding_keys = tuple(value.currency.code for value in snapshot.rounding_balances)
    unresolved_keys = tuple(_economic_id_key(value.fill_id) for value in snapshot.unresolved_fills)
    reconciliation_binding_keys = tuple(
        _economic_id_key(value.fill_id) for value in snapshot.open_reconciliation_bindings
    )
    reconciliation_keys = tuple(
        _economic_id_key(value.fill_id) for value in snapshot.open_reconciliation_refs
    )
    for keys, field_name in (
        (cash_keys, "cash_balances"),
        (position_keys, "position_balances"),
        (rounding_keys, "rounding_balances"),
        (unresolved_keys, "unresolved_fills"),
        (reconciliation_binding_keys, "open_reconciliation_bindings"),
        (reconciliation_keys, "open_reconciliation_refs"),
    ):
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise _fail(OutcomeCode.CONFLICTING_ID, f"{field_name} keys are not canonical")
    for unresolved in snapshot.unresolved_fills:
        _require_economic_id(
            unresolved.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            run_id=snapshot.run_id,
            field_name="unresolved fill_id",
        )
    open_bindings: dict[EconomicId, ExistingLedgerBinding] = {}
    open_entry_ids: set[EconomicId] = set()
    for binding in snapshot.open_reconciliation_bindings:
        _require_economic_id(
            binding.entry_id,
            owner=EconomicOwnerKind.LEDGER_ENTRY,
            run_id=snapshot.run_id,
            field_name="open reconciliation entry_id",
        )
        _require_economic_id(
            binding.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            run_id=snapshot.run_id,
            field_name="open reconciliation binding fill_id",
        )
        if binding.entry_id.owner_sequence == 0:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "open reconciliation binding does not identify an applied transaction",
            )
        if binding.entry_id.owner_sequence > snapshot.ledger_sequence:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "open reconciliation binding is ahead of the snapshot frontier",
            )
        if (
            binding.entry_id.owner_sequence == snapshot.ledger_sequence
            and binding.transaction_sha256 != snapshot.last_transaction_sha256
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "open reconciliation binding conflicts with the last transaction",
            )
        if binding.entry_id in open_entry_ids:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "open reconciliation bindings reuse a ledger entry",
            )
        open_entry_ids.add(binding.entry_id)
        open_bindings[binding.fill_id] = binding
    open_references: dict[EconomicId, OpenReconciliationRef] = {}
    for reference in snapshot.open_reconciliation_refs:
        _require_economic_id(
            reference.fill_id,
            owner=EconomicOwnerKind.EXECUTION_FILL,
            run_id=snapshot.run_id,
            field_name="open reconciliation fill_id",
        )
        matched_binding = open_bindings.get(reference.fill_id)
        if matched_binding is None or matched_binding.fill_sha256 != reference.fill_sha256:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "open reconciliation reference lacks its exact ledger binding",
            )
        open_references[reference.fill_id] = reference
    if set(open_bindings) != set(open_references):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "open reconciliation binding lacks its exact reference",
        )
    unresolved_by_fill = {
        reference.fill_id: reference.fill_sha256 for reference in snapshot.unresolved_fills
    }
    for fill_id, reference in open_references.items():
        unresolved_sha256 = unresolved_by_fill.get(fill_id)
        if unresolved_sha256 is not None and unresolved_sha256 != reference.fill_sha256:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "open and unresolved Fill digests conflict",
            )


def _economic_id_key(identity: EconomicId) -> tuple[str, str, int]:
    return (identity.run_id.value, identity.owner_kind.value, identity.owner_sequence)


def _decimal_integer_bytes(value: int) -> bytes:
    if type(value) is not int:
        raise TypeError("canonical integer encoder requires exact int")
    if value == 0:
        return b"0"
    sign = b""
    remaining = value
    if remaining < 0:
        sign = b"-"
        remaining = -remaining
    chunks: list[int] = []
    while remaining:
        remaining, chunk = divmod(remaining, 1_000_000_000)
        chunks.append(chunk)
    head = str(chunks.pop()).encode("ascii")
    tail = b"".join(f"{chunk:09d}".encode("ascii") for chunk in reversed(chunks))
    return sign + head + tail


def _encode_json(value: object) -> bytes:
    if value is None:
        return b"null"
    if type(value) is bool:
        return b"true" if value else b"false"
    if type(value) is str:
        return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    if type(value) is int:
        return _decimal_integer_bytes(value)
    if type(value) is list:
        return b"[" + b",".join(_encode_json(item) for item in value) + b"]"
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("canonical JSON object keys must be exact str")
        return (
            b"{"
            + b",".join(
                _encode_json(key) + b":" + _encode_json(value[key]) for key in sorted(value)
            )
            + b"}"
        )
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _instrument_document(instrument: Instrument) -> dict[str, object]:
    return {"symbol": instrument.symbol, "venue": instrument.venue.code}


def _commodity_document(commodity: LedgerCommodity) -> dict[str, object]:
    if type(commodity) is InstrumentCommodity:
        return {"instrument": _instrument_document(commodity.instrument), "kind": "instrument"}
    return {"currency": commodity.currency.code, "kind": "currency"}


def _posting_document(posting: LedgerPosting) -> dict[str, object]:
    return {
        "account": posting.account.value,
        "amount": posting.amount.text,
        "commodity": _commodity_document(posting.commodity),
    }


def _fact_key_document(key: FactDedupKey) -> dict[str, object]:
    identity = key.identity
    if type(identity) is ExternalFactId:
        dedup: dict[str, object] = {"kind": "external_id", "value": identity.value}
    elif type(identity) is SourceNativeSequence:
        dedup = {"kind": "source_native_sequence", "value": identity.value}
    else:
        raise AssertionError("canonical fact key contains an unsupported identity")
    return {"dedup_identity": dedup, "source_namespace": key.source_namespace.value}


def _provenance_document(provenance: FactProvenance) -> dict[str, object]:
    return {
        "provenance_id": provenance.identifier.value,
        "source_payload_sha256": provenance.source_payload_sha256.value,
    }


def _optional_id_document(identity: EconomicId | None) -> dict[str, object] | None:
    return None if identity is None else _economic_id_document(identity)


def _transaction_document(transaction: LedgerTransaction) -> dict[str, object]:
    return {
        "canonicalization": LEDGER_TRANSACTION_CANONICALIZATION,
        "causation_id": _optional_id_document(transaction.causation_id),
        "client_submission_key": (
            None
            if transaction.client_submission_key is None
            else transaction.client_submission_key.value
        ),
        "correlation_id": _optional_id_document(transaction.correlation_id),
        "entry_id": _economic_id_document(transaction.entry_id),
        "fact_key": _fact_key_document(transaction.fact_key),
        "fill_id": _economic_id_document(transaction.fill_id),
        "fill_sha256": transaction.fill_sha256.value,
        "instrument_specification_id": transaction.instrument_specification_id.value,
        "instrument_spec_set_id": transaction.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": transaction.instrument_spec_set_sha256.value,
        "ledger_sequence": transaction.ledger_sequence,
        "message_type": "ledger_transaction",
        "occurred_at": transaction.occurred_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "order_id": _optional_id_document(transaction.order_id),
        "postings": [_posting_document(posting) for posting in transaction.postings],
        "previous_transaction_sha256": (
            None
            if transaction.previous_transaction_sha256 is None
            else transaction.previous_transaction_sha256.value
        ),
        "provenance": _provenance_document(transaction.provenance),
        "requires_reconciliation": transaction.requires_reconciliation,
        "run_id": transaction.run_id.value,
        "schema_version": LEDGER_TRANSACTION_SCHEMA_VERSION,
        "venue_order_id": (
            None if transaction.venue_order_id is None else transaction.venue_order_id.value
        ),
    }


def _snapshot_document(snapshot: PortfolioSnapshot) -> dict[str, object]:
    return {
        "canonicalization": PORTFOLIO_SNAPSHOT_CANONICALIZATION,
        "cash_balances": [
            {
                "amount": balance.amount.text,
                "currency": balance.currency.code,
                "currency_quantum": balance.currency_quantum.text,
            }
            for balance in snapshot.cash_balances
        ],
        "instrument_spec_set_id": snapshot.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": snapshot.instrument_spec_set_sha256.value,
        "last_entry_id": _optional_id_document(snapshot.last_entry_id),
        "last_transaction_sha256": (
            None
            if snapshot.last_transaction_sha256 is None
            else snapshot.last_transaction_sha256.value
        ),
        "ledger_sequence": snapshot.ledger_sequence,
        "message_type": "portfolio_snapshot",
        "open_reconciliation_bindings": [
            _binding_document(binding) for binding in snapshot.open_reconciliation_bindings
        ],
        "open_reconciliation_refs": [
            {
                "fill_id": _economic_id_document(reference.fill_id),
                "fill_sha256": reference.fill_sha256.value,
                "processing_outcome_sha256": reference.processing_outcome_sha256.value,
            }
            for reference in snapshot.open_reconciliation_refs
        ],
        "position_balances": [
            {
                "instrument": _instrument_document(balance.instrument),
                "quantity": balance.quantity.text,
                "quantity_quantum": balance.quantity_quantum.text,
            }
            for balance in snapshot.position_balances
        ],
        "rounding_balances": [
            {"amount": balance.amount.text, "currency": balance.currency.code}
            for balance in snapshot.rounding_balances
        ],
        "run_id": snapshot.run_id.value,
        "schema_version": PORTFOLIO_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_version": snapshot.snapshot_version,
        "unresolved_fills": [
            {
                "fill_id": _economic_id_document(reference.fill_id),
                "fill_sha256": reference.fill_sha256.value,
            }
            for reference in snapshot.unresolved_fills
        ],
    }


def _binding_document(binding: ExistingLedgerBinding) -> dict[str, object]:
    return {
        "entry_id": _economic_id_document(binding.entry_id),
        "fill_id": _economic_id_document(binding.fill_id),
        "fill_sha256": binding.fill_sha256.value,
        "transaction_sha256": binding.transaction_sha256.value,
    }


def _optional_binding_document(
    binding: ExistingLedgerBinding | None,
) -> dict[str, object] | None:
    return None if binding is None else _binding_document(binding)


def _outcome_document(outcome: LedgerApplyOutcome) -> dict[str, object]:
    return {
        "after_snapshot_version": outcome.after_snapshot_version,
        "before_snapshot_version": outcome.before_snapshot_version,
        "canonicalization": LEDGER_APPLY_OUTCOME_CANONICALIZATION,
        "code": outcome.code.value,
        "conflict_kind": (None if outcome.conflict_kind is None else outcome.conflict_kind.value),
        "entry_index_binding": _optional_binding_document(outcome.entry_index_binding),
        "fact_index_binding": _optional_binding_document(outcome.fact_index_binding),
        "failure_stage": (None if outcome.failure_stage is None else outcome.failure_stage.value),
        "fill_index_binding": _optional_binding_document(outcome.fill_index_binding),
        "message_type": "ledger_apply_outcome",
        "run_id": outcome.run_id.value,
        "schema_version": LEDGER_APPLY_OUTCOME_SCHEMA_VERSION,
        "snapshot_sha256": outcome.snapshot_sha256.value,
        "submitted_fill_id": _economic_id_document(outcome.submitted_fill_id),
        "submitted_fill_sha256": outcome.submitted_fill_sha256.value,
        "transaction_entry_id": _optional_id_document(outcome.transaction_entry_id),
        "transaction_sha256": (
            None if outcome.transaction_sha256 is None else outcome.transaction_sha256.value
        ),
    }
