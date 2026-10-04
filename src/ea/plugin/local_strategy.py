"""Explicit trusted local ``.eastrategy`` adapter for the shared plugin catalog.

One captured local strategy package becomes one ordinary plugin capability: a host
``AdapterV1`` record plus the declarative ``PluginManifestV1`` the caller supplies,
which the existing ``PluginCatalogV1``, ``PluginSelectionV1`` and frozen
``PluginBindingV1`` machinery then treats like any other capability. Nothing here
discovers, downloads, installs, imports or fetches a dependency.

Static first, trust second. ``inspect_local_strategy`` validates the manifest
identity, version, interface major/minor, effects, provides, requires and
dependencies through the existing catalog, compares a declared config schema with
the package descriptor, refuses a built-in shadow through the existing research
catalog, and derives the host adapter record from ``inspect_package`` of the
captured bytes — with no grant at all and with nothing executed: the package's
Python is never compiled, imported or run while the host is deciding what it is. A
caller can therefore inspect a package it has not trusted yet.

Trust is the caller's own human decision, never a package claim. The grant names
one exact plugin id, ``package_version``, manifest digest and package
implementation digest, plus the effects granted. Selection and activation always
require that exact grant, and every effect the capability requests must be covered
by it at that moment, so an effect the operator withdrew is refused even though an
earlier binding was accepted. A wrong identity, a changed version, changed bytes,
a missing or revoked grant, a withdrawn permission, built-in shadowing or a
drifted frozen binding all fail closed before any code could run. Activation
re-checks the current grant as its last step before handing the engine its catalog,
and it is the caller's duty to activate again at each new dispatch: the returned
``ResearchStrategyCatalogV1`` is the ordinary one, and it does not intercept later
Python calls.

This is not a sandbox, and nothing here is operating-system enforcement. Once the
engine dispatches, the package runs as ordinary local Python with this process's
own privileges, which is what ``LOCAL_STRATEGY_WARNING`` tells the caller. Adopt
only bytes whose digest you have verified yourself.

Smallest integration example::

    from ea.plugin.local_strategy import (
        LocalStrategyGrantV1,
        activate_local_strategy,
        inspect_local_strategy,
        select_local_strategy,
    )

    # Static: executes nothing, needs no trust, establishes none.
    capability = inspect_local_strategy(manifest, payload)
    grant = LocalStrategyGrantV1(
        trusted=True,                          # the caller's own decision
        plugin_id=capability.plugin_id,        # ea.local.strategy.<package_id>
        package_version=capability.package_version,
        manifest_digest=capability.manifest.artifact_digest,
        implementation_digest="sha256:" + captured_sha256,
    )
    binding = select_local_strategy(capability, grant=grant)
    catalog = activate_local_strategy(
        binding, payload, catalog=capability.catalog(), grant=grant
    )
    scenario = load_backtest_scenario(path, catalog=catalog)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast

from ea.plugin.catalog import AdapterV1, PluginCatalogV1, VerifiedPluginV1
from ea.plugin.manifest import (
    EFFECT_LOCAL_COMPUTE,
    HOST_GRANTED_EFFECTS,
    KNOWN_EFFECTS,
    ManifestError,
    PluginManifestV1,
    decode_manifest,
    is_exact_version,
    is_plugin_id,
)
from ea.plugin.selection import (
    Authorize,
    PluginBindingV1,
    PluginSelectionV1,
    resolve_selection,
    verify_binding,
)
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import (
    MAX_ARTIFACT_BYTES,
    StrategyPackage,
    StrategyPackageError,
    StrategyPackageV2,
    StrategyPackageV3,
    inspect_package,
)

LOCAL_STRATEGY_SCHEMA = "ea.plugin-local-strategy.v1"
LOCAL_STRATEGY_GRANT_SCHEMA = "ea.plugin-local-strategy-grant.v1"

# The one host adapter entrypoint a local strategy capability may name. It is this
# host's own identifier for "execute the captured bytes of a trusted local
# package", never an import path or a module reference.
LOCAL_STRATEGY_ENTRYPOINT = "ea.host.adapter.local-strategy"

# The host-owned namespace a local package id maps into. A package never names its
# own capability identifier, so it cannot claim a built-in or another package.
LOCAL_STRATEGY_PLUGIN_PREFIX = "ea.local.strategy."

LOCAL_STRATEGY_WARNING = (
    "trusted local strategy Python runs with this process's own privileges and is not "
    "sandboxed; adopt only bytes whose sha256 you have verified yourself"
)

# Descriptor facts a declared config schema may be compared against. The descriptor
# schema version is deliberately absent: the container schema version it could be
# confused with is named separately as package_schema_version.
_DECLARED_FACT_NAMES: tuple[str, ...] = (
    "strategy_id",
    "strategy_version",
    "display_name",
    "research_visible",
    "parameters",
    "action_contract",
    "position_lifecycle",
)
_GRANT_KEYS = frozenset(
    {
        "schema",
        "trusted",
        "plugin_id",
        "package_version",
        "manifest_digest",
        "implementation_digest",
        "effects",
    }
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(document: Any) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _normalized(document: Any) -> dict[str, Any]:
    """One document as plain JSON data, so comparisons never depend on tuples."""
    return cast("dict[str, Any]", json.loads(_canonical(document)))


def local_strategy_plugin_id(package_id: str) -> str:
    """The host-owned plugin id one local package id maps to.

    Deriving the capability identifier here, in the host's own namespace, is what
    makes built-in shadowing impossible by construction: a package names its own
    ``package_id`` and can never claim ``ea.builtin.*`` or another package's
    identity.
    """
    plugin_id = f"{LOCAL_STRATEGY_PLUGIN_PREFIX}{package_id}"
    if not is_plugin_id(plugin_id):
        raise ManifestError(
            "package_invalid", "a local strategy package_id must be one identifier token"
        )
    return plugin_id


@dataclass(frozen=True, slots=True)
class LocalStrategyGrantV1:
    """One explicit operator decision: trust one exact local package identity.

    ``trusted`` is the human decision and ``effects`` is what the operator grants;
    ``local_compute`` must be among them, because reading these bytes at all is
    local computation. All four identity fields are the exact values this host
    verifies — the plugin id the manifest must declare, the exact semver
    ``package_version``, the digest of the sealed declarative manifest body, and the
    digest of the captured ``.eastrategy`` implementation bytes. Nothing is
    inferred, nothing falls back, and a grant that does not match is refused.
    """

    trusted: bool
    plugin_id: str
    package_version: str
    manifest_digest: str
    implementation_digest: str
    effects: tuple[str, ...] = (EFFECT_LOCAL_COMPUTE,)

    def __post_init__(self) -> None:
        if type(self.trusted) is not bool:
            raise ManifestError("grant_invalid", "trusted must be one explicit boolean decision")
        if not is_plugin_id(self.plugin_id):
            raise ManifestError("grant_invalid", "plugin_id must be one namespaced identifier")
        if not is_exact_version(self.package_version):
            raise ManifestError("grant_invalid", "package_version must be one exact version")
        for field_name, value in (
            ("manifest_digest", self.manifest_digest),
            ("implementation_digest", self.implementation_digest),
        ):
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ManifestError("grant_invalid", f"{field_name} must be one sha256 value")
        seen: list[str] = []
        for effect in self.effects:
            if type(effect) is not str or effect not in KNOWN_EFFECTS:
                raise ManifestError(
                    "grant_invalid", f"effect {effect!r} is not one of: {', '.join(KNOWN_EFFECTS)}"
                )
            if effect in seen:
                raise ManifestError("grant_invalid", "effects must not repeat")
            seen.append(effect)

    def document(self) -> dict[str, Any]:
        return {
            "schema": LOCAL_STRATEGY_GRANT_SCHEMA,
            "trusted": self.trusted,
            "plugin_id": self.plugin_id,
            "package_version": self.package_version,
            "manifest_digest": self.manifest_digest,
            "implementation_digest": self.implementation_digest,
            "effects": list(self.effects),
        }

    @staticmethod
    def decode(payload: Any) -> LocalStrategyGrantV1:
        """Read one granted decision from data; an unknown or partial grant is refused."""
        if type(payload) is not dict or set(payload) != _GRANT_KEYS:
            raise ManifestError(
                "grant_invalid", "a grant declares exactly: " + ", ".join(sorted(_GRANT_KEYS))
            )
        if payload["schema"] != LOCAL_STRATEGY_GRANT_SCHEMA:
            raise ManifestError("grant_schema", f"schema must be {LOCAL_STRATEGY_GRANT_SCHEMA}")
        effects = payload["effects"]
        if type(effects) is not list:
            raise ManifestError("grant_invalid", "effects must be a list of known effect names")
        return LocalStrategyGrantV1(
            trusted=payload["trusted"],
            plugin_id=payload["plugin_id"],
            package_version=payload["package_version"],
            manifest_digest=payload["manifest_digest"],
            implementation_digest=payload["implementation_digest"],
            effects=tuple(effects),
        )


@dataclass(frozen=True, slots=True)
class LocalStrategyCapabilityV1:
    """One statically verified local strategy: host records plus package facts.

    Inspection is static: it establishes no trust and activates nothing, which is
    what ``document()`` reports. Trust and permissions are re-checked by
    ``select_local_strategy`` and ``activate_local_strategy`` against the grant the
    caller supplies at that moment.
    """

    manifest: PluginManifestV1
    adapter: AdapterV1
    descriptor: dict[str, Any]
    package_id: str
    package_schema_version: int
    artifact_sha256: str
    warning: str = LOCAL_STRATEGY_WARNING

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def package_version(self) -> str:
        return self.manifest.package_version

    def source(self) -> dict[str, str]:
        """The v3/v4/v5 scenario ``strategy.source`` selecting exactly these bytes."""
        return {
            "kind": "local-package",
            "package_id": self.package_id,
            "artifact_sha256": self.artifact_sha256,
        }

    def pair(self) -> tuple[AdapterV1, PluginManifestV1]:
        """The static records, for a caller's own shared catalog and selection."""
        return self.adapter, self.manifest

    def catalog(self) -> PluginCatalogV1:
        """A one-entry catalog over these records, re-verified by the existing catalog."""
        return PluginCatalogV1([self.manifest], [self.adapter])

    def document(self) -> dict[str, Any]:
        return {
            "schema": LOCAL_STRATEGY_SCHEMA,
            "warning": self.warning,
            # Inspection is static: it neither trusts nor activates anything.
            "static_only": True,
            "trust_established": False,
            "plugin_id": self.plugin_id,
            "package_version": self.package_version,
            "manifest_digest": self.manifest.artifact_digest,
            "entrypoint": self.adapter.entrypoint,
            "effects": list(self.adapter.effects),
            "identity": dict(self.adapter.identity),
            "package": {
                "package_id": self.package_id,
                "schema_version": self.package_schema_version,
                "artifact_sha256": self.artifact_sha256,
                "source": self.source(),
            },
            "descriptor": dict(self.descriptor),
            "manifest": self.manifest.document(),
        }


