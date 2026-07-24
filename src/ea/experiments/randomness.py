"""Narrow raw-word PCG64 capabilities for deterministic component streams."""

from __future__ import annotations

import threading
from typing import Protocol

import numpy as np

from ea.core.run import RunContractError, derive_component_seed, validate_stream_label
from ea.experiments.manifest import LineageSpec

_MAX_UINT64 = (1 << 64) - 1
_FACTORY_SEAL = object()
_STREAM_SEAL = object()


def _current_owner() -> threading.Thread:
    """Return the stable object identity used to bind one stream owner."""
    return threading.current_thread()


class RawWordStream(Protocol):
    """Runtime-visible scalar random-word capability."""

    @property
    def label(self) -> str: ...

    def next_u64(self) -> int: ...


class _Pcg64RawStream:
    """One-owner scalar ``PCG64.random_raw(size=None)`` capability."""

    __slots__ = ("_bit_generator", "_label", "_owner_thread", "_use_lock")

    def __init__(self, seal: object, *, master_seed: int, label: str) -> None:
        if seal is not _STREAM_SEAL:
            raise RunContractError("PCG64 streams can only be issued by a lineage-bound factory")
        self._label = validate_stream_label(label)
        component_seed = derive_component_seed(master_seed, self._label)
        self._bit_generator = np.random.PCG64(component_seed)
        self._owner_thread: threading.Thread | None = None
        self._use_lock = threading.Lock()

    @property
    def label(self) -> str:
        return self._label

    def next_u64(self) -> int:
        """Return one raw word and reject cross-thread or concurrent consumption."""
        if not self._use_lock.acquire(blocking=False):
            raise RunContractError("a PCG64 stream cannot be invoked concurrently")
        try:
            owner = _current_owner()
            if self._owner_thread is None:
                self._owner_thread = owner
            elif self._owner_thread is not owner:
                raise RunContractError("a PCG64 stream cannot be shared across component threads")
            value = int(self._bit_generator.random_raw(size=None))
        finally:
            self._use_lock.release()
        if value < 0 or value > _MAX_UINT64:  # defensive boundary around NumPy
            raise RunContractError("PCG64 produced a value outside uint64")
        return value


class Pcg64StreamFactory:
    """Claim each label from one validated lineage specification exactly once."""

    __slots__ = ("_claim_lock", "_claimed", "_labels", "_master_seed")

    def __init__(
        self,
        seal: object,
        *,
        master_seed: int,
        stream_labels: tuple[str, ...],
    ) -> None:
        if seal is not _FACTORY_SEAL:
            raise RunContractError("PCG64 factories must be derived from a lineage specification")
        self._master_seed = master_seed
        self._labels = stream_labels
        self._claimed: set[str] = set()
        self._claim_lock = threading.Lock()

    @classmethod
    def from_lineage(cls, spec: LineageSpec) -> Pcg64StreamFactory:
        """Bind the only stream factory to the seed and labels persisted in lineage."""
        if type(spec) is not LineageSpec:
            raise RunContractError("random stream factory requires a LineageSpec")
        randomness = spec.randomness
        # The validated spec already guarantees sorted, unique labels and a uint64 seed.
        return cls(
            _FACTORY_SEAL,
            master_seed=randomness.master_seed,
            stream_labels=randomness.stream_labels,
        )

    @property
    def master_seed(self) -> int:
        return self._master_seed

    @property
    def stream_labels(self) -> tuple[str, ...]:
        return self._labels

    def claim(self, label: str) -> RawWordStream:
        """Return the sole capability for one declared label."""
        canonical = validate_stream_label(label)
        if canonical not in self._labels:
            raise RunContractError("stream label was not declared in run provenance")
        with self._claim_lock:
            if canonical in self._claimed:
                raise RunContractError("stream label already has an owner")
            self._claimed.add(canonical)
        return _Pcg64RawStream(
            _STREAM_SEAL,
            master_seed=self._master_seed,
            label=canonical,
        )
