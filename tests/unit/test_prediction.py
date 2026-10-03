import pytest

from ea.product.prediction import Contract, Observation, PredictionJournal

AT = "2026-10-03T11:10:08+00:00"
BEFORE = "2024-11-05T00:00:00+00:00"


def contract(**changes):
    return Contract.model_validate(
        {
            "version": 1,
            "contract_id": "historic-binary",
            "question": "Did X win?",
            "rules": "YES if X wins; otherwise NO.",
            "authority": "official-page",
            "source": "https://example.org/result",
            "source_kind": "official_history",
            "rules_observed_at": AT,
            "currency": "USD_SIM",
            "quantum": "0.01",
            "face_value": "1",
            "void_payout": None,
            **changes,
        }
    )


def observation(**changes):
    return Observation.model_validate(
        {
            "contract_id": "historic-binary",
            "authority": "official-page",
            "source": "https://example.org/result",
            "available_at": AT,
            "status": "resolved",
            "outcome": "YES",
            "payout_yes": "1",
            "payout_no": "0",
            "evidence": "Official page explicitly displays final YES.",
            **changes,
        }
    )


def open_journal(tmp_path, **changes):
    return PredictionJournal(tmp_path / "journal.json", contract(**changes), "YES", 10)


def test_visibility_then_exactly_once_and_restart(tmp_path):
    journal = open_journal(tmp_path)
    assert journal.apply(observation(), BEFORE)["action"] == "not_visible"
    assert journal.report()["status"] == "pending"
    assert journal.report()["settlement_count"] == 0
    report = journal.apply(observation(), AT)
    assert report["action"] == "applied"
    assert report["cash"] == "10"
    assert report["open_claims"] == 0
    assert report["postings"] == [
        {"account": "cash", "currency": "USD_SIM", "amount": "10"},
        {"account": "settlement_clearing", "currency": "USD_SIM", "amount": "-10"},
    ]
    before = (tmp_path / "journal.json").read_bytes()
    assert journal.apply(observation(), AT)["action"] == "duplicate_noop"
    reopened = open_journal(tmp_path)
    assert reopened.report() == journal.report()
    assert reopened.apply(observation(), AT)["action"] == "duplicate_noop"
    assert (tmp_path / "journal.json").read_bytes() == before


@pytest.mark.parametrize("status", ["pending", "disputed", "unknown"])
def test_nonfinal_never_success_or_payout(tmp_path, status):
    journal = open_journal(tmp_path)
    report = journal.apply(
        observation(status=status, outcome=None, payout_yes=None, payout_no=None), AT
    )
    assert report["status"] == status
    assert report["success"] is False
    assert report["cash"] == "0"
    assert report["open_claims"] == 10
    assert report["postings"] == []
    assert open_journal(tmp_path).report() == journal.report()


def test_missing_input_remains_pending(tmp_path):
    assert open_journal(tmp_path).report()["success"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"payout_yes": "0.5"},
        {"payout_no": "1"},
        {"outcome": None},
        {"authority": "rumour"},
        {"source": "https://foreign.test"},
        {"contract_id": "other"},
        {"payout_yes": "-1"},
        {"payout_yes": "NaN"},
        {"status": "disputed"},
        {"status": "void", "outcome": None},
    ],
)
def test_invalid_results_do_not_mutate(tmp_path, changes):
    journal = open_journal(tmp_path)
    original = (tmp_path / "journal.json").read_bytes()
    with pytest.raises(ValueError):
        journal.apply(observation(**changes), AT)
    assert (tmp_path / "journal.json").read_bytes() == original
    assert journal.report()["success"] is False


def test_conflicting_revision_blocked_even_after_restart(tmp_path):
    journal = open_journal(tmp_path)
    journal.apply(observation(), AT)
    original = (tmp_path / "journal.json").read_bytes()
    with pytest.raises(ValueError, match="terminal conflict"):
        open_journal(tmp_path).apply(observation(outcome="NO", payout_yes="0", payout_no="1"), AT)
    assert (tmp_path / "journal.json").read_bytes() == original


def test_losing_claim_retired_at_zero(tmp_path):
    journal = PredictionJournal(tmp_path / "journal.json", contract(), "NO", 7)
    report = journal.apply(observation(), AT)
    assert report["cash"] == "0"
    assert report["open_claims"] == 0
    assert report["settlement_count"] == 1
    assert report["success"] is True


