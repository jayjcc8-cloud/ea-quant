"""Fixed offline examples, projected through the existing C2 journal only."""

from pathlib import Path
from typing import Any, cast

from ea.product.prediction import (
    Contract,
    Observation,
    PredictionJournal,
    instant,
    read_document,
)

SAMPLES = ("historical-resolved", "synthetic-void")


def sample_document(sample_id: str) -> dict[str, Any]:
    if sample_id not in SAMPLES:
        raise KeyError("unknown offline event sample")
    return cast(
        dict[str, Any], read_document(Path(__file__).parent / "event_samples" / f"{sample_id}.json")
    )


class EventView:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def view(self, sample_id: str, as_of: str | None = None) -> dict[str, object]:
        fixture = sample_document(sample_id)
        contract = Contract.model_validate(fixture["contract"])
        observation = Observation.model_validate(fixture["observation"])
        if as_of is not None:
            instant(as_of)  # Validate before creating any journal.
        root = self.workspace / "events"
        path = root / f"{sample_id}.json"
        if (
            root.is_symlink()
            or path.is_symlink()
            or not root.resolve().is_relative_to(self.workspace.resolve())
        ):
            raise ValueError("event workspace boundary rejected")
        result = None
        if as_of is not None or path.exists():
            journal = PredictionJournal(path, contract, fixture["side"], fixture["quantity"])
            result = journal.report() if as_of is None else journal.apply(observation, as_of)
        return {"sample_id": sample_id, **fixture, "result": result}
