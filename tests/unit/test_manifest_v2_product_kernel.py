"""Focused reconstruction contracts for the Phase 1 V2 product boundary."""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest


def test_installed_runtime_v2_collector_is_a_closed_product_entrypoint() -> None:
    from ea.experiments.provenance import collect_installed_runtime_spec_v2

    assert collect_installed_runtime_spec_v2.__name__ == "collect_installed_runtime_spec_v2"


def test_store_exposes_a_v2_product_preparation_boundary() -> None:
    from ea.experiments.store import LocalResultStore

    assert "prepare_product" in dir(LocalResultStore)


def test_product_kernel_exposes_the_funded_boundary_value() -> None:
    from ea.composition.product_kernel import Phase1ProductKernel

    assert Phase1ProductKernel.__name__ == "Phase1ProductKernel"


def test_product_kernel_exposes_the_funded_recovery_boundary() -> None:
    from ea.composition.product_kernel import recover_phase1_product_kernel

    assert recover_phase1_product_kernel.__name__ == "recover_phase1_product_kernel"


def test_public_product_boundaries_reject_invalid_manifest_input_without_attempt_mutation(
    tmp_path: Path,
) -> None:
    from ea.composition.product_kernel import (
        ProductKernelError,
        prepare_phase1_product_kernel,
        recover_phase1_product_kernel,
    )
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments.manifest import LineageSpecV2, RunManifestV2
    from ea.experiments.store import LocalResultStore, RunIdProvider

    store = LocalResultStore(tmp_path)
    unused_spec_set = cast(InstrumentExecutionSpecSet, object())
    unused_policy = cast(ExecutionPolicyRef, object())
    unused_risk = cast(Phase1RiskPolicy, object())
    with pytest.raises(ProductKernelError, match="invalid product input"):
        prepare_phase1_product_kernel(
            store=store,
            spec=cast(LineageSpecV2, object()),
            run_id_provider=cast(RunIdProvider, object()),
            spec_set=unused_spec_set,
            execution_policy=unused_policy,
            risk_policy=unused_risk,
        )
    with pytest.raises(ProductKernelError, match="manifest v2"):
        recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, object()),
            spec_set=unused_spec_set,
            execution_policy=unused_policy,
            risk_policy=unused_risk,
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_wheel_owned_rows_require_closed_record_hashes(tmp_path: Path) -> None:
    from ea.experiments.provenance import ProvenanceError, _wheel_owned_rows

    wheel = tmp_path / "ea_quant-0.1.1-py3-none-any.whl"
    source = b"value = 1\n"
    digest = hashlib.sha256(source).digest()
    record = (
        b"ea/__init__.py,sha256="
        + __import__("base64").urlsafe_b64encode(digest).rstrip(b"=")
        + b",10\n"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ea/__init__.py", source)
        archive.writestr("ea_quant-0.1.1.dist-info/METADATA", b"Name: ea-quant\n")
        archive.writestr("ea_quant-0.1.1.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
        archive.writestr("ea_quant-0.1.1.dist-info/top_level.txt", b"ea\n")
        archive.writestr("ea_quant-0.1.1.dist-info/RECORD", record)

    with pytest.raises(ProvenanceError):
        _wheel_owned_rows(wheel.read_bytes())


def test_direct_url_requires_a_present_matching_local_wheel(tmp_path: Path) -> None:
    from ea.experiments.provenance import _local_wheel_artifact

    wheel = tmp_path / "ea.whl"
    wheel.write_bytes(b"wheel")
    document = json.dumps(
        {
            "url": wheel.as_uri(),
            "archive_info": {"hash": "sha256=" + hashlib.sha256(b"wheel").hexdigest()},
        }
    )

    assert _local_wheel_artifact(document) == (b"wheel", hashlib.sha256(b"wheel").hexdigest())


def test_local_wheel_snapshot_owns_rows_after_same_path_replacement(tmp_path: Path) -> None:
    from ea.experiments.provenance import _local_wheel_artifact, _wheel_owned_rows

    wheel = tmp_path / "ea_quant-0.1.1-py3-none-any.whl"
    root = "ea_quant-0.1.1.dist-info"

    def write_wheel(source: bytes) -> tuple[tuple[str, bytes], ...]:
        rows = {
            "ea/__init__.py": source,
            root + "/METADATA": b"Name: ea-quant\n",
            root + "/WHEEL": b"Wheel-Version: 1.0\n",
            root + "/entry_points.txt": b"[console_scripts]\n",
            root + "/top_level.txt": b"ea\n",
        }
        record = (
            b"".join(
                name.encode()
                + b",sha256="
                + __import__("base64")
                .urlsafe_b64encode(hashlib.sha256(value).digest())
                .rstrip(b"=")
                + b","
                + str(len(value)).encode()
                + b"\n"
                for name, value in rows.items()
            )
            + (root + "/RECORD,,\n").encode()
        )
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, value in rows.items():
                archive.writestr(name, value)
            archive.writestr(root + "/RECORD", record)
        return tuple(sorted(rows.items()))

    expected = write_wheel(b"value = 'A'\n")
    document = json.dumps(
        {
            "url": wheel.as_uri(),
            "archive_info": {"hash": "sha256=" + hashlib.sha256(wheel.read_bytes()).hexdigest()},
        }
    )
    snapshot, digest = _local_wheel_artifact(document)
    write_wheel(b"value = 'B'\n")

    assert snapshot is not None
    assert hashlib.sha256(snapshot).hexdigest() == digest
    assert snapshot != wheel.read_bytes()
    assert _wheel_owned_rows(snapshot) == expected


@pytest.mark.parametrize(
    "archive_info",
    [
        "hash",
        "hashes",
        "both",
    ],
)
def test_direct_url_accepts_only_pip_archive_hash_encodings(
    tmp_path: Path, archive_info: str
) -> None:
    from ea.experiments.provenance import _local_wheel_artifact

    wheel = tmp_path / "ea.whl"
    wheel.write_bytes(b"wheel")
    digest = hashlib.sha256(b"wheel").hexdigest()
    encodings = {
        "hash": {"hash": "sha256=" + digest},
        "hashes": {"hashes": {"sha256": digest}},
        "both": {"hash": "sha256=" + digest, "hashes": {"sha256": digest}},
    }

    assert _local_wheel_artifact(
        json.dumps({"url": wheel.as_uri(), "archive_info": encodings[archive_info]})
    ) == (b"wheel", digest)


@pytest.mark.parametrize(
    "archive_info",
    [
        {},
        {"hash": "sha256=" + "0" * 63},
        {"hash": None},
        {"hash": "sha512=" + "0" * 64},
        {"hash": "sha256=" + "A" * 64},
        {"hash": "sha256=" + "0" * 64, "hashes": {"sha256": "1" * 64}},
        {"hashes": {}},
        {"hashes": None},
        {"hashes": {"sha256": "0" * 64, "sha512": "0" * 64}},
        {"hashes": {"sha256": "A" * 64}},
        {"hash": "sha256=" + "0" * 64, "extra": "no"},
    ],
)
def test_direct_url_rejects_non_pip_or_ambiguous_archive_hashes(
    tmp_path: Path, archive_info: object
) -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    wheel = tmp_path / "ea.whl"
    wheel.write_bytes(b"wheel")
    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(json.dumps({"url": wheel.as_uri(), "archive_info": archive_info}))


@pytest.mark.parametrize("raw", ["pip", "pip\n", "pip \t\r\n\f\v"])
def test_installed_installer_admits_only_pip_with_ascii_trailing_whitespace(raw: str) -> None:
    from ea.experiments.provenance import _require_pip_installer

    _require_pip_installer(raw)


@pytest.mark.parametrize("raw", [None, "uv\n", "pip\u00a0", " pip\n", "pipx\n", "pip\x00"])
def test_installed_installer_rejects_missing_uv_or_noncanonical_value(raw: object) -> None:
    from ea.experiments.provenance import ProvenanceError, _require_pip_installer

    with pytest.raises(ProvenanceError):
        _require_pip_installer(raw)


def test_local_wheel_runtime_uses_closed_vocabulary_and_recovery_allows_absent_artifact(
    tmp_path: Path,
) -> None:
    from ea.experiments._manifest_model import InstalledRuntimeSpecV2
    from ea.experiments.provenance import _local_wheel_artifact

    wheel = tmp_path / "ea.whl"
    wheel.write_bytes(b"wheel")
    digest = hashlib.sha256(b"wheel").hexdigest()
    document = json.dumps({"url": wheel.as_uri(), "archive_info": {"hash": "sha256=" + digest}})

    assert InstalledRuntimeSpecV2.__dataclass_fields__["provenance_kind"].default == (
        "installed_local_wheel_v1"
    )
    wheel.unlink()
    assert _local_wheel_artifact(document, require_artifact=False) == (None, digest)


def test_local_wheel_recovery_rejects_a_missing_nonwheel_artifact(tmp_path: Path) -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    document = json.dumps(
        {
            "url": (tmp_path / "not-a-wheel.txt").as_uri(),
            "archive_info": {"hash": "sha256=" + "0" * 64},
        }
    )

    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(document, require_artifact=False)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/%-invalid.whl",
        "file:///tmp/%GG-invalid.whl",
        "file:///tmp/%FF-invalid.whl",
        "file:///tmp/\ud800-invalid.whl",
        "file:///tmp/e\u0301-invalid.whl",
        "file:///tmp/%65a-invalid.whl",
        "file:///private%2Ftmp/missing.whl",
        "file:///tmp/../tmp/missing.whl",
        "file:///tmp/%2E%2E/tmp/missing.whl",
        "file:///tmp//missing.whl",
        "file:relative.whl",
        "https:///tmp/missing.whl",
        "file://remote/tmp/missing.whl",
        "file:///tmp/missing.whl?query=yes",
        "file:///tmp/missing.whl#fragment",
        "file:///tmp/missing.txt",
        "file:///tmp/bad\nname.whl",
        "file:///tmp/bad\\name.whl",
        "file:///tmp/%00-invalid.whl",
    ],
)
def test_optional_wheel_recovery_rejects_noncanonical_urls(url: str) -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    document = json.dumps({"url": url, "archive_info": {"hash": "sha256=" + "0" * 64}})
    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(document, require_artifact=False)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "denied"),
        OSError(errno.EIO, "io"),
        OSError(errno.ENAMETOOLONG, "long"),
        OSError(errno.ELOOP, "loop"),
    ],
)
def test_optional_wheel_recovery_rejects_ambiguous_open_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    wheel = tmp_path / "present.whl"
    wheel.write_bytes(b"wheel")
    document = json.dumps({"url": wheel.as_uri(), "archive_info": {"hash": "sha256=" + "0" * 64}})
    real_open = os.open

    def fail_leaf(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is not None:
            raise error
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_leaf)
    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(document, require_artifact=False)


