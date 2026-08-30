"""Focused v2 manifest contracts for the funded product boundary."""

from __future__ import annotations

import gc
import json
import os
import weakref
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

import ea.composition.product_kernel as product_kernel
from ea.composition.product_kernel import (
    ProductKernelError,
    ProductKernelFailureCode,
    prepare_phase1_product_kernel,
    recover_phase1_product_kernel,
)
from ea.core.audit import (
    AuditRecordKind,
    AuditSubjectKind,
    audit_chain_head,
    audit_subject_digest,
)
from ea.core.economics import CanonicalDecimal
from ea.core.execution import (
    InstrumentExecutionSpecSet,
    InstrumentSpecSetId,
    SettlementCurrency,
    instrument_spec_set_digest,
)
from ea.core.execution_messages import ExecutionPolicyId, ExecutionPolicyRef
from ea.core.initial_funding import InitialFundingSpec
from ea.core.risk import Phase1RiskPolicy, RiskPolicyId, create_phase1_risk_policy
from ea.core.run import DataFingerprint, ReplayWindow, RunId, Sha256Digest
from ea.experiments._manifest_model import (
    DistributionIdentity,
    EffectiveParameter,
    InstalledRuntimeSpecV2,
    LineageInputsV2,
    LineageSpecV2,
    NormalizedConfiguration,
    build_lineage_spec_v2,
    build_manifest_v2,
    canonical_manifest_bytes,
)
from ea.experiments.manifest import RunManifestV2, read_manifest, read_manifest_v2
from ea.experiments.provenance import (
    ProvenanceError,
    _installed_file_rows,
    _validate_installed_direct_url,
)
from ea.experiments.store import CorruptAuditRecoveryError, LocalResultStore, StoreError
from unit.test_portfolio_ledger import RUN_ID, _spec_set


def test_manifest_v2_binds_installed_runtime_scenario_and_funding() -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    execution = ExecutionPolicyRef(ExecutionPolicyId("phase1.execution.v1"), Sha256Digest("1" * 64))
    risk = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution,
        instrument_limits=(),
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
                execution_policy=execution,
                risk_policy=risk,
            )
        ),
        RunId("12345678-1234-4234-8234-123456789abc"),
    )

    assert manifest.manifest_schema_version == 2
    assert manifest.spec.initial_funding.amount == CanonicalDecimal("1000")
    encoded = canonical_manifest_bytes(manifest)
    assert b'"execution_policy":{"identifier":"phase1.execution.v1","sha256":"' in encoded
    assert b'"risk_policy":{"identifier":"phase1.risk.v1","sha256":"' in encoded
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
    for document in (
        '{"archive_info":{},"url":"file:///tmp/ea.whl"}',
        '{"archive_info":{"hash":"sha256=' + "a" * 64 + '"},"url":"file:///tmp/ea.whl"}',
        '{"archive_info":{"hashes":{"sha256":"' + "b" * 64 + '"}},"url":"file:///tmp/ea.whl"}',
    ):
        _validate_installed_direct_url(Distribution(document))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    (
        " file:///tmp/ea.whl",
        "file:///tmp/ea.whl\\n",
        "file:///tmp/ea\\t.whl",
        "file:///tmp/ea\\x1f.whl",
        "file:///tmp/a b.whl",
        "file:///tmp/a\\\\b.whl",
        "file:///tmp/café.whl",
        "file:///tmp/a%zz.whl",
        "file:///tmp/a%.whl",
        "file:///tmp/a%00.whl",
        "FILE:///tmp/ea.whl",
        "https://example/ea.whl",
        "git+https://example/ea.whl",
        "file:///tmp/ea",
        "file:///tmp/source.tar.gz",
        "file://host/tmp/ea.whl",
        "file:///tmp/ea.whl?query",
        "file:///tmp/ea.whl#fragment",
    ),
)
def test_installed_direct_url_rejects_noncanonical_raw_or_nonwheel_origins(url: str) -> None:
    class Distribution:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"archive_info": {}, "url": url})

    with pytest.raises(ProvenanceError):
        _validate_installed_direct_url(Distribution())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "document",
    (
        '{"archive_info":{"hash":"sha256=' + "A" * 64 + '"},"url":"file:///tmp/ea.whl"}',
        '{"archive_info":{"hashes":{"sha256":"' + "A" * 64 + '"}},"url":"file:///tmp/ea.whl"}',
        '{"archive_info":{"hash":"sha512=' + "a" * 64 + '"},"url":"file:///tmp/ea.whl"}',
        '{"archive_info":{"editable":true},"url":"file:///tmp/ea.whl"}',
        '{"archive_info":{},"dir_info":{},"url":"file:///tmp/ea.whl"}',
    ),
)
def test_installed_direct_url_rejects_open_or_non_sha256_metadata(document: str) -> None:
    class Distribution:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return document

    with pytest.raises(ProvenanceError):
        _validate_installed_direct_url(Distribution())  # type: ignore[arg-type]


