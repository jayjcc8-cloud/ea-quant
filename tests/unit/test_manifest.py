from __future__ import annotations

import json
import pickle
import re
import struct
import unicodedata
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from typing import NoReturn

import pytest

from ea.core import DataFingerprint, ReplayWindow, RunId, Sha256Digest
from ea.experiments import _manifest_codec, _manifest_evidence, _manifest_model
from ea.experiments import manifest as manifest_module
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    EffectiveParameter,
    EvidenceMismatchError,
    LineageInputs,
    ManifestError,
    ManifestFormatError,
    NormalizedConfiguration,
    RuntimeEvidence,
    build_lineage_spec,
    build_manifest,
    canonical_lineage_bytes,
    canonical_manifest_bytes,
    read_manifest,
    verify_manifest_evidence,
)

DATA_DIGEST = "ef22439b2e2fa38e22cea9f544b0c29f1908827d62487815b486163c3c0a1624"
RUN_ID = RunId("123e4567-e89b-42d3-a456-426614174000")
OTHER_RUN_ID = RunId("123e4567-e89b-42d3-b456-426614174000")
LOCK_BYTES = b"version = 1\n"
GOLDEN_LINEAGE = (
    b'{"code":{"commit":"0123456789abcdef0123456789abcdef01234567",'
    b'"worktree_clean":true},"configuration":{"canonicalization":"ea-settings-v1",'
    b'"normalized":{"environment":"development","run":{"mode":"backtest"},'
    b'"schema_version":1},"sha256":'
    b'"d9b133a06fe8ccf587e75ea4dd1c9edc8f6a2af4e39cf9badf323446b79cf66a"},'
    b'"data":{"canonicalization":"ea-market-data-envelope-v1","record_count":1,'
    b'"sha256":"ef22439b2e2fa38e22cea9f544b0c29f1908827d62487815b486163c3c0a1624"},'
    b'"lineage_schema_version":1,"parameters":['
    b'{"name":"matcher.enabled","type":"boolean","value":true},'
    b'{"name":"strategy.label","type":"string","value":"baseline"},'
    b'{"name":"strategy.lookback","type":"integer","value":20},'
    b'{"name":"strategy.threshold","type":"float64","value":"3fb999999999999a"}],'
    b'"randomness":{"generator":"numpy-pcg64","master_seed":0,'
    b'"stream_derivation":"ea-sha256-component-label-v1",'
    b'"stream_labels":["matcher.primary","strategy.primary"]},'
    b'"replay_window":{"end_exclusive":"2026-02-01T00:00:00.000000Z",'
    b'"initial_state":"empty","selection_field":"available_at",'
    b'"start_inclusive":"2026-01-01T00:00:00.000000Z","timezone":"UTC"},'
    b'"runtime":{"distributions":[{"name":"ea-quant","version":"0.1.1"}],'
    b'"ea_version":"0.1.1","numeric_policy":"deterministic-ordered-float64-v1",'
    b'"platform_tag":"macosx-11.0-arm64","python_cache_tag":"cpython-312",'
    b'"python_implementation":"cpython","python_version":"3.12.13",'
    b'"sys_platform":"darwin","uv_lock_sha256":'
    b'"d50f516b162e487ee268e4540483fb77ba119a588a10135066a29e23edbcb531"}}'
)
GOLDEN_MANIFEST = (
    b'{"lineage_sha256":'
    b'"b408ac8218e19b3ef4b066d9a4f9d6abb1ff1120fdedf01212843f573c977ccf",'
    b'"manifest_schema_version":1,"run_id":"123e4567-e89b-42d3-a456-426614174000",'
    b'"spec":' + GOLDEN_LINEAGE + b"}"
)


def _runtime(*, lock_bytes: bytes = LOCK_BYTES) -> RuntimeEvidence:
    return RuntimeEvidence(
        ea_version="0.1.1",
        python_implementation="cpython",
        python_version="3.12.13",
        python_cache_tag="cpython-312",
        sys_platform="darwin",
        platform_tag="macosx-11.0-arm64",
        distributions=(DistributionIdentity("ea-quant", "0.1.1"),),
        uv_lock_bytes=lock_bytes,
    )


def _inputs(
    *,
    code: CodeEvidence | None = None,
    runtime: RuntimeEvidence | None = None,
    parameters: tuple[EffectiveParameter, ...] | None = None,
    stream_labels: tuple[str, ...] = ("strategy.primary", "matcher.primary"),
) -> LineageInputs:
    if parameters is None:
        parameters = (
            EffectiveParameter.from_value("strategy.threshold", 0.1),
            EffectiveParameter.from_value("matcher.enabled", True),
            EffectiveParameter.from_value("strategy.lookback", 20),
            EffectiveParameter.from_value("strategy.label", "baseline"),
        )
    return LineageInputs(
        code=code or CodeEvidence("0123456789abcdef0123456789abcdef01234567"),
        configuration=NormalizedConfiguration(1, "development", "backtest"),
        data=DataFingerprint(Sha256Digest(DATA_DIGEST), 1),
        replay_window=ReplayWindow(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 2, 1, tzinfo=UTC),
        ),
        parameters=parameters,
        runtime=runtime or _runtime(),
        master_seed=0,
        stream_labels=stream_labels,
    )


def _manifest_bytes() -> bytes:
    return canonical_manifest_bytes(build_manifest(build_lineage_spec(_inputs()), RUN_ID))


def test_complete_lineage_and_manifest_match_literal_goldens() -> None:
    spec = build_lineage_spec(_inputs())
    manifest = build_manifest(spec, RUN_ID)

    assert canonical_lineage_bytes(spec) == GOLDEN_LINEAGE
    assert len(GOLDEN_LINEAGE) == 1470
    assert (
        manifest.lineage_sha256.value
        == "b408ac8218e19b3ef4b066d9a4f9d6abb1ff1120fdedf01212843f573c977ccf"
    )
    assert canonical_manifest_bytes(manifest) == GOLDEN_MANIFEST
    assert len(GOLDEN_MANIFEST) == 1639
    assert (
        sha256(GOLDEN_MANIFEST).hexdigest()
        == "536e0876d28d949cf105dc194d55ae017db75b506a0dd52eaf3b373878d9f468"
    )


def test_config_and_lock_domains_match_literal_goldens() -> None:
    spec = build_lineage_spec(_inputs())

    assert (
        spec.configuration.sha256.value
        == "d9b133a06fe8ccf587e75ea4dd1c9edc8f6a2af4e39cf9badf323446b79cf66a"
    )
    assert (
        spec.runtime.uv_lock_sha256.value
        == "d50f516b162e487ee268e4540483fb77ba119a588a10135066a29e23edbcb531"
    )


def test_input_order_does_not_change_lineage_but_uuid_changes_attempt() -> None:
    parameters = _inputs().parameters
    forward = build_lineage_spec(
        _inputs(parameters=parameters, stream_labels=("strategy.primary", "matcher.primary"))
    )
    reverse = build_lineage_spec(
        _inputs(
            parameters=tuple(reversed(parameters)),
            stream_labels=("matcher.primary", "strategy.primary"),
        )
    )

    first = build_manifest(forward, RUN_ID)
    second = build_manifest(reverse, OTHER_RUN_ID)

    assert forward == reverse
    assert first.lineage_sha256 == second.lineage_sha256
    assert first.run_id != second.run_id
    assert canonical_manifest_bytes(first) != canonical_manifest_bytes(second)


def test_manifest_round_trip_is_strict_and_transitively_immutable() -> None:
    parsed = read_manifest(GOLDEN_MANIFEST)

    assert canonical_manifest_bytes(parsed) == GOLDEN_MANIFEST
    with pytest.raises(FrozenInstanceError):
        parsed.spec.runtime.ea_version = "changed"  # type: ignore[misc]


