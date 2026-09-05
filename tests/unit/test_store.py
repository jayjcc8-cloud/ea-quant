from __future__ import annotations

import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid1

import pytest

from ea.core import DataFingerprint, ReplayWindow, RunId, RunReference, Sha256Digest
from ea.experiments import store as store_module
from ea.experiments.audit import create_posix_audit_journal
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    LineageInputs,
    LineageSpec,
    NormalizedConfiguration,
    RuntimeEvidence,
    build_lineage_spec,
    canonical_manifest_bytes,
    read_manifest,
)
from ea.experiments.store import (
    AuditRunBinding,
    CanonicalAttemptManifest,
    LocalResultStore,
    StoreCollisionError,
    StoreError,
    _OsStoreOps,
)

RUN_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
OTHER_UUID = UUID("123e4567-e89b-42d3-b456-426614174000")


def _spec() -> LineageSpec:
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
            master_seed=0,
            stream_labels=(),
        )
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "results"
    root.mkdir()
    return root.resolve()


def test_store_durably_publishes_before_returning_narrow_context(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = LocalResultStore(root)
    prepared = store.prepare(_spec(), lambda: RUN_UUID)
    run_directory = root / str(RUN_UUID)
    manifest_path = run_directory / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    parsed = read_manifest(manifest_bytes)

    assert prepared.reference == parsed.reference
    assert prepared.manifest_sha256.value == sha256(manifest_bytes).hexdigest()
    assert prepared.audit.binding == prepared.output.binding
    assert prepared.audit.binding.reference == prepared.reference
    assert store.verify_manifest(prepared.manifest_verification).reference == prepared.reference
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert (run_directory / "audit").is_dir()
    assert (run_directory / "outputs").is_dir()
    assert not hasattr(prepared, "root")
    assert not hasattr(prepared, "manifest_path")
    assert canonical_manifest_bytes(parsed) == manifest_bytes


def test_store_binds_a_canonical_product_manifest_for_fresh_and_recovery_use(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    reference = RunReference(RunId(str(RUN_UUID)), Sha256Digest("2" * 64))
    payload = (
        b'{"canonicalization":"ea-backtest-attempt-v1",'
        b'"run_id":"123e4567-e89b-42d3-a456-426614174000",'
        b'"schema":"ea.backtest-attempt.v1"}'
    )
    manifest = CanonicalAttemptManifest(reference=reference, canonical_bytes=payload)

    original = LocalResultStore(root)
    prepared = original.prepare_canonical_attempt(manifest)
    journal = create_posix_audit_journal(prepared.audit)
    journal.close()

    assert prepared.reference == reference
    assert (root / reference.run_id.value / "manifest.json").read_bytes() == payload
    original.close()

    recovered = LocalResultStore(root)
    verified = recovered.verify_recovery_attempt(manifest)

    assert verified.binding.reference == reference
    recovered.close()


def test_capability_registry_rejects_cross_attempt_role_and_store_substitution(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    store = LocalResultStore(root)
    first = store.prepare(_spec(), lambda: RUN_UUID)
    second = store.prepare(_spec(), lambda: OTHER_UUID)
    first_record, first_child = store._resolve_child(first.audit.capability)
    second_record, second_child = store._resolve_child(second.output.capability)

    assert first_record.authority.binding == first.audit.binding
    assert first_child == "audit"
    assert second_record.authority.binding == second.output.binding
    assert second_child == "outputs"

    with pytest.raises(StoreError, match="exact attempt authority"):
        AuditRunBinding(
            binding=first.audit.binding,
            capability=second.audit.capability,
            authority=first.audit._authority,
            seal=store_module._BINDING_SEAL,
        )
    with pytest.raises(StoreError, match="invalid role"):
        store._resolve_child(second.manifest_verification)  # type: ignore[arg-type]

    other_root = (tmp_path / "other-results").resolve()
    other_root.mkdir()
    other_store = LocalResultStore(other_root)
    with pytest.raises(StoreError, match="another result store"):
        other_store._resolve_child(first.audit.capability)
    with pytest.raises(StoreError, match="another result store"):
        other_store.verify_manifest(first.manifest_verification)


@pytest.mark.parametrize(
    "mutation",
    ["same-inode-content", "same-bytes-replacement", "symlink-replacement"],
)
def test_manifest_verification_detects_content_identity_and_symlink_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _root(tmp_path)
    store = LocalResultStore(root)
    prepared = store.prepare(_spec(), lambda: RUN_UUID)
    manifest_path = root / str(RUN_UUID) / "manifest.json"
    original = manifest_path.read_bytes()
    original_stat = manifest_path.stat()

    if mutation == "same-inode-content":
        manifest_path.write_bytes(original + b"\n")
        expected_message = "manifest bytes no longer match the prepared digest"
    elif mutation == "same-bytes-replacement":
        replacement = manifest_path.with_name("manifest-replacement")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        replacement_stat = replacement.stat()
        assert (replacement_stat.st_dev, replacement_stat.st_ino) != (
            original_stat.st_dev,
            original_stat.st_ino,
        )
        os.replace(replacement, manifest_path)
        expected_message = "manifest file identity changed after preparation"
    else:
        canary = tmp_path / "manifest-canary"
        canary.write_bytes(original)
        manifest_path.unlink()
        manifest_path.symlink_to(canary)
        expected_message = "manifest could not be re-opened without following links"

    with pytest.raises(StoreError, match=expected_message):
        store.verify_manifest(prepared.manifest_verification)


def test_same_lineage_attempts_use_distinct_uuid_directories(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = LocalResultStore(root)

    first = store.prepare(_spec(), lambda: RUN_UUID)
    second = store.prepare(_spec(), lambda: OTHER_UUID)

    assert first.reference.lineage_sha256 == second.reference.lineage_sha256
    assert first.reference.run_id != second.reference.run_id
    assert {path.name for path in root.iterdir()} == {str(RUN_UUID), str(OTHER_UUID)}


@pytest.mark.parametrize("collision_kind", ["file", "directory", "symlink"])
def test_existing_attempt_entry_is_never_adopted_or_overwritten(
    tmp_path: Path,
    collision_kind: str,
) -> None:
    root = _root(tmp_path)
    target = root / str(RUN_UUID)
    canary = tmp_path / "canary"
    canary.write_text("unchanged", encoding="utf-8")
    if collision_kind == "file":
        target.write_text("existing", encoding="utf-8")
    elif collision_kind == "directory":
        target.mkdir()
        (target / "canary").write_text("existing", encoding="utf-8")
    else:
        target.symlink_to(canary)

    with pytest.raises(StoreCollisionError):
        LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)

    assert canary.read_text(encoding="utf-8") == "unchanged"
    if collision_kind == "file":
        assert target.read_text(encoding="utf-8") == "existing"
    elif collision_kind == "directory":
        assert (target / "canary").read_text(encoding="utf-8") == "existing"
    else:
        assert target.is_symlink()


def test_result_root_final_symlink_is_rejected(tmp_path: Path) -> None:
    actual = _root(tmp_path)
    link = tmp_path / "results-link"
    link.symlink_to(actual, target_is_directory=True)

    with pytest.raises(StoreError, match="symlink"):
        LocalResultStore(link)


def test_invalid_uuid_provider_fails_before_reservation(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with pytest.raises(StoreError, match="UUID4"):
        LocalResultStore(root).prepare(_spec(), uuid1)

    assert tuple(root.iterdir()) == ()


def test_concurrent_same_uuid_reservation_has_one_winner(tmp_path: Path) -> None:
    root = _root(tmp_path)
    barrier = threading.Barrier(2)

    def provider() -> UUID:
        barrier.wait()
        return RUN_UUID

    def prepare() -> object:
        return LocalResultStore(root).prepare(_spec(), provider)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(prepare) for _ in range(2)]
    successes = [future.result() for future in futures if future.exception() is None]
    errors = [future.exception() for future in futures if future.exception() is not None]

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], StoreCollisionError)
    assert (root / str(RUN_UUID) / "manifest.json").is_file()


class FaultOps:
    def __init__(self, *, fail_fsync: int | None = None, corrupt_read: bool = False) -> None:
        self.delegate = _OsStoreOps()
        self.fail_fsync = fail_fsync
        self.corrupt_read = corrupt_read
        self.fsync_count = 0
        self.events: list[str] = []
        self._corrupted = False

    def open_root(self, path: Path) -> int:
        self.events.append("open_root")
        return self.delegate.open_root(path)

    def mkdir_at(self, parent_fd: int, name: str, mode: int) -> None:
        self.events.append(f"mkdir:{name}")
        self.delegate.mkdir_at(parent_fd, name, mode)

    def open_dir_at(self, parent_fd: int, name: str) -> int:
        self.events.append(f"open_dir:{name}")
        return self.delegate.open_dir_at(parent_fd, name)

    def create_file_at(self, parent_fd: int, name: str, mode: int) -> int:
        self.events.append(f"create:{name}")
        return self.delegate.create_file_at(parent_fd, name, mode)

    def open_file_read_at(self, parent_fd: int, name: str) -> int:
        self.events.append(f"open_read:{name}")
        return self.delegate.open_file_read_at(parent_fd, name)

    def write(self, file_fd: int, data: memoryview) -> int:
        self.events.append("write")
        return self.delegate.write(file_fd, data)

    def read(self, file_fd: int, size: int) -> bytes:
        self.events.append("read")
        value = self.delegate.read(file_fd, size)
        if self.corrupt_read and value and not self._corrupted:
            self._corrupted = True
            return b"x" + value[1:]
        return value

    def fsync(self, file_fd: int) -> None:
        self.fsync_count += 1
        self.events.append(f"fsync:{self.fsync_count}")
        if self.fsync_count == self.fail_fsync:
            raise OSError("injected fsync failure")
        self.delegate.fsync(file_fd)

    def close(self, file_fd: int) -> None:
        self.events.append("close")
        self.delegate.close(file_fd)

    def fstat(self, file_fd: int) -> os.stat_result:
        return self.delegate.fstat(file_fd)


@pytest.mark.parametrize(
    ("fail_fsync", "corrupt_read"),
    [(1, False), (2, False), (3, False), (None, True)],
)
def test_durability_or_readback_failure_retains_poison_and_returns_no_context(
    tmp_path: Path,
    fail_fsync: int | None,
    corrupt_read: bool,
) -> None:
    root = _root(tmp_path)
    ops = FaultOps(fail_fsync=fail_fsync, corrupt_read=corrupt_read)

    with pytest.raises(StoreError):
        LocalResultStore(root, _ops=ops).prepare(
            _spec(),
            lambda: RUN_UUID,
        )

    poisoned = root / str(RUN_UUID)
    assert poisoned.is_dir()
    assert not any(event.startswith("unlink") for event in ops.events)
    with pytest.raises(StoreCollisionError):
        LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)


