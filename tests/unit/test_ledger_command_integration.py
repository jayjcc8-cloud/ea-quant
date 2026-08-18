from __future__ import annotations

import pytest

from ea.core import (
    LedgerConflictKind,
    OpenReconciliationRef,
    OutcomeCode,
    PortfolioLedgerError,
    Sha256Digest,
    canonical_portfolio_snapshot_bytes,
    fill_digest,
    portfolio_snapshot_digest,
)
from ea.core.execution_messages import Fill
from ea.core.ledger_integration import (
    LedgerApplicationCommand,
    _create_ledger_application_command,
)
from ea.core.portfolio import LedgerTransaction
from ea.portfolio import create_portfolio_ledger
from unit.test_portfolio_ledger import RUN_ID, _fill, _spec_set

DIGESTS = tuple(Sha256Digest(f"{index:064x}") for index in range(1, 8))


def _command(fill: Fill, *, requires_reconciliation: bool = False) -> LedgerApplicationCommand:
    return _create_ledger_application_command(
        run_id=RUN_ID,
        dispatch_sequence=3,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        fill_id=fill.fill_id,
        fill_sha256=fill_digest(fill),
        requires_reconciliation=requires_reconciliation,
    )


def test_command_application_commits_economics_and_handoff_binding_once() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-a")
    command = _command(fill)

    outcome = ledger.apply_ledger_application_command(command, fill)
    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.snapshot_version == 1
    assert ledger.snapshot.open_reconciliation_refs == ()
    assert portfolio_snapshot_digest(ledger.snapshot) == outcome.snapshot_sha256

    before_replay = canonical_portfolio_snapshot_bytes(ledger.snapshot)
    replayed = ledger.apply_ledger_application_command(command, fill)
    assert replayed.code is OutcomeCode.LEDGER_APPLIED
    assert replayed == outcome
    assert canonical_portfolio_snapshot_bytes(ledger.snapshot) == before_replay
    assert len(ledger.transactions) == 1


def test_command_with_requires_reconciliation_populates_open_reference_tuples() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-b", resolved=False)
    command = _command(fill, requires_reconciliation=True)

    outcome = ledger.apply_ledger_application_command(command, fill)

    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.unresolved_fills == ()
    assert ledger.snapshot.open_reconciliation_refs == (
        OpenReconciliationRef(
            fill_id=fill.fill_id,
            fill_sha256=fill_digest(fill),
            processing_outcome_sha256=DIGESTS[1],
        ),
    )
    binding = ledger.snapshot.open_reconciliation_bindings[0]
    assert binding.fill_id == fill.fill_id
    assert binding.entry_id.owner_sequence == 1
    transaction = ledger.transactions[0]
    assert type(transaction) is LedgerTransaction
    assert transaction.requires_reconciliation is True
    assert outcome.transaction_sha256 == binding.transaction_sha256


def test_complete_ancestry_fill_can_carry_outcome_driven_reconciliation_flag() -> None:
    # ADR 0022 L111-116 supersession: the processing-outcome flag is OR-ed in,
    # so a complete-ancestry Fill may still require reconciliation.
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-c", resolved=True)
    command = _command(fill, requires_reconciliation=True)

    outcome = ledger.apply_ledger_application_command(command, fill)

    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    committed_transaction = ledger.transactions[0]
    assert type(committed_transaction) is LedgerTransaction
    assert committed_transaction.requires_reconciliation is True
    assert ledger.snapshot.open_reconciliation_refs != ()


def test_unbound_existing_fill_fails_closed_without_retroactive_provenance() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-d")
    legacy = ledger.apply_fill(fill)
    assert legacy.code is OutcomeCode.LEDGER_APPLIED

    command = _command(fill)
    outcome = ledger.apply_ledger_application_command(command, fill)

    assert outcome.code is OutcomeCode.LEDGER_CONFLICT
    assert outcome.conflict_kind is LedgerConflictKind.UNBOUND_EXISTING_FILL
    assert ledger.snapshot.snapshot_version == 1


def test_command_evidence_conflicts_are_rejected_before_mutation() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-e")
    other = _fill(spec_set, fill_sequence=2, dedup="integration-e2")

    with pytest.raises(PortfolioLedgerError, match="fill evidence"):
        ledger.apply_ledger_application_command(_command(fill), other)
    with pytest.raises(PortfolioLedgerError, match="fill evidence"):
        ledger.apply_ledger_application_command(_command(other), fill)
    assert ledger.snapshot.snapshot_version == 0


def test_handoff_digest_replay_with_different_command_conflicts() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-f")
    command = _command(fill)
    ledger.apply_ledger_application_command(command, fill)

    drift = _create_ledger_application_command(
        run_id=RUN_ID,
        dispatch_sequence=4,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[2],
        fill_id=fill.fill_id,
        fill_sha256=fill_digest(fill),
        requires_reconciliation=False,
    )
    with pytest.raises(PortfolioLedgerError, match="replay conflicts"):
        ledger.apply_ledger_application_command(drift, fill)


def test_legacy_apply_fill_keeps_adr_0010_unresolved_semantics() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="integration-g", resolved=False)

    outcome = ledger.apply_fill(fill)

    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.open_reconciliation_refs == ()
    assert ledger.snapshot.unresolved_fills != ()
    legacy_transaction = ledger.transactions[0]
    assert type(legacy_transaction) is LedgerTransaction
    assert legacy_transaction.requires_reconciliation is True
