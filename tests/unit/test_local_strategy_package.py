"""Immutable local artifact contract, independent of a Python plugin installation."""

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest

from ea.strategy.package import StrategyPackageV1


def test_local_package_surface_exists() -> None:
    assert importlib.util.find_spec("ea.strategy.package") is not None


def test_deterministic_pack_and_identity(tmp_path: Path) -> None:
    from ea.strategy.package import pack_strategy, validate_package

    source = tmp_path / "source"
    source.mkdir()
    manifest = {
        "schema_version": 1,
        "package_id": "example.threshold",
        "strategy": {
            "id": "local-threshold-v1",
            "version": 1,
            "display_name": "Threshold",
            "parameters": [],
            "outcome_mode": "no_entry",
        },
    }
    (source / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )
    (source / "strategy.py").write_text(
        "def validate_parameters(parameters, context): pass\n"
        "class Logic:\n"
        " def on_bar(self, bar): return None\n"
        "def create_logic(parameters): return Logic()\n"
    )
    a, b = tmp_path / "a.eastrategy", tmp_path / "b.eastrategy"
    pack_strategy(source, a)
    pack_strategy(source, b)
    assert a.read_bytes() == b.read_bytes()
    package = validate_package(a.read_bytes())
    assert package.identity.artifact_sha256 == hashlib.sha256(a.read_bytes()).hexdigest()
    (source / "strategy.py").write_text((source / "strategy.py").read_text() + "\n")
    pack_strategy(source, tmp_path / "c.eastrategy")
    assert a.read_bytes() != (tmp_path / "c.eastrategy").read_bytes()


def test_additive_catalog_and_digest_rejection(tmp_path: Path) -> None:
    import pytest

    from ea.strategy.catalog import ResearchStrategyCatalogV1

    catalog = ResearchStrategyCatalogV1()
    assert catalog.get("bounded-long-v1", 1).descriptor.strategy_id == "bounded-long-v1"
    with pytest.raises(ValueError):
        catalog.resolve(
            {
                "id": "local-v1",
                "version": 1,
                "source": {
                    "kind": "local-package",
                    "package_id": "example.local",
                    "artifact_sha256": "a" * 64,
                },
            }
        )


def local_scenario(tmp_path: Path) -> tuple[Path, Path, StrategyPackageV1]:
    import yaml

    from ea.strategy.package import pack_strategy
    from unit.test_scenario_v2 import write_v2

    path = write_v2(tmp_path / "scenarios")
    source = tmp_path / "source"
    source.mkdir()
    manifest = {
        "schema_version": 1,
        "package_id": "example.threshold",
        "strategy": {
            "id": "local-close-threshold-entry-v1",
            "version": 1,
            "display_name": "Local threshold",
            "parameters": [
                dict(
                    name=name,
                    type="decimal",
                    required=True,
                    default=value,
                    static_minimum="0",
                    static_maximum=None,
                )
                for name, value in [("threshold_price", "1"), ("target_quantity", "2")]
            ],
            "outcome_mode": "optional_single_long_entry",
        },
    }
    (source / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )
    (source / "strategy.py").write_text(
        "from decimal import Decimal\n"
        "from ea.strategy.sdk_v1 import StrategyDecisionV1\n"
        "def validate_parameters(parameters, context): pass\n"
        "class Logic:\n"
        " def __init__(self,p): self.p=p\n"
        " def on_bar(self,bar):\n"
        '  if bar.close > Decimal(self.p["threshold_price"]):\n'
        '   return StrategyDecisionV1(self.p["target_quantity"])\n'
        "def create_logic(parameters): return Logic(parameters)\n"
    )
    root = tmp_path / "packages"
    root.mkdir()
    package = pack_strategy(source, root / "threshold.eastrategy")
    document = yaml.safe_load(path.read_text())
    document["schema_version"] = 3
    document["strategy"] = {
        "id": package.descriptor.strategy_id,
        "version": 1,
        "source": {
            "kind": "local-package",
            "package_id": package.identity.package_id,
            "artifact_sha256": package.identity.artifact_sha256,
        },
        "parameters": {"threshold_price": "1", "target_quantity": "2"},
    }
    path.write_text(yaml.safe_dump(document))
    return path, root, package


