"""Read-only deterministic reports for completed Backtest attempts."""

from __future__ import annotations

import csv
import fcntl
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from pathlib import Path

import ea
from ea.core import (
    AUDIT_FRAME_DIGEST_DOMAIN,
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    FUNDING_APPLY_OUTCOME_DIGEST_DOMAIN,
    FUNDING_TRANSACTION_DIGEST_DOMAIN,
    AuditRecord,
    AuditRecordKind,
    CanonicalDecimal,
    InitialFunding,
    RunBinding,
    RunId,
    SettlementCurrency,
    audit_chain_head,
    audit_record_digest,
    canonical_audit_record_header_bytes,
    canonical_initial_funding_bytes,
    decode_audit_record,
    initial_funding_digest,
    require_quantized,
)
from ea.core.audit import (
    MAX_AUDIT_HEADER_BYTES,
    MAX_AUDIT_RECORDS,
    MAX_LARGE_AUDIT_PAYLOAD_BYTES,
)
from ea.experiments.audit import (
    AUDIT_JOURNAL_PREAMBLE,
    MAX_AUDIT_JOURNAL_BYTES,
)
from ea.product.backtest import BacktestResumeFailure, _load_verified_attempt
from ea.product.identity import semantic_outcome_sha256
from ea.product.scenario import BacktestStrategyId, LoadedBacktestScenario

_REPORT_SCHEMA = "ea.backtest-report.v1"
_VALUATION_RULE = "last-admitted-close-v1"
_EQUITY_RULE = "single-settlement-last-price-equity-v1"
_RETURN_RULE = "initial-funding-total-return-v1"
_FEE_RULE = "phase1-zero-commission-v1"
_RETURN_SCALE = 18
_SUPPORTED_RESULT_SCHEMA = "ea.backtest-single-run-result.v1"
_SUPPORTED_ATTEMPT_SCHEMA = "ea.backtest-resumable-attempt.v2"
_SUPPORTED_EXECUTION_POLICY = "phase1.next-bar-close.v1"
_SUPPORTED_EXECUTION_POLICY_SHA256 = "1" * 64
_RISK_CONTEXT_KEYS = {
    "available_cash_capacity",
    "binding_constraints",
    "declared_max_notional",
    "declared_max_order_quantity",
    "declared_max_position_quantity",
    "effective_max_order_quantity",
    "maximum_execution_price_bound",
    "notional_capacity",
}


BACKTEST_REPORT_FIELD_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("field", "source", "source_version", "rule"),
    ("schema", "reporter", _REPORT_SCHEMA, "fixed closed schema"),
    (
        "run.identity",
        "manifest.json + result.json + audit",
        _SUPPORTED_ATTEMPT_SCHEMA,
        "exact binding",
    ),
    (
        "run.code_distribution_runtime",
        "manifest.json",
        _SUPPORTED_ATTEMPT_SCHEMA,
        "lineage-bound projection",
    ),
    ("report.generator", "installed ea-quant", _REPORT_SCHEMA, "separate generator identity"),
    ("scenario", "manifest.json", "BacktestScenario v1", "canonical scenario digest"),
    (
        "data",
        "manifest.json + bound OHLCV",
        "ea-phase1-ohlcv-csv-v1",
        "fingerprint and file digest",
    ),
    (
        "instrument",
        "manifest.json scenario",
        "InstrumentExecutionSpec v1",
        "single instrument/currency",
    ),
    ("strategy", "manifest.json + result.json", "BacktestScenario v1", "exact supported strategy"),
    ("policies", "manifest.json + audit", _SUPPORTED_ATTEMPT_SCHEMA, "exact policy id and digest"),
    ("randomness", "manifest.json + result.json", "BacktestRandomness v1", "profile none"),
    (
        "economics.initial_funding",
        "funding.json + manifest.json",
        "ea-initial-funding-v1",
        "digest-bound",
    ),
    (
        "economics.ending_balances",
        "result.json + reconciliation audit",
        _SUPPORTED_RESULT_SCHEMA,
        "completed snapshot",
    ),
    (
        "valuation.last_price",
        "bound OHLCV + admitted matcher audit",
        "ea-phase1-ohlcv-csv-v1",
        _VALUATION_RULE,
    ),
    ("economics.equity", "ending cash + valued position", _REPORT_SCHEMA, _EQUITY_RULE),
    (
        "economics.net_pnl",
        "equity + initial funding",
        _REPORT_SCHEMA,
        "equity minus initial funding",
    ),
    ("economics.total_return", "net P&L + initial funding", _REPORT_SCHEMA, _RETURN_RULE),
    (
        "economics.counts",
        "unique audit economic ids",
        "ea-audit-record v1",
        "unique order/fill identities",
    ),
    (
        "economics.fees",
        "Phase 1 fill contract + complete fill ids",
        "Phase 1 execution v1",
        _FEE_RULE,
    ),
    (
        "completion",
        "terminal/reconciliation audit + result.json",
        "ea.audit-run-terminal.v2",
        "success and full match",
    ),
    (
        "semantic_outcome_sha256",
        "result.json",
        "ea.backtest-semantic-outcome.v1",
        "accepted projection",
    ),
)


class BacktestReportError(ValueError):
    """A completed attempt cannot be safely read or published as a report."""