@pytest.mark.parametrize("kind", ["dangling-symlink", "directory", "hardlink"])
def test_optional_wheel_recovery_rejects_nonregular_or_linked_artifacts(
    tmp_path: Path, kind: str
) -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    wheel = tmp_path / "ambiguous.whl"
    if kind == "dangling-symlink":
        wheel.symlink_to(tmp_path / "missing.whl")
    elif kind == "directory":
        wheel.mkdir()
    else:
        wheel.write_bytes(b"wheel")
        os.link(wheel, tmp_path / "alias.whl")
    document = json.dumps({"url": wheel.as_uri(), "archive_info": {"hash": "sha256=" + "0" * 64}})
    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(document, require_artifact=False)


@pytest.mark.parametrize("race", ["content", "replacement"])
def test_local_wheel_snapshot_rejects_content_or_replacement_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    original = b"A" * 70_000
    wheel = tmp_path / "raced.whl"
    wheel.write_bytes(original)
    document = json.dumps(
        {
            "url": wheel.as_uri(),
            "archive_info": {"hash": "sha256=" + hashlib.sha256(original).hexdigest()},
        }
    )
    real_read = os.read
    changed = False

    def race_after_read(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            if race == "content":
                wheel.write_bytes(b"B" * 70_001)
            else:
                replacement = tmp_path / "replacement.whl"
                replacement.write_bytes(original)
                replacement.replace(wheel)
        return chunk

    monkeypatch.setattr(os, "read", race_after_read)
    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(document)


def test_wheel_owned_rows_include_all_required_metadata(tmp_path: Path) -> None:
    from ea.experiments.provenance import _wheel_owned_rows

    wheel = tmp_path / "ea_quant-0.1.1-py3-none-any.whl"
    rows = {
        "ea/__init__.py": b"value = 1\n",
        "ea_quant-0.1.1.dist-info/METADATA": b"Name: ea-quant\n",
        "ea_quant-0.1.1.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "ea_quant-0.1.1.dist-info/entry_points.txt": b"[console_scripts]\nea = ea:main\n",
        "ea_quant-0.1.1.dist-info/top_level.txt": b"ea\n",
    }
    record = (
        b"".join(
            name.encode()
            + b",sha256="
            + __import__("base64").urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
            + b","
            + str(len(value)).encode()
            + b"\n"
            for name, value in rows.items()
        )
        + b"ea_quant-0.1.1.dist-info/RECORD,,\n"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, value in rows.items():
            archive.writestr(name, value)
        archive.writestr("ea_quant-0.1.1.dist-info/RECORD", record)

    assert _wheel_owned_rows(wheel.read_bytes()) == tuple(sorted(rows.items()))


def test_wheel_owned_rows_converts_missing_record_member_to_provenance_error(
    tmp_path: Path,
) -> None:
    from ea.experiments.provenance import ProvenanceError, _wheel_owned_rows

    wheel = tmp_path / "ea_quant-0.1.1-py3-none-any.whl"
    required = "ea_quant-0.1.1.dist-info"
    rows = {
        "ea/__init__.py": b"value = 1\n",
        required + "/METADATA": b"Name: ea-quant\n",
        required + "/WHEEL": b"Wheel-Version: 1.0\n",
        required + "/entry_points.txt": b"[console_scripts]\n",
        required + "/top_level.txt": b"ea\n",
    }
    record = (
        b"".join(
            name.encode()
            + b",sha256="
            + __import__("base64").urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
            + b","
            + str(len(value)).encode()
            + b"\n"
            for name, value in rows.items()
        )
        + (required + "/RECORD,,\n").encode()
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ea/__init__.py", rows["ea/__init__.py"])
        archive.writestr(required + "/RECORD", record)

    with pytest.raises(ProvenanceError):
        _wheel_owned_rows(wheel.read_bytes())


def test_local_wheel_artifact_converts_malformed_bracketed_file_url_to_provenance_error() -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(
            '{"url":"file://[bad/ea.whl","archive_info":{"hash":"sha256=' + "0" * 64 + '"}}'
        )


def test_wheel_owned_rows_converts_oversized_record_field_to_provenance_error(
    tmp_path: Path,
) -> None:
    from ea.experiments.provenance import ProvenanceError, _wheel_owned_rows

    wheel = tmp_path / "ea_quant-0.1.1-py3-none-any.whl"
    record = b"x" * (csv.field_size_limit() + 1) + b",sha256=" + b"a" * 43 + b",1\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ea_quant-0.1.1.dist-info/RECORD", record)

    with pytest.raises(ProvenanceError):
        _wheel_owned_rows(wheel.read_bytes())


def test_installed_rows_read_a_closed_safe_tree_and_match_wheel_rows(tmp_path: Path) -> None:
    import importlib.metadata

    from ea.experiments.provenance import _installed_wheel_rows

    root = tmp_path / "site"
    rows = {
        "ea/__init__.py": b"",
        "ea/worker.py": b"value = 1\n",
        "ea_quant-0.1.1.dist-info/METADATA": b"Name: ea-quant\n",
        "ea_quant-0.1.1.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "ea_quant-0.1.1.dist-info/entry_points.txt": b"[console_scripts]\n",
        "ea_quant-0.1.1.dist-info/top_level.txt": b"ea\n",
    }
    for name, content in rows.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    class ClosedDistribution:
        files = tuple(rows)

        def locate_file(self, path: object) -> Path:
            return root / str(path)

    distribution = cast(importlib.metadata.Distribution, ClosedDistribution())
    expected = tuple(sorted(rows.items()))

    assert _installed_wheel_rows(distribution, expected) == expected


def test_installed_runtime_collector_binds_pip_admission_artifact_and_closed_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.metadata

    import ea.experiments.provenance as provenance

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def locate_file(self, _path: object) -> Path:
            return tmp_path

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    distribution = Distribution()
    monkeypatch.setattr(importlib.metadata, "distributions", lambda **_kwargs: [distribution])
    monkeypatch.setattr(provenance, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(provenance, "_active_metadata_roots", lambda: (tmp_path,))
    monkeypatch.setattr(provenance, "_validate_installed_sys_path", lambda _roots: None)
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance, "_installed_wheel_rows", lambda *_args: (("ea/__init__.py", b""),)
    )
    monkeypatch.setattr(provenance, "_validate_installed_ea_import", lambda *_args: None)

    collected = provenance.collect_installed_runtime_spec_v2(require_artifact=False)

    assert collected.provenance_kind == "installed_local_wheel_v1"
    assert collected.ea_distribution.name == "ea-quant"


def test_installed_runtime_collector_requires_isolated_runtime_before_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata

    import ea.experiments.provenance as provenance

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    distribution = Distribution()
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [distribution])
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance, "_installed_wheel_rows", lambda *_args: (("ea/__init__.py", b""),)
    )

    def reject_runtime() -> None:
        raise provenance.ProvenanceError("non-isolated runtime")

    monkeypatch.setattr(provenance, "_require_runtime_flags", reject_runtime)

    with pytest.raises(provenance.ProvenanceError, match="non-isolated runtime"):
        provenance.collect_installed_runtime_spec_v2(require_artifact=False)


