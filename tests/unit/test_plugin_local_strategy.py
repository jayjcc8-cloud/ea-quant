"""Explicit trusted local .eastrategy adapter: static adoption, frozen execution.

The sentinel fixture below is self-owned and harmless. Its Python writes one file
when it is executed, which is exactly what lets these tests show that inspection,
selection and activation never execute a package: the same bytes are executed
once, through the existing legacy ``validate_package``, as a positive control for
the sentinel mechanism.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest

from ea.plugin.catalog import PluginCatalogV1
from ea.plugin.local_strategy import (
    LOCAL_STRATEGY_ENTRYPOINT,
    LOCAL_STRATEGY_WARNING,
    LocalStrategyGrantV1,
    activate_local_strategy,
    inspect_local_strategy,
    local_strategy_plugin_id,
    select_local_strategy,
)
from ea.plugin.manifest import (
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    ManifestError,
    PluginDependencyV1,
    PluginManifestV1,
    seal_manifest,
)
from ea.plugin.selection import PluginSelectionV1, resolve_selection
from ea.strategy.package import _container, validate_package
from unit.test_local_strategy_package import local_scenario

# One existing host capability identity, used to show a local package cannot
# reach a capability this build already owns without the host verifying it.
_BUILTIN_DEPENDENCY = PluginDependencyV1("ea.builtin.strategy.price-rule-entry-v1", "1.0.0")


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _sentinel_package(
    tmp_path: Path,
    *,
    package_id: str = "example.adapter",
    strategy_id: str = "local-adapter-v1",
    schema_version: int = 1,
) -> tuple[bytes, Path]:
    """One canonical local package whose module writes a sentinel when executed."""
    marker = tmp_path / "executed.marker"
    strategy: dict[str, Any] = {
        "id": strategy_id,
        "version": 1,
        "display_name": "Adapter fixture",
        "parameters": [],
    }
    if schema_version == 1:
        strategy["outcome_mode"] = "no_entry"
    else:
        strategy["action_contract"] = "V2"
        strategy["position_lifecycle"] = (
            "single-long-round-trip-v1" if schema_version == 2 else "bounded-long-round-trips-v1"
        )
    document = {"schema_version": schema_version, "package_id": package_id, "strategy": strategy}
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "def validate_parameters(parameters, context): pass\n"
        "class Logic:\n"
        " def on_bar(self, bar, position=None): return None\n"
        "def create_logic(parameters): return Logic()\n"
    )
    return _container(_canonical(document), source.encode("utf-8")), marker


def _manifest(
    package_id: str, *, config_schema: dict[str, Any] | None = None, **overrides: Any
) -> PluginManifestV1:
    values: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin_id": local_strategy_plugin_id(package_id),
        "kind": "strategy",
        "package_version": "1.0.0",
        "artifact_digest": "",
        "interface": InterfaceRangeV1("ea.strategy-contract", 1, 0),
        "entrypoint": LOCAL_STRATEGY_ENTRYPOINT,
        "provides": ("strategy.research",),
        "requires": (),
        "dependencies": (),
        "effects": ("local_compute",),
        "runtime_profile": RUNTIME_PROFILE,
        "config_schema": config_schema if config_schema is not None else {},
    }
    values.update(overrides)
    return seal_manifest(PluginManifestV1(**values))


def _grant(
    manifest: PluginManifestV1, artifact_sha256: str, **overrides: Any
) -> LocalStrategyGrantV1:
    values: dict[str, Any] = {
        "trusted": True,
        "plugin_id": manifest.plugin_id,
        "package_version": manifest.package_version,
        "manifest_digest": manifest.artifact_digest,
        "implementation_digest": "sha256:" + artifact_sha256,
        "effects": ("local_compute",),
    }
    values.update(overrides)
    return LocalStrategyGrantV1(**values)


def test_static_records_warning_and_shared_selection(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path)
    manifest = _manifest("example.adapter")
    grant = _grant(manifest, sha256(payload).hexdigest())
    capability = inspect_local_strategy(manifest, payload, grant=grant)
    adapter, sealed = capability.pair()
    assert capability.warning == LOCAL_STRATEGY_WARNING
    assert capability.document()["warning"] == LOCAL_STRATEGY_WARNING
    assert adapter.entrypoint == LOCAL_STRATEGY_ENTRYPOINT
    assert adapter.effects == ("local_compute",)
    assert adapter.identity == {
        "strategy_id": "local-adapter-v1",
        "strategy_version": 1,
        "implementation_sha256": grant.implementation_digest,
        "package_id": "example.adapter",
        "package_schema_version": 1,
        "plugin_manifest_sha256": sealed.artifact_digest,
    }
    assert capability.source() == {
        "kind": "local-package",
        "package_id": "example.adapter",
        "artifact_sha256": sha256(payload).hexdigest(),
    }
    binding = select_local_strategy(capability, grant=grant)
    component = binding.component("strategy")
    assert component is not None
    assert component.identity["implementation_sha256"] == grant.implementation_digest
    # The static records also bind through a caller's own shared catalog.
    shared = PluginCatalogV1([sealed], [adapter])
    requested = {"plugin_id": sealed.plugin_id, "package_version": sealed.package_version}
    selection = PluginSelectionV1.decode({"strategy": requested})
    shared_component = resolve_selection(shared, selection).component("strategy")
    assert shared_component is not None
    assert shared_component.document() == component.document()
    assert activate_local_strategy(binding, payload, catalog=shared, grant=grant) is not None
    assert not marker.exists()


def test_missing_trust_or_permission_fails_closed_before_code(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path)
    manifest = _manifest("example.adapter")
    digest = sha256(payload).hexdigest()
    # Static inspection needs no trust decision, and executes nothing.
    capability = inspect_local_strategy(manifest, payload)
    assert capability.document()["static_only"] is True
    assert capability.document()["trust_established"] is False
    assert not marker.exists()
    untrusted = _grant(manifest, digest, trusted=False)
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, payload, grant=untrusted)
    assert caught.value.code == "trust_required"
    ungranted = _grant(manifest, digest, effects=())
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, payload, grant=ungranted)
    assert caught.value.code == "effect_not_granted"
    # The same capability is never selected or activated without a current grant.
    catalog = capability.catalog()
    with pytest.raises(ManifestError) as caught:
        select_local_strategy(capability)
    assert caught.value.code == "grant_required"
    with pytest.raises(ManifestError) as caught:
        select_local_strategy(capability, grant=untrusted)
    assert caught.value.code == "trust_required"
    with pytest.raises(ManifestError) as caught:
        select_local_strategy(capability, grant=ungranted)
    assert caught.value.code == "effect_not_granted"
    binding = select_local_strategy(capability, grant=_grant(manifest, digest))
    for refused in (None, untrusted, ungranted):
        with pytest.raises(ManifestError) as caught:
            activate_local_strategy(binding, payload, catalog=catalog, grant=refused)
        assert caught.value.code in {"grant_required", "trust_required", "effect_not_granted"}
    assert not marker.exists()
    # Positive control: these very bytes do execute when something executes them.
    validate_package(payload)
    assert marker.exists()


def test_withdrawn_effect_is_refused_before_code(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path)
    digest = sha256(payload).hexdigest()
    effects = ("local_compute", "read_network")
    manifest = _manifest("example.adapter", effects=effects)
    full = _grant(manifest, digest, effects=effects)
    capability = inspect_local_strategy(manifest, payload, grant=full)
    binding = select_local_strategy(capability, grant=full)
    # read_network withdrawn: the identical identity must not be revived by the
    # binding that was accepted while it was still granted.
    reduced = _grant(manifest, digest, effects=("local_compute",))
    with pytest.raises(ManifestError) as caught:
        select_local_strategy(capability, grant=reduced)
    assert caught.value.code == "effect_not_granted"
    with pytest.raises(ManifestError) as caught:
        activate_local_strategy(binding, payload, catalog=capability.catalog(), grant=reduced)
    assert caught.value.code == "effect_not_granted"
    assert not marker.exists()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"kind": "validator"}, "local_strategy_manifest"),
        ({"entrypoint": "ea.host.adapter.strategy.alpha"}, "local_strategy_manifest"),
        (
            {"interface": InterfaceRangeV1("ea.strategy-contract", 2, 0)},
            "unknown_interface_major",
        ),
        (
            {"interface": InterfaceRangeV1("ea.backtest-method", 1, 0)},
            "interface_kind_mismatch",
        ),
        ({"effects": ("external_write",)}, "effects_denied"),
        ({"provides": ("strategy.internal",)}, "provides_not_verified"),
        ({"requires": ("host.not-a-primitive",)}, "dependency_unresolved"),
        ({"dependencies": (_BUILTIN_DEPENDENCY,)}, "dependency_unavailable"),
        ({"plugin_id": "ea.local.strategy.somewhere-else"}, "manifest_package_mismatch"),
        (
            {"config_schema": {"strategy_id": "other-strategy-v1", "strategy_version": 1}},
            "config_schema_mismatch",
        ),
    ],
)
def test_incompatible_manifest_rejects_before_code(
    tmp_path: Path, overrides: dict[str, Any], code: str
) -> None:
    payload, marker = _sentinel_package(tmp_path)
    manifest = _manifest("example.adapter", **overrides)
    grant = _grant(manifest, sha256(payload).hexdigest())
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, payload, grant=grant)
    assert caught.value.code == code
    assert not marker.exists()


def test_manifest_package_version_must_be_exact_semver(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path)
    manifest = _manifest("example.adapter", package_version="1.0")
    grant = _grant(_manifest("example.adapter"), sha256(payload).hexdigest())
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, payload, grant=grant)
    assert caught.value.code == "manifest_field"
    assert not marker.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_id", "ea.local.strategy.other.package"),
        ("package_version", "2.0.0"),
        ("manifest_digest", "sha256:" + "a" * 64),
        ("implementation_digest", "sha256:" + "b" * 64),
    ],
)
def test_grant_identity_must_match_exactly(tmp_path: Path, field: str, value: str) -> None:
    payload, marker = _sentinel_package(tmp_path)
    manifest = _manifest("example.adapter")
    grant = _grant(manifest, sha256(payload).hexdigest(), **{field: value})
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, payload, grant=grant)
    assert caught.value.code == "grant_identity_mismatch"
    assert not marker.exists()


def test_builtin_shadow_is_rejected_statically(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path, strategy_id="bounded-long-v1")
    manifest = _manifest("example.adapter")
    grant = _grant(manifest, sha256(payload).hexdigest())
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, payload, grant=grant)
    assert caught.value.code == "builtin_shadow"
    assert not marker.exists()


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_container_schema_version_is_frozen(tmp_path: Path, schema_version: int) -> None:
    payload, marker = _sentinel_package(tmp_path, schema_version=schema_version)
    manifest = _manifest("example.adapter")
    grant = _grant(manifest, sha256(payload).hexdigest())
    capability = inspect_local_strategy(manifest, payload, grant=grant)
    adapter, _sealed = capability.pair()
    assert capability.package_schema_version == schema_version
    assert adapter.identity["package_schema_version"] == schema_version
    assert not marker.exists()


def test_non_container_bytes_are_refused_statically(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path)
    manifest = _manifest("example.adapter")
    truncated = payload[:-1]
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(
            manifest, truncated, grant=_grant(manifest, sha256(truncated).hexdigest())
        )
    assert caught.value.code == "package_invalid"
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(manifest, b"", grant=_grant(manifest, sha256(payload).hexdigest()))
    assert caught.value.code == "package_invalid"
    with pytest.raises(ManifestError) as caught:
        inspect_local_strategy(
            manifest,
            cast("bytes", "not bytes"),
            grant=_grant(manifest, sha256(payload).hexdigest()),
        )
    assert caught.value.code == "package_invalid"
    with pytest.raises(ManifestError) as caught:
        local_strategy_plugin_id("not a valid id!")
    assert caught.value.code == "package_invalid"
    assert not marker.exists()


def test_drift_revocation_and_other_bytes_reject_before_code(tmp_path: Path) -> None:
    payload, marker = _sentinel_package(tmp_path)
    digest = sha256(payload).hexdigest()
    manifest = _manifest("example.adapter")
    grant = _grant(manifest, digest)
    capability = inspect_local_strategy(manifest, payload, grant=grant)
    binding = select_local_strategy(capability, grant=grant)
    catalog = capability.catalog()
    # Revoked trust cannot be revived by the binding that was accepted earlier.
    revoked = _grant(manifest, digest, trusted=False)
    with pytest.raises(ManifestError) as caught:
        activate_local_strategy(binding, payload, catalog=catalog, grant=revoked)
    assert caught.value.code == "trust_required"
    # A changed frozen version is a grant mismatch, never a silent substitution.
    other_version = _grant(manifest, digest, package_version="2.0.0")
    with pytest.raises(ManifestError) as caught:
        activate_local_strategy(binding, payload, catalog=catalog, grant=other_version)
    assert caught.value.code == "grant_identity_mismatch"
    # A changed capability record is binding drift against the current catalog.
    changed_manifest = _manifest(
        "example.adapter",
        config_schema={"strategy_id": "local-adapter-v1", "strategy_version": 1},
    )
    changed = inspect_local_strategy(
        changed_manifest, payload, grant=_grant(changed_manifest, digest)
    )
    with pytest.raises(ManifestError) as caught:
        activate_local_strategy(binding, payload, catalog=changed.catalog(), grant=grant)
    assert caught.value.code == "binding_drift"
    # Different bytes are never adopted under an accepted identity.
    other, other_marker = _sentinel_package(tmp_path / "other", package_id="example.other")
    with pytest.raises(ManifestError) as caught:
        activate_local_strategy(binding, other, catalog=catalog, grant=grant)
    assert caught.value.code == "grant_identity_mismatch"
    assert not marker.exists()
    assert not other_marker.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "ea.plugin-local-strategy-grant.v0"),
        ("trusted", "yes"),
        ("plugin_id", "not-namespaced"),
        ("package_version", "1.0"),
        ("manifest_digest", "0" * 64),
        ("implementation_digest", "sha256:" + "z" * 64),
        ("effects", ["local_compute", "local_compute"]),
        ("effects", ["write_everywhere"]),
    ],
)
def test_grant_document_shape_is_refused(field: str, value: Any) -> None:
    document = LocalStrategyGrantV1(
        trusted=True,
        plugin_id="ea.local.strategy.example.adapter",
        package_version="1.0.0",
        manifest_digest="sha256:" + "0" * 64,
        implementation_digest="sha256:" + "1" * 64,
    ).document()
    document[field] = value
    with pytest.raises(ManifestError) as caught:
        LocalStrategyGrantV1.decode(document)
    assert caught.value.code in {"grant_invalid", "grant_schema"}
    partial = {name: item for name, item in document.items() if name != field}
    with pytest.raises(ManifestError) as caught:
        LocalStrategyGrantV1.decode(partial)
    assert caught.value.code == "grant_invalid"


def test_grant_document_round_trips() -> None:
    grant = LocalStrategyGrantV1(
        trusted=True,
        plugin_id="ea.local.strategy.example.adapter",
        package_version="1.0.0",
        manifest_digest="sha256:" + "0" * 64,
        implementation_digest="sha256:" + "1" * 64,
    )
    assert LocalStrategyGrantV1.decode(grant.document()) == grant
    assert grant.document()["effects"] == ["local_compute"]


def test_bound_bytes_run_the_existing_engine_and_formal_report(tmp_path: Path) -> None:
    from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario

    path, root, package = local_scenario(tmp_path)
    payload = package.artifact_bytes
    descriptor = json.loads(json.dumps(package.descriptor.document()))
    manifest = _manifest(package.identity.package_id, config_schema=descriptor)
    grant = _grant(manifest, package.identity.artifact_sha256)
    capability = inspect_local_strategy(manifest, payload, grant=grant)
    assert capability.source() == {
        "kind": "local-package",
        "package_id": package.identity.package_id,
        "artifact_sha256": package.identity.artifact_sha256,
    }
    binding = select_local_strategy(capability, grant=grant)
    catalog = activate_local_strategy(binding, payload, catalog=capability.catalog(), grant=grant)
    # The frozen bytes are the evidence; the external artifact is not needed again.
    (root / "threshold.eastrategy").unlink()
    scenario = load_backtest_scenario(path, catalog=catalog)
    result = run_backtest_scenario(scenario, tmp_path / "runs")
    assert (result.output_directory / "strategy.eastrategy").read_bytes() == payload
    report = generate_backtest_report(result.output_directory, tmp_path / "report")
    assert package.identity.artifact_sha256 in report.report.canonical_bytes.decode()
