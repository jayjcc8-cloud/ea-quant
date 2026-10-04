"""Bounded declarative strategy and validator declarations.

A declaration is data: it names one host primitive and fixes a bounded preset for
it. It is never imported or evaluated, and it can only make a run stricter.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ea.plugin.builtins import builtin_catalog
from ea.plugin.declarative import (
    DECLARATION_SCHEMA,
    MAX_DECLARATION_BYTES,
    STRATEGY_TARGET_PLUGIN_ID,
    VALIDATOR_TARGET_PLUGIN_ID,
    DeclarationError,
    DeclarationStoreV1,
    DeclarationV1,
    constraints_digest,
    declarative_capabilities,
    declared_strategy_rejection,
    declared_validator_rejection,
    decode_declaration,
    rule_digest,
    seal_declaration,
)
from ea.plugin.manifest import ManifestError
from ea.strategy.rules import DEFAULT_RULE

OTHER_RULE = (
    '{"version":1,"when":{"op":"lt","left":{"kind":"price","field":"close"},'
    '"right":{"kind":"sma","window":3}},"action":"ENTER_LONG"}'
)


def _strategy_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "declaration_id": "user.rule.entry",
        "kind": "strategy",
        "package_version": "1.0.0",
        "title": "User entry rule",
        "declared_by": "user",
        "rule": DEFAULT_RULE,
        "rule_history": None,
        "constraints": None,
    }
    body.update(overrides)
    return seal_declaration(body)


def _validator_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "declaration_id": "user.validator.declared-availability",
        "kind": "validator",
        "package_version": "1.0.0",
        "title": "Require a declared availability policy",
        "declared_by": "user",
        "rule": None,
        "rule_history": None,
        "constraints": {"require_declared_availability": True, "min_record_count": 2},
    }
    body.update(overrides)
    return seal_declaration(body)


def _bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True).encode("utf-8")


def _catalog(declarations: tuple[DeclarationV1, ...]) -> Any:
    class _Service:
        datasets = None
        dataset_catalog = None

    return builtin_catalog(_Service(), None, declarations)


# -- decoding ---------------------------------------------------------------


def test_a_strategy_declaration_is_sealed_and_canonicalized() -> None:
    declaration = decode_declaration(_bytes(_strategy_body()))
    assert declaration.kind == "strategy"
    assert declaration.plugin_id == "user.rule.entry"
    assert declaration.entrypoint == "ea.host.declared.strategy.user.rule.entry"
    # The rule is stored in the closed engine's own canonical form.
    assert declaration.rule == decode_declaration(_bytes(_strategy_body())).rule
    assert declaration.rule_history == 2
    assert declaration.artifact_digest.startswith("sha256:")
    assert declaration.document()["schema"] == DECLARATION_SCHEMA


def test_declaration_shape_is_closed() -> None:
    body = _strategy_body()
    body["extra"] = 1
    with pytest.raises(DeclarationError) as unknown:
        decode_declaration(_bytes(seal_declaration(body)))
    assert unknown.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as version:
        decode_declaration(_bytes(_strategy_body(package_version="1.0.1")))
    assert version.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as kind:
        decode_declaration(_bytes(_strategy_body(kind="tool")))
    assert kind.value.code == "declaration_kind"
    with pytest.raises(DeclarationError) as reserved:
        decode_declaration(_bytes(_strategy_body(declaration_id="ea.builtin.strategy.shadow")))
    assert reserved.value.code == "declaration_reserved"
    with pytest.raises(DeclarationError) as unnamespaced:
        decode_declaration(_bytes(_strategy_body(declaration_id="entry")))
    assert unnamespaced.value.code == "declaration_field"


def test_a_tampered_body_is_refused() -> None:
    body = _strategy_body()
    body["rule"] = OTHER_RULE
    with pytest.raises(DeclarationError) as tampered:
        # The digest was sealed over the original rule, so the body no longer matches.
        decode_declaration(_bytes(body))
    assert tampered.value.code == "declaration_digest"


def test_declaration_size_is_bounded() -> None:
    with pytest.raises(DeclarationError) as size:
        decode_declaration(b"x" * (MAX_DECLARATION_BYTES + 1))
    assert size.value.code == "declaration_size"
    with pytest.raises(DeclarationError) as not_json:
        decode_declaration(b"{")
    assert not_json.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as not_utf8:
        decode_declaration(b"\xff\xfe")
    assert not_utf8.value.code == "declaration_encoding"


def test_a_rule_is_validated_by_the_closed_engine() -> None:
    with pytest.raises(DeclarationError) as illegal:
        decode_declaration(
            _bytes(_strategy_body(rule='{"version":1,"when":{"op":"exec"},"action":"ENTER_LONG"}'))
        )
    assert illegal.value.code == "declaration_rule"
    with pytest.raises(DeclarationError) as python:
        decode_declaration(_bytes(_strategy_body(rule="__import__('os').system('id')")))
    assert python.value.code == "declaration_rule"
    with pytest.raises(DeclarationError) as wrong_history:
        decode_declaration(_bytes(_strategy_body(rule_history=7)))
    assert wrong_history.value.code == "declaration_rule"


def test_validator_constraints_are_closed_and_bounded() -> None:
    with pytest.raises(DeclarationError) as empty:
        decode_declaration(_bytes(_validator_body(constraints={})))
    assert empty.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as unknown:
        decode_declaration(_bytes(_validator_body(constraints={"allow_everything": True})))
    assert unknown.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as wrong_type:
        decode_declaration(_bytes(_validator_body(constraints={"min_record_count": "2"})))
    assert wrong_type.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as too_wide:
        decode_declaration(_bytes(_validator_body(constraints={"min_record_count": 0})))
    assert too_wide.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as not_boolean:
        decode_declaration(
            _bytes(_validator_body(constraints={"require_declared_availability": 1}))
        )
    assert not_boolean.value.code == "declaration_field"
    with pytest.raises(DeclarationError) as stray_rule:
        decode_declaration(_bytes(_validator_body(rule=DEFAULT_RULE)))
    assert "does not declare a rule" in stray_rule.value.message


# -- capabilities -----------------------------------------------------------


def test_declarations_become_host_verified_capabilities() -> None:
    declaration = decode_declaration(_bytes(_strategy_body()))
    catalog = _catalog((declaration,))
    entry = catalog.get("user.rule.entry", "1.0.0")
    identity = entry.identity()
    assert identity["strategy_id"] == "price-rule-entry-v1"
    assert identity["strategy_version"] == 1
    assert identity["declared_rule_sha256"] == rule_digest(declaration.rule or "")
    assert identity["declaration_sha256"] == declaration.artifact_digest
    assert identity["wraps"] == STRATEGY_TARGET_PLUGIN_ID
    implementation = identity["implementation_sha256"]
    assert implementation and implementation.startswith("sha256:")
    # The declaration's own view of itself is preserved for inspection.
    document = entry.document()
    assert document["manifest"]["dependencies"][0]["plugin_id"] == STRATEGY_TARGET_PLUGIN_ID
    assert document["manifest"]["dependencies"][0]["artifact_digest"].startswith("sha256:")
    assert document["effects"]["granted"] == ["local_compute"]
    assert document["effects"]["host_granted"] == ["local_compute", "read_network", "write_local"]


def test_a_dictated_validator_keeps_its_constraints_in_its_identity() -> None:
    declaration = decode_declaration(_bytes(_validator_body()))
    entry = _catalog((declaration,)).get("user.validator.declared-availability", "1.0.0")
    identity = entry.identity()
    assert identity["validator_id"] == "ea.local-web-scenario-validator"
    assert identity["validator_version"]
    assert identity["validator_constraints"] == declaration.constraints
    constraints = declaration.constraints or {}
    assert identity["validator_constraints_sha256"] == constraints_digest(constraints)
    assert identity["wraps"] == VALIDATOR_TARGET_PLUGIN_ID


def test_a_declaration_cannot_be_bound_when_its_target_is_absent() -> None:
    declaration = decode_declaration(_bytes(_strategy_body()))
    with pytest.raises(DeclarationError) as unavailable:
        # The host cannot name the wrapped artifact, so the declaration is not
        # bound to a guessed or newer implementation.
        declarative_capabilities(
            (declaration,),
            dependency_digests={},
            implementation_digests={"strategy": "sha256:" + "0" * 64},
        )
    assert unavailable.value.code == "declaration_target_unavailable"
    with pytest.raises(DeclarationError) as unnamed_validator:
        declarative_capabilities(
            (decode_declaration(_bytes(_validator_body())),),
            dependency_digests={VALIDATOR_TARGET_PLUGIN_ID: "sha256:" + "0" * 64},
            implementation_digests={"validator": "sha256:" + "0" * 64},
        )
    assert unnamed_validator.value.code == "declaration_target_unavailable"


def test_a_declaration_cannot_shadow_a_builtin_identity() -> None:
    declaration = decode_declaration(_bytes(_strategy_body()))

    class _Service:
        datasets = None
        dataset_catalog = None

    with pytest.raises(ManifestError) as duplicate:
        # The same declaration twice in one catalog is a duplicate identity, not
        # a replacement for the first one.
        builtin_catalog(_Service(), None, (declaration, declaration))
    assert duplicate.value.code in {"adapter_duplicate", "manifest_duplicate"}
    # And the host namespace itself is not addressable by a declaration.
    with pytest.raises(DeclarationError):
        decode_declaration(_bytes(_strategy_body(declaration_id="ea.builtin.data.x")))


# -- enforcement ------------------------------------------------------------


def test_a_declared_rule_must_be_the_dispatched_rule() -> None:
    declared = decode_declaration(_bytes(_strategy_body()))
    identity = {"declared_rule_sha256": rule_digest(declared.rule or "")}
    assert declared_strategy_rejection(identity, {"rule": DEFAULT_RULE}) is None
    assert declared_strategy_rejection(identity, {}) is not None
    other = declared_strategy_rejection(identity, {"rule": OTHER_RULE})
    assert other is not None and "different price rule" in other
    assert declared_strategy_rejection({}, {"rule": OTHER_RULE}) is None


def test_a_declared_validator_can_only_refuse() -> None:
    constraints = {"require_declared_availability": True, "min_record_count": 4}
    identity = {"validator_constraints": constraints}
    declared = {"record_count": 8, "observed_gap_count": 0, "availability_policy": {"id": "x"}}
    assert declared_validator_rejection(identity, declared) is None
    # Unknown availability is never treated as verified.
    unknown = {"record_count": 8, "observed_gap_count": 0}
    assert "never" in (declared_validator_rejection(identity, unknown) or "")
    assert declared_validator_rejection(identity, None) is not None
    assert declared_validator_rejection(identity, {**declared, "record_count": 3}) is not None
    gaps = {"validator_constraints": {"max_observed_gap_count": 1}}
    assert declared_validator_rejection(gaps, {"observed_gap_count": 5}) is not None
    assert declared_validator_rejection(gaps, {"observed_gap_count": 1}) is None
    assert declared_validator_rejection(gaps, {}) is not None
    claim = {"validator_constraints": {"require_declared_availability": True}}
    assert (
        declared_validator_rejection(
            claim, {**declared, "availability_policy": {"id": "x", "pit_verified": True}}
        )
        is not None
    )
    # A capability that declares nothing constrains nothing.
    assert declared_validator_rejection({}, {}) is None


# -- store ------------------------------------------------------------------


def test_registration_is_add_only_and_survives_reconstruction(tmp_path: Any) -> None:
    store = DeclarationStoreV1(tmp_path / "workspace")
    assert store.load() == ()
    declaration = decode_declaration(_bytes(_strategy_body()))
    document = store.store(declaration)
    assert document["declaration_id"] == "user.rule.entry"
    assert store.has("user.rule.entry") is True
    again = DeclarationStoreV1(tmp_path / "workspace").load()
    assert [item.declaration_id for item in again] == ["user.rule.entry"]
    assert again[0].rule == declaration.rule
    assert again[0].artifact_digest == declaration.artifact_digest
    with pytest.raises(DeclarationError) as exists:
        store.store(declaration)
    assert exists.value.code == "declaration_exists"
    # What is on disk is the exact sealed body that was verified: reloading it
    # recomputes the same digest rather than a view derived after the fact.
    stored = store.path("user.rule.entry").read_bytes()
    assert stored == declaration.payload
    assert json.loads(stored) == declaration.document()
    assert json.loads(stored)["artifact_digest"] == declaration.artifact_digest


def test_a_stored_declaration_edited_out_of_band_is_refused(tmp_path: Any) -> None:
    """The digest is about the stored bytes, not about a body re-derived on load."""
    store = DeclarationStoreV1(tmp_path / "workspace")
    declaration = decode_declaration(_bytes(_strategy_body()))
    store.store(declaration)
    edited = json.loads(store.path("user.rule.entry").read_bytes())
    edited["title"] = "Edited after registration"
    store.path("user.rule.entry").write_bytes(_bytes(edited))
    # The stored digest is untouched by that edit, so it is what disagrees.
    assert edited["artifact_digest"] == declaration.artifact_digest
    with pytest.raises(DeclarationError) as unreadable:
        DeclarationStoreV1(tmp_path / "workspace").load()
    assert unreadable.value.code == "declaration_unreadable"


def test_a_different_declaration_cannot_take_a_registered_identity(tmp_path: Any) -> None:
    """A second, validly sealed body never replaces the registered one.

    A digest proves only that one body is intact; it cannot prove which body was
    registered. What protects the identity is that registration is add-only and
    that a capability's own view names the digest it wraps, so a binding frozen
    against the first body no longer re-derives from the second.
    """
    store = DeclarationStoreV1(tmp_path / "workspace")
    registered = decode_declaration(_bytes(_strategy_body()))
    store.store(registered)
    replacement = decode_declaration(_bytes(_strategy_body(rule=OTHER_RULE)))
    assert replacement.artifact_digest != registered.artifact_digest
    assert replacement.rule != registered.rule
    with pytest.raises(DeclarationError) as exists:
        store.store(replacement)
    assert exists.value.code == "declaration_exists"
    assert store.load()[0].artifact_digest == registered.artifact_digest
    first = _catalog((registered,)).get("user.rule.entry", "1.0.0").identity()
    second = _catalog((replacement,)).get("user.rule.entry", "1.0.0").identity()
    assert first["declaration_sha256"] != second["declaration_sha256"]
    assert first["declared_rule_sha256"] != second["declared_rule_sha256"]


def test_a_symlinked_declaration_root_is_refused(tmp_path: Any) -> None:
    """Nothing outside the workspace is read, and nothing in it is removed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.json"
    keep.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "plugin-declarations").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DeclarationError) as untrusted:
        DeclarationStoreV1(workspace)
    assert untrusted.value.code == "declaration_unavailable"
    assert keep.exists()


def test_declaration_storage_is_bounded(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("ea.plugin.declarative.MAX_DECLARATIONS", 1)
    store = DeclarationStoreV1(tmp_path / "workspace")
    store.store(decode_declaration(_bytes(_strategy_body())))
    with pytest.raises(DeclarationError) as quota:
        store.store(decode_declaration(_bytes(_validator_body())))
    assert quota.value.code == "declaration_quota"
    assert [item.declaration_id for item in store.load()] == ["user.rule.entry"]


def test_a_declaration_cannot_name_a_host_file_or_permission() -> None:
    """Only bounded text, a closed rule and closed constraints are ever read."""
    for bad in (
        _strategy_body(declaration_id="user.rule.entry2", title="x" * 500),
        _strategy_body(declaration_id="user.rule.entry3", declared_by="model"),
    ):
        with pytest.raises(DeclarationError):
            decode_declaration(_bytes(bad))
    body = _strategy_body()
    body["entrypoint"] = "/bin/sh"
    with pytest.raises(DeclarationError):
        decode_declaration(_bytes(seal_declaration(body)))
