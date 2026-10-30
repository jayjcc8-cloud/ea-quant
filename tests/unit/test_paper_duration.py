"""A finite desktop session uses ordinary audited stop, never healthy source exhaustion."""

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from ea.product.paper_run import resume_local_paper, run_local_paper
from ea.web.paper_desktop import DesktopPaper
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_candidate_runtime import _create_candidate_evidence


@pytest.fixture
def accepted(tmp_path: Path) -> tuple[Path, str, Path]:
    path = bounded_scenario(tmp_path / "scenarios")
    _create_candidate_evidence(tmp_path, path, None)
    candidate = json.loads((tmp_path / "selection.json").read_text())["candidate_id"]
    return tmp_path / "workspace", candidate, path


def test_duration_stops_a_real_child_and_reopens_without_another_dispatch(
    tmp_path: Path, accepted: tuple[Path, str, Path]
) -> None:
    workspace, candidate, _ = accepted
    root = tmp_path / "desktop-paper"
    manager = DesktopPaper(root, workspace)
    manager.open()
    request = dict(
        request_id=str(uuid4()),
        candidate_id=candidate,
        fingerprint=manager.prepare(candidate)["fingerprint"],
        name="One finite session",
        prices=[100.0],
        interval_seconds=0.01,
        duration_seconds=1,
    )
    try:
        manager.start(request)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = manager.get(request["request_id"])
            if row["terminal_confirmed"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(row)
        assert row["state"] == "stopped", row
        assert row["overview"]["reason"] == "stop_requested"
        assert row["overview"]["stop_trigger"] == "duration_budget"
        assert row["overview"]["fills"] == 6
        assert row["overview"]["cash"] == "9998.8"
        assert row["overview"]["position"] == "0"
        assert not row["overview"]["lease_held"]
        assert row["process_exit"] == 0
    finally:
        manager.close()
    manager = DesktopPaper(root, workspace)
    manager.open()
    try:
        reopened = manager.start(request)
        assert reopened["terminal_confirmed"]
        assert reopened["overview"]["stop_trigger"] == "duration_budget"
        assert reopened["overview"]["market_events"] == row["overview"]["market_events"]
        assert len(list((root / "runs").iterdir())) == 1
        # The persisted duration is part of the requested identity.
        with pytest.raises(ValueError, match="different configuration"):
            manager.start({**request, "duration_seconds": 2})
    finally:
        manager.close()


@pytest.mark.parametrize("mode", ["requested", "source_exhausted"])
def test_duration_does_not_relabel_manual_stop_or_source_failure(
    tmp_path: Path, accepted: tuple[Path, str, Path], mode: str
) -> None:
    workspace, candidate, path = accepted
    result = run_local_paper(
        workspace,
        candidate,
        path,
        tmp_path / "runs",
        interval=0.01,
        duration_seconds=60,
        parent_stop=lambda: mode == "requested",
        event_limit=1 if mode == "source_exhausted" else None,
    )
    assert result["reason"] == ("stop_requested" if mode == "requested" else "source_exhausted")
    assert result["state"] == ("stopped" if mode == "requested" else "failed")
    assert result.get("stop_trigger") != "duration_budget"
    assert resume_local_paper(Path(result["run_dir"])).get("stop_trigger") != "duration_budget"


@pytest.mark.parametrize("duration", [0, -1, 3601, True, 1.5, float("inf"), "60"])
def test_invalid_duration_refuses_before_creating_runtime(tmp_path: Path, duration: object) -> None:
    with pytest.raises(ValueError, match="duration"):
        run_local_paper(
            tmp_path,
            "unused",
            tmp_path / "missing.yaml",
            tmp_path / "runs",
            duration_seconds=duration,  # type: ignore[arg-type]
        )
    assert not (tmp_path / "runs").exists()
