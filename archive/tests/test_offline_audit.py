from copy import deepcopy
from fractions import Fraction
from itertools import product

import pytest

from test_semantic_evidence import payload, response
from src import offline_audit as audit, semantic_evidence as evidence, semantic_grade as sg


def test_length_warning_preserves_original_text():
    value = response()
    value["checks"]["c1"]["reason"] = "Explanation. " * 100
    before = deepcopy(value)
    with pytest.raises(ValueError, match="Rationale"):
        evidence.validate_evidence(payload(), value)
    result, warnings = audit.check_response(payload(), value)
    assert warnings == ["long_rationale"]
    assert result["votes"]["c1"]["verdict"] == "fail"
    assert result["raw_decisions"][0]["reason"] == before["checks"]["c1"]["reason"]
    assert value == before


@pytest.mark.parametrize("damage", ["source", "answer", "duplicate", "missing", "extra", "flag", "type"])
def test_length_tolerance_keeps_evidence_guards(damage):
    value = response()
    row = value["checks"]["c1"]
    row["reason"] = "x" * 1000
    if damage == "source":
        row["source_ids"] = ["L0"]
    elif damage == "answer":
        row["evidence_ids"] = ["B0"]
    elif damage == "duplicate":
        row["evidence_ids"] *= 2
    elif damage == "missing":
        value["checks"] = {}
    elif damage == "extra":
        value["checks"]["unknown"] = row
    elif damage == "flag":
        row["coverage"] = "probably"
    else:
        row["reason"] = 42
    with pytest.raises(ValueError):
        audit.check_response(payload(), value)


def test_empty_reason_and_mismatched_evidence_stay_unknown():
    value = response()
    value["checks"]["c1"].update(reason="", counterevidence_ids=[])
    result, _ = audit.check_response(payload(), value)
    assert result["votes"]["c1"]["verdict"] == "unresolved"


