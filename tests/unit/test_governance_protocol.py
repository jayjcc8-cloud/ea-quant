from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTER_PATH = PROJECT_ROOT / ".governance" / "router.yaml"
PROMPT_ROOT = PROJECT_ROOT / ".governance" / "prompts"
REPORT_SCHEMA_PATH = PROJECT_ROOT / ".governance" / "schemas" / "report.schema.json"
CONTEXT_SCHEMA_PATH = PROJECT_ROOT / ".governance" / "schemas" / "context-manifest.schema.json"
APPROVAL_BODY_PATH = PROJECT_ROOT / ".agents" / "approval-owner.md"
WORKFLOW_PATH = PROJECT_ROOT / "docs" / "governance" / "WORKFLOW.md"
CONTRIBUTING_PATH = PROJECT_ROOT / "CONTRIBUTING.md"
READY_ADR_PATH = (
    PROJECT_ROOT / "docs" / "adr" / "0026-pre-implementation-ready-and-candidate-merge-approval.md"
)
DETERMINISTIC_GATE_PATH = PROMPT_ROOT / "deterministic-gate-v1.md"


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


def _representative_deterministic_report(
    *,
    gate: str,
    verdict: str,
    authorized_mutation: str,
    reviewed_sha: str,
    evidence_fact: str,
) -> dict[str, Any]:
    return {
        "protocol_version": "1.0",
        "report_id": f"REPORT-ISSUE-108-DETERMINISTIC-{gate.upper()}-{verdict}",
        "role": "deterministic_gate",
        "actor_id": "issue108-deterministic-gate",
        "work_unit_id": "issue108-deterministic-gate",
        "base_sha": "0" * 40,
        "reviewed_sha": reviewed_sha,
        "scope": ["Issue #108 deterministic gate regression representation"],
        "evidence": [
            {
                "source": ".governance/prompts/deterministic-gate-v1.md",
                "sha256": "1" * 64,
                "fact": evidence_fact,
            }
        ],
        "findings": [],
        "verdict": verdict,
        "gate": gate,
        "decision": verdict,
        "expected_transitions": [f"{gate} report produces {authorized_mutation}"],
        "authorized_mutation": authorized_mutation,
        "verdict_reason": evidence_fact,
        "provenance": {
            "model_id": "deterministic-tools",
            "model_version": "unavailable",
            "reasoning_effort": "deterministic",
            "codex_version": "unavailable",
            "prompt_id": "deterministic-gate-v1",
            "prompt_version": "1.2.0",
            "prompt_sha256": "2" * 64,
            "context_payload_sha256": "3" * 64,
            "router_rules_sha256": "4" * 64,
            "skills": [],
        },
    }