def test_installed_runtime_collector_requires_active_metadata_roots_before_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata

    import ea.experiments.provenance as provenance

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    distribution = Distribution()
    monkeypatch.setattr(importlib.metadata, "distributions", lambda **_kwargs: [distribution])
    monkeypatch.setattr(provenance, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(
        provenance,
        "_active_metadata_roots",
        lambda: (_ for _ in ()).throw(provenance.ProvenanceError("active roots rejected")),
    )
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance, "_installed_wheel_rows", lambda *_args: (("ea/__init__.py", b""),)
    )

    with pytest.raises(provenance.ProvenanceError, match="active roots rejected"):
        provenance.collect_installed_runtime_spec_v2(require_artifact=False)


def test_installed_runtime_collector_rejects_an_injected_import_root_before_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.metadata
    import sys

    import ea.experiments.provenance as provenance

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    distribution = Distribution()
    monkeypatch.setattr(importlib.metadata, "distributions", lambda **_kwargs: [distribution])
    monkeypatch.setattr(provenance, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(provenance, "_active_metadata_roots", lambda: (Path("/active"),))
    monkeypatch.setattr(sys, "path", [*sys.path, str(tmp_path)])
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance, "_installed_wheel_rows", lambda *_args: (("ea/__init__.py", b""),)
    )

    with pytest.raises(provenance.ProvenanceError, match="injected import root"):
        provenance.collect_installed_runtime_spec_v2(require_artifact=False)


def test_installed_runtime_collector_scopes_metadata_to_active_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.metadata

    import ea.experiments.provenance as provenance

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def locate_file(self, _path: object) -> Path:
            return tmp_path

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    observed: list[object] = []

    def distributions(*, path: object = None) -> list[Distribution]:
        observed.append(path)
        return [Distribution()]

    monkeypatch.setattr(importlib.metadata, "distributions", distributions)
    monkeypatch.setattr(provenance, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(provenance, "_active_metadata_roots", lambda: (tmp_path,))
    monkeypatch.setattr(provenance, "_validate_installed_sys_path", lambda _roots: None)
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance, "_installed_wheel_rows", lambda *_args: (("ea/__init__.py", b""),)
    )
    monkeypatch.setattr(provenance, "_validate_installed_ea_import", lambda *_args: None)

    provenance.collect_installed_runtime_spec_v2(require_artifact=False)

    assert observed == [[str(tmp_path)]]


