"""The closed claim vocabulary: allowlist, sign reading and host templates.

These are pure functions over a report document, so the fixtures here are report
slices rather than engine runs. Every path and shape used below is the one the
frozen `ea.backtest-report.v1|v2|v3` documents actually carry; the engine-backed
proof that the real report satisfies them lives in
``test_web_research_interpretation.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ea.web import report_claims as rc


def _report(**economics: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "currency": "USD",
        "counts": {"orders": 2, "fills": 2},
        "net_pnl": {"amount": "4.25", "rule": "equity-minus-initial-funding-v1"},
        "fees": {"amount": "0.50", "count": 2, "currency": "USD", "rule": "per-fill-commission"},
        "total_return": {
            "value": "0.004250000000000000",
            "unit": "ratio",
            "denominator": "initial_funding",
            "precision": "18-decimal-places",
            "rounding": "half_even",
            "rule": "net-pnl-over-initial-funding",
        },
    }
    base.update(economics)
    return {"schema": "ea.backtest-report.v3", "run_id": "r-1", "economics": base}


# --- the allowlist -----------------------------------------------------------


def test_only_allowlisted_report_paths_are_accepted() -> None:
    accepted = rc.validate_criterion(
        {"field": "economics.net_pnl.amount", "expectation": "positive"}
    )
    assert accepted == {
        "schema": rc.CRITERION_SCHEMA,
        "field": "economics.net_pnl.amount",
        "expectation": "positive",
    }


@pytest.mark.parametrize(
    "value",
    [
        {"field": "economics.equity.amount", "expectation": "positive"},
        {"field": "net_pnl", "expectation": "positive"},
        {"field": "economics.net_pnl.amount", "expectation": "profitable"},
        {"field": "economics.net_pnl.amount"},
        {"expectation": "positive"},
        "economics.net_pnl.amount",
    ],
)
def test_any_other_criterion_shape_is_refused(value: Any) -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.validate_criterion(value)


def test_no_criterion_is_allowed_and_means_nothing_was_stated() -> None:
    assert rc.validate_criterion(None) is None


def test_a_stored_criterion_that_cannot_be_read_is_refused() -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.decode_criterion({"field": "economics.not_a_field", "expectation": "zero"})


# --- reading the report ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("4.25", "positive"),
        ("-4.25", "negative"),
        ("0", "zero"),
        ("0.000", "zero"),
        (3, "positive"),
    ],
)
def test_sign_is_read_from_the_report_value(value: Any, expected: str) -> None:
    assert rc.sign_of(value) == expected


@pytest.mark.parametrize("value", ["", "not-a-number", None, True, {"amount": "1"}, float("nan")])
def test_a_value_that_is_not_a_number_has_no_sign(value: Any) -> None:
    assert rc.sign_of(value) is None


def test_a_missing_field_is_reported_as_absent_not_as_zero() -> None:
    reading = rc.observed(
        {"field": "economics.completed_round_trips", "expectation": "positive"},
        _report(),
    )
    assert reading["state"] == "field_absent"
    assert "value" not in reading


def test_a_no_trade_run_cannot_settle_a_derived_criterion() -> None:
    # Net P&L is exactly zero when nothing traded, but that is not evidence about
    # the idea, so it must not be reported as "zero was expected".
    report = _report(
        counts={"orders": 0, "fills": 0},
        net_pnl={"amount": "0", "rule": "equity-minus-initial-funding-v1"},
    )
    outcome, inference = rc.host_outcome(
        {"field": "economics.net_pnl.amount", "expectation": "zero"}, report
    )
    assert (outcome, inference) == (
        "insufficient_evidence",
        "criterion_undefined_on_empty_activity",
    )


def test_an_activity_count_is_real_evidence_even_at_zero() -> None:
    report = _report(counts={"orders": 0, "fills": 0})
    outcome, inference = rc.host_outcome(
        {"field": "economics.counts.fills", "expectation": "zero"}, report
    )
    assert (outcome, inference) == ("supported_in_scope", "criterion_direction_matched")


# --- the host computes the outcome -------------------------------------------


@pytest.mark.parametrize(
    ("expectation", "outcome"),
    [("positive", "supported_in_scope"), ("negative", "not_supported"), ("zero", "not_supported")],
)
def test_outcome_follows_the_frozen_expectation(expectation: str, outcome: str) -> None:
    assert (
        rc.host_outcome(
            {"field": "economics.net_pnl.amount", "expectation": expectation}, _report()
        )[0]
        == outcome
    )


def test_an_absent_criterion_is_insufficient_evidence_not_a_claim() -> None:
    assert rc.host_outcome(None, _report()) == ("insufficient_evidence", "criterion_not_stated")


@pytest.mark.parametrize(
    ("criterion", "report", "met"),
    [
        ({"field": "economics.net_pnl.amount", "expectation": "positive"}, _report(), True),
        (
            {"field": "economics.net_pnl.amount", "expectation": "negative"},
            _report(),
            False,
        ),
        (None, _report(), None),
        (
            {"field": "economics.net_pnl.amount", "expectation": "zero"},
            _report(counts={"orders": 0, "fills": 0}, net_pnl={"amount": "0"}),
            None,
        ),
    ],
)
def test_criterion_met_is_a_three_valued_host_fact(
    criterion: Any, report: dict[str, Any], met: bool | None
) -> None:
    # True/False are the host's reading of a numeric criterion; None means the
    # criterion was not settled here, which is never the model's to decide.
    assert rc.host_reading(criterion, report)["criterion_met"] is met


def test_a_field_this_report_schema_never_records_is_a_capability_gap() -> None:
    # `v1` has no realized P&L at all, so naming it is a capability gap rather
    # than a weaker version of the same answer.
    outcome, inference = rc.host_outcome(
        {"field": "economics.realized_pnl.amount", "expectation": "positive"},
        {**_report(), "schema": "ea.backtest-report.v1"},
    )
    assert (outcome, inference) == (
        "capability_unsupported",
        "criterion_field_unavailable_in_report",
    )


def test_the_allowlist_is_read_per_report_schema() -> None:
    # The same criterion path is available in one frozen schema and not another;
    # the host decides from the report the task actually ran, never from a
    # single global table.
    stated = {"field": "economics.completed_round_trips", "expectation": "positive"}
    assert rc.observed(stated, _report(completed_round_trips=3))["state"] == "value"
    v1 = {**_report(), "schema": "ea.backtest-report.v1"}
    assert rc.observed(stated, v1)["state"] == "field_unavailable_in_schema"
    assert rc.host_outcome(stated, v1) == (
        "capability_unsupported",
        "criterion_field_unavailable_in_report",
    )


# --- the reading is typed, and the type comes from the frozen schema ----------


def test_a_value_of_the_wrong_json_type_is_never_read_as_evidence() -> None:
    # Every frozen schema records a money amount as a decimal string. A JSON
    # number here is a broken report, not a finding, so nothing may be published
    # from it.
    cri = {"field": "economics.net_pnl.amount", "expectation": "positive"}
    broken = _report(net_pnl={"amount": 4.25, "rule": "equity-minus-initial-funding-v1"})
    reading = rc.observed(cri, broken)
    assert reading["state"] == "type_mismatch"
    assert reading["expected_json_type"] == "string"
    assert reading["actual_json_type"] == "number"
    assert "sign" not in reading
    with pytest.raises(rc.CriterionRejectedError):
        rc.host_outcome(cri, broken)
    with pytest.raises(rc.CriterionRejectedError):
        rc.build_interpretation(
            question="Is the edge positive?",
            criterion=cri,
            report=broken,
            report_identity={"job_id": "j-1"},
            inference="criterion_direction_matched",
            model=None,
            versions={},
        )


def test_a_count_recorded_as_a_string_is_never_read_as_evidence() -> None:
    cri = {"field": "economics.counts.fills", "expectation": "zero"}
    broken = _report(counts={"orders": 2, "fills": "2"})
    assert rc.observed(cri, broken)["state"] == "type_mismatch"
    with pytest.raises(rc.CriterionRejectedError):
        rc.host_outcome(cri, broken)


def test_an_unrecognised_report_schema_settles_nothing() -> None:
    cri = {"field": "economics.net_pnl.amount", "expectation": "positive"}
    unknown = {**_report(), "schema": "ea.backtest-report.v9"}
    assert rc.observed(cri, unknown)["state"] == "report_schema_unknown"
    with pytest.raises(rc.CriterionRejectedError):
        rc.host_outcome(cri, unknown)


def test_a_string_that_is_not_a_number_stays_insufficient_rather_than_broken() -> None:
    # The field is the type the schema promises; its content simply cannot be
    # read as a number, which is an honest "cannot evaluate here".
    cri = {"field": "economics.net_pnl.amount", "expectation": "positive"}
    assert rc.host_outcome(cri, _report(net_pnl={"amount": "not-a-number"})) == (
        "insufficient_evidence",
        "criterion_not_evaluable",
    )


def test_the_unit_shown_is_the_reports_own_currency() -> None:
    cri = {"field": "economics.net_pnl.amount", "expectation": "positive"}
    reading = rc.observed(cri, _report(currency="EUR"))
    assert reading["unit"] == "currency"
    assert reading["currency"] == "EUR"
    assert reading["value_text"] == "4.25 EUR"
    assert reading["json_type"] == "string"
    # The criterion names the path only; the currency is never asserted by the
    # user or the model, so the same criterion reads honestly in any currency.
    eur = rc.build_interpretation(
        question="Is the edge positive?",
        criterion=cri,
        report=_report(currency="EUR"),
        report_identity={"job_id": "j-1"},
        inference="criterion_direction_matched",
        model=None,
        versions={},
    )
    assert eur["observed"]["currency"] == "EUR"


def test_the_reading_binds_the_exact_report_and_field_it_came_from() -> None:
    cri = {"field": "economics.net_pnl.amount", "expectation": "positive"}
    identity = {
        "job_id": "j-1",
        "run_id": "r-1",
        "report_sha256": "a" * 64,
        "semantic_outcome_sha256": "b" * 64,
    }
    ref = rc.observed(cri, _report(), identity=identity)["evidence_ref"]
    assert ref == {
        "schema": rc.EVIDENCE_REF_SCHEMA,
        "claim_semantics": rc.CLAIM_SEMANTICS,
        "report_schema": "ea.backtest-report.v3",
        "run_id": "r-1",
        "report_sha256": "a" * 64,
        "semantic_outcome_sha256": "b" * 64,
        "field": "economics.net_pnl.amount",
        "json_type": "string",
        "unit": "currency",
        "currency": "USD",
        "state": "value",
    }
    published = rc.build_interpretation(
        question="Is the edge positive?",
        criterion=cri,
        report=_report(),
        report_identity=identity,
        inference="criterion_direction_matched",
        model=None,
        versions={},
    )
    assert published["observed"]["evidence_ref"] == ref


def test_a_count_is_read_as_a_json_integer_with_a_count_unit() -> None:
    reading = rc.observed(
        {"field": "economics.counts.fills", "expectation": "positive"},
        _report(counts={"orders": 2, "fills": 2}),
    )
    assert reading["json_type"] == "integer"
    assert reading["unit"] == "count"
    assert reading["value"] == 2
    assert reading["value_text"] == "2"
    assert reading["currency"] is None


# --- a criterion and a model selection are shapes, not bags -------------------


def test_a_criterion_may_not_carry_keys_the_vocabulary_does_not_define() -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.validate_criterion(
            {
                "field": "economics.net_pnl.amount",
                "expectation": "positive",
                "threshold": "100",
            }
        )


def test_a_conclude_call_may_not_carry_an_argument_the_host_did_not_define() -> None:
    # The model cannot smuggle a derived number into the claim: an argument the
    # vocabulary does not define is a rejection, never something to display.
    with pytest.raises(rc.CriterionRejectedError):
        rc.claim_selection(
            {"inference": "criterion_direction_matched", "annualized_return": "0.42"}
        )


def test_a_conclude_call_must_be_codes_of_the_declared_shape() -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.claim_selection({"inference": "criterion_direction_matched", "conclusion_scope": 7})
    with pytest.raises(rc.CriterionRejectedError):
        rc.claim_selection({"inference": "criterion_direction_matched", "evidence_ref": "net_pnl"})
    with pytest.raises(rc.CriterionRejectedError):
        rc.claim_selection(
            {"inference": "criterion_direction_matched", "evidence_ref": {"path": "net_pnl"}}
        )
    with pytest.raises(rc.CriterionRejectedError):
        rc.claim_selection(
            {"inference": "criterion_direction_matched", "evidence_ref": {"field": 7}}
        )
    with pytest.raises(rc.CriterionRejectedError):
        rc.claim_selection({"inference": 1})


def test_a_conclude_call_carries_exactly_the_three_closed_arguments() -> None:
    assert rc.claim_selection({"inference": "criterion_direction_matched"}) == {
        "inference": "criterion_direction_matched",
        "evidence_ref": None,
        "conclusion_scope": None,
    }
    assert rc.claim_selection(
        {
            "inference": "criterion_direction_not_matched",
            "evidence_ref": {"field": "economics.net_pnl.amount"},
            "conclusion_scope": rc.FROZEN_SCOPE,
        }
    ) == {
        "inference": "criterion_direction_not_matched",
        "evidence_ref": {"field": "economics.net_pnl.amount"},
        "conclusion_scope": rc.FROZEN_SCOPE,
    }


def test_the_host_adds_the_limitations_its_own_facts_justify() -> None:
    # Nothing here is a sample-size judgement: each code names a section the
    # verified report does not carry, or a scope proof this product does not hold.
    interpretation = rc.build_interpretation(
        question="Is the edge positive?",
        criterion={"field": "economics.net_pnl.amount", "expectation": "positive"},
        report=_report(),
        report_identity={"job_id": "j-1"},
        inference="criterion_direction_matched",
        model=None,
        versions={},
        data_scope={"point_in_time": None, "quality_verified": False},
    )
    codes = [item["code"] for item in interpretation["limitations"]]
    assert codes == [
        "no_out_of_sample_period",
        "no_benchmark_comparison",
        "deterministic_engine_no_uncertainty_estimate",
        "fees_are_modelled_not_observed",
        "backtest_only_no_live_execution",
        "coverage_unknown",
        "point_in_time_not_proven",
        "data_quality_not_independently_verified",
    ]
    assert interpretation["outcome"] == "supported_in_scope"
    assert all(
        item["text"] == rc.LIMITATION_TEXT[item["code"]] for item in interpretation["limitations"]
    )


def test_no_limitation_is_a_sample_size_or_statistical_verdict() -> None:
    # The product holds no sufficiency contract, so the vocabulary has no code
    # for "too few observations", "too little history" or "quality unknown" as a
    # reason to withdraw a criterion the report itself settled.
    for forbidden in (
        "insufficient_observations",
        "insufficient_time_coverage",
        "data_quality_unknown",
        "short_sample",
        "low_power",
    ):
        assert forbidden not in rc.LIMITATION_CODES
        assert forbidden not in rc.INFERENCE_CODES


# --- the model restates the reading, and only within the frozen scope --------


def test_an_inference_code_that_does_not_match_the_reading_is_refused() -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(
            {"field": "economics.net_pnl.amount", "expectation": "positive"},
            _report(),
            inference="criterion_direction_not_matched",
        )


def test_an_unknown_inference_code_is_refused() -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(
            {"field": "economics.net_pnl.amount", "expectation": "positive"},
            _report(),
            inference="looks_promising",
        )


def test_an_evidence_ref_to_another_allowlisted_field_is_refused() -> None:
    # Another allowlisted path is still not this run's frozen measurement, so it
    # is refused rather than silently substituted.
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(
            {"field": "economics.net_pnl.amount", "expectation": "positive"},
            _report(),
            inference="criterion_direction_matched",
            evidence_ref={"field": "economics.fees.amount"},
        )


def test_an_unknown_conclusion_scope_is_refused() -> None:
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(
            {"field": "economics.net_pnl.amount", "expectation": "positive"},
            _report(),
            inference="criterion_direction_matched",
            conclusion_scope="whole_market",
        )


def test_a_wider_scope_is_refused_when_the_accepted_input_declared_none() -> None:
    # The controller's counterexample: a positive net P&L with one order and one
    # fill, where the model declares a scope wider than the frozen run. The
    # accepted input declares one single-run scope and carries no field for
    # anything wider, so the wider conclusion is refused rather than published.
    report = _report(
        counts={"orders": 1, "fills": 1},
        net_pnl={"amount": "1.00", "rule": "equity-minus-initial-funding-v1"},
    )
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(
            {"field": "economics.net_pnl.amount", "expectation": "positive"},
            report,
            inference="declared_scope_not_verified",
            evidence_ref={"field": "economics.net_pnl.amount"},
            conclusion_scope=rc.BEYOND_SCOPE,
        )


def test_a_satisfied_criterion_cannot_be_talked_down_to_insufficient_evidence() -> None:
    # The controller's counterexample: a positive net P&L with one order and one
    # fill and a small record count is a satisfied criterion, and no code the
    # model can select retracts it.
    report = _report(
        counts={"orders": 1, "fills": 1},
        net_pnl={"amount": "1.00", "rule": "equity-minus-initial-funding-v1"},
    )
    for code in rc.INFERENCE_CODES:
        if code == "criterion_direction_matched":
            continue
        with pytest.raises(rc.CriterionRejectedError):
            rc.resolve_claim(
                {"field": "economics.net_pnl.amount", "expectation": "positive"},
                report,
                inference=code,
            )
    claim = rc.resolve_claim(
        {"field": "economics.net_pnl.amount", "expectation": "positive"},
        report,
        inference="criterion_direction_matched",
    )
    assert claim["outcome"] == "supported_in_scope"
    assert claim["criterion_met"] is True
    assert claim["host_inference"] == "criterion_direction_matched"
    assert claim["limitations"] == []


def test_a_wider_scope_is_refused_whatever_code_carries_it() -> None:
    # The wider scope is not selectable at all: not with the code that used to
    # mean "this claim is wider", and not with the code that matches the host
    # reading either. A model cannot grant itself a scope the accepted input
    # never declared.
    for inference in ("declared_scope_not_verified", "criterion_direction_matched"):
        with pytest.raises(rc.CriterionRejectedError):
            rc.resolve_claim(
                {"field": "economics.net_pnl.amount", "expectation": "positive"},
                _report(),
                inference=inference,
                conclusion_scope=rc.BEYOND_SCOPE,
            )


def test_the_same_scope_claim_keeps_the_satisfied_numeric_reading() -> None:
    claim = rc.resolve_claim(
        {"field": "economics.net_pnl.amount", "expectation": "positive"},
        _report(),
        inference="criterion_direction_matched",
        evidence_ref={"field": "economics.net_pnl.amount"},
        conclusion_scope=rc.FROZEN_SCOPE,
    )
    assert claim["outcome"] == "supported_in_scope"
    assert claim["conclusion_scope"] == rc.FROZEN_SCOPE
    assert claim["criterion_met"] is True
    assert claim["limitations"] == []


def test_no_criterion_publishes_the_honest_non_answer_only() -> None:
    claim = rc.resolve_claim(None, _report(), inference="criterion_not_stated")
    assert claim["outcome"] == "insufficient_evidence"
    assert claim["inference"] == "criterion_not_stated"
    assert claim["criterion_met"] is None
    assert claim["evidence_field"] is None
    # Nothing was frozen, so there is no field to name and nothing to widen onto.
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(None, _report(), inference="criterion_direction_matched")
    with pytest.raises(rc.CriterionRejectedError):
        rc.resolve_claim(
            None,
            _report(),
            inference="criterion_not_stated",
            conclusion_scope=rc.BEYOND_SCOPE,
        )


# --- the rendered text is entirely the host's --------------------------------


def test_every_rendered_sentence_comes_from_a_host_constant() -> None:
    interpretation = rc.build_interpretation(
        question="Does buying strength pay?",
        criterion={"field": "economics.net_pnl.amount", "expectation": "positive"},
        report=_report(),
        report_identity={"job_id": "j-1", "report_sha256": "a" * 64},
        inference="criterion_direction_matched",
        model={"provider": "fixture", "model": "fixture-model"},
        versions={"renderer": rc.INTERPRETATION_SCHEMA},
    )
    assert interpretation["schema"] == rc.INTERPRETATION_SCHEMA
    assert interpretation["outcome_text"] == rc.OUTCOME_TEXT["supported_in_scope"]
    assert interpretation["inference_text"] == rc.INFERENCE_TEXT["criterion_direction_matched"]
    assert interpretation["conclusion_scope"] == rc.FROZEN_SCOPE
    assert interpretation["conclusion_scope_text"] == rc.SCOPE_TEXT[rc.FROZEN_SCOPE]
    assert interpretation["criterion_met"] is True
    # The limitation list is the host's own derivation, and every sentence in it
    # is a constant this module owns.
    codes = [item["code"] for item in interpretation["limitations"]]
    assert codes == rc.host_limitations(_report())
    assert all(
        item["text"] == rc.LIMITATION_TEXT[item["code"]] for item in interpretation["limitations"]
    )
    # The question is carried as the user's own input, never as a finding.
    assert interpretation["question"] == "Does buying strength pay?"
    assert interpretation["question_is_user_input"] is True


def test_the_host_reads_the_displayed_value_rather_than_trusting_the_model() -> None:
    report = _report(net_pnl={"amount": "-9.75", "rule": "equity-minus-initial-funding-v1"})
    interpretation = rc.build_interpretation(
        question="Is the edge positive?",
        criterion={"field": "economics.net_pnl.amount", "expectation": "positive"},
        report=report,
        report_identity={"job_id": "j-1"},
        inference="criterion_direction_not_matched",
        model=None,
        versions={},
    )
    assert interpretation["observed"]["value"] == "-9.75"
    assert interpretation["observed"]["sign"] == "negative"
    assert interpretation["observed"]["unit"] == "currency"
    assert interpretation["outcome"] == "not_supported"
    assert interpretation["criterion_met"] is False


def test_a_stored_conclusion_of_another_semantics_is_a_technical_unavailability() -> None:
    # A rendering this build does not implement is not a claim to withdraw, so it
    # must raise rather than be silently reported as stale.
    for published in (
        {"schema": "radian.research-interpretation.v1", "versions": {"renderer": "v1"}},
        {"schema": rc.INTERPRETATION_SCHEMA, "versions": {"renderer": "radian.claim-semantics.v1"}},
        {"schema": rc.INTERPRETATION_SCHEMA, "versions": None},
    ):
        with pytest.raises(rc.InterpretationUnsupportedError):
            rc.rerender_interpretation(
                published, report=_report(), report_identity={"job_id": "j-1"}
            )
    assert not issubclass(rc.InterpretationUnsupportedError, rc.CriterionRejectedError)


def test_a_stored_conclusion_of_this_semantics_re_renders_from_the_report_alone() -> None:
    # The criterion is the one the host froze, in decoded form: that is the shape
    # a published conclusion always stores, so re-rendering must reach it exactly.
    published = rc.build_interpretation(
        question="Is the edge positive?",
        criterion={
            "schema": rc.CRITERION_SCHEMA,
            "field": "economics.net_pnl.amount",
            "expectation": "positive",
        },
        report=_report(),
        report_identity={"job_id": "j-1"},
        inference="criterion_direction_matched",
        model={"provider": "fixture", "model": "fixture-model"},
        versions={"renderer": rc.INTERPRETATION_SCHEMA},
    )
    assert (
        rc.rerender_interpretation(published, report=_report(), report_identity={"job_id": "j-1"})
        == published
    )
    # A report that no longer shows what the conclusion claimed re-renders to
    # nothing rather than to the stored text.
    tampered = _report(net_pnl={"amount": "-4.25", "rule": "equity-minus-initial-funding-v1"})
    assert rc.rerender_interpretation(published, report=tampered, report_identity={}) is None


def test_the_packet_carries_codes_and_host_read_values_only() -> None:
    packet = rc.packet(
        question="Is the edge positive?",
        criterion={"field": "economics.net_pnl.amount", "expectation": "positive"},
        report=_report(),
        report_identity={"job_id": "j-1"},
    )
    assert packet["allowlisted_fields"]["economics.net_pnl.amount"] == "4.25"
    assert packet["pending_inference_codes"] == list(rc.INFERENCE_CODES)
    assert packet["question_is_user_input"] is True
    # The model is given the host's own reading and the allowed scope, so the
    # matching code is visible rather than guessable.
    assert packet["criterion_observed"]["outcome"] == "supported_in_scope"
    assert packet["criterion_observed"]["inference"] == "criterion_direction_matched"
    assert packet["criterion_observed"]["reading"]["sign"] == "positive"
    assert packet["criterion_observed"]["criterion_met"] is True
    assert packet["conclusion_scopes"] == list(rc.CONCLUSION_SCOPES)
    assert packet["default_conclusion_scope"] == rc.FROZEN_SCOPE
    assert packet["host_added_limitations"] == rc.host_limitations(_report())