def _require_grant(
    grant: LocalStrategyGrantV1 | None, requested: Iterable[str] = ()
) -> LocalStrategyGrantV1:
    """Fail closed on a missing trust decision or a permission the grant lacks.

    ``requested`` is what the capability asks to exercise at this boundary: every
    one of those effects must still be in the current grant, so withdrawing an
    effect refuses a capability whose identical earlier binding was accepted.
    ``local_compute`` is always required as well, because reading and running these
    bytes at all is local computation.
    """
    if not isinstance(grant, LocalStrategyGrantV1):
        raise ManifestError(
            "grant_required",
            "a local strategy needs an explicit trust and permission grant here; "
            "inspection alone establishes none",
        )
    if grant.trusted is not True:
        raise ManifestError(
            "trust_required", "the operator has not trusted this local package identity"
        )
    missing = sorted((set(requested) | {EFFECT_LOCAL_COMPUTE}) - set(grant.effects))
    if missing:
        raise ManifestError(
            "effect_not_granted",
            "the current grant does not cover the effects this capability exercises: "
            + ", ".join(missing),
        )
    return grant


def _package_digest(payload: object) -> str:
    """The sha256 of the captured container bytes, before anything reads them."""
    if type(payload) is not bytes:
        raise ManifestError("package_invalid", "captured package bytes must be one container")
    if not payload or len(payload) > MAX_ARTIFACT_BYTES:
        raise ManifestError(
            "package_invalid",
            f"captured package bytes must be at most {MAX_ARTIFACT_BYTES} bytes",
        )
    return sha256(payload).hexdigest()


