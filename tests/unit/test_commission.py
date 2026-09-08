from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ea.core import CanonicalDecimal, ExecutionFact, Fill
from ea.product import load_backtest_scenario, run_backtest_scenario
from ea.product.reporting import generate_backtest_report
from unit.test_backtest_report import _priced_scenario

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


def commission_scenario(root: Path, bps: str = "100") -> Path:
    path = _priced_scenario(root)
    document = yaml.safe_load(path.read_text())
    document["execution"]["commission"] = {
        "policy": "deterministic-commission-v1",
        "commission_bps": bps,
    }
    path.write_text(yaml.safe_dump(document))
    return path


@pytest.mark.parametrize(
    ("price", "quantity", "multiplier", "bps", "quantum", "expected"),
    [
        ("100", "2", "1", "0", "0.01", "0"),
        ("100", "2", "1", "100", "0.01", "2"),
        ("100", "2", "1", "10000", "0.01", "200"),
        ("1", "1", "1", "50", "0.01", "0"),
        ("3", "1", "1", "50", "0.01", "0.02"),
        ("100", "2", "10", "100", "0.01", "20"),
        ("1", "1", "1", "750", "0.05", "0.1"),
    ],
)
def test_fee_math(
    price: str, quantity: str, multiplier: str, bps: str, quantum: str, expected: str
) -> None:
    from ea.core.commission import commission_amount

    assert (
        commission_amount(
            *(CanonicalDecimal(x) for x in (price, quantity, multiplier, bps, quantum))
        ).text
        == expected
    )


@pytest.mark.parametrize("bps", ["-1", "10001", "NaN", "Infinity", "01", "1e2", "1.0"])
def test_invalid_rate(tmp_path: Path, bps: str) -> None:
    with pytest.raises(ValueError):
        load_backtest_scenario(commission_scenario(tmp_path, bps))


def test_positive_commission_real_report(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(commission_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    generate_backtest_report(attempt, tmp_path / "report")
    report = json.loads((tmp_path / "report/report.json").read_bytes())
    # Fill 2 shares at 101.50; notional 203, raw fee 2.03 at 100 bps.
    # Mark 110, initial cash 10000: cash 9794.97, position 220, net P&L 14.97.
    assert report["economics"]["fees"]["amount"] == "2.03"
    assert report["economics"]["fees"]["count"] == 1
    assert report["economics"]["net_pnl"]["amount"] == "14.97"
    assert report["economics"]["equity"]["amount"] == "10014.97"
    assert report["economics"]["total_return"]["value"] == "0.001497"
    assert report["economics"]["ending_cash"][0]["amount"] == "9794.97"
    assert (
        next(row for row in report["field_sources"] if row["field"] == "economics.fees")["rule"]
        == "deterministic-commission-v1"
    )


def test_legacy_registered_identity_unchanged() -> None:
    for name, digest in [
        ("bounded-long", "d15009d4c620666c32b945448731064c37d54c2f5fedf0da7b97d2f63cd884b0"),
        ("flat", "e739c15e34240498fd553a658c5e365bb4e7c75e398b9abdd4e28b85b5a8bb39"),
    ]:
        scenario = load_backtest_scenario(Path(f"examples/web-scenarios/{name}.yaml").resolve())
        assert scenario.scenario_sha256.value == digest
        assert json.loads(scenario.canonical_bytes)["execution"] == {
            "policy": "phase1.next-bar-close.v1"
        }


def test_per_fill_rounding_precedes_sum() -> None:
    from ea.core.commission import commission_amount

    # Each 1 USD Fill at 50 bps yields 0.005 -> 0.00, twice => 0, not 0.01.
    values = [
        commission_amount(*(CanonicalDecimal(x) for x in ("1", "1", "1", "50", "0.01")))
        for _ in range(2)
    ]
    assert [value.text for value in values] == ["0", "0"]


def fee_fact(bps: str = "100", sequence: int = 1) -> ExecutionFact:
    from ea.core import ExternalFactId, OrderSide, create_trade_execution_fact
    from unit.test_portfolio_ledger import INSTRUMENT, PROVENANCE, SOURCE, TIME, _spec_set

    return create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId(str(sequence)),
        occurred_at=TIME,
        provenance=PROVENANCE,
        spec_set=_spec_set(),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal("2"),
        price=CanonicalDecimal("100"),
        commission_bps=CanonicalDecimal(bps),
    )


def fee_fill(bps: str = "100", sequence: int = 1) -> Fill:
    from ea.core import EconomicOwnerKind, create_fill
    from unit.test_portfolio_ledger import _id, _spec_set

    return create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, sequence),
        fact=fee_fact(bps, sequence),
        spec_set=_spec_set(),
    )


