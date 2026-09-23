import json

import pytest

from src import semantic_diagnostics as diagnostic


def record():
    return {"id": "one", "payload": {"brief": "A fact", "statements": [],
        "criteria": [{"id": "c1", "kind": "landmine", "reference": "A concern"}]}}


def envelope():
    return {"custom_id": "one", "result": {"type": "succeeded", "message": {
        "model": "grader", "stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"checks": ['}],
        "usage": {"input_tokens": 100, "output_tokens": 200}}}}


def test_invalid_grade_still_has_billable_usage(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(envelope()) + "\n")
    before = path.read_bytes()
    rows, envelopes, errors = diagnostic.diagnostic_rows(path, [record()], "grader", "anthropic")
    assert errors
    assert rows["one"]["votes"]["c1"] == {"verdict": "unresolved", "issues": ["invalid_response"]}
    config = {"batch_rate_bounds": {"anthropic": [1.25, 5]}, "max_output_tokens": 300}
    usage = diagnostic.usage_report(envelopes, [{}], [record()], "anthropic", config)
    assert usage["conservative_usage_usd"] == "0.001125"
    assert usage["usage_complete"]
    assert not usage["budget_allowance_released"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("error", ["missing", "duplicate", "unknown"])
def test_roster_failure_is_not_a_diagnostic_grade(tmp_path, error):
    path = tmp_path / "results.jsonl"
    rows = [envelope()]
    if error == "missing":
        rows = []
    elif error == "duplicate":
        rows *= 2
    else:
        rows[0]["custom_id"] = "invented"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError):
        diagnostic.diagnostic_rows(path, [record()], "grader", "anthropic")


@pytest.mark.parametrize("usage", [{}, {"input_tokens": 100, "output_tokens": 301},
                                  {"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 1}])
def test_usage_failure_retains_reservation(usage):
    row = envelope()
    row["result"]["message"]["usage"] = usage
    config = {"batch_rate_bounds": {"anthropic": [1.25, 5]}, "max_output_tokens": 300}
    report = diagnostic.usage_report({"one": row}, [{}], [record()], "anthropic", config)
    assert report["conservative_usage_usd"] is None
    assert not report["usage_complete"]
    assert not report["budget_allowance_released"]


def test_uncertainty_resamples_families():
    report = diagnostic.interval([("family_a", True)] * 100 + [("family_b", False)] * 100)
    assert report["clusters"] == 2
    assert report["value"] == 0.5
    assert (report["lo"], report["hi"]) == (0, 1)