def test_installed_runtime_collector_rejects_distribution_outside_active_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.metadata

    import ea.experiments.provenance as provenance

    active = tmp_path / "active"
    foreign = tmp_path / "foreign"
    active.mkdir()
    foreign.mkdir()

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def locate_file(self, _path: object) -> Path:
            return foreign

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    monkeypatch.setattr(importlib.metadata, "distributions", lambda **_kwargs: [Distribution()])
    monkeypatch.setattr(provenance, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(provenance, "_active_metadata_roots", lambda: (active,))
    monkeypatch.setattr(provenance, "_validate_installed_sys_path", lambda _roots: None)
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance, "_installed_wheel_rows", lambda *_args: (("ea/__init__.py", b""),)
    )

    with pytest.raises(provenance.ProvenanceError, match="escaped active metadata roots"):
        provenance.collect_installed_runtime_spec_v2(require_artifact=False)


def test_installed_runtime_collector_rejects_foreign_executing_ea_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.metadata
    import importlib.util
    from types import SimpleNamespace

    import ea
    import ea.experiments.provenance as provenance

    package = tmp_path / "ea"
    package.mkdir()
    expected_init = package / "__init__.py"
    expected_init.write_text('__version__ = "0.1.1"\n')
    foreign_init = tmp_path / "foreign.py"
    foreign_init.write_text('__version__ = "0.1.1"\n')

    class Distribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def locate_file(self, _path: object) -> Path:
            return tmp_path

        def read_text(self, name: str) -> str:
            return {"INSTALLER": "pip\n", "direct_url.json": "direct-url"}[name]

    monkeypatch.setattr(importlib.metadata, "distributions", lambda **_kwargs: [Distribution()])
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.1.1")
    monkeypatch.setattr(provenance, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(provenance, "_active_metadata_roots", lambda: (tmp_path,))
    monkeypatch.setattr(provenance, "_validate_installed_sys_path", lambda _roots: None)
    monkeypatch.setattr(
        provenance, "_local_wheel_artifact", lambda *_args, **_kwargs: (None, "1" * 64)
    )
    monkeypatch.setattr(
        provenance,
        "_installed_wheel_rows",
        lambda *_args: (("ea/__init__.py", expected_init.read_bytes()),),
    )
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(
            origin=str(expected_init), submodule_search_locations=[str(package)]
        ),
    )
    monkeypatch.setattr(ea, "__file__", str(foreign_init))

    with pytest.raises(provenance.ProvenanceError, match="executing EA package"):
        provenance.collect_installed_runtime_spec_v2(require_artifact=False)


@pytest.mark.parametrize(
    ("raw", "admitted"),
    [
        (None, True),
        (
            json.dumps(
                {
                    "url": "file:///private/tmp/ea.whl",
                    "archive_info": {"hash": "sha256=" + "1" * 64},
                }
            ),
            True,
        ),
        (
            json.dumps(
                {
                    "url": "file:///private/tmp/ea.whl",
                    "archive_info": {"hashes": {"sha256": "1" * 64}},
                }
            ),
            True,
        ),
        (object(), False),
        ("not-json", False),
        ("[]", False),
        (json.dumps({"url": "file:///bad path.whl", "archive_info": {}}), False),
        (json.dumps({"url": "file:/bad%ZZ.whl", "archive_info": {}}), False),
        (json.dumps({"url": "https://host/ea.whl", "archive_info": {}}), False),
        (json.dumps({"url": "file://host/ea.whl", "archive_info": {}}), False),
        (json.dumps({"url": "file:///tmp/ea.txt", "archive_info": {}}), False),
        (
            json.dumps(
                {
                    "url": "file:///tmp/ea.whl",
                    "archive_info": {"hash": "sha256=" + "1" * 64, "hashes": {"sha256": "2" * 64}},
                }
            ),
            False,
        ),
        (json.dumps({"url": "file:///tmp/ea.whl", "archive_info": {"hash": "bad"}}), False),
        (json.dumps({"url": "file:///tmp/ea.whl", "archive_info": {"hashes": None}}), False),
        (
            json.dumps(
                {"url": "file:///tmp/ea.whl", "archive_info": {"hashes": {"sha256": "A" * 64}}}
            ),
            False,
        ),
    ],
)
def test_legacy_direct_url_validator_is_closed_for_every_unsupported_surface(
    raw: object, admitted: bool
) -> None:
    import importlib.metadata

    from ea.experiments.provenance import ProvenanceError, _validate_installed_direct_url

    class Distribution:
        def read_text(self, _name: str) -> object:
            return raw

    distribution = cast(importlib.metadata.Distribution, Distribution())
    if admitted:
        _validate_installed_direct_url(distribution)
    else:
        with pytest.raises(ProvenanceError):
            _validate_installed_direct_url(distribution)