def _assert_matches_deterministic_report_schema(report: dict[str, Any]) -> None:
    """Validate every unchanged schema constraint applicable to deterministic_gate reports."""
    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= report.keys()
    assert set(report) <= properties.keys()
    assert report["protocol_version"] == properties["protocol_version"]["const"]
    assert report["role"] == "deterministic_gate"
    assert report["role"] in properties["role"]["enum"]
    assert re.fullmatch(r"REPORT-[A-Z0-9._-]+", report["report_id"])
    assert re.fullmatch(r"[0-9a-f]{40}", report["base_sha"])
    assert report["reviewed_sha"] == "WORKING_TREE" or re.fullmatch(
        r"[0-9a-f]{40}", report["reviewed_sha"]
    )
    assert all(isinstance(value, str) and value for value in report["scope"])
    for evidence in report["evidence"]:
        assert set(evidence) <= properties["evidence"]["items"]["properties"].keys()
        assert {"source", "fact"} <= evidence.keys()
        assert isinstance(evidence["source"], str) and evidence["source"]
        assert isinstance(evidence["fact"], str) and evidence["fact"]
        assert evidence["sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])
    assert isinstance(report["findings"], list)
    assert report["verdict"] in properties["verdict"]["enum"]
    assert report["gate"] in properties["gate"]["enum"]
    assert report["decision"] in properties["decision"]["enum"]
    assert report["expected_transitions"]
    assert report["authorized_mutation"] in properties["authorized_mutation"]["enum"]
    provenance = report["provenance"]
    provenance_properties = properties["provenance"]["properties"]
    assert set(properties["provenance"]["required"]) <= provenance.keys()
    assert set(provenance) <= provenance_properties.keys()
    assert provenance["reasoning_effort"] in provenance_properties["reasoning_effort"]["enum"]
    for field in ("prompt_sha256", "context_payload_sha256", "router_rules_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", provenance[field])
    assert isinstance(provenance["skills"], list)


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


def test_ready_gate_regression_matrix_preserves_candidate_less_task_package_authority() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    approval_body = APPROVAL_BODY_PATH.read_text(encoding="utf-8")
    contributing = CONTRIBUTING_PATH.read_text(encoding="utf-8")
    ready_adr = READY_ADR_PATH.read_text(encoding="utf-8")
    deterministic_gate = DETERMINISTIC_GATE_PATH.read_text(encoding="utf-8")

    ready_contract = (
        "complete Draft task package",
        "Architecture review is PASS",
        "budget and any exception are approved",
        "does not require a PR, candidate SHA, implementation diff, writer lease, "
        "exact-head tests, or hosted CI",
        "authorizes only `draft_to_ready`",
    )
    for source in (workflow, approval_body, ready_adr, deterministic_gate):
        source = " ".join(source.split()).lower()
        assert all(clause.lower() in source for clause in ready_contract)

    assert "Obtain the applicable task-package Ready decision" in contributing
    assert contributing.index("task-package Ready decision") < contributing.index("writer lease")


def test_ready_gate_holds_incomplete_acceptance_or_unapproved_budget() -> None:
    expected_holds = (
        "incomplete acceptance criteria produces `HOLD`",
        "unapproved budget produces `HOLD`",
    )
    for source in (
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        APPROVAL_BODY_PATH.read_text(encoding="utf-8"),
        READY_ADR_PATH.read_text(encoding="utf-8"),
        DETERMINISTIC_GATE_PATH.read_text(encoding="utf-8"),
    ):
        source = " ".join(source.split()).lower()
        assert all(clause.lower() in source for clause in expected_holds)


def test_merge_gate_retains_frozen_candidate_and_ci_evidence() -> None:
    expected_merge_contract = (
        "PR and exact candidate SHA",
        "valid writer-lease history and complete scoped diff",
        "hosted CI SUCCESS at exact candidate HEAD",
        "missing CI or an unfrozen candidate produces `HOLD`",
        "authorizes only `squash_merge`",
    )
    for source in (
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        APPROVAL_BODY_PATH.read_text(encoding="utf-8"),
        READY_ADR_PATH.read_text(encoding="utf-8"),
        DETERMINISTIC_GATE_PATH.read_text(encoding="utf-8"),
    ):
        source = " ".join(source.split()).lower()
        assert all(clause.lower() in source for clause in expected_merge_contract)


def test_ready_authority_sources_agree_without_schema_or_router_change() -> None:
    lifecycle = "Draft → Ready → In Progress → Review → Verified → Done"
    sources = (
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        APPROVAL_BODY_PATH.read_text(encoding="utf-8"),
        READY_ADR_PATH.read_text(encoding="utf-8"),
        DETERMINISTIC_GATE_PATH.read_text(encoding="utf-8"),
        CONTRIBUTING_PATH.read_text(encoding="utf-8"),
    )
    assert lifecycle in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert all("Ready" in source for source in sources)

    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["gate"]["enum"] == ["ready", "merge", "cleanup"]
    router = yaml.safe_load(ROUTER_PATH.read_text(encoding="utf-8"))
    assert router["model_routes"]["tier0"]["approval"]["prompt_id"] == "deterministic-gate-v1"


def test_deterministic_prompt_permits_bounded_authorization_but_not_mutation() -> None:
    metadata = _prompt_frontmatter()["deterministic-gate-v1"]
    forbidden_actions = metadata["forbidden_actions"]
    assert "perform_git_or_github_mutations" in forbidden_actions
    assert "authorize_or_perform_git_or_github_mutations" not in forbidden_actions

    prompt = " ".join(DETERMINISTIC_GATE_PATH.read_text(encoding="utf-8").split())
    assert "authorizes only `draft_to_ready`" in prompt
    assert "authorizes only `squash_merge`" in prompt
    assert "must not perform a Git or GitHub mutation" in prompt


def test_ready_policy_keeps_absent_candidate_ci_outside_ready_hold_conditions() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert (
        "An absent candidate or CI is not a Ready defect and must not cause Ready HOLD." in workflow
    )
    assert "For Merge, Approval returns `HOLD` for non-successful exact-head CI" in workflow
    assert (
        "For Ready and Merge, Approval returns `HOLD` for missing or contradictory applicable"
        in workflow
    )


def test_representative_deterministic_ready_and_merge_reports_are_schema_valid() -> None:
    ready = _representative_deterministic_report(
        gate="ready",
        verdict="APPROVE",
        authorized_mutation="draft_to_ready",
        reviewed_sha="WORKING_TREE",
        evidence_fact="Complete Draft task package; no candidate or CI is required.",
    )
    merge_missing_ci = _representative_deterministic_report(
        gate="merge",
        verdict="HOLD",
        authorized_mutation="none",
        reviewed_sha="a" * 40,
        evidence_fact="Frozen candidate lacks exact-head hosted CI.",
    )
    merge_complete = _representative_deterministic_report(
        gate="merge",
        verdict="APPROVE",
        authorized_mutation="squash_merge",
        reviewed_sha="b" * 40,
        evidence_fact="Frozen candidate has complete scoped evidence and exact-head hosted CI.",
    )

    for report in (ready, merge_missing_ci, merge_complete):
        _assert_matches_deterministic_report_schema(report)
        json.dumps(report)

    assert (ready["gate"], ready["authorized_mutation"]) == ("ready", "draft_to_ready")
    assert ready["reviewed_sha"] == "WORKING_TREE"
    assert (merge_missing_ci["gate"], merge_missing_ci["verdict"]) == ("merge", "HOLD")
    assert merge_missing_ci["authorized_mutation"] == "none"
    assert (merge_complete["gate"], merge_complete["authorized_mutation"]) == (
        "merge",
        "squash_merge",
    )