def test_v3_exact_execution_and_report_after_external_deletion(tmp_path: Path) -> None:
    from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario

    path, root, package = local_scenario(tmp_path)
    scenario = load_backtest_scenario(path, strategy_root=root)
    (root / "threshold.eastrategy").unlink()
    result = run_backtest_scenario(scenario, tmp_path / "runs")
    assert (result.output_directory / "strategy.eastrategy").read_bytes() == package.artifact_bytes
    report = generate_backtest_report(result.output_directory, tmp_path / "report")
    assert package.identity.artifact_sha256 in report.report.canonical_bytes.decode()


def test_web_local_history_survives_root_deletion(tmp_path: Path) -> None:
    import time

    from ea.web.service import WebService

    path, root, package = local_scenario(tmp_path)
    service = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    service.start()
    try:
        validated = service.validate_scenario(path.name)
        job, _ = service.create_job(
            request_id="local-one",
            scenario_id=path.name,
            input_identity=cast(dict[str, object], validated["input_identity"]),
        )
        job_id = job.job_id
        for _ in range(100):
            if service.get_job(job_id).status in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        assert service.get_job(job_id).status == "succeeded"
    finally:
        service.stop()
    (root / "threshold.eastrategy").unlink()
    restarted = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    restarted.start()
    try:
        presented = restarted.get_job(job_id).document(presentation=True)
        assert (
            cast(dict[str, Any], presented["strategy_descriptor"])["display_name"]
            == "Local threshold"
        )
        assert package.identity.artifact_sha256 in restarted.report(job_id).decode()
    finally:
        restarted.stop()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "directory",
        "symlink",
        "zip",
        "json",
        "utf8",
        "size",
        "timestamp",
        "duplicate",
        "digest",
    ],
)
def test_container_rejects_invalid_bytes(tmp_path: Path, mutation: str) -> None:
    import io
    import zipfile

    from ea.strategy.package import MAX_MEMBER_BYTES, _container, validate_package

    _, _, package = local_scenario(tmp_path)
    with zipfile.ZipFile(io.BytesIO(package.artifact_bytes)) as archive:
        manifest = archive.read("manifest.json")
        source = archive.read("strategy.py")
    payload = package.artifact_bytes
    if mutation in {"extra", "directory", "symlink", "timestamp", "duplicate"}:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("manifest.json", manifest)
            info = zipfile.ZipInfo("strategy.py")
            if mutation == "symlink":
                info.create_system = 3
                info.external_attr = 0o120777 << 16
            archive.writestr(info, source)
            if mutation in {"extra", "directory", "duplicate"}:
                archive.writestr(
                    {"extra": "other.py", "directory": "sub/x.py", "duplicate": "strategy.py"}[
                        mutation
                    ],
                    "x",
                )
        payload = buf.getvalue()
    elif mutation == "zip":
        payload = b"not zip"
    elif mutation == "json":
        payload = _container(manifest + b"\n", source)
    elif mutation == "utf8":
        payload = _container(manifest, b"\xff")
    elif mutation == "size":
        payload = _container(manifest, b"x" * (MAX_MEMBER_BYTES + 1))
    with pytest.raises(ValueError):
        validate_package(payload, expected_sha256="0" * 64 if mutation == "digest" else None)


