"""Bounded declarative strategy and validator declarations.

A declaration is data. It names one already-authorized host primitive and fixes a
bounded preset for it; it is never imported, evaluated, or granted an effect, and
it cannot add a primitive of its own. Two kinds are supported:

* ``strategy`` — a preset over the built-in closed price rule of
  ``price-rule-entry-v1``. The declaration carries the complete rule document, so
  selecting it means the dispatched scenario's normalized rule must be exactly
  that rule; a scenario that runs any other rule is refused before dispatch.
* ``validator`` — a closed admission-constraint set evaluated against dataset
  evidence. It can only refuse: it never admits, repairs, rewrites or resamples an
  input, and it never treats unknown availability as verified point-in-time
  availability.

Registering a declaration adds one adapter record to this host's closed adapter
table and one manifest bound to the exact built-in plugin id, version and
artifact digest it wraps. No host module is edited per declaration, and nothing
in a declaration can promote itself into a capability the host does not already
provide.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from ea.plugin.catalog import AdapterV1
from ea.plugin.manifest import (
    EFFECT_LOCAL_COMPUTE,
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    PluginDependencyV1,
    PluginManifestV1,
    compute_artifact_digest,
    seal_manifest,
)
from ea.strategy.rules import decode_rule

DECLARATION_SCHEMA = "ea.plugin-declaration.v1"
MAX_DECLARATION_BYTES = 16 * 1024
MAX_TITLE_BYTES = 120
MAX_DECLARATIONS = 32
DECLARED_PACKAGE_VERSION = "1.0.0"

STRATEGY_TARGET_ID = "price-rule-entry-v1"
STRATEGY_TARGET_PLUGIN_ID = "ea.builtin.strategy.price-rule-entry-v1"
VALIDATOR_TARGET_PLUGIN_ID = "ea.builtin.validator.local-scenario-input"

DECLARATION_KINDS: tuple[str, ...] = ("strategy", "validator")

# The closed constraint vocabulary one declarative validator may use. Every entry
# can only make admission stricter, so a declaration never widens what runs.
VALIDATOR_CONSTRAINTS: tuple[str, ...] = (
    "min_record_count",
    "max_observed_gap_count",
    "require_declared_availability",
)
MAX_RECORD_COUNT = 1_000_001
MAX_GAP_COUNT = 1_000_000

_PUBLISHED_ID = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$")
_HOST_PREFIX = "ea."
_DECLARATION_KEYS = frozenset(
    {
        "schema",
        "declaration_id",
        "kind",
        "package_version",
        "title",
        "declared_by",
        "rule",
        "rule_history",
        "constraints",
        "artifact_digest",
    }
)

# Per-kind host facts. A declaration selects one row; it never supplies any of it.
_KIND_FACTS: dict[str, dict[str, Any]] = {
    "strategy": {
        "interface": "ea.strategy-contract",
        "provides": ("strategy.research",),
        "entrypoint": "ea.host.declared.strategy",
        "dependency": STRATEGY_TARGET_PLUGIN_ID,
        "identity_kind": "strategy",
    },
    "validator": {
        "interface": "ea.scenario-validator",
        "provides": ("validator.scenario-input",),
        "entrypoint": "ea.host.declared.validator",
        "dependency": VALIDATOR_TARGET_PLUGIN_ID,
        "identity_kind": "validator",
    },
}


class DeclarationError(ValueError):
    """One declaration cannot be read, verified, or registered."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in items:
        if key in result:
            raise DeclarationError("declaration_field", "a declaration field is repeated")
        result[key] = item
    return result


def _text(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value.encode("utf-8")) <= maximum:
        raise DeclarationError("declaration_field", f"{field} must be bounded text")
    return value


def _exact_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or isinstance(value, bool) or not minimum <= value <= maximum:
        raise DeclarationError(
            "declaration_field", f"{field} must be an integer in {minimum}..{maximum}"
        )
    return value


