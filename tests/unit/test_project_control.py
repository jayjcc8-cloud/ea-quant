from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATUS_PATH = PROJECT_ROOT / "docs" / "STATUS.md"
ROADMAP_PATH = PROJECT_ROOT / "docs" / "ROADMAP.md"
WORKFLOW_PATH = PROJECT_ROOT / "docs" / "governance" / "WORKFLOW.md"
ADR25_PATH = (
    PROJECT_ROOT / "docs" / "adr" / "0025-project-control-plane-and-authority-precedence.md"
)
ADR29_PATH = PROJECT_ROOT / "docs" / "adr" / "0029-governance-freeze-and-bounded-delivery.md"
ISSUE_FORM_PATH = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE" / "task.yml"
ISSUE_CONFIG_PATH = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
PR_TEMPLATE_PATH = PROJECT_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing project-control file: {path.relative_to(PROJECT_ROOT)}"
    return path.read_text(encoding="utf-8")


def _headings(document: str) -> set[str]:
    return {
        match.group(1).strip()
        for line in document.splitlines()
        if (match := re.fullmatch(r"#{1,6}\s+(.+)", line)) is not None
    }


def _issue_fields() -> dict[str, dict[str, Any]]:
    form = yaml.safe_load(_read(ISSUE_FORM_PATH))
    assert isinstance(form, dict)
    return {
        item["id"]: item
        for item in form["body"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def test_control_plane_has_one_present_one_future_and_immutable_decisions() -> None:
    assert {
        "Current Phase",
        "Phase Objective",
        "Completed",
        "Incomplete",
        "Blockers",
        "Effective ADRs",
        "Primary Issues",
        "Last Confirmed",
        "Phase Completion Conditions",
        "Delivery Metrics",
    } <= _headings(_read(STATUS_PATH))
    assert {"Authority Precedence", "Risk Tiers", "Blocking Findings", "Definition of Done"} <= (
        _headings(_read(WORKFLOW_PATH))
    )
    assert "Phase 1 — Backtest MVP" in _headings(_read(ROADMAP_PATH))
    assert "Accepted" in _read(ADR29_PATH)
    assert all(
        path.parent == PROJECT_ROOT / "docs" / "adr"
        for path in (PROJECT_ROOT / "docs").rglob("*.md")
        if _read(path).startswith("# ADR ")
    )


def test_status_declares_healthy_main_and_denies_live() -> None:
    status = _read(STATUS_PATH)
    assert "main healthy" in status
    assert "live unavailable" in status
    assert "containing `main` commit" in status


def test_authority_precedence_remains_stable_while_process_is_superseded() -> None:
    expected = [
        "merged code, test results, and CI",
        "Accepted ADRs and formal specifications",
        "docs/STATUS.md",
        "Issue and pull-request bodies",
        "Issue comments, pull-request comments, and chat",
    ]
    for document in (_read(ADR25_PATH), _read(WORKFLOW_PATH)):
        positions = [document.index(item) for item in expected]
        assert positions == sorted(positions)
    adr29 = _read(ADR29_PATH)
    assert all(prior in adr29 for prior in ("ADR 0025", "ADR 0026", "ADR 0028"))


def test_entry_documents_are_small_and_delivery_first() -> None:
    for name, maximum in (("README.md", 120), ("AGENTS.md", 120), ("CONTRIBUTING.md", 100)):
        text = _read(PROJECT_ROOT / name)
        assert len(text.splitlines()) <= maximum
        assert "docs/STATUS.md" in text
        assert "docs/governance/WORKFLOW.md" in text

    agents = _read(PROJECT_ROOT / "AGENTS.md")
    assert "Primary objective" in agents
    assert "one writer" in agents.lower()
    assert "four conditions" in agents.lower()
    assert "all possible defects" in agents.lower()
    assert "Combined Safety Verification" not in agents


def test_issue_form_is_small_complete_and_uses_actual_impact_tiers() -> None:
    fields = _issue_fields()
    required = {
        "objective",
        "scope",
        "non_goals",
        "acceptance_criteria",
        "risk_tier",
        "risk_rationale",
        "validation",
        "owner",
    }
    assert required <= fields.keys()
    assert len(fields) <= 9
    assert all(fields[field]["validations"]["required"] is True for field in required)
    assert fields["risk_tier"]["attributes"]["options"] == [
        "T0 — documentation/tooling",
        "T1 — offline product/simulation",
        "T2 — operational authority",
        "T3 — real-money/irreversible",
    ]


def test_pr_template_records_finite_review_and_delivery_cost() -> None:
    template = _read(PR_TEMPLATE_PATH)
    for clause in (
        "Acceptance criteria",
        "Blocking finding test",
        "Primary review",
        "Concentrated repair",
        "Hardening backlog",
        "Token cost",
        "Human time",
        "Runnable capability",
    ):
        assert clause in template
    for legacy in (
        "Combined Safety Verification",
        "Merge Approval",
        "Context/report evidence",
        "Ready/Merge/Cleanup",
    ):
        assert legacy not in template


def test_blank_issue_bypass_stays_disabled() -> None:
    assert yaml.safe_load(_read(ISSUE_CONFIG_PATH)) == {"blank_issues_enabled": False}