@pytest.mark.parametrize(
    "case",
    [
        "configuration_schema",
        "configuration_environment",
        "code_commit",
        "distribution_name",
        "distribution_version",
        "runtime_token",
        "runtime_duplicate_distribution",
        "runtime_missing_ea",
        "runtime_lock_type",
        "parameter_kind",
        "parameter_boolean",
        "parameter_integer",
        "parameter_float",
        "parameter_string",
        "parameter_value_type",
        "randomness_generator",
        "randomness_labels",
        "installed_numeric_policy",
        "installed_provenance_kind",
        "installed_ea_name",
        "installed_digest",
        "installed_inventory",
        "installed_token",
        "randomness_seed",
        "randomness_tuple",
        "randomness_derivation",
        "runtime_spec_lock",
        "runtime_spec_policy",
        "configuration_digest",
        "code_spec_clean",
        "inputs_configuration",
        "inputs_data",
        "inputs_replay_window",
        "inputs_parameters",
        "inputs_runtime",
        "inputs_seed",
        "inputs_seed_type",
        "inputs_parameters_duplicate",
        "runtime_distribution_list",
        "runtime_distribution_identity",
        "inputs_stream_tuple",
        "inputs_stream_duplicate",
    ],
)
def test_manifest_codec_rejects_each_closed_validator_surface(case: str) -> None:
    from dataclasses import replace

    from ea.core.run import DataFingerprint, ReplayWindow, Sha256Digest
    from ea.experiments._manifest_model import (
        CodeEvidence,
        CodeSpec,
        ConfigurationSpec,
        DistributionIdentity,
        EffectiveParameter,
        InstalledRuntimeSpecV2,
        ManifestError,
        NormalizedConfiguration,
        ParameterKind,
        RandomnessSpec,
        RuntimeEvidence,
        RuntimeSpec,
    )

    ea_distribution = DistributionIdentity("ea-quant", "1")

    def runtime_evidence(
        *,
        python_cache_tag: str = "cpython-312",
        distributions: tuple[DistributionIdentity, ...] = (ea_distribution,),
        uv_lock_bytes: bytes = b"",
    ) -> RuntimeEvidence:
        return RuntimeEvidence(
            ea_version="1",
            python_implementation="cpython",
            python_version="3.12",
            python_cache_tag=python_cache_tag,
            sys_platform="linux",
            platform_tag="linux-x86_64",
            distributions=distributions,
            uv_lock_bytes=uv_lock_bytes,
        )

    def installed_runtime(
        *,
        ea: DistributionIdentity = ea_distribution,
        digest: Sha256Digest | None = None,
        python_cache_tag: str = "cpython-312",
        distributions: tuple[DistributionIdentity, ...] = (ea_distribution,),
        numeric_policy: str = "deterministic-ordered-float64-v1",
        provenance_kind: str = "installed_local_wheel_v1",
    ) -> InstalledRuntimeSpecV2:
        return InstalledRuntimeSpecV2(
            ea_distribution=ea,
            ea_installed_files_sha256=Sha256Digest("1" * 64) if digest is None else digest,
            python_implementation="cpython",
            python_version="3.12",
            python_cache_tag=python_cache_tag,
            sys_platform="linux",
            platform_tag="linux-x86_64",
            distributions=distributions,
            numeric_policy=numeric_policy,
            provenance_kind=provenance_kind,
        )

    with pytest.raises(ManifestError):
        if case == "configuration_schema":
            NormalizedConfiguration(2, "development", "backtest")
        elif case == "configuration_environment":
            NormalizedConfiguration(1, "local", "backtest")
        elif case == "code_commit":
            CodeEvidence("A" * 40)
        elif case == "distribution_name":
            DistributionIdentity("ea_quant", "1")
        elif case == "distribution_version":
            DistributionIdentity("ea-quant", "")
        elif case == "runtime_token":
            runtime_evidence(python_cache_tag="")
        elif case == "runtime_duplicate_distribution":
            runtime_evidence(distributions=(ea_distribution, ea_distribution))
        elif case == "runtime_missing_ea":
            runtime_evidence(distributions=(DistributionIdentity("numpy", "1"),))
        elif case == "runtime_lock_type":
            runtime_evidence(uv_lock_bytes=cast(bytes, "lock"))
        elif case == "runtime_distribution_list":
            runtime_evidence(
                distributions=cast(tuple[DistributionIdentity, ...], [ea_distribution])
            )
        elif case == "runtime_distribution_identity":
            runtime_evidence(distributions=cast(tuple[DistributionIdentity, ...], (object(),)))
        elif case == "parameter_kind":
            EffectiveParameter("value", cast(ParameterKind, "boolean"), True)
        elif case == "parameter_boolean":
            EffectiveParameter("value", ParameterKind.BOOLEAN, cast(bool, 1))
        elif case == "parameter_integer":
            EffectiveParameter("value", ParameterKind.INTEGER, cast(int, True))
        elif case == "parameter_float":
            EffectiveParameter("value", ParameterKind.FLOAT64, float("nan"))
        elif case == "parameter_string":
            EffectiveParameter("value", ParameterKind.STRING, "bad\nvalue")
        elif case == "parameter_value_type":
            EffectiveParameter.from_value("value", cast(bool | float | int | str, object()))
        elif case == "randomness_generator":
            RandomnessSpec(1, (), generator="random")
        elif case == "randomness_labels":
            RandomnessSpec(1, ("alpha", "alpha"))
        elif case == "installed_numeric_policy":
            installed_runtime(numeric_policy="other")
        elif case == "installed_provenance_kind":
            installed_runtime(provenance_kind="other")
        elif case == "installed_ea_name":
            installed_runtime(ea=DistributionIdentity("numpy", "1"))
        elif case == "installed_digest":
            installed_runtime(digest=cast(Sha256Digest, "digest"))
        elif case == "installed_inventory":
            installed_runtime(distributions=(DistributionIdentity("numpy", "1"),))
        elif case == "installed_token":
            installed_runtime(python_cache_tag="")
        elif case == "randomness_seed":
            RandomnessSpec(-1, ())
        elif case == "randomness_tuple":
            RandomnessSpec(1, cast(tuple[str, ...], ["alpha"]))
        elif case == "randomness_derivation":
            RandomnessSpec(1, (), stream_derivation="random")
        elif case == "runtime_spec_lock":
            RuntimeSpec(
                "1",
                "cpython",
                "3.12",
                "cpython-312",
                "linux",
                "linux-x86_64",
                (ea_distribution,),
                cast(Sha256Digest, "lock"),
            )
        elif case == "runtime_spec_policy":
            RuntimeSpec(
                "1",
                "cpython",
                "3.12",
                "cpython-312",
                "linux",
                "linux-x86_64",
                (ea_distribution,),
                Sha256Digest("1" * 64),
                "other",
            )
        elif case == "configuration_digest":
            ConfigurationSpec(
                NormalizedConfiguration(1, "development", "backtest"), Sha256Digest("1" * 64)
            )
        elif case == "code_spec_clean":
            CodeSpec("1" * 40, False)
        else:
            from unit.test_manifest import _inputs

            inputs = _inputs()
            if case == "inputs_configuration":
                replace(inputs, configuration=cast(NormalizedConfiguration, object()))
            elif case == "inputs_data":
                replace(inputs, data=cast(DataFingerprint, object()))
            elif case == "inputs_replay_window":
                replace(inputs, replay_window=cast(ReplayWindow, object()))
            elif case == "inputs_parameters":
                replace(inputs, parameters=cast(tuple[EffectiveParameter, ...], []))
            elif case == "inputs_runtime":
                replace(inputs, runtime=cast(RuntimeEvidence, object()))
            elif case == "inputs_seed":
                replace(inputs, master_seed=-1)
            elif case == "inputs_seed_type":
                replace(inputs, master_seed=cast(int, True))
            elif case == "inputs_parameters_duplicate":
                replace(inputs, parameters=(inputs.parameters[0], inputs.parameters[0]))
            elif case == "inputs_stream_tuple":
                replace(inputs, stream_labels=cast(tuple[str, ...], ["alpha"]))
            else:
                replace(inputs, stream_labels=("alpha", "alpha"))


