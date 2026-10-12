"""Thin typed local plugin manifest and host compatibility verification.

A manifest is declarative, non-trusted data: it states what one capability claims
about itself. The host verifies that claim against its own adapter records and
the current host policy before anything may be selected, and it keeps the
package's claim and the verified/authorized capability as two separate views.

Nothing here imports a third-party module, installs a dependency, or grants an
effect. An entrypoint is an identifier resolved in the closed host adapter table,
never an import path or a module reference.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any

MANIFEST_SCHEMA_VERSION = 1

# Kinds this batch wraps. Everything else in the product direction is a declared
# extension boundary and is rejected explicitly rather than ignored.
SUPPORTED_KINDS: tuple[str, ...] = (
    "model",
    "data",
    "strategy",
    "validator",
    "backtest-method",
)
RESERVED_KINDS: tuple[str, ...] = (
    "agent",
    "tool",
    "evaluator",
    "market-mechanism",
    "trading-channel",
    "risk-policy",
    "ui",
    "workflow",
)

# The written extension boundary. A kind currently in RESERVED_KINDS becomes
# selectable only when the host — not a package — supplies all of:
#   1. a host interface name in HOST_INTERFACES at a known major,
#   2. a host adapter record in the closed adapter table, with the exact identity
#      fields REQUIRED_IDENTITY demands for that kind,
#   3. a host-granted effect set, with the adapter inside HOST_GRANTED_EFFECTS,
#   4. a real authority to wrap: this batch wraps existing implementations and
#      adds no second engine, registry, provider or dataset loader.
# Until then a manifest declaring that kind is refused with `manifest_kind`, and
# nothing in the package's own declaration can promote it into a capability.

KIND_INTERFACES: dict[str, str] = {
    "model": "ea.model-port",
    "data": "ea.dataset-catalog",
    "strategy": "ea.strategy-contract",
    "validator": "ea.scenario-validator",
    "backtest-method": "ea.backtest-method",
}

EFFECT_LOCAL_COMPUTE = "local_compute"
EFFECT_WRITE_LOCAL = "write_local"
EFFECT_READ_NETWORK = "read_network"
EFFECT_EXTERNAL_WRITE = "external_write"
EFFECT_FUNDS = "funds"
KNOWN_EFFECTS: tuple[str, ...] = (
    EFFECT_LOCAL_COMPUTE,
    EFFECT_WRITE_LOCAL,
    EFFECT_READ_NETWORK,
    EFFECT_EXTERNAL_WRITE,
    EFFECT_FUNDS,
)

# Host policy for the default offline research runtime profile. An external write
# or any financial effect is outside it, so a capability that requests one is
# never usable here, whatever its manifest claims.
HOST_GRANTED_EFFECTS: frozenset[str] = frozenset(
    {EFFECT_LOCAL_COMPUTE, EFFECT_WRITE_LOCAL, EFFECT_READ_NETWORK}
)

# The existing risk/order/ledger fact authority stays the default runtime profile.
RUNTIME_PROFILE = "ea.runtime-profile.default-offline-research.v1"

# Primitives the host itself supplies, so a capability may require them.
HOST_PROVIDES: frozenset[str] = frozenset(
    {
        "host.utc-clock",
        "host.research-budget",
        "host.job-evidence",
        "host.risk-authority",
        "host.writer-lease",
        "host.audit",
    }
)

_MAX_TOKENS = 16
_MAX_DEPENDENCIES = 16
_MAX_TEXT_BYTES = 200
_MAX_CONFIG_BYTES = 16 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManifestError(ValueError):
    """A manifest or capability cannot be verified, authorized, or bound."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def is_plugin_id(value: object) -> bool:
    """One namespaced capability identifier; never a module path or import string."""
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None and "." in value


def is_exact_version(value: object) -> bool:
    """One exact version string; ranges and floating tags are not versions here."""
    return type(value) is str and _VERSION.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class InterfaceRangeV1:
    name: str
    major: int
    minor: int

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "major": self.major, "minor": self.minor}


@dataclass(frozen=True, slots=True)
class HostInterfaceV1:
    name: str
    major: int
    supported_minor: int

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "major": self.major,
            "supported_minor": self.supported_minor,
        }


HOST_INTERFACES: tuple[HostInterfaceV1, ...] = (
    HostInterfaceV1("ea.model-port", 1, 0),
    HostInterfaceV1("ea.dataset-catalog", 1, 0),
    HostInterfaceV1("ea.strategy-contract", 1, 0),
    HostInterfaceV1("ea.scenario-validator", 1, 0),
    HostInterfaceV1("ea.backtest-method", 1, 0),
)
HOST_INTERFACES_BY_NAME: dict[str, HostInterfaceV1] = {item.name: item for item in HOST_INTERFACES}


@dataclass(frozen=True, slots=True)
class PluginDependencyV1:
    plugin_id: str
    package_version: str
    artifact_digest: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "package_version": self.package_version,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True, slots=True)