def test_installed_file_identity_includes_every_record_except_console_script(
    tmp_path: Path,
) -> None:
    for name in ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json"):
        (tmp_path / name).write_bytes(name.encode())

    class Distribution:
        files = tuple(
            Path(name)
            for name in ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "../../../bin/ea")
        )

        def locate_file(self, record: object) -> Path:
            return tmp_path if str(record) == "." else tmp_path / str(record)

    assert tuple(name for name, _ in _installed_file_rows(Distribution())) == (  # type: ignore[arg-type]
        "INSTALLER",
        "RECORD",
        "REQUESTED",
        "direct_url.json",
    )


@pytest.mark.parametrize("record", ("./RECORD", "x/../RECORD", "pkg//RECORD"))
def test_installed_record_rejects_noncanonical_raw_segments(tmp_path: Path, record: str) -> None:
    (tmp_path / "RECORD").write_bytes(b"record")
    (tmp_path / "x").mkdir()
    (tmp_path / "pkg").mkdir()

    class Distribution:
        files = (record,)

        def locate_file(self, value: object) -> Path:
            return tmp_path / str(value)

    with pytest.raises(ProvenanceError):
        _installed_file_rows(Distribution())  # type: ignore[arg-type]


def test_installed_record_rejects_a_symlinked_parent_inside_the_distribution(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "payload").write_bytes(b"payload")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)

    class Distribution:
        files = ("alias/payload",)

        def locate_file(self, value: object) -> Path:
            return tmp_path / str(value)

    with pytest.raises(ProvenanceError):
        _installed_file_rows(Distribution())  # type: ignore[arg-type]


def test_installed_record_rejects_a_final_symlink_swap_before_fd_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "payload"
    target.write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.write_bytes(b"not-recorded")
    moved = tmp_path / "moved-payload"

    class Distribution:
        files = ("payload",)

        def locate_file(self, value: object) -> Path:
            return tmp_path / str(value)

    original_open = os.open
    swapped = False

    def swap_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "payload" and dir_fd is not None and not flags & os.O_DIRECTORY:
            target.rename(moved)
            target.symlink_to(outside)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)
    try:
        with pytest.raises(ProvenanceError):
            _installed_file_rows(Distribution())  # type: ignore[arg-type]
        assert swapped
    finally:
        if target.is_symlink():
            target.unlink()
        moved.rename(target)


def test_installed_record_rejects_a_final_symlink_swap_after_fd_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "payload"
    target.write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.write_bytes(b"not-recorded")
    moved = tmp_path / "moved-payload"

    class Distribution:
        files = ("payload",)

        def locate_file(self, value: object) -> Path:
            return tmp_path / str(value)

    original_open = os.open
    swapped = False

    def swap_after_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "payload" and dir_fd is not None and not flags & os.O_DIRECTORY:
            target.rename(moved)
            target.symlink_to(outside)
            swapped = True
        return descriptor

    monkeypatch.setattr(os, "open", swap_after_open)
    try:
        with pytest.raises(ProvenanceError):
            _installed_file_rows(Distribution())  # type: ignore[arg-type]
        assert swapped
    finally:
        if target.is_symlink():
            target.unlink()
        moved.rename(target)


