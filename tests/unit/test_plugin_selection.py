"""Selection resolution and the frozen run binding."""

from __future__ import annotations

from typing import Any

import pytest

from ea.plugin.catalog import AdapterV1, PluginCatalogV1
from ea.plugin.manifest import (
    KIND_INTERFACES,
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    ManifestError,
    PluginManifestV1,
    decode_manifest,
    seal_manifest,
)
from ea.plugin.selection import (
    PluginBindingV1,
    PluginSelectionV1,
    resolve_selection,
    verify_binding,
)

# A host adapter record is this host's own fact; tests supply a fixed stand-in
# for the real implementation fingerprint the built-ins carry.
IMPLEMENTATION = "sha256:" + "0" * 64

MODEL_ADAPTER = AdapterV1(
    entrypoint="test.host.adapter.model.local",
    kind="model",
    provides=("model.tool-use",),
    effects=("read_network", "local_compute"),
    identity={
        "provider": "deepseek",
        "protocol": "deepseek-chat-completions",
        "implementation_sha256": IMPLEMENTATION,
    },
)
STRATEGY_ADAPTER = AdapterV1(
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


def _manifest(adapter: AdapterV1, **overrides: Any) -> PluginManifestV1:
    values: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin_id": f"test.capability.{adapter.kind}",
        "kind": adapter.kind,
        "package_version": "1.0.0",
        "artifact_digest": "",
        "interface": InterfaceRangeV1(KIND_INTERFACES[adapter.kind], 1, 0),
        "entrypoint": adapter.entrypoint,
        "provides": adapter.provides,
        "effects": adapter.effects,
        "runtime_profile": RUNTIME_PROFILE,
        "config_schema": {},
    }
    values.update(overrides)
    return seal_manifest(PluginManifestV1(**values))


def _catalog(**overrides: Any) -> tuple[PluginCatalogV1, dict[str, PluginManifestV1]]:
    manifests = {
        "model": _manifest(MODEL_ADAPTER),
        "strategy": _manifest(STRATEGY_ADAPTER),
    }
    manifests.update(overrides)
    return (
        PluginCatalogV1(list(manifests.values()), [MODEL_ADAPTER, STRATEGY_ADAPTER]),
        manifests,
    )


def _selection(*layers: str) -> PluginSelectionV1:
    return PluginSelectionV1.decode(
        {
            layer: {"plugin_id": f"test.capability.{layer}", "package_version": "1.0.0"}
            for layer in layers
        }
    )


def test_selection_decodes_exactly_one_version_per_layer() -> None:
    selection = _selection("model", "strategy")
    assert selection.document()["layers"] == {
        "model": {"plugin_id": "test.capability.model", "package_version": "1.0.0"},
        "strategy": {"plugin_id": "test.capability.strategy", "package_version": "1.0.0"},
    }
    assert not PluginSelectionV1.decode({})
    assert not PluginSelectionV1.decode(None)


@pytest.mark.parametrize(
    "payload",
    [
        {"orders": {"plugin_id": "test.capability.model", "package_version": "1.0.0"}},
        {"model": {"plugin_id": "test.capability.model"}},
        {"model": {"plugin_id": "test.capability.model", "package_version": "1.x"}},
        {"model": {"plugin_id": "no-namespace", "package_version": "1.0.0"}},
        {"model": "test.capability.model"},
    ],
)
def test_selection_shape_is_rejected(payload: Any) -> None:
    with pytest.raises(ManifestError) as caught:
        PluginSelectionV1.decode(payload)
    assert caught.value.code == "selection_invalid"


def test_resolve_binds_exact_versions() -> None:
    catalog, _manifests = _catalog()
    binding = resolve_selection(catalog, _selection("strategy"))
    component = binding.component("strategy")
    assert component is not None
    assert component.package_version == "1.0.0"
    assert component.identity == {
        "strategy_id": "alpha",
        "strategy_version": 1,
        "implementation_sha256": IMPLEMENTATION,
    }
    assert component.effects == ("local_compute",)
    assert len(binding.binding_sha256) == 64


def test_binding_is_deterministic_and_re_readable() -> None:
    catalog, _manifests = _catalog()
    binding = resolve_selection(catalog, _selection("model", "strategy"))
    again = resolve_selection(catalog, _selection("model", "strategy"))
    assert binding.document() == again.document()
    assert PluginBindingV1.decode(binding.document()).document() == binding.document()
    verify_binding(catalog, binding)


def test_tampered_binding_is_refused() -> None:
    catalog, _manifests = _catalog()
    document = resolve_selection(catalog, _selection("strategy")).document()
    document["components"][0]["identity"]["strategy_version"] = 9
    with pytest.raises(ManifestError) as caught:
        PluginBindingV1.decode(document)
    assert caught.value.code == "binding_invalid"


def test_layer_kind_mismatch_is_rejected() -> None:
    catalog, _manifests = _catalog()
    selection = PluginSelectionV1.decode(
        {"strategy": {"plugin_id": "test.capability.model", "package_version": "1.0.0"}}
    )
    with pytest.raises(ManifestError) as caught:
        resolve_selection(catalog, selection)
    assert caught.value.code == "selection_kind_mismatch"


def test_authorization_can_only_refuse() -> None:
    catalog, _manifests = _catalog()

    def refuse(plugin: Any) -> None:
        raise ManifestError("model_route_not_configured", "no such route here")

    with pytest.raises(ManifestError) as caught:
        resolve_selection(catalog, _selection("model"), authorize=refuse)
    assert caught.value.code == "model_route_not_configured"
    with pytest.raises(ManifestError) as caught:
        verify_binding(catalog, resolve_selection(catalog, _selection("model")), authorize=refuse)
    assert caught.value.code == "model_route_not_configured"


def test_verify_binding_detects_drift_and_missing_versions() -> None:
    catalog, _manifests = _catalog()
    binding = resolve_selection(catalog, _selection("strategy"))
    changed, _manifests = _catalog(
        strategy=_manifest(STRATEGY_ADAPTER, config_schema={"strategy_id": "alpha", "rev": 2})
    )
    with pytest.raises(ManifestError) as caught:
        verify_binding(changed, binding)
    assert caught.value.code == "binding_drift"
    empty = PluginCatalogV1([], [STRATEGY_ADAPTER, MODEL_ADAPTER])
    with pytest.raises(ManifestError) as caught:
        verify_binding(empty, binding)
    assert caught.value.code == "plugin_unavailable"


def test_empty_selection_binds_nothing() -> None:
    catalog, _manifests = _catalog()
    binding = resolve_selection(catalog, PluginSelectionV1.decode(None))
    assert binding.empty and binding.document()["components"] == []


def test_decoded_manifest_is_still_selectable_by_exact_identity() -> None:
    manifest = decode_manifest(_manifest(STRATEGY_ADAPTER).document())
    catalog = PluginCatalogV1([manifest], [STRATEGY_ADAPTER])
    binding = resolve_selection(catalog, _selection("strategy"))
    component = binding.component("strategy")
    assert component is not None
    assert component.artifact_digest == manifest.artifact_digest
