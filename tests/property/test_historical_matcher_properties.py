from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from ea.core.economics import CanonicalDecimal
from ea.core.execution import (
    InstrumentSpecId,
    InstrumentSpecSetId,
    build_instrument_spec_set,
    canonical_instrument_spec_set_bytes,
    instrument_spec_set_digest,
)
from ea.core.execution_messages import execution_fact_ingress_digest, order_digest
from ea.core.historical_matching import (
    HistoricalMatcherDecodeContext,
    canonical_historical_matcher_dispatch_batch_bytes,
    canonical_historical_matcher_state_bytes,
    canonical_historical_submission_receipt_bytes,
    decode_historical_matcher_state,
    historical_matcher_dispatch_batch_digest,
    historical_submission_receipt_digest,
)
from ea.core.identity import Instrument, VenueId

_HELPER_PATH = Path(__file__).parents[1] / "unit" / "test_historical_matcher.py"
_SPEC = importlib.util.spec_from_file_location("historical_matcher_property_helper", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HELPER = importlib.util.module_from_spec(_SPEC)
sys.modules["historical_matcher_property_helper"] = _HELPER
_SPEC.loader.exec_module(_HELPER)
_system = cast(Callable[[], tuple[Any, ...]], _HELPER.__dict__["_system"])


@settings(deadline=None)
@given(permutation=st.permutations((0, 1)))
def test_pending_presentation_permutation_cannot_change_emission(
    permutation: list[int],
) -> None:
    _, baseline, orders, causal, delayed, end = _system()
    baseline.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    baseline.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    expected_pending_state = baseline.state
    expected_batch = baseline.expire_at_active_end(end, dispatch_sequence=9)
    expected_state = baseline.state

    _, candidate, orders, causal, delayed, end = _system()
    candidate.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    candidate.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    candidate._state = replace(
        candidate._state,
        pending=tuple(candidate._state.pending[index] for index in permutation),
    )
    assert canonical_historical_matcher_state_bytes(candidate.state) == (
        canonical_historical_matcher_state_bytes(expected_pending_state)
    )
    actual_batch = candidate.expire_at_active_end(end, dispatch_sequence=9)

    assert canonical_historical_matcher_dispatch_batch_bytes(actual_batch) == (
        canonical_historical_matcher_dispatch_batch_bytes(expected_batch)
    )
    assert canonical_historical_matcher_state_bytes(candidate.state) == (
        canonical_historical_matcher_state_bytes(expected_state)
    )


@settings(max_examples=24, deadline=None)
@given(
    future_close=st.floats(
        min_value=1.0,
        max_value=200.0,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    ),
    future_source_sequence=st.integers(min_value=3, max_value=1000),
)
def test_future_roots_cannot_change_a_frozen_prefix(
    future_close: float,
    future_source_sequence: int,
) -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    receipt_bytes = canonical_historical_submission_receipt_bytes(receipt)
    batch_bytes = canonical_historical_matcher_dispatch_batch_bytes(batch)
    prefix_state = matcher.state
    state_bytes = canonical_historical_matcher_state_bytes(prefix_state)

    future_end = delayed.payload.interval_end + timedelta(minutes=1)
    future = replace(
        delayed,
        payload=replace(
            delayed.payload,
            interval_start=delayed.payload.interval_end,
            interval_end=future_end,
            open=future_close,
            high=future_close,
            low=future_close,
            close=future_close,
        ),
        available_at=future_end + timedelta(seconds=5),
        source_sequence=future_source_sequence,
    )
    matcher.match_active_market_root(future, dispatch_sequence=9)

    assert canonical_historical_submission_receipt_bytes(receipt) == receipt_bytes
    assert canonical_historical_matcher_dispatch_batch_bytes(batch) == batch_bytes
    assert canonical_historical_matcher_state_bytes(prefix_state) == state_bytes


@settings(deadline=None)
@given(
    order_permutation=st.permutations((0, 1)),
    receipt_permutation=st.permutations((0, 1)),
    batch_permutation=st.permutations((0, 1)),
)
def test_lookup_registry_insertion_order_cannot_change_state_decode(
    order_permutation: list[int],
    receipt_permutation: list[int],
    batch_permutation: list[int],
) -> None:
    _, matcher, orders, causal, delayed, end = _system()
    receipts = [matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)]
    batches = [matcher.match_active_market_root(delayed, dispatch_sequence=8)]
    receipts.append(matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8))
    batches.append(matcher.expire_at_active_end(end, dispatch_sequence=9))
    encoded = canonical_historical_matcher_state_bytes(matcher.state)
    ingresses = [ingress for batch in batches for ingress in batch.ingresses]
    context = HistoricalMatcherDecodeContext(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        execution_policy=matcher.execution_policy,
        source_namespace=matcher.source_namespace,
        provenance_id=matcher.provenance_id,
        orders_by_sha256={
            order_digest(orders[index]): orders[index] for index in order_permutation
        },
        market_roots_by_sha256={
            receipts[0].causal_market_sha256: causal,
            batches[0].trigger_root_sha256: delayed,
        },
        end_roots_by_sha256={batches[1].trigger_root_sha256: end},
        receipts_by_sha256={
            historical_submission_receipt_digest(receipts[index]): receipts[index]
            for index in receipt_permutation
        },
        batches_by_sha256={
            historical_matcher_dispatch_batch_digest(batches[index]): batches[index]
            for index in batch_permutation
        },
        ingresses_by_sha256={
            execution_fact_ingress_digest(ingress): ingress for ingress in reversed(ingresses)
        },
    )
    decoded = decode_historical_matcher_state(encoded, context=context)
    assert canonical_historical_matcher_state_bytes(decoded) == encoded


@given(permutation=st.permutations((0, 1)))
def test_instrument_spec_construction_permutation_is_canonical(
    permutation: list[int],
) -> None:
    _, matcher, _, _, _, _ = _system()
    first = matcher.spec_set.specifications[0]
    second = replace(
        first,
        instrument=Instrument(VenueId("XNYS"), "MSFT"),
        specification_id=InstrumentSpecId("xnys.msft.v1"),
        price_quantum=CanonicalDecimal("0.05"),
    )
    specifications = (first, second)
    baseline = build_instrument_spec_set(
        InstrumentSpecSetId("phase1.permutation.v1"),
        specifications,
    )
    candidate = build_instrument_spec_set(
        InstrumentSpecSetId("phase1.permutation.v1"),
        tuple(specifications[index] for index in permutation),
    )
    assert canonical_instrument_spec_set_bytes(candidate) == (
        canonical_instrument_spec_set_bytes(baseline)
    )
    assert instrument_spec_set_digest(candidate) == instrument_spec_set_digest(baseline)


def test_future_bounded_end_cannot_change_frozen_market_prefix() -> None:
    _, matcher, orders, causal, delayed, end = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    receipt_bytes = canonical_historical_submission_receipt_bytes(receipt)
    batch_bytes = canonical_historical_matcher_dispatch_batch_bytes(batch)
    state = matcher.state
    state_bytes = canonical_historical_matcher_state_bytes(state)

    matcher.expire_at_active_end(end, dispatch_sequence=9)

    assert canonical_historical_submission_receipt_bytes(receipt) == receipt_bytes
    assert canonical_historical_matcher_dispatch_batch_bytes(batch) == batch_bytes
    assert canonical_historical_matcher_state_bytes(state) == state_bytes