class PluginManifestV1:
    schema_version: int
    plugin_id: str
    kind: str
    package_version: str
    artifact_digest: str
    interface: InterfaceRangeV1
    entrypoint: str
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    dependencies: tuple[PluginDependencyV1, ...] = ()
    effects: tuple[str, ...] = ()
    runtime_profile: str = RUNTIME_PROFILE
    config_schema: dict[str, Any] = field(default_factory=dict)
    author: str = ""
    license: str = ""

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plugin_id": self.plugin_id,
            "kind": self.kind,
            "package_version": self.package_version,
            "artifact_digest": self.artifact_digest,
            "interface": self.interface.document(),
            "entrypoint": self.entrypoint,
            "provides": list(self.provides),
            "requires": list(self.requires),
            "dependencies": [item.document() for item in self.dependencies],
            "effects": list(self.effects),
            "runtime_profile": self.runtime_profile,
            "config_schema": self.config_schema,
            "author": self.author,
            "license": self.license,
        }


_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "plugin_id",
        "kind",
        "package_version",
        "artifact_digest",
        "interface",
        "entrypoint",
        "provides",
        "requires",
        "dependencies",
        "effects",
        "runtime_profile",
        "config_schema",
        "author",
        "license",
    }
)
_INTERFACE_KEYS = frozenset({"name", "major", "minor"})


def compute_artifact_digest(document: Mapping[str, Any]) -> str:
    """Integrity identity of one declared manifest body.

    This proves the declaration is intact; it proves nothing about what the code
    does, or which bytes of it run. The implementation artifact is named
    separately, by the host adapter identity a run binding freezes.
    """
    body = {key: value for key, value in document.items() if key != "artifact_digest"}
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return "sha256:" + sha256(payload.encode("ascii")).hexdigest()


def seal_manifest(manifest: PluginManifestV1) -> PluginManifestV1:
    """Fill in the digest of this manifest's own body."""
    return replace(manifest, artifact_digest=compute_artifact_digest(manifest.document()))


def verify_interface(manifest: PluginManifestV1) -> None:
    """Compatibility follows interface major and explicit minor, never semver."""
    host = HOST_INTERFACES_BY_NAME.get(manifest.interface.name)
    if host is None:
        raise ManifestError(
            "unknown_interface", f"interface {manifest.interface.name!r} is not a host interface"
        )
    if manifest.interface.major != host.major:
        raise ManifestError(
            "unknown_interface_major",
            f"interface {manifest.interface.name!r} major {manifest.interface.major} is not "
            f"supported; this host serves major {host.major}",
        )
    if manifest.interface.minor > host.supported_minor:
        raise ManifestError(
            "unsupported_interface_minor",
            f"interface {manifest.interface.name!r} minor {manifest.interface.minor} is newer "
            f"than the supported minor {host.supported_minor}",
        )
    expected = KIND_INTERFACES.get(manifest.kind)
    if expected is not None and manifest.interface.name != expected:
        raise ManifestError(
            "interface_kind_mismatch", f"kind {manifest.kind!r} must declare interface {expected!r}"
        )


def _text(value: object, *, field_name: str) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ManifestError("manifest_field", f"{field_name} must be bounded text")
    return value


