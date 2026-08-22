from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATUS_PATH = PROJECT_ROOT / "docs" / "STATUS.md"
ROADMAP_PATH = PROJECT_ROOT / "docs" / "ROADMAP.md"
WORKFLOW_PATH = PROJECT_ROOT / "docs" / "governance" / "WORKFLOW.md"
ADR_PATH = PROJECT_ROOT / "docs" / "adr" / "0025-project-control-plane-and-authority-precedence.md"
ISSUE_FORM_PATH = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE" / "task.yml"
ISSUE_TEMPLATE_CONFIG_PATH = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
PR_TEMPLATE_PATH = PROJECT_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
ROUTER_PATH = PROJECT_ROOT / ".governance" / "router.yaml"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing project-control file: {path.relative_to(PROJECT_ROOT)}"
    return path.read_text(encoding="utf-8")


def _headings(document: str) -> set[str]:
    return {
        match.group(1).strip()
        for line in document.splitlines()
        if (match := re.fullmatch(r"#{1,6}\s+(.+)", line)) is not None
    }


def _local_links(path: Path) -> list[Path]:
    links: list[Path] = []
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", _read(path)):
        target = target.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        links.append((path.parent / target).resolve())
    return links


def _issue_form_fields() -> dict[str, dict[str, Any]]:
    form = yaml.safe_load(_read(ISSUE_FORM_PATH))
    assert isinstance(form, dict)
    body = form.get("body")
    assert isinstance(body, list)
    fields: dict[str, dict[str, Any]] = {}
    for entry in body:
        assert isinstance(entry, dict)
        field_id = entry.get("id")
        if isinstance(field_id, str):
            fields[field_id] = entry
    return fields


def test_core_control_plane_documents_have_fixed_sections() -> None:
    required = {
        STATUS_PATH: {
            "Current Phase",
            "Phase Objective",
            "Completed",
            "Incomplete",
            "Blockers",
            "Effective ADRs",
            "Primary Issues",
            "Last Confirmed",
            "Phase Completion Conditions",
            "Weekly Governance Metrics",
        },
        ROADMAP_PATH: {
            "Phase 1 — Backtest MVP",
            "Phase 1 Non-goals",
            "Phase 1 Exit Criteria",
            "Phase 2 Entry Gate",
        },
        WORKFLOW_PATH: {
            "Authority Precedence",
            "Issue Lifecycle",
            "Task Package",
            "Risk Tiers and Model Routing",
            "Writer Lease and Agent Concurrency",
            "Context and Review Evidence",
            "Long-item Compression",
            "Definition of Done",
            "Weekly Governance Metrics",
        },
        ADR_PATH: {
            "Status",
            "Context",
            "Decision",
            "Rejected Alternatives",
            "Consequences",
            "Implementation and Validation",
            "References",
        },
    }
    for path, sections in required.items():
        assert sections <= _headings(_read(path))


def test_status_declares_the_current_phase_and_health_without_claiming_live_readiness() -> None:
    status = _read(STATUS_PATH)
    assert "Phase 1" in status
    assert "Incomplete" in status
    assert "main healthy" in status
    assert "live unavailable" in status
    assert "2026-08-22" in status
    assert "af7bc08cecd70fc5479b92e4393da468727ddb55" in status
    assert "last independently verified pre-consolidation checkpoint" in status.lower()
    assert "containing `main` commit" in status


def test_status_links_the_phase1_closeout_and_clean_successor_issues() -> None:
    status = _read(STATUS_PATH)
    for issue_number in (81, 82, 83, 84):
        assert f"https://github.com/jayjcc8-cloud/ea-quant/issues/{issue_number}" in status
    assert "Open pull requests: Draft #73 and Draft #80" not in status


def test_status_rejects_self_referential_premerge_facts() -> None:
    status = _read(STATUS_PATH)
    assert "The authoritative merged baseline is" not in status
    assert "Open pull requests: Draft #73 and Draft #80" not in status
    assert "PR #80" in status
    assert re.search(r"does not assert\s+mutable open, closed, Draft, or merged state", status)


def test_authority_precedence_is_identical_in_adr_and_workflow() -> None:
    expected = [
        "merged code, test results, and CI",
        "Accepted ADRs and formal specifications",
        "docs/STATUS.md",
        "Issue and pull-request bodies",
        "Issue comments, pull-request comments, and chat",
    ]
    for document in (_read(ADR_PATH), _read(WORKFLOW_PATH)):
        positions = [document.index(item) for item in expected]
        assert positions == sorted(positions)
        assert "DRIFT/BLOCKED" in document


def test_entry_documents_are_small_and_link_to_the_control_plane() -> None:
    limits = {
        PROJECT_ROOT / "README.md": 120,
        PROJECT_ROOT / "AGENTS.md": 120,
        PROJECT_ROOT / "CONTRIBUTING.md": 100,
    }
    for path, maximum in limits.items():
        text = _read(path)
        assert len(text.splitlines()) <= maximum
        assert "docs/STATUS.md" in text
        assert "docs/governance/WORKFLOW.md" in text
        assert all(target.exists() for target in _local_links(path))

    agents = _read(PROJECT_ROOT / "AGENTS.md")
    assert "## Controlled expert lifecycle" not in agents
    assert "## Delegated Approval Owner" not in agents
    assert "## Expert context bundle" not in agents
    assert "## Review validity and output" not in agents
    assert "one writer" in agents.lower()
    assert "silent downgrade" in agents.lower()