@pytest.mark.parametrize("mutation", ["shadow", "strategy_duplicate", "package_duplicate"])
def test_catalog_rejects_collisions(tmp_path: Path, mutation: str) -> None:
    import io
    import zipfile

    from ea.strategy.catalog import ResearchStrategyCatalogV1
    from ea.strategy.package import _container, validate_package

    _, _, package = local_scenario(tmp_path)
    with zipfile.ZipFile(io.BytesIO(package.artifact_bytes)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    if mutation == "shadow":
        manifest["strategy"]["id"] = "bounded-long-v1"
    elif mutation == "strategy_duplicate":
        manifest["package_id"] = "other.package"
    else:
        manifest["strategy"]["id"] = "other-strategy-v1"
    changed = validate_package(
        _container(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            package.source_bytes,
        )
    )
    with pytest.raises(ValueError):
        ResearchStrategyCatalogV1((changed,) if mutation == "shadow" else (package, changed))


@pytest.mark.parametrize("mutation", ["file_symlink", "source_symlink", "extra", "relative"])
def test_packer_rejects_paths(tmp_path: Path, mutation: str) -> None:
    from ea.strategy.package import pack_strategy

    local_scenario(tmp_path)
    source = tmp_path / "source"
    if mutation == "file_symlink":
        content = (source / "strategy.py").read_bytes()
        (source / "strategy.py").unlink()
        (tmp_path / "external.py").write_bytes(content)
        (source / "strategy.py").symlink_to(tmp_path / "external.py")
    elif mutation == "source_symlink":
        (tmp_path / "linked").symlink_to(source, target_is_directory=True)
        source = tmp_path / "linked"
    elif mutation == "extra":
        (source / "extra").write_text("x")
    else:
        source = Path("relative")
    with pytest.raises(ValueError):
        pack_strategy(source, tmp_path / "reject.eastrategy")


def test_artifact_symlink_and_unknown_digest_reject(tmp_path: Path) -> None:
    import yaml

    from ea.product import load_backtest_scenario
    from ea.strategy.catalog import ResearchStrategyCatalogV1

    path, root, _ = local_scenario(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["strategy"]["source"]["artifact_sha256"] = "a" * 64
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError):
        load_backtest_scenario(path, strategy_root=root)
    (root / "escape.eastrategy").symlink_to(root / "threshold.eastrategy")
    with pytest.raises(ValueError):
        ResearchStrategyCatalogV1.from_root(root)


@pytest.mark.parametrize(
    "code",
    [
        "invalid python!",
        "import nonexistent_strategy_dependency",
        "def create_logic(p): pass",
    ],
)
def test_code_contract_failures_are_safe(tmp_path: Path, code: str) -> None:
    from ea.strategy.package import pack_strategy

    local_scenario(tmp_path)
    (tmp_path / "source" / "strategy.py").write_text(code)
    with pytest.raises(ValueError, match="strategy pack failed"):
        pack_strategy(tmp_path / "source", tmp_path / "invalid.eastrategy")


def test_v3_code_and_parameter_identity_changes(tmp_path: Path) -> None:
    import yaml

    from ea.product import load_backtest_scenario
    from ea.product.scenario import parameterize_strategy_scenario
    from ea.strategy.package import pack_strategy

    path, root, _ = local_scenario(tmp_path)
    original = load_backtest_scenario(path, strategy_root=root)
    changed_parameters = parameterize_strategy_scenario(
        original, initial_cash="10000", parameters={"threshold_price": "2", "target_quantity": "2"}
    )
    assert changed_parameters.scenario_sha256 != original.scenario_sha256
    source = tmp_path / "source" / "strategy.py"
    source.write_bytes(source.read_bytes() + b"\n")
    (root / "threshold.eastrategy").unlink()
    changed = pack_strategy(source.parent, root / "threshold.eastrategy")
    document = yaml.safe_load(path.read_text())
    document["strategy"]["source"]["artifact_sha256"] = changed.identity.artifact_sha256
    path.write_text(yaml.safe_dump(document))
    assert (
        load_backtest_scenario(path, strategy_root=root).scenario_sha256 != original.scenario_sha256
    )


def test_local_holdout_uses_source_bytes_after_deletion(tmp_path: Path) -> None:
    import time

    import yaml

    from ea.web.service import WebService
    from unit.test_web_holdout import _target

    path, root, package = local_scenario(tmp_path)
    (path.parent / "bounded-long.yaml").write_bytes(path.read_bytes())
    _target(path.parent)
    target_path = path.parent / "later.yaml"
    document = yaml.safe_load(target_path.read_text())
    document["strategy"].pop("target_quantity")
    document["strategy"]["parameters"]["target_quantity"] = "9"
    target_path.write_text(yaml.safe_dump(document))
    service = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    service.start()
    try:
        validated = service.validate_scenario(path.name)
        job, _ = service.create_job(
            request_id="local-source",
            scenario_id=path.name,
            input_identity=cast(dict[str, object], validated["input_identity"]),
        )
        for _ in range(200):
            if service.get_job(job.job_id).status in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert service.get_job(job.job_id).status == "succeeded"
        (root / "threshold.eastrategy").unlink()
        relation = service.create_holdout(source_job_id=job.job_id, scenario_id="later.yaml")
        for _ in range(200):
            holdout = service.get_job(relation["holdout_job_id"])
            if holdout.status in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert holdout.status == "succeeded"
        frozen = json.loads(holdout.input_snapshot_bytes or b"{}")["scenario"]["strategy"]
        assert frozen["parameters"]["target_quantity"] == "2"
        assert frozen["source"]["artifact_sha256"] == package.identity.artifact_sha256
        assert (
            service.inputs_dir / f"{holdout.job_id}.eastrategy"
        ).read_bytes() == package.artifact_bytes
    finally:
        service.stop()


@pytest.mark.parametrize(
    "decision",
    [
        "object()",
        'StrategyDecisionV1("01")',
        'StrategyDecisionV1("-1")',
        'StrategyDecisionV1("0.5")',
        "1/0",
    ],
)
def test_invalid_decision_retains_failure_without_order(tmp_path: Path, decision: str) -> None:
    import yaml

    from ea.product import BacktestRunFailure, load_backtest_scenario, run_backtest_scenario
    from ea.strategy.package import pack_strategy

    path, root, _ = local_scenario(tmp_path)
    source = tmp_path / "source" / "strategy.py"
    source.write_text(
        "from ea.strategy.sdk_v1 import StrategyDecisionV1\n"
        "def validate_parameters(parameters, context): pass\nclass Logic:\n"
        f" def on_bar(self,bar): return {decision}\n"
        "def create_logic(parameters): return Logic()\n"
    )
    (root / "threshold.eastrategy").unlink()
    package = pack_strategy(source.parent, root / "threshold.eastrategy")
    document = yaml.safe_load(path.read_text())
    document["strategy"]["source"]["artifact_sha256"] = package.identity.artifact_sha256
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(BacktestRunFailure) as caught:
        run_backtest_scenario(load_backtest_scenario(path, strategy_root=root), tmp_path / "runs")
    attempt = caught.value.output_directory
    assert not (attempt / "result.json").exists()
    assert (attempt / "failure.json").exists()
    assert "ZeroDivisionError" not in (attempt / "failure.json").read_text()


def test_sdk_bar_has_no_future_or_authority(tmp_path: Path) -> None:
    from dataclasses import fields

    from ea.strategy.sdk_v1 import StrategyBarV1, StrategyValidationContextV1

    assert {f.name for f in fields(StrategyBarV1)} == {
        "event_time",
        "available_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    assert {f.name for f in fields(StrategyValidationContextV1)} == {
        "history_bar_count",
        "last_entry_index",
        "quantity_quantum",
    }


def test_baseline_v2_identity_is_unchanged() -> None:
    from ea.product import load_backtest_scenario

    example = (
        Path(__file__).resolve().parents[2] / "examples/web-scenarios/moving-average-entry.yaml"
    )
    assert (
        load_backtest_scenario(example).scenario_sha256.value
        == "8d7108349c2e7f071b8e07b195a13a228312be53871d0eb8a1166325a094c3ea"
    )


def test_manifest_byte_change_changes_digest(tmp_path: Path) -> None:
    from ea.strategy.package import pack_strategy

    _, _, package = local_scenario(tmp_path)
    manifest = tmp_path / "source" / "manifest.json"
    manifest.write_bytes(manifest.read_bytes().replace(b"Local threshold", b"Local Threshold"))
    changed = pack_strategy(manifest.parent, tmp_path / "changed.eastrategy")
    assert package.identity.artifact_sha256 != changed.identity.artifact_sha256


@pytest.mark.parametrize("hook", ["validation", "factory", "factory_object"])
def test_hook_failures_are_classified(tmp_path: Path, hook: str) -> None:
    import yaml

    from ea.product import BacktestRunFailure, load_backtest_scenario, run_backtest_scenario
    from ea.strategy.package import pack_strategy

    path, root, _ = local_scenario(tmp_path)
    source = tmp_path / "source" / "strategy.py"
    code = source.read_text()
    if hook == "validation":
        code = code.replace(
            "def validate_parameters(parameters, context): pass",
            'def validate_parameters(parameters, context): raise RuntimeError("private error")',
        )
    elif hook == "factory":
        code = code.replace("return Logic(parameters)", 'raise RuntimeError("private error")')
    else:
        code = code.replace("return Logic(parameters)", "return object()")
    source.write_text(code)
    (root / "threshold.eastrategy").unlink()
    package = pack_strategy(source.parent, root / "threshold.eastrategy")
    document = yaml.safe_load(path.read_text())
    document["strategy"]["source"]["artifact_sha256"] = package.identity.artifact_sha256
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError if hook == "validation" else BacktestRunFailure) as caught:
        run_backtest_scenario(load_backtest_scenario(path, strategy_root=root), tmp_path / "runs")
    assert "private error" not in str(caught.value)


