from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import pytest
from pytest import MonkeyPatch

from ea.composition import run as run_composition
from ea.composition.run import (
    AdmittedRun,
    PreflightSession,
    PreparedReproducibleRun,
    RunCompositionError,
    admit_reproducible_run,
    prepare_reproducible_run,
    verify_reproducible_run,
)
from ea.config.settings import Settings
from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    Adjustment,
    AuditAppendAcknowledgement,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    Bar,
    Instrument,
    MarketDataEnvelope,
    RunBinding,
    RunId,
    RunReference,
    Sha256Digest,
    SourceId,
    VenueId,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
)
from ea.data import MarketDataSelection, select_and_fingerprint_market_data
from ea.experiments.binding import BoundaryBindingError
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    ManifestError,
    RuntimeEvidence,
)
from ea.experiments.provenance import ProvenanceEvidence
from ea.experiments.store import (
    AuditRunBinding,
    LocalResultStore,
    OutputCapability,
    StoreError,
    _OsStoreOps,
)

RUN_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER_PATH = PROJECT_ROOT / "scripts/reproducible_run.py"


class _LauncherModule(Protocol):
    def _bootstrap(self, repository: Path, launcher: Path) -> PreflightSession: ...


_LAUNCHER_SPEC = importlib.util.spec_from_file_location(
    "ea_test_composition_launcher",
    _LAUNCHER_PATH,
)
if _LAUNCHER_SPEC is None or _LAUNCHER_SPEC.loader is None:
    raise RuntimeError("cannot load the reproducible launcher for composition tests")
_LAUNCHER_MODULE = importlib.util.module_from_spec(_LAUNCHER_SPEC)
sys.modules[_LAUNCHER_SPEC.name] = _LAUNCHER_MODULE
_LAUNCHER_SPEC.loader.exec_module(_LAUNCHER_MODULE)
launcher_module = cast(_LauncherModule, _LAUNCHER_MODULE)


class _FakeGrant:
    def __init__(self, repository: Path, commit: str = COMMIT) -> None:
        self.repository = repository
        self.commit = commit
        self.consumed = False

    def consume(self) -> tuple[Path, str]:
        if self.consumed:
            raise RuntimeError("grant already consumed")
        self.consumed = True
        return self.repository, self.commit


def _session(
    repository: Path,
    monkeypatch: MonkeyPatch,
    commit: str = COMMIT,
) -> PreflightSession:
    """Exercise the real local-grant handoff while stubbing expensive preflight evidence."""
    monkeypatch.setattr(
        launcher_module,
        "_preflight",
        lambda actual_repository, launcher: commit,
    )
    monkeypatch.setattr(launcher_module, "_require_no_ea_modules", lambda: None)
    return launcher_module._bootstrap(repository, _LAUNCHER_PATH)


def _selection() -> MarketDataSelection:
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    event = MarketDataEnvelope(
        payload=Bar(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            interval_start=start,
            interval_end=end,
            adjustment=Adjustment.RAW,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
        ),
        source=SourceId("primary.raw"),
        available_at=end,
        source_sequence=0,
        revision=0,
    )
    from ea.core import ReplayWindow

    return select_and_fingerprint_market_data(
        [event],
        ReplayWindow(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 2, 1, tzinfo=UTC),
        ),
    )


def _provenance(repository: Path) -> ProvenanceEvidence:
    return ProvenanceEvidence(
        repository=repository,
        code=CodeEvidence(COMMIT),
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
    )


class OrderedOps(_OsStoreOps):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fsync_count = 0

    def fsync(self, file_fd: int) -> None:
        super().fsync(file_fd)
        self.fsync_count += 1
        self.events.append(f"store.fsync.{self.fsync_count}")
        if self.fsync_count == 3:
            self.events.append("manifest.durable")


