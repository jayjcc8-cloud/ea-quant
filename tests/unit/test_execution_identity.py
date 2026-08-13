from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from ea.core import (
    EconomicId,
    EconomicOwnerKind,
    ExecutionIdentityError,
    ExternalFactId,
    FactDedupKey,
    IdentityDisposition,
    IngressIdentity,
    OutcomeCode,
    RunId,
    SourceNamespace,
    SourceNativeSequence,
    canonical_economic_id_bytes,
    canonical_fact_dedup_identity_bytes,
    canonical_fact_dedup_key_bytes,
    canonical_ingress_identity_bytes,
    classify_identity_replay,
    economic_id_digest,
    fact_dedup_identity_digest,
    fact_dedup_key_digest,
    ingress_identity_digest,
    require_owner_kind,
    require_same_run,
)

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
SOURCE = SourceNamespace("sim.primary")


def _assert_code(
    error: pytest.ExceptionInfo[ExecutionIdentityError],
    code: OutcomeCode,
) -> None:
    assert error.value.code is code


class _IntSubclass(int):
    pass


class _StringSubclass(str):
    pass


@pytest.mark.parametrize("sequence", [-1, 1 << 64])
def test_economic_id_rejects_owner_sequence_outside_uint64(sequence: int) -> None:
    with pytest.raises(ExecutionIdentityError) as error:
        EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, sequence)

    _assert_code(error, OutcomeCode.OUT_OF_RANGE)


@pytest.mark.parametrize("sequence", [True, 1.0, _IntSubclass(1)])
def test_economic_id_requires_exact_owner_sequence_type(sequence: object) -> None:
    with pytest.raises(ExecutionIdentityError) as error:
        EconomicId(
            RUN_ID,
            EconomicOwnerKind.PORTFOLIO_INTENT,
            cast(int, sequence),
        )

    _assert_code(error, OutcomeCode.INVALID_TYPE)


def test_economic_id_bytes_and_digest_are_frozen() -> None:
    identity = EconomicId(
        RUN_ID,
        EconomicOwnerKind.PORTFOLIO_INTENT,
        (1 << 64) - 1,
    )
    expected = (
        b'{"canonicalization":"ea-economic-id-v1",'
        b'"owner_kind":"portfolio.intent","owner_sequence":18446744073709551615,'
        b'"run_id":"12345678-1234-4234-8234-123456789abc","schema_version":1}'
    )

    assert canonical_economic_id_bytes(identity) == expected
    assert (
        economic_id_digest(identity).value
        == "f1f62ff244009e4c884bf086e735ae0afd0c33ef7667f3383cdd9e7956bce62a"
    )


def test_reconciliation_owner_kinds_are_closed_run_scoped_identities() -> None:
    authorization = EconomicId(
        RUN_ID,
        EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
        1,
    )
    adjustment = EconomicId(
        RUN_ID,
        EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
        2,
    )

    assert authorization.owner_kind.value == "reconciliation.authorization"
    assert adjustment.owner_kind.value == "reconciliation.adjustment"


def test_economic_id_digest_changes_for_every_identity_component() -> None:
    baseline = EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 1)
    changed = (
        EconomicId(OTHER_RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 1),
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_ORDER, 1),
        EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 2),
    )

    assert all(economic_id_digest(value) != economic_id_digest(baseline) for value in changed)


def test_economic_id_is_immutable_and_defines_no_total_order() -> None:
    identity = EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 1)

    with pytest.raises(FrozenInstanceError):
        identity.owner_sequence = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        _ = identity < EconomicId(  # type: ignore[operator]
            RUN_ID,
            EconomicOwnerKind.PORTFOLIO_INTENT,
            2,
        )


