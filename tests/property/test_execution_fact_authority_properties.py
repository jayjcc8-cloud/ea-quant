from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ea.core import (
    CanonicalDecimal,
    ExecutionFactAction,
    ExecutionFactKind,
    OrderProjectionState,
)
from tests.unit.test_execution_fact_authority import (
    _ingress,
    _lifecycle,
    _orders,
    _process_next,
    _runtime,
    _trade,
)


@given(
    quantities=st.lists(
        st.integers(min_value=1, max_value=3),
        min_size=1,
        max_size=7,
    )
)
def test_observed_fill_total_is_exact_while_projection_is_bounded(
    quantities: list[int],
) -> None:
    spec_set, order_authority, orders = _orders(quantity="5")
    ingresses = tuple(
        _ingress(
            _trade(
                spec_set,
                orders[0],
                external_id=f"property-trade-{sequence}",
                quantity=str(quantity),
            ),
            sequence=sequence,
        )
        for sequence, quantity in enumerate(quantities, start=1)
    )
    _source, queue, authority = _runtime(spec_set, order_authority, ingresses)

    outcomes = []
    for _ingress_value in ingresses:
        lease, outcome = _process_next(queue, authority)
        outcomes.append(outcome)
        queue.acknowledge(lease)

    exact_total = CanonicalDecimal(str(sum(quantities)))
    assert tuple(fill.quantity for fill in authority.fills) == tuple(
        CanonicalDecimal(str(quantity)) for quantity in quantities
    )
    assert authority.observed_quantity_for_order(orders[0].order_id) == exact_total
    projection = authority.projection_for_order(orders[0].order_id)
    assert projection is not None
    assert projection.projected_executed_quantity <= orders[0].quantity
    if sum(quantities) < 5:
        assert projection.projection_state is OrderProjectionState.PARTIALLY_FILLED
        assert projection.projected_executed_quantity == exact_total
    else:
        assert projection.projection_state is OrderProjectionState.FILLED
        assert projection.projected_executed_quantity == orders[0].quantity
    for outcome in outcomes:
        assert len(outcome.anomalies) == len(set(outcome.anomalies))
        assert outcome.action is (
            ExecutionFactAction.ACCEPTED
            if not outcome.anomalies
            else ExecutionFactAction.UNRESOLVED
        )


@given(permutation=st.permutations((0, 1)))
def test_stable_conflict_accepted_first_is_root_order_invariant(
    permutation: list[int],
) -> None:
    spec_set, order_authority, orders = _orders()
    candidates = (
        _ingress(
            _lifecycle(
                orders[0],
                kind=ExecutionFactKind.ACKNOWLEDGEMENT,
                external_id="property-conflict",
            ),
            sequence=2,
        ),
        _ingress(
            _lifecycle(
                orders[0],
                kind=ExecutionFactKind.REJECTION,
                external_id="property-conflict",
            ),
            sequence=1,
        ),
    )
    ingresses = tuple(candidates[index] for index in permutation)
    _source, queue, authority = _runtime(spec_set, order_authority, ingresses)

    first_lease, first = _process_next(queue, authority)
    queue.acknowledge(first_lease)
    second_lease, second = _process_next(queue, authority)
    queue.acknowledge(second_lease)

    assert authority.first_facts[0].kind is ExecutionFactKind.REJECTION
    assert first.action is ExecutionFactAction.ACCEPTED
    assert second.action is ExecutionFactAction.CONFLICT
