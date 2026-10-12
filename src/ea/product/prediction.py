"""Closed, offline binary-claim settlement; no order, strategy, wallet or transport."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ea.core.economics import CanonicalDecimal, require_quantized, settle_product
from ea.core.time import require_utc

State = Literal["pending", "resolved", "void", "disputed", "unknown"]
Side = Literal["YES", "NO"]


def instant(text: str) -> datetime:
    return require_utc(datetime.fromisoformat(text), field="prediction time")


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Contract(ClosedModel):
    version: Literal[1]
    contract_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=512)
    rules: str = Field(min_length=1, max_length=2048)
    authority: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=1024)
    source_kind: Literal["official_history", "synthetic"]
    rules_observed_at: str
    currency: Literal["USD_SIM"]
    quantum: Literal["0.01"]
    face_value: Literal["1"]
    void_payout: str | None

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be exact integer")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Contract:
        if type(self.version) is not int:
            raise ValueError("version must be exact integer")
        instant(self.rules_observed_at)
        if self.source_kind == "official_history" and not self.source.startswith("https://"):
            raise ValueError("official history requires an explicit HTTPS source")
        if self.void_payout is not None:
            if self.source_kind != "synthetic":
                raise ValueError("historical fixture has no evidenced void rule")
            value = CanonicalDecimal(self.void_payout)
            require_quantized(value, CanonicalDecimal(self.quantum), field_name="void payout")
            if not CanonicalDecimal("0") <= value <= CanonicalDecimal(self.face_value):
                raise ValueError("void payout outside face value")
        return self


class Observation(ClosedModel):
    contract_id: str
    authority: str
    source: str
    available_at: str
    status: State
    outcome: Side | None
    payout_yes: str | None
    payout_no: str | None
    evidence: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_observation(self) -> Observation:
        instant(self.available_at)
        if self.status == "resolved":
            if self.outcome is None or self.payout_yes is None or self.payout_no is None:
                raise ValueError("resolved requires an explicit binary outcome and payouts")
        elif self.status == "void":
            if self.outcome is not None or self.payout_yes is None or self.payout_no is None:
                raise ValueError("void requires explicit payouts and no winning outcome")
        elif any(x is not None for x in (self.outcome, self.payout_yes, self.payout_no)):
            raise ValueError("nonfinal observations cannot carry outcome or payout")
        for text in (self.payout_yes, self.payout_no):
            if text is not None:
                value = CanonicalDecimal(text)
                if not CanonicalDecimal("0") <= value <= CanonicalDecimal("1"):
                    raise ValueError("illegal payout")
        return self


class Record(ClosedModel):
    observation: Observation
    applied_at: str


class Journal(ClosedModel):
    version: Literal[1]
    contract: Contract
    side: Side
    quantity: int = Field(ge=1, le=1_000_000)
    records: list[Record] = Field(max_length=256)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_document(path: Path) -> object:
    if path.is_symlink():
        raise ValueError("symlink evidence is unsupported")
    with path.open("rb") as handle:
        payload = handle.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("prediction evidence exceeds bound")
    return json.loads(payload, object_pairs_hook=_unique)


def _publish(path: Path, document: Journal) -> None:
    # Same temp+fsync+rename+directory-fsync barrier as the research store.
    # One document contains observation and settlement inputs, avoiding split publication.
    payload = json.dumps(document.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    descriptor, name = tempfile.mkstemp(prefix=".prediction-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class PredictionJournal:
    """One pre-endowed claim, local cash zero; payout is gross, never trading P&L.

    Reads replay the complete journal; no independently saved balance can be doubled
    after a crash. Each operation locks/reloads, so stale instances have no authority.
    Manual local evidence is trusted input; source binding is not signature verification.
    """

    def __init__(self, path: Path, contract: Contract, side: Side, quantity: int):
        self.path = path
        self.seed = Journal(version=1, contract=contract, side=side, quantity=quantity, records=[])
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("symlink journal unsupported")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if path.exists():
                self._load()
            else:
                _publish(path, self.seed)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(
            str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def _load(self) -> Journal:
        document = Journal.model_validate(read_document(self.path))
        if (document.contract, document.side, document.quantity) != (
            self.seed.contract,
            self.seed.side,
            self.seed.quantity,
        ):
            raise ValueError("journal bootstrap conflict")
        self._replay(document)
        return document

    def _check(self, event: Observation, at: str) -> bool:
        contract = self.seed.contract
        if (event.contract_id, event.authority, event.source) != (
            contract.contract_id,
            contract.authority,
            contract.source,
        ):
            raise ValueError("unbound result authority/source/contract")
        if instant(event.available_at) < instant(contract.rules_observed_at):
            raise ValueError(
                "availability precedes captured rules; historical availability unproven"
            )
        if event.status == "resolved":
            expected = ("1", "0") if event.outcome == "YES" else ("0", "1")
            if (event.payout_yes, event.payout_no) != expected:
                raise ValueError("payout disagrees with binary outcome")
        if event.status == "void" and (
            contract.void_payout is None
            or (event.payout_yes, event.payout_no) != (contract.void_payout, contract.void_payout)
        ):
            raise ValueError("void has no bound cancellation payout rule")
        return instant(at) >= instant(event.available_at)

    def _replay(self, document: Journal) -> dict[str, object]:
        terminal: Observation | None = None
        state: State = "pending"
        last_at: datetime | None = None
        for record in document.records:
            at = instant(record.applied_at)
            if not self._check(record.observation, record.applied_at):
                raise ValueError("replay contains invisible result")
            if last_at is not None and at < last_at:
                raise ValueError("replay time regressed")
            if terminal is not None:
                raise ValueError("journal contains record after terminal settlement")
            last_at = at
            state = record.observation.status
            if state in ("resolved", "void"):
                terminal = record.observation
        cash = "0"
        postings: list[dict[str, str]] = []
        if terminal is not None:
            payout = terminal.payout_yes if document.side == "YES" else terminal.payout_no
            assert payout is not None
            amount = settle_product(
                CanonicalDecimal(payout),
                CanonicalDecimal(str(document.quantity)),
                CanonicalDecimal("1"),
                CanonicalDecimal("0.01"),
            )
            if amount.rounding_residual.text != "0":
                raise ValueError("prediction payout must settle exactly")
            cash = amount.amount.text
            negative = "0" if cash == "0" else "-" + cash
            postings = [
                {"account": "cash", "currency": "USD_SIM", "amount": cash},
                {
                    "account": "settlement_clearing",
                    "currency": "USD_SIM",
                    "amount": negative,
                },
            ]
        return {
            "domain": "offline-binary-claim-v1",
            "contract_id": document.contract.contract_id,
            "status": state,
            "outcome": terminal.outcome if terminal is not None else None,
            "claim_side": document.side,
            "authority": document.contract.authority,
            "result_available_at": terminal.available_at if terminal is not None else None,
            "success": state == "resolved",
            "currency": "USD_SIM",
            "cash": cash,
            "cash_semantics": "gross simulated payout; no trading P&L",
            "open_claims": document.quantity if terminal is None else 0,
            "retired_claims": document.quantity if terminal is not None else 0,
            "settlement_count": int(terminal is not None),
            "postings": postings,
            "source_kind": document.contract.source_kind,
            "source": document.contract.source,
        }

    def report(self, at: str | None = None) -> dict[str, object]:
        with self._lock():
            document = self._load()
            if at is not None:
                self._check_frontier(document, at)
            return self._replay(document)

    @staticmethod
    def _check_frontier(document: Journal, at: str) -> None:
        current = instant(at)
        if document.records and current < instant(document.records[-1].applied_at):
            raise ValueError("application time regressed; use a fresh historical journal")

    def apply(self, event: Observation, at: str) -> dict[str, object]:
        with self._lock():
            document = self._load()
            report = self._replay(document)
            self._check_frontier(document, at)
            if not self._check(event, at):
                return {**report, "action": "not_visible"}
            if document.records:
                previous = document.records[-1]
                if instant(at) < instant(previous.applied_at):
                    raise ValueError("application time regressed")
                if previous.observation.status in ("resolved", "void"):
                    old = previous.observation
                    if (old.status, old.outcome, old.payout_yes, old.payout_no) != (
                        event.status,
                        event.outcome,
                        event.payout_yes,
                        event.payout_no,
                    ):
                        raise ValueError(
                            "terminal conflict: revisions require explicit adjudication"
                        )
                    return {**report, "action": "duplicate_noop"}
                if previous.observation == event:
                    return {**report, "action": "duplicate_noop"}
            updated = Journal.model_validate(
                {
                    **document.model_dump(),
                    "records": [
                        *[r.model_dump() for r in document.records],
                        Record(observation=event, applied_at=at).model_dump(),
                    ],
                }
            )
            result = self._replay(updated)
            _publish(self.path, updated)
            return {**result, "action": "applied"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline binary-claim lifecycle only")
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    fixture = read_document(args.fixture)
    if type(fixture) is not dict or set(fixture) != {
        "contract",
        "side",
        "quantity",
        "observation",
    }:
        raise ValueError("fixture requires exact contract/side/quantity/observation fields")
    journal = PredictionJournal(
        args.journal,
        Contract.model_validate(fixture["contract"]),
        fixture["side"],
        fixture["quantity"],
    )
    event = fixture["observation"]
    result = (
        journal.report(args.as_of)
        if event is None
        else journal.apply(Observation.model_validate(event), args.as_of)
    )
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