@dataclass(frozen=True, slots=True)
class BacktestReportV1:
    """Validated canonical bytes for one closed report schema."""

    canonical_bytes: bytes
    summary_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.canonical_bytes) is not bytes or type(self.summary_bytes) is not bytes:
            raise TypeError("BacktestReportV1 artifacts must be exact bytes")
        document = _decode_canonical(self.canonical_bytes, newline=True)
        if document.get("schema") != _REPORT_SCHEMA or document.get("schema_version") != 1:
            raise BacktestReportError("BacktestReportV1 schema is invalid")
        if not self.summary_bytes.endswith(b"\n"):
            raise BacktestReportError("BacktestReportV1 summary is invalid")

    @property
    def document(self) -> dict[str, object]:
        return _decode_canonical(self.canonical_bytes, newline=True)


@dataclass(frozen=True, slots=True)
class BacktestReportResult:
    status: str
    output_directory: Path
    report: BacktestReportV1


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _decode_canonical(payload: bytes, *, newline: bool) -> dict[str, object]:
    body = payload[:-1] if newline and payload.endswith(b"\n") else payload
    if newline and (not payload.endswith(b"\n") or body.endswith(b"\n")):
        raise ValueError("canonical document newline is invalid")
    try:
        document = json.loads(
            body.decode("ascii"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("canonical document is invalid") from None
    if type(document) is not dict or _canonical_json(document) != body:
        raise ValueError("canonical document bytes conflict")
    return document


def _read_regular(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise OSError("source is not one regular file")
        return path.read_bytes()
    except OSError:
        raise BacktestReportError("completed attempt evidence is invalid") from None


def _exact_keys(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("document fields conflict")
    return value


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("document object is invalid")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("text field is invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("integer field is invalid")
    return value


def _identity(value: object, *, run_id: str, owner_kind: str) -> tuple[str, int]:
    document = _exact_keys(value, {"owner_kind", "owner_sequence", "run_id"})
    if document["run_id"] != run_id or document["owner_kind"] != owner_kind:
        raise ValueError("economic identity conflicts")
    return owner_kind, _integer(document["owner_sequence"])


@contextmanager
def _read_lease(attempt: Path) -> Iterator[None]:
    lock_fd: int | None = None
    try:
        lock_fd = os.open(
            attempt / "audit" / "writer-v1.lock",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
            or lock_stat.st_nlink != 1
        ):
            raise OSError("writer lock identity is invalid")
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    except (BlockingIOError, OSError):
        raise BacktestReportError("completed attempt evidence is unavailable") from None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_audit_journal(attempt: Path, binding: RunBinding) -> tuple[AuditRecord, ...]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            attempt / "audit" / "audit-v1.journal",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
            or not len(AUDIT_JOURNAL_PREAMBLE) <= value.st_size <= MAX_AUDIT_JOURNAL_BYTES
        ):
            raise ValueError("audit journal identity is invalid")
        if _read_exact(descriptor, len(AUDIT_JOURNAL_PREAMBLE)) != AUDIT_JOURNAL_PREAMBLE:
            raise ValueError("audit journal preamble is invalid")
        records: list[AuditRecord] = []
        keys: set[tuple[object, object, object]] = set()
        previous_record = EMPTY_RECORD_SHA256
        previous_chain = EMPTY_CHAIN_HEAD_SHA256
        consumed = len(AUDIT_JOURNAL_PREAMBLE)
        terminal = False
        while consumed < value.st_size:
            raw_header_length = _read_exact(descriptor, 8)
            if len(raw_header_length) != 8:
                raise ValueError("audit frame is incomplete")
            header_length = int.from_bytes(raw_header_length, "big")
            if not 1 <= header_length <= MAX_AUDIT_HEADER_BYTES:
                raise ValueError("audit header length is invalid")
            header = _read_exact(descriptor, header_length)
            raw_payload_length = _read_exact(descriptor, 8)
            if len(header) != header_length or len(raw_payload_length) != 8:
                raise ValueError("audit frame is incomplete")
            payload_length = int.from_bytes(raw_payload_length, "big")
            if not 1 <= payload_length <= MAX_LARGE_AUDIT_PAYLOAD_BYTES:
                raise ValueError("audit payload length is invalid")
            payload = _read_exact(descriptor, payload_length)
            checksum = _read_exact(descriptor, 32)
            if len(payload) != payload_length or len(checksum) != 32:
                raise ValueError("audit frame is incomplete")
            framed = raw_header_length + header + raw_payload_length + payload
            if checksum != sha256(AUDIT_FRAME_DIGEST_DOMAIN + framed).digest():
                raise ValueError("audit frame checksum conflicts")
            record = decode_audit_record(
                binding=binding,
                canonical_header=header,
                canonical_payload=payload,
            )
            key = (record.record_kind, record.subject_kind, record.subject_sha256)
            if (
                terminal
                or record.record_id.owner_sequence != len(records) + 1
                or record.previous_record_sha256 != previous_record
                or record.previous_chain_head_sha256 != previous_chain
                or key in keys
                or len(records) >= MAX_AUDIT_RECORDS
            ):
                raise ValueError("audit sequence or chain conflicts")
            records.append(record)
            keys.add(key)
            previous_record = audit_record_digest(record)
            previous_chain = audit_chain_head(record)
            terminal = record.record_kind is AuditRecordKind.RUN_TERMINAL
            consumed += len(framed) + 32
        if consumed != value.st_size or not records or not terminal:
            raise ValueError("audit terminal evidence is invalid")
        return tuple(records)
    except (OSError, ValueError):
        raise BacktestReportError("completed attempt audit evidence is invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _audit_export(records: tuple[AuditRecord, ...]) -> bytes:
    return b"".join(
        _canonical_json(
            {
                "chain_head_sha256": audit_chain_head(record).value,
                "header": json.loads(canonical_audit_record_header_bytes(record)),
                "payload": json.loads(record.canonical_payload),
                "record_sha256": audit_record_digest(record).value,
            }
        )
        + b"\n"
        for record in records
    )


def _payload(record: AuditRecord) -> dict[str, object]:
    return _decode_canonical(record.canonical_payload, newline=False)


def _domain_digest(domain: bytes, document: object) -> str:
    return sha256(domain + _canonical_json(document)).hexdigest()


def _validate_funding(
    document: dict[str, object],
    *,
    manifest: dict[str, object],
    scenario: LoadedBacktestScenario,
    run_id: str,
) -> str:
    _exact_keys(
        document,
        {
            "amount",
            "apply_outcome",
            "currency",
            "funding_apply_outcome_sha256",
            "funding_sha256",
            "funding_transaction_sha256",
            "initial_funding",
            "ledger_sequence",
            "status",
            "transaction",
        },
    )
    manifest_funding = _exact_keys(manifest["funding"], {"amount", "currency", "funding_sha256"})
    amount = CanonicalDecimal(_text(document["amount"]))
    currency = SettlementCurrency(_text(document["currency"]))
    scenario_run_id = RunId(run_id)
    expected_funding = InitialFunding(scenario_run_id, currency, amount)
    expected_initial = json.loads(canonical_initial_funding_bytes(expected_funding))
    funding_sha256 = initial_funding_digest(expected_funding).value
    if (
        document["status"] != "applied"
        or document["ledger_sequence"] != 1
        or document["initial_funding"] != expected_initial
        or document["funding_sha256"] != funding_sha256
        or manifest_funding
        != {"amount": amount.text, "currency": currency.code, "funding_sha256": funding_sha256}
        or scenario_run_id.value != run_id
    ):
        raise ValueError("initial funding conflicts")
    transaction = _exact_keys(
        document["transaction"],
        {
            "amount",
            "canonicalization",
            "currency",
            "entry_id",
            "funding_sha256",
            "instrument_spec_set_id",
            "instrument_spec_set_sha256",
            "ledger_sequence",
            "message_type",
            "postings",
            "previous_transaction_sha256",
            "run_id",
            "schema_version",
        },
    )
    scenario_document = _exact_keys(
        manifest["scenario"], {"canonical", "path", "sha256", "source_file_sha256"}
    )
    canonical_scenario = _exact_keys(
        scenario_document["canonical"],
        {
            "canonicalization",
            "data",
            "execution",
            "funding",
            "instrument",
            "randomness_profile",
            "risk",
            "schema_version",
            "strategy",
        },
    )
    instrument = _exact_keys(
        canonical_scenario["instrument"],
        {
            "contract_multiplier",
            "currency_quantum",
            "price_quantum",
            "quantity_quantum",
            "settlement_currency",
            "specification_id",
            "specification_set_id",
            "symbol",
            "venue",
        },
    )
    postings = transaction["postings"]
    expected_postings = [
        {
            "account": "portfolio.cash",
            "amount": amount.text,
            "commodity": {"currency": currency.code, "kind": "currency"},
        },
        {
            "account": "external.settlement",
            "amount": _negated(amount).text,
            "commodity": {"currency": currency.code, "kind": "currency"},
        },
    ]
    _identity(transaction["entry_id"], run_id=run_id, owner_kind="ledger.entry")
    if (
        transaction["amount"] != amount.text
        or transaction["currency"] != currency.code
        or transaction["funding_sha256"] != funding_sha256
        or transaction["instrument_spec_set_id"] != instrument["specification_set_id"]
        or transaction["instrument_spec_set_sha256"] != manifest["instrument_spec_set_sha256"]
        or transaction["ledger_sequence"] != 1
        or transaction["previous_transaction_sha256"] is not None
        or transaction["run_id"] != run_id
        or transaction["canonicalization"] != "ea-funding-transaction-v1"
        or transaction["message_type"] != "funding_transaction"
        or transaction["schema_version"] != 1
        or postings != expected_postings
    ):
        raise ValueError("funding transaction conflicts")
    transaction_sha256 = _domain_digest(FUNDING_TRANSACTION_DIGEST_DOMAIN, transaction)
    outcome = _exact_keys(
        document["apply_outcome"],
        {
            "after_snapshot_version",
            "before_snapshot_version",
            "canonicalization",
            "code",
            "existing_funding_sha256",
            "message_type",
            "run_id",
            "schema_version",
            "snapshot_sha256",
            "submitted_funding_sha256",
            "transaction_sha256",
        },
    )
    if (
        outcome["after_snapshot_version"] != 1
        or outcome["before_snapshot_version"] != 0
        or outcome["canonicalization"] != "ea-funding-apply-outcome-v1"
        or outcome["code"] != "ledger.applied"
        or outcome["existing_funding_sha256"] is not None
        or outcome["message_type"] != "funding_apply_outcome"
        or outcome["run_id"] != run_id
        or outcome["schema_version"] != 1
        or outcome["submitted_funding_sha256"] != funding_sha256
        or outcome["transaction_sha256"] != transaction_sha256
        or document["funding_transaction_sha256"] != transaction_sha256
        or document["funding_apply_outcome_sha256"]
        != _domain_digest(FUNDING_APPLY_OUTCOME_DIGEST_DOMAIN, outcome)
    ):
        raise ValueError("funding outcome conflicts")
    _text(outcome["snapshot_sha256"])
    return _text(outcome["snapshot_sha256"])


def _scaled(coefficient: int, scale: int) -> CanonicalDecimal:
    if coefficient == 0:
        return CanonicalDecimal("0")
    while scale and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale:
        digits = digits.rjust(scale + 1, "0")
        digits = f"{digits[:-scale]}.{digits[-scale:]}"
    return CanonicalDecimal(sign + digits)


def _negated(value: CanonicalDecimal) -> CanonicalDecimal:
    return _scaled(-value.coefficient, value.scale)


def _add(left: CanonicalDecimal, right: CanonicalDecimal) -> CanonicalDecimal:
    scale = max(left.scale, right.scale)
    return _scaled(
        left.coefficient * 10 ** (scale - left.scale)
        + right.coefficient * 10 ** (scale - right.scale),
        scale,
    )


def _subtract(left: CanonicalDecimal, right: CanonicalDecimal) -> CanonicalDecimal:
    return _add(left, _negated(right))


def _multiply(*values: CanonicalDecimal) -> CanonicalDecimal:
    coefficient = 1
    scale = 0
    for value in values:
        coefficient *= value.coefficient
        scale += value.scale
    return _scaled(coefficient, scale)


def _ratio(numerator: CanonicalDecimal, denominator: CanonicalDecimal) -> CanonicalDecimal:
    if denominator.coefficient <= 0:
        raise ValueError("return denominator is invalid")
    scaled_numerator = numerator.coefficient * 10 ** (_RETURN_SCALE + denominator.scale)
    scaled_denominator = denominator.coefficient * 10**numerator.scale
    sign = -1 if scaled_numerator < 0 else 1
    quotient, remainder = divmod(abs(scaled_numerator), scaled_denominator)
    doubled = remainder * 2
    if doubled > scaled_denominator or (doubled == scaled_denominator and quotient % 2):
        quotient += 1
    return _scaled(sign * quotient, _RETURN_SCALE)


def _decimal_from_source(text: str) -> CanonicalDecimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("market price is invalid") from None
    if not value.is_finite():
        raise ValueError("market price is non-finite")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"-0", ""}:
        rendered = "0"
    return CanonicalDecimal(rendered)


def _last_admitted_price(
    scenario: LoadedBacktestScenario,
    records: tuple[AuditRecord, ...],
) -> tuple[CanonicalDecimal, dict[str, object]]:
    market_payloads = [
        _payload(record)
        for record in records
        if record.record_kind is AuditRecordKind.MATCHER_DISPATCH_BATCH
        and _payload(record).get("dispatch_kind") == "market"
    ]
    if not market_payloads:
        raise ValueError("completed attempt has no admitted market data")
    root = _exact_keys(
        market_payloads[-1]["trigger_root_key"],
        {
            "adjustment",
            "available_at",
            "domain_rank",
            "event_time",
            "instrument",
            "interval_end",
            "interval_start",
            "kind_rank",
            "revision",
            "root_domain",
            "source",
            "source_sequence",
        },
    )
    instrument = _exact_keys(root["instrument"], {"symbol", "venue"})
    data_bytes = _read_regular(scenario.data_path)
    try:
        rows = list(csv.DictReader(StringIO(data_bytes.decode("utf-8"), newline=""), strict=True))
    except (UnicodeError, csv.Error):
        raise ValueError("market data cannot be decoded") from None
    matching = [
        row
        for row in rows
        if row.get("venue") == instrument["venue"]
        and row.get("symbol") == instrument["symbol"]
        and row.get("adjustment") == root["adjustment"]
        and row.get("interval_start") == root["interval_start"]
        and row.get("interval_end") == root["interval_end"]
        and row.get("source") == root["source"]
        and row.get("source_sequence") == str(root["source_sequence"])
        and row.get("revision") == str(root["revision"])
        and row.get("available_at") == root["available_at"]
    ]
    if len(matching) != 1:
        raise ValueError("admitted market identity conflicts")
    price = _decimal_from_source(_text(matching[0].get("close")))
    require_quantized(
        price,
        scenario.spec_set.require(scenario.instrument).price_quantum,
        field_name="last_price",
    )
    return price, {
        "adjustment": root["adjustment"],
        "available_at": root["available_at"],
        "price": price.text,
        "revision": root["revision"],
        "rule": _VALUATION_RULE,
        "source": root["source"],
        "source_sequence": root["source_sequence"],
    }


def _validate_result(
    result: dict[str, object],
    *,
    manifest: dict[str, object],
    scenario: LoadedBacktestScenario,
    records: tuple[AuditRecord, ...],
    funded_snapshot_sha256: str,
    run_id: str,
    lineage: str,
) -> tuple[int, int, str]:
    _exact_keys(
        result,
        {
            "audit_chain_head_sha256",
            "ending_cash",
            "ending_positions",
            "fill",
            "initial_funding",
            "ledger_sequence",
            "lineage_sha256",
            "order",
            "randomness",
            "reconciliation",
            "risk",
            "run_id",
            "scenario_sha256",
            "schema",
            "semantic_outcome_sha256",
            "status",
            "strategy",
            "terminal_state",
        },
    )
    scenario_manifest = _exact_keys(
        manifest["scenario"], {"canonical", "path", "sha256", "source_file_sha256"}
    )
    if (
        result["schema"] != _SUPPORTED_RESULT_SCHEMA
        or result["status"] != "success"
        or result["terminal_state"] != "completed"
        or result["run_id"] != run_id
        or result["lineage_sha256"] != lineage
        or result["scenario_sha256"] != scenario_manifest["sha256"]
        or result["randomness"] != manifest["randomness"]
        or result["audit_chain_head_sha256"] != audit_chain_head(records[-1]).value
    ):
        raise ValueError("completed result identity conflicts")
    initial = _exact_keys(
        result["initial_funding"], {"amount", "currency", "ledger_sequence", "status"}
    )
    if (
        initial["amount"] != scenario.initial_cash.text
        or initial["currency"] != scenario.funding_currency.code
        or initial["ledger_sequence"] != 1
        or initial["status"] != "applied"
    ):
        raise ValueError("result funding conflicts")
    cash = result["ending_cash"]
    if type(cash) is not list or len(cash) != 1:
        raise ValueError("ending cash is unsupported")
    cash_entry = _exact_keys(cash[0], {"amount", "currency"})
    CanonicalDecimal(_text(cash_entry["amount"]))
    if cash_entry["currency"] != scenario.funding_currency.code:
        raise ValueError("ending cash currency conflicts")
    positions = result["ending_positions"]
    if type(positions) is not list or len(positions) > 1:
        raise ValueError("ending positions are unsupported")
    for position in positions:
        entry = _exact_keys(position, {"quantity", "symbol", "venue"})
        CanonicalDecimal(_text(entry["quantity"]))
        if (
            entry["symbol"] != scenario.instrument.symbol
            or entry["venue"] != scenario.instrument.venue.code
        ):
            raise ValueError("ending position identity conflicts")
    strategy = _exact_keys(result["strategy"], {"id", "signal"})
    if strategy["id"] != scenario.strategy_id.value:
        raise ValueError("result strategy conflicts")
    order_ids: set[tuple[str, int]] = set()
    fill_ids: set[tuple[str, int]] = set()
    committed_fill_ids: set[tuple[str, int]] = set()
    reconciliation_payloads: list[dict[str, object]] = []
    terminal: dict[str, object] | None = None
    final_snapshot_sha256: str | None = None
    for record in records:
        payload = _payload(record)
        if record.record_kind is AuditRecordKind.RUN_PREPARED:
            if payload.get("manifest_sha256") != manifest_digest(manifest):
                raise ValueError("prepared manifest digest conflicts")
        elif record.record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
            order_ids.add(
                _identity(payload["order_id"], run_id=run_id, owner_kind="execution.order")
            )
        elif record.record_kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME:
            if payload.get("action") == "accepted":
                fill_doc = _exact_keys(payload["fill"], {"fill_id", "fill_sha256"})
                fill_ids.add(
                    _identity(fill_doc["fill_id"], run_id=run_id, owner_kind="execution.fill")
                )
        elif record.record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME:
            if payload.get("action") == "effect_committed":
                committed_fill_ids.add(
                    _identity(payload["fill_id"], run_id=run_id, owner_kind="execution.fill")
                )
                original = _exact_keys(
                    payload["original_ledger_apply_outcome"],
                    {
                        "after_snapshot_version",
                        "before_snapshot_version",
                        "canonicalization",
                        "code",
                        "conflict_kind",
                        "entry_index_binding",
                        "fact_index_binding",
                        "failure_stage",
                        "fill_index_binding",
                        "message_type",
                        "run_id",
                        "schema_version",
                        "snapshot_sha256",
                        "submitted_fill_id",
                        "submitted_fill_sha256",
                        "transaction_entry_id",
                        "transaction_sha256",
                    },
                )
                if (
                    original["code"] != "ledger.applied"
                    or payload["before_snapshot_sha256"] != funded_snapshot_sha256
                ):
                    raise ValueError("ledger handoff conflicts")
                final_snapshot_sha256 = _text(payload["after_snapshot_sha256"])
        elif record.record_kind is AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME:
            reconciliation_payloads.append(payload)
        elif record.record_kind is AuditRecordKind.RUN_TERMINAL:
            terminal = payload
    if terminal is None or terminal.get("terminal_kind") != "success":
        raise ValueError("terminal outcome conflicts")
    terminal_snapshot = _text(terminal.get("final_published_snapshot_sha256"))
    if final_snapshot_sha256 is None:
        final_snapshot_sha256 = funded_snapshot_sha256
    if terminal_snapshot != final_snapshot_sha256:
        raise ValueError("terminal snapshot conflicts")
    expected_reconciliations = 2 if scenario.strategy_id is BacktestStrategyId.BOUNDED_LONG else 1
    if len(reconciliation_payloads) != expected_reconciliations or any(
        item.get("outcome_code") != "reconciliation.match"
        or item.get("requested_action") != "none"
        or item.get("run_id") != run_id
        or item.get("ledger_sequence") != result["ledger_sequence"]
        or item.get("local_snapshot_sha256") != final_snapshot_sha256
        for item in reconciliation_payloads
    ):
        raise ValueError("reconciliation evidence is incomplete")
    if fill_ids != committed_fill_ids or len(order_ids) > 1 or len(fill_ids) > 1:
        raise ValueError("economic audit identities conflict")
    if result["order"] is None:
        if order_ids:
            raise ValueError("order count conflicts")
    else:
        order = _exact_keys(result["order"], {"order_id", "quantity", "side"})
        if {_identity(order["order_id"], run_id=run_id, owner_kind="execution.order")} != order_ids:
            raise ValueError("result order identity conflicts")
        CanonicalDecimal(_text(order["quantity"]))
        _text(order["side"])
    if result["fill"] is None:
        if fill_ids:
            raise ValueError("fill count conflicts")
    else:
        fill = _exact_keys(result["fill"], {"fill_id", "price", "quantity", "side"})
        if {_identity(fill["fill_id"], run_id=run_id, owner_kind="execution.fill")} != fill_ids:
            raise ValueError("result fill identity conflicts")
        CanonicalDecimal(_text(fill["price"]))
        CanonicalDecimal(_text(fill["quantity"]))
        _text(fill["side"])
    reconciliation = _exact_keys(result["reconciliation"], {"cash", "position"})
    expected_position = "match" if positions else "not_required_empty"
    if reconciliation != {"cash": "match", "position": expected_position}:
        raise ValueError("result reconciliation conflicts")
    manifest_risk = _exact_keys(manifest["risk"], {"context", "policy_id", "policy_sha256"})
    risk_context = _exact_keys(manifest_risk["context"], _RISK_CONTEXT_KEYS)
    risk = _exact_keys(result["risk"], _RISK_CONTEXT_KEYS | {"decision", "reason"})
    if any(risk.get(key) != value for key, value in risk_context.items()):
        raise ValueError("result risk context conflicts")
    if scenario.strategy_id is BacktestStrategyId.ALWAYS_FLAT:
        if (
            order_ids
            or fill_ids
            or result["ledger_sequence"] != 1
            or risk["decision"] != "not_applicable"
            or risk["reason"] is not None
        ):
            raise ValueError("flat result economics conflict")
    elif (
        len(order_ids) != 1
        or len(fill_ids) != 1
        or result["ledger_sequence"] != 2
        or risk["decision"] not in {"allow", "resize"}
        or type(risk["reason"]) is not str
    ):
        raise ValueError("bounded-long result economics conflict")
    fill_semantic: dict[str, object] | None = None
    if result["fill"] is not None:
        fill_result = _exact_keys(result["fill"], {"fill_id", "price", "quantity", "side"})
        fill_semantic = {key: fill_result[key] for key in ("price", "quantity", "side")}
    order_semantic: dict[str, object] | None = None
    if result["order"] is not None:
        order_result = _exact_keys(result["order"], {"order_id", "quantity", "side"})
        order_semantic = {key: order_result[key] for key in ("quantity", "side")}
    semantic = {
        "ending_cash": cash,
        "ending_positions": positions,
        "fill": fill_semantic,
        "initial_funding": {"amount": initial["amount"], "currency": initial["currency"]},
        "lineage_sha256": lineage,
        "order": order_semantic,
        "reconciliation": reconciliation,
        "risk": risk,
        "schema": "ea.backtest-semantic-outcome.v1",
        "strategy": strategy,
        "terminal_state": "completed",
    }
    if result["semantic_outcome_sha256"] != semantic_outcome_sha256(semantic).value:
        raise ValueError("semantic outcome digest conflicts")
    return len(order_ids), len(fill_ids), terminal_snapshot


def manifest_digest(manifest: Mapping[str, object]) -> str:
    return sha256(_canonical_json(manifest)).hexdigest()


def _build_report(
    *,
    manifest: dict[str, object],
    result: dict[str, object],
    scenario: LoadedBacktestScenario,
    records: tuple[AuditRecord, ...],
    order_count: int,
    fill_count: int,
) -> dict[str, object]:
    price, valuation = _last_admitted_price(scenario, records)
    ending_cash_entries = result["ending_cash"]
    if type(ending_cash_entries) is not list or len(ending_cash_entries) != 1:
        raise ValueError("ending cash is invalid")
    cash_entry = _exact_keys(ending_cash_entries[0], {"amount", "currency"})
    ending_cash = CanonicalDecimal(_text(cash_entry["amount"]))
    positions = result["ending_positions"]
    if type(positions) is not list:
        raise ValueError("ending positions are invalid")
    quantity = CanonicalDecimal("0")
    if positions:
        position = _exact_keys(positions[0], {"quantity", "symbol", "venue"})
        quantity = CanonicalDecimal(_text(position["quantity"]))
    specification = scenario.spec_set.require(scenario.instrument)
    fill = result["fill"]
    order = result["order"]
    execution: dict[str, object]
    if scenario.strategy_id is BacktestStrategyId.ALWAYS_FLAT:
        if (
            ending_cash != scenario.initial_cash
            or positions
            or fill is not None
            or order is not None
        ):
            raise ValueError("flat accounting evidence conflicts")
        execution = {"fill": None, "order": None}
    else:
        fill_document = _exact_keys(fill, {"fill_id", "price", "quantity", "side"})
        order_document = _exact_keys(order, {"order_id", "quantity", "side"})
        fill_price = CanonicalDecimal(_text(fill_document["price"]))
        fill_quantity = CanonicalDecimal(_text(fill_document["quantity"]))
        if (
            fill_document["side"] != "buy"
            or order_document["side"] != "buy"
            or order_document["quantity"] != fill_quantity.text
            or quantity != fill_quantity
        ):
            raise ValueError("bounded-long execution evidence conflicts")
        fill_notional = _multiply(fill_price, fill_quantity, specification.contract_multiplier)
        require_quantized(fill_notional, specification.currency_quantum, field_name="fill_notional")
        if ending_cash != _subtract(scenario.initial_cash, fill_notional):
            raise ValueError("ending cash conflicts with fill and zero-fee policy")
        execution = {
            "fill": {
                "price": fill_price.text,
                "quantity": fill_quantity.text,
                "side": "buy",
            },
            "order": {"quantity": fill_quantity.text, "side": "buy"},
        }
    position_value = _multiply(price, quantity, specification.contract_multiplier)
    require_quantized(position_value, specification.currency_quantum, field_name="position_value")
    equity = _add(ending_cash, position_value)
    net_pnl = _subtract(equity, scenario.initial_cash)
    total_return = _ratio(net_pnl, scenario.initial_cash)
    valuation["position_value"] = position_value.text
    scenario_manifest = _exact_keys(
        manifest["scenario"], {"canonical", "path", "sha256", "source_file_sha256"}
    )
    canonical_scenario = _exact_keys(
        scenario_manifest["canonical"],
        {
            "canonicalization",
            "data",
            "execution",
            "funding",
            "instrument",
            "randomness_profile",
            "risk",
            "schema_version",
            "strategy",
        },
    )
    scenario_data = _exact_keys(canonical_scenario["data"], {"end_utc", "fingerprint", "start_utc"})
    return {
        "completion": {
            "audit_chain_head_sha256": result["audit_chain_head_sha256"],
            "reconciliation": result["reconciliation"],
            "semantic_outcome_sha256": result["semantic_outcome_sha256"],
            "terminal_outcome": "success",
        },
        "economics": {
            "counts": {"fills": fill_count, "orders": order_count},
            "currency": scenario.funding_currency.code,
            "ending_cash": result["ending_cash"],
            "ending_positions": positions,
            "equity": {"amount": equity.text, "rule": _EQUITY_RULE},
            "execution": execution,
            "fees": {
                "amount": "0",
                "count": fill_count,
                "currency": scenario.funding_currency.code,
                "rule": _FEE_RULE,
            },
            "initial_funding": {
                "amount": scenario.initial_cash.text,
                "currency": scenario.funding_currency.code,
            },
            "net_pnl": {"amount": net_pnl.text, "rule": "equity-minus-initial-funding-v1"},
            "total_return": {
                "denominator": "initial_funding",
                "precision": "18-decimal-places",
                "rounding": "half_even",
                "rule": _RETURN_RULE,
                "unit": "ratio",
                "value": total_return.text,
            },
            "valuation": valuation,
        },
        "field_sources": [
            {"field": row[0], "rule": row[3], "source": row[1], "source_version": row[2]}
            for row in BACKTEST_REPORT_FIELD_SOURCES[1:]
        ],
        "lineage_sha256": result["lineage_sha256"],
        "report_generator": {"distribution": "ea-quant", "version": ea.__version__},
        "run_id": result["run_id"],
        "schema": _REPORT_SCHEMA,
        "schema_version": 1,
        "source": {
            "code_sha256": manifest["code_sha256"],
            "data": manifest["data"],
            "distribution": manifest["distribution"],
            "execution": manifest["execution"],
            "instrument": canonical_scenario["instrument"],
            "instrument_spec_set_sha256": manifest["instrument_spec_set_sha256"],
            "randomness": manifest["randomness"],
            "replay_window": {
                "end_exclusive": scenario_data["end_utc"],
                "selection_field": "available_at",
                "start_inclusive": scenario_data["start_utc"],
                "timezone": "UTC",
            },
            "risk": manifest["risk"],
            "runtime": manifest["runtime"],
            "scenario_sha256": scenario_manifest["sha256"],
            "strategy": canonical_scenario["strategy"],
        },
    }


def _summary(document: dict[str, object]) -> bytes:
    economics = _object(document["economics"])
    valuation = _object(economics["valuation"])
    counts = _object(economics["counts"])
    fees = _object(economics["fees"])
    initial_funding = _object(economics["initial_funding"])
    equity = _object(economics["equity"])
    net_pnl = _object(economics["net_pnl"])
    total_return = _object(economics["total_return"])
    ending_cash = economics["ending_cash"]
    if type(ending_cash) is not list or len(ending_cash) != 1:
        raise ValueError("summary ending cash is invalid")
    ending_cash_entry = _object(ending_cash[0])
    completion = _object(document["completion"])
    reconciliation = _object(completion["reconciliation"])
    lines = (
        "EA Backtest Report V1",
        f"Run ID: {document['run_id']}",
        f"Lineage SHA-256: {document['lineage_sha256']}",
        f"Terminal outcome: {completion['terminal_outcome']}",
        f"Currency: {economics['currency']}",
        f"Initial funding: {initial_funding['amount']}",
        f"Ending cash: {ending_cash_entry['amount']}",
        f"Last admitted price: {valuation['price']} at {valuation['available_at']}",
        f"Position value: {valuation['position_value']}",
        f"Last-price equity: {equity['amount']}",
        f"Net P&L: {net_pnl['amount']}",
        f"Total return: {total_return['value']}",
        f"Orders/Fills: {counts['orders']}/{counts['fills']}",
        f"Fees: {fees['amount']} ({fees['count']} commission entries)",
        f"Reconciliation: cash={reconciliation['cash']}, position={reconciliation['position']}",
        f"Semantic outcome SHA-256: {completion['semantic_outcome_sha256']}",
    )
    return ("\n".join(lines) + "\n").encode("ascii")


def _safe_attempt(run_dir: Path) -> Path:
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise BacktestReportError("report run directory must be an absolute path")
    try:
        if run_dir.is_symlink() or run_dir.resolve(strict=True) != run_dir or not run_dir.is_dir():
            raise OSError("attempt identity is invalid")
    except OSError:
        raise BacktestReportError("report run directory identity is invalid") from None
    return run_dir


def _safe_output(attempt: Path, output_dir: Path) -> Path:
    if not isinstance(output_dir, Path) or not output_dir.is_absolute():
        raise BacktestReportError("report output directory must be an absolute path")
    try:
        prospective = output_dir.resolve(strict=False)
        if prospective != output_dir or prospective == attempt or attempt in prospective.parents:
            raise OSError("output overlaps source")
        if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
            raise OSError("output identity is invalid")
    except OSError:
        raise BacktestReportError("report output directory identity is invalid") from None
    return output_dir


def _verify_existing(output: Path, report_bytes: bytes, summary_bytes: bytes) -> bool:
    if not output.exists():
        return False
    try:
        entries = {entry.name for entry in output.iterdir()}
        if entries != {"report.json", "summary.txt"}:
            raise OSError("report directory is incomplete")
        if (
            _read_regular(output / "report.json") != report_bytes
            or _read_regular(output / "summary.txt") != summary_bytes
        ):
            raise OSError("report bytes conflict")
    except (BacktestReportError, OSError):
        raise BacktestReportError("existing report output conflicts") from None
    return True


def _write_report_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("report write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(output: Path, report_bytes: bytes, summary_bytes: bytes) -> None:
    try:
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if output.parent.resolve(strict=True) != output.parent:
            raise OSError("report parent identity changed")
    except OSError:
        raise BacktestReportError("report output directory identity is invalid") from None
    if _verify_existing(output, report_bytes, summary_bytes):
        return
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.pending-", dir=output.parent))
    try:
        os.chmod(staging, 0o700)
        _write_report_file(staging / "report.json", report_bytes)
        _write_report_file(staging / "summary.txt", summary_bytes)
        _fsync_directory(staging)
        if output.exists():
            raise OSError("report destination appeared during publication")
        os.rename(staging, output)
        _fsync_directory(output.parent)
    except OSError:
        raise BacktestReportError("report output could not be atomically published") from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def generate_backtest_report(run_dir: Path, output_dir: Path) -> BacktestReportResult:
    """Verify a completed attempt without execution and publish deterministic report bytes."""
    attempt = _safe_attempt(run_dir)
    output = _safe_output(attempt, output_dir)
    try:
        with _read_lease(attempt):
            scenario, run_id, lineage, _risk_policy, _risk_context, canonical_manifest = (
                _load_verified_attempt(attempt)
            )
            manifest = _decode_canonical(canonical_manifest.canonical_bytes, newline=False)
            if manifest["schema"] != _SUPPORTED_ATTEMPT_SCHEMA:
                raise ValueError("attempt schema is unsupported")
            if any(
                (attempt / name).exists()
                for name in ("failure.json", "result.pending", "result.publication.json")
            ):
                raise ValueError("attempt terminal publication is ambiguous")
            records = _read_audit_journal(attempt, canonical_manifest.binding)
            if _read_regular(attempt / "audit.jsonl") != _audit_export(records):
                raise ValueError("published audit conflicts with journal")
            funding = _decode_canonical(_read_regular(attempt / "funding.json"), newline=True)
            funded_snapshot = _validate_funding(
                funding,
                manifest=manifest,
                scenario=scenario,
                run_id=run_id.value,
            )
            result = _decode_canonical(_read_regular(attempt / "result.json"), newline=True)
            order_count, fill_count, _terminal_snapshot = _validate_result(
                result,
                manifest=manifest,
                scenario=scenario,
                records=records,
                funded_snapshot_sha256=funded_snapshot,
                run_id=run_id.value,
                lineage=lineage.value,
            )
            execution = _exact_keys(manifest["execution"], {"policy_id", "policy_sha256"})
            if execution != {
                "policy_id": _SUPPORTED_EXECUTION_POLICY,
                "policy_sha256": _SUPPORTED_EXECUTION_POLICY_SHA256,
            }:
                raise ValueError("execution fee policy is unsupported")
            report = _build_report(
                manifest=manifest,
                result=result,
                scenario=scenario,
                records=records,
                order_count=order_count,
                fill_count=fill_count,
            )
            report_model = BacktestReportV1(
                canonical_bytes=_canonical_json(report) + b"\n",
                summary_bytes=_summary(report),
            )
            _publish(output, report_model.canonical_bytes, report_model.summary_bytes)
    except BacktestReportError:
        raise
    except (BacktestResumeFailure, KeyError, TypeError, ValueError):
        raise BacktestReportError("completed attempt evidence is invalid") from None
    return BacktestReportResult("success", output, report_model)


__all__ = [
    "BACKTEST_REPORT_FIELD_SOURCES",
    "BacktestReportError",
    "BacktestReportResult",
    "BacktestReportV1",
    "generate_backtest_report",
]
