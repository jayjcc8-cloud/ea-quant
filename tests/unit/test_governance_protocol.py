from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTER_PATH = PROJECT_ROOT / ".governance" / "router.yaml"
WORKFLOW_PATH = PROJECT_ROOT / "docs" / "governance" / "WORKFLOW.md"
STATUS_PATH = PROJECT_ROOT / "docs" / "STATUS.md"
RESET_ADR_PATH = PROJECT_ROOT / "docs" / "adr" / "0029-governance-freeze-and-bounded-delivery.md"
ISSUE_TEMPLATE_PATH = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE" / "task.yml"
PR_TEMPLATE_PATH = PROJECT_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
CANDIDATE_FULL_PATH = PROJECT_ROOT / ".github" / "workflows" / "candidate-full.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _workflow_jobs(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(_read(path))
    assert isinstance(document, dict)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_router_is_advisory_and_cannot_activate_a_review_chain() -> None:
    router = yaml.safe_load(_read(ROUTER_PATH))
    assert isinstance(router, dict)

    assert router["role"] == "classification_adviser"
    assert router["authority"] == "none"
    assert set(router["risk_tiers"]) == {"tier0", "tier1", "tier2", "tier3"}
    assert "model_routes" not in router
    assert "actor_separation" not in router
    assert "sol_capacity" not in router
    assert "activate_agent" in router["prohibited_actions"]
    assert "create_review_chain" in router["prohibited_actions"]
    assert router["output_contract"]["classification"]["risk_tier_suggestion"] == [
        "tier0",
        "tier1",
        "tier2",
        "tier3",
    ]


def test_governance_reset_is_accepted_and_supersedes_recursive_delivery_gates() -> None:
    adr = _read(RESET_ADR_PATH)
    workflow = _read(WORKFLOW_PATH)
    status = _read(STATUS_PATH)

    assert "## Status\n\nAccepted" in adr
    for prior in ("ADR 0025", "ADR 0026", "ADR 0028"):
        assert prior in adr
    for clause in (
        "Governance Freeze",
        "Product Owner",
        "Implementer",
        "Reviewer",
        "four conditions",
        "one primary review",
        "one concentrated repair",
    ):
        assert clause.lower() in adr.lower()

    normalized_workflow = " ".join(workflow.lower().split())
    assert "governance is a constraint on delivery" in normalized_workflow
    assert "hardening backlog" in workflow.lower()
    assert "proof that all possible defects are absent" in workflow.lower()
    assert "phase 1 delivery reset" in status.lower()
    assert "#154" in status


def test_workflow_routes_by_actual_impact_and_has_finite_review_rules() -> None:
    workflow = _read(WORKFLOW_PATH)
    normalized = " ".join(workflow.split())

    for tier in ("T0", "T1", "T2", "T3"):
        assert re.search(rf"\| {tier} \|", workflow)
    assert "actual reachable impact" in workflow.lower()
    assert "one primary review" in workflow.lower()
    assert "one verification" in workflow.lower()
    assert "current diff or its direct execution path" in workflow
    assert "failing test, minimal reproduction, or concrete exploit" in normalized
    assert "exact-head CI" in workflow
    assert "T2/T3" in workflow
    assert "real-money" in workflow.lower()


def test_task_and_pr_templates_focus_on_delivery_not_role_evidence() -> None:
    issue = yaml.safe_load(_read(ISSUE_TEMPLATE_PATH))
    assert isinstance(issue, dict)
    fields = {item.get("id"): item for item in issue["body"] if isinstance(item, dict)}
    assert set(fields) >= {
        "objective",
        "scope",
        "non_goals",
        "acceptance_criteria",
        "risk_tier",
        "risk_rationale",
        "validation",
        "owner",
    }
    assert fields["risk_tier"]["attributes"]["options"] == [
        "T0 — documentation/tooling",
        "T1 — offline product/simulation",
        "T2 — operational authority",
        "T3 — real-money/irreversible",
    ]

    pr = _read(PR_TEMPLATE_PATH)
    for clause in (
        "Acceptance criteria",
        "Blocking finding test",
        "Primary review",
        "Concentrated repair",
        "Token cost",
        "Human time",
    ):
        assert clause in pr
    for legacy in (
        "Combined Safety Verification",
        "Merge Approval",
        "Context/report evidence",
        "Ready/Merge/Cleanup",
    ):
        assert legacy not in pr


def test_ci_keeps_path_aware_quality_and_installed_main_smoke() -> None:
    jobs = _workflow_jobs(CI_WORKFLOW_PATH)
    assert set(jobs) == {"classify", "governance", "quality", "main_smoke", "frontend"}
    assert "needs.classify.outputs.docs_only == 'true'" in jobs["governance"]["if"]
    assert "github.event_name == 'pull_request'" in jobs["quality"]["if"]
    assert jobs["main_smoke"]["if"] == "github.event_name == 'push'"
    assert "needs.classify.outputs.web == 'true'" in jobs["frontend"]["if"]

    smoke = "\n".join(step.get("run", "") for step in jobs["main_smoke"]["steps"])
    assert "uv build --wheel" in smoke
    assert "uv pip install" in smoke
    assert "ea doctor" in smoke


def test_exact_candidate_full_remains_available_for_t2_t3_and_release() -> None:
    document = yaml.safe_load(_read(CANDIDATE_FULL_PATH))
    assert isinstance(document, dict)
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict)
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["candidate_sha"]["required"] is True
    assert inputs["purpose"]["options"] == ["frozen_candidate", "release_candidate"]

    full = document["jobs"]["full"]
    commands = "\n".join(step.get("run", "") for step in full["steps"])
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in commands
    assert "scripts/verify.py --profile full" in commands
