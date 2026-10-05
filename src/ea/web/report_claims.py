"""Closed, host-rendered claims about one verified backtest report.

This module is deliberately free of I/O and of the model port. It owns four
things and nothing else:

* the allowlist of report paths a frozen criterion may name,
* the closed inference and limitation vocabularies,
* the one pure mapping from a model selection to a host outcome, and
* the host templates that turn that outcome into the text a user reads.

The model never supplies a number, a unit or a sentence. It selects closed
codes; the host reads every value from the verified report document and renders
every sentence from a constant below. A model string that is not a known code is
rejected, never displayed.

The host's own reading is deterministic and is computed before any model is
consulted. The model may repeat that reading, and it may declare that it wants
to conclude beyond the single frozen experiment -- which this product records as
insufficient evidence rather than as a supported wider claim. It can never
retract a numeric criterion the host already satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

INTERPRETATION_SCHEMA = "radian.research-interpretation.v2"
CRITERION_SCHEMA = "radian.report-criterion.v1"
EVIDENCE_REF_SCHEMA = "radian.report-field.v1"

#: The meaning of a claim is a versioned artefact of this module, not of the
#: model. A published reading names the semantics it was rendered under.
CLAIM_SEMANTICS = "radian.claim-semantics.v2"

V1 = "ea.backtest-report.v1"
V2 = "ea.backtest-report.v2"
V3 = "ea.backtest-report.v3"
REPORT_SCHEMAS = (V1, V2, V3)


@dataclass(frozen=True)
class FieldSpec:
    """How one allowlisted report path is typed and unit-labelled.

    The frozen report schemas do not agree with each other, so a single global
    table would let the host read a field out of a schema that never carried it,
    or accept a JSON number where the schema promises a decimal string.
    """

    path: str
    unit: str
    json_type: str
    schemas: tuple[str, ...]
    #: Report path the unit's own label is read from; ``None`` when the unit is
    #: fixed by the schema (ratios and counts).
    currency_path: str | None = None

    def applies_to(self, schema: str) -> bool:
        return schema in self.schemas


#: The exact report paths a criterion may name, how the host types them, and
#: which frozen schemas actually carry them. Absence is reported honestly
#: rather than substituted.
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "economics.net_pnl.amount", "currency", "string", REPORT_SCHEMAS, "economics.currency"
    ),
    FieldSpec(
        "economics.realized_pnl.amount",
        "currency",
        "string",
        (V2, V3),
        "economics.currency",
    ),
    FieldSpec(
        "economics.fees.amount", "currency", "string", REPORT_SCHEMAS, "economics.fees.currency"
    ),
    FieldSpec("economics.total_return.value", "ratio", "string", REPORT_SCHEMAS),
    FieldSpec("economics.counts.fills", "count", "integer", REPORT_SCHEMAS),
    FieldSpec("economics.counts.orders", "count", "integer", REPORT_SCHEMAS),
    FieldSpec("economics.completed_round_trips", "count", "integer", (V2, V3)),
)

FIELD_SPECS_BY_PATH: dict[str, FieldSpec] = {spec.path: spec for spec in FIELD_SPECS}

#: Paths that are themselves an activity count. For these, a zero is real
#: evidence about trading activity rather than an artefact of not trading.
ACTIVITY_PATHS = frozenset({"economics.counts.fills", "economics.counts.orders"})

#: Readings that are a broken contract rather than a weaker answer: the report
#: is not the document the claim vocabulary is defined against, so nothing may
#: be published from it.
CONTRACT_STATES = frozenset({"report_schema_unknown", "type_mismatch"})

EXPECTATIONS = ("positive", "negative", "zero")
OUTCOMES = (
    "supported_in_scope",
    "not_supported",
    "insufficient_evidence",
    "capability_unsupported",
)

#: The scope a conclusion declares. ``frozen_single_run`` is the one scope the
#: accepted input freezes, so it is the only scope a conclusion may carry. The
#: name for a wider claim is kept only so the wider scope can be *named* in a
#: refusal and in the limitation vocabulary; it is never admissible, because no
#: accepted input declares one.
FROZEN_SCOPE = "frozen_single_run"
BEYOND_SCOPE = "beyond_frozen_run"
CONCLUSION_SCOPES = (FROZEN_SCOPE,)

SCOPE_TEXT: dict[str, str] = {
    FROZEN_SCOPE: (
        "This conclusion is about the single frozen experiment above and claims nothing wider."
    ),
    BEYOND_SCOPE: (
        "The conclusion was declared to reach beyond the single frozen experiment, and "
        "this product holds no verification for a claim that wide."
    ),
}

#: The inference codes the model may select. Each one is a restatement of an
#: outcome the host already computed. There is no code that lets a model invent
#: a weaker answer about a criterion the host read from the report, and none
#: that declares a scope wider than the one the accepted input froze.
INFERENCE_CODES = (
    "criterion_direction_matched",
    "criterion_direction_not_matched",
    "criterion_not_evaluable",
    "criterion_undefined_on_empty_activity",
    "criterion_field_unavailable_in_report",
    "criterion_not_stated",
)

#: Every limitation here is re-derivable from the verified report, the projected
#: data scope and the declared scope alone. That is deliberate: the same host
#: function re-renders a stored conclusion on every read, and it must reach the
#: same list without a model, a second store or a statistic.
LIMITATION_CODES = (
    "no_out_of_sample_period",
    "no_benchmark_comparison",
    "deterministic_engine_no_uncertainty_estimate",
    "fees_are_modelled_not_observed",
    "backtest_only_no_live_execution",
    "coverage_unknown",
    "point_in_time_not_proven",
    "data_quality_not_independently_verified",
    "declared_scope_not_verified",
)

OUTCOME_TEXT: dict[str, str] = {
    "supported_in_scope": (
        "Within this single frozen experiment, the result is consistent with the stated "
        "expectation. This is a statement about one criterion and one run, not about the "
        "idea in general."
    ),
    "not_supported": (
        "Within this single frozen experiment, the result is opposite to the stated "
        "expectation. This is a statement about one criterion and one run, not a proof "
        "that the idea is wrong in general."
    ),
    "insufficient_evidence": (
        "This run does not settle the stated expectation: either no criterion was frozen "
        "before it, or the frozen criterion could not be read as a number from this report, "
        "or nothing traded. Nothing in this report is evidence either way, and nothing here "
        "speaks to anything beyond this single frozen experiment."
    ),
    "capability_unsupported": (
        "The stated expectation cannot be evaluated from this report: the product does not "
        "record the quantity it names."
    ),
}

INFERENCE_TEXT: dict[str, str] = {
    "criterion_direction_matched": (
        "The frozen criterion field changed in the expected direction."
    ),
    "criterion_direction_not_matched": (
        "The frozen criterion field changed in the opposite direction to the expectation."
    ),
    "criterion_not_evaluable": ("The criterion field is not present as a number in this report."),
    "criterion_undefined_on_empty_activity": (
        "No orders or fills were recorded, so this criterion would only describe not "
        "trading rather than the idea."
    ),
    "criterion_field_unavailable_in_report": (
        "This report schema does not record the criterion field."
    ),
    "criterion_not_stated": (
        "No criterion was frozen before the run, so there is nothing to compare against."
    ),
    "declared_scope_not_verified": (
        "The conclusion was declared to reach beyond the single frozen experiment, and "
        "this product holds no verification for a claim that wide, so it is recorded as "
        "insufficient evidence rather than as a supported result."
    ),
}

LIMITATION_TEXT: dict[str, str] = {
    "no_out_of_sample_period": "No out-of-sample period was held back from this run.",
    "no_benchmark_comparison": (
        "The verified report carries no benchmark or buy-and-hold comparison."
    ),
    "deterministic_engine_no_uncertainty_estimate": (
        "The engine is deterministic; this is one path with no uncertainty estimate."
    ),
    "fees_are_modelled_not_observed": (
        "Fees come from the rule recorded in the report, not from a venue."
    ),
    "backtest_only_no_live_execution": "Backtest only; nothing was executed live.",
    "coverage_unknown": (
        "Nothing here proves the captured window covers the time the question is about, so "
        "time coverage is unknown rather than known to be short."
    ),
    "point_in_time_not_proven": (
        "no PIT proof provided: nothing here shows the capture is the point-in-time "
        "membership that was tradeable then."
    ),
    "data_quality_not_independently_verified": (
        "The captured data is host-verified by hash only; no independent audit of its "
        "content was performed."
    ),
    "declared_scope_not_verified": (
        "The conclusion declares a scope wider than the single frozen run, and no "
        "verification for a claim that wide exists in this product."
    ),
}


class CriterionRejectedError(ValueError):
    """A criterion, or a claim about one, is not a shape this vocabulary defines."""


class InterpretationUnsupportedError(ValueError):
    """A stored conclusion was rendered under semantics this build cannot read.

    Deliberately not a :class:`CriterionRejectedError`: a version this build does
    not implement is a technical unavailability, never a claim to withdraw.
    """


def read_field(report: dict[str, Any], path: str) -> Any | None:
    """Walk one dotted allowlisted path. A missing path reads as ``None``."""
    node: Any = report
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    return None


def _json_type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Decimal):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "null"


def _matches_json_type(value: Any, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        # `type` rather than `isinstance`: a JSON boolean is not a count.
        return type(value) is int
    return False


def _value_text(value: Any, spec: FieldSpec, currency: str | None) -> str:
    """The one place a number is turned into display text. Host-owned."""
    if spec.unit == "currency":
        return f"{value} {currency}" if currency else f"{value} (unit not recorded)"
    return f"{value}"


def sign_of(value: Any) -> str | None:
    """``positive``/``negative``/``zero`` for a numeric report value."""
    parsed = _decimal(value)
    if parsed is None:
        return None
    if parsed > 0:
        return "positive"
    if parsed < 0:
        return "negative"
    return "zero"


def report_schema_of(report: dict[str, Any]) -> str | None:
    schema = report.get("schema")
    return schema if isinstance(schema, str) else None


def validate_criterion(value: Any) -> dict[str, Any] | None:
    """Accept exactly one allowlisted field plus a sign expectation."""
    if value is None:
        return None
    if type(value) is not dict:
        raise CriterionRejectedError("criterion must be one object")
    unknown = sorted(set(value) - {"schema", "field", "expectation"})
    if unknown:
        raise CriterionRejectedError(f"criterion has an unknown key: {unknown[0]!r}")
    path = value.get("field")
    expectation = value.get("expectation")
    if path not in FIELD_SPECS_BY_PATH:
        allowed = ", ".join(sorted(FIELD_SPECS_BY_PATH))
        raise CriterionRejectedError(f"criterion field must be one of: {allowed}")
    if expectation not in EXPECTATIONS:
        raise CriterionRejectedError(
            f"criterion expectation must be one of: {', '.join(EXPECTATIONS)}"
        )
    return {
        "schema": CRITERION_SCHEMA,
        "field": str(path),
        "expectation": str(expectation),
    }


def decode_criterion(document: Any) -> dict[str, Any]:
    """Read a stored criterion strictly: a record we cannot read is refused."""
    if type(document) is not dict:
        raise CriterionRejectedError("stored criterion must be one object")
    return cast(dict[str, Any], validate_criterion(document))


def validate_evidence_ref(value: Any, criterion: dict[str, Any] | None) -> str:
    """The one report field a conclusion may reference, checked against the freeze.

    The model may only ever point at the field the run actually froze. Naming a
    different allowlisted field would silently move the conclusion onto another
    measurement, so it is refused rather than substituted. Omitting the argument
    defaults to the frozen field; there is nothing to default to when no
    criterion was frozen.
    """
    if criterion is None:
        raise CriterionRejectedError("no criterion was frozen, so no evidence field exists")
    frozen = str(criterion.get("field"))
    if value is None:
        return frozen
    if type(value) is not dict:
        raise CriterionRejectedError("evidence_ref must be one object")
    ref = cast(dict[str, Any], value)
    unknown = sorted(set(ref) - {"field"})
    if unknown:
        raise CriterionRejectedError(f"unknown evidence_ref key: {unknown[0]!r}")
    field = ref.get("field")
    if not isinstance(field, str):
        raise CriterionRejectedError("evidence_ref.field must be one report path")
    if field not in FIELD_SPECS_BY_PATH:
        allowed = ", ".join(sorted(FIELD_SPECS_BY_PATH))
        raise CriterionRejectedError(f"evidence_ref field must be one of: {allowed}")
    if field != frozen:
        raise CriterionRejectedError(
            f"evidence_ref {field!r} is not the frozen criterion field {frozen!r}"
        )
    return field


def validate_conclusion_scope(value: Any) -> str:
    """``frozen_single_run``: the only scope the accepted input declares.

    A missing scope means the frozen scope, and a wider one is refused rather
    than narrowed, because the accepted input carries no field that could have
    declared it and the model is not the party that gets to widen a claim.
    """
    if value is None:
        return FROZEN_SCOPE
    if not isinstance(value, str) or value not in CONCLUSION_SCOPES:
        raise CriterionRejectedError(
            f"conclusion_scope must be one of: {', '.join(CONCLUSION_SCOPES)}"
        )
    return value


def _evidence_ref_block(
    spec: FieldSpec, schema: str, reading: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    """The immutable binding between a displayed number and its source.

    Nothing here is ever supplied by the model: it names the exact verified
    report, the exact path, the JSON type the frozen schema promises, the unit
    read from the report, and the semantics this rendering is defined by.
    """
    return {
        "schema": EVIDENCE_REF_SCHEMA,
        "claim_semantics": CLAIM_SEMANTICS,
        "report_schema": schema,
        "run_id": identity.get("run_id"),
        "report_sha256": identity.get("report_sha256"),
        "semantic_outcome_sha256": identity.get("semantic_outcome_sha256"),
        "field": spec.path,
        "json_type": spec.json_type,
        "unit": spec.unit,
        "currency": reading.get("currency"),
        "state": reading["state"],
    }


def observed(
    criterion: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Host-read evidence for one frozen criterion, or the reason it is absent.

    The reading is typed by the frozen schema the report declares, so a value
    of the wrong JSON type is a broken contract rather than a finding, and the
    displayed unit is the report's own currency rather than a fixed label.
    """
    if criterion is None:
        return {"state": "not_stated"}
    path = str(criterion.get("field"))
    spec = FIELD_SPECS_BY_PATH.get(path)
    if spec is None:
        return {"state": "field_not_allowlisted", "field": path}
    schema = report_schema_of(report)
    if schema not in REPORT_SCHEMAS:
        return {"state": "report_schema_unknown", "field": path, "report_schema": schema}
    if not spec.applies_to(schema):
        return {
            "state": "field_unavailable_in_schema",
            "field": path,
            "report_schema": schema,
            "unit": spec.unit,
        }
    value = read_field(report, path)
    if value is None:
        return {
            "state": "field_absent",
            "field": path,
            "report_schema": schema,
            "unit": spec.unit,
        }
    if not _matches_json_type(value, spec.json_type):
        return {
            "state": "type_mismatch",
            "field": path,
            "report_schema": schema,
            "unit": spec.unit,
            "expected_json_type": spec.json_type,
            "actual_json_type": _json_type_of(value),
        }
    sign = sign_of(value)
    if sign is None:
        return {
            "state": "not_numeric",
            "field": path,
            "report_schema": schema,
            "unit": spec.unit,
        }
    raw_currency = read_field(report, spec.currency_path) if spec.currency_path else None
    currency = raw_currency if isinstance(raw_currency, str) else None
    reading: dict[str, Any] = {
        "state": "value",
        "field": path,
        "report_schema": schema,
        "unit": spec.unit,
        "json_type": spec.json_type,
        "currency": currency,
        "value": value if spec.json_type == "integer" else str(value),
        "value_text": _value_text(value, spec, currency),
        "sign": sign,
    }
    orders = read_field(report, "economics.counts.orders")
    if path not in ACTIVITY_PATHS and type(orders) is int and orders == 0:
        # Nothing traded, so this field describes the absence of a decision rather
        # than the idea. The host keeps the field identity and the counts that
        # prove it, and withholds the number: a "0.00 USD" next to a claim about
        # the idea is exactly the overstatement this vocabulary exists to stop.
        reading["state"] = "empty_activity"
        reading["counts"] = read_field(report, "economics.counts")
        for withheld in ("value", "value_text", "sign"):
            reading.pop(withheld, None)
    if identity is not None:
        reading["evidence_ref"] = _evidence_ref_block(spec, schema, reading, identity)
    return reading