def test_owner_and_run_binding_helpers_fail_with_conflicting_id() -> None:
    intent = EconomicId(RUN_ID, EconomicOwnerKind.PORTFOLIO_INTENT, 1)
    other_run = EconomicId(OTHER_RUN_ID, EconomicOwnerKind.RISK_DECISION, 1)

    assert require_owner_kind(intent, EconomicOwnerKind.PORTFOLIO_INTENT) is intent

    with pytest.raises(ExecutionIdentityError) as owner_error:
        require_owner_kind(intent, EconomicOwnerKind.EXECUTION_ORDER)
    _assert_code(owner_error, OutcomeCode.CONFLICTING_ID)

    with pytest.raises(ExecutionIdentityError) as run_error:
        require_same_run(intent, other_run)
    _assert_code(run_error, OutcomeCode.CONFLICTING_ID)


@pytest.mark.parametrize(
    "value",
    ["", "Sim", "sim/source", "sim source", "é", "a" * 129],
)
def test_source_namespace_has_one_closed_grammar(value: str) -> None:
    with pytest.raises(ExecutionIdentityError) as error:
        SourceNamespace(value)

    _assert_code(error, OutcomeCode.OUT_OF_RANGE)


@pytest.mark.parametrize("value", ["", "has space", "\n", "é", "a" * 129])
def test_external_fact_id_rejects_non_visible_ascii_or_invalid_length(value: str) -> None:
    with pytest.raises(ExecutionIdentityError) as error:
        ExternalFactId(value)

    _assert_code(error, OutcomeCode.OUT_OF_RANGE)


def test_external_fact_id_requires_exact_str_and_freezes_json_escaping() -> None:
    with pytest.raises(ExecutionIdentityError) as error:
        ExternalFactId(_StringSubclass("vendor-1"))
    _assert_code(error, OutcomeCode.INVALID_TYPE)

    identity = ExternalFactId('trade"\\42')
    assert canonical_fact_dedup_identity_bytes(identity) == (
        b'{"canonicalization":"ea-fact-dedup-id-v1","kind":"external_id",'
        b'"schema_version":1,"value":"trade\\"\\\\42"}'
    )
    assert (
        fact_dedup_identity_digest(identity).value
        == "2aa9ca018a5e939072d851a6ecbb4331cc2ae3387f2aef364c5328a2a6e2b737"
    )


def test_native_and_ingress_sequences_are_unbounded_non_negative_integers() -> None:
    above_uint64 = (1 << 256) + 7
    native = SourceNativeSequence(above_uint64)
    ingress = IngressIdentity(SOURCE, above_uint64)

    assert canonical_fact_dedup_identity_bytes(native) == (
        b'{"canonicalization":"ea-fact-dedup-id-v1","kind":"source_native_sequence",'
        b'"schema_version":1,"value":' + str(above_uint64).encode("ascii") + b"}"
    )
    assert canonical_ingress_identity_bytes(ingress) == (
        b'{"canonicalization":"ea-execution-ingress-id-v1","ingress_sequence":'
        + str(above_uint64).encode("ascii")
        + b',"schema_version":1,"source_namespace":"sim.primary"}'
    )

    for factory in (
        lambda: SourceNativeSequence(-1),
        lambda: IngressIdentity(SOURCE, -1),
    ):
        with pytest.raises(ExecutionIdentityError) as error:
            factory()
        _assert_code(error, OutcomeCode.OUT_OF_RANGE)

    for factory in (
        lambda: SourceNativeSequence(cast(int, True)),
        lambda: IngressIdentity(SOURCE, cast(int, _IntSubclass(1))),
    ):
        with pytest.raises(ExecutionIdentityError) as error:
            factory()
        _assert_code(error, OutcomeCode.INVALID_TYPE)


def test_unbounded_sequence_bytes_do_not_depend_on_python_int_string_limit() -> None:
    huge = 10**5000

    native_bytes = canonical_fact_dedup_identity_bytes(SourceNativeSequence(huge))
    native_digits = native_bytes.split(b'"value":', 1)[1][:-1]
    assert len(native_digits) == 5001
    assert native_digits[:1] == b"1"
    assert native_digits[1:] == b"0" * 5000

    ingress_bytes = canonical_ingress_identity_bytes(IngressIdentity(SOURCE, huge))
    ingress_digits = ingress_bytes.split(b'"ingress_sequence":', 1)[1].split(b",", 1)[0]
    assert ingress_digits == native_digits


