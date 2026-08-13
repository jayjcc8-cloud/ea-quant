from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import decimal
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.environ["EA_STRATEGY_PLANNING_SOURCE_ROOT"])
context = decimal.getcontext()
if os.environ["EA_STRATEGY_PLANNING_TEST_NOISE"] == "first":
    context.prec = 7
    context.rounding = decimal.ROUND_DOWN
else:
    context.prec = 31
    context.rounding = decimal.ROUND_CEILING

from ea.core import (
    CanonicalDecimal,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    Phase1PortfolioPolicyEntry,
    PortfolioPolicyId,
    PriceDomain,
    ReplayWindow,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SignalDirection,
    VenueId,
    build_instrument_spec_set,
    canonical_phase1_portfolio_policy_bytes,
    canonical_portfolio_planning_authority_state_bytes,
    canonical_portfolio_planning_result_bytes,
    canonical_strategy_signal_authority_state_bytes,
    canonical_strategy_signal_bytes,
    create_phase1_portfolio_policy,
    phase1_portfolio_policy_digest,
    portfolio_planning_authority_state_digest,
    portfolio_planning_result_digest,
    strategy_signal_authority_state_digest,
    strategy_signal_digest,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.portfolio import create_portfolio_ledger, create_portfolio_planning_authority
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority

header = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
row = (
    "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,"
    "raw,100.0,101.0,99.0,100.5,10.0,cross.raw,0,0,2026-01-02T09:31:00.000000Z"
)
window = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
)
dataset = decode_phase1_ohlcv_csv(
    ("\n".join((header, row)) + "\n").encode(),
    replay_window=window,
)
source = create_phase1_historical_market_data_source(dataset)
bridge = create_phase1_historical_market_source_bridge(source)
instrument = Instrument(VenueId("XNAS"), "AAPL")
other_instrument = Instrument(VenueId("XNAS"), "MSFT")
specifications = [
    InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId("xnas-aapl-cross.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    ),
    InstrumentExecutionSpec(
        instrument=other_instrument,
        specification_id=InstrumentSpecId("xnas-msft-cross.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    ),
]
if os.environ["EA_STRATEGY_PLANNING_TEST_NOISE"] == "second":
    specifications.reverse()
spec_set = build_instrument_spec_set(
    InstrumentSpecSetId("strategy-planning-cross.v1"),
    tuple(sorted(specifications, key=lambda item: item.instrument.key)),
)
run_id = RunId("12345678-1234-4234-8234-123456789abc")
runtime = create_phase1_historical_market_runtime(
    run_id=run_id,
    spec_set=spec_set,
    source=bridge,
)
lease = runtime.pop()
verifier = create_active_market_dispatch_verifier(runtime)
signal_authority = create_strategy_signal_authority(run_id=run_id, verifier=verifier)
signal = signal_authority.issue(
    lease.root,
    dispatch_sequence=lease.dispatch_sequence,
    direction=SignalDirection.LONG,
)
policy_entries = [
    Phase1PortfolioPolicyEntry(
        instrument=instrument,
        target_quantity=CanonicalDecimal("10"),
    ),
    Phase1PortfolioPolicyEntry(
        instrument=other_instrument,
        target_quantity=CanonicalDecimal("20"),
    ),
]
if os.environ["EA_STRATEGY_PLANNING_TEST_NOISE"] == "second":
    policy_entries.reverse()
policy = create_phase1_portfolio_policy(
    policy_id=PortfolioPolicyId("strategy-planning-cross.v1"),
    entries=tuple(sorted(policy_entries, key=lambda item: item.instrument.key)),
    spec_set=spec_set,
)
ledger = create_portfolio_ledger(run_id=run_id, spec_set=spec_set)
planner = create_portfolio_planning_authority(
    run_id=run_id,
    ledger=ledger,
    spec_set=spec_set,
    policy=policy,
    execution_policy=ExecutionPolicyRef(
        ExecutionPolicyId("phase1.execution.v1"),
        Sha256Digest("1" * 64),
    ),
)
result = planner.plan(signal)
print(json.dumps({
    "planner_state": canonical_portfolio_planning_authority_state_bytes(
        planner.state
    ).decode("ascii"),
    "planner_state_sha256": portfolio_planning_authority_state_digest(
        planner.state
    ).value,
    "policy": canonical_phase1_portfolio_policy_bytes(policy).decode("ascii"),
    "policy_sha256": phase1_portfolio_policy_digest(policy).value,
    "result": canonical_portfolio_planning_result_bytes(result).decode("ascii"),
    "result_sha256": portfolio_planning_result_digest(result).value,
    "signal": canonical_strategy_signal_bytes(signal).decode("ascii"),
    "signal_sha256": strategy_signal_digest(signal).value,
    "signal_state": canonical_strategy_signal_authority_state_bytes(
        signal_authority.state
    ).decode("ascii"),
    "signal_state_sha256": strategy_signal_authority_state_digest(
        signal_authority.state
    ).value,
}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
"""


def _run(
    cwd: Path,
    *,
    hash_seed: str,
    timezone: str,
    locale: str,
    noise: str,
) -> bytes:
    cwd.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_STRATEGY_PLANNING_TEST_NOISE": noise,
            "EA_STRATEGY_PLANNING_SOURCE_ROOT": str(Path(__file__).resolve().parents[2] / "src"),
        }
    )
    return subprocess.check_output(
        [sys.executable, "-I", "-B", "-c", SCRIPT],
        cwd=cwd,
        env=environment,
    )


def test_strategy_planning_evidence_is_cross_process_deterministic(tmp_path: Path) -> None:
    expected = _run(
        tmp_path / "first",
        hash_seed="1",
        timezone="UTC",
        locale="C",
        noise="first",
    )
    actual = _run(
        tmp_path / "second",
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
        noise="second",
    )

    assert actual == expected
    assert expected.endswith(b"\n")
    document = json.loads(expected)
    assert document["signal_sha256"] == (
        "047472e5c2c38366e2065d183abdfbe3f35ccf3d7c89de4eb4713a1b9b937477"
    )
    assert document["policy_sha256"] == (
        "51795ed8be83e31d054619815accd1c3567dc101faa74f87c83a26eb8803c1a4"
    )
    assert document["result_sha256"] == (
        "53a969640dd0ad215be4bf983c757f4b714d7ea2bdf984155a47fe974ca18bec"
    )
    assert document["signal_state_sha256"] == (
        "8e46407287f1ace3ebd56c6309f5acb188bc602da5debff78873c161d73d403b"
    )
    assert document["planner_state_sha256"] == (
        "5572ab3d0a3cdf1d1197f96a9a179088e0fd287187d21ccb4ac0ad92bf919dc2"
    )
