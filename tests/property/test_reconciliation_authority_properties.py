from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from unit.test_portfolio_ledger import RUN_ID, _spec, _spec_set

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    FactProvenanceId,
    Instrument,
    PositionReconciliationBalance,
    ReconciliationObservationKind,
    ReconciliationScopeKind,
    RuntimeIdentifier,
    Sha256Digest,
    SourceNamespace,
    VenueId,
    canonical_portfolio_snapshot_bytes,
    create_reconciliation_observation,
)
from ea.portfolio import create_portfolio_ledger
from ea.reconciliation import (
    create_phase1_reconciliation_authority,
)

TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
SOURCE = SourceNamespace("reconciliation.sim")
WATERMARK = SourceNamespace("ledger.portfolio")


@st.composite
def observations(draw: st.DrawFn) -> tuple[Instrument, str, str, int]:
    index = draw(st.integers(min_value=0, max_value=7))
    local_text = draw(st.sampled_from(["0", "10", "100", "1000"]))
    observed_text = draw(st.sampled_from(["0", "10", "12", "100"]))
    source_sequence = draw(st.integers(min_value=1, max_value=50))
    return _instruments()[index], local_text, observed_text, source_sequence


_INSTRUMENTS = tuple(
    Instrument(VenueId("XNAS"), symbol)
    for symbol in ("AAPL", "TSLA", "MSFT", "NVDA", "AMD", "INTC", "ORCL", "IBM")
)


def _instruments() -> tuple[Instrument, ...]:
    return _INSTRUMENTS


def _spec_set_for(selected: tuple[Instrument, ...]) -> object:
    return _spec_set(
        *(
            _spec(instrument=instrument, specification_id=f"xnas.{instrument.symbol.lower()}.v1")
            for instrument in selected
        )
    )


@pytest.mark.parametrize("seed", [0, 1, 2])
@given(data=observations())
def test_admission_is_deterministic_and_replay_returns_original(
    seed: int,
    data: tuple[Instrument, str, str, int],
) -> None:
    instrument, local_text, observed_text, source_sequence = data
    spec_set = _spec_set_for((instrument,))
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    authority = create_phase1_reconciliation_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        snapshot_view=lambda: ledger.snapshot,
    )
    observation = create_reconciliation_observation(
        run_id=RUN_ID,
        spec_set=spec_set,
        observation_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            source_sequence,
        ),
        kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
        source_namespace=SOURCE,
        source_sequence=source_sequence,
        occurred_at=TIME,
        available_at=TIME,
        watermark_namespace=WATERMARK,
        watermark_sequence=ledger.snapshot.ledger_sequence,
        declared_scope_kind=ReconciliationScopeKind.POSITION,
        declared_scope_id=RuntimeIdentifier("portfolio.default"),
        provenance_id=FactProvenanceId("reconciliation.fixture.v1"),
        provenance_payload_sha256=Sha256Digest("ab" * 32),
        balances=(PositionReconciliationBalance(instrument, CanonicalDecimal(observed_text)),),
    )

    outcome = authority.admit_observation(observation, dispatch_sequence=7)
    assert outcome.observation_sha256.value
    replay = authority.admit_observation(observation, dispatch_sequence=8)
    assert replay is outcome
    # The acknowledged local snapshot is never mutated by admission.
    assert canonical_portfolio_snapshot_bytes(
        ledger.snapshot
    ) == canonical_portfolio_snapshot_bytes(ledger.snapshot)
    assert ledger.snapshot.snapshot_version == 0