@pytest.mark.parametrize("bps", ["0", "100", "10000"])
def test_fee_fact_fill_codec(bps: str) -> None:
    from ea.core import (
        IndependentFactDecodeContext,
        canonical_execution_fact_bytes,
        canonical_fill_bytes,
        decode_execution_fact,
        decode_fill,
    )
    from unit.test_portfolio_ledger import _spec_set

    fact = fee_fact(bps)
    decoded = decode_execution_fact(
        canonical_execution_fact_bytes(fact), context=IndependentFactDecodeContext(_spec_set())
    )
    fill = fee_fill(bps)
    assert decode_fill(canonical_fill_bytes(fill), fact=decoded, spec_set=_spec_set()) == fill
    assert len(fill.fees) == 1
    assert fill.fees[0].commission_bps == CanonicalDecimal(bps)


@pytest.mark.parametrize("mutation", ["amount", "currency", "policy", "rate"])
def test_fee_codec_tampering(mutation: str) -> None:
    from ea.core import (
        IndependentFactDecodeContext,
        canonical_execution_fact_bytes,
        decode_execution_fact,
    )
    from unit.test_portfolio_ledger import _spec_set

    doc = json.loads(canonical_execution_fact_bytes(fee_fact()))
    fee = doc["payload"]["fees"][0]
    if mutation == "amount":
        fee["amount"] = "1"
    elif mutation == "currency":
        fee["currency"] = "EUR"
    elif mutation == "policy":
        fee["commission"]["policy"] = "deterministic-commission-v2"
    else:
        fee["commission"]["commission_bps"] = "200"
    with pytest.raises(ValueError):
        decode_execution_fact(
            json.dumps(doc).encode(), context=IndependentFactDecodeContext(_spec_set())
        )


def test_ledger_fee_balanced_atomic_replay() -> None:
    from ea.core import InitialFunding, OutcomeCode
    from ea.portfolio import create_portfolio_ledger
    from unit.test_portfolio_ledger import RUN_ID, USD, _spec_set, _state_bytes

    ledger = create_portfolio_ledger(RUN_ID, _spec_set())
    ledger.apply_initial_funding(InitialFunding(RUN_ID, USD, CanonicalDecimal("1000")))
    fill = fee_fill()
    outcome = ledger.apply_fill(fill)
    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.cash_balances[0].amount.text == "798"
    assert outcome.transaction is not None
    from decimal import Decimal

    currency_postings = [
        p for p in outcome.transaction.postings if hasattr(p.commodity, "currency")
    ]
    assert sum(Decimal(p.amount.text) for p in currency_postings) == 0
    assert any(
        p.account.value == "portfolio.commission" and p.amount.text == "2"
        for p in outcome.transaction.postings
    )
    before = _state_bytes(ledger)
    assert ledger.apply_fill(fill).code is OutcomeCode.LEDGER_DUPLICATE
    assert _state_bytes(ledger) == before
    assert ledger.apply_fill(fee_fill("200")).code is OutcomeCode.LEDGER_CONFLICT
    assert _state_bytes(ledger) == before
    ledger.apply_fill(fee_fill(sequence=2))
    assert ledger.snapshot.cash_balances[0].amount.text == "596"
    poor = create_portfolio_ledger(RUN_ID, _spec_set())
    poor.apply_initial_funding(InitialFunding(RUN_ID, USD, CanonicalDecimal("201")))
    before = _state_bytes(poor)
    assert poor.apply_fill(fill).code is OutcomeCode.OUT_OF_RANGE
    assert _state_bytes(poor) == before