def _constraints(value: object) -> dict[str, Any]:
    """Read the closed constraint set; every constraint can only reject."""
    if type(value) is not dict or not value:
        raise DeclarationError(
            "declaration_field", "a validator declares at least one admission constraint"
        )
    unknown = sorted(set(value) - set(VALIDATOR_CONSTRAINTS))
    if unknown:
        raise DeclarationError(
            "declaration_field",
            "unsupported validator constraints: " + ", ".join(str(item) for item in unknown),
        )
    out: dict[str, Any] = {}
    for name, item in value.items():
        if name == "min_record_count":
            out[name] = _exact_int(item, field=name, minimum=1, maximum=MAX_RECORD_COUNT)
        elif name == "max_observed_gap_count":
            out[name] = _exact_int(item, field=name, minimum=0, maximum=MAX_GAP_COUNT)
        else:
            if type(item) is not bool:
                raise DeclarationError("declaration_field", f"{name} must be a boolean")
            out[name] = item
    return out


@dataclass(frozen=True, slots=True)
class DeclarationV1:
    """One verified declaration: a bounded preset over one host primitive.

    ``payload`` is the exact canonical bytes of the sealed body that was verified.
    Storing and reloading those bytes — never a re-derived view of them — is what
    makes the digest a statement about what is on disk.
    """

    declaration_id: str
    kind: str
    package_version: str
    title: str
    rule: str | None
    rule_history: int | None
    constraints: dict[str, Any] | None
    artifact_digest: str
    payload: bytes

    @property
    def plugin_id(self) -> str:
        """The declaration's own namespaced capability identifier."""
        return self.declaration_id

    @property
    def entrypoint(self) -> str:
        return f"{_KIND_FACTS[self.kind]['entrypoint']}.{self.declaration_id}"

    def document(self) -> dict[str, Any]:
        """The declaration's own sealed body, exactly as it was submitted."""
        return cast("dict[str, Any]", json.loads(self.payload))


