"""Directory-backed local research inputs; filenames are references only."""

from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path

from ea.data.historical import (
    HistoricalMarketDataError,
    Phase1HistoricalDataset,
    read_full_capture_phase1_ohlcv_csv,
)

DATASET_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.csv"


def _seconds(value: timedelta) -> str:
    micros = (value.days * 86400 + value.seconds) * 1000000 + value.microseconds
    whole, fraction = divmod(micros, 1000000)
    return str(whole) if not fraction else f"{whole}.{fraction:06d}".rstrip("0")


def _coverage(dataset: Phase1HistoricalDataset) -> dict[str, object]:
    events = dataset.selection.events
    # Revisions describe the same source-scoped bar. Coverage is the union of intervals,
    # so overlapping periods and multiple sources cannot fabricate temporal gaps.
    intervals = sorted({(e.payload.interval_start, e.payload.interval_end) for e in events})
    end = intervals[0][1]
    gaps = []
    for start, stop in intervals[1:]:
        if start > end:
            gaps.append(start - end)
        end = max(end, stop)
    return {
        "bar_count": len({e.logical_key for e in events}),
        "revision_count": sum(e.revision > 0 for e in events),
        "bar_durations_seconds": [_seconds(d) for d in sorted({b - a for a, b in intervals})],
        "observed_gap_count": len(gaps),
        "largest_gap_seconds": _seconds(max(gaps)) if gaps else None,
        "bar_start_utc": intervals[0][0].strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "bar_end_utc": end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


class LocalResearchDatasetRegistryV1:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("data root must be an explicit absolute directory")
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("data root must be a directory")
        self._identity = self.root.stat()

    def load(self, dataset_id: str) -> tuple[Path, Phase1HistoricalDataset]:
        if type(dataset_id) is not str or re.fullmatch(DATASET_ID_PATTERN, dataset_id) is None:
            raise ValueError("invalid dataset_id")
        # Anchor the authorization boundary, reject replaced/symlinked ancestor directories.
        directory = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            current = os.fstat(directory)
            if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
                raise ValueError("data root changed")
            path = self.root / dataset_id
            dataset = read_full_capture_phase1_ohlcv_csv(path, directory_descriptor=directory)
            after = self.root.stat(follow_symlinks=False)
            if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("data root changed")
        finally:
            os.close(directory)
        if len({event.payload.instrument for event in dataset.selection.events}) != 1:
            raise ValueError("registered input requires exactly one instrument")
        return path, dataset

    def inspect(self, dataset_id: str) -> dict[str, object]:
        _, dataset = self.load(dataset_id)
        instrument = dataset.selection.events[0].payload.instrument
        return {
            "dataset_id": dataset_id,
            "valid": True,
            "profile": dataset.profile,
            "source_sha256": dataset.source_bytes_sha256.value,
            "data_sha256": dataset.selection.fingerprint.sha256.value,
            "record_count": dataset.selection.fingerprint.record_count,
            **_coverage(dataset),
            "venue": instrument.venue.code,
            "symbol": instrument.symbol,
            "replay_start_utc": dataset.replay_window.start_inclusive.strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "replay_end_utc": dataset.replay_window.end_exclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

    def list(self) -> list[dict[str, object]]:
        entries = []
        for path in sorted(self.root.iterdir()):
            if re.fullmatch(DATASET_ID_PATTERN, path.name) is None:
                continue
            try:
                entry = self.inspect(path.name)
            except HistoricalMarketDataError as error:
                entry = {
                    "dataset_id": path.name,
                    "valid": False,
                    "message": "strict OHLCV validation failed",
                    "error_code": error.code.value,
                    "record_number": error.record_number,
                    "field_name": error.field_name,
                }
            except (OSError, ValueError):
                entry = {
                    "dataset_id": path.name,
                    "valid": False,
                    "message": "invalid or unavailable strict OHLCV input",
                    "error_code": "invalid_or_unavailable_input",
                }
            entries.append(entry)
        return entries
