from pathlib import Path

from ea.web.datasets import LocalResearchDatasetRegistryV1
from unit.test_historical_market_data import _content, _row


def test_catalog_distinguishes_revisions_durations_and_observed_gaps(tmp_path: Path) -> None:
    (tmp_path / "research.csv").write_bytes(
        _content(
            _row(),
            _row(revision="1", sequence="1", available="2026-01-02T09:32:00.000000Z"),
            _row(
                start="2026-01-02T09:33:00.000000Z",
                end="2026-01-02T09:35:00.000000Z",
                available="2026-01-02T09:35:00.000000Z",
                sequence="2",
            ),
        )
    )
    item = LocalResearchDatasetRegistryV1(tmp_path).inspect("research.csv")
    assert item["record_count"] == 3
    assert item["bar_count"] == 2
    assert item["revision_count"] == 1
    assert item["bar_durations_seconds"] == ["60", "120"]
    assert item["observed_gap_count"] == 1
    assert item["largest_gap_seconds"] == "120"
    assert item["bar_start_utc"] == "2026-01-02T09:30:00.000000Z"
    assert item["bar_end_utc"] == "2026-01-02T09:35:00.000000Z"


def test_invalid_catalog_retains_safe_structured_location(tmp_path: Path) -> None:
    (tmp_path / "invalid.csv").write_bytes(_content(_row(high="wrong")))
    item = LocalResearchDatasetRegistryV1(tmp_path).list()[0]
    assert item["valid"] is False
    assert item["error_code"] == "invalid_float"
    assert item["record_number"] == 2  # Existing decoder counts the CSV header as record 1.
    assert item["field_name"] == "high"
    assert str(tmp_path) not in str(item)


def test_contiguous_bars_have_no_observed_gap(tmp_path: Path) -> None:
    (tmp_path / "research.csv").write_bytes(
        _content(
            _row(),
            _row(
                start="2026-01-02T09:31:00.000000Z",
                end="2026-01-02T09:32:00.000000Z",
                available="2026-01-02T09:32:00.000000Z",
                sequence="1",
            ),
        )
    )
    item = LocalResearchDatasetRegistryV1(tmp_path).inspect("research.csv")
    assert item["observed_gap_count"] == 0
    assert item["largest_gap_seconds"] is None
