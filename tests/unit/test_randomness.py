from __future__ import annotations

import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ea.core import DataFingerprint, ReplayWindow, RunContractError, Sha256Digest
from ea.experiments import randomness as randomness_module
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    LineageInputs,
    LineageSpec,
    NormalizedConfiguration,
    RuntimeEvidence,
    build_lineage_spec,
)
from ea.experiments.randomness import Pcg64StreamFactory


def _spec(
    *,
    master_seed: int = 0,
    stream_labels: tuple[str, ...] = ("matcher.primary",),
) -> LineageSpec:
    return build_lineage_spec(
        LineageInputs(
            code=CodeEvidence("0123456789abcdef0123456789abcdef01234567"),
            configuration=NormalizedConfiguration(1, "development", "backtest"),
            data=DataFingerprint(Sha256Digest("1" * 64), 1),
            replay_window=ReplayWindow(
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 2, 1, tzinfo=UTC),
            ),
            parameters=(),
            runtime=RuntimeEvidence(
                ea_version="0.1.1",
                python_implementation="cpython",
                python_version="3.12.13",
                python_cache_tag="cpython-312",
                sys_platform="darwin",
                platform_tag="macosx-11.0-arm64",
                distributions=(DistributionIdentity("ea-quant", "0.1.1"),),
                uv_lock_bytes=b"version = 1\n",
            ),
            master_seed=master_seed,
            stream_labels=stream_labels,
        )
    )


def test_raw_stream_matches_normative_words_and_exposes_no_generator_api() -> None:
    stream = Pcg64StreamFactory.from_lineage(_spec()).claim("matcher.primary")

    assert [stream.next_u64() for _ in range(4)] == [
        1256042036395257240,
        5315971738016275038,
        8151513107568622237,
        4281872282868094881,
    ]
    assert not hasattr(stream, "random")
    assert not hasattr(stream, "normal")
    assert not hasattr(stream, "random_raw")


def test_uuid_generation_does_not_consume_or_change_economic_rng() -> None:
    lineage = _spec(stream_labels=("strategy.primary",))
    baseline = Pcg64StreamFactory.from_lineage(lineage).claim("strategy.primary")
    compared = Pcg64StreamFactory.from_lineage(lineage).claim("strategy.primary")

    expected = [baseline.next_u64() for _ in range(4)]
    uuid4()
    uuid4()
    actual = [compared.next_u64() for _ in range(4)]

    assert (
        expected
        == actual
        == [
            530788929622977885,
            9219954638712931725,
            6538790841956837850,
            11941987564432633371,
        ]
    )


def test_factory_sorts_labels_and_rejects_duplicate_or_undeclared_owners() -> None:
    factory = Pcg64StreamFactory.from_lineage(
        _spec(stream_labels=("strategy.primary", "matcher.primary"))
    )

    assert factory.stream_labels == ("matcher.primary", "strategy.primary")
    factory.claim("matcher.primary")
    with pytest.raises(RunContractError, match="already has an owner"):
        factory.claim("matcher.primary")
    with pytest.raises(RunContractError, match="not declared"):
        factory.claim("risk.primary")
    with pytest.raises(RunContractError, match="must be derived"):
        Pcg64StreamFactory(
            object(),
            master_seed=0,
            stream_labels=("matcher.primary",),
        )


def test_stream_rejects_cross_thread_consumption() -> None:
    stream = Pcg64StreamFactory.from_lineage(_spec()).claim("matcher.primary")
    stream.next_u64()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            stream.next_u64()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    worker.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RunContractError)


def test_recycled_thread_ident_does_not_transfer_stream_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = Pcg64StreamFactory.from_lineage(_spec()).claim("matcher.primary")
    first_owner = threading.Thread(name="first-owner")
    second_owner = threading.Thread(name="second-owner")
    owners = iter((first_owner, second_owner, first_owner))
    monkeypatch.setattr(randomness_module, "_current_owner", lambda: next(owners))

    assert stream.next_u64() == 1256042036395257240
    with pytest.raises(RunContractError, match="shared across component threads"):
        stream.next_u64()
    # A rejected owner must not consume a raw word.
    assert stream.next_u64() == 5315971738016275038


def test_simultaneous_first_use_has_one_owner_and_one_failure() -> None:
    stream = Pcg64StreamFactory.from_lineage(_spec()).claim("matcher.primary")
    barrier = threading.Barrier(3)
    values: list[int] = []
    errors: list[BaseException] = []

    def consume() -> None:
        barrier.wait()
        try:
            values.append(stream.next_u64())
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=consume) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert len(values) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RunContractError)


def test_simultaneous_claim_issues_exactly_one_stream() -> None:
    factory = Pcg64StreamFactory.from_lineage(_spec())
    barrier = threading.Barrier(3)
    streams: list[object] = []
    errors: list[BaseException] = []

    def claim() -> None:
        barrier.wait()
        try:
            streams.append(factory.claim("matcher.primary"))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert len(streams) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RunContractError)
