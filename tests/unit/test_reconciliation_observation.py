from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from ea.core import (
    CanonicalDecimal,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    FactProvenanceId,
    Instrument,
    PositionReconciliationBalance,
    ReconciliationContractError,
    ReconciliationObservation,
    ReconciliationObservationKind,
    ReconciliationScopeKind,
    RuntimeIdentifier,
    Sha256Digest,
    SourceNamespace,
    VenueId,
    canonical_reconciliation_observation_bytes,
    create_reconciliation_observation,
    decode_reconciliation_observation,
    reconciliation_observation_digest,
)
from unit.test_portfolio_ledger import INSTRUMENT, RUN_ID, USD, _spec, _spec_set

TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
SOURCE = SourceNamespace("reconciliation.sim")
DIGEST = Sha256Digest("ab" * 32)


def _canonical(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _observation(
    *,
    kind: ReconciliationObservationKind = ReconciliationObservationKind.POSITION_SNAPSHOT,
    scope: ReconciliationScopeKind = ReconciliationScopeKind.POSITION,
    balances: tuple[PositionReconciliationBalance | CashReconciliationBalance, ...] | None = None,
    available_at: datetime = TIME,
) -> ReconciliationObservation:
    selected = balances
    if selected is None:
        selected = (PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("10")),)
    return create_reconciliation_observation(
        run_id=RUN_ID,
        spec_set=_spec_set(),
        observation_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            1,
        ),
        kind=kind,
        source_namespace=SOURCE,
        source_sequence=7,
        occurred_at=TIME,
        available_at=available_at,
        watermark_namespace=SourceNamespace("ledger.portfolio"),
        watermark_sequence=3,
        declared_scope_kind=scope,
        declared_scope_id=RuntimeIdentifier("portfolio.default"),
        provenance_id=FactProvenanceId("reconciliation.fixture.v1"),
        provenance_payload_sha256=DIGEST,
        balances=selected,
    )


def test_position_observation_round_trips_with_exact_specification_binding() -> None:
    spec_set = _spec_set()
    observation = _observation()

    encoded = canonical_reconciliation_observation_bytes(observation)
    decoded = decode_reconciliation_observation(encoded, spec_set)

    assert decoded == observation
    assert canonical_reconciliation_observation_bytes(decoded) == encoded
    assert len(reconciliation_observation_digest(observation).value) == 64
    assert len(encoded) < 16_384


def test_cash_and_detail_observation_matrices_are_closed() -> None:
    cash = _observation(
        kind=ReconciliationObservationKind.CASH_SNAPSHOT,
        scope=ReconciliationScopeKind.CASH,
        balances=(CashReconciliationBalance(USD, CanonicalDecimal("125.5")),),
    )
    order = _observation(
        kind=ReconciliationObservationKind.ORDER_DETAIL,
        scope=ReconciliationScopeKind.ORDER,
        balances=(),
    )

    cash_balance = cash.balances[0]
    assert type(cash_balance) is CashReconciliationBalance
    assert cash_balance.amount == CanonicalDecimal("125.5")
    assert order.balances == ()
    with pytest.raises(ReconciliationContractError):
        _observation(kind=ReconciliationObservationKind.ORDER_DETAIL, balances=())
    with pytest.raises(ReconciliationContractError):
        _observation(balances=())
    with pytest.raises(ReconciliationContractError):
        _observation(available_at=TIME - timedelta(microseconds=1))


def test_observation_rejects_unquantized_or_noncanonical_balance_order() -> None:
    with pytest.raises(ReconciliationContractError):
        _observation(balances=(PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("0.5")),))
    with pytest.raises(ReconciliationContractError):
        _observation(
            balances=(
                PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("1")),
                PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("2")),
            )
        )


