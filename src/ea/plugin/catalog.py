"""Host-owned adapter table and the closed local capability catalog.

The catalog is the host's own record of what this distribution can execute. A
manifest's declarations are checked against that record and the current host
policy before anything becomes selectable, so a package can never promote its
own claim into an authorized capability. A rejected entry stays visible with its
reason and is never silently dropped into a usable one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ea.plugin.manifest import (
    HOST_GRANTED_EFFECTS,
    HOST_INTERFACES,
    HOST_PROVIDES,
    KIND_INTERFACES,
    RESERVED_KINDS,
    RUNTIME_PROFILE,
    SUPPORTED_KINDS,
    ManifestError,
    PluginManifestV1,
    decode_manifest,
    is_plugin_id,
    verify_interface,
)

CATALOG_SCHEMA = "ea.plugin-catalog.v1"

# Identity the host record must be able to name for each kind; a binding without
# it could not be re-verified later, so it is refused instead of guessed. Every
# kind requires the fingerprint of the implementation artifact it wraps, not only
# the digest of the manifest that describes it.
REQUIRED_IDENTITY: dict[str, tuple[str, ...]] = {
    "model": ("provider", "protocol", "implementation_sha256"),
    "data": ("dataset_id", "implementation_sha256"),
    "strategy": ("strategy_id", "strategy_version", "implementation_sha256"),
    "validator": ("validator_id", "validator_version", "implementation_sha256"),
    "backtest-method": ("method_id", "method_version", "implementation_sha256"),
}


@dataclass(frozen=True, slots=True)
class AdapterV1:
    """One host adapter record: a fact about this host, not a package claim.

    ``entrypoint`` names one identifier in this closed table. Nothing here is an
    import path, a module reference or an executable string.
    """

    entrypoint: str
    kind: str
    provides: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    identity: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerifiedPluginV1:
    """One manifest verified against the host record and current host policy."""

    manifest: PluginManifestV1
    adapter: AdapterV1
    granted_effects: tuple[str, ...]

    def identity(self) -> dict[str, Any]:
        return dict(self.adapter.identity)

    def document(self) -> dict[str, Any]:
        return {
            "plugin_id": self.manifest.plugin_id,
            "kind": self.manifest.kind,
            "package_version": self.manifest.package_version,
            "artifact_digest": self.manifest.artifact_digest,
            "interface": self.manifest.interface.document(),
            "entrypoint": self.manifest.entrypoint,
            "effects": {
                "declared": list(self.manifest.effects),
                "granted": list(self.granted_effects),
                "host_granted": sorted(HOST_GRANTED_EFFECTS),
            },
            "identity": self.identity(),
            "manifest": self.manifest.document(),
            "usable": True,
            "reason": None,
        }


def _validated(manifest: PluginManifestV1) -> PluginManifestV1:
    """Re-read one declared body so a dataclass built in-process is validated too.

    A manifest is only ever a declaration. Whether it arrived as data or was
    constructed directly, it passes the same decode, field, type and digest
    checks, so no caller can hand this catalog a claim that skipped them.
    """
    try:
        document = manifest.document()
    except (AttributeError, TypeError, ValueError):
        raise ManifestError(
            "manifest_invalid",
            f"capability {manifest.plugin_id!r} does not declare a readable manifest body",
        ) from None
    decoded = decode_manifest(document)
    # An already canonical declaration keeps its identity; anything else is
    # replaced by the validated form it decodes to.
    return manifest if decoded == manifest else decoded


class PluginCatalogV1:
    """One closed catalog: exact identities, no discovery, no latest fallback."""

    def __init__(
        self,
        manifests: Iterable[PluginManifestV1],
        adapters: Iterable[AdapterV1],
    ) -> None:
        self._adapters: dict[str, AdapterV1] = {}
        for adapter in adapters:
            self._register(adapter)
        self._manifests: dict[tuple[str, str], PluginManifestV1] = {}
        self._declared: dict[tuple[str, str], PluginManifestV1] = {}
        self._rejected: dict[tuple[str, str], ManifestError] = {}
        for manifest in manifests:
            if not isinstance(manifest, PluginManifestV1):
                raise ManifestError("manifest_invalid", "a catalog entry must be one manifest")
            key = (manifest.plugin_id, manifest.package_version)
            if key in self._manifests:
                raise ManifestError(
                    "manifest_duplicate",
                    f"capability {manifest.plugin_id} version "
                    f"{manifest.package_version} is declared twice",
                )
            self._manifests[key] = manifest
            try:
                self._declared[key] = _validated(manifest)
            except ManifestError as error:
                # Visible and unusable, never repaired into an acceptable claim.
                self._rejected[key] = error
        candidates: dict[tuple[str, str], AdapterV1] = {}
        for key, manifest in self._declared.items():
            if key in self._rejected:
                continue
            try:
                candidates[key] = self._verify(manifest)
            except ManifestError as error:
                self._rejected[key] = error
        self._verified: dict[tuple[str, str], VerifiedPluginV1] = {}
        self._resolve_dependencies(candidates)

    def _register(self, adapter: AdapterV1) -> None:
        if not is_plugin_id(adapter.entrypoint) or adapter.kind not in SUPPORTED_KINDS:
            raise ManifestError(
                "adapter_invalid",
                "a host adapter needs a namespaced entrypoint and a known kind",
            )
        if adapter.entrypoint in self._adapters:
            raise ManifestError(
                "adapter_duplicate", f"adapter {adapter.entrypoint} is declared twice"
            )
        denied = set(adapter.effects) - HOST_GRANTED_EFFECTS
        if denied:
            # Host misconfiguration, refused before any manifest can rely on it.
            raise ManifestError(
                "adapter_effect_denied",
                f"host adapter {adapter.entrypoint} declares effects outside host policy: "
                f"{', '.join(sorted(denied))}",
            )
        self._adapters[adapter.entrypoint] = adapter

    def _verify(self, manifest: PluginManifestV1) -> AdapterV1:
        """Check one claim against the host record and current host policy."""
        verify_interface(manifest)
        adapter = self._adapters.get(manifest.entrypoint)
        if adapter is None:
            raise ManifestError(
                "entrypoint_unknown",
                f"entrypoint {manifest.entrypoint!r} is not one of this host's adapters; "
                "third-party code is never imported",
            )
        if adapter.kind != manifest.kind:
            raise ManifestError(
                "entrypoint_kind_mismatch",
                f"entrypoint {manifest.entrypoint!r} is a {adapter.kind} adapter, "
                f"not a {manifest.kind} one",
            )
        missing = [
            name for name in REQUIRED_IDENTITY[manifest.kind] if not adapter.identity.get(name)
        ]
        if missing:
            raise ManifestError(
                "adapter_identity_incomplete",
                f"host adapter {manifest.entrypoint!r} cannot name: {', '.join(missing)}",
            )
        undeclared = sorted(set(manifest.provides) - set(adapter.provides))
        if undeclared:
            raise ManifestError(
                "provides_not_verified",
                f"host adapter {manifest.entrypoint!r} does not provide: {', '.join(undeclared)}",
            )
        # Host policy first: an effect outside it is refused whatever the host
        # adapter table says, so a malicious manifest cannot reach a grant.
        denied = sorted(set(manifest.effects) - HOST_GRANTED_EFFECTS)
        if denied:
            raise ManifestError(
                "effects_denied",
                f"host policy does not grant: {', '.join(denied)}",
            )
        unverified = sorted(set(manifest.effects) - set(adapter.effects))
        if unverified:
            raise ManifestError(
                "effects_not_verified",
                f"host adapter {manifest.entrypoint!r} does not exercise: {', '.join(unverified)}",
            )
        return adapter

    def _resolve_dependencies(self, candidates: dict[tuple[str, str], AdapterV1]) -> None:
        """Only the host or an already verified capability satisfies a requirement.

        A candidate becomes usable once the host primitives and the capabilities
        already verified supply everything it requires, and every exact
        dependency it names is itself verified with the declared digest. A
        rejected or unverified provider therefore never carries a dependent in
        with it, and a cycle leaves both sides refused rather than usable.
        """
        remaining = dict(candidates)
        provided: set[str] = set(HOST_PROVIDES)
        while remaining:
            progressed = False
            for key, adapter in list(remaining.items()):
                manifest = self._declared[key]
                if not set(manifest.requires) <= provided:
                    continue
                if not all(
                    (item.plugin_id, item.package_version) in self._verified
                    for item in manifest.dependencies
                ):
                    continue
                del remaining[key]
                progressed = True
                mismatch = self._digest_mismatch(manifest)
                if mismatch is not None:
                    self._rejected[key] = mismatch
                    continue
                self._verified[key] = VerifiedPluginV1(manifest, adapter, tuple(manifest.effects))
                provided.update(manifest.provides)
            if not progressed:
                break
        # Diagnosed as one batch, so a leftover reason never depends on the order
        # the remaining entries happen to be visited in.
        self._rejected.update(
            {key: self._unsatisfied(self._declared[key], provided) for key in remaining}
        )

    def _digest_mismatch(self, manifest: PluginManifestV1) -> ManifestError | None:
        for dependency in manifest.dependencies:
            target = self._verified.get((dependency.plugin_id, dependency.package_version))
            if target is None or dependency.artifact_digest is None:
                continue
            if target.manifest.artifact_digest != dependency.artifact_digest:
                return ManifestError(
                    "dependency_digest_mismatch",
                    f"capability {dependency.plugin_id} version "
                    f"{dependency.package_version} is not the declared artifact",
                )
        return None

    def _unsatisfied(self, manifest: PluginManifestV1, provided: set[str]) -> ManifestError:
        for dependency in manifest.dependencies:
            key = (dependency.plugin_id, dependency.package_version)
            if key in self._verified:
                continue
            if key not in self._manifests:
                return ManifestError(
                    "dependency_unavailable",
                    f"capability {dependency.plugin_id} version "
                    f"{dependency.package_version} is not in this catalog",
                )
            reason = self._rejected.get(key)
            if reason is not None:
                return ManifestError(
                    "dependency_unavailable",
                    f"capability {dependency.plugin_id} version "
                    f"{dependency.package_version} is declared but not usable: {reason.code}",
                )
            return ManifestError(
                "dependency_cycle",
                f"capability {manifest.plugin_id} version {manifest.package_version} and "
                f"capability {dependency.plugin_id} version {dependency.package_version} "
                "cannot be resolved together",
            )
        unresolved = sorted(set(manifest.requires) - provided)
        if unresolved:
            return ManifestError(
                "dependency_unresolved",
                f"no verified capability or host primitive provides: {', '.join(unresolved)}",
            )
        return ManifestError(
            "dependency_cycle",
            f"capability {manifest.plugin_id} version {manifest.package_version} is part "
            "of an unresolved dependency cycle",
        )

    def get(self, plugin_id: str, package_version: str) -> VerifiedPluginV1:
        """Resolve one exact capability identity; never the newest version."""
        key = (plugin_id, package_version)
        if key not in self._manifests:
            raise ManifestError(
                "plugin_unavailable",
                f"capability {plugin_id!r} version {package_version!r} is not in this catalog; "
                "no other version is substituted",
            )
        error = self._rejected.get(key)
        if error is not None:
            raise ManifestError(error.code, error.message)
        return self._verified[key]

    def entries(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Every declared entry, usable or not, with its reason."""
        entries: list[dict[str, Any]] = []
        for key, manifest in sorted(self._manifests.items()):
            if kind is not None and manifest.kind != kind:
                continue
            verified = self._verified.get(key)
            if verified is not None:
                entries.append(verified.document())
                continue
            error = self._rejected[key]
            # The validated body when there is one, so a rejected entry is shown
            # as it was read rather than as it was hand-built.
            declared = self._declared.get(key, manifest)
            entries.append(
                {
                    "plugin_id": declared.plugin_id,
                    "kind": declared.kind,
                    "package_version": declared.package_version,
                    "artifact_digest": declared.artifact_digest,
                    "interface": declared.interface.document(),
                    "entrypoint": declared.entrypoint,
                    "effects": {
                        "declared": list(declared.effects),
                        "granted": [],
                        "host_granted": sorted(HOST_GRANTED_EFFECTS),
                    },
                    "identity": dict(self._adapters[declared.entrypoint].identity)
                    if declared.entrypoint in self._adapters
                    else {},
                    "manifest": declared.document(),
                    "usable": False,
                    "reason": {"code": error.code, "message": error.message},
                }
            )
        return entries

    def document(self) -> dict[str, Any]:
        return {
            "schema": CATALOG_SCHEMA,
            "runtime_profile": RUNTIME_PROFILE,
            "supported_kinds": list(SUPPORTED_KINDS),
            "reserved_kinds": list(RESERVED_KINDS),
            "kind_interfaces": dict(KIND_INTERFACES),
            "host_granted_effects": sorted(HOST_GRANTED_EFFECTS),
            "interfaces": [item.document() for item in HOST_INTERFACES],
            "plugins": self.entries(),
        }