def _prepare(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    events: list[str] | None = None,
) -> PreparedReproducibleRun:
    repository = PROJECT_ROOT
    lock = repository / "uv.lock"
    results = (tmp_path / "results").resolve()
    results.mkdir()
    provenance = _provenance(repository)

    def collect(actual_repository: Path, actual_lock: Path) -> ProvenanceEvidence:
        assert actual_repository == repository
        assert actual_lock == lock
        return provenance

    monkeypatch.setattr(run_composition, "collect_provenance", collect)
    ops = OrderedOps(events) if events is not None else None
    store = LocalResultStore(results, _ops=ops)
    settings = Settings.model_validate(
        {
            "schema_version": 1,
            "environment": "development",
            "run": {"mode": "backtest"},
        }
    )
    return prepare_reproducible_run(
        preflight=_session(repository, monkeypatch),
        settings=settings,
        market_data=_selection(),
        parameters=(),
        master_seed=0,
        stream_labels=("matcher.primary",),
        store=store,
        run_id_provider=lambda: RUN_UUID,
    )


class RecordingAudit:
    def __init__(
        self,
        events: list[str],
        binding: RunBinding,
        acknowledgement: AuditAppendAcknowledgement | None = None,
    ) -> None:
        self.events = events
        self.binding = binding
        self.acknowledgement = acknowledgement

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        assert record_kind is AuditRecordKind.RUN_PREPARED
        assert subject_kind is AuditSubjectKind.RUN_MANIFEST
        assert subject_sha256 == self.binding.manifest_sha256
        assert canonical_payload == canonical_run_prepared_audit_payload(self.binding)
        self.events.append("audit.append")
        acknowledgement = self.acknowledgement or _prepared_ack(self.binding)
        self.events.append("audit.ack")
        return acknowledgement

    def settle_append(
        self,
        *,
        logical_key: AuditLogicalKey,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        del logical_key, canonical_payload
        return self.acknowledgement


def _prepared_ack(binding: RunBinding) -> AuditAppendAcknowledgement:
    return create_audit_append_acknowledgement(
        create_audit_record(
            binding=binding,
            owner_sequence=1,
            record_kind=AuditRecordKind.RUN_PREPARED,
            subject_kind=AuditSubjectKind.RUN_MANIFEST,
            subject_sha256=binding.manifest_sha256,
            canonical_payload=canonical_run_prepared_audit_payload(binding),
            previous_record_sha256=EMPTY_RECORD_SHA256,
            previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
        )
    )


class RecordingOutput:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def write(
        self,
        binding: RunBinding,
        capability: OutputCapability,
        payload: bytes,
    ) -> RunBinding:
        assert type(capability) is OutputCapability
        self.events.append("output.write")
        return binding


def test_composition_binds_raw_data_and_collector_evidence_before_uuid(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)

    assert prepared.spec.data == prepared.market_data.fingerprint
    assert prepared.spec.replay_window == prepared.market_data.window
    assert prepared.spec.code.commit == prepared.provenance.code.commit
    assert prepared.reference.run_id == RunId(str(RUN_UUID))

    with pytest.raises(RunCompositionError, match="only be issued"):
        PreparedReproducibleRun(
            _seal=object(),
            _prepared=prepared._prepared,
            spec=prepared.spec,
            market_data=prepared.market_data,
            provenance=prepared.provenance,
            _store=prepared._store,
        )


def test_non_backtest_rejects_before_collector_uuid_or_reservation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        run_composition,
        "collect_provenance",
        lambda *args: events.append("collect"),
    )
    results = (tmp_path / "results").resolve()
    results.mkdir()
    settings = Settings.model_validate(
        {
            "schema_version": 1,
            "environment": "development",
            "run": {"mode": "paper"},
        }
    )

    def provide_uuid() -> UUID:
        events.append("uuid")
        return RUN_UUID

    with pytest.raises(ManifestError, match="only run.mode backtest"):
        prepare_reproducible_run(
            preflight=_session(PROJECT_ROOT, monkeypatch),
            settings=settings,
            market_data=_selection(),
            parameters=(),
            master_seed=0,
            stream_labels=("matcher.primary",),
            store=LocalResultStore(results),
            run_id_provider=provide_uuid,
        )

    assert events == []
    assert tuple(results.iterdir()) == ()