def test_manifest_facade_preserves_public_object_identity_and_pickle_paths() -> None:
    expected = {
        "ManifestError": _manifest_model.ManifestError,
        "ManifestFormatError": _manifest_model.ManifestFormatError,
        "EvidenceMismatchError": _manifest_model.EvidenceMismatchError,
        "NormalizedConfiguration": _manifest_model.NormalizedConfiguration,
        "CodeEvidence": _manifest_model.CodeEvidence,
        "DistributionIdentity": _manifest_model.DistributionIdentity,
        "RuntimeEvidence": _manifest_model.RuntimeEvidence,
        "ParameterKind": _manifest_model.ParameterKind,
        "EffectiveParameter": _manifest_model.EffectiveParameter,
        "LineageInputs": _manifest_model.LineageInputs,
        "CodeSpec": _manifest_model.CodeSpec,
        "ConfigurationSpec": _manifest_model.ConfigurationSpec,
        "RuntimeSpec": _manifest_model.RuntimeSpec,
        "RandomnessSpec": _manifest_model.RandomnessSpec,
        "LineageSpec": _manifest_model.LineageSpec,
        "RunManifest": _manifest_model.RunManifest,
        "canonical_lineage_bytes": _manifest_model.canonical_lineage_bytes,
        "canonical_manifest_bytes": _manifest_model.canonical_manifest_bytes,
        "build_lineage_spec": _manifest_model.build_lineage_spec,
        "build_manifest": _manifest_model.build_manifest,
        "read_manifest": _manifest_codec.read_manifest,
        "verify_manifest_evidence": _manifest_evidence.verify_manifest_evidence,
    }

    for name, value in expected.items():
        assert getattr(manifest_module, name) is value
        assert value.__module__ == "ea.experiments.manifest"

    incidental_compatibility = {
        "json": json,
        "re": re,
        "struct": struct,
        "unicodedata": unicodedata,
        "Sequence": Sequence,
        "dataclass": dataclass,
        "UTC": UTC,
        "datetime": datetime,
        "StrEnum": StrEnum,
        "sha256": sha256,
        "isfinite": isfinite,
        "NoReturn": NoReturn,
    }
    for name, value in incidental_compatibility.items():
        assert getattr(manifest_module, name) is value

    for value in (CodeEvidence("0" * 40), _manifest_model.ParameterKind.INTEGER):
        restored = pickle.loads(pickle.dumps(value))
        assert type(restored) is type(value)
        assert restored == value

    error = ManifestFormatError("invalid")
    restored_error = pickle.loads(pickle.dumps(error))
    assert type(restored_error) is ManifestFormatError
    assert restored_error.args == error.args


@pytest.mark.parametrize(
    "mutated",
    [
        b" " + GOLDEN_MANIFEST,
        GOLDEN_MANIFEST + b"\n",
        b"\xef\xbb\xbf" + GOLDEN_MANIFEST,
        GOLDEN_MANIFEST.replace(b'"manifest_schema_version":1', b'"unknown":0', 1),
        GOLDEN_MANIFEST.replace(b'"mode":"backtest"', b'"mode":"paper"', 1),
        GOLDEN_MANIFEST.replace(b'"record_count":1', b'"record_count":true', 1),
        GOLDEN_MANIFEST.replace(b'"worktree_clean":true', b'"worktree_clean":1', 1),
        GOLDEN_MANIFEST.replace(b'"type":"float64"', b'"type":"number"', 1),
        GOLDEN_MANIFEST.replace(b'"3fb999999999999a"', b'"3FB999999999999A"', 1),
    ],
)
def test_strict_reader_rejects_noncanonical_unknown_or_wrong_typed_bytes(
    mutated: bytes,
) -> None:
    with pytest.raises(ManifestFormatError):
        read_manifest(mutated)


def test_strict_reader_rejects_duplicate_keys_before_hash_validation() -> None:
    duplicate = GOLDEN_MANIFEST.replace(
        b'{"lineage_sha256":',
        b'{"manifest_schema_version":1,"lineage_sha256":',
        1,
    )

    with pytest.raises(ManifestFormatError, match="duplicate JSON key"):
        read_manifest(duplicate)


