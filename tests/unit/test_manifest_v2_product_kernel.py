"""Focused v2 manifest contracts for the funded product boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

import ea
import ea.experiments.provenance as provenance_module
from ea.composition.product_kernel import (
    _consume_phase1_product_kernel,
    prepare_phase1_product_kernel,
    recover_phase1_product_kernel,
)
from ea.core.audit import (
    AuditRecordKind,
    AuditSubjectKind,
    audit_subject_digest,
    require_canonical_audit_payload,
)
from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency, instrument_spec_set_digest
from ea.core.execution_messages import ExecutionPolicyId, ExecutionPolicyRef
from ea.core.initial_funding import (
    InitialFundingSpec,
    canonical_initial_funding_outcome_bytes,
    initial_funding_outcome_digest,
)
from ea.core.risk import RiskPolicyId, create_phase1_risk_policy
from ea.core.run import DataFingerprint, ReplayWindow, RunBinding, RunId, RunReference, Sha256Digest
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
from ea.experiments.provenance import (
    ProvenanceError,
    collect_installed_runtime_spec_v2,
)
from ea.experiments.store import LocalResultStore, StoreError
from ea.portfolio import create_portfolio_ledger
from unit.test_portfolio_ledger import RUN_ID, _spec_set


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


def test_installed_runtime_collector_rejects_the_editable_source_tree() -> None:
    with pytest.raises(ProvenanceError, match="source-tree"):
        collect_installed_runtime_spec_v2()


def test_installed_runtime_collector_hashes_sorted_regular_distribution_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "site-packages"
    package = root / "ea"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.2.0'\n")
    (package / "kernel.py").write_text("value = 1\n")
    metadata = root / "ea_quant-0.2.0.dist-info"
    metadata.mkdir()
    (metadata / "RECORD").write_text("ignored\n")

    class FakeDistribution:
        metadata = {"Name": "ea-quant"}
        version = "0.2.0"
        files = (
            Path("ea/kernel.py"),
            Path("ea_quant-0.2.0.dist-info/RECORD"),
            Path("ea/__init__.py"),
        )

        def locate_file(self, path: object) -> Path:
            return root / str(path)

        def read_text(self, name: str) -> None:
            assert name == "direct_url.json"
            return None

    distribution = FakeDistribution()
    monkeypatch.setattr(
        provenance_module.importlib.metadata,
        "distributions",
        lambda: [distribution],
    )
    monkeypatch.setattr(ea, "__file__", str(package / "__init__.py"))

    first = collect_installed_runtime_spec_v2()
    (package / "kernel.py").write_text("value = 2\n")
    second = collect_installed_runtime_spec_v2()

    assert first.ea_distribution == DistributionIdentity("ea-quant", "0.2.0")
    assert first.ea_installed_files_sha256 != second.ea_installed_files_sha256


def test_initial_funding_outcome_has_its_own_canonical_audit_record() -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    binding = RunBinding(
        RunReference(RUN_ID, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    outcome = create_portfolio_ledger(RUN_ID, spec_set).apply_initial_funding(
        funding,
        binding=binding,
        prepared_acknowledgement=Sha256Digest("33" * 32),
    )

    payload = canonical_initial_funding_outcome_bytes(outcome)

    assert AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME.value == (
        "portfolio.initial_funding_outcome"
    )
    assert AuditSubjectKind.INITIAL_FUNDING_OUTCOME.value == "initial_funding_outcome"
    assert (
        require_canonical_audit_payload(AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME, payload)
        == payload
    )
    assert audit_subject_digest(
        AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME, payload
    ) == initial_funding_outcome_digest(outcome)


def test_store_product_prepare_requires_and_publishes_a_v2_manifest(tmp_path: Path) -> None:
    root = tmp_path / "results"
    root.mkdir()
    store = LocalResultStore(root.resolve())

    prepared = store.prepare_product(
        _manifest_v2().spec,
        lambda: UUID("12345678-1234-4234-8234-123456789abc"),
    )

    assert store.verify_manifest(prepared.manifest_verification).manifest_schema_version == 2
    with pytest.raises(StoreError, match="LineageSpecV2"):
        store.prepare_product(object(), lambda: UUID("12345678-1234-4234-8234-123456789abc"))


def test_product_kernel_publishes_only_the_funded_read_only_boundary(tmp_path: Path) -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    original = _manifest_v2().spec
    spec = build_lineage_spec_v2(
        LineageInputsV2(
            configuration=original.configuration.normalized,
            data=original.data,
            replay_window=original.replay_window,
            parameters=original.parameters,
            runtime=original.runtime,
            randomness_seed=original.randomness.master_seed,
            stream_labels=original.randomness.stream_labels,
            scenario_sha256=original.scenario_sha256,
            initial_funding=funding,
        )
    )
    execution_policy = ExecutionPolicyRef(
        ExecutionPolicyId("phase1.execution.v1"), Sha256Digest("1" * 64)
    )
    risk_policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution_policy,
        instrument_limits=(),
    )
    (tmp_path / "results").mkdir()
    store = LocalResultStore((tmp_path / "results").resolve())

    kernel = prepare_phase1_product_kernel(
        store=store,
        spec=spec,
        run_id_provider=lambda: UUID("12345678-1234-4234-8234-123456789abc"),
        spec_set=spec_set,
        execution_policy=execution_policy,
        risk_policy=risk_policy,
    )

    assert not hasattr(kernel, "ledger")
    assert not hasattr(kernel, "audit")
    assert kernel.binding.reference.run_id == RunId("12345678-1234-4234-8234-123456789abc")
    assert kernel.funding_outcome.transaction is not None
    assert kernel.portfolio_snapshot.snapshot_version == 1
    assert kernel.risk_state.risk_state_version == 0


def test_product_kernel_recovery_api_is_exposed() -> None:
    assert callable(recover_phase1_product_kernel)


def test_failed_handoff_retires_the_store_writer_lease(tmp_path: Path) -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    original = _manifest_v2().spec
    spec = build_lineage_spec_v2(
        LineageInputsV2(
            configuration=original.configuration.normalized,
            data=original.data,
            replay_window=original.replay_window,
            parameters=original.parameters,
            runtime=original.runtime,
            randomness_seed=original.randomness.master_seed,
            stream_labels=original.randomness.stream_labels,
            scenario_sha256=original.scenario_sha256,
            initial_funding=funding,
        )
    )
    execution_policy = ExecutionPolicyRef(
        ExecutionPolicyId("phase1.execution.v1"), Sha256Digest("1" * 64)
    )
    risk_policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution_policy,
        instrument_limits=(),
    )
    (tmp_path / "results").mkdir()
    store = LocalResultStore((tmp_path / "results").resolve())
    kernel = prepare_phase1_product_kernel(
        store=store,
        spec=spec,
        run_id_provider=lambda: UUID("12345678-1234-4234-8234-123456789abc"),
        spec_set=spec_set,
        execution_policy=execution_policy,
        risk_policy=risk_policy,
    )

    with pytest.raises(RuntimeError, match="consumer failure"):
        _consume_phase1_product_kernel(
            kernel, lambda _handoff: (_ for _ in ()).throw(RuntimeError("consumer failure"))
        )

    recovered = recover_phase1_product_kernel(
        store=store,
        expected_manifest=build_manifest_v2(spec, kernel.binding.reference.run_id),
        spec_set=spec_set,
        execution_policy=execution_policy,
        risk_policy=risk_policy,
    )

    assert recovered.funding_outcome == kernel.funding_outcome