def host_reading(
    criterion: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The host's own deterministic reading of one frozen criterion.

    This is computed from the verified report alone; no model value is read
    here, and this function never raises. ``outcome`` is ``None`` when the
    report is not the document this vocabulary is defined against, which the
    callers that publish treat as a refusal.

    Counts prove activity and nothing else: a small number of orders or fills
    never turns into "not enough evidence" on its own, because this product
    holds no sufficiency contract for it. Missing time facts stay unknown
    rather than being reported as known-short coverage.
    """
    reading = observed(criterion, report, identity=identity)
    state = reading["state"]
    if state in CONTRACT_STATES:
        return {"reading": reading, "outcome": None, "inference": None, "criterion_met": None}
    if state == "not_stated":
        outcome, inference = "insufficient_evidence", "criterion_not_stated"
    elif state in {"field_not_allowlisted", "field_unavailable_in_schema", "field_absent"}:
        # The criterion names a real report path, but this report schema does not
        # carry it: the product cannot evaluate it here, which is a capability
        # gap rather than a weaker version of the same answer.
        outcome, inference = "capability_unsupported", "criterion_field_unavailable_in_report"
    elif state == "not_numeric":
        outcome, inference = "insufficient_evidence", "criterion_not_evaluable"
    elif state == "empty_activity":
        outcome, inference = "insufficient_evidence", "criterion_undefined_on_empty_activity"
    else:
        assert criterion is not None
        if reading["sign"] == criterion.get("expectation"):
            outcome, inference = "supported_in_scope", "criterion_direction_matched"
        else:
            outcome, inference = "not_supported", "criterion_direction_not_matched"
    if outcome == "supported_in_scope":
        criterion_met: bool | None = True
    elif outcome == "not_supported":
        criterion_met = False
    else:
        criterion_met = None
    return {
        "reading": reading,
        "outcome": outcome,
        "inference": inference,
        "criterion_met": criterion_met,
    }


def host_outcome(criterion: dict[str, Any] | None, report: dict[str, Any]) -> tuple[str, str]:
    """The host-computed outcome and inference code, or a refusal.

    A reading that means the report is not the document this vocabulary is
    defined against raises rather than being published as a weaker answer.
    """
    found = host_reading(criterion, report)
    if found["outcome"] is None:
        reading = found["reading"]
        raise CriterionRejectedError(
            f"the report cannot be read as evidence: {reading['state']} "
            f"({reading.get('expected_json_type') or reading.get('report_schema')})"
        )
    return cast(str, found["outcome"]), cast(str, found["inference"])


def host_limitations(
    report: dict[str, Any],
    *,
    data_scope: Any = None,
    declared_scope: str = FROZEN_SCOPE,
) -> list[str]:
    """Every limitation this run's own recorded facts justify, in a fixed order.

    Each code is grounded in something the host can point at: an absent report
    section, an absent scope proof, or the offline single-run shape of the
    product itself. Nothing here is a generic statistical threshold or a
    judgement about sample size, and nothing here is the model's to grant or
    withhold. The function reads only the verified report document and the
    projected data scope, so a stored conclusion re-renders to the same list on
    every read without a model or a second store.
    """
    scope = data_scope if type(data_scope) is dict else {}
    codes: set[str] = {
        # The frozen requirement is an offline deterministic backtest over the
        # one confirmed run; both of these are true of the run by construction.
        "backtest_only_no_live_execution",
        "no_out_of_sample_period",
        # The replay window is a selection policy, not proof that the window
        # covers the time the question is about.
        "coverage_unknown",
    }
    if read_field(report, "economics.benchmark") is None:
        codes.add("no_benchmark_comparison")
    if read_field(report, "economics.uncertainty") is None:
        codes.add("deterministic_engine_no_uncertainty_estimate")
    if isinstance(read_field(report, "economics.fees.rule"), str):
        codes.add("fees_are_modelled_not_observed")
    if scope.get("point_in_time") is None:
        # Not projected, or projected as unknown: either way no PIT proof exists.
        codes.add("point_in_time_not_proven")
    if scope.get("quality_verified") is not True:
        codes.add("data_quality_not_independently_verified")
    if declared_scope == BEYOND_SCOPE:
        codes.add("declared_scope_not_verified")
    return [code for code in LIMITATION_CODES if code in codes]


def resolve_claim(
    criterion: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    inference: str,
    evidence_ref: Any = None,
    conclusion_scope: Any = None,
) -> dict[str, Any]:
    """The one pure mapping from a model selection to a publishable claim.

    Three things are checked together, in this order and in this one place: the
    evidence reference names the frozen field, the declared scope is the one
    scope the accepted input froze, and the inference code is the exact
    restatement of what the host already read. A code that is not the host's own
    reading is refused, so a model can never talk a satisfied numeric criterion
    down to a weaker answer, substitute a different one, or declare a scope
    wider than the accepted input carries.
    """
    found = host_reading(criterion, report)
    if found["outcome"] is None:
        reading = found["reading"]
        raise CriterionRejectedError(
            f"the report cannot be read as evidence: {reading['state']} "
            f"({reading.get('expected_json_type') or reading.get('report_schema')})"
        )
    scope = validate_conclusion_scope(conclusion_scope)
    if inference not in INFERENCE_TEXT:
        raise CriterionRejectedError(f"unknown inference code: {inference!r}")
    if criterion is None:
        # Nothing was frozen before the run, so there is no field to reference and
        # no numeric criterion to retract. The only publishable answer is the
        # honest non-answer.
        if scope != FROZEN_SCOPE or inference != "criterion_not_stated":
            raise CriterionRejectedError(
                "no criterion was frozen before the run, so the only conclusion available "
                "is criterion_not_stated within the frozen scope"
            )
        return {
            "outcome": found["outcome"],
            "inference": "criterion_not_stated",
            "conclusion_scope": FROZEN_SCOPE,
            "criterion_met": None,
            "host_inference": found["inference"],
            "evidence_field": None,
            "limitations": [],
        }
    field = validate_evidence_ref(evidence_ref, criterion)
    if inference != found["inference"]:
        raise CriterionRejectedError(
            f"inference {inference!r} is not what the verified report shows "
            f"({found['inference']!r})"
        )
    return {
        "outcome": found["outcome"],
        "inference": inference,
        "conclusion_scope": FROZEN_SCOPE,
        "criterion_met": found["criterion_met"],
        "host_inference": found["inference"],
        "evidence_field": field,
        "limitations": [],
    }


def build_interpretation(
    *,
    question: str,
    criterion: dict[str, Any] | None,
    report: dict[str, Any],
    report_identity: dict[str, Any],
    inference: str,
    evidence_ref: Any = None,
    conclusion_scope: Any = None,
    model: dict[str, Any] | None,
    versions: dict[str, Any],
    data_scope: Any = None,
) -> dict[str, Any]:
    """Render the published conclusion. Every sentence here is a host constant."""
    claim = resolve_claim(
        criterion,
        report,
        inference=inference,
        evidence_ref=evidence_ref,
        conclusion_scope=conclusion_scope,
    )
    reading = observed(criterion, report, identity=report_identity)
    codes = set(claim["limitations"]) | set(
        host_limitations(report, data_scope=data_scope, declared_scope=claim["conclusion_scope"])
    )
    ordered = [code for code in LIMITATION_CODES if code in codes]
    return {
        "schema": INTERPRETATION_SCHEMA,
        "question": question,
        "question_is_user_input": True,
        "criterion": criterion,
        "criterion_met": claim["criterion_met"],
        "observed": reading,
        "conclusion_scope": claim["conclusion_scope"],
        "conclusion_scope_text": SCOPE_TEXT[claim["conclusion_scope"]],
        "host_inference": claim["host_inference"],
        "outcome": claim["outcome"],
        "outcome_text": OUTCOME_TEXT[claim["outcome"]],
        "inference": claim["inference"],
        "inference_text": INFERENCE_TEXT[claim["inference"]],
        "limitations": [{"code": code, "text": LIMITATION_TEXT[code]} for code in ordered],
        "report": report_identity,
        "data_scope": data_scope if type(data_scope) is dict else None,
        "model": model,
        "versions": versions,
    }


def rerender_interpretation(
    published: dict[str, Any],
    *,
    report: dict[str, Any],
    report_identity: dict[str, Any],
) -> dict[str, Any] | None:
    """Rebuild one published conclusion against a freshly verified report.

    Only the frozen claim references persisted with the conclusion are used, so
    a stale stored rendering is replaced by what the verified report says now.
    ``None`` means those references no longer describe this report and the
    conclusion must not be served.

    A conclusion rendered under other semantics is a different case and raises
    :class:`InterpretationUnsupportedError`: this build cannot honestly
    re-derive it, and pretending it is stale would call a technical gap a
    withdrawn claim.
    """
    if type(published) is not dict:
        return None
    versions = published.get("versions")
    renderer = versions.get("renderer") if type(versions) is dict else None
    if published.get("schema") != INTERPRETATION_SCHEMA or renderer != INTERPRETATION_SCHEMA:
        raise InterpretationUnsupportedError(
            "the stored conclusion was rendered under "
            f"{published.get('schema')!r}/{renderer!r}, not {INTERPRETATION_SCHEMA!r}"
        )
    inference = published.get("inference")
    if not isinstance(inference, str) or inference not in INFERENCE_TEXT:
        return None
    scope = published.get("data_scope")
    try:
        criterion = decode_criterion(published.get("criterion"))
        return build_interpretation(
            question=str(published.get("question") or ""),
            criterion=criterion,
            report=report,
            report_identity=report_identity,
            inference=inference,
            evidence_ref=None if criterion is None else {"field": criterion["field"]},
            conclusion_scope=published.get("conclusion_scope"),
            model=published.get("model"),
            versions=cast(dict[str, Any], versions or {}),
            data_scope=scope,
        )
    except CriterionRejectedError:
        return None


CONCLUDE_TOOL_NAME = "conclude"


def interpretation_tool() -> dict[str, Any]:
    """The one tool the interpretation call may use. It carries no numbers."""
    return {
        "name": CONCLUDE_TOOL_NAME,
        "description": (
            "Record one bounded conclusion about the verified report. You choose codes "
            "only: the host reads every value from the report and writes every sentence, "
            "and the host's own reading of the frozen criterion is in the packet. "
            f"inference must be one of {', '.join(INFERENCE_CODES)}, and it must be the "
            "code that matches the host reading you were given. Point evidence_ref at the "
            f"frozen criterion field. conclusion_scope is {FROZEN_SCOPE}, the one scope the "
            "confirmed experiment fixed: a conclusion wider than this single frozen run is "
            "refused, because nothing in this task's accepted input declares one. These are "
            "the only arguments: any other argument, and any number, is refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "inference": {"type": "string", "enum": list(INFERENCE_CODES)},
                "evidence_ref": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": sorted(FIELD_SPECS_BY_PATH)}
                    },
                    "required": ["field"],
                    "additionalProperties": False,
                },
                "conclusion_scope": {"type": "string", "enum": list(CONCLUSION_SCOPES)},
            },
            "required": ["inference"],
            "additionalProperties": False,
        },
    }


def claim_selection(arguments: Any) -> dict[str, Any]:
    """Read one ``conclude`` call into the three closed arguments it may carry.

    This is a shape check and nothing more: an argument the vocabulary does not
    define, or a value of the wrong type, is a refusal. Semantics are decided
    later by :func:`resolve_claim` against the verified report, so the call can
    be persisted first and read back through this same function.
    """
    if type(arguments) is not dict:
        raise CriterionRejectedError("the conclusion call must carry one object")
    arguments = cast(dict[str, Any], arguments)
    unknown = sorted(set(arguments) - {"inference", "evidence_ref", "conclusion_scope"})
    if unknown:
        raise CriterionRejectedError(f"unknown conclusion argument: {unknown[0]!r}")
    inference = arguments.get("inference")
    if not isinstance(inference, str):
        raise CriterionRejectedError("inference must be one code")
    evidence_ref = arguments.get("evidence_ref")
    if evidence_ref is not None:
        if type(evidence_ref) is not dict:
            raise CriterionRejectedError("evidence_ref must be one object")
        unknown_ref = sorted(set(cast(dict[str, Any], evidence_ref)) - {"field"})
        if unknown_ref:
            raise CriterionRejectedError(f"unknown evidence_ref key: {unknown_ref[0]!r}")
        if not isinstance(cast(dict[str, Any], evidence_ref).get("field"), str):
            raise CriterionRejectedError("evidence_ref.field must be one report path")
    scope = arguments.get("conclusion_scope")
    if scope is not None and not isinstance(scope, str):
        raise CriterionRejectedError("conclusion_scope must be one code")
    return {"inference": inference, "evidence_ref": evidence_ref, "conclusion_scope": scope}


def packet(
    *,
    question: str,
    criterion: dict[str, Any] | None,
    report: dict[str, Any],
    report_identity: dict[str, Any],
    confirmed_spec: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    data_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A projection of verified facts for the model to read, never an authority.

    Values and units are included so the model can understand the situation, and
    the host's own reading is included so the allowed answer is visible rather
    than guessable. Nothing here is accepted back: the host re-reads the report
    when it renders.
    """
    economics = report.get("economics")
    schema = report_schema_of(report)
    available: dict[str, Any] = {}
    for spec in FIELD_SPECS:
        if schema is not None and not spec.applies_to(schema):
            continue
        available[spec.path] = read_field(report, spec.path)
    return {
        "question": question,
        "question_is_user_input": True,
        "criterion": criterion,
        "criterion_observed": host_reading(criterion, report, identity=report_identity),
        "report_identity": report_identity,
        "report_schema": schema,
        "report_economics": economics if isinstance(economics, dict) else {},
        "allowlisted_fields": available,
        "confirmed_spec": confirmed_spec,
        "job": job,
        "data_scope": data_scope,
        "host_added_limitations": host_limitations(report, data_scope=data_scope),
        "pending_inference_codes": list(INFERENCE_CODES),
        "conclusion_scopes": list(CONCLUSION_SCOPES),
        "default_conclusion_scope": FROZEN_SCOPE,
    }


def identity_of(report: dict[str, Any], record: Any) -> dict[str, Any]:
    """Exact verified identity of the one report a conclusion may reference."""
    source = cast(dict[str, Any], report.get("source") or {})
    completion = cast(dict[str, Any], report.get("completion") or {})
    return {
        "job_id": getattr(record, "job_id", None),
        "report_schema": report.get("schema"),
        "run_id": report.get("run_id"),
        "report_sha256": getattr(record, "report_sha256", None),
        "semantic_outcome_sha256": completion.get("semantic_outcome_sha256"),
        "input_sha256": source.get("input_sha256") or getattr(record, "input_sha256", None),
    }