def test_strict_reader_rejects_valid_json_with_noncanonical_key_order() -> None:
    parsed = json.loads(GOLDEN_MANIFEST)
    reordered = json.dumps(
        {
            "run_id": parsed["run_id"],
            "lineage_sha256": parsed["lineage_sha256"],
            "manifest_schema_version": parsed["manifest_schema_version"],
            "spec": parsed["spec"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(ManifestFormatError, match="not canonical"):
        read_manifest(reordered)


def test_strict_reader_rejects_tampered_hashes() -> None:
    tampered = GOLDEN_MANIFEST.replace(
        b'"strategy.lookback","type":"integer","value":20',
        b'"strategy.lookback","type":"integer","value":21',
        1,
    )

    with pytest.raises(ManifestFormatError, match="lineage digest"):
        read_manifest(tampered)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("strategy.nan", float("nan")),
        ("strategy.infinity", float("inf")),
        ("strategy.too_large", 1 << 63),
        ("Strategy.case", 1),
        ("strategy.control", "bad\nvalue"),
        ("strategy.non_nfc", "e\u0301"),
    ],
)
def test_effective_parameters_fail_closed(name: str, value: object) -> None:
    with pytest.raises(ManifestError):
        EffectiveParameter.from_value(name, value)  # type: ignore[arg-type]


def test_parameter_float_bits_preserve_negative_zero() -> None:
    spec = build_lineage_spec(
        _inputs(parameters=(EffectiveParameter.from_value("strategy.zero", -0.0),))
    )

    assert b'"value":"8000000000000000"' in canonical_lineage_bytes(spec)


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_manifest_v1_rejects_non_backtest_before_attempt_creation(mode: str) -> None:
    with pytest.raises(ManifestError, match="only run.mode backtest"):
        NormalizedConfiguration(1, "development", mode)


def test_external_evidence_verifier_recomputes_lock_code_runtime_and_data() -> None:
    manifest = build_manifest(build_lineage_spec(_inputs()), RUN_ID)
    verify_manifest_evidence(
        manifest,
        code=_inputs().code,
        runtime=_runtime(),
        data=_inputs().data,
    )

    with pytest.raises(EvidenceMismatchError):
        verify_manifest_evidence(
            manifest,
            code=CodeEvidence("f" * 40),
            runtime=_runtime(),
            data=_inputs().data,
        )
    with pytest.raises(EvidenceMismatchError):
        verify_manifest_evidence(
            manifest,
            code=_inputs().code,
            runtime=_runtime(lock_bytes=b"different"),
            data=_inputs().data,
        )
    with pytest.raises(EvidenceMismatchError):
        verify_manifest_evidence(
            manifest,
            code=_inputs().code,
            runtime=_runtime(),
            data=DataFingerprint(Sha256Digest("0" * 64), 1),
        )


def test_manifest_error_causality_boundaries_remain_stable() -> None:
    with pytest.raises(ManifestFormatError) as invalid_utf8:
        read_manifest(b"\xff")
    assert isinstance(invalid_utf8.value.__cause__, UnicodeDecodeError)

    duplicate = GOLDEN_MANIFEST.replace(
        b'{"lineage_sha256":',
        b'{"manifest_schema_version":1,"lineage_sha256":',
        1,
    )
    with pytest.raises(ManifestFormatError) as duplicate_key:
        read_manifest(duplicate)
    assert duplicate_key.value.__cause__ is None

    invalid_schema = GOLDEN_MANIFEST.replace(
        b'"manifest_schema_version":1',
        b'"manifest_schema_version":true',
        1,
    )
    with pytest.raises(ManifestFormatError) as wrapped_model_error:
        read_manifest(invalid_schema)
    assert type(wrapped_model_error.value.__cause__) is ManifestError

    manifest = build_manifest(build_lineage_spec(_inputs()), RUN_ID)
    with pytest.raises(EvidenceMismatchError) as invalid_runtime:
        verify_manifest_evidence(
            manifest,
            code=_inputs().code,
            runtime=object(),  # type: ignore[arg-type]
            data=_inputs().data,
        )
    assert isinstance(invalid_runtime.value.__cause__, AttributeError)

    with pytest.raises(EvidenceMismatchError) as ordinary_mismatch:
        verify_manifest_evidence(
            manifest,
            code=CodeEvidence("f" * 40),
            runtime=_runtime(),
            data=_inputs().data,
        )
    assert ordinary_mismatch.value.__cause__ is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            b'"spec":{"code":',
            b'"spec":{"lineage_schema_version":1,"code":',
        ),
        (
            b'"code":{"commit":',
            b'"code":{"worktree_clean":true,"commit":',
        ),
        (
            b'"configuration":{"canonicalization":',
            b'"configuration":{"canonicalization":"ea-settings-v1","canonicalization":',
        ),
        (
            b'"normalized":{"environment":',
            b'"normalized":{"schema_version":1,"environment":',
        ),
        (
            b'"run":{"mode":',
            b'"run":{"mode":"backtest","mode":',
        ),
        (
            b'"data":{"canonicalization":',
            b'"data":{"record_count":1,"canonicalization":',
        ),
        (
            b'{"name":"matcher.enabled","type":',
            b'{"name":"duplicate","name":"matcher.enabled","type":',
        ),
        (
            b'"randomness":{"generator":',
            b'"randomness":{"master_seed":0,"generator":',
        ),
        (
            b'"replay_window":{"end_exclusive":',
            b'"replay_window":{"timezone":"UTC","end_exclusive":',
        ),
        (
            b'"runtime":{"distributions":',
            b'"runtime":{"ea_version":"0.1.1","distributions":',
        ),
        (
            b'{"name":"ea-quant","version":',
            b'{"name":"duplicate","name":"ea-quant","version":',
        ),
    ],
)
def test_strict_reader_rejects_duplicate_keys_in_every_nested_shape(
    old: bytes,
    new: bytes,
) -> None:
    with pytest.raises(ManifestFormatError, match="duplicate JSON key"):
        read_manifest(GOLDEN_MANIFEST.replace(old, new, 1))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'"spec":{', b'"spec":{"unknown":0,'),
        (b'"code":{', b'"code":{"unknown":0,'),
        (b'"configuration":{', b'"configuration":{"unknown":0,'),
        (b'"normalized":{', b'"normalized":{"unknown":0,'),
        (b'"run":{', b'"run":{"unknown":0,'),
        (b'"data":{', b'"data":{"unknown":0,'),
        (
            b'{"name":"matcher.enabled"',
            b'{"unknown":0,"name":"matcher.enabled"',
        ),
        (b'"randomness":{', b'"randomness":{"unknown":0,'),
        (b'"replay_window":{', b'"replay_window":{"unknown":0,'),
        (b'"runtime":{', b'"runtime":{"unknown":0,'),
        (
            b'{"name":"ea-quant"',
            b'{"unknown":0,"name":"ea-quant"',
        ),
    ],
)
def test_strict_reader_rejects_unknown_keys_in_every_nested_shape(
    old: bytes,
    new: bytes,
) -> None:
    with pytest.raises(ManifestFormatError, match="missing or unknown"):
        read_manifest(GOLDEN_MANIFEST.replace(old, new, 1))