def test_snapshot_observation_rejects_more_than_32_complete_balances() -> None:
    instruments = tuple(Instrument(VenueId("XNAS"), f"S{index:02d}") for index in range(33))
    spec_set = _spec_set(
        *(
            _spec(instrument=instrument, specification_id=f"xnas.s{index:02d}.v1")
            for index, instrument in enumerate(instruments)
        )
    )
    with pytest.raises(ReconciliationContractError):
        create_reconciliation_observation(
            run_id=RUN_ID,
            spec_set=spec_set,
            observation_id=EconomicId(
                RUN_ID,
                EconomicOwnerKind.RECONCILIATION_OBSERVATION,
                2,
            ),
            kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
            source_namespace=SOURCE,
            source_sequence=8,
            occurred_at=TIME,
            available_at=TIME,
            watermark_namespace=SourceNamespace("ledger.portfolio"),
            watermark_sequence=4,
            declared_scope_kind=ReconciliationScopeKind.POSITION,
            declared_scope_id=RuntimeIdentifier("portfolio.default"),
            provenance_id=FactProvenanceId("reconciliation.fixture.v1"),
            provenance_payload_sha256=DIGEST,
            balances=tuple(
                PositionReconciliationBalance(instrument, CanonicalDecimal("1"))
                for instrument in instruments
            ),
        )


def test_decoder_rejects_duplicate_noncanonical_unknown_and_bool_integer() -> None:
    spec_set = _spec_set()
    encoded = canonical_reconciliation_observation_bytes(_observation())
    document = json.loads(encoded)
    invalid_payloads = (
        encoded.replace(b'"balances":', b'"balances":[],"balances":', 1),
        encoded + b" ",
        _canonical({**document, "extra": None}),
        _canonical({**document, "source_sequence": True}),
        _canonical({**document, "instrument_spec_set_sha256": "11" * 32}),
        _canonical({**document, "provenance_payload_sha256": "not-a-digest"}),
        _canonical({**document, "run_id": "not-a-run"}),
        _canonical({**document, "declared_scope_kind": "unknown"}),
        _canonical({**document, "balances": [{"kind": "instrument_position"}]}),
        _canonical(
            {
                **document,
                "balances": [
                    {
                        "instrument": [],
                        "kind": "instrument_position",
                        "quantity": "10",
                    }
                ],
            }
        ),
        _canonical(
            {
                **document,
                "balances": [{"amount": "125.5", "kind": "settlement_cash"}],
            }
        ),
        _canonical({**document, "balances": [{"kind": "unknown"}]}),
    )
    for payload in invalid_payloads:
        with pytest.raises(ReconciliationContractError):
            decode_reconciliation_observation(payload, spec_set)


def test_decoder_rejects_subclass_float_and_payload_above_bound() -> None:
    class BytesSubclass(bytes):
        pass

    spec_set = _spec_set()
    encoded = canonical_reconciliation_observation_bytes(_observation())
    document = json.loads(encoded)
    with pytest.raises(ReconciliationContractError):
        decode_reconciliation_observation(BytesSubclass(encoded), spec_set)
    with pytest.raises(ReconciliationContractError):
        decode_reconciliation_observation(
            json.dumps({**document, "watermark_sequence": 1.0}, sort_keys=True).encode(),
            spec_set,
        )
    with pytest.raises(ReconciliationContractError):
        decode_reconciliation_observation(b"x" * 16_385, spec_set)


def test_observation_digest_matches_hand_computed_golden() -> None:
    # VERIFY-004: pin the observation digest formula with an independent
    # hashlib computation: domain + u64be(payload length) + payload.
    observation = _observation()
    payload = canonical_reconciliation_observation_bytes(observation)

    expected = Sha256Digest(
        hashlib.sha256(
            b"ea.reconciliation-observation.v1\0" + len(payload).to_bytes(8, "big") + payload
        ).hexdigest()
    )

    assert reconciliation_observation_digest(observation) == expected
    assert expected.value == "1d2002d5615c3321e47a4c6c65de7d7f5d6248f455efc99b66de4902e875f563"