@pytest.mark.parametrize("stage", ["funding_durable", "dispatch_durable", "reconciliation_durable"])
def test_commission_resume_equivalence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    import ea.product.backtest as backtest
    from ea.core import InstrumentExecutionSpecSet, LedgerTransaction, RunId
    from ea.portfolio.ledger import PortfolioLedger
    from ea.product import resume_backtest_attempt
    from unit.test_backtest_report import _economic_projection, _evidence_tree, _report
    from unit.test_backtest_resume import _AbruptInterruption, _interrupt_at

    ledgers: dict[str, PortfolioLedger] = {}
    from ea.portfolio import create_portfolio_ledger as create_ledger

    def capture(run_id: RunId, spec_set: InstrumentExecutionSpecSet) -> PortfolioLedger:
        ledger = create_ledger(run_id, spec_set)
        ledgers[run_id.value] = ledger
        return ledger

    monkeypatch.setattr(backtest, "create_portfolio_ledger", capture)
    scenario = load_backtest_scenario(commission_scenario(tmp_path / "input"))
    baseline = run_backtest_scenario(scenario, tmp_path / "baseline").output_directory
    _interrupt_at(monkeypatch, stage)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, tmp_path / "resumed")
    attempt = next((tmp_path / "resumed").iterdir())
    monkeypatch.setattr(backtest, "_TEST_INTERRUPT", None)
    resume_backtest_attempt(attempt)
    first = _report(baseline, tmp_path / "report1")
    second = _report(attempt, tmp_path / "report2")
    assert _economic_projection(first) == _economic_projection(second)
    result1 = json.loads((baseline / "result.json").read_bytes())
    result2 = json.loads((attempt / "result.json").read_bytes())
    assert result1["fill_evidence"]["fees"] == result2["fill_evidence"]["fees"]
    assert result1["ending_cash"] == result2["ending_cash"]
    assert result1["ledger_sequence"] == result2["ledger_sequence"]
    assert [
        t.postings for t in ledgers[baseline.name].transactions if isinstance(t, LedgerTransaction)
    ] == [
        t.postings for t in ledgers[attempt.name].transactions if isinstance(t, LedgerTransaction)
    ]
    before = _evidence_tree(attempt)
    resume_backtest_attempt(attempt)
    generate_backtest_report(attempt, tmp_path / "report2")
    assert _evidence_tree(attempt) == before


@pytest.mark.parametrize("bps", ["0", "100"])
def test_no_fill_keeps_no_fee(tmp_path: Path, bps: str) -> None:
    path = commission_scenario(tmp_path / "input", bps)
    doc = yaml.safe_load(path.read_text())
    doc["strategy"] = {"id": "always-flat-v1"}
    path.write_text(yaml.safe_dump(doc))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    generate_backtest_report(attempt, tmp_path / "report")
    economics = json.loads((tmp_path / "report/report.json").read_bytes())["economics"]
    assert economics["fees"]["amount"] == "0"
    assert economics["fees"]["count"] == 0
    assert economics["net_pnl"]["amount"] == "0"


def test_web_commission_batch_restart(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings, _validate, _wait

    settings = _settings(tmp_path)
    path = settings.scenario_root / "bounded-long.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["execution"]["commission"] = {
        "policy": "deterministic-commission-v1",
        "commission_bps": "100",
    }
    path.write_text(yaml.safe_dump(doc))
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        assert validated["summary"]["commission"]["commission_bps"] == "100"
        response = client.post(
            "/api/batches",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "initial_cash": "10000",
                "runs": [
                    {"strategy_parameters": {"target_quantity": "2", "entry_delay_bars": 0}},
                    {"strategy_parameters": {"target_quantity": "2", "entry_delay_bars": 2}},
                ],
            },
        )
        assert response.status_code == 202, response.text
        batch = response.json()
        jobs = [_wait(client, member["job_id"]) for member in batch["members"]]
        reports = [client.get(f"/api/backtests/{job['job_id']}/report").json() for job in jobs]
        assert [r["economics"]["fees"]["amount"] for r in reports] == ["2.03", "2.2"]
        assert [r["economics"]["net_pnl"]["amount"] for r in reports] == ["14.97", "-2.2"]
        before = client.get(f"/api/batches/{batch['batch_id']}").json()
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get(f"/api/batches/{batch['batch_id']}").json() == before
        for job, report in zip(jobs, reports, strict=True):
            assert (
                job["input_snapshot"]["scenario"]["execution"]["commission"]["commission_bps"]
                == "100"
            )
            assert client.get(f"/api/backtests/{job['job_id']}/report").json() == report


@pytest.mark.parametrize("policy", ["unknown", "deterministic-commission-v2"])
def test_unknown_policy_rejected(tmp_path: Path, policy: str) -> None:
    path = commission_scenario(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["execution"]["commission"]["policy"] = policy
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError):
        load_backtest_scenario(path)


@pytest.mark.parametrize("quantum", ["0", "-0.01"])
def test_invalid_quantum(quantum: str) -> None:
    from ea.core.commission import commission_amount

    with pytest.raises(ValueError):
        commission_amount(*(CanonicalDecimal(x) for x in ("100", "2", "1", "100", quantum)))