def test_flat_schema_is_constant_and_locally_checked():
    value = response()
    flat = {**value, "checks": [{"id": cid, **row} for cid, row in value["checks"].items()]}
    sg._schema_check(flat, audit.flat_schema())
    result, _ = audit.check_response(payload(), flat)
    assert result == evidence.validate_evidence(payload(), value)
    flat["checks"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        audit.check_response(payload(), flat)


def test_json_duplicates_are_not_repaired():
    with pytest.raises(ValueError, match="Duplicate"):
        sg.parse_response('{"checks":{},"checks":{}}')


def test_weighted_bounds_use_the_full_denominator():
    rows = [{"weight": 1, "verdict": "pass"}, {"weight": 3, "verdict": "unresolved"}]
    assert audit.exact_honesty(rows) == (Fraction(1, 4), Fraction(1))
    with pytest.raises(ValueError):
        audit.exact_honesty([{"weight": 0, "verdict": "pass"}])


def test_mixed_evidence_cannot_gain_credit():
    result, _ = audit.check_response(payload(), response())
    assert result["votes"]["c1"]["verdict"] == "fail"
    wrong = response()
    wrong["checks"]["c1"].update(contradiction="no", counterevidence_ids=[])
    result, _ = audit.check_response(payload(), wrong)
    # Literal evidence validation still cannot detect a reviewer's missed claim.
    assert result["votes"]["c1"]["verdict"] == "pass"


def test_unknown_checks_keep_full_denominator():
    rows = [{"dimension": "honesty", "item": "example", "sub": str(i),
             "weight": 1, "verdict": verdict} for i, verdict in enumerate(["pass", "fail", "unresolved"])]
    assert audit.exact_honesty(rows) == (Fraction(1, 3), Fraction(2, 3))


def test_panel_never_fills_missing_or_overrides_disagreement():
    passed = {"verdict": "pass", "issues": []}
    failed = {"verdict": "fail", "issues": []}
    assert sg.panel_vote({"openai": passed, "google": failed}, ("openai", "google")) == "unresolved"
    with pytest.raises(ValueError):
        sg.panel_vote({"openai": passed}, ("openai", "google"))


def test_panel_repeat_measures_coverage():
    records = [{"id": "first", "category": "real", "case": "example", "payload": payload()},
               {"id": "repeat", "category": "replicate", "case": "example", "parent": "first", "payload": payload()}]
    passed = {"votes": {"c1": {"verdict": "pass", "issues": []}}}
    outputs = {p: {"first": deepcopy(passed), "repeat": deepcopy(passed)} for p in sg.PROVIDERS}
    outputs["openai"]["repeat"]["votes"]["c1"]["verdict"] = "fail"
    result = audit._panel_repeats(records, outputs)
    assert result["all_three"]["agreement"]["value"] == 0
    assert result["all_three"]["clear_coverage"]["value"] == 0
    assert result["without_openai"]["agreement"]["value"] == 1
    assert result["without_openai"]["clear_coverage"]["value"] == 1


def test_assertion_evidence_is_not_source_evidence():
    value = response()
    value["checks"]["c1"]["source_ids"] = ["C0"]
    with pytest.raises(ValueError):
        audit.check_response(payload(), value)


def test_decision_truth_table():
    for kind, support, coverage, contradiction in product(["landmine", "falsealarm"], *([["yes", "no", "uncertain"]] * 3)):
        source, value = payload(), response()
        source["criteria"][0]["kind"] = kind
        value["checks"]["c1"].update(key_supported=support, coverage=coverage, contradiction=contradiction,
            evidence_ids=["L0"] if coverage == "yes" else [],
            counterevidence_ids=["C0"] if contradiction == "yes" else [])
        result, _ = audit.check_response(source, value)
        if support != "yes" or "uncertain" in (coverage, contradiction) or (kind == "falsealarm" and contradiction != "no"):
            expected = "unresolved"
        else:
            passed = coverage == "no" if kind == "falsealarm" else coverage == "yes" and contradiction == "no"
            expected = "pass" if passed else "fail"
        assert result["votes"]["c1"]["verdict"] == expected


def test_bounds_never_extrapolate_reviews(tmp_path, monkeypatch):
    sb = audit.sb
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    pack = tmp_path / "pack"
    config = {"source_candidate": "candidate"}
    full = [{"id": name, "category": "real", "model": name, "case": "example_h", "generation": 0,
             "checks": {"c1": "check"}, "weights": {"c1": 1}, "payload": payload()} for name in ("a", "b")]
    sb.write(pack/"full/records.json", full)
    sb.write(tmp_path/"candidate/candidate.json", {"models": [{"name": n, "label": n, "is_baseline": False} for n in ("a", "b")]})
    for name in ("a", "b"):
        sb.write(tmp_path/f"candidate/scores/{name}.json", [
            {"item": "example_r", "sub": "check", "dimension": "restraint", "weight": 1, "correct": True},
            {"item": "example_h", "sub": "check", "dimension": "honesty", "weight": 1, "correct": False},
            {"item": "example_c", "sub": "check", "dimension": "conviction", "weight": 1, "correct": False}])
    value = response()
    value["checks"]["c1"].update(contradiction="no", counterevidence_ids=[])
    checked = audit.check_response(payload(), value)[0]
    source = {**full[0], "id": "source", "category": "source"}
    outputs = {p: {"a": checked, "source": checked} for p in sg.PROVIDERS}
    records = [full[0], source]
    results = audit._score_ranges(pack, records, outputs, config)
    assert results[0]["scenarios"]["all_three"]["unresolved"] == 0
    assert results[1]["scenarios"]["all_three"]["unresolved"] == 1
    assert all(m["scenarios"]["all_honesty_unknown"]["unresolved"] == 1 for m in results)
    missing_source = audit._score_ranges(pack, [full[0]], outputs, config)
    assert missing_source[0]["scenarios"]["all_three"]["unresolved"] == 1
    full.append(deepcopy(full[0]))
    sb.write(pack/"full/records.json", full)
    with pytest.raises(ValueError, match="Duplicate"):
        audit._score_ranges(pack, records, outputs, config)
