"""Focused reconstruction contracts for the Phase 1 V2 product boundary."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

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
        _wheel_owned_rows(wheel)


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

    assert _local_wheel_artifact(document) == (wheel, hashlib.sha256(b"wheel").hexdigest())


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

    assert _wheel_owned_rows(wheel) == tuple(sorted(rows.items()))


def test_local_wheel_artifact_rejects_relative_file_url_after_deletion() -> None:
    from ea.experiments.provenance import ProvenanceError, _local_wheel_artifact

    with pytest.raises(ProvenanceError):
        _local_wheel_artifact(
            '{"url":"file:relative.whl","archive_info":{"hash":"sha256=' + "0" * 64 + '"}}',
            require_artifact=False,
        )
