from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ea.core import (
    EconomicId,
    EconomicOwnerKind,
    ExternalFactId,
    FactDedupKey,
    IdentityDisposition,
    RunId,
    SourceNamespace,
    classify_identity_replay,
    economic_id_digest,
)

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")


@given(
    sequence=st.integers(min_value=0, max_value=(1 << 64) - 1),
    owner=st.sampled_from(tuple(EconomicOwnerKind)),
)
def test_economic_id_digest_is_stable_for_equal_values(
    sequence: int,
    owner: EconomicOwnerKind,
) -> None:
    first = EconomicId(RUN_ID, owner, sequence)
    second = EconomicId(RUN_ID, owner, sequence)

    assert first == second
    assert economic_id_digest(first) == economic_id_digest(second)


@given(
    external_id=st.from_regex(r"[A-Za-z0-9._:/-]{1,40}", fullmatch=True),
    first=st.binary(max_size=64),
    second=st.binary(max_size=64),
)
def test_fact_replay_classification_is_argument_order_invariant(
    external_id: str,
    first: bytes,
    second: bytes,
) -> None:
    key = FactDedupKey(SourceNamespace("sim.primary"), ExternalFactId(external_id))
    forward = classify_identity_replay(key, first, key, second)
    reverse = classify_identity_replay(key, second, key, first)

    assert forward is reverse
    assert forward is (
        IdentityDisposition.EXACT_REPLAY if first == second else IdentityDisposition.CONFLICT
    )


@given(
    external_id=st.from_regex(r"[A-Za-z0-9._:/-]{1,40}", fullmatch=True),
    payload=st.binary(max_size=64),
)
def test_same_fact_identity_in_different_sources_is_always_new(
    external_id: str,
    payload: bytes,
) -> None:
    first = FactDedupKey(SourceNamespace("sim.primary"), ExternalFactId(external_id))
    second = FactDedupKey(SourceNamespace("sim.secondary"), ExternalFactId(external_id))

    assert classify_identity_replay(first, payload, second, payload) is IdentityDisposition.NEW
