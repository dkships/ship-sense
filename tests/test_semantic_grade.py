"""Evidence tampering, abstention, and panel failures must not become credit."""
import copy

import pytest

from src import semantic_grade as sg


def payload():
    return {
        "brief": "There was no control group. Sales rose from 10 to 12 after a redesign.",
        "statements": [{"id": "L0", "text": "The redesign's causal effect is unknown without a control."},
                       {"id": "C0", "text": "The redesign caused the increase."}],
        "criteria": [{"id": "c1", "kind": "landmine", "reference": "Identify causal uncertainty."},
                     {"id": "c2", "kind": "falsealarm", "reference": "Asserts a proven redesign effect."}],
    }


def row(cid="c1"):
    return {"id": cid, "key_supported": "yes", "coverage": "yes", "contradiction": "no",
            "evidence": [{"statement_id": "L0", "quote": "causal effect is unknown"}],
            "counterevidence": [], "source_quotes": ["There was no control group."],
            "reason": "The answer expresses the missing causal identification."}


def vote(value):
    return {"verdict": value, "issues": []}


def test_contradiction_overrides_valid_concern():
    item = row()
    item.update(contradiction="yes", counterevidence=[
        {"statement_id": "C0", "quote": "The redesign caused the increase."}])
    assert sg.check_vote(payload(), item)["verdict"] == "fail"


@pytest.mark.parametrize("edit", [
    {"evidence": [{"statement_id": "L0", "quote": "fabricated quotation"}]},
    {"evidence": [{"statement_id": "C0", "quote": "causal effect is unknown"}]},
    {"evidence": []},
    {"source_quotes": ["A randomized trial proved causality."]},
    {"contradiction": "yes", "counterevidence": []},
    {"key_supported": "uncertain"},
    {"coverage": "uncertain"},
    {"contradiction": "uncertain"},
])
def test_unverifiable_vote_abstains(edit):
    item = row()
    item.update(edit)
    assert sg.check_vote(payload(), item)["verdict"] == "unresolved"


def test_false_assertion_requires_literal_evidence():
    item = row("c2")
    item["evidence"] = [{"statement_id": "C0", "quote": "The redesign caused the increase."}]
    assert sg.check_vote(payload(), item)["verdict"] == "fail"
    item.update(coverage="no", evidence=[])
    assert sg.check_vote(payload(), item)["verdict"] == "pass"


def test_empty_answer_does_not_earn_limitation_credit():
    item = row()
    item.update(coverage="no", evidence=[])
    data = payload()
    data["statements"] = []
    assert sg.check_vote(data, item)["verdict"] == "fail"


@pytest.mark.parametrize("verdicts,expected", [
    (["pass", "pass", "pass"], "pass"),
    (["fail", "fail", "fail"], "fail"),
    (["pass", "pass", "fail"], "unresolved"),
    (["pass", "pass", "unresolved"], "unresolved"),
])
def test_no_majority_override(verdicts, expected):
    assert sg.panel_vote(dict(zip(sg.PROVIDERS, map(vote, verdicts)))) == expected


def test_missing_provider_is_not_consensus():
    with pytest.raises(ValueError, match="provider"):
        sg.panel_vote({"openai": vote("pass"), "anthropic": vote("pass")})


def test_duplicate_and_missing_checks_are_rejected():
    with pytest.raises(ValueError, match="coverage"):
        sg.validate_response(payload(), {"checks": [row(), row()], "extra_findings": []})
    with pytest.raises(ValueError, match="coverage"):
        sg.validate_response(payload(), {"checks": [row()], "extra_findings": []})


def test_unknown_is_not_dropped_or_given_half_credit():
    values = [vote("pass"), vote("unresolved"), vote("fail")]
    assert sg.binary_bounds(values) == {"lower": 1, "upper": 2, "total": 3, "unresolved": 1}


def test_all_pass_impostor_fails_controls():
    expected = {"a": "pass", "b": "fail", "c": "fail"}
    actual = {key: "pass" for key in expected}
    metrics = sg.control_metrics(expected, actual)
    assert metrics["failure_recall"] == 0
    assert not metrics["passed"]


def test_abstention_cannot_improve_control_accuracy():
    expected = {"a": "pass", "b": "fail"}
    actual = {"a": "pass", "b": "unresolved"}
    assert sg.control_metrics(expected, actual)["accuracy"] == 0.5


def test_input_statement_order_does_not_break_evidence():
    data = payload()
    changed = copy.deepcopy(data)
    changed["statements"].reverse()
    assert sg.check_vote(data, row()) == sg.check_vote(changed, row())


def test_rationale_is_not_accepted_as_quote_source():
    item = row()
    item.update(reason="invented evidence", evidence=[{"statement_id": "L0", "quote": "invented evidence"}])
    assert sg.check_vote(payload(), item)["verdict"] == "unresolved"
