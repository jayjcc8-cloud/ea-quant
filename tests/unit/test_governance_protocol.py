from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTER_PATH = PROJECT_ROOT / ".governance" / "router.yaml"
PROMPT_ROOT = PROJECT_ROOT / ".governance" / "prompts"
REPORT_SCHEMA_PATH = PROJECT_ROOT / ".governance" / "schemas" / "report.schema.json"
CONTEXT_SCHEMA_PATH = PROJECT_ROOT / ".governance" / "schemas" / "context-manifest.schema.json"
APPROVAL_BODY_PATH = PROJECT_ROOT / ".agents" / "approval-owner.md"


def _prompt_frontmatter() -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    for path in sorted(PROMPT_ROOT.glob("*.md")):
        parts = path.read_text(encoding="utf-8").split("---", 2)
        assert len(parts) == 3, f"missing YAML frontmatter: {path}"
        metadata = yaml.safe_load(parts[1])
        assert isinstance(metadata, dict), f"invalid YAML frontmatter: {path}"
        prompt_id = metadata.get("prompt_id")
        assert isinstance(prompt_id, str), f"missing prompt_id: {path}"
        assert prompt_id not in registry, f"duplicate prompt_id: {prompt_id}"
        registry[prompt_id] = metadata
    return registry


def _route_value(router: dict[str, Any], *keys: str) -> Any:
    value: Any = router
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def test_required_routes_bind_governed_role_contracts() -> None:
    router = yaml.safe_load(ROUTER_PATH.read_text(encoding="utf-8"))
    assert isinstance(router, dict)
    registry = _prompt_frontmatter()
    expected = {
        ("model_routes", "tier0", "approval", "prompt_id"): (
            "deterministic-gate-v1",
            "deterministic_gate",
        ),
        ("model_routes", "tier1", "architecture", "prompt_id"): (
            "architecture-v1",
            "architecture_owner",
        ),
        ("model_routes", "tier1", "verification", "prompt_id"): (
            "terra-verification-v1",
            "verification_owner",
        ),
    }

    resolved: dict[str, tuple[str | None, str | None]] = {}
    for route in expected:
        prompt_id = _route_value(router, *route)
        metadata = registry.get(prompt_id, {})
        required_output = metadata.get("required_output", {})
        resolved[".".join(route)] = (
            prompt_id,
            required_output.get("role") if isinstance(required_output, dict) else None,
        )

    assert resolved == {
        ".".join(route): expected_contract for route, expected_contract in expected.items()
    }


def test_every_router_prompt_id_resolves_to_matching_registry_entry() -> None:
    router = yaml.safe_load(ROUTER_PATH.read_text(encoding="utf-8"))
    assert isinstance(router, dict)
    registry = _prompt_frontmatter()

    prompt_ids: set[str] = set()
    pending: list[Any] = [router]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "prompt_id":
                    assert isinstance(item, str)
                    prompt_ids.add(item)
                else:
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)

    assert prompt_ids
    assert prompt_ids <= registry.keys()
    assert all(registry[prompt_id]["prompt_id"] == prompt_id for prompt_id in prompt_ids)


def test_route_effort_matches_governed_contract() -> None:
    router = yaml.safe_load(ROUTER_PATH.read_text(encoding="utf-8"))
    assert isinstance(router, dict)
    registry = _prompt_frontmatter()

    route_names = {
        "tier0": ("implementation", "verification", "approval"),
        "tier1": ("implementation", "architecture", "verification", "approval"),
        "tier2": ("decision", "implementation", "adversarial", "verification", "approval"),
    }
    mismatches: dict[str, tuple[str | None, str | None]] = {}
    for tier, names in route_names.items():
        for name in names:
            route = _route_value(router, "model_routes", tier, name)
            assert isinstance(route, dict)
            prompt_id = route.get("prompt_id")
            assert isinstance(prompt_id, str)
            metadata = registry[prompt_id]
            effort_by_tier = metadata.get("required_effort_by_tier")
            if isinstance(effort_by_tier, dict):
                required_effort = effort_by_tier.get(tier)
            else:
                required_effort = metadata.get("required_effort")
            if route.get("effort") != required_effort:
                mismatches[f"{tier}.{name}"] = (route.get("effort"), required_effort)

    assert mismatches == {}


def test_approval_owner_contract_is_representable_by_report_schema() -> None:
    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    required_fields = {"gate", "decision", "expected_transitions"}
    assert required_fields <= schema["properties"].keys()

    approval_contract = next(
        clause
        for clause in schema["allOf"]
        if clause.get("if", {}).get("properties", {}).get("role", {}).get("const")
        == "approval_owner"
    )
    assert required_fields <= set(approval_contract["then"]["required"])

    approval_body = APPROVAL_BODY_PATH.read_text(encoding="utf-8")
    assert all(f"{field}:" in approval_body for field in required_fields)
    registry = _prompt_frontmatter()["approval-v1"]["required_output"]
    assert registry == {
        "schema": ".governance/schemas/report.schema.json",
        "role": "approval_owner",
        "verdicts": ["APPROVE", "HOLD"],
    }


def test_tier0_deterministic_gate_has_a_schema_valid_context_role() -> None:
    context_schema = json.loads(CONTEXT_SCHEMA_PATH.read_text(encoding="utf-8"))
    report_schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    router = yaml.safe_load(ROUTER_PATH.read_text(encoding="utf-8"))
    assert isinstance(router, dict)

    assert "deterministic_gate" in context_schema["properties"]["role"]["enum"]
    assert "deterministic_gate" in report_schema["properties"]["role"]["enum"]
    assert router["model_routes"]["tier0"]["verification"]["prompt_id"] == ("deterministic-gate-v1")
    assert router["model_routes"]["tier0"]["approval"]["prompt_id"] == ("deterministic-gate-v1")