def test_manifest_persistence_order_precedes_directory_durability_ack(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    ops = FaultOps()

    LocalResultStore(root, _ops=ops).prepare(
        _spec(),
        lambda: RUN_UUID,
    )

    create = ops.events.index("create:manifest.json")
    first_write = ops.events.index("write")
    file_fsync = ops.events.index("fsync:1")
    reopen = ops.events.index("open_read:manifest.json")
    run_fsync = ops.events.index("fsync:2")
    root_fsync = ops.events.index("fsync:3")
    assert create < first_write < file_fsync < reopen < run_fsync < root_fsync


class ShortWriteOps(FaultOps):
    def __init__(self, *, zero_first: bool = False) -> None:
        super().__init__()
        self.zero_first = zero_first
        self.write_calls = 0

    def write(self, file_fd: int, data: memoryview) -> int:
        self.write_calls += 1
        self.events.append("write")
        if self.zero_first and self.write_calls == 1:
            return 0
        amount = max(1, len(data) // 2)
        return self.delegate.write(file_fd, data[:amount])


def test_positive_short_writes_are_completed_before_manifest_fsync(tmp_path: Path) -> None:
    root = _root(tmp_path)
    ops = ShortWriteOps()

    prepared = LocalResultStore(root, _ops=ops).prepare(_spec(), lambda: RUN_UUID)
    manifest = root / prepared.reference.run_id.value / "manifest.json"

    assert ops.write_calls > 1
    assert read_manifest(manifest.read_bytes()).reference == prepared.reference


def test_zero_write_fails_closed_and_retains_poison(tmp_path: Path) -> None:
    root = _root(tmp_path)
    ops = ShortWriteOps(zero_first=True)

    with pytest.raises(StoreError, match="forward progress"):
        LocalResultStore(root, _ops=ops).prepare(_spec(), lambda: RUN_UUID)

    assert (root / str(RUN_UUID)).is_dir()
    with pytest.raises(StoreCollisionError):
        LocalResultStore(root).prepare(_spec(), lambda: RUN_UUID)


class CloseFailureOps(FaultOps):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self, file_fd: int) -> None:
        self.close_calls += 1
        self.events.append("close")
        if self.close_calls == 1:
            raise OSError("injected close failure")
        self.delegate.close(file_fd)


def test_manifest_close_failure_returns_no_context_and_retains_poison(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    ops = CloseFailureOps()

    with pytest.raises(StoreError, match="poisoned directory retained"):
        LocalResultStore(root, _ops=ops).prepare(_spec(), lambda: RUN_UUID)

    assert (root / str(RUN_UUID)).is_dir()
    assert ops.close_calls >= 2


class SymlinkInjectionOps(FaultOps):
    def __init__(self, *, stage: str, target: Path) -> None:
        super().__init__()
        self.stage = stage
        self.target = target

    def mkdir_at(self, parent_fd: int, name: str, mode: int) -> None:
        if name == self.stage:
            os.symlink(self.target, name, dir_fd=parent_fd)
        super().mkdir_at(parent_fd, name, mode)

    def create_file_at(self, parent_fd: int, name: str, mode: int) -> int:
        if self.stage == "manifest.json" and name == self.stage:
            os.symlink(self.target, name, dir_fd=parent_fd)
        return super().create_file_at(parent_fd, name, mode)


@pytest.mark.parametrize("stage", ["audit", "outputs", "manifest.json"])
def test_child_or_manifest_symlink_injection_fails_without_following_target(
    tmp_path: Path,
    stage: str,
) -> None:
    root = _root(tmp_path)
    canary = tmp_path / "canary"
    canary.write_text("unchanged", encoding="utf-8")
    ops = SymlinkInjectionOps(stage=stage, target=canary)

    with pytest.raises(StoreError, match="poison"):
        LocalResultStore(root, _ops=ops).prepare(_spec(), lambda: RUN_UUID)

    assert canary.read_text(encoding="utf-8") == "unchanged"
    poisoned_entry = root / str(RUN_UUID) / stage
    assert poisoned_entry.is_symlink()