def test_architecture_contains_stable_architecture_not_phase_roadmap() -> None:
    architecture = _read(PROJECT_ROOT / "docs" / "architecture.md")
    assert "docs/STATUS.md" in architecture
    assert "docs/ROADMAP.md" in architecture
    assert "## 10. 阶段性落地" not in architecture
    assert "### Phase 2" not in architecture
    assert "### Phase 3" not in architecture
    assert "### Phase 4" not in architecture


def test_task_issue_form_requires_the_complete_task_package() -> None:
    fields = _issue_form_fields()
    required_ids = {
        "objective",
        "scope",
        "non_goals",
        "authoritative_inputs",
        "acceptance_criteria",
        "risk_tier",
        "risk_rationale",
        "validation",
        "expected_outputs",
        "reuse_assessment",
        "owners",
    }
    assert required_ids <= fields.keys()
    for field_id in required_ids:
        validations = fields[field_id].get("validations")
        assert isinstance(validations, dict)
        assert validations.get("required") is True

    options = fields["risk_tier"]["attributes"]["options"]
    assert options == ["Tier 0 — low", "Tier 1 — normal", "Tier 2 — high"]
    rationale = fields["risk_rationale"]
    assert rationale["type"] == "textarea"
    assert "highest" in rationale["attributes"]["description"].lower()
    assert "trigger" in rationale["attributes"]["description"].lower()


def test_pull_request_template_records_evidence_cost_and_state_sync() -> None:
    template = _read(PR_TEMPLATE_PATH)
    required = {
        "Authoritative inputs",
        "STATUS / ADR synchronization",
        "Candidate HEAD SHA",
        "Validation evidence",
        "Rework rounds",
        "Token cost",
        "Human time",
        "Successor Issues",
    }
    assert all(item in template for item in required)
    assert (
        "Governed Issue lifecycle state: Draft / Ready / In Progress / Review / Verified / Done"
    ) in template
    assert "GitHub pull request review state: Draft / Ready for review" in template
    assert "separate from the governed Issue lifecycle" in template


def test_issue_template_chooser_disables_blank_issue_bypass() -> None:
    config = yaml.safe_load(_read(ISSUE_TEMPLATE_CONFIG_PATH))
    assert config == {"blank_issues_enabled": False}
    assert type(config["blank_issues_enabled"]) is bool


def test_workflow_defines_state_machine_capacity_compression_and_done() -> None:
    workflow = _read(WORKFLOW_PATH)
    assert "Draft → Ready → In Progress → Review → Verified → Done" in workflow
    assert "Tier 1/2" in workflow and "one active task" in workflow
    assert "Tier 0" in workflow and "two independent tasks" in workflow
    assert "30" in workflow
    assert "two decision reversals" in workflow
    assert "three independent problems" in workflow
    assert "repeated misreading" in workflow
    for condition in (
        "acceptance criteria",
        "merged code and tests",
        "CI",
        "ADR",
        "STATUS",
        "final conclusion",
        "validation evidence",
        "Successor Issues",
    ):
        assert condition.lower() in workflow.lower()


def test_workflow_absorbs_the_focused_green_checkpoint_rule() -> None:
    workflow = _read(WORKFLOW_PATH).lower()
    assert "focused-green" in workflow
    assert "checkpoint sha" in workflow
    assert "before the next independent slice" in workflow
    assert "at most one bounded dirty slice" in workflow
    assert "does not authorize ready or merge" in workflow


def test_tier_vocabulary_and_adr_location_remain_canonical() -> None:
    router = yaml.safe_load(_read(ROUTER_PATH))
    assert isinstance(router, dict)
    assert set(router["model_routes"]) == {"tier0", "tier1", "tier2"}
    assert not (PROJECT_ROOT / "docs" / "decisions").exists()
    adr_documents = [
        path
        for path in (PROJECT_ROOT / "docs").rglob("*.md")
        if path.read_text(encoding="utf-8").startswith("# ADR ")
    ]
    assert adr_documents
    assert all(path.parent == PROJECT_ROOT / "docs" / "adr" for path in adr_documents)


def test_workflow_preserves_router_governance_authority_minimum_tier() -> None:
    router = yaml.safe_load(_read(ROUTER_PATH))
    assert isinstance(router, dict)
    tier1_surfaces = router["rules"]["tier1_contract_surfaces"]["surfaces"]
    assert "governance_authority" in tier1_surfaces

    workflow = _read(WORKFLOW_PATH).lower()
    tier1_row = next(line for line in workflow.splitlines() if line.startswith("| tier 1"))
    tier2_row = next(line for line in workflow.splitlines() if line.startswith("| tier 2"))
    assert "governance authority" in tier1_row
    assert "governance authority" not in tier2_row
    assert "human may always raise" in workflow


def test_governance_consumers_reference_workflow_as_the_full_contract() -> None:
    router = yaml.safe_load(_read(ROUTER_PATH))
    assert router["authority_references"] == {
        "status": "docs/STATUS.md",
        "roadmap": "docs/ROADMAP.md",
        "workflow": "docs/governance/WORKFLOW.md",
        "adrs": "docs/adr/",
    }

    consumers = [
        PROJECT_ROOT / ".agents" / "approval-owner.md",
        PROJECT_ROOT / ".governance" / "prompts" / "approval-v1.md",
    ]
    for path in consumers:
        assert "docs/governance/WORKFLOW.md" in _read(path), path

    context_schema = _read(
        PROJECT_ROOT / ".governance" / "schemas" / "context-manifest.schema.json"
    )
    report_schema = _read(PROJECT_ROOT / ".governance" / "schemas" / "report.schema.json")
    assert "docs/governance/WORKFLOW.md" in context_schema
    assert "docs/governance/WORKFLOW.md" in report_schema