def test_fee_overflow() -> None:
    from ea.core import OutcomeCode
    from ea.core.commission import commission_amount
    from ea.core.economics import EconomicValidationError

    with pytest.raises(EconomicValidationError) as caught:
        commission_amount(
            *(CanonicalDecimal(x) for x in ("99999999999999999999", "2", "1", "10000", "0.01"))
        )
    assert caught.value.code is OutcomeCode.ARITHMETIC_OVERFLOW


def test_policy_digest_binding() -> None:
    from ea.core.commission import commission_bps_from_identity, commission_policy_identity

    identifier, digest = commission_policy_identity(CanonicalDecimal("100"))
    assert commission_bps_from_identity(identifier, digest) == CanonicalDecimal("100")
    with pytest.raises(ValueError):
        commission_bps_from_identity(identifier, "0" * 64)


def test_forged_fee_rejected_by_fill_and_ledger() -> None:
    from dataclasses import replace

    from ea.core import EconomicOwnerKind, create_fill
    from ea.portfolio import create_portfolio_ledger
    from unit.test_portfolio_ledger import RUN_ID, _id, _replace_fill, _spec_set, _state_bytes

    fill = fee_fill()
    bad_fees = (replace(fill.fees[0], amount=CanonicalDecimal("1")),)
    ledger = create_portfolio_ledger(RUN_ID, _spec_set())
    before = _state_bytes(ledger)
    with pytest.raises(ValueError):
        ledger.apply_fill(_replace_fill(fill, fees=bad_fees))
    assert _state_bytes(ledger) == before
    fact = fee_fact()
    object.__setattr__(fact.payload, "fees", bad_fees)
    with pytest.raises(ValueError):
        create_fill(
            fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 1), fact=fact, spec_set=_spec_set()
        )


def test_zero_and_positive_identity_and_determinism(tmp_path: Path) -> None:
    from unit.test_backtest_report import _economic_projection, _report

    reports = []
    identities = []
    for label, bps in [("zero", "0"), ("positive", "100"), ("repeat", "100")]:
        scenario = load_backtest_scenario(commission_scenario(tmp_path / label, bps))
        identities.append(scenario.scenario_sha256)
        attempt = run_backtest_scenario(scenario, tmp_path / (label + "-runs")).output_directory
        reports.append(_report(attempt, tmp_path / (label + "-report")))
    assert identities[0] != identities[1] == identities[2]
    assert _economic_projection(reports[1]) == _economic_projection(reports[2])
    assert reports[0]["completion"] != reports[1]["completion"]
    assert reports[0]["economics"]["fees"]["count"] == 1  # type: ignore[index]
    assert reports[0]["economics"]["fees"]["amount"] == "0"  # type: ignore[index]


@pytest.mark.parametrize("mutation", ["amount", "currency", "rate"])
def test_report_rejects_fee_tampering(tmp_path: Path, mutation: str) -> None:
    scenario = load_backtest_scenario(commission_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    file = attempt / "result.json"
    doc = json.loads(file.read_bytes())
    fee = doc["fill_evidence"]["fees"][0]
    if mutation == "rate":
        fee["commission"]["commission_bps"] = "200"
    else:
        fee[mutation] = "1" if mutation == "amount" else "EUR"
    file.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(ValueError):
        generate_backtest_report(attempt, tmp_path / "report")
    assert not (tmp_path / "report/report.json").exists()


def test_combined_cash_delta_overflow_is_atomic() -> None:
    from ea.core import (
        EconomicOwnerKind,
        ExternalFactId,
        OrderSide,
        OutcomeCode,
        create_fill,
        create_trade_execution_fact,
    )
    from ea.portfolio import create_portfolio_ledger
    from unit.test_portfolio_ledger import (
        INSTRUMENT,
        PROVENANCE,
        RUN_ID,
        SOURCE,
        TIME,
        _id,
        _spec_set,
        _state_bytes,
    )

    fact = create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId("overflow"),
        occurred_at=TIME,
        provenance=PROVENANCE,
        spec_set=_spec_set(),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal("1"),
        price=CanonicalDecimal("99999999999999999999"),
        commission_bps=CanonicalDecimal("10000"),
    )
    fill = create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, 1), fact=fact, spec_set=_spec_set()
    )
    ledger = create_portfolio_ledger(RUN_ID, _spec_set())
    before = _state_bytes(ledger)
    assert ledger.apply_fill(fill).code is OutcomeCode.ARITHMETIC_OVERFLOW
    assert _state_bytes(ledger) == before
