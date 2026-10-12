"""Thin typed local plugin capability layer.

This module wraps capability boundaries that already exist in this distribution.
It holds a declarative, non-trusted manifest model, host-owned adapter records, a
closed local catalog and the exact-version run binding. It never imports a
third-party module, installs a dependency, grants an effect, or re-implements an
existing authority.
"""

from ea.plugin.catalog import AdapterV1, PluginCatalogV1, VerifiedPluginV1
from ea.plugin.manifest import (
    HOST_GRANTED_EFFECTS,
    KIND_INTERFACES,
    MANIFEST_SCHEMA_VERSION,
    RESERVED_KINDS,
    RUNTIME_PROFILE,
    SUPPORTED_KINDS,
    InterfaceRangeV1,
    ManifestError,
    PluginDependencyV1,
    PluginManifestV1,
    decode_manifest,
    is_exact_version,
    is_plugin_id,
    seal_manifest,
)
from ea.plugin.selection import (
    LAYER_KINDS,
    LAYER_ORDER,
    BoundCapabilityV1,
    PluginBindingV1,
    PluginSelectionV1,
    SelectedComponentV1,
    resolve_selection,
    verify_binding,
)

__all__ = [
    "HOST_GRANTED_EFFECTS",
    "KIND_INTERFACES",
    "LAYER_KINDS",
    "LAYER_ORDER",
    "MANIFEST_SCHEMA_VERSION",
    "RESERVED_KINDS",
    "RUNTIME_PROFILE",
    "SUPPORTED_KINDS",
    "AdapterV1",
    "BoundCapabilityV1",
    "InterfaceRangeV1",
    "ManifestError",
    "PluginBindingV1",
    "PluginCatalogV1",
    "PluginDependencyV1",
    "PluginManifestV1",
    "PluginSelectionV1",
    "SelectedComponentV1",
    "VerifiedPluginV1",
    "decode_manifest",
    "is_exact_version",
    "is_plugin_id",
    "resolve_selection",
    "seal_manifest",
    "verify_binding",
]