def _product_inputs() -> tuple[
    LineageSpecV2, InstrumentExecutionSpecSet, ExecutionPolicyRef, Phase1RiskPolicy
]:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    execution = ExecutionPolicyRef(ExecutionPolicyId("phase1.execution.v1"), Sha256Digest("1" * 64))
    risk = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution,
        instrument_limits=(),
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
            execution_policy=execution,
            risk_policy=risk,
        )
    )
    return spec, spec_set, execution, risk


@pytest.fixture(autouse=True)
def _synthetic_runtime_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        product_kernel, "collect_installed_runtime_spec_v2", lambda: _product_inputs()[0].runtime
    )


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
    assert not hasattr(first, "_handoff")
    manifest = read_manifest((tmp_path / "runs" / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    _release_for_recovery(first)
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
            store=None,  # type: ignore[arg-type]
            expected_manifest=None,  # type: ignore[arg-type]
            spec_set=None,  # type: ignore[arg-type]
            execution_policy=None,  # type: ignore[arg-type]
            risk_policy=None,  # type: ignore[arg-type]
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_MANIFEST_V1


def test_product_prepare_maps_a_store_collision_to_a_closed_boundary(tmp_path: Path) -> None:
    spec, spec_set, execution, risk = _product_inputs()
    root = tmp_path / "runs"
    root.mkdir()
    prepare_phase1_product_kernel(
        store=LocalResultStore(root),
        spec=spec,
        run_id_provider=lambda: UUID(RUN_ID.value),
        spec_set=spec_set,
        execution_policy=execution,
        risk_policy=risk,
    )
    with pytest.raises(ProductKernelError) as error:
        prepare_phase1_product_kernel(
            store=LocalResultStore(root),
            spec=spec,
            run_id_provider=lambda: UUID(RUN_ID.value),
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT


@pytest.mark.parametrize("collector_fails", (False, True))
def test_product_prepare_rejects_unverified_installed_runtime_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_fails: bool
) -> None:
    spec, spec_set, execution, risk = _product_inputs()
    root = tmp_path / "runs"
    root.mkdir()
    if collector_fails:

        def collect() -> InstalledRuntimeSpecV2:
            raise ProvenanceError("injected collector failure")
    else:

        def collect() -> InstalledRuntimeSpecV2:
            return replace(spec.runtime, ea_installed_files_sha256=Sha256Digest("66" * 32))

    monkeypatch.setattr(product_kernel, "collect_installed_runtime_spec_v2", collect)
    with pytest.raises(ProductKernelError) as error:
        prepare_phase1_product_kernel(
            store=LocalResultStore(root),
            spec=spec,
            run_id_provider=lambda: UUID(RUN_ID.value),
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT
    assert not (root / RUN_ID.value).exists()

    monkeypatch.setattr(product_kernel, "collect_installed_runtime_spec_v2", lambda: spec.runtime)
    assert (
        prepare_phase1_product_kernel(
            store=LocalResultStore(root),
            spec=spec,
            run_id_provider=lambda: UUID(RUN_ID.value),
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        ).binding.reference.run_id
        == RUN_ID
    )


@pytest.mark.parametrize("collector_fails", (False, True))
def test_product_recovery_rejects_unverified_runtime_without_taking_a_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_fails: bool
) -> None:
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
    manifest = read_manifest((root / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    _release_for_recovery(first)
    if collector_fails:

        def collect() -> InstalledRuntimeSpecV2:
            raise ProvenanceError("injected collector failure")
    else:

        def collect() -> InstalledRuntimeSpecV2:
            return replace(spec.runtime, ea_installed_files_sha256=Sha256Digest("66" * 32))

    monkeypatch.setattr(product_kernel, "collect_installed_runtime_spec_v2", collect)
    with pytest.raises(ProductKernelError) as error:
        recover_phase1_product_kernel(
            store=LocalResultStore(root),
            expected_manifest=manifest,
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT
    monkeypatch.setattr(product_kernel, "collect_installed_runtime_spec_v2", lambda: spec.runtime)
    assert (
        recover_phase1_product_kernel(
            store=LocalResultStore(root),
            expected_manifest=manifest,
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        ).binding.reference.run_id
        == RUN_ID
    )


def test_product_rejects_a_mutually_consistent_replacement_execution_risk_pair(
    tmp_path: Path,
) -> None:
    spec, spec_set, execution, risk = _product_inputs()
    replacement_execution = ExecutionPolicyRef(
        ExecutionPolicyId("phase1.execution.replacement.v1"), Sha256Digest("77" * 32)
    )
    replacement_risk = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.replacement.v1"),
        spec_set=spec_set,
        execution_policy=replacement_execution,
        instrument_limits=(),
    )
    root = tmp_path / "runs"
    root.mkdir()
    with pytest.raises(ProductKernelError) as error:
        prepare_phase1_product_kernel(
            store=LocalResultStore(root),
            spec=spec,
            run_id_provider=lambda: UUID(RUN_ID.value),
            spec_set=spec_set,
            execution_policy=replacement_execution,
            risk_policy=replacement_risk,
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_RISK_STATE_DRIFT
    assert not (root / RUN_ID.value).exists()

    first = prepare_phase1_product_kernel(
        store=LocalResultStore(root),
        spec=spec,
        run_id_provider=lambda: UUID(RUN_ID.value),
        spec_set=spec_set,
        execution_policy=execution,
        risk_policy=risk,
    )
    manifest = read_manifest((root / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    _release_for_recovery(first)
    with pytest.raises(ProductKernelError) as recovery_error:
        recover_phase1_product_kernel(
            store=LocalResultStore(root),
            expected_manifest=manifest,
            spec_set=spec_set,
            execution_policy=replacement_execution,
            risk_policy=replacement_risk,
        )
    assert recovery_error.value.code is ProductKernelFailureCode.INTEGRITY_RISK_STATE_DRIFT
    assert (
        recover_phase1_product_kernel(
            store=LocalResultStore(root),
            expected_manifest=manifest,
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        ).binding.reference.run_id
        == RUN_ID
    )


@pytest.mark.parametrize(
    ("mismatch", "code"),
    (
        ("spec_set", ProductKernelFailureCode.FUNDING_SPEC_MISMATCH),
        ("execution_policy", ProductKernelFailureCode.INTEGRITY_RISK_STATE_DRIFT),
    ),
)
def test_product_prepare_maps_contract_mismatches_to_precise_codes(
    tmp_path: Path, mismatch: str, code: ProductKernelFailureCode
) -> None:
    spec, spec_set, execution, risk = _product_inputs()
    provided_spec_set = (
        replace(spec_set, identifier=InstrumentSpecSetId("phase1.drift.v1"))
        if mismatch == "spec_set"
        else spec_set
    )
    provided_execution = (
        ExecutionPolicyRef(ExecutionPolicyId("phase1.execution.drift.v1"), Sha256Digest("2" * 64))
        if mismatch == "execution_policy"
        else execution
    )
    with pytest.raises(ProductKernelError) as error:
        prepare_phase1_product_kernel(
            store=LocalResultStore(tmp_path),
            spec=spec,
            run_id_provider=lambda: UUID(RUN_ID.value),
            spec_set=provided_spec_set,
            execution_policy=provided_execution,
            risk_policy=risk,
        )
    assert error.value.code is code


def _release_for_recovery(kernel: object) -> None:
    product_kernel._retire_phase1_product_kernel(kernel)  # type: ignore[arg-type]


def _append_terminal(root: Path, manifest: RunManifestV2) -> None:
    store = LocalResultStore(root)
    verified = store.verify_recovery_attempt(manifest)
    recovered = store.recover_incomplete_attempt(verified)  # type: ignore[arg-type]
    from ea.experiments.audit import reopen_posix_audit_journal

    journal = reopen_posix_audit_journal(recovered.audit)
    payload = json.dumps(
        {
            "canonicalization": "ea-canonical-json-v1",
            "last_dispatch_sequence": 1,
            "last_trigger_root_sha256": "33" * 32,
            "pre_terminal_state_sha256": "44" * 32,
            "previous_chain_head_sha256": audit_chain_head(journal.records[-1]).value,
            "run_id": RUN_ID.value,
            "schema": "ea.audit-run-terminal.v1",
            "terminal_kind": "success",
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    journal.append(
        record_kind=AuditRecordKind.RUN_TERMINAL,
        subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
        subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        canonical_payload=payload,
    )
    journal.close()
    store._retire_product_attempt(recovered.audit)


def test_product_recovers_a_mechanically_torn_final_audit_tail(tmp_path: Path) -> None:
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
    manifest = read_manifest((root / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    _release_for_recovery(first)
    journal_path = root / RUN_ID.value / "audit" / "audit-v1.journal"
    with journal_path.open("ab", buffering=0) as journal:
        journal.write(b"torn")
        os.fsync(journal.fileno())

    recovered = recover_phase1_product_kernel(
        store=LocalResultStore(root),
        expected_manifest=manifest,
        spec_set=spec_set,
        execution_policy=execution,
        risk_policy=risk,
    )
    assert recovered.funding_outcome == first.funding_outcome


@pytest.mark.parametrize(
    "contents",
    (b"", b"EA-AUDIT-V1\\n", b"EA-AUDIT-V1\\ntorn-before-first-record"),
)
def test_product_recovery_rejects_an_empty_or_header_only_audit_prefix(
    tmp_path: Path, contents: bytes
) -> None:
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
    manifest = read_manifest((root / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    _release_for_recovery(first)
    journal_path = root / RUN_ID.value / "audit" / "audit-v1.journal"
    journal_path.write_bytes(contents)

    for _ in range(2):
        with pytest.raises(ProductKernelError) as error:
            recover_phase1_product_kernel(
                store=LocalResultStore(root),
                expected_manifest=manifest,
                spec_set=spec_set,
                execution_policy=execution,
                risk_policy=risk,
            )
        assert error.value.code is ProductKernelFailureCode.INTEGRITY_AUDIT_INCOMPLETE


def test_product_recovery_maps_a_missing_journal_to_incomplete(tmp_path: Path) -> None:
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
    manifest = read_manifest((root / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    _release_for_recovery(first)
    (root / RUN_ID.value / "audit" / "audit-v1.journal").unlink()

    with pytest.raises(ProductKernelError) as error:
        recover_phase1_product_kernel(
            store=LocalResultStore(root),
            expected_manifest=manifest,
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_AUDIT_INCOMPLETE


def test_first_frame_open_is_nonblocking_and_symlink_journal_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    audit_dir = root / RUN_ID.value / "audit"
    flags: list[int] = []
    original_open = os.open

    def recording_open(path: object, value: int, *args: object, **kwargs: object) -> int:
        if path == "audit-v1.journal":
            flags.append(value)
        return original_open(path, value, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording_open)
    descriptor = original_open(audit_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        LocalResultStore._require_durable_first_audit_frame(descriptor, first.binding)
    finally:
        os.close(descriptor)
    assert flags and flags[-1] & os.O_NONBLOCK
    _release_for_recovery(first)
    journal = audit_dir / "audit-v1.journal"
    target = tmp_path / "other.journal"
    target.write_bytes(journal.read_bytes())
    journal.unlink()
    journal.symlink_to(target)
    manifest = read_manifest((root / RUN_ID.value / "manifest.json").read_bytes())
    assert type(manifest) is RunManifestV2
    with pytest.raises(ProductKernelError) as error:
        recover_phase1_product_kernel(
            store=LocalResultStore(root),
            expected_manifest=manifest,
            spec_set=spec_set,
            execution_policy=execution,
            risk_policy=risk,
        )
    assert error.value.code is ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT
