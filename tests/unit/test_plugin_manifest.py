"""Capability manifests: declared claims are never promoted into authorization."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from ea.plugin.builtins import BACKTEST_METHOD_ID, builtin_catalog
from ea.plugin.catalog import AdapterV1, PluginCatalogV1
from ea.plugin.manifest import (
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    ManifestError,
    PluginDependencyV1,
    PluginManifestV1,
    decode_manifest,
    seal_manifest,
)

# A host adapter record is this host's own fact; tests supply a fixed stand-in
# for the real implementation fingerprint the built-ins carry.
IMPLEMENTATION = "sha256:" + "0" * 64

ADAPTER = AdapterV1(
    entrypoint="test.host.adapter.strategy.alpha",
    kind="strategy",
    provides=("strategy.research",),
    effects=("local_compute",),
    identity={
        "strategy_id": "alpha",
        "strategy_version": 1,
        "implementation_sha256": IMPLEMENTATION,
    },
)
# A second host adapter, so one capability can provide a token another needs.
SECOND_ADAPTER = AdapterV1(
    entrypoint="test.host.adapter.strategy.beta",
    kind="strategy",
    provides=("strategy.research", "strategy.experimental"),
    effects=("local_compute",),
    identity={
        "strategy_id": "beta",
        "strategy_version": 1,
        "implementation_sha256": IMPLEMENTATION,
    },
)


def _manifest(**overrides: Any) -> PluginManifestV1:
    values: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin_id": "test.capability.alpha",
        "kind": "strategy",
        "package_version": "1.0.0",
        "artifact_digest": "",
        "interface": InterfaceRangeV1("ea.strategy-contract", 1, 0),
        "entrypoint": ADAPTER.entrypoint,
        "provides": ("strategy.research",),
        "requires": (),
        "dependencies": (),
        "effects": ("local_compute",),
        "runtime_profile": RUNTIME_PROFILE,
        "config_schema": {"strategy_id": "alpha"},
        "author": "",
        "license": "",
    }
    values.update(overrides)
    return seal_manifest(PluginManifestV1(**values))


def _rejected(
    manifests: list[PluginManifestV1], adapters: list[AdapterV1] | None = None
) -> ManifestError:
    catalog = PluginCatalogV1(manifests, [ADAPTER] if adapters is None else adapters)
    manifest = manifests[0]
    with pytest.raises(ManifestError) as caught:
        catalog.get(manifest.plugin_id, manifest.package_version)
    return caught.value


def test_manifest_round_trip() -> None:
    manifest = _manifest()
    decoded = decode_manifest(manifest.document())
    assert decoded == manifest
    assert decoded.document()["artifact_digest"].startswith("sha256:")


def test_digest_mismatch_is_rejected() -> None:
    document = _manifest().document()
    document["package_version"] = "2.0.0"
    with pytest.raises(ManifestError) as caught:
        decode_manifest(document)
    assert caught.value.code == "digest_mismatch"


def test_unknown_interface_major_is_rejected_without_executing_anything() -> None:
    manifest = _manifest(interface=InterfaceRangeV1("ea.strategy-contract", 2, 0))
    error = _rejected([manifest])
    assert error.code == "unknown_interface_major"


def test_newer_interface_minor_is_rejected() -> None:
    manifest = _manifest(interface=InterfaceRangeV1("ea.strategy-contract", 1, 7))
    assert _rejected([manifest]).code == "unsupported_interface_minor"


def test_reserved_and_unsupported_kinds_are_rejected() -> None:
    for kind, code in (("agent", "manifest_kind"), ("gateway", "manifest_kind")):
        document = _manifest(kind=kind).document()
        with pytest.raises(ManifestError) as caught:
            decode_manifest(document)
        assert caught.value.code == code


def test_missing_kind_is_rejected() -> None:
    document = _manifest().document()
    del document["kind"]
    with pytest.raises(ManifestError) as caught:
        decode_manifest(document)
    assert caught.value.code == "manifest_field"


@pytest.mark.parametrize("entrypoint", ["pkg.mod:attr", "../evil", "os.system()"])
def test_entrypoint_must_be_one_identifier_not_a_module_reference(entrypoint: str) -> None:
    document = _manifest(entrypoint=entrypoint).document()
    with pytest.raises(ManifestError) as caught:
        decode_manifest(document)
    assert caught.value.code == "manifest_field"


def test_unknown_entrypoint_is_never_imported() -> None:
    manifest = _manifest(entrypoint="os.system")
    error = _rejected([manifest])
    assert error.code == "entrypoint_unknown"


def test_malicious_manifest_cannot_gain_effects() -> None:
    # Claiming an effect the host adapter does not exercise.
    assert _rejected([_manifest(effects=("write_local",))]).code == "effects_not_verified"
    # Claiming an effect the host policy does not grant at all.
    assert _rejected([_manifest(effects=("funds",))]).code == "effects_denied"
    # A host adapter that itself declares a denied effect is refused up front.
    with pytest.raises(ManifestError) as caught:
        PluginCatalogV1([], [AdapterV1("test.host.adapter.bad", "strategy", effects=("funds",))])
    assert caught.value.code == "adapter_effect_denied"


def test_illegal_dependency_is_rejected() -> None:
    unresolved = _manifest(requires=("strategy.teleport",))
    assert _rejected([unresolved]).code == "dependency_unresolved"
    unavailable = _manifest(dependencies=(PluginDependencyV1("test.capability.absent", "1.0.0"),))
    assert _rejected([unavailable]).code == "dependency_unavailable"


def test_a_rejected_provider_cannot_carry_a_dependent_into_the_catalog() -> None:
    # The provider declares an interface major this host does not serve.
    provider = _manifest(
        plugin_id="test.capability.bad",
        interface=InterfaceRangeV1("ea.strategy-contract", 99, 0),
    )
    dependent = _manifest(
        plugin_id="test.capability.dependent",
        dependencies=(PluginDependencyV1("test.capability.bad", "1.0.0"),),
    )
    catalog = PluginCatalogV1([provider, dependent], [ADAPTER])
    entries = {entry["plugin_id"]: entry for entry in catalog.entries()}
    assert entries["test.capability.bad"]["reason"]["code"] == "unknown_interface_major"
    assert entries["test.capability.dependent"]["usable"] is False
    assert entries["test.capability.dependent"]["reason"]["code"] == "dependency_unavailable"
    with pytest.raises(ManifestError) as caught:
        catalog.get("test.capability.dependent", "1.0.0")
    assert caught.value.code == "dependency_unavailable"


def test_only_a_verified_provider_satisfies_a_requirement() -> None:
    provider = _manifest(
        plugin_id="test.capability.broken",
        entrypoint=SECOND_ADAPTER.entrypoint,
        provides=("strategy.research", "strategy.experimental"),
        requires=("strategy.teleport",),
    )
    dependent = _manifest(
        plugin_id="test.capability.dependent",
        requires=("strategy.experimental",),
    )
    catalog = PluginCatalogV1([provider, dependent], [ADAPTER, SECOND_ADAPTER])
    entries = {entry["plugin_id"]: entry for entry in catalog.entries()}
    assert entries["test.capability.broken"]["reason"]["code"] == "dependency_unresolved"
    assert entries["test.capability.dependent"]["usable"] is False
    assert entries["test.capability.dependent"]["reason"]["code"] == "dependency_unresolved"


def test_a_dependency_cycle_is_refused() -> None:
    left = _manifest(
        plugin_id="test.capability.left",
        dependencies=(PluginDependencyV1("test.capability.right", "1.0.0"),),
    )
    right = _manifest(
        plugin_id="test.capability.right",
        dependencies=(PluginDependencyV1("test.capability.left", "1.0.0"),),
    )
    catalog = PluginCatalogV1([left, right], [ADAPTER])
    assert {entry["usable"] for entry in catalog.entries()} == {False}
    assert {entry["reason"]["code"] for entry in catalog.entries()} == {"dependency_cycle"}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"artifact_digest": ""}, "manifest_field"),
        ({"schema_version": 2}, "manifest_schema"),
        ({"package_version": "2.0.0"}, "digest_mismatch"),
    ],
)
def test_a_dataclass_built_in_process_cannot_skip_manifest_validation(
    overrides: dict[str, Any], code: str
) -> None:
    manifest = replace(_manifest(), **overrides)
    catalog = PluginCatalogV1([manifest], [ADAPTER])
    entry = catalog.entries()[0]
    assert entry["usable"] is False
    assert entry["reason"]["code"] == code
    with pytest.raises(ManifestError) as caught:
        catalog.get(manifest.plugin_id, manifest.package_version)
    assert caught.value.code == code


def test_capability_cannot_claim_what_the_host_adapter_does_not_provide() -> None:
    manifest = _manifest(provides=("strategy.research", "strategy.live-orders"))
    assert _rejected([manifest]).code == "provides_not_verified"


def test_rejected_entry_stays_visible_and_unusable() -> None:
    manifest = _manifest(effects=("funds",))
    catalog = PluginCatalogV1([manifest], [ADAPTER])
    entries = catalog.entries()
    assert len(entries) == 1
    assert entries[0]["usable"] is False
    assert entries[0]["reason"]["code"] == "effects_denied"
    assert entries[0]["effects"]["granted"] == []
    assert catalog.document()["schema"] == "ea.plugin-catalog.v1"


def test_exact_version_is_required_and_no_latest_is_substituted() -> None:
    manifest = _manifest()
    catalog = PluginCatalogV1([manifest], [ADAPTER])
    assert catalog.get(manifest.plugin_id, "1.0.0").manifest is manifest
    with pytest.raises(ManifestError) as caught:
        catalog.get(manifest.plugin_id, "1.0.1")
    assert caught.value.code == "plugin_unavailable"
    assert "no other version is substituted" in caught.value.message


def test_an_adapter_that_cannot_name_its_implementation_is_unusable() -> None:
    adapter = AdapterV1(
        entrypoint="test.host.adapter.strategy.gamma",
        kind="strategy",
        provides=("strategy.research",),
        effects=("local_compute",),
        identity={"strategy_id": "gamma", "strategy_version": 1},
    )
    manifest = _manifest(plugin_id="test.capability.gamma", entrypoint=adapter.entrypoint)
    catalog = PluginCatalogV1([manifest], [adapter])
    entry = catalog.entries()[0]
    # No fingerprint is invented or substituted for the missing one.
    assert entry["usable"] is False
    assert entry["reason"]["code"] == "adapter_identity_incomplete"
    with pytest.raises(ManifestError) as caught:
        catalog.get("test.capability.gamma", "1.0.0")
    assert caught.value.code == "adapter_identity_incomplete"


def test_builtin_catalog_wraps_existing_implementations() -> None:
    from ea.plugin.builtins import implementation_identity
    from ea.product.reporting import _SUPPORTED_RESULT_SCHEMA

    catalog = builtin_catalog(SimpleNamespace(datasets=None))
    kinds = {entry["kind"] for entry in catalog.entries()}
    assert kinds == {"model", "strategy", "validator", "backtest-method"}
    assert all(entry["usable"] for entry in catalog.entries())
    method = catalog.get("ea.builtin.backtest.event-replay", "1.0.0")
    fingerprint = implementation_identity("backtest-method")
    assert method.identity() == {
        "method_id": BACKTEST_METHOD_ID,
        "method_version": _SUPPORTED_RESULT_SCHEMA,
        "implementation_sha256": fingerprint,
    }
    # The implementation's own identity, not the digest of the manifest that
    # merely describes it.
    assert fingerprint is not None and fingerprint.startswith("sha256:")
    assert fingerprint != method.manifest.artifact_digest
    strategy = catalog.get("ea.builtin.strategy.bounded-long-v1", "1.0.0")
    assert strategy.identity()["strategy_version"] == 1
    assert strategy.manifest.interface.name == "ea.strategy-contract"