@pytest.mark.parametrize("drift", ["runtime", "policy", "risk"])
def test_product_boundary_fails_closed_for_every_runtime_or_policy_drift(
    monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import LineageSpecV2
    from ea.experiments.provenance import ProvenanceError

    runtime = object()
    policy = object()
    digest = object()
    spec = SimpleNamespace(
        runtime=runtime if drift != "runtime" else object(),
        execution_policy=policy,
        risk_policy_id="risk.v1",
        risk_policy_sha256=digest,
    )
    risk = SimpleNamespace(policy_id="risk.v1", digest=digest)
    monkeypatch.setattr(
        product_kernel,
        "collect_installed_runtime_spec_v2",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(product_kernel, "phase1_risk_policy_digest", lambda value: value.digest)
    if drift == "policy":
        policy = object()
    if drift == "risk":
        risk.digest = object()

    with pytest.raises(product_kernel.ProductKernelError):
        product_kernel._require_product_boundary(
            cast(LineageSpecV2, spec),
            cast(ExecutionPolicyRef, policy),
            cast(Phase1RiskPolicy, risk),
            require_artifact=True,
        )

    monkeypatch.setattr(
        product_kernel,
        "collect_installed_runtime_spec_v2",
        lambda **_kwargs: (_ for _ in ()).throw(ProvenanceError("missing")),
    )
    with pytest.raises(product_kernel.ProductKernelError, match="provenance") as raised:
        product_kernel._require_product_boundary(
            cast(LineageSpecV2, spec),
            cast(ExecutionPolicyRef, policy),
            cast(Phase1RiskPolicy, risk),
            require_artifact=False,
        )
    assert raised.value.code is product_kernel.ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT
    assert isinstance(raised.value.__cause__, ProvenanceError)


def test_public_prepare_maps_collision_and_does_not_create_a_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import LineageSpecV2
    from ea.experiments.store import LocalResultStore, RunIdProvider, StoreCollisionError

    class Spec:
        pass

    spec = Spec()
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "LineageSpecV2", Spec)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        LocalResultStore,
        "prepare_product",
        lambda *_args: (_ for _ in ()).throw(StoreCollisionError("occupied")),
    )

    with pytest.raises(product_kernel.ProductKernelError, match="already exists") as error:
        product_kernel.prepare_phase1_product_kernel(
            store=store,
            spec=cast(LineageSpecV2, spec),
            run_id_provider=cast(RunIdProvider, object()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert error.value.code is product_kernel.ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        ("incomplete", "INTEGRITY_AUDIT_INCOMPLETE", "lacks"),
        ("corrupt", "INTEGRITY_AUDIT_CORRUPT", "corrupt"),
        ("scenario", "INTEGRITY_SCENARIO_DRIFT", "scenario"),
        ("store", "INTEGRITY_MANIFEST_DRIFT", "evidence"),
    ],
)
def test_public_recover_stably_classifies_store_boundary_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    code: str,
    message: str,
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import (
        CorruptAuditRecoveryError,
        IncompleteAuditRecoveryError,
        LocalResultStore,
        ScenarioRecoveryDriftError,
        StoreError,
    )

    class Manifest:
        spec = object()

    errors = {
        "incomplete": IncompleteAuditRecoveryError("missing"),
        "corrupt": CorruptAuditRecoveryError("corrupt"),
        "scenario": ScenarioRecoveryDriftError("scenario"),
        "store": StoreError("store"),
    }
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        LocalResultStore,
        "verify_recovery_attempt",
        lambda *_args: (_ for _ in ()).throw(errors[failure]),
    )

    with pytest.raises(product_kernel.ProductKernelError, match=message) as error:
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert error.value.code.name == code
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("replay", [False, True])
def test_kernel_materializes_one_funded_attempt_from_closed_preparation_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replay: bool
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.composition.product_kernel import Phase1ProductKernel
    from ea.core.audit import AuditRecordKind
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.initial_funding import InitialFundingOutcome, InitialFundingResult
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    acknowledgement = SimpleNamespace(record_sha256=object())
    subject, payload = object(), b"outcome"
    replay_record = SimpleNamespace(
        record_kind=AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME,
        subject_sha256=subject,
        canonical_payload=payload,
    )
    records = SimpleNamespace(
        record_count=2 if replay else 1,
        record_at=lambda index: replay_record if index == 1 else object(),
    )
    journal = SimpleNamespace(recovery_records=records, close=lambda: None)
    audit = SimpleNamespace(binding=object())
    prepared = SimpleNamespace(audit=audit)
    outcome = SimpleNamespace(result=InitialFundingResult.APPLIED)
    ledger = SimpleNamespace(
        apply_initial_funding=lambda *_args, **_kwargs: outcome, snapshot=object()
    )
    risk = SimpleNamespace(
        risk_state=SimpleNamespace(policy_sha256=object()),
        policy=SimpleNamespace(policy_id="risk.v1"),
    )
    manifest = SimpleNamespace(run_id=object(), spec=SimpleNamespace(initial_funding=object()))
    appended: list[dict[str, object]] = []

    monkeypatch.setattr(
        product_kernel,
        "BoundAuditPort",
        lambda *_args: SimpleNamespace(append=lambda **kwargs: appended.append(kwargs)),
    )
    monkeypatch.setattr(product_kernel, "create_portfolio_ledger", lambda *_args: ledger)
    monkeypatch.setattr(
        product_kernel, "create_audit_append_acknowledgement", lambda _record: acknowledgement
    )
    monkeypatch.setattr(
        product_kernel, "canonical_initial_funding_outcome_bytes", lambda _outcome: payload
    )
    monkeypatch.setattr(product_kernel, "initial_funding_outcome_digest", lambda _outcome: subject)
    monkeypatch.setattr(product_kernel, "create_phase1_risk_authority", lambda **_kwargs: risk)
    monkeypatch.setattr(
        product_kernel, "create_phase1_ledger_handoff_authority", lambda *_args: object()
    )
    monkeypatch.setattr(
        product_kernel, "create_phase1_portfolio_risk_refresh_authority", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        product_kernel, "create_acknowledged_lifecycle_frontier", lambda **_kwargs: object()
    )
    monkeypatch.setattr(LocalResultStore, "_retire_product_attempt", lambda *_args: None)

    kernel = product_kernel._make_kernel(
        LocalResultStore(tmp_path),
        prepared,
        cast(RunManifestV2, manifest),
        cast(InstrumentExecutionSpecSet, object()),
        cast(ExecutionPolicyRef, object()),
        cast(Phase1RiskPolicy, object()),
        journal,
    )

    assert kernel.funding_outcome is cast(InitialFundingOutcome, outcome)
    assert kernel.binding is audit.binding
    assert kernel.portfolio_snapshot is ledger.snapshot
    assert kernel.risk_state is risk.risk_state
    with pytest.raises(AttributeError, match="immutable"):
        kernel.__setattr__("binding", object())
    with pytest.raises(product_kernel.ProductKernelError, match="invalid handoff"):
        product_kernel._retire_phase1_product_kernel(cast(Phase1ProductKernel, object()))
    product_kernel._retire_phase1_product_kernel(kernel)
    product_kernel._retire_phase1_product_kernel(kernel)
    assert len(appended) == (0 if replay else 1)


