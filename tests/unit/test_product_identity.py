from __future__ import annotations

import pytest

from ea.product import BacktestIdentityError, semantic_outcome_sha256


def test_semantic_outcome_rejects_attempt_identity_nested_in_tuple() -> None:
    with pytest.raises(BacktestIdentityError, match="attempt-only field 'run_id'"):
        semantic_outcome_sha256(
            {
                "schema": "ea.backtest-semantic-outcome.v1",
                "wrapper": ({"run_id": "123e4567-e89b-42d3-a456-426614174000"},),
            }
        )
