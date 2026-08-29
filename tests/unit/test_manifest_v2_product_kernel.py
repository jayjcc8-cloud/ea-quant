"""Focused v2 manifest contracts for the funded product boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency
from ea.core.initial_funding import InitialFundingSpec
from ea.core.run import DataFingerprint, ReplayWindow, RunId, Sha256Digest
from ea.experiments.manifest import (
    DistributionIdentity,
    EffectiveParameter,
    InstalledRuntimeSpecV2,
    LineageInputsV2,
    NormalizedConfiguration,
    build_lineage_spec_v2,
    build_manifest_v2,
    canonical_manifest_bytes,
    read_manifest,
    read_manifest_v2,
)


def _manifest_v2():
    funding = InitialFundingSpec(
        InstrumentSpecSetId("phase1.test.v1"),
        Sha256Digest("11" * 32),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    inputs = LineageInputsV2(
        configuration=NormalizedConfiguration(1, "development", "backtest"),
        data=DataFingerprint(Sha256Digest("22" * 32), 1),
        replay_window=ReplayWindow(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        ),
        parameters=(EffectiveParameter.from_value("strategy.limit", 1),),
        runtime=InstalledRuntimeSpecV2(
            ea_distribution=DistributionIdentity("ea-quant", "0.2.0"),
            ea_installed_files_sha256=Sha256Digest("33" * 32),
            python_implementation="CPython",
            python_version="3.12.0",
            python_cache_tag="cpython-312",
            sys_platform="linux",
            platform_tag="manylinux",
            distributions=(DistributionIdentity("ea-quant", "0.2.0"),),
            numeric_policy="deterministic-ordered-float64-v1",
        ),
        randomness_seed=7,
        stream_labels=("strategy",),
        scenario_sha256=Sha256Digest("44" * 32),
        initial_funding=funding,
    )

    manifest = build_manifest_v2(
        build_lineage_spec_v2(inputs),
        RunId("12345678-1234-4234-8234-123456789abc"),
    )
    return manifest


def test_manifest_v2_binds_installed_runtime_scenario_and_funding() -> None:
    manifest = _manifest_v2()
    encoded = canonical_manifest_bytes(manifest)

    assert manifest.manifest_schema_version == 2
    assert manifest.spec.initial_funding.amount == CanonicalDecimal("1000")
    assert b'"manifest_schema_version":2' in encoded
    assert b'"scenario_sha256":"' + b"44" * 32 + b'"' in encoded


def test_manifest_v2_reader_dispatches_and_requires_exact_canonical_bytes() -> None:
    manifest = _manifest_v2()
    encoded = canonical_manifest_bytes(manifest)

    assert read_manifest(encoded) == manifest
    assert read_manifest_v2(encoded) == manifest