def test_source_scoped_fact_key_has_frozen_bytes_and_digest() -> None:
    key = FactDedupKey(SOURCE, ExternalFactId("trade-42"))
    expected = (
        b'{"canonicalization":"ea-fact-dedup-key-v1",'
        b'"identity":{"kind":"external_id","value":"trade-42"},'
        b'"schema_version":1,"source_namespace":"sim.primary"}'
    )

    assert canonical_fact_dedup_key_bytes(key) == expected
    assert (
        fact_dedup_key_digest(key).value
        == "64a3f0f533b6100c0632e2f9a10af0c6e4c9e8926f1b82b92088c358b90f75c7"
    )


def test_ingress_identity_digest_changes_with_delivery_but_fact_key_does_not() -> None:
    first_ingress = IngressIdentity(SOURCE, 1)
    redelivery = IngressIdentity(SOURCE, 2)
    key = FactDedupKey(SOURCE, ExternalFactId("trade-42"))

    assert ingress_identity_digest(first_ingress) != ingress_identity_digest(redelivery)
    assert fact_dedup_key_digest(key) == fact_dedup_key_digest(
        FactDedupKey(SOURCE, ExternalFactId("trade-42"))
    )


def test_fact_key_is_source_scoped_and_tagged() -> None:
    external = FactDedupKey(SOURCE, ExternalFactId("42"))
    native = FactDedupKey(SOURCE, SourceNativeSequence(42))
    other_source = FactDedupKey(SourceNamespace("sim.secondary"), ExternalFactId("42"))

    assert external != native
    assert external != other_source
    assert canonical_fact_dedup_key_bytes(external) != canonical_fact_dedup_key_bytes(native)


@pytest.mark.parametrize(
    ("existing_payload", "candidate_payload", "expected"),
    [
        (b"", b"", IdentityDisposition.EXACT_REPLAY),
        (b"same", b"same", IdentityDisposition.EXACT_REPLAY),
        (b"first", b"second", IdentityDisposition.CONFLICT),
    ],
)
def test_replay_classification_uses_same_key_and_exact_bytes(
    existing_payload: bytes,
    candidate_payload: bytes,
    expected: IdentityDisposition,
) -> None:
    key = FactDedupKey(SOURCE, ExternalFactId("trade-42"))

    assert classify_identity_replay(key, existing_payload, key, candidate_payload) is expected
    assert classify_identity_replay(key, candidate_payload, key, existing_payload) is expected


def test_replay_classification_distinguishes_source_and_ingress_identity() -> None:
    first = FactDedupKey(SOURCE, ExternalFactId("42"))
    other_source = FactDedupKey(SourceNamespace("sim.secondary"), ExternalFactId("42"))

    assert (
        classify_identity_replay(first, b"same", other_source, b"same") is IdentityDisposition.NEW
    )
    assert (
        classify_identity_replay(
            IngressIdentity(SOURCE, 1),
            b"same",
            IngressIdentity(SOURCE, 2),
            b"same",
        )
        is IdentityDisposition.NEW
    )


@pytest.mark.parametrize(
    "arguments",
    [
        (ExternalFactId("42"), b"", ExternalFactId("42"), b""),
        (
            EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_ORDER, 1),
            b"",
            IngressIdentity(SOURCE, 1),
            b"",
        ),
        (
            FactDedupKey(SOURCE, ExternalFactId("42")),
            bytearray(),
            FactDedupKey(SOURCE, ExternalFactId("42")),
            b"",
        ),
        (
            FactDedupKey(SOURCE, ExternalFactId("42")),
            b"",
            FactDedupKey(SOURCE, ExternalFactId("42")),
            memoryview(b""),
        ),
    ],
)
def test_replay_classification_checks_all_exact_types_before_comparison(
    arguments: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ExecutionIdentityError) as error:
        classify_identity_replay(
            cast(EconomicId, arguments[0]),
            cast(bytes, arguments[1]),
            cast(EconomicId, arguments[2]),
            cast(bytes, arguments[3]),
        )

    _assert_code(error, OutcomeCode.INVALID_TYPE)
