"""Focused v2 manifest contracts for the funded product boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency
from ea.core.initial_funding import InitialFundingSpec
from ea.core.run import DataFingerprint, ReplayWindow, RunId, Sha256Digest
from ea.experiments._manifest_model import (
    DistributionIdentity,
    EffectiveParameter,
    InstalledRuntimeSpecV2,
    LineageInputsV2,
    NormalizedConfiguration,
    build_lineage_spec_v2,
    build_manifest_v2,
    canonical_manifest_bytes,
)
from ea.experiments.manifest import read_manifest, read_manifest_v2
from ea.experiments.provenance import ProvenanceError, _validate_installed_direct_url


def test_manifest_v2_binds_installed_runtime_scenario_and_funding() -> None:
    funding = InitialFundingSpec(
        InstrumentSpecSetId("phase1.test.v1"),
        Sha256Digest("11" * 32),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    runtime = InstalledRuntimeSpecV2(
        ea_distribution=DistributionIdentity("ea-quant", "0.2.0"),
        ea_installed_files_sha256=Sha256Digest("33" * 32),
        python_implementation="CPython",
        python_version="3.12.0",
        python_cache_tag="cpython-312",
        sys_platform="linux",
        platform_tag="manylinux",
        distributions=(DistributionIdentity("ea-quant", "0.2.0"),),
    )
    manifest = build_manifest_v2(
        build_lineage_spec_v2(
            LineageInputsV2(
                configuration=NormalizedConfiguration(1, "development", "backtest"),
                data=DataFingerprint(Sha256Digest("22" * 32), 1),
                replay_window=ReplayWindow(
                    datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
                ),
                parameters=(EffectiveParameter.from_value("strategy.limit", 1),),
                runtime=runtime,
                randomness_seed=7,
                stream_labels=("strategy",),
                scenario_sha256=Sha256Digest("44" * 32),
                initial_funding=funding,
            )
        ),
        RunId("12345678-1234-4234-8234-123456789abc"),
    )

    assert manifest.manifest_schema_version == 2
    assert manifest.spec.initial_funding.amount == CanonicalDecimal("1000")
    encoded = canonical_manifest_bytes(manifest)
    assert b'"scenario_sha256":"' + b"44" * 32 + b'"' in encoded
    assert read_manifest(encoded) == manifest
    assert read_manifest_v2(encoded) == manifest


def test_installed_direct_url_accepts_only_an_absolute_local_wheel() -> None:
    class Distribution:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return self.value

    _validate_installed_direct_url(Distribution(None))  # type: ignore[arg-type]
    _validate_installed_direct_url(Distribution('{"archive_info":{},"url":"file:///tmp/ea.whl"}'))  # type: ignore[arg-type]
    for url in (
        "https://example/ea.whl",
        "file:///tmp/a%zz.whl",
        "file:///tmp/a%00.whl",
        "file:///tmp/a b.whl",
    ):
        with pytest.raises(ProvenanceError):
            _validate_installed_direct_url(Distribution('{"archive_info":{},"url":"' + url + '"}'))  # type: ignore[arg-type]