def test_kernel_rejects_a_replayed_funding_record_with_different_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.audit import AuditRecordKind
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.initial_funding import InitialFundingResult
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    subject = object()
    journal = SimpleNamespace(
        recovery_records=SimpleNamespace(
            record_count=2,
            record_at=lambda index: SimpleNamespace(
                record_kind=AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME,
                subject_sha256=object() if index == 1 else subject,
                canonical_payload=b"other" if index == 1 else b"outcome",
            ),
        )
    )
    ledger = SimpleNamespace(
        apply_initial_funding=lambda *_args, **_kwargs: SimpleNamespace(
            result=InitialFundingResult.APPLIED
        )
    )
    monkeypatch.setattr(product_kernel, "BoundAuditPort", lambda *_args: object())
    monkeypatch.setattr(product_kernel, "create_portfolio_ledger", lambda *_args: ledger)
    monkeypatch.setattr(
        product_kernel,
        "create_audit_append_acknowledgement",
        lambda _record: SimpleNamespace(record_sha256=object()),
    )
    monkeypatch.setattr(
        product_kernel, "canonical_initial_funding_outcome_bytes", lambda _: b"outcome"
    )
    monkeypatch.setattr(product_kernel, "initial_funding_outcome_digest", lambda _: subject)

    with pytest.raises(product_kernel.ProductKernelError, match="funding replay differs") as raised:
        product_kernel._make_kernel(
            LocalResultStore(tmp_path),
            SimpleNamespace(audit=SimpleNamespace(binding=object())),
            cast(
                RunManifestV2,
                SimpleNamespace(run_id=object(), spec=SimpleNamespace(initial_funding=object())),
            ),
            cast(InstrumentExecutionSpecSet, object()),
            cast(ExecutionPolicyRef, object()),
            cast(Phase1RiskPolicy, object()),
            journal,
        )

    assert raised.value.code is product_kernel.ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT


@pytest.mark.parametrize("record_count", [0, 3])
def test_kernel_rejects_audit_prefixes_outside_funding_recovery_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_count: int
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    journal = SimpleNamespace(recovery_records=SimpleNamespace(record_count=record_count))
    monkeypatch.setattr(product_kernel, "BoundAuditPort", lambda *_args: object())
    with pytest.raises(
        product_kernel.ProductKernelError, match="unsupported audit prefix"
    ) as raised:
        product_kernel._make_kernel(
            LocalResultStore(tmp_path),
            SimpleNamespace(audit=object()),
            cast(RunManifestV2, object()),
            cast(InstrumentExecutionSpecSet, object()),
            cast(ExecutionPolicyRef, object()),
            cast(Phase1RiskPolicy, object()),
            journal,
        )

    assert (
        raised.value.code
        is product_kernel.ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_RECOVERY_BOUNDARY
    )


def test_kernel_rejects_an_initial_funding_conflict_before_risk_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.initial_funding import InitialFundingResult
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    journal = SimpleNamespace(
        recovery_records=SimpleNamespace(record_count=1, record_at=lambda _index: object())
    )
    ledger = SimpleNamespace(
        apply_initial_funding=lambda *_args, **_kwargs: SimpleNamespace(
            result=InitialFundingResult.CONFLICT
        )
    )
    monkeypatch.setattr(product_kernel, "BoundAuditPort", lambda *_args: object())
    monkeypatch.setattr(product_kernel, "create_portfolio_ledger", lambda *_args: ledger)
    monkeypatch.setattr(
        product_kernel,
        "create_audit_append_acknowledgement",
        lambda _record: SimpleNamespace(record_sha256=object()),
    )

    with pytest.raises(product_kernel.ProductKernelError, match="funding conflict") as raised:
        product_kernel._make_kernel(
            LocalResultStore(tmp_path),
            SimpleNamespace(audit=SimpleNamespace(binding=object())),
            cast(
                RunManifestV2,
                SimpleNamespace(run_id=object(), spec=SimpleNamespace(initial_funding=object())),
            ),
            cast(InstrumentExecutionSpecSet, object()),
            cast(ExecutionPolicyRef, object()),
            cast(Phase1RiskPolicy, object()),
            journal,
        )

    assert raised.value.code is product_kernel.ProductKernelFailureCode.FUNDING_CONFLICT


