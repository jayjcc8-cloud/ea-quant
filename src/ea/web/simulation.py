"""Read-only preparation for the existing explicit local Paper operation."""

from pathlib import Path
from typing import Any

from ea import __version__
from ea.product.candidate import inspect_candidate_binding
from ea.product.offline_demo import _package_code_digest
from ea.web.candidate_evidence import CandidateEvidenceReader


def prepare_simulation(
    workspace: Path, candidate_id: str, *, reader: CandidateEvidenceReader | None = None
) -> dict[str, Any]:
    reader = CandidateEvidenceReader(workspace) if reader is None else reader
    record = reader.record(candidate_id)
    projection = record["projection"]
    _, _, scenario = reader.job(projection["source"]["job_id"])
    blockers = []
    if record["status"] != "ACCEPTED":
        blockers.append("explicit_research_acceptance_required")
    if scenario["schema_version"] not in (4, 5):
        blockers.append("unsupported_paper_profile_requires_action_v2_v4_v5")
    implementation = projection["strategy"]["implementation"]
    if implementation["code_sha256"] != _package_code_digest().value or implementation[
        "distribution"
    ] != {"name": "ea-quant", "version": __version__}:
        blockers.append("recorded_implementation_required")
    binding = (
        inspect_candidate_binding(workspace, candidate_id, reader=reader).document()
        if record["status"] == "ACCEPTED"
        else None
    )
    return {
        "schema": "ea.local-simulation-preparation.v1",
        "candidate_id": candidate_id,
        "fingerprint": record["fingerprint"],
        "candidate_status": record["status"],
        "strategy": projection["strategy"],
        "scenario_version": scenario["schema_version"],
        "binding": binding,
        "eligible": not blockers,
        "blockers": blockers,
        "mode": "local-simulation",
        "feed": "generated-local-prices",
        "broker": "in-process-paper",
        "execution_authorized": False,
        "start_available": False,
        "runtime_state": "not_started",
        "required_action": (
            "Separate explicit bounded local Paper start; acceptance is not authorization."
        ),
        "stop_semantics": (
            "Stop cuts off new decisions and drains issued facts; it does not liquidate."
        ),
        "small_sample_warning": min(
            projection["source"]["record_count"], projection["holdout"]["record_count"]
        )
        <= 6,
    }
