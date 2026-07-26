"""Single-consumer queue over one sealed bounded runtime-root plan."""

from __future__ import annotations

from typing import final

from ea.core.outcomes import OutcomeCode
from ea.core.runtime import (
    BoundedRuntimeRootPlan,
    RuntimeOrderingError,
    RuntimeRoot,
    _require_bounded_runtime_root_plan,
)


@final
class DeterministicRootQueue:
    """Consume one immutable bounded plan without insertion or reordering."""

    __slots__ = ("_index", "_plan")

    def __init__(self, plan: BoundedRuntimeRootPlan) -> None:
        self._plan = _require_bounded_runtime_root_plan(plan)
        self._index = 0

    @property
    def remaining(self) -> int:
        return len(self._plan) - self._index

    def _current(self) -> RuntimeRoot:
        if self._index >= len(self._plan):
            raise RuntimeOrderingError(
                OutcomeCode.OUT_OF_RANGE,
                "bounded runtime root queue is exhausted",
            )
        return self._plan.roots[self._index]

    def peek(self) -> RuntimeRoot:
        """Return the next root without advancing."""
        return self._current()

    def pop(self) -> RuntimeRoot:
        """Return the next root exactly once."""
        root = self._current()
        self._index += 1
        return root
