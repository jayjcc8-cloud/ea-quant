from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ea.core import (
    DataFingerprint,
    ReplayWindow,
    RunBinding,
    RunBindingMismatchError,
    RunContractError,
    RunId,
    RunReference,
    Sha256Digest,
    derive_component_seed,
    require_run_binding,
)

ZERO_DIGEST = Sha256Digest("0" * 64)
ONE_DIGEST = Sha256Digest("1" * 64)
RUN_ID = RunId("123e4567-e89b-42d3-a456-426614174000")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0" * 63,
        "0" * 65,
        "G" * 64,
        "A" * 64,
        "0" * 63 + "\n",
    ],
)
def test_sha256_digest_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(RunContractError, match="SHA-256"):
        Sha256Digest(value)


@pytest.mark.parametrize(
    "value",
    [
        "123e4567-e89b-12d3-a456-426614174000",
        "123E4567-E89B-42D3-A456-426614174000",
        "{123e4567-e89b-42d3-a456-426614174000}",
        "urn:uuid:123e4567-e89b-42d3-a456-426614174000",
        "00000000-0000-0000-0000-000000000000",
    ],
)
def test_run_id_rejects_noncanonical_or_non_v4_values(value: str) -> None:
    with pytest.raises(RunContractError, match="UUID4"):
        RunId(value)


def test_replay_window_is_exact_utc_half_open_and_immutable() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    window = ReplayWindow(start, end)

    assert window.start_inclusive.tzinfo is UTC
    assert window.end_exclusive.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        window.end_exclusive = start  # type: ignore[misc]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC)),
        (
            datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 1, 2, tzinfo=UTC),
        ),
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_replay_window_rejects_non_utc_or_empty_values(
    start: datetime,
    end: datetime,
) -> None:
    with pytest.raises(RunContractError):
        ReplayWindow(start, end)


@pytest.mark.parametrize("count", [0, -1, True, 1.0, 1 << 64])
def test_data_fingerprint_requires_nonempty_uint64_count(count: object) -> None:
    with pytest.raises(RunContractError, match="record_count"):
        DataFingerprint(sha256=ZERO_DIGEST, record_count=count)  # type: ignore[arg-type]


def test_run_binding_is_typed_and_mismatch_fails_before_boundary_use() -> None:
    reference = RunReference(run_id=RUN_ID, lineage_sha256=ZERO_DIGEST)
    expected = RunBinding(reference=reference, manifest_sha256=ONE_DIGEST)
    equal = RunBinding(reference=reference, manifest_sha256=ONE_DIGEST)
    wrong = RunBinding(
        reference=RunReference(run_id=RUN_ID, lineage_sha256=ONE_DIGEST),
        manifest_sha256=ONE_DIGEST,
    )

    require_run_binding(expected, equal)
    with pytest.raises(RunBindingMismatchError):
        require_run_binding(expected, wrong)


def test_component_seed_matches_normative_vector() -> None:
    assert derive_component_seed(0, "matcher.primary") == 148415519428445905247115446051054473768


@pytest.mark.parametrize(
    ("seed", "label"),
    [
        (-1, "matcher.primary"),
        (1 << 64, "matcher.primary"),
        (True, "matcher.primary"),
        (0, ""),
        (0, "Matcher"),
        (0, "a" * 129),
    ],
)
def test_component_seed_rejects_invalid_seed_or_label(seed: object, label: str) -> None:
    with pytest.raises(RunContractError):
        derive_component_seed(seed, label)  # type: ignore[arg-type]
