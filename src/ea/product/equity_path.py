"""Bounded, read-only path evidence for the completed single-entry engine."""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any

from ea.core import AuditRecordKind, CanonicalDecimal, require_quantized, settle_execution
from ea.core.economics import settle_product
from ea.product import reporting as r
from ea.product.backtest import _load_verified_attempt
from ea.product.round_trip_report import BacktestReportV2, BacktestReportV3

MAX_DISPLAY_POINTS = 2048
DISPLAY_SAMPLING = "uniform-index-extrema-v1"


@dataclass(frozen=True, slots=True)
class EquityPoint:
    index: int
    time: str
    equity: str

    def document(self) -> dict[str, object]:
        return {"index": self.index, "time": self.time, "equity": self.equity}


@dataclass(frozen=True, slots=True)
class BacktestEquityPathAnalysisV1:
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class BacktestEquityPathAnalysisV2:
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class BacktestEquityPathAnalysisV3(BacktestEquityPathAnalysisV2):
    pass


def analyze_points(points: Callable[[], Iterator[EquityPoint]]) -> dict[str, Any]:
    """Two passes: exact full-path extrema, then bounded deterministic display selection."""
    peak: EquityPoint | None = None
    best_peak: EquityPoint | None = None
    trough: EquityPoint | None = None
    best_amount = CanonicalDecimal("0")
    best_ratio = CanonicalDecimal("0")
    count = 0
    for point in points():
        if point.index != count:
            raise ValueError("equity path indices are not contiguous")
        count += 1
        value = CanonicalDecimal(point.equity)
        if peak is None:
            if value.coefficient <= 0:
                raise ValueError("initial equity must be positive")
            peak = best_peak = trough = point
        if r._subtract(value, CanonicalDecimal(peak.equity)).coefficient > 0:
            peak = point
        amount = r._subtract(CanonicalDecimal(peak.equity), value)
        ratio = r._ratio(amount, CanonicalDecimal(peak.equity))
        if r._subtract(ratio, best_ratio).coefficient > 0:
            best_peak, trough, best_amount, best_ratio = peak, point, amount, ratio
    if best_peak is None or trough is None:
        raise ValueError("empty equity path")
    required = {0, count - 1, best_peak.index, trough.index}
    if count <= MAX_DISPLAY_POINTS:
        selected = set(range(count))
    else:
        # Reserve all extrema, fill remaining slots uniformly over non-required indices.
        remaining = count - len(required)
        slots = MAX_DISPLAY_POINTS - len(required)
        ranks = {i * (remaining - 1) // (slots - 1) for i in range(slots)}
        selected = set(required)
        rank = 0
        for index in range(count):
            if index not in required:
                if rank in ranks:
                    selected.add(index)
                rank += 1
    display = [point.document() for point in points() if point.index in selected]
    return {
        "point_count": count,
        "display_sampling": DISPLAY_SAMPLING,
        "display_points": display,
        "max_drawdown": {
            "amount": best_amount.text,
            "ratio": best_ratio.text,
            "peak_index": best_peak.index,
            "peak_time": best_peak.time,
            "peak_equity": best_peak.equity,
            "trough_index": trough.index,
            "trough_time": trough.time,
            "trough_equity": trough.equity,
        },
    }


def _market_key(row: dict[str, Any]) -> tuple[str, ...]:
    instrument = row.get("instrument", row)
    return tuple(
        str(value)
        for value in (
            instrument["venue"],
            instrument["symbol"],
            row["adjustment"],
            row["interval_start"],
            row["interval_end"],
            row["source"],
            row["source_sequence"],
            row["revision"],
            row["available_at"],
        )
    )


def generate_equity_path_analysis(
    run_dir: Path,
    report: r.BacktestReportV1 | BacktestReportV2,
) -> BacktestEquityPathAnalysisV1 | BacktestEquityPathAnalysisV2:
    """Verify exact completed evidence and derive points without rerunning execution."""
    attempt = r._safe_attempt(run_dir)
    try:
        with r._read_lease(attempt):
            # The closed reporter owns all existing funding/fill/ledger/result validation.
            with tempfile.TemporaryDirectory(prefix="ea-path-verify-") as temporary:
                verified = r.generate_backtest_report(attempt, Path(temporary).resolve() / "report")
                if verified.report.canonical_bytes != report.canonical_bytes:
                    raise ValueError("path report binding conflicts")
            scenario, _, _, _, _, manifest = _load_verified_attempt(attempt)
            records = r._read_audit_journal(attempt, manifest.binding)
            document: Any = report.document
            source = document["source"]
            data = r._read_regular(scenario.data_path)
            if sha256(data).hexdigest() != source["data"]["source_file_sha256"]:
                raise ValueError("path data binding conflicts")
            prices: dict[tuple[str, ...], CanonicalDecimal] = {}
            for row in csv.DictReader(StringIO(data.decode("utf-8"), newline=""), strict=True):
                key = _market_key(row)
                if key in prices:
                    raise ValueError("ambiguous admitted market identity")
                prices[key] = r._decimal_from_source(row["close"])
            roots: list[dict[str, Any]] = []
            admitted: dict[int, dict[str, Any]] = {}
            committed_at: int | None = None
            commits: list[int] = []
            v2 = isinstance(report, BacktestReportV2)
            for record in records:
                payload: Any = r._payload(record)
                if (
                    record.record_kind is AuditRecordKind.MATCHER_DISPATCH_BATCH
                    and payload["dispatch_kind"] == "market"
                ):
                    admitted[payload["dispatch_sequence"]] = payload["trigger_root_key"]
                elif (
                    record.record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
                    and payload.get("action") == "effect_committed"
                ):
                    if committed_at is not None and not v2:
                        raise ValueError("multiple fill commits unsupported")
                    commits.append(payload["dispatch_sequence"])
                    committed_at = payload["dispatch_sequence"]
                elif (
                    record.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
                    and payload["dispatch_kind"] == "market"
                ):
                    sequence = payload["dispatch_sequence"]
                    if admitted.pop(sequence, None) != payload["trigger_root_key"]:
                        raise ValueError("completed market root conflicts")
                    roots.append(payload)
            if admitted or not roots:
                raise ValueError("incomplete market path")
            economics = document["economics"]
            filled = economics["counts"]["fills"] == 1
            if v2:
                return _round_trip_path(report, scenario, economics, roots, commits, prices)
            if filled != (committed_at is not None) or (
                filled and committed_at not in {root["dispatch_sequence"] for root in roots}
            ):
                raise ValueError("fill is not bound to a completed market root")
            initial = scenario.initial_cash
            ending_cash = CanonicalDecimal(economics["ending_cash"][0]["amount"])
            quantity = CanonicalDecimal(
                economics["execution"]["fill"]["quantity"] if filled else "0"
            )
            spec = scenario.spec_set.require(scenario.instrument)

            def points() -> Iterator[EquityPoint]:
                yield EquityPoint(0, source["replay_window"]["start_inclusive"], initial.text)
                last = initial
                for index, root in enumerate(roots, 1):
                    key = root["trigger_root_key"]
                    price = prices[_market_key(key)]
                    require_quantized(price, spec.price_quantum, field_name="path_price")
                    if committed_at is not None and root["dispatch_sequence"] >= committed_at:
                        position = r._multiply(price, quantity, spec.contract_multiplier)
                        require_quantized(
                            position, spec.currency_quantum, field_name="path_position"
                        )
                        last = r._add(ending_cash, position)
                    else:
                        last = initial
                    yield EquityPoint(index, key["available_at"], last.text)
                if last.text != economics["equity"]["amount"]:
                    raise ValueError("final path equity conflicts with formal report")

            analysis = analyze_points(points)
            analysis.update(
                {
                    "schema": "ea.backtest-equity-path.v1",
                    "schema_version": 1,
                    "evidence_role": "DERIVED_PATH_EVIDENCE",
                    "run_id": document["run_id"],
                    "scenario_sha256": source["scenario_sha256"],
                    "data_sha256": source["data"]["fingerprint"]["sha256"],
                    "record_count": source["data"]["fingerprint"]["record_count"],
                    "report_sha256": sha256(report.canonical_bytes).hexdigest(),
                    "semantic_outcome_sha256": document["completion"]["semantic_outcome_sha256"],
                    "currency": economics["currency"],
                    "valuation_rule": r._VALUATION_RULE,
                }
            )
            return BacktestEquityPathAnalysisV1(r._canonical_json(analysis) + b"\n")
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise r.BacktestReportError("completed path evidence is invalid") from error


def _round_trip_path(
    report: BacktestReportV2 | r.BacktestReportV1,
    scenario: Any,
    economics: dict[str, Any],
    roots: list[dict[str, Any]],
    commits: list[int],
    prices: dict[tuple[str, ...], CanonicalDecimal],
) -> BacktestEquityPathAnalysisV2:
    legs = [leg for leg in economics["execution_legs"] if leg["fill"] is not None]
    sequences = {root["dispatch_sequence"] for root in roots}
    if (
        len(commits) != len(legs)
        or len(set(commits)) != len(commits)
        or not set(commits) <= sequences
    ):
        raise ValueError("fills are not bound to completed roots")
    acknowledged = dict(zip(commits, legs, strict=True))
    spec = scenario.spec_set.require(scenario.instrument)

    def points() -> Iterator[EquityPoint]:
        cash, quantity = scenario.initial_cash, CanonicalDecimal("0")
        yield EquityPoint(
            0,
            scenario.dataset.replay_window.start_inclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            cash.text,
        )
        last = cash
        for index, root in enumerate(roots, 1):
            leg = acknowledged.get(root["dispatch_sequence"])
            if leg is not None:
                fill = leg["fill"]
                amount = settle_execution(
                    scenario.spec_set,
                    scenario.instrument,
                    CanonicalDecimal(fill["price"]),
                    CanonicalDecimal(fill["quantity"]),
                ).amount
                cash = r._subtract(
                    r._subtract(cash, amount) if leg["role"] == "entry" else r._add(cash, amount),
                    CanonicalDecimal(fill["fees"][0]["amount"]),
                )
                quantity = CanonicalDecimal(fill["quantity"] if leg["role"] == "entry" else "0")
            key = root["trigger_root_key"]
            price = prices[_market_key(key)]
            require_quantized(price, spec.price_quantum, field_name="path_price")
            value = (
                settle_product(
                    price, quantity, spec.contract_multiplier, spec.currency_quantum
                ).amount
                if quantity.coefficient
                else CanonicalDecimal("0")
            )
            last = r._add(cash, value)
            yield EquityPoint(index, key["available_at"], last.text)
        if last.text != economics["equity"]["amount"]:
            raise ValueError("final path equity conflicts with formal report")

    document: Any = report.document
    source = document["source"]
    analysis = analyze_points(points)
    analysis.update(
        {
            "schema": "ea.backtest-equity-path.v3"
            if isinstance(report, BacktestReportV3)
            else "ea.backtest-equity-path.v2",
            "schema_version": 3 if isinstance(report, BacktestReportV3) else 2,
            "evidence_role": "DERIVED_PATH_EVIDENCE",
            "run_id": document["run_id"],
            "scenario_sha256": source["scenario_sha256"],
            "data_sha256": source["data"]["fingerprint"]["sha256"],
            "record_count": source["data"]["fingerprint"]["record_count"],
            "report_sha256": sha256(report.canonical_bytes).hexdigest(),
            "semantic_outcome_sha256": document["completion"]["semantic_outcome_sha256"],
            "currency": economics["currency"],
            "valuation_rule": r._VALUATION_RULE,
        }
    )
    return (
        BacktestEquityPathAnalysisV3
        if isinstance(report, BacktestReportV3)
        else BacktestEquityPathAnalysisV2
    )(r._canonical_json(analysis) + b"\n")