def _tokens(value: object, *, field_name: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > _MAX_TOKENS:
        raise ManifestError(
            "manifest_field", f"{field_name} must list at most {_MAX_TOKENS} capability tokens"
        )
    out: list[str] = []
    for item in value:
        if type(item) is not str or _TOKEN.fullmatch(item) is None:
            raise ManifestError(
                "manifest_field", f"{field_name} entries must be lowercase capability tokens"
            )
        if item in out:
            raise ManifestError("manifest_field", f"{field_name} must not repeat a token")
        out.append(item)
    return tuple(out)


def _effects(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) > len(KNOWN_EFFECTS):
        raise ManifestError(
            "manifest_field", f"effects must be known names: {', '.join(KNOWN_EFFECTS)}"
        )
    out: list[str] = []
    for item in value:
        if type(item) is not str or item not in KNOWN_EFFECTS:
            raise ManifestError(
                "manifest_field", f"effect {item!r} is not one of: {', '.join(KNOWN_EFFECTS)}"
            )
        if item in out:
            raise ManifestError("manifest_field", "effects must not repeat")
        out.append(item)
    return tuple(out)


def _dependencies(value: object) -> tuple[PluginDependencyV1, ...]:
    if type(value) is not list or len(value) > _MAX_DEPENDENCIES:
        raise ManifestError(
            "manifest_field", f"dependencies must list at most {_MAX_DEPENDENCIES} entries"
        )
    out: list[PluginDependencyV1] = []
    for item in value:
        if type(item) is not dict or not {"plugin_id", "package_version"} <= set(item):
            raise ManifestError(
                "manifest_field", "each dependency must declare plugin_id and package_version"
            )
        if set(item) - {"plugin_id", "package_version", "artifact_digest"}:
            raise ManifestError("manifest_field", "a dependency declares an unsupported field")
        plugin_id, version, digest = (
            item["plugin_id"],
            item["package_version"],
            item.get("artifact_digest"),
        )
        if not is_plugin_id(plugin_id):
            raise ManifestError(
                "manifest_field", "a dependency plugin_id must be one namespaced identifier"
            )
        if type(version) is not str or _VERSION.fullmatch(version) is None:
            raise ManifestError("manifest_field", "a dependency package_version must be exact")
        if digest is not None and (type(digest) is not str or _DIGEST.fullmatch(digest) is None):
            raise ManifestError("manifest_field", "a dependency digest must be one sha256 value")
        out.append(PluginDependencyV1(plugin_id, version, digest))
    return tuple(out)


def _config_schema(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ManifestError("manifest_field", "config_schema must be one object")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError):
        raise ManifestError("manifest_field", "config_schema must be finite JSON") from None
    if len(encoded) > _MAX_CONFIG_BYTES:
        raise ManifestError("manifest_field", "config_schema exceeds the supported size")
    return dict(value)


def _interface(value: object) -> InterfaceRangeV1:
    if type(value) is not dict or set(value) != _INTERFACE_KEYS:
        raise ManifestError(
            "manifest_field", "interface must declare exactly name, major and minor"
        )
    name, major, minor = value["name"], value["major"], value["minor"]
    if type(name) is not str or _TOKEN.fullmatch(name) is None:
        raise ManifestError("manifest_field", "interface name must be one lowercase token")
    if type(major) is not int or type(minor) is not int or major < 0 or minor < 0:
        raise ManifestError("manifest_field", "interface major and minor must be integers")
    return InterfaceRangeV1(name, major, minor)


def decode_manifest(document: Any) -> PluginManifestV1:
    """Decode one declarative manifest; execute nothing it contains."""
    if type(document) is not dict:
        raise ManifestError("manifest_field", "a manifest must be one object")
    if set(document) != _MANIFEST_KEYS:
        unknown = sorted(str(key) for key in set(document) - _MANIFEST_KEYS)
        missing = sorted(_MANIFEST_KEYS - set(document))
        detail = "; ".join(
            part
            for part in (
                f"unsupported fields: {', '.join(unknown)}" if unknown else "",
                f"missing fields: {', '.join(missing)}" if missing else "",
            )
            if part
        )
        raise ManifestError("manifest_field", f"manifest shape is not supported: {detail}")
    schema_version = document["schema_version"]
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            "manifest_schema", f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}"
        )
    if not is_plugin_id(document["plugin_id"]):
        raise ManifestError("manifest_field", "plugin_id must be one namespaced identifier")
    kind = document["kind"]
    if type(kind) is not str:
        raise ManifestError("manifest_field", "kind is required and must be one string")
    if kind in RESERVED_KINDS:
        raise ManifestError(
            "manifest_kind",
            f"kind {kind!r} is a declared extension boundary and is not selectable in this profile",
        )
    if kind not in SUPPORTED_KINDS:
        raise ManifestError(
            "manifest_kind",
            f"kind {kind!r} is not supported; this profile supports: {', '.join(SUPPORTED_KINDS)}",
        )
    version = document["package_version"]
    if type(version) is not str or _VERSION.fullmatch(version) is None:
        raise ManifestError("manifest_field", "package_version must be one exact version")
    digest = document["artifact_digest"]
    if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
        raise ManifestError("manifest_field", "artifact_digest must be one sha256 value")
    if not is_plugin_id(document["entrypoint"]):
        raise ManifestError(
            "manifest_field",
            "entrypoint must name one host adapter identifier, not a module path or import",
        )
    runtime_profile = document["runtime_profile"]
    if runtime_profile != RUNTIME_PROFILE:
        raise ManifestError(
            "unsupported_runtime_profile",
            f"runtime profile {runtime_profile!r} is not the host profile {RUNTIME_PROFILE!r}",
        )
    manifest = PluginManifestV1(
        schema_version=MANIFEST_SCHEMA_VERSION,
        plugin_id=document["plugin_id"],
        kind=kind,
        package_version=version,
        artifact_digest=digest,
        interface=_interface(document["interface"]),
        entrypoint=document["entrypoint"],
        provides=_tokens(document["provides"], field_name="provides"),
        requires=_tokens(document["requires"], field_name="requires"),
        dependencies=_dependencies(document["dependencies"]),
        effects=_effects(document["effects"]),
        runtime_profile=runtime_profile,
        config_schema=_config_schema(document["config_schema"]),
        author=_text(document["author"], field_name="author"),
        license=_text(document["license"], field_name="license"),
    )
    if manifest.artifact_digest != compute_artifact_digest(manifest.document()):
        raise ManifestError(
            "digest_mismatch", "the declared artifact digest does not match the manifest body"
        )
    return manifest