def decode_declaration(payload: object) -> DeclarationV1:
    """Read one bounded declaration; execute nothing it contains."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if type(payload) is not bytes:
        raise DeclarationError("declaration_field", "a declaration must be JSON text or bytes")
    if not 1 <= len(payload) <= MAX_DECLARATION_BYTES:
        raise DeclarationError(
            "declaration_size",
            f"a declaration must be at most {MAX_DECLARATION_BYTES} bytes",
        )
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
    except UnicodeDecodeError:
        raise DeclarationError("declaration_encoding", "a declaration must be UTF-8") from None
    except (ValueError, RecursionError):
        raise DeclarationError("declaration_field", "a declaration must be JSON") from None
    if type(document) is not dict or set(document) != _DECLARATION_KEYS:
        raise DeclarationError(
            "declaration_field",
            "a declaration declares exactly: " + ", ".join(sorted(_DECLARATION_KEYS)),
        )
    if document["schema"] != DECLARATION_SCHEMA:
        raise DeclarationError("declaration_schema", f"schema must be {DECLARATION_SCHEMA}")
    declaration_id = document["declaration_id"]
    if type(declaration_id) is not str or _PUBLISHED_ID.fullmatch(declaration_id) is None:
        raise DeclarationError(
            "declaration_field",
            "declaration_id must be a lowercase namespaced identifier such as user.rule.entry",
        )
    if declaration_id.startswith(_HOST_PREFIX):
        raise DeclarationError(
            "declaration_reserved",
            "the ea. namespace belongs to this host; a declaration cannot shadow a host plugin",
        )
    kind = document["kind"]
    if kind not in DECLARATION_KINDS:
        raise DeclarationError(
            "declaration_kind",
            "a declaration kind must be one of: " + ", ".join(DECLARATION_KINDS),
        )
    if document["package_version"] != DECLARED_PACKAGE_VERSION:
        raise DeclarationError(
            "declaration_field", f"package_version must be {DECLARED_PACKAGE_VERSION}"
        )
    if document["declared_by"] != "user":
        raise DeclarationError("declaration_field", "declared_by must be the user")
    title = _text(document["title"], field="title", maximum=MAX_TITLE_BYTES)
    rule: str | None = None
    rule_history: int | None = None
    constraints: dict[str, Any] | None = None
    if kind == "strategy":
        if document["constraints"] is not None:
            raise DeclarationError(
                "declaration_field", "a strategy declaration does not declare constraints"
            )
        try:
            decoded_rule = decode_rule(document["rule"])
        except ValueError as error:
            raise DeclarationError("declaration_rule", str(error)) from None
        rule = decoded_rule.canonical
        rule_history = decoded_rule.history
        if document["rule_history"] is not None and document["rule_history"] != rule_history:
            raise DeclarationError(
                "declaration_rule", "rule_history does not match the declared rule"
            )
    else:
        if document["rule"] is not None or document["rule_history"] is not None:
            raise DeclarationError(
                "declaration_field", "a validator declaration does not declare a rule"
            )
        constraints = _constraints(document["constraints"])
    digest = compute_artifact_digest(document)
    if document["artifact_digest"] != digest:
        raise DeclarationError(
            "declaration_digest", "artifact_digest does not match the declaration body"
        )
    return DeclarationV1(
        declaration_id=declaration_id,
        kind=kind,
        package_version=DECLARED_PACKAGE_VERSION,
        title=title,
        rule=rule,
        rule_history=rule_history,
        constraints=constraints,
        artifact_digest=digest,
        payload=_canonical_json(document),
    )


def seal_declaration(document: Mapping[str, Any]) -> dict[str, Any]:
    """Fill in the integrity digest of one declaration body."""
    body = dict(document)
    body["artifact_digest"] = compute_artifact_digest(body)
    return body


def rule_digest(rule: str) -> str:
    return "sha256:" + sha256(rule.encode("utf-8")).hexdigest()


def constraints_digest(constraints: Mapping[str, Any]) -> str:
    return "sha256:" + sha256(_canonical_json(dict(constraints))).hexdigest()


def declarative_capabilities(
    declarations: Iterable[DeclarationV1],
    *,
    dependency_digests: Mapping[str, str],
    implementation_digests: Mapping[str, str | None],
    validator_version: str | None = None,
) -> list[tuple[AdapterV1, PluginManifestV1]]:
    """One host adapter plus one sealed manifest per declaration.

    ``dependency_digests``, ``implementation_digests`` and ``validator_version``
    are host facts computed by the built-in layer, so a declaration is bound to
    the exact wrapped implementation identity and never to a substituted or newer
    one.
    """
    built: list[tuple[AdapterV1, PluginManifestV1]] = []
    for declaration in declarations:
        facts = _KIND_FACTS[declaration.kind]
        target = facts["dependency"]
        digest = dependency_digests.get(target)
        if digest is None:
            raise DeclarationError(
                "declaration_target_unavailable",
                f"the wrapped host capability {target} is unavailable in this build",
            )
        identity: dict[str, Any] = {
            "declaration_id": declaration.declaration_id,
            "declaration_sha256": declaration.artifact_digest,
            "declared_by": "user",
            "implementation_sha256": implementation_digests.get(facts["identity_kind"]),
            "wraps": target,
        }
        if declaration.kind == "strategy":
            assert declaration.rule is not None and declaration.rule_history is not None
            identity.update(
                {
                    "strategy_id": STRATEGY_TARGET_ID,
                    "strategy_version": 1,
                    "declared_rule": declaration.rule,
                    "declared_rule_sha256": rule_digest(declaration.rule),
                    "declared_rule_history": declaration.rule_history,
                }
            )
            config: dict[str, Any] = {
                "declaration_id": declaration.declaration_id,
                "title": declaration.title,
                "strategy_id": STRATEGY_TARGET_ID,
                "rule": declaration.rule,
                "rule_history": declaration.rule_history,
            }
        else:
            assert declaration.constraints is not None
            if validator_version is None:
                raise DeclarationError(
                    "declaration_target_unavailable",
                    "the host scenario validator version cannot be named in this build",
                )
            identity.update(
                {
                    "validator_id": "ea.local-web-scenario-validator",
                    "validator_version": validator_version,
                    "validator_constraints": declaration.constraints,
                    "validator_constraints_sha256": constraints_digest(declaration.constraints),
                }
            )
            config = {
                "declaration_id": declaration.declaration_id,
                "title": declaration.title,
                "constraints": declaration.constraints,
            }
        adapter = AdapterV1(
            entrypoint=declaration.entrypoint,
            kind=declaration.kind,
            provides=tuple(facts["provides"]),
            effects=(EFFECT_LOCAL_COMPUTE,),
            identity=identity,
        )
        manifest = seal_manifest(
            PluginManifestV1(
                schema_version=MANIFEST_SCHEMA_VERSION,
                plugin_id=declaration.plugin_id,
                kind=declaration.kind,
                package_version=declaration.package_version,
                artifact_digest="",
                interface=InterfaceRangeV1(facts["interface"], 1, 0),
                entrypoint=declaration.entrypoint,
                provides=tuple(facts["provides"]),
                requires=(),
                dependencies=(PluginDependencyV1(target, DECLARED_PACKAGE_VERSION, digest),),
                effects=(EFFECT_LOCAL_COMPUTE,),
                runtime_profile=RUNTIME_PROFILE,
                config_schema=config,
                author="user",
                license="",
            )
        )
        built.append((adapter, manifest))
    return built


# -- enforcement ------------------------------------------------------------


def declared_strategy_rejection(
    identity: Mapping[str, Any], parameters: Mapping[str, Any]
) -> str | None:
    """Refuse a dispatch whose scenario does not run the declared rule."""
    declared = identity.get("declared_rule_sha256")
    if declared is None:
        return None
    if parameters.get("rule") is None:
        return (
            "the selected declarative strategy fixes the price rule, so the spec must set "
            "parameters.rule to exactly the rule it declares"
        )
    try:
        canonical = decode_rule(parameters.get("rule")).canonical
    except (ValueError, TypeError):
        return "the scenario's price rule could not be read against the declared rule"
    if rule_digest(canonical) != declared:
        return (
            "the registered scenario runs a different price rule than the selected "
            "declarative capability declares"
        )
    return None


def declared_validator_rejection(
    identity: Mapping[str, Any], entry: Mapping[str, Any] | None
) -> str | None:
    """Refuse a dispatch whose input evidence fails the declared constraints.

    The constraints were frozen into the capability identity, so this reads the
    binding itself and needs no store lookup. Every branch can only refuse.
    """
    constraints = identity.get("validator_constraints")
    if constraints is None:
        return None
    if type(constraints) is not dict or entry is None:
        return "the selected declarative validator needs an explicit dataset_id it can verify"
    if type(entry) is not dict:
        return "the selected declarative validator cannot read this input's evidence"
    minimum = constraints.get("min_record_count")
    if minimum is not None:
        count = entry.get("record_count")
        if type(count) is not int or isinstance(count, bool) or count < minimum:
            return f"the input has fewer than the declared {minimum} records"
    maximum_gaps = constraints.get("max_observed_gap_count")
    if maximum_gaps is not None:
        gaps = entry.get("observed_gap_count")
        if type(gaps) is not int or isinstance(gaps, bool) or gaps > maximum_gaps:
            return f"the input has more than the declared {maximum_gaps} observed gaps"
    if constraints.get("require_declared_availability"):
        policy = entry.get("availability_policy")
        if type(policy) is not dict or type(policy.get("id")) is not str:
            return (
                "the input declares no availability policy; unknown availability is never "
                "treated as verified point-in-time availability"
            )
        if policy.get("pit_verified"):
            return "the input claims verified point-in-time availability, which this host refuses"
    return None


DECLARATION_ROOT_NAME = "plugin-declarations"


def _open_declaration_root(workspace: Path) -> int:
    """Create and open the declaration root, before anything is written to it.

    The workspace and the root are both checked without following links, the root
    is created relative to the already-verified workspace descriptor, and it is
    opened with ``O_NOFOLLOW``. A symlinked or replaced workspace or root is
    refused here, so no later read, write or removal can be redirected outside the
    workspace. This is path anchoring, not an operating-system sandbox.
    """
    if workspace.is_symlink():
        raise DeclarationError("declaration_unavailable", "the workspace is not a real directory")
    try:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise DeclarationError(
            "declaration_unavailable", "the workspace could not be created"
        ) from None
    if workspace.is_symlink() or not workspace.is_dir():
        raise DeclarationError("declaration_unavailable", "the workspace is not a real directory")
    try:
        parent = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise DeclarationError(
            "declaration_unavailable", "the workspace could not be secured"
        ) from None
    try:
        try:
            os.mkdir(DECLARATION_ROOT_NAME, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            raise DeclarationError(
                "declaration_unavailable", "the declaration root could not be created"
            ) from None
        try:
            info = os.stat(DECLARATION_ROOT_NAME, dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise DeclarationError(
                "declaration_unavailable", "the declaration root could not be read"
            ) from None
        if not stat.S_ISDIR(info.st_mode):
            raise DeclarationError(
                "declaration_unavailable", "the declaration root must be a real directory"
            )
        try:
            return os.open(
                DECLARATION_ROOT_NAME,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except OSError:
            raise DeclarationError(
                "declaration_unavailable", "the declaration root could not be secured"
            ) from None
    finally:
        os.close(parent)


def _read_entry(directory: int, name: str, limit: int) -> bytes | None:
    """Read one host-named regular file through a nofollow descriptor, or nothing."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        remaining = info.st_size
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


