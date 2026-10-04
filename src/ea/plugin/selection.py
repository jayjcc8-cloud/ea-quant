"""Explicit user selection, exact-version resolution and the frozen run binding.

The user selects a capability per layer; the host verifies it and freezes the
result. A binding records the exact identity of everything a run used, so a
later catalog, configuration or plugin change cannot silently alter an accepted
run, and a missing exact version is refused instead of replaced by a newer one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ea.plugin.catalog import PluginCatalogV1, VerifiedPluginV1
from ea.plugin.manifest import ManifestError, is_exact_version, is_plugin_id

SELECTION_SCHEMA = "ea.plugin-selection.v1"
BINDING_SCHEMA = "ea.plugin-binding.v1"

# Selectable layers this batch binds; every other kind stays a written boundary.
LAYER_KINDS: dict[str, str] = {
    "model": "model",
    "data": "data",
    "strategy": "strategy",
    "validator": "validator",
    "backtest": "backtest-method",
}
LAYER_ORDER: tuple[str, ...] = tuple(LAYER_KINDS)

# A plugin verifier supplied by the caller: it may only refuse, never grant.
Authorize = Callable[[VerifiedPluginV1], None]


@dataclass(frozen=True, slots=True)
class SelectedComponentV1:
    layer: str
    plugin_id: str
    package_version: str

    def document(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "plugin_id": self.plugin_id,
            "package_version": self.package_version,
        }


@dataclass(frozen=True, slots=True)
class PluginSelectionV1:
    """One explicit selection; an empty selection keeps the host default route."""

    components: tuple[SelectedComponentV1, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.components)

    @staticmethod
    def decode(payload: Any) -> PluginSelectionV1:
        if payload is None:
            return PluginSelectionV1()
        if type(payload) is not dict:
            raise ManifestError(
                "selection_invalid",
                f"selection must be one object keyed by layer: {', '.join(LAYER_ORDER)}",
            )
        unknown = sorted(set(payload) - set(LAYER_KINDS))
        if unknown:
            raise ManifestError(
                "selection_invalid",
                f"layer selection is not supported: {', '.join(unknown)}; "
                f"selectable layers are: {', '.join(LAYER_ORDER)}",
            )
        components: list[SelectedComponentV1] = []
        for layer in LAYER_ORDER:
            if layer not in payload:
                continue
            entry = payload[layer]
            if type(entry) is not dict or set(entry) != {"plugin_id", "package_version"}:
                raise ManifestError(
                    "selection_invalid",
                    f"layer {layer!r} must name exactly plugin_id and package_version",
                )
            plugin_id = entry["plugin_id"]
            version = entry["package_version"]
            if not is_plugin_id(plugin_id):
                raise ManifestError(
                    "selection_invalid",
                    f"layer {layer!r} must name one namespaced capability identifier",
                )
            if not is_exact_version(version):
                raise ManifestError(
                    "selection_invalid",
                    f"layer {layer!r} must name one exact package_version; "
                    "ranges and tags are not selectable",
                )
            components.append(SelectedComponentV1(layer, plugin_id, version))
        return PluginSelectionV1(tuple(components))

    def document(self) -> dict[str, Any]:
        return {
            "schema": SELECTION_SCHEMA,
            "components": [item.document() for item in self.components],
            "layers": {
                item.layer: {
                    "plugin_id": item.plugin_id,
                    "package_version": item.package_version,
                }
                for item in self.components
            },
        }


_BOUND_KEYS = frozenset(
    {
        "layer",
        "plugin_id",
        "kind",
        "package_version",
        "artifact_digest",
        "interface",
        "entrypoint",
        "effects",
        "identity",
    }
)


@dataclass(frozen=True, slots=True)
class BoundCapabilityV1:
    """One resolved capability: exact version plus the host facts that fix it."""

    layer: str
    plugin_id: str
    kind: str
    package_version: str
    artifact_digest: str
    interface: dict[str, Any]
    entrypoint: str
    effects: tuple[str, ...]
    identity: dict[str, Any]

    def document(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "plugin_id": self.plugin_id,
            "kind": self.kind,
            "package_version": self.package_version,
            "artifact_digest": self.artifact_digest,
            # Copies: a frozen binding is read many times, and no reader may be
            # able to mutate the record it is verifying.
            "interface": dict(self.interface),
            "entrypoint": self.entrypoint,
            "effects": list(self.effects),
            "identity": dict(self.identity),
        }

    @staticmethod
    def decode(document: Any) -> BoundCapabilityV1:
        if type(document) is not dict or set(document) != _BOUND_KEYS:
            raise ManifestError("binding_invalid", "a bound capability has an unsupported shape")
        layer = document["layer"]
        if type(layer) is not str or layer not in LAYER_KINDS:
            raise ManifestError("binding_invalid", "a bound capability names an unknown layer")
        if document["kind"] != LAYER_KINDS[layer]:
            raise ManifestError("binding_invalid", f"layer {layer!r} and kind do not match")
        if type(document["interface"]) is not dict or type(document["identity"]) is not dict:
            raise ManifestError("binding_invalid", "a bound capability needs object facts")
        effects = document["effects"]
        if type(effects) is not list or any(type(item) is not str for item in effects):
            raise ManifestError("binding_invalid", "a bound capability needs text effects")
        return BoundCapabilityV1(
            layer=layer,
            plugin_id=str(document["plugin_id"]),
            kind=str(document["kind"]),
            package_version=str(document["package_version"]),
            artifact_digest=str(document["artifact_digest"]),
            interface=dict(document["interface"]),
            entrypoint=str(document["entrypoint"]),
            effects=tuple(effects),
            identity=dict(document["identity"]),
        )


def _digest(components: tuple[BoundCapabilityV1, ...]) -> str:
    payload = json.dumps(
        [item.document() for item in components],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(payload.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class PluginBindingV1:
    """The frozen capability identity of one accepted run."""

    components: tuple[BoundCapabilityV1, ...] = ()
    binding_sha256: str = ""

    @property
    def empty(self) -> bool:
        return not self.components

    def component(self, layer: str) -> BoundCapabilityV1 | None:
        for item in self.components:
            if item.layer == layer:
                return item
        return None

    def identity(self, layer: str) -> dict[str, Any]:
        item = self.component(layer)
        return {} if item is None else dict(item.identity)

    def document(self) -> dict[str, Any]:
        return {
            "schema": BINDING_SCHEMA,
            "binding_sha256": self.binding_sha256,
            "components": [item.document() for item in self.components],
        }

    @staticmethod
    def decode(document: Any) -> PluginBindingV1:
        """Read one frozen binding back; a tampered record is refused, not repaired."""
        if (
            type(document) is not dict
            or set(document) != {"schema", "binding_sha256", "components"}
            or document.get("schema") != BINDING_SCHEMA
            or type(document.get("components")) is not list
            or type(document.get("binding_sha256")) is not str
        ):
            raise ManifestError("binding_invalid", "a frozen binding has an unsupported shape")
        components = tuple(BoundCapabilityV1.decode(item) for item in document["components"])
        if len({item.layer for item in components}) != len(components):
            raise ManifestError("binding_invalid", "a frozen binding repeats a layer")
        binding = PluginBindingV1(components, document["binding_sha256"])
        if _digest(components) != binding.binding_sha256:
            raise ManifestError("binding_invalid", "a frozen binding is not self-consistent")
        return binding


def _bound(layer: str, plugin: VerifiedPluginV1) -> BoundCapabilityV1:
    manifest = plugin.manifest
    return BoundCapabilityV1(
        layer=layer,
        plugin_id=manifest.plugin_id,
        kind=manifest.kind,
        package_version=manifest.package_version,
        artifact_digest=manifest.artifact_digest,
        interface=manifest.interface.document(),
        entrypoint=manifest.entrypoint,
        effects=plugin.granted_effects,
        identity=plugin.identity(),
    )


def resolve_selection(
    catalog: PluginCatalogV1,
    selection: PluginSelectionV1,
    *,
    authorize: Authorize | None = None,
) -> PluginBindingV1:
    """Resolve one explicit selection to exact versions, or refuse before dispatch."""
    if not isinstance(selection, PluginSelectionV1):
        raise ManifestError("selection_invalid", "selection must be one decoded selection")
    components: list[BoundCapabilityV1] = []
    for item in selection.components:
        kind = LAYER_KINDS[item.layer]
        plugin = catalog.get(item.plugin_id, item.package_version)
        if plugin.manifest.kind != kind:
            raise ManifestError(
                "selection_kind_mismatch",
                f"layer {item.layer!r} selects a {kind} capability; "
                f"{item.plugin_id!r} is a {plugin.manifest.kind} capability",
            )
        if authorize is not None:
            authorize(plugin)
        components.append(_bound(item.layer, plugin))
    ordered = tuple(sorted(components, key=lambda item: LAYER_ORDER.index(item.layer)))
    return PluginBindingV1(ordered, _digest(ordered))


def verify_binding(
    catalog: PluginCatalogV1,
    binding: PluginBindingV1,
    *,
    authorize: Authorize | None = None,
) -> None:
    """Re-verify a frozen binding; never substitute another version for it."""
    if not isinstance(binding, PluginBindingV1):
        raise ManifestError("binding_invalid", "the frozen binding is not self-consistent")
    if _digest(binding.components) != binding.binding_sha256:
        raise ManifestError("binding_invalid", "the frozen binding is not self-consistent")
    for item in binding.components:
        plugin = catalog.get(item.plugin_id, item.package_version)
        current = _bound(item.layer, plugin)
        if current.document() != item.document():
            raise ManifestError(
                "binding_drift",
                f"capability {item.plugin_id!r} version {item.package_version!r} no longer "
                "matches the binding it was accepted with",
            )
        if authorize is not None:
            authorize(plugin)