def _read_package(payload: bytes, hex_digest: str) -> StrategyPackage:
    """Decode and verify one package statically; its Python is never executed here."""
    try:
        return inspect_package(payload, expected_sha256=hex_digest)
    except StrategyPackageError:
        raise ManifestError(
            "package_invalid", "the captured bytes are not one valid .eastrategy container"
        ) from None


def _package_schema_version(package: StrategyPackage) -> int:
    if isinstance(package, StrategyPackageV3):
        return 3
    if isinstance(package, StrategyPackageV2):
        return 2
    return 1


def _require_descriptor_identity(descriptor: Mapping[str, Any]) -> None:
    if not descriptor.get("strategy_id"):
        raise ManifestError("package_invalid", "a local strategy descriptor needs a strategy_id")
    version = descriptor.get("strategy_version")
    if type(version) is not int or version < 1:
        raise ManifestError(
            "package_invalid",
            "a local strategy descriptor must name one positive integer strategy_version",
        )


def _require_no_shadow(package: StrategyPackage) -> None:
    """Built-in shadowing is refused statically, by the existing research catalog."""
    try:
        ResearchStrategyCatalogV1((package,))
    except StrategyPackageError:
        raise ManifestError(
            "builtin_shadow",
            f"strategy {package.descriptor.strategy_id!r} shadows a capability this "
            "distribution already owns",
        ) from None