def test_public_recover_reopens_only_the_closed_incomplete_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    class Manifest:
        spec = object()

    class Incomplete:
        pass

    binding, prepared, journal, result = (
        Incomplete(),
        SimpleNamespace(audit=object()),
        object(),
        object(),
    )
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "VerifiedIncompleteRecoveryBinding", Incomplete)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: binding)
    monkeypatch.setattr(LocalResultStore, "recover_incomplete_attempt", lambda *_args: prepared)
    monkeypatch.setattr(product_kernel, "reopen_posix_audit_journal", lambda _audit: journal)
    monkeypatch.setattr(product_kernel, "_make_kernel", lambda *_args: result)

    assert (
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )
        is result
    )


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("funding", "FUNDING_SPEC_MISMATCH"),
        ("risk", "INTEGRITY_RISK_STATE_DRIFT"),
        ("audit", "INTEGRITY_AUDIT_CORRUPT"),
        ("product", "INTEGRITY_MANIFEST_DRIFT"),
    ],
)
def test_public_prepare_closes_and_retires_every_post_prepare_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, code: str
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.initial_funding import InitialFundingError
    from ea.core.outcomes import OutcomeCode
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import LineageSpecV2
    from ea.experiments.store import LocalResultStore, RunIdProvider
    from ea.risk.authority import RiskAuthorityError

    class Spec:
        pass

    class Manifest:
        def __init__(self, spec: Spec) -> None:
            self.spec = spec

    if failure == "funding":
        error: BaseException = InitialFundingError("funding")
    elif failure == "risk":
        error = RiskAuthorityError(OutcomeCode.INVALID_TYPE, "risk")
    elif failure == "audit":
        error = OSError("audit")
    else:
        error = product_kernel.ProductKernelError(
            product_kernel.ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT, "product"
        )
    spec, audit = Spec(), object()
    prepared = SimpleNamespace(audit=audit, manifest_verification=object())
    journal = SimpleNamespace(closes=0)
    journal.close = lambda: setattr(journal, "closes", journal.closes + 1)
    retired: list[object] = []
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "LineageSpecV2", Spec)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "prepare_product", lambda *_args: prepared)
    monkeypatch.setattr(LocalResultStore, "verify_manifest", lambda *_args: Manifest(spec))
    monkeypatch.setattr(product_kernel, "create_posix_audit_journal", lambda *_args: journal)
    monkeypatch.setattr(product_kernel, "_make_kernel", lambda *_args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(
        LocalResultStore, "_retire_product_attempt", lambda _self, item: retired.append(item)
    )

    with pytest.raises(product_kernel.ProductKernelError) as raised:
        product_kernel.prepare_phase1_product_kernel(
            store=store,
            spec=cast(LineageSpecV2, spec),
            run_id_provider=cast(RunIdProvider, object()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert raised.value.code.name == code
    assert journal.closes == 1
    assert retired == [audit]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("funding", "FUNDING_SPEC_MISMATCH"),
        ("risk", "INTEGRITY_RISK_STATE_DRIFT"),
        ("audit", "INTEGRITY_AUDIT_CORRUPT"),
        ("unknown", "passthrough"),
    ],
)
def test_public_recover_closes_and_retires_every_reopened_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, code: str
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.initial_funding import InitialFundingError
    from ea.core.outcomes import OutcomeCode
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore
    from ea.risk.authority import RiskAuthorityError

    class Manifest:
        spec = object()

    class Incomplete:
        pass

    error: BaseException = {
        "funding": InitialFundingError("funding"),
        "risk": RiskAuthorityError(OutcomeCode.INVALID_TYPE, "risk"),
        "audit": OSError("audit"),
        "unknown": RuntimeError("unknown"),
    }[failure]
    binding, audit = Incomplete(), object()
    prepared = SimpleNamespace(audit=audit)
    journal = SimpleNamespace(closes=0)
    journal.close = lambda: setattr(journal, "closes", journal.closes + 1)
    retired: list[object] = []
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "VerifiedIncompleteRecoveryBinding", Incomplete)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: binding)
    monkeypatch.setattr(LocalResultStore, "recover_incomplete_attempt", lambda *_args: prepared)
    monkeypatch.setattr(product_kernel, "reopen_posix_audit_journal", lambda *_args: journal)
    monkeypatch.setattr(product_kernel, "_make_kernel", lambda *_args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(
        LocalResultStore, "_retire_product_attempt", lambda _self, item: retired.append(item)
    )

    expected_error: type[BaseException] = (
        RuntimeError if failure == "unknown" else product_kernel.ProductKernelError
    )
    with pytest.raises(expected_error) as raised:
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    if failure != "unknown":
        assert isinstance(raised.value, product_kernel.ProductKernelError)
        assert raised.value.code.name == code
    assert journal.closes == 1
    assert retired == [audit]


@pytest.mark.parametrize(
    ("consume_failure", "code"),
    [(False, "INTEGRITY_TERMINAL_DRIFT"), (True, "INTEGRITY_AUDIT_CORRUPT")],
)
def test_public_recover_consumes_terminal_evidence_before_retiring_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, consume_failure: bool, code: str
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    class Manifest:
        spec = object()

    class TerminalBinding:
        pass

    class Terminal:
        def __init__(self) -> None:
            self.finished = 0

        def _consume(self) -> None:
            if consume_failure:
                raise OSError("consume")

        def _finish(self) -> None:
            self.finished += 1

    binding, terminal = TerminalBinding(), Terminal()
    retired: list[object] = []
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "VerifiedTerminalRecoveryBinding", TerminalBinding)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: binding)
    monkeypatch.setattr(LocalResultStore, "recover_terminal_attempt", lambda *_args: terminal)
    monkeypatch.setattr(
        LocalResultStore,
        "_retire_verified_product_recovery",
        lambda _self, item: retired.append(item),
    )

    with pytest.raises(product_kernel.ProductKernelError) as raised:
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert raised.value.code.name == code
    assert terminal.finished == 1
    assert retired == [binding]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("funding", "FUNDING_SPEC_MISMATCH"),
        ("risk", "INTEGRITY_RISK_STATE_DRIFT"),
        ("audit", "INTEGRITY_AUDIT_CORRUPT"),
    ],
)
def test_public_recover_retires_unopened_binding_when_opening_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, code: str
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.initial_funding import InitialFundingError
    from ea.core.outcomes import OutcomeCode
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore
    from ea.risk.authority import RiskAuthorityError

    class Manifest:
        spec = object()

    class Incomplete:
        pass

    if failure == "funding":
        error: BaseException = InitialFundingError("funding")
    elif failure == "risk":
        error = RiskAuthorityError(OutcomeCode.INVALID_TYPE, "risk")
    else:
        error = OSError("audit")
    binding = Incomplete()
    retired: list[object] = []
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "VerifiedIncompleteRecoveryBinding", Incomplete)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: binding)
    monkeypatch.setattr(
        LocalResultStore, "recover_incomplete_attempt", lambda *_args: (_ for _ in ()).throw(error)
    )
    monkeypatch.setattr(
        LocalResultStore,
        "_retire_verified_product_recovery",
        lambda _self, item: retired.append(item),
    )

    with pytest.raises(product_kernel.ProductKernelError) as raised:
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert raised.value.code.name == code
    assert retired == [binding]


def test_public_recover_passes_unknown_opening_failure_after_retiring_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    class Manifest:
        spec = object()

    class Incomplete:
        pass

    binding, retired = Incomplete(), []
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "VerifiedIncompleteRecoveryBinding", Incomplete)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: binding)
    monkeypatch.setattr(
        LocalResultStore,
        "recover_incomplete_attempt",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unknown")),
    )
    monkeypatch.setattr(
        LocalResultStore,
        "_retire_verified_product_recovery",
        lambda _self, item: retired.append(item),
    )

    with pytest.raises(RuntimeError, match="unknown"):
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert retired == [binding]


def test_public_recover_rejects_unknown_verified_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    class Manifest:
        spec = object()

    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: object())

    with pytest.raises(product_kernel.ProductKernelError, match="unknown recovery") as raised:
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert (
        raised.value.code
        is product_kernel.ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_RECOVERY_BOUNDARY
    )


def test_public_recover_retires_terminal_binding_when_consumption_raises_unknown_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ea.composition.product_kernel as product_kernel
    from ea.core.execution import InstrumentExecutionSpecSet
    from ea.core.execution_messages import ExecutionPolicyRef
    from ea.core.risk import Phase1RiskPolicy
    from ea.experiments._manifest_model import RunManifestV2
    from ea.experiments.store import LocalResultStore

    class Manifest:
        spec = object()

    class TerminalBinding:
        pass

    class Terminal:
        finished = 0

        def _consume(self) -> None:
            raise RuntimeError("unknown")

        def _finish(self) -> None:
            self.finished += 1

    binding, terminal = TerminalBinding(), Terminal()
    retired: list[object] = []
    store = LocalResultStore(tmp_path)
    monkeypatch.setattr(product_kernel, "RunManifestV2", Manifest)
    monkeypatch.setattr(product_kernel, "VerifiedTerminalRecoveryBinding", TerminalBinding)
    monkeypatch.setattr(product_kernel, "_require_product_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(LocalResultStore, "verify_recovery_attempt", lambda *_args: binding)
    monkeypatch.setattr(LocalResultStore, "recover_terminal_attempt", lambda *_args: terminal)
    monkeypatch.setattr(
        LocalResultStore,
        "_retire_verified_product_recovery",
        lambda _self, item: retired.append(item),
    )

    with pytest.raises(RuntimeError, match="unknown"):
        product_kernel.recover_phase1_product_kernel(
            store=store,
            expected_manifest=cast(RunManifestV2, Manifest()),
            spec_set=cast(InstrumentExecutionSpecSet, object()),
            execution_policy=cast(ExecutionPolicyRef, object()),
            risk_policy=cast(Phase1RiskPolicy, object()),
        )

    assert terminal.finished == 1
    assert retired == [binding]


def test_local_wheel_artifact_rejects_relative_file_url_after_deletion() -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(
            '{"url":"file:relative.whl","archive_info":{"hash":"sha256=' + "0" * 64 + '"}}',
            require_artifact=False,
        )