@pytest.mark.parametrize(
    "field",
    [
        b'"manifest_schema_version":1,',
        b'"lineage_schema_version":1,',
        b',"worktree_clean":true',
        b'"canonicalization":"ea-settings-v1",',
        b'"environment":"development",',
        b'"mode":"backtest"',
        b'"record_count":1,',
        b'"type":"boolean",',
        b'"generator":"numpy-pcg64",',
        b'"initial_state":"empty",',
        b'"ea_version":"0.1.1",',
        b',"version":"0.1.1"',
    ],
)
def test_strict_reader_rejects_missing_fields_at_every_schema_level(field: bytes) -> None:
    with pytest.raises(ManifestFormatError, match="missing or unknown"):
        read_manifest(GOLDEN_MANIFEST.replace(field, b"", 1))


def test_strict_reader_rejects_pep503_noncanonical_distribution_name() -> None:
    with pytest.raises(ManifestError, match="PEP 503"):
        DistributionIdentity("foo--bar", "1")

    mutated = GOLDEN_MANIFEST.replace(b'"ea-quant"', b'"ea--quant"', 1)
    with pytest.raises(ManifestFormatError, match="PEP 503"):
        read_manifest(mutated)


def test_strict_reader_rejects_semantically_equal_noncanonical_escapes_and_numbers() -> None:
    slash_spec = build_lineage_spec(
        _inputs(parameters=(EffectiveParameter.from_value("strategy.label", "a/b"),))
    )
    slash_manifest = canonical_manifest_bytes(build_manifest(slash_spec, RUN_ID))
    escaped_solidus = slash_manifest.replace(b'"a/b"', b'"a\\/b"', 1)

    unicode_spec = build_lineage_spec(
        _inputs(parameters=(EffectiveParameter.from_value("strategy.label", "caf\u00e9"),))
    )
    unicode_manifest = canonical_manifest_bytes(build_manifest(unicode_spec, RUN_ID))
    escaped_unicode = unicode_manifest.replace("é".encode(), b"\\u00e9", 1)

    negative_zero_integer = GOLDEN_MANIFEST.replace(
        b'"master_seed":0',
        b'"master_seed":-0',
        1,
    )
    for mutated in (escaped_solidus, escaped_unicode, negative_zero_integer):
        with pytest.raises(ManifestFormatError, match="not canonical"):
            read_manifest(mutated)


