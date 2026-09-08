"""Directory-backed local research inputs; filenames are references only."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ea.data.historical import Phase1HistoricalDataset, read_full_capture_phase1_ohlcv_csv

DATASET_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.csv"


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
            except (OSError, ValueError):
                entry = {
                    "dataset_id": path.name,
                    "valid": False,
                    "message": "invalid or unavailable strict OHLCV input",
                }
            entries.append(entry)
        return entries