class DeclarationStoreV1:
    """One workspace-local store of user-registered declarations.

    Registration is explicit and add-only: an existing declaration id is never
    overwritten, so a registered capability can never be silently replaced by a
    different body under the same identity. Every read, write and removal goes
    through one anchored, nofollow descriptor for the declaration root.
    """

    def __init__(self, workspace: Path) -> None:
        self.root = workspace / DECLARATION_ROOT_NAME
        self._fd: int | None = None
        try:
            self._fd = _open_declaration_root(workspace)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the anchored descriptor; no stored declaration is touched."""
        descriptor, self._fd = self._fd, None
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)

    def __del__(self) -> None:
        with suppress(BaseException):
            self.close()

    def path(self, declaration_id: str) -> Path:
        return self.root / f"{declaration_id}.json"

    def has(self, declaration_id: object) -> bool:
        if type(declaration_id) is not str or self._fd is None:
            return False
        try:
            info = os.stat(f"{declaration_id}.json", dir_fd=self._fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def load(self) -> tuple[DeclarationV1, ...]:
        """Every registered declaration, re-read from its own stored bytes."""
        if self._fd is None:
            raise DeclarationError("declaration_unavailable", "the declaration root is not open")
        try:
            entries = sorted(os.listdir(self._fd))
        except OSError:
            raise DeclarationError(
                "declaration_unavailable", "the declaration root could not be read"
            ) from None
        declarations: list[DeclarationV1] = []
        for name in entries:
            if not name.endswith(".json"):
                continue
            payload = _read_entry(self._fd, name, MAX_DECLARATION_BYTES)
            if payload is None:
                raise DeclarationError(
                    "declaration_unreadable", f"stored declaration {name} is unreadable"
                )
            try:
                declaration = decode_declaration(payload)
            except DeclarationError as error:
                raise DeclarationError(
                    "declaration_unreadable",
                    f"stored declaration {name} is not valid: {error.message}",
                ) from None
            if name != f"{declaration.declaration_id}.json":
                raise DeclarationError(
                    "declaration_unreadable", f"stored declaration {name} is misnamed"
                )
            declarations.append(declaration)
        return tuple(declarations)

    def store(self, declaration: DeclarationV1) -> dict[str, Any]:
        """Persist one verified declaration; an existing identity is refused."""
        if self._fd is None:
            raise DeclarationError("declaration_unavailable", "the declaration root is not open")
        if len(self.load()) >= MAX_DECLARATIONS:
            raise DeclarationError(
                "declaration_quota",
                f"the workspace holds the {MAX_DECLARATIONS}-declaration limit",
            )
        name = f"{declaration.declaration_id}.json"
        try:
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._fd,
            )
        except FileExistsError:
            raise DeclarationError(
                "declaration_exists",
                f"declaration {declaration.declaration_id} is already registered and is never "
                "overwritten",
            ) from None
        except OSError:
            raise DeclarationError(
                "declaration_unavailable", "the declaration could not be stored"
            ) from None
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(declaration.payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(self._fd)
        except OSError:
            with suppress(OSError):
                os.unlink(name, dir_fd=self._fd)
            raise DeclarationError(
                "declaration_unavailable", "the declaration could not be stored"
            ) from None
        return declaration.document()

    def discard(self, declaration_id: str) -> None:
        """Remove one declaration that failed verification before it took effect."""
        if self._fd is None:
            return
        with suppress(OSError):
            os.unlink(f"{declaration_id}.json", dir_fd=self._fd)
