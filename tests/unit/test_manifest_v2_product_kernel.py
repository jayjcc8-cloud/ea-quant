"""Focused v2 manifest contracts for the funded product boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ea.composition.product_kernel import (
    ProductKernelError,
    ProductKernelFailureCode,
    prepare_phase1_product_kernel,
    recover_phase1_product_kernel,
)
from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency, instrument_spec_set_digest
from ea.core.execution_messages import ExecutionPolicyId, ExecutionPolicyRef
from ea.core.initial_funding import InitialFundingSpec
from ea.core.risk import Phase1RiskPolicy, RiskPolicyId, create_phase1_risk_policy
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
from ea.experiments.store import LocalResultStore
from unit.test_portfolio_ledger import RUN_ID, _spec_set


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


def _product_inputs() -> tuple[object, object, ExecutionPolicyRef, Phase1RiskPolicy]:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    spec = build_lineage_spec_v2(
        LineageInputsV2(
            configuration=NormalizedConfiguration(1, "development", "backtest"),
            data=DataFingerprint(Sha256Digest("22" * 32), 1),
            replay_window=ReplayWindow(
                datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
            ),
            parameters=(EffectiveParameter.from_value("strategy.limit", 1),),
            runtime=InstalledRuntimeSpecV2(
                DistributionIdentity("ea-quant", "0.2.0"),
                Sha256Digest("33" * 32),
                "CPython",
                "3.12.0",
                "cpython-312",
                "linux",
                "manylinux",
                (DistributionIdentity("ea-quant", "0.2.0"),),
            ),
            randomness_seed=7,
            stream_labels=("strategy",),
            scenario_sha256=Sha256Digest("44" * 32),
            initial_funding=funding,
        )
    )
    execution = ExecutionPolicyRef(ExecutionPolicyId("phase1.execution.v1"), Sha256Digest("1" * 64))
    risk = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution,
        instrument_limits=(),
    )
    return spec, spec_set, execution, risk


def test_product_kernel_prepares_and_recovers_the_funded_prefix(tmp_path: Path) -> None:
    spec, spec_set, execution, risk = _product_inputs()
    root = tmp_path / "runs"
    root.mkdir()
    first = prepare_phase1_product_kernel(
        store=LocalResultStore(root),
        spec=spec,
        run_id_provider=lambda: UUID(RUN_ID.value),
        spec_set=spec_set,
        execution_policy=execution,
        risk_policy=risk,
    )
    manifest = read_manifest(
        first.binding.manifest_sha256
        and (tmp_path / "runs" / RUN_ID.value / "manifest.json").read_bytes()
    )
    first._handoff.journal.close()  # type: ignore[attr-defined]
    first._handoff.retire()  # type: ignore[attr-defined]
    recovered = recover_phase1_product_kernel(
        store=LocalResultStore(root),
        expected_manifest=manifest,
        spec_set=spec_set,
        execution_policy=execution,
        risk_policy=risk,
    )
    assert first.funding_outcome == recovered.funding_outcome
    assert first.portfolio_snapshot == recovered.portfolio_snapshot
    assert first.risk_state == recovered.risk_state


def test_product_rejects_v1_manifest_recovery() -> None:
    with pytest.raises(ProductKernelError) as error:
        recover_phase1_product_kernel(
            store=object(),
            expected_manifest=object(),
            spec_set=object(),
            execution_policy=object(),
            risk_policy=object(),
        )  # type: ignore[arg-type]
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_MANIFEST_V1