def _descriptor_facts(package: StrategyPackage, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """The static package facts a declared config schema may be compared against."""
    facts = {name: descriptor[name] for name in _DECLARED_FACT_NAMES if name in descriptor}
    facts["package_id"] = package.identity.package_id
    facts["package_schema_version"] = _package_schema_version(package)
    facts["artifact_sha256"] = package.identity.artifact_sha256
    return facts


def _require_declared_config(declared: PluginManifestV1, facts: Mapping[str, Any]) -> None:
    """A declared config schema may never contradict the package descriptor."""
    schema = declared.config_schema
    if not schema:
        return
    missing = [name for name in ("strategy_id", "strategy_version") if name not in schema]
    if missing:
        raise ManifestError(
            "config_schema_mismatch",
            "a declared local strategy config schema must name: " + ", ".join(missing),
        )
    for name, value in schema.items():
        if name in facts and _canonical(value) != _canonical(facts[name]):
            raise ManifestError(
                "config_schema_mismatch",
                f"the declared config schema field {name!r} does not match the package descriptor",
            )


def _adapter(
    declared: PluginManifestV1,
    facts: Mapping[str, Any],
    package: StrategyPackage,
    hex_digest: str,
) -> AdapterV1:
    """The host adapter record: this host's facts about the package, not its claims.

    The record's effects are the declared effects this host's current policy grants
    (``HOST_GRANTED_EFFECTS``), which is a bounded statement about this profile and
    never a claim of operating-system enforcement; a declared effect outside that
    policy is left to the catalog's own ``effects_denied`` refusal.
    """
    return AdapterV1(
        entrypoint=LOCAL_STRATEGY_ENTRYPOINT,
        kind="strategy",
        provides=("strategy.research",) if facts["research_visible"] else ("strategy.internal",),
        effects=tuple(effect for effect in declared.effects if effect in HOST_GRANTED_EFFECTS),
        identity={
            "strategy_id": facts["strategy_id"],
            "strategy_version": facts["strategy_version"],
            # The existing implementation-digest scheme, applied to the bytes that
            # actually run: the captured container, read statically by inspect_package.
            "implementation_sha256": "sha256:" + hex_digest,
            "package_id": package.identity.package_id,
            "package_schema_version": _package_schema_version(package),
            "plugin_manifest_sha256": declared.artifact_digest,
        },
    )


def _authorize_local(plugin: VerifiedPluginV1, grant: LocalStrategyGrantV1 | None) -> None:
    """The grant re-check every boundary applies; it may only refuse."""
    if plugin.manifest.entrypoint != LOCAL_STRATEGY_ENTRYPOINT:
        # This grant covers the local strategy capability only; every other layer
        # keeps whatever authorization it already has.
        return
    # Every effect the capability requests must be granted now, not only when the
    # binding was frozen: a withdrawn effect refuses the same identity.
    granted = _require_grant(grant, plugin.manifest.effects)
    manifest = plugin.manifest
    if (manifest.plugin_id, manifest.package_version, manifest.artifact_digest) != (
        granted.plugin_id,
        granted.package_version,
        granted.manifest_digest,
    ):
        raise ManifestError(
            "grant_identity_mismatch", "the grant does not name the capability being selected"
        )
    if plugin.identity().get("implementation_sha256") != granted.implementation_digest:
        raise ManifestError(
            "grant_identity_mismatch",
            "the grant does not name the package implementation being selected",
        )


def _authorizer(grant: LocalStrategyGrantV1 | None) -> Authorize:
    def authorize(plugin: VerifiedPluginV1) -> None:
        _authorize_local(plugin, grant)

    return authorize


def inspect_local_strategy(
    manifest: PluginManifestV1 | Mapping[str, Any],
    payload: bytes,
    *,
    grant: LocalStrategyGrantV1 | None = None,
) -> LocalStrategyCapabilityV1:
    """Read one captured ``.eastrategy`` package statically; execute nothing.

    ``manifest`` is the declarative capability the package claims and ``payload``
    the captured container bytes. The manifest is decoded by the existing manifest
    layer, the derived host adapter record and that manifest are verified by the
    existing catalog, the built-in namespace is checked by the existing research
    catalog, and the package descriptor comes from ``inspect_package``. The
    package's Python is never compiled, imported or executed, so this is safe to run
    on a package whose trust has not been decided yet.

    ``grant`` is optional and inspection needs no trust: passing ``None`` returns a
    static record that establishes nothing. A grant that *is* supplied must name
    this exact identity and cover every effect the manifest requests, so an
    untrusted or insufficient grant is refused rather than quietly ignored.
    """
    declared = decode_manifest(
        manifest.document() if isinstance(manifest, PluginManifestV1) else manifest
    )
    if declared.kind != "strategy" or declared.entrypoint != LOCAL_STRATEGY_ENTRYPOINT:
        raise ManifestError(
            "local_strategy_manifest",
            f"a local strategy capability must be one {LOCAL_STRATEGY_ENTRYPOINT} "
            "adapter of kind strategy",
        )
    hex_digest = _package_digest(payload)
    package = _read_package(payload, hex_digest)
    _require_no_shadow(package)
    if declared.plugin_id != local_strategy_plugin_id(package.identity.package_id):
        raise ManifestError(
            "manifest_package_mismatch",
            "the manifest plugin_id is not this package's host-owned capability identifier",
        )
    descriptor = _normalized(package.descriptor.document())
    _require_descriptor_identity(descriptor)
    facts = _descriptor_facts(package, descriptor)
    _require_declared_config(declared, facts)
    adapter = _adapter(declared, facts, package, hex_digest)
    verified = PluginCatalogV1([declared], [adapter]).get(
        declared.plugin_id,
        declared.package_version,
    )
    if grant is not None:
        granted = _require_grant(grant, declared.effects)
        if (declared.plugin_id, declared.package_version, declared.artifact_digest) != (
            granted.plugin_id,
            granted.package_version,
            granted.manifest_digest,
        ):
            raise ManifestError(
                "grant_identity_mismatch", "the grant does not name this capability"
            )
        if "sha256:" + hex_digest != granted.implementation_digest:
            raise ManifestError(
                "grant_identity_mismatch",
                "the captured bytes are not the granted implementation_digest",
            )
    return LocalStrategyCapabilityV1(
        manifest=verified.manifest,
        adapter=verified.adapter,
        descriptor=descriptor,
        package_id=package.identity.package_id,
        package_schema_version=_package_schema_version(package),
        artifact_sha256=hex_digest,
    )


def select_local_strategy(
    capability: LocalStrategyCapabilityV1, *, grant: LocalStrategyGrantV1 | None = None
) -> PluginBindingV1:
    """Bind one inspected capability to the existing frozen run binding.

    The selection is explicit and names one exact version, and it always requires
    the caller's current trusted grant covering every requested effect, so a
    capability inspected without one, or under a grant that was since withdrawn, is
    not selected now.
    """
    if not isinstance(capability, LocalStrategyCapabilityV1):
        raise ManifestError(
            "local_strategy_unbound", "selection needs one inspected local strategy capability"
        )
    selection = PluginSelectionV1.decode(
        {
            "strategy": {
                "plugin_id": capability.plugin_id,
                "package_version": capability.package_version,
            }
        }
    )
    return resolve_selection(capability.catalog(), selection, authorize=_authorizer(grant))


def activate_local_strategy(
    binding: PluginBindingV1,
    payload: bytes,
    *,
    catalog: PluginCatalogV1,
    grant: LocalStrategyGrantV1 | None = None,
) -> ResearchStrategyCatalogV1:
    """Re-check one frozen binding and its grant, then hand the engine exact bytes.

    Nothing here executes the package: the returned research catalog is built from
    ``inspect_package`` of the captured bytes, and only the engine's own dispatch
    compiles them. The binding is re-verified against the current catalog and the
    caller's current grant — trust, identity and every requested effect — first, so
    drift, a changed version, changed bytes, a revoked trust, a withdrawn effect or
    a missing permission all fail closed before that point. Call this at each new
    dispatch: the catalog it returns is the ordinary one and does not re-check
    anything by itself afterwards.
    """
    granted = _require_grant(grant)
    if not isinstance(catalog, PluginCatalogV1):
        raise ManifestError("local_strategy_unbound", "activation needs the current host catalog")
    verify_binding(catalog, binding, authorize=_authorizer(granted))
    component = binding.component("strategy")
    if component is None or component.entrypoint != LOCAL_STRATEGY_ENTRYPOINT:
        raise ManifestError(
            "local_strategy_unbound", "the frozen binding selects no local strategy capability"
        )
    if (component.plugin_id, component.package_version, component.artifact_digest) != (
        granted.plugin_id,
        granted.package_version,
        granted.manifest_digest,
    ):
        raise ManifestError(
            "grant_identity_mismatch", "the frozen binding is not the granted plugin identity"
        )
    if component.identity.get("implementation_sha256") != granted.implementation_digest:
        raise ManifestError(
            "grant_identity_mismatch",
            "the frozen binding is not the granted package implementation",
        )
    hex_digest = _package_digest(payload)
    if "sha256:" + hex_digest != granted.implementation_digest:
        raise ManifestError(
            "grant_identity_mismatch",
            "the captured bytes are not the granted package implementation",
        )
    package = _read_package(payload, hex_digest)
    descriptor = _normalized(package.descriptor.document())
    if (
        component.identity.get("strategy_id") != descriptor.get("strategy_id")
        or component.identity.get("strategy_version") != descriptor.get("strategy_version")
        or component.identity.get("package_id") != package.identity.package_id
        or component.identity.get("package_schema_version") != _package_schema_version(package)
    ):
        raise ManifestError(
            "binding_drift", "the captured bytes are not the strategy this binding froze"
        )
    try:
        return ResearchStrategyCatalogV1((package,))
    except StrategyPackageError:
        raise ManifestError(
            "builtin_shadow",
            f"strategy {package.descriptor.strategy_id!r} shadows a capability this "
            "distribution already owns",
        ) from None