def test_synthetic_void_is_explicit_refund_not_win(tmp_path):
    journal = open_journal(tmp_path, source_kind="synthetic", void_payout="0.4")
    report = journal.apply(
        observation(status="void", outcome=None, payout_yes="0.4", payout_no="0.4"), AT
    )
    assert report["cash"] == "4"
    assert report["status"] == "void"
    assert report["success"] is False
    assert report["settlement_count"] == 1
    assert report["open_claims"] == 0
    assert (
        open_journal(tmp_path, source_kind="synthetic", void_payout="0.4").report()
        == journal.report()
    )


@pytest.mark.parametrize("kwargs", [{"quantity": True}, {"quantity": 0}, {"side": "MAYBE"}])
def test_invalid_claim(tmp_path, kwargs):
    with pytest.raises(ValueError):
        PredictionJournal(
            tmp_path / "journal.json",
            contract(),
            kwargs.get("side", "YES"),
            kwargs.get("quantity", 10),
        )


def test_bootstrap_conflict_and_corrupt_replay(tmp_path):
    journal = open_journal(tmp_path)
    journal.apply(observation(), AT)
    with pytest.raises(ValueError):
        PredictionJournal(tmp_path / "journal.json", contract(), "NO", 10)
    path = tmp_path / "journal.json"
    path.write_text(path.read_text().replace('"payout_yes":"1"', '"payout_yes":"0"'))
    with pytest.raises(ValueError):
        open_journal(tmp_path).report()


def test_duplicate_json_key_rejected(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text('{"version":1,"version":2}')
    with pytest.raises(ValueError):
        open_journal(tmp_path)


def test_persisted_future_cannot_leak_into_earlier_view(tmp_path):
    journal = open_journal(tmp_path)
    journal.apply(observation(), AT)
    with pytest.raises(ValueError, match="regressed"):
        journal.apply(observation(), BEFORE)
    with pytest.raises(ValueError, match="regressed"):
        journal.report(BEFORE)


def test_backdated_capture_rejected(tmp_path):
    journal = open_journal(tmp_path)
    with pytest.raises(ValueError, match="availability precedes"):
        journal.apply(observation(available_at=BEFORE), AT)
    assert journal.report()["settlement_count"] == 0


def test_stale_instance_reloads_before_payout(tmp_path):
    first = open_journal(tmp_path)
    stale = open_journal(tmp_path)
    first.apply(observation(), AT)
    assert stale.apply(observation(), AT)["action"] == "duplicate_noop"
    assert stale.report()["settlement_count"] == 1


@pytest.mark.parametrize("after_replace", [False, True])
def test_unconfirmed_publish_reopens_without_double_payout(tmp_path, monkeypatch, after_replace):
    import ea.product.prediction as module

    journal = open_journal(tmp_path)
    original = module.os.replace

    def fail(source, target):
        if after_replace:
            original(source, target)
        raise OSError("injected interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        with pytest.raises(OSError):
            journal.apply(observation(), AT)
    reopened = open_journal(tmp_path)
    assert reopened.report()["settlement_count"] == int(after_replace)
    result = reopened.apply(observation(), AT)
    assert result["action"] == ("duplicate_noop" if after_replace else "applied")
    assert result["cash"] == "10"
    assert result["settlement_count"] == 1


def test_dispute_then_authoritative_final(tmp_path):
    journal = open_journal(tmp_path)
    journal.apply(
        observation(status="disputed", outcome=None, payout_yes=None, payout_no=None),
        AT,
    )
    assert journal.apply(observation(), AT)["success"] is True


def test_lock_contention_fails_closed(tmp_path):
    journal = open_journal(tmp_path)
    with journal._lock(), pytest.raises(BlockingIOError):
        open_journal(tmp_path)


def test_currency_quantum_and_void_rules_closed():
    for change in [
        {"version": True},
        {"currency": "USDC"},
        {"quantum": "0.001"},
        {"void_payout": "0.4"},
        {"source_kind": "synthetic", "void_payout": "0.401"},
    ]:
        with pytest.raises(ValueError):
            contract(**change)
