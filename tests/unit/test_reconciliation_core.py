from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ea.core import (
    EconomicId,
    EconomicOwnerKind,
    OpenReconciliationRef,
    OutcomeCode,
    PortfolioLedgerError,
    RunId,
    Sha256Digest,
)

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")


def test_open_reconciliation_reference_is_exact_immutable_evidence() -> None:
    reference = OpenReconciliationRef(
        fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 7),
        fill_sha256=Sha256Digest("11" * 32),
        processing_outcome_sha256=Sha256Digest("22" * 32),
    )

    assert reference.fill_id.run_id == RUN_ID
    assert reference.processing_outcome_sha256 == Sha256Digest("22" * 32)
    with pytest.raises(FrozenInstanceError):
        reference.fill_sha256 = Sha256Digest("33" * 32)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "fill_id",
            EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_ORDER, 7),
            OutcomeCode.CONFLICTING_ID,
        ),
        ("fill_id", object(), OutcomeCode.INVALID_TYPE),
        ("fill_sha256", str("11" * 32), OutcomeCode.INVALID_TYPE),
        ("processing_outcome_sha256", str("22" * 32), OutcomeCode.INVALID_TYPE),
    ],
)
def test_open_reconciliation_reference_rejects_noncanonical_fields(
    field: str,
    value: object,
    code: OutcomeCode,
) -> None:
    arguments: dict[str, object] = {
        "fill_id": EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 7),
        "fill_sha256": Sha256Digest("11" * 32),
        "processing_outcome_sha256": Sha256Digest("22" * 32),
    }
    arguments[field] = value

    with pytest.raises(PortfolioLedgerError) as captured:
        OpenReconciliationRef(**arguments)  # type: ignore[arg-type]

    assert captured.value.code is code