def test_every_mutable_lineage_input_changes_the_lineage_digest() -> None:
    baseline = _inputs()
    baseline_digest = build_manifest(build_lineage_spec(baseline), RUN_ID).lineage_sha256
    runtime = baseline.runtime
    parameters = baseline.parameters
    variants = [
        replace(baseline, code=CodeEvidence("f" * 40)),
        replace(
            baseline,
            configuration=NormalizedConfiguration(1, "staging", "backtest"),
        ),
        replace(
            baseline,
            data=DataFingerprint(Sha256Digest("0" * 64), 1),
        ),
        replace(
            baseline,
            data=DataFingerprint(baseline.data.sha256, 2),
        ),
        replace(
            baseline,
            replay_window=ReplayWindow(
                baseline.replay_window.start_inclusive.replace(day=2),
                baseline.replay_window.end_exclusive,
            ),
        ),
        replace(
            baseline,
            replay_window=ReplayWindow(
                baseline.replay_window.start_inclusive,
                baseline.replay_window.end_exclusive.replace(day=2),
            ),
        ),
        replace(
            baseline,
            parameters=tuple(
                EffectiveParameter.from_value("matcher.enabled", False)
                if item.name == "matcher.enabled"
                else item
                for item in parameters
            ),
        ),
        replace(
            baseline,
            parameters=tuple(
                EffectiveParameter.from_value("strategy.label", "changed")
                if item.name == "strategy.label"
                else item
                for item in parameters
            ),
        ),
        replace(
            baseline,
            parameters=tuple(
                EffectiveParameter.from_value("strategy.lookback", 21)
                if item.name == "strategy.lookback"
                else item
                for item in parameters
            ),
        ),
        replace(
            baseline,
            parameters=tuple(
                EffectiveParameter.from_value("strategy.threshold", 0.2)
                if item.name == "strategy.threshold"
                else item
                for item in parameters
            ),
        ),
        replace(
            baseline,
            parameters=tuple(
                EffectiveParameter.from_value("strategy.name", item.value)
                if item.name == "strategy.label"
                else item
                for item in parameters
            ),
        ),
        replace(
            baseline,
            runtime=replace(
                runtime,
                ea_version="0.1.2",
                distributions=(DistributionIdentity("ea-quant", "0.1.2"),),
            ),
        ),
        replace(baseline, runtime=replace(runtime, python_implementation="pypy")),
        replace(baseline, runtime=replace(runtime, python_version="3.12.12")),
        replace(baseline, runtime=replace(runtime, python_cache_tag="cpython-313")),
        replace(baseline, runtime=replace(runtime, sys_platform="linux")),
        replace(baseline, runtime=replace(runtime, platform_tag="manylinux_2_39_x86_64")),
        replace(
            baseline,
            runtime=replace(
                runtime,
                distributions=(
                    DistributionIdentity("ea-quant", "0.1.1"),
                    DistributionIdentity("numpy", "2.0.0"),
                ),
            ),
        ),
        replace(baseline, runtime=replace(runtime, uv_lock_bytes=b"different")),
        replace(baseline, master_seed=1),
        replace(baseline, stream_labels=("matcher.secondary",)),
    ]

    for variant in variants:
        assert build_manifest(build_lineage_spec(variant), RUN_ID).lineage_sha256 != baseline_digest