def test_ordered_gate_is_manifest_then_adapters_then_audit_ack_then_runtime(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    prepared = _prepare(tmp_path, monkeypatch, events=events)

    def audit_factory(prepared: AuditRunBinding) -> RecordingAudit:
        events.append("audit.start")
        return RecordingAudit(events, prepared.binding)

    def output_factory() -> RecordingOutput:
        events.append("output.start")
        return RecordingOutput(events)

    def start(admitted: AdmittedRun) -> str:
        events.append("feed.start")
        assert admitted.market_data is prepared.market_data.events
        events.append("runtime.start")
        assert admitted.randomness.claim("matcher.primary").next_u64() == 1256042036395257240
        assert admitted.numeric.sum_ordered((1.0, 2.0)) == 3.0
        return "started"

    result = admit_reproducible_run(
        prepared,
        audit_factory=audit_factory,
        output_factory=output_factory,
        start=start,
    )

    assert result == "started"
    assert events.index("manifest.durable") < events.index("audit.start")
    assert events.index("manifest.durable") < events.index("output.start")
    assert events.index("audit.start") < events.index("audit.append")
    assert events.index("audit.ack") < events.index("feed.start")
    assert events.index("audit.ack") < events.index("runtime.start")


def test_bad_first_audit_ack_prevents_feed_and_runtime_start(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    prepared = _prepare(tmp_path, monkeypatch, events=events)
    wrong = RunBinding(
        reference=RunReference(
            run_id=RunId("123e4567-e89b-42d3-b456-426614174000"),
            lineage_sha256=prepared.reference.lineage_sha256,
        ),
        manifest_sha256=Sha256Digest("0" * 64),
    )

    with pytest.raises(BoundaryBindingError, match="acknowledgement"):
        admit_reproducible_run(
            prepared,
            audit_factory=lambda binding: RecordingAudit(
                events,
                binding.binding,
                _prepared_ack(wrong),
            ),
            output_factory=lambda: RecordingOutput(events),
            start=lambda admitted: events.append("runtime.start"),
        )

    assert "runtime.start" not in events


def test_invalid_lifecycle_inputs_fail_before_adapter_factories(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    events: list[str] = []

    with pytest.raises(RunCompositionError, match="callable"):
        admit_reproducible_run(
            prepared,
            audit_factory=cast(run_composition.AuditPortFactory, object()),
            output_factory=lambda: cast(
                RecordingOutput,
                events.append("output.start"),
            ),
            start=lambda admitted: None,
        )

    assert events == []


def test_terminal_verifier_recollects_provenance_and_recomputes_data(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    calls: list[tuple[ProvenanceEvidence, Path, Path]] = []

    def verify(
        expected: ProvenanceEvidence,
        repository: Path,
        uv_lock_path: Path,
    ) -> None:
        calls.append((expected, repository, uv_lock_path))

    monkeypatch.setattr(run_composition, "verify_provenance", verify)
    repository = prepared.provenance.repository
    lock = repository / "uv.lock"

    verify_reproducible_run(
        prepared,
    )

    assert calls == [(prepared.provenance, repository, lock)]


def test_terminal_verifier_rebinds_current_durable_manifest_bytes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    manifest = tmp_path / "results" / prepared.reference.run_id.value / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")

    with pytest.raises(StoreError, match="prepared digest"):
        verify_reproducible_run(prepared)


def test_preparation_requires_unconsumed_launcher_session_before_effects(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = PROJECT_ROOT
    results = (tmp_path / "results").resolve()
    results.mkdir()
    settings = Settings.model_validate(
        {
            "schema_version": 1,
            "environment": "development",
            "run": {"mode": "backtest"},
        }
    )
    monkeypatch.setattr(
        run_composition,
        "collect_provenance",
        lambda *args: events.append("collect"),
    )

    fake = _FakeGrant(repository)
    with pytest.raises(RunCompositionError, match="pending tracked-launcher"):
        run_composition._accept_launcher_preflight(fake)
    assert not fake.consumed
    setattr(sys, run_composition._PREFLIGHT_SLOT, fake)
    with pytest.raises(RunCompositionError, match="not created by launcher bootstrap"):
        run_composition._accept_launcher_preflight(fake)
    assert not hasattr(sys, run_composition._PREFLIGHT_SLOT)
    assert not fake.consumed
    with pytest.raises(RunCompositionError, match="only be accepted"):
        PreflightSession(object(), repository=repository, commit=COMMIT)

    with pytest.raises(RunCompositionError, match="preflight session"):
        prepare_reproducible_run(
            preflight=object(),  # type: ignore[arg-type]
            settings=settings,
            market_data=_selection(),
            parameters=(),
            master_seed=0,
            stream_labels=("matcher.primary",),
            store=LocalResultStore(results.resolve()),
            run_id_provider=lambda: cast(UUID, events.append("uuid")),
        )

    forged = object.__new__(PreflightSession)
    with pytest.raises(RunCompositionError, match="not issued by launcher bootstrap"):
        prepare_reproducible_run(
            preflight=forged,
            settings=settings,
            market_data=_selection(),
            parameters=(),
            master_seed=0,
            stream_labels=("matcher.primary",),
            store=LocalResultStore(results.resolve()),
            run_id_provider=lambda: cast(UUID, events.append("uuid")),
        )

    assert events == []
    assert tuple(results.iterdir()) == ()


def test_preflight_session_is_single_use_and_binds_collector_commit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = PROJECT_ROOT
    first_results = (tmp_path / "results-a").resolve()
    second_results = (tmp_path / "results-b").resolve()
    first_results.mkdir()
    second_results.mkdir()
    settings = Settings.model_validate(
        {
            "schema_version": 1,
            "environment": "development",
            "run": {"mode": "backtest"},
        }
    )
    events: list[str] = []
    provenance = _provenance(repository)

    def collect(actual_repository: Path, actual_lock: Path) -> ProvenanceEvidence:
        assert actual_repository == repository
        assert actual_lock == repository / "uv.lock"
        events.append("collect")
        return provenance

    monkeypatch.setattr(run_composition, "collect_provenance", collect)
    preflight = _session(repository, monkeypatch)
    prepare_reproducible_run(
        preflight=preflight,
        settings=settings,
        market_data=_selection(),
        parameters=(),
        master_seed=0,
        stream_labels=("matcher.primary",),
        store=LocalResultStore(first_results),
        run_id_provider=lambda: RUN_UUID,
    )

    with pytest.raises(RunCompositionError, match="already consumed"):
        prepare_reproducible_run(
            preflight=preflight,
            settings=settings,
            market_data=_selection(),
            parameters=(),
            master_seed=0,
            stream_labels=("matcher.primary",),
            store=LocalResultStore(second_results),
            run_id_provider=lambda: cast(UUID, events.append("uuid")),
        )

    assert events == ["collect"]
    assert tuple(second_results.iterdir()) == ()

    mismatch_results = (tmp_path / "results-c").resolve()
    mismatch_results.mkdir()
    with pytest.raises(RunCompositionError, match="different commit"):
        prepare_reproducible_run(
            preflight=_session(repository, monkeypatch, "a" * 40),
            settings=settings,
            market_data=_selection(),
            parameters=(),
            master_seed=0,
            stream_labels=("matcher.primary",),
            store=LocalResultStore(mismatch_results),
            run_id_provider=lambda: cast(UUID, events.append("uuid")),
        )
    assert events == ["collect", "collect"]
    assert tuple(mismatch_results.iterdir()) == ()
