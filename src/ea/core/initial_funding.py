"""Canonical manifest-bound initial-funding evidence for the product kernel."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import InstrumentExecutionSpecSet, InstrumentSpecSetId, SettlementCurrency
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.portfolio import (
    CurrencyCommodity,
    LedgerAccountKind,
    LedgerPosting,
    PortfolioSnapshot,
    portfolio_snapshot_digest,
)
from ea.core.run import RunContractError, RunId, Sha256Digest

INITIAL_FUNDING_SPEC_CANONICALIZATION = "ea-initial-funding-spec-v1"
INITIAL_FUNDING_SPEC_DOMAIN = b"ea.initial-funding-spec.v1\0"
INITIAL_FUNDING_TRANSACTION_DOMAIN = b"ea.initial-funding-transaction.v1\0"
INITIAL_FUNDING_OUTCOME_DOMAIN = b"ea.initial-funding-outcome.v1\0"


class InitialFundingError(RunContractError):
    """A funding specification violates the closed product contract."""


def _fail(message: str) -> InitialFundingError:
    return InitialFundingError(message)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@final
@dataclass(frozen=True, slots=True)
class InitialFundingSpec:
    """The single currency, positive, exact genesis amount for one spec set."""

    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    settlement_currency: SettlementCurrency
    currency_quantum: CanonicalDecimal
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.instrument_spec_set_id) is not InstrumentSpecSetId:
            raise _fail("funding instrument specification-set identity must be exact")
        if type(self.instrument_spec_set_sha256) is not Sha256Digest:
            raise _fail("funding instrument specification-set digest must be exact")
        if type(self.settlement_currency) is not SettlementCurrency:
            raise _fail("funding settlement currency must be exact")
        if (
            type(self.currency_quantum) is not CanonicalDecimal
            or type(self.amount) is not CanonicalDecimal
        ):
            raise _fail("funding amount and quantum must be exact canonical decimals")
        try:
            require_positive(self.currency_quantum, field_name="funding currency_quantum")
            require_positive(self.amount, field_name="funding amount")
            require_quantized(self.amount, self.currency_quantum, field_name="funding amount")
        except EconomicValidationError as error:
            raise _fail(str(error)) from error


def canonical_initial_funding_spec_bytes(spec: InitialFundingSpec) -> bytes:
    """Return the literal closed-schema bytes which enter v2 lineage."""
    if type(spec) is not InitialFundingSpec:
        raise _fail("funding spec must be exact")
    return _canonical_json(
        {
            "amount": spec.amount.text,
            "canonicalization": INITIAL_FUNDING_SPEC_CANONICALIZATION,
            "currency_quantum": spec.currency_quantum.text,
            "instrument_spec_set_id": spec.instrument_spec_set_id.value,
            "instrument_spec_set_sha256": spec.instrument_spec_set_sha256.value,
            "schema_version": 1,
            "settlement_currency": spec.settlement_currency.code,
        }
    )


def initial_funding_spec_digest(spec: InitialFundingSpec) -> Sha256Digest:
    """Derive the domain-separated immutable funding identity."""
    return Sha256Digest(
        sha256(INITIAL_FUNDING_SPEC_DOMAIN + canonical_initial_funding_spec_bytes(spec)).hexdigest()
    )


def validate_initial_funding_spec(
    spec: InitialFundingSpec, spec_set: InstrumentExecutionSpecSet
) -> None:
    """Bind funding to exactly one settlement currency and quantum in the full set."""
    from ea.core.execution import instrument_spec_set_digest

    if type(spec) is not InitialFundingSpec or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail("funding specification and specification set must be exact")
    currencies = {item.settlement_currency for item in spec_set.specifications}
    quanta = {item.currency_quantum for item in spec_set.specifications}
    if len(currencies) != 1 or len(quanta) != 1:
        raise _fail("funding specification set must have exactly one currency and quantum")
    if (
        spec.instrument_spec_set_id != spec_set.identifier
        or spec.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or spec.settlement_currency != next(iter(currencies))
        or spec.currency_quantum != next(iter(quanta))
    ):
        raise _fail("funding specification does not match the complete specification set")


class InitialFundingResult(StrEnum):
    APPLIED = "applied"
    CONFLICT = "conflict"


class InitialFundingConflictKind(StrEnum):
    ENTRY_ID_OCCUPIED = "entry_id_occupied"
    FUNDING_BINDING_CONFLICT = "funding_binding_conflict"
    MANIFEST_BINDING_CONFLICT = "manifest_binding_conflict"
    AUDIT_PREDECESSOR_CONFLICT = "audit_predecessor_conflict"


@final
@dataclass(frozen=True, slots=True)
class InitialFundingTransaction:
    """The immutable, balanced ledger genesis transaction."""

    run_id: RunId
    entry_id: EconomicId
    ledger_sequence: int
    manifest_sha256: Sha256Digest
    lineage_sha256: Sha256Digest
    funding_spec_sha256: Sha256Digest
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    settlement_currency: SettlementCurrency
    currency_quantum: CanonicalDecimal
    amount: CanonicalDecimal
    prepared_audit_acknowledgement_sha256: Sha256Digest
    previous_transaction_sha256: None
    postings: tuple[LedgerPosting, LedgerPosting]

    def __post_init__(self) -> None:
        if type(self.run_id) is not RunId or type(self.entry_id) is not EconomicId:
            raise _fail("funding transaction identity must be exact")
        if (
            self.entry_id.run_id != self.run_id
            or self.entry_id.owner_kind is not EconomicOwnerKind.LEDGER_ENTRY
            or self.entry_id.owner_sequence != 1
            or type(self.ledger_sequence) is not int
            or self.ledger_sequence != 1
            or self.previous_transaction_sha256 is not None
        ):
            raise _fail("funding transaction must be the first ledger entry")
        if any(
            type(value) is not Sha256Digest
            for value in (
                self.manifest_sha256,
                self.lineage_sha256,
                self.funding_spec_sha256,
                self.instrument_spec_set_sha256,
                self.prepared_audit_acknowledgement_sha256,
            )
        ):
            raise _fail("funding transaction digests must be exact")
        if type(self.instrument_spec_set_id) is not InstrumentSpecSetId:
            raise _fail("funding transaction specification-set identity must be exact")
        if type(self.settlement_currency) is not SettlementCurrency:
            raise _fail("funding transaction currency must be exact")
        try:
            require_positive(self.currency_quantum, field_name="funding currency_quantum")
            require_positive(self.amount, field_name="funding amount")
            require_quantized(self.amount, self.currency_quantum, field_name="funding amount")
        except EconomicValidationError as error:
            raise _fail(str(error)) from error
        if type(self.postings) is not tuple or len(self.postings) != 2:
            raise _fail("funding transaction requires two exact postings")
        if (
            initial_funding_spec_digest(
                InitialFundingSpec(
                    self.instrument_spec_set_id,
                    self.instrument_spec_set_sha256,
                    self.settlement_currency,
                    self.currency_quantum,
                    self.amount,
                )
            )
            != self.funding_spec_sha256
        ):
            raise _fail("funding transaction specification digest conflicts with its fields")
        positive, negative = self.postings
        if (
            type(positive) is not LedgerPosting
            or type(negative) is not LedgerPosting
            or positive.account is not LedgerAccountKind.PORTFOLIO_CASH
            or negative.account is not LedgerAccountKind.EXTERNAL_SETTLEMENT
            or positive.commodity != CurrencyCommodity(self.settlement_currency)
            or negative.commodity != CurrencyCommodity(self.settlement_currency)
            or positive.amount != self.amount
            or negative.amount.coefficient != -self.amount.coefficient
            or negative.amount.scale != self.amount.scale
        ):
            raise _fail("funding transaction postings conflict with funding evidence")


def canonical_initial_funding_transaction_bytes(transaction: InitialFundingTransaction) -> bytes:
    if type(transaction) is not InitialFundingTransaction:
        raise _fail("funding transaction must be exact")
    return _canonical_json(
        {
            "amount": transaction.amount.text,
            "currency_quantum": transaction.currency_quantum.text,
            "entry_id": {
                "owner_kind": transaction.entry_id.owner_kind.value,
                "owner_sequence": 1,
                "run_id": transaction.run_id.value,
            },
            "funding_spec_sha256": transaction.funding_spec_sha256.value,
            "instrument_spec_set_id": transaction.instrument_spec_set_id.value,
            "instrument_spec_set_sha256": transaction.instrument_spec_set_sha256.value,
            "ledger_sequence": 1,
            "lineage_sha256": transaction.lineage_sha256.value,
            "manifest_sha256": transaction.manifest_sha256.value,
            "prepared_audit_acknowledgement_sha256": (
                transaction.prepared_audit_acknowledgement_sha256.value
            ),
            "previous_transaction_sha256": None,
            "postings": [
                {
                    "account": posting.account.value,
                    "amount": posting.amount.text,
                    "currency": transaction.settlement_currency.code,
                }
                for posting in transaction.postings
            ],
            "schema": "ea-initial-funding-transaction-v1",
            "settlement_currency": transaction.settlement_currency.code,
        }
    )


def initial_funding_transaction_digest(transaction: InitialFundingTransaction) -> Sha256Digest:
    return Sha256Digest(
        sha256(
            INITIAL_FUNDING_TRANSACTION_DOMAIN
            + canonical_initial_funding_transaction_bytes(transaction)
        ).hexdigest()
    )


@final
@dataclass(frozen=True, slots=True)
class InitialFundingOutcome:
    """Retained exact replay or conflict evidence for the funding transition."""

    run_id: RunId
    result: InitialFundingResult
    manifest_sha256: Sha256Digest
    submitted_funding_spec_sha256: Sha256Digest
    prepared_audit_acknowledgement_sha256: Sha256Digest
    before_snapshot_version: int
    after_snapshot_version: int
    snapshot: PortfolioSnapshot
    transaction: InitialFundingTransaction | None
    existing_transaction_sha256: Sha256Digest | None
    conflict_kind: InitialFundingConflictKind | None

    def __post_init__(self) -> None:
        if type(self.run_id) is not RunId or type(self.result) is not InitialFundingResult:
            raise _fail("funding outcome identity must be exact")
        if (
            any(
                type(value) is not Sha256Digest
                for value in (
                    self.manifest_sha256,
                    self.submitted_funding_spec_sha256,
                    self.prepared_audit_acknowledgement_sha256,
                )
            )
            or type(self.snapshot) is not PortfolioSnapshot
        ):
            raise _fail("funding outcome bindings must be exact")
        if (
            type(self.before_snapshot_version) is not int
            or type(self.after_snapshot_version) is not int
            or self.snapshot.run_id != self.run_id
            or self.before_snapshot_version
            != self.snapshot.snapshot_version
            - (1 if self.result is InitialFundingResult.APPLIED else 0)
            or self.after_snapshot_version != self.snapshot.snapshot_version
        ):
            raise _fail("funding outcome snapshot versions conflict")
        if self.result is InitialFundingResult.APPLIED:
            if (
                type(self.transaction) is not InitialFundingTransaction
                or self.conflict_kind is not None
                or self.existing_transaction_sha256 is not None
            ):
                raise _fail("applied funding outcome fields conflict")
            if (
                self.transaction.run_id != self.run_id
                or self.transaction.manifest_sha256 != self.manifest_sha256
                or self.transaction.funding_spec_sha256 != self.submitted_funding_spec_sha256
                or (
                    self.transaction.prepared_audit_acknowledgement_sha256
                    != self.prepared_audit_acknowledgement_sha256
                )
            ):
                raise _fail("applied funding outcome bindings conflict")
            transaction, snapshot = self.transaction, self.snapshot
            cash = snapshot.cash_balances
            if (
                self.before_snapshot_version != 0
                or self.after_snapshot_version != 1
                or snapshot.instrument_spec_set_id != transaction.instrument_spec_set_id
                or snapshot.instrument_spec_set_sha256 != transaction.instrument_spec_set_sha256
                or snapshot.last_entry_id != transaction.entry_id
                or snapshot.last_transaction_sha256
                != initial_funding_transaction_digest(transaction)
                or len(cash) != 1
                or (cash[0].currency, cash[0].currency_quantum, cash[0].amount)
                != (
                    transaction.settlement_currency,
                    transaction.currency_quantum,
                    transaction.amount,
                )
                or snapshot.position_balances
                or snapshot.rounding_balances
                or snapshot.unresolved_fills
                or snapshot.open_reconciliation_bindings
                or snapshot.open_reconciliation_refs
            ):
                raise _fail("applied funding outcome conflicts with exact genesis snapshot")
        elif (
            self.transaction is not None
            or type(self.existing_transaction_sha256) is not Sha256Digest
            or type(self.conflict_kind) is not InitialFundingConflictKind
        ):
            raise _fail("conflicting funding outcome fields conflict")


def canonical_initial_funding_outcome_bytes(outcome: InitialFundingOutcome) -> bytes:
    if type(outcome) is not InitialFundingOutcome:
        raise _fail("funding outcome must be exact")
    return _canonical_json(
        {
            "after_snapshot_version": outcome.after_snapshot_version,
            "before_snapshot_version": outcome.before_snapshot_version,
            "canonicalization": "ea.initial-funding-outcome.v1",
            "conflict_kind": None if outcome.conflict_kind is None else outcome.conflict_kind.value,
            "existing_transaction_sha256": None
            if outcome.existing_transaction_sha256 is None
            else outcome.existing_transaction_sha256.value,
            "manifest_sha256": outcome.manifest_sha256.value,
            "prepared_audit_acknowledgement_sha256": (
                outcome.prepared_audit_acknowledgement_sha256.value
            ),
            "result": outcome.result.value,
            "run_id": outcome.run_id.value,
            "schema": "ea.initial-funding-outcome.v1",
            "snapshot_sha256": portfolio_snapshot_digest(outcome.snapshot).value,
            "submitted_funding_spec_sha256": outcome.submitted_funding_spec_sha256.value,
            "transaction_sha256": None
            if outcome.transaction is None
            else initial_funding_transaction_digest(outcome.transaction).value,
        }
    )


def initial_funding_outcome_digest(outcome: InitialFundingOutcome) -> Sha256Digest:
    return Sha256Digest(
        sha256(
            INITIAL_FUNDING_OUTCOME_DOMAIN + canonical_initial_funding_outcome_bytes(outcome)
        ).hexdigest()
    )
