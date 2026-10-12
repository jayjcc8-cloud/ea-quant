"""One workspace-local strategy package slot: captured bytes, explicit trust.

This is the *storage* half of the explicitly trusted local strategy adapter
(``ea.plugin.local_strategy``): it keeps exactly one captured ``.eastrategy``
package, the declarative capability that package claims, the sealed grant an
operator decided on, and the virtual scenario the slot exposes. Every read
re-derives the capability from the retained bytes and re-checks the exact
identity, digests and granted effects, so a replaced slot, a drifted manifest or
a withdrawn trust fails closed instead of falling back to anything.

Every directory here is opened once, with ``O_NOFOLLOW``, relative to an
already-verified parent descriptor, and every later read, write and revocation
goes through those descriptors rather than through a re-resolved path: a
symlinked slot is never adopted, a workspace or slot directory swapped after
this store was built is refused before anything is written, and the reads are
bounded by the bytes actually returned rather than by a size the descriptor
merely reports. This is path anchoring, not an operating-system sandbox.

Limits, all deliberate:

* one slot per workspace — a second registration replaces the first, and the
  replaced identity's binding is no longer selectable;
* the captured package is bounded, read through anchored no-follow descriptors,
  and never executed here;
* the grant is never derived from the manifest it authorizes: every identity
  field and digest must be supplied by the caller and must match what the static
  inspection actually measured. Nothing in the package can grant itself.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from ea.plugin.local_strategy import (
    LOCAL_STRATEGY_ENTRYPOINT,
    LOCAL_STRATEGY_SCHEMA,
    LOCAL_STRATEGY_WARNING,
    LocalStrategyCapabilityV1,
    LocalStrategyGrantV1,
    activate_local_strategy,
    inspect_local_strategy,
    select_local_strategy,
)
from ea.plugin.manifest import (
    EFFECT_LOCAL_COMPUTE,
    KNOWN_EFFECTS,
    ManifestError,
    PluginManifestV1,
    compute_artifact_digest,
    decode_manifest,
)
from ea.plugin.selection import PluginBindingV1
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import MAX_ARTIFACT_BYTES
from ea.web.uploads import UploadError, _open_child_directory, _open_workspace

SLOT_SCHEMA = "ea.local-strategy-slot.v1"
SLOT_DIRECTORY = "local-strategy"
LOCAL_SCENARIO_ID = "local-package.yaml"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_META_BYTES = 16 * 1024
MAX_SCENARIO_BYTES = 1024 * 1024
PACKAGE_NAME = "package.eastrategy"
MANIFEST_NAME = "manifest.json"
GRANT_NAME = "grant.json"
META_NAME = "meta.json"
SCENARIO_NAME = "scenario.yaml"
SLOT_LIMITATIONS: tuple[str, ...] = (
    "One local package slot per workspace: registering again replaces it, and the "
    "replaced identity's binding stops being selectable.",
    "Capturing a package is an operator decision, not a review: this host verifies "
    "identity, digests and declared effects, and never the Python itself. The "
    "package runs with the ordinary privileges of this process, in this process, "
    "with no sandbox.",
    "Revoking trust stops every later selection, dispatch and history read. It "
    "cannot interrupt Python that is already running, and it does not undo a run "
    "that already happened.",
)


def sealed_manifest(declared: Any) -> PluginManifestV1:
    """One complete declaration, with the host filling only a blank artifact_digest.

    ``artifact_digest`` is the integrity identity of the declaration body itself, so
    a caller that pastes a complete manifest may leave that one field blank and this
    host computes it. Any other value is the caller's own claim and is verified, not
    repaired: a digest that does not match the body it arrived with is refused, so an
    edited declaration cannot be re-sealed into a fresh identity. This proves the
    declaration is intact. It still proves nothing about what the package does.
    """
    body = declared.document() if isinstance(declared, PluginManifestV1) else declared
    if type(body) is not dict:
        raise ManifestError("manifest_field", "a capability manifest must be one object")
    if body.get("artifact_digest") != "":
        # A caller-declared digest is verified, not repaired: decode_manifest checks
        # it against the body it arrived with and refuses a mismatch, so an edited
        # declaration cannot be re-sealed into a fresh identity.
        return decode_manifest(body)
    return decode_manifest({**body, "artifact_digest": compute_artifact_digest(body)})


def _canonical(document: Any) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _bounded_payload(payload: Any) -> bytes:
    if type(payload) is not bytes:
        raise ManifestError("package_invalid", "captured package bytes must be one container")
    if not payload or len(payload) > MAX_ARTIFACT_BYTES:
        raise ManifestError(
            "package_invalid",
            f"captured package bytes must be at most {MAX_ARTIFACT_BYTES} bytes",
        )
    return payload


def _effects(effects: Any) -> list[str]:
    if type(effects) is not tuple and type(effects) is not list:
        raise ManifestError("grant_invalid", "effects must be one explicit list of permissions")
    granted: list[str] = []
    for effect in effects:
        if type(effect) is not str or effect not in KNOWN_EFFECTS:
            raise ManifestError(
                "grant_invalid", f"effect {effect!r} is not one of: {', '.join(KNOWN_EFFECTS)}"
            )
        if effect in granted:
            raise ManifestError("grant_invalid", "effects must not repeat")
        granted.append(effect)
    if EFFECT_LOCAL_COMPUTE not in granted:
        raise ManifestError(
            "effect_not_granted",
            "a local strategy always needs local_compute: reading these bytes is local computation",
        )
    return granted


def _scenario_document(scenario: bytes) -> dict[str, Any]:
    import yaml

    try:
        document = yaml.safe_load(scenario.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ManifestError(
            "scenario_invalid", "the retained scenario is not one YAML document"
        ) from None
    if type(document) is not dict:
        raise ManifestError("scenario_invalid", "the retained scenario is not one YAML mapping")
    return document


def _require_scenario_matches(
    document: Mapping[str, Any], current: LocalStrategyCapabilityV1
) -> None:
    """The retained scenario must select exactly this capability and nothing else."""
    strategy = document.get("strategy")
    if type(strategy) is not dict:
        raise ManifestError("scenario_invalid", "the retained scenario declares no strategy")
    descriptor = current.descriptor
    expected_source = current.source()
    if (
        strategy.get("id") != descriptor.get("strategy_id")
        or strategy.get("version") != descriptor.get("strategy_version")
        or strategy.get("source") != expected_source
    ):
        raise ManifestError(
            "scenario_invalid",
            "the retained scenario does not select exactly the captured package identity",
        )
    if type(document.get("schema_version")) is not int:
        raise ManifestError("scenario_invalid", "the retained scenario needs one schema_version")


class LocalStrategyStoreV1:
    """The single trusted-package slot of one workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.root = self.workspace / SLOT_DIRECTORY
        self._workspace_fd: int | None = None
        self._slot_fd: int | None = None
        try:
            self._workspace_fd = _open_workspace(self.workspace)
        except UploadError as error:
            self.close()
            raise ManifestError("store_untrusted", error.message) from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the anchored descriptors; no file is touched."""
        descriptors = [self._slot_fd, self._workspace_fd]
        self._slot_fd = None
        self._workspace_fd = None
        for descriptor in descriptors:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def __del__(self) -> None:
        with suppress(BaseException):
            self.close()

    # -- anchoring ----------------------------------------------------------

    def _slot(self) -> int:
        """The slot descriptor, created and opened nofollow under a verified parent.

        ``O_NOFOLLOW`` and a link-free stat are what keep this store from adopting a
        symlinked slot: a link where the slot belongs is refused rather than followed,
        so no write can land outside the workspace through it.
        """
        if self._workspace_fd is None:
            raise ManifestError("store_untrusted", "this local strategy slot is closed")
        if self._slot_fd is None:
            try:
                self._slot_fd = _open_child_directory(self._workspace_fd, SLOT_DIRECTORY)
            except UploadError as error:
                raise ManifestError("store_untrusted", error.message) from None
        return self._slot_fd

    def _anchor(self) -> int:
        """The slot descriptor, re-checked against the live names before any use.

        A descriptor this store holds cannot change identity by itself, so what is
        checked is whether the names still reach it: a workspace or slot directory
        that was swapped, removed or linked after this store was built is a refusal,
        not a write into a directory nobody can reach afterwards.
        """
        directory = self._slot()
        workspace_fd = self._workspace_fd
        if workspace_fd is None:
            raise ManifestError("store_untrusted", "this local strategy slot is closed")
        try:
            live = os.stat(self.workspace, follow_symlinks=False)
            named = os.stat(SLOT_DIRECTORY, dir_fd=workspace_fd, follow_symlinks=False)
        except OSError:
            raise ManifestError(
                "store_untrusted", "the local strategy slot is no longer reachable"
            ) from None
        pinned = os.fstat(workspace_fd)
        held = os.fstat(directory)
        if (
            not stat.S_ISDIR(live.st_mode)
            or (live.st_dev, live.st_ino) != (pinned.st_dev, pinned.st_ino)
            or not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
        ):
            raise ManifestError(
                "store_untrusted",
                "the workspace or the local strategy slot was replaced; this slot is "
                "refused rather than followed",
            )
        return directory

    def _read(self, name: str, limit: int, *, label: str) -> bytes | None:
        """One bounded slot file, read relative to the anchored descriptor.

        The bound is enforced on the bytes actually returned, one byte past the
        limit, so a file that reports one size and yields another cannot be read
        past it.
        """
        directory = self._anchor()
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError:
            raise ManifestError(
                "slot_invalid", f"the stored {label} is not readable evidence"
            ) from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ManifestError("slot_invalid", f"the stored {label} is not one regular file")
            payload = b""
            while True:
                chunk = os.read(descriptor, min(1 << 20, limit + 1 - len(payload)))
                if not chunk:
                    return payload
                payload += chunk
                if len(payload) > limit:
                    raise ManifestError(
                        "slot_invalid", f"the stored {label} exceeds its {limit}-byte bound"
                    )
        except OSError:
            raise ManifestError(
                "slot_invalid", f"the stored {label} is not readable evidence"
            ) from None
        finally:
            with suppress(OSError):
                os.close(descriptor)

    def _write(self, name: str, payload: bytes) -> None:
        """One atomic replacement inside the anchored slot directory.

        The temporary name is created relative to the slot descriptor, so the write
        never resolves a path again, and the rename that publishes it is a directory
        operation on that same descriptor.
        """
        directory = self._anchor()
        temporary = f".{name}-{uuid4().hex}"
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            raise ManifestError(
                "slot_write_failed", f"the local strategy slot could not write {name}"
            ) from None
        with suppress(OSError):  # directory fsync is unavailable on some filesystems
            os.fsync(directory)

    def _decode(self, name: str, limit: int, *, label: str) -> dict[str, Any] | None:
        raw = self._read(name, limit, label=label)
        if raw is None:
            return None
        try:
            document = json.loads(raw)
        except ValueError:
            raise ManifestError(
                "slot_invalid", f"the stored {label} is not a JSON document"
            ) from None
        if type(document) is not dict:
            raise ManifestError("slot_invalid", f"the stored {label} is not one JSON object")
        return document

    # -- static inspection --------------------------------------------------

    def inspect(self, manifest: Any, payload: Any) -> LocalStrategyCapabilityV1:
        """Read one captured package statically. Establishes no trust, writes nothing.

        A complete declaration may leave ``artifact_digest`` blank for this host to
        compute; it is the only field the host fills, and a wrong non-empty digest is
        refused rather than replaced.
        """
        return inspect_local_strategy(sealed_manifest(manifest), _bounded_payload(payload))

    # -- slot reads ---------------------------------------------------------

    def meta(self) -> dict[str, Any] | None:
        return self._decode(META_NAME, MAX_META_BYTES, label="slot metadata")

    def grant(self) -> LocalStrategyGrantV1 | None:
        raw = self._read(GRANT_NAME, MAX_META_BYTES, label="grant")
        if raw is None:
            return None
        try:
            return LocalStrategyGrantV1.decode(json.loads(raw))
        except (ValueError, TypeError):
            raise ManifestError(
                "slot_invalid", "the stored grant is not one grant document"
            ) from None

    def retained(self) -> tuple[bytes, dict[str, Any]] | None:
        """The captured bytes and the sealed manifest this slot holds, or None."""
        meta = self.meta()
        if meta is None:
            return None
        manifest = self._decode(MANIFEST_NAME, MAX_MANIFEST_BYTES, label="manifest")
        if manifest is None:
            raise ManifestError("slot_invalid", "the slot holds no readable manifest")
        payload = self._read(PACKAGE_NAME, MAX_ARTIFACT_BYTES, label="package")
        if payload is None:
            raise ManifestError("slot_invalid", "the slot holds no readable package")
        return payload, manifest

    def capability(self) -> LocalStrategyCapabilityV1 | None:
        """The current trusted capability, re-derived and re-checked on every call.

        Returns None whenever there is no slot, no trusted grant, a revoked trust,
        or a slot that is not there any more; a slot whose recorded identity no
        longer matches what the retained bytes actually are is a refusal.
        """
        meta = self.meta()
        if meta is None or meta.get("revoked") is True:
            return None
        grant = self.grant()
        if grant is None or grant.trusted is not True:
            return None
        retained = self.retained()
        if retained is None:
            return None
        payload, manifest = retained
        current = inspect_local_strategy(manifest, payload, grant=grant)
        if (
            current.plugin_id,
            current.package_version,
            "sha256:" + current.artifact_sha256,
        ) != (
            meta.get("plugin_id"),
            meta.get("package_version"),
            meta.get("implementation_digest"),
        ):
            raise ManifestError(
                "slot_changed", "the retained package is not the identity this slot recorded"
            )
        return current

    def binding(self) -> dict[str, Any] | None:
        """The exact frozen binding the current slot selects, or None."""
        current = self.capability()
        if current is None:
            return None
        return select_local_strategy(current, grant=self.grant()).document()

    def catalog(
        self, plugin_binding: dict[str, object] | None = None, *, payload: bytes | None = None
    ) -> ResearchStrategyCatalogV1:
        """Activate this slot's exact current grant over retained or frozen bytes.

        Activation is static. The caller can load/execute only after this check;
        a frozen payload never substitutes a newer slot implementation.
        """
        capability = self.capability()
        if capability is None:
            raise ManifestError(
                "grant_required",
                "this workspace has no currently trusted local strategy package",
            )
        grant = self.grant()
        current = select_local_strategy(capability, grant=grant)
        if plugin_binding is not None:
            # A caller's binding carries every layer its recipe selected; only the
            # strategy component names this slot. That component must be exactly the
            # one this slot's current grant selects, and activation then runs over
            # the strategy-only binding: this host's one-entry catalog is the only
            # thing that can vouch for this capability, and the other layers keep
            # the verification their own catalogs and the frozen binding give them.
            frozen = PluginBindingV1.decode(plugin_binding)
            selected = frozen.component("strategy")
            expected = current.component("strategy")
            if selected is None or expected is None or selected.document() != expected.document():
                raise ManifestError(
                    "binding_drift",
                    "the frozen binding does not select the local strategy this slot "
                    "currently grants",
                )
        retained = self.retained()
        if retained is None:
            raise ManifestError("slot_invalid", "the local strategy slot is unreadable")
        return activate_local_strategy(
            current,
            retained[0] if payload is None else payload,
            catalog=capability.catalog(),
            grant=grant,
        )

    def scenario_path(self) -> Path | None:
        """The retained scenario path, only while the slot it belongs to is current.

        The loader reads a path, so the entry is checked here — no-follow, under the
        anchored slot descriptor, a regular file within its bound, in a directory the
        checks above just proved is still this store's slot — before it is handed on.
        """
        meta = self.meta()
        if meta is None or meta.get("revoked") is True:
            return None
        directory = self._anchor()
        try:
            info = os.stat(SCENARIO_NAME, dir_fd=directory, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCENARIO_BYTES:
            return None
        return self.root / SCENARIO_NAME

    def status(self) -> dict[str, Any]:
        """What this slot currently is, exactly as it can be re-derived right now.

        An unreadable, replaced, symlinked or closed slot reads as nothing
        registered and nothing trusted, with its refusal named, so a caller can show
        the state instead of meeting an unhandled failure.
        """
        try:
            return self._status()
        except ManifestError as error:
            return {
                "schema": SLOT_SCHEMA,
                "registered": False,
                "trusted": False,
                "revoked": False,
                "scenario_id": LOCAL_SCENARIO_ID,
                "capability": None,
                "grant": None,
                "error": {"code": error.code, "message": error.message},
                "warning": LOCAL_STRATEGY_WARNING,
                "limitations": list(SLOT_LIMITATIONS),
            }

    def _status(self) -> dict[str, Any]:
        meta = self.meta()
        if meta is None:
            return {
                "schema": SLOT_SCHEMA,
                "registered": False,
                "trusted": False,
                "revoked": False,
                "scenario_id": LOCAL_SCENARIO_ID,
                "capability": None,
                "grant": None,
                "warning": LOCAL_STRATEGY_WARNING,
                "limitations": list(SLOT_LIMITATIONS),
            }
        grant = self.grant()
        trusted = grant is not None and grant.trusted is True and meta.get("revoked") is not True
        capability: dict[str, Any] | None = None
        if trusted:
            try:
                current = self.capability()
            except ManifestError:
                current = None
            capability = None if current is None else current.document()
        return {
            "schema": SLOT_SCHEMA,
            "registered": True,
            "trusted": trusted and capability is not None,
            "revoked": meta.get("revoked") is True,
            "scenario_id": meta.get("scenario_id") or LOCAL_SCENARIO_ID,
            "base_scenario_id": meta.get("base_scenario_id"),
            "registered_at": meta.get("registered_at"),
            "capability": capability,
            "grant": None if grant is None else grant.document(),
            "warning": LOCAL_STRATEGY_WARNING,
            "limitations": list(SLOT_LIMITATIONS),
        }

    # -- writes -------------------------------------------------------------

    def register(
        self,
        *,
        manifest: Any,
        payload: Any,
        trusted: Any,
        effects: Any,
        base_scenario_id: Any,
        expected_plugin_id: Any,
        expected_package_version: Any,
        expected_manifest_digest: Any,
        expected_implementation_digest: Any,
        scenario: bytes,
        scenario_id: str,
        registered_at: str,
    ) -> dict[str, Any]:
        """Trust one exact captured identity, after re-measuring every claim.

        The grant is built from the *caller's* expected values, never from the
        manifest, and the inspection below refuses unless those values name the
        bytes actually captured. Nothing is written before every check passes, and
        every write is anchored to the slot directory this store opened itself, so a
        refused or misdirected registration leaves no slot and no stray file behind.
        """
        if trusted is not True:
            raise ManifestError(
                "trust_required",
                "registering a local strategy needs one explicit trusted=true decision",
            )
        if type(base_scenario_id) is not str or not base_scenario_id:
            raise ManifestError(
                "base_scenario_required",
                "registering a local strategy needs the registered scenario it borrows "
                "data, funding, risk and execution from",
            )
        if base_scenario_id == scenario_id:
            raise ManifestError(
                "base_scenario_invalid",
                "a local strategy scenario cannot be its own base scenario",
            )
        granted = LocalStrategyGrantV1(
            trusted=True,
            plugin_id=expected_plugin_id,
            package_version=expected_package_version,
            manifest_digest=expected_manifest_digest,
            implementation_digest=expected_implementation_digest,
            effects=tuple(_effects(effects)),
        )
        captured = _bounded_payload(payload)
        declared = sealed_manifest(manifest)
        current = inspect_local_strategy(declared, captured, grant=granted)
        document = _scenario_document(scenario)
        _require_scenario_matches(document, current)
        if len(scenario) > MAX_SCENARIO_BYTES:
            raise ManifestError("scenario_invalid", "the retained scenario exceeds its bound")
        meta = {
            "schema": SLOT_SCHEMA,
            "registered_at": registered_at,
            "base_scenario_id": base_scenario_id,
            "scenario_id": scenario_id,
            "plugin_id": current.plugin_id,
            "package_version": current.package_version,
            "manifest_digest": current.manifest.artifact_digest,
            "implementation_digest": "sha256:" + current.artifact_sha256,
            "revoked": False,
        }
        self._write(PACKAGE_NAME, captured)
        self._write(MANIFEST_NAME, _canonical(declared.document()))
        self._write(SCENARIO_NAME, scenario)
        self._write(GRANT_NAME, _canonical(granted.document()))
        # Metadata last: a slot is registered only once its evidence is complete.
        self._write(META_NAME, _canonical(meta))
        return self.status()

    def revoke(self) -> dict[str, Any]:
        """Persist the withdrawal of trust; the retained bytes stay auditable."""
        meta = self.meta()
        if meta is None:
            raise ManifestError("slot_empty", "this workspace holds no local strategy slot")
        grant = self.grant()
        document = {
            "schema": SLOT_SCHEMA,
            **{key: value for key, value in meta.items() if key != "schema"},
            "revoked": True,
        }
        self._write(META_NAME, _canonical(document))
        if grant is not None:
            self._write(
                GRANT_NAME,
                _canonical(
                    LocalStrategyGrantV1(
                        trusted=False,
                        plugin_id=grant.plugin_id,
                        package_version=grant.package_version,
                        manifest_digest=grant.manifest_digest,
                        implementation_digest=grant.implementation_digest,
                        effects=grant.effects,
                    ).document()
                ),
            )
        return self.status()


__all__ = [
    "GRANT_NAME",
    "LOCAL_SCENARIO_ID",
    "LOCAL_STRATEGY_ENTRYPOINT",
    "LOCAL_STRATEGY_SCHEMA",
    "MANIFEST_NAME",
    "META_NAME",
    "PACKAGE_NAME",
    "SCENARIO_NAME",
    "SLOT_LIMITATIONS",
    "SLOT_SCHEMA",
    "LocalStrategyStoreV1",
    "sealed_manifest",
]


def bound_local_catalog(
    workspace: object, binding: object, payload: bytes | None = None
) -> ResearchStrategyCatalogV1 | None:
    """Recheck a local binding at each process boundary before loading its code."""
    decoded = None if binding is None else PluginBindingV1.decode(binding)
    strategy = None if decoded is None else decoded.component("strategy")
    local = strategy is not None and strategy.entrypoint == LOCAL_STRATEGY_ENTRYPOINT
    if not local and workspace is None:
        return None
    if not local or type(workspace) is not str or not Path(workspace).is_absolute():
        raise ManifestError(
            "local_strategy_unbound", "a local strategy needs its bound workspace trust slot"
        )
    store = LocalStrategyStoreV1(Path(workspace))
    try:
        assert decoded is not None
        return store.catalog(decoded.document(), payload=payload)
    finally:
        store.close()