@pytest.mark.parametrize("frontier", ["root", "ancestor", "member"])
def test_root_replacement_cannot_redirect_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frontier: str
) -> None:
    import os

    from ea.strategy.package import StrategyPackageError, read_regular

    parent = tmp_path / "parent"
    root = parent / "authorized"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "x.eastrategy").write_bytes(b"authorized")
    (outside / "x.eastrategy").write_bytes(b"OUTSIDE")
    real_open = os.open
    swapped = False

    def swap(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        chosen = root if frontier == "root" else parent
        if not swapped and (frontier != "member" or str(file).endswith("x.eastrategy")):
            swapped = True
            if frontier == "member":
                (root / "x.eastrategy").unlink()
                (root / "x.eastrategy").symlink_to(outside / "x.eastrategy")
            else:
                chosen.rename(tmp_path / "original")
                chosen.symlink_to(outside, target_is_directory=True)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr("ea.strategy.package.os.open", swap)
    try:
        result = read_regular(root / "x.eastrategy", root)
    except StrategyPackageError:
        return
    assert result == b"authorized"


def test_catalog_root_race_never_executes_outside_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import os
    import zipfile

    from ea.strategy.catalog import ResearchStrategyCatalogV1
    from ea.strategy.package import _container

    _, root, package = local_scenario(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = tmp_path / "outside-executed"
    with zipfile.ZipFile(io.BytesIO(package.artifact_bytes)) as archive:
        manifest = archive.read("manifest.json")
    code = (
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode() + package.source_bytes
    )
    (outside / "threshold.eastrategy").write_bytes(_container(manifest, code))
    real_open = os.open
    swapped = False

    def swap(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(tmp_path / "original")
            root.symlink_to(outside, target_is_directory=True)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr("ea.strategy.package.os.open", swap)
    with pytest.raises(ValueError):
        ResearchStrategyCatalogV1.from_root(root)
    assert not marker.exists()


def test_required_entry_without_signal_fails_run(tmp_path: Path) -> None:
    import yaml

    from ea.product import BacktestRunFailure, load_backtest_scenario, run_backtest_scenario
    from ea.strategy.package import pack_strategy

    path, root, _ = local_scenario(tmp_path)
    source = tmp_path / "source"
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["strategy"]["outcome_mode"] = "required_single_long_entry"
    (source / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )
    (root / "threshold.eastrategy").unlink()
    package = pack_strategy(source, root / "threshold.eastrategy")
    doc = yaml.safe_load(path.read_text())
    doc["strategy"]["source"]["artifact_sha256"] = package.identity.artifact_sha256
    doc["strategy"]["parameters"]["threshold_price"] = "100000"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(load_backtest_scenario(path, strategy_root=root), tmp_path / "runs")
