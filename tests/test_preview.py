"""Preview tracks (v4.8): store, prompts, graders, gates, runner, report.

Synthetic cases and the mock provider only; no provider call anywhere.
"""
import json
import math
import re
import subprocess
import uuid

import numpy as np
import pytest
import yaml

from src import (batch, grade, loader, preview, preview_complete, preview_gates, preview_grade,
                 preview_report, preview_run, providers)
from src.preview import CaseScope, Track

PY_ROOT = preview.ROOT
SENTINEL = "ZZSENTINEL"


# --- Fixtures ----------------------------------------------------------------------------------
def _run_id(tag: str) -> str:
    return f"pytest-preview-{tag}-{uuid.uuid4().hex[:8]}"


def _claimcheck_record(case_id="cc_test_case", labels=None, tones=None):
    labels = labels or ["SUPPORTED", "CONTRADICTED", "CANNOT_TELL", "SUPPORTED"]
    tones = tones or ["favourable", "unfavourable", "neutral", "favourable"]
    return {
        "id": case_id, "type": "claimcheck", "ledger": f"{SENTINEL}_ledger",
        "relationship": "consulting_client", "product_key": f"{SENTINEL}_product",
        "event_year": 1987, "ai_related": False, "evidence": "client_document",
        "source": f"{SENTINEL}_source", "scaled": False,
        "why_hard": f"{SENTINEL}_why", "deid_notes": f"{SENTINEL}_deid",
        "source_check": f"{SENTINEL}_check", "drafted_by": f"{SENTINEL}_drafter",
        "brief": "A plain brief with a table of numbers. " * 20,
        "claims": [{"id": f"k{n}", "text": f"Claim number {n} says something {'long ' * n}.",
                    "label": label, "basis": f"{SENTINEL}_basis_{n}",
                    "origin": "stated_in_source" if n == 1 else "written_for_balance",
                    "tone": tone}
                   for n, (label, tone) in enumerate(zip(labels, tones), 1)],
    }


def _forecast_record(case_id="fc_test_case", plan=987654, actual=123457):
    return {
        "id": case_id, "type": "forecast", "ledger": f"{SENTINEL}_ledger",
        "relationship": "employer", "product_key": f"{SENTINEL}_product",
        "event_year": 1987, "decision_date": f"{SENTINEL}_date", "ai_related": False,
        "evidence": "measured_export", "source": f"{SENTINEL}_source",
        "fixed_goal": f"{SENTINEL}_goal", "scaled": False,
        "why_hard": f"{SENTINEL}_why", "deid_notes": f"{SENTINEL}_deid",
        "brief": "What was known on the decision date. " * 30,
        "questions": [{"id": "q1", "text": "How many by the end of the quarter (count)?",
                       "unit": "count", "plan": plan, "actual": actual,
                       "actual_source": f"{SENTINEL}_actual_source"}],
    }


def _write_cases(root, records):
    for record in records:
        directory = root / record["type"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{record['id']}.yaml").write_text(yaml.safe_dump(record))


@pytest.fixture
def sentinel_store(tmp_path, monkeypatch):
    """A private store of sentinel-laden cases, swapped in for previews/."""
    _write_cases(tmp_path, [_claimcheck_record(), _forecast_record()])
    monkeypatch.setattr(preview, "PREVIEWS_DIR", tmp_path)
    return tmp_path


def _batch_model() -> str:
    _, models = loader.load_models()
    return next(m["name"] for m in models
                if m["provider"] == "anthropic" and m.get("batch_supported"))


# --- Store and views ---------------------------------------------------------------------------
@pytest.mark.parametrize("track", list(Track))
def test_examples_load_three_per_track(track):
    cases = preview.load_cases(track, CaseScope.EXAMPLES)
    assert len(cases) == 3
    assert all(case.is_example and case.path.stem == case.id for case in cases)
    assert preview.load_cases(track, CaseScope.ALL, [cases[0].id])[0].id == cases[0].id


def _all_keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | set().union(*(_all_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_all_keys(v) for v in node)) if node else set()
    return set()


def test_no_key_field_reaches_a_prompt(sentinel_store):
    """Every key field carries a sentinel value (or a distinctive number); none
    appears in the model view, the prompt, or a provider batch request."""
    model = _batch_model()
    by_name = {m["name"]: m for m in loader.load_models()[1]}
    for track in Track:
        (case,) = preview.load_cases(track, CaseScope.ALL)
        assert not _all_keys(case.model_view) & set(preview.KEY_FIELDS)
        prompt = json.dumps(preview.messages(case))
        request = json.dumps(batch.provider_request(
            "cid", by_name[model], preview.messages(case), track.value,
            preview.provider_item(case), 1024))
        for text in (prompt, request, json.dumps(preview.provider_item(case))):
            assert SENTINEL not in text
            for number in ("987654", "123457", "1987"):
                assert number not in text
            for word in ("favourable", "unfavourable", "stated_in_source",
                         "written_for_balance", "consulting_client", "employer"):
                assert word not in text
        assert set(case.key) >= {"product_key", "relationship", "source"}


def test_unknown_field_fails_closed(tmp_path):
    record = _claimcheck_record()
    record["claims"][0]["secret_note"] = "x"
    with pytest.raises(ValueError, match="unknown field"):
        preview.parse_case(record, Track.CLAIMCHECK)
    record = _forecast_record()
    record["owner_opinion"] = "x"
    with pytest.raises(ValueError, match="unknown field"):
        preview.parse_case(record, Track.FORECAST)


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.update(id="other"), "file name"),
    (lambda r: r["claims"][0].update(label="TRUE"), "label"),
    (lambda r: r["claims"][1].update(id="k1"), "unique"),
    (lambda r: r.pop("product_key"), "missing"),
])
def test_invalid_claimcheck_cases_are_rejected(mutate, message):
    record = _claimcheck_record()
    mutate(record)
    with pytest.raises(ValueError, match=message):
        preview.parse_case(record, Track.CLAIMCHECK, stem="cc_test_case")


def test_forecast_needs_positive_plan_and_actual():
    with pytest.raises(ValueError, match="positive"):
        preview.parse_case(_forecast_record(actual=0), Track.FORECAST)


def test_unknown_pilot_case_is_an_error(sentinel_store):
    with pytest.raises(ValueError, match="not found"):
        preview.load_cases(Track.CLAIMCHECK, CaseScope.ALL, ["cc_missing"])


# --- Prompts -----------------------------------------------------------------------------------
def test_forecast_template_has_no_example_numbers():
    text = preview.FORECAST_TEMPLATE.template
    for allowed in ("80%", "10th", "90th", "q1"):
        text = text.replace(allowed, "")
    assert not re.search(r"\d", text)
    assert "10th percentile" in preview.FORECAST_TEMPLATE.template
    assert "90th percentile" in preview.FORECAST_TEMPLATE.template


def test_claimcheck_template_names_each_label_equally():
    text = preview.CLAIMCHECK_TEMPLATE.template
    counts = {label: len(re.findall(rf"\b{label}\b", text)) for label in preview.LABELS}
    assert len(set(counts.values())) == 1 and counts["SUPPORTED"] > 0
    format_line = text.splitlines()[-1]
    assert "<SUPPORTED|CONTRADICTED|CANNOT_TELL>" in format_line


def test_claim_order_is_a_fixed_hash_and_renumbered():
    record = _claimcheck_record(labels=["SUPPORTED"] * 6, tones=["neutral"] * 6)
    first = preview.parse_case(record, Track.CLAIMCHECK)
    second = preview.parse_case(json.loads(json.dumps(record)), Track.CLAIMCHECK)
    shown = [c["id"] for c in first.model_view["claims"]]
    assert shown == [f"c{n}" for n in range(1, 7)]
    assert first.display_to_item == second.display_to_item
    assert sorted(first.display_to_item.values()) == [f"k{n}" for n in range(1, 7)]
    # Pinned: sha256-seeded order, identical on every machine and Python run.
    assert list(first.display_to_item.values()) == preview._shuffled(
        "cc_test_case", [f"k{n}" for n in range(1, 7)])
    texts = [c["text"] for c in first.model_view["claims"]]
    assert all(t in preview.user_prompt(first) for t in texts)


def test_template_fingerprint_is_stable_and_covers_templates(monkeypatch):
    before = preview.template_fingerprint()
    assert before == preview.template_fingerprint()
    monkeypatch.setattr(preview, "SHUFFLE_SALT", "changed")
    assert preview.template_fingerprint() != before


def test_reply_schemas_follow_visible_ids():
    case = preview.parse_case(_claimcheck_record(), Track.CLAIMCHECK)
    schema = providers._json_schema("claimcheck", preview.provider_item(case))
    assert schema["properties"]["labels"]["required"] == ["c1", "c2", "c3", "c4"]
    case = preview.parse_case(_forecast_record(), Track.FORECAST)
    schema = providers._json_schema("forecast", preview.provider_item(case))
    q1 = schema["properties"]["forecasts"]["properties"]["q1"]
    assert q1["required"] == ["low", "median", "high"]
    assert q1["properties"]["median"] == {"type": "number"}


# --- Graders -----------------------------------------------------------------------------------
def _cc_case():
    return preview.parse_case(_claimcheck_record(), Track.CLAIMCHECK)


def _answer_for(case, by_item: dict) -> str:
    item_to_display = {v: k for k, v in case.display_to_item.items()}
    return json.dumps({"labels": {item_to_display[k]: v for k, v in by_item.items()},
                       "reasons": {}})


def test_claimcheck_exact_match_through_display_ids():
    case = _cc_case()
    truth = {c["id"]: c["label"] for c in case.key["claims"]}
    rows = preview_grade.grade_claimcheck(case, _answer_for(case, truth), 1)
    assert [r["score"] for r in rows] == [1.0] * 4
    assert {r["generation"] for r in rows} == {1}
    assert {r["sub"] for r in rows} == set(truth)


def test_claimcheck_missing_and_unreadable_labels_are_wrong():
    case = _cc_case()
    answer = _answer_for(case, {"k1": "supported", "k2": "maybe", "k3": "Cannot tell"})
    rows = {r["sub"]: r for r in preview_grade.grade_claimcheck(case, answer, 0)}
    assert rows["k1"]["score"] == 1.0          # case and spacing normalised
    assert rows["k2"]["score"] == 0.0 and rows["k2"]["answer"] is None
    assert rows["k3"]["score"] == 1.0
    assert rows["k4"]["score"] == 0.0 and rows["k4"]["answer"] is None


def test_claimcheck_unparseable_is_ungraded_but_wrong_shape_is_wrong():
    case = _cc_case()
    assert preview_grade.grade_claimcheck(case, "no json here", 0) is None
    rows = preview_grade.grade_claimcheck(case, json.dumps({"labels": ["SUPPORTED"]}), 0)
    assert len(rows) == 4 and not any(r["score"] for r in rows)


def test_wis_matches_hand_computation():
    # y = 2, m = 2.5 (log10), l = 2, u = 3: (0.25 + 0.1) / 1.5
    value = preview_grade.wis(100, 100, 10 ** 2.5, 1000)
    assert value == pytest.approx(0.35 / 1.5)
    # Outside the interval adds the full miss: y = 0, l = 1, m = 1, u = 1
    assert preview_grade.wis(1, 10, 10, 10) == pytest.approx((0.5 + 1.0) / 1.5)
    assert preview_grade.wis(1, 1e6, 1e6, 1e6) == preview_grade.WIS_CAP


@pytest.mark.parametrize("entry", [
    None, {}, {"low": 1, "median": 2}, {"low": 0, "median": 2, "high": 3},
    {"low": 3, "median": 2, "high": 4}, {"low": True, "median": 2, "high": 3},
    {"low": "x", "median": 2, "high": 3}, {"low": 1, "median": float("nan"), "high": 3},
])
def test_invalid_forecasts_score_the_cap(entry):
    case = preview.parse_case(_forecast_record(), Track.FORECAST)
    raw = json.dumps({"forecasts": {"q1": entry}}, allow_nan=True)
    (row,) = preview_grade.grade_forecast(case, raw, 0)
    assert row["wis"] == preview_grade.WIS_CAP and row["valid"] is False


def test_forecast_valid_row_and_numeric_strings():
    case = preview.parse_case(_forecast_record(plan=100, actual=80), Track.FORECAST)
    raw = json.dumps({"forecasts": {"q1": {"low": "50", "median": 80, "high": 120}}})
    (row,) = preview_grade.grade_forecast(case, raw, 1)
    assert row["valid"] and row["inside"] and row["abs_log_err"] == pytest.approx(0)
    assert preview_grade.grade_forecast(case, "nothing", 0) is None


# v4.8 fix round (FIX-LIST A1, lead ruling): a reply is graded only when read in full.
def _two_question_case():
    record = _forecast_record(plan=100, actual=80)
    record["questions"].append({**record["questions"][0], "id": "q2"})
    return preview.parse_case(record, Track.FORECAST)


FULL = {"forecasts": {"q1": {"low": 50, "median": 80, "high": 120},
                      "q2": {"low": 60, "median": 90, "high": 130}}}


def test_stray_closing_brace_is_unreadable_not_the_cap():
    # The shared reader's repair drops the last field (q2's high); grading that as an
    # invalid forecast put a complete forecast at the cap. It is now ungraded.
    case = _two_question_case()
    raw = json.dumps(FULL) + "}"
    assert grade.parse_json(raw, grade.Reader.V4_9)["forecasts"]["q2"].get("high") is None   # the lossy repair
    assert preview_grade.grade_forecast(case, raw, 0) is None


def test_fenced_reply_is_read_in_full():
    case = _two_question_case()
    raw = "```json\n" + json.dumps(FULL) + "\n```"
    rows = preview_grade.grade_forecast(case, raw, 0)
    assert [r["valid"] for r in rows] == [True, True]


def test_cut_off_reply_that_loses_a_field_is_unreadable():
    case = _two_question_case()
    text = json.dumps(FULL)
    raw = text[:text.rindex('"high"')]                    # cut off before q2's high
    assert preview_grade.grade_forecast(case, raw, 0) is None
    cc = _cc_case()
    truth = {c["id"]: c["label"] for c in cc.key["claims"]}
    full_cc = _answer_for(cc, truth)
    assert preview_grade.grade_claimcheck(cc, full_cc[:full_cc.rindex('",') ], 0) is None


def test_cut_off_reply_that_keeps_every_graded_field_is_graded():
    cc = _cc_case()
    truth = {c["id"]: c["label"] for c in cc.key["claims"]}
    item_to_display = {v: k for k, v in cc.display_to_item.items()}
    labels = {item_to_display[k]: v for k, v in truth.items()}
    raw = json.dumps({"labels": labels})[:-1] + ', "reasons": {"c1": "because the tab'
    rows = preview_grade.grade_claimcheck(cc, raw, 0)
    assert rows is not None and [r["score"] for r in rows] == [1.0] * 4


def test_readable_reply_missing_one_forecast_still_scores_the_cap():
    case = _two_question_case()
    raw = json.dumps({"forecasts": {"q1": FULL["forecasts"]["q1"]}})
    q1, q2 = preview_grade.grade_forecast(case, raw, 0)
    assert q1["valid"] and q2["wis"] == preview_grade.WIS_CAP and q2["valid"] is False


# --- Gates and baselines -----------------------------------------------------------------------
def test_always_label_scores_equal_label_shares():
    cases = preview.load_cases(Track.CLAIMCHECK, CaseScope.EXAMPLES)
    result = preview_gates.evaluate(Track.CLAIMCHECK, cases)
    shares = {c["check"]: c["value"] for c in result["balance"]}
    gates = {g["check"]: g["value"] for g in result["gates"]}
    for label in preview.LABELS:
        assert gates[f"always_{label}"] == pytest.approx(shares[f"label_share_{label}"])
    assert result["passed"]


def test_tone_policy_and_length_policy():
    case = preview.parse_case(_claimcheck_record(
        labels=["CONTRADICTED", "SUPPORTED", "CANNOT_TELL", "SUPPORTED"],
        tones=["favourable", "unfavourable", "neutral", "favourable"]), Track.CLAIMCHECK)
    assert preview_gates.policy_score([case], preview_gates.tone_policy) == 0.75
    labels = preview_gates.length_policy(case, True, "CONTRADICTED", "SUPPORTED")
    longest = max(case.model_view["claims"], key=lambda c: len(c["text"]))["id"]
    assert labels[longest] == "CONTRADICTED"
    assert sum(v == "CONTRADICTED" for v in labels.values()) == 1


def test_tone_gate_fails_when_tone_predicts_the_key():
    records = [_claimcheck_record(f"cc_t{n}", labels=["CONTRADICTED", "SUPPORTED",
                                                    "CANNOT_TELL", "CONTRADICTED"],
                                  tones=["favourable", "unfavourable", "neutral", "favourable"])
               for n in range(3)]
    cases = [preview.parse_case(r, Track.CLAIMCHECK) for r in records]
    gates = {g["check"]: g for g in preview_gates.claimcheck_gates(cases)["gates"]}
    assert gates["tone_policy"]["value"] == 1.0 and not gates["tone_policy"]["passed"]


def test_balance_caps_products_and_employer_share():
    records = [_forecast_record(f"fc_{n}") for n in range(4)]   # all employer, one product
    cases = [preview.parse_case(r, Track.FORECAST) for r in records]
    checks = {c.name: c for c in preview_gates.forecast_balance(cases)}
    assert not checks["max_cases_per_product"].passed
    assert not checks["employer_share"].passed


def test_hindsight_constant_beats_or_ties_fixed_baselines():
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES)
    base = preview_gates.forecast_baselines(cases)
    assert base["hindsight"]["mean_wis"] <= base["plan"]["mean_wis"] + 1e-12
    assert base["hindsight"]["mean_wis"] <= base["haircut"]["mean_wis"] + 1e-12
    # The vectorised grid is the scorer's formula: check it at the found point.
    hind = base["hindsight"]
    rows = preview_gates.baseline_rows(cases, lambda p: preview_gates.constant_forecast(
        p, hind["multiplier"], hind["factor"]))
    assert np.mean([r["wis"] for r in rows]) == pytest.approx(hind["mean_wis"])


def test_baselines_follow_the_spec():
    assert preview_gates.plan_forecast(100) == (80, 100, 125)
    assert preview_gates.haircut_forecast(100) == (25, 50, 100)


# --- Runner, grade, completeness, report -------------------------------------------------------
MOCKS = ["mock-strong", "mock-weak", "mock-naive"]


@pytest.mark.parametrize("track", list(Track))
def test_live_mock_end_to_end_and_resumable(track):
    run_id = _run_id(track.value)
    cases = preview.load_cases(track, CaseScope.EXAMPLES)
    first = preview_run.run_live(MOCKS, run_id, track, cases, scope=CaseScope.EXAMPLES)
    assert all(v["done"] == len(cases) * preview_run.GENERATIONS for v in first.values())
    again = preview_run.run_live(MOCKS, run_id, track, cases, scope=CaseScope.EXAMPLES)
    assert all(v["done"] == 0 for v in again.values())

    paths = preview_run.RunPaths(run_id, track)
    trace = json.loads(paths.trace("mock-strong", cases[0].id).read_text())
    assert len(trace) == 2 and trace[0]["max_tokens"] == 8192
    preview_run.grade_run(run_id, track)
    errors, reports = preview_complete.errors_for(run_id, track, MOCKS)
    assert errors == []
    assert reports["mock-strong"]["expected"] == 2 * len(cases)

    aggregate = preview_report.write(run_id, track)
    text = (paths.base / preview_report.AGGREGATE_FILE).read_text()
    json.loads(text)                                  # strict JSON: no NaN
    report = (paths.base / preview_report.REPORT_FILE).read_text()
    for case in cases:
        for blob in (text, report):
            assert case.id not in blob
            assert case.model_view["brief"][:40] not in blob
            for entry in case.model_view[preview.ITEMS_FIELD[track]]:
                assert entry["text"] not in blob
    assert set(aggregate["models"]) == set(MOCKS)
    assert "Peak/cap" in report


def test_claimcheck_naive_mock_is_counted_ungraded():
    run_id = _run_id("naive")
    cases = preview.load_cases(Track.CLAIMCHECK, CaseScope.EXAMPLES)
    preview_run.run_live(["mock-naive"], run_id, Track.CLAIMCHECK, cases,
                         scope=CaseScope.EXAMPLES)
    (scores,) = preview_run.grade_run(run_id, Track.CLAIMCHECK).values()
    assert scores["rows"] == [] and len(scores["ungraded"]) == 2 * len(cases)


def test_forecast_weak_mock_scores_the_cap():
    run_id = _run_id("weak")
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES)
    preview_run.run_live(["mock-weak"], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES)
    (scores,) = preview_run.grade_run(run_id, Track.FORECAST).values()
    assert {r["wis"] for r in scores["rows"]} == {preview_grade.WIS_CAP}


def test_live_lane_refuses_batch_routed_models():
    cases = preview.load_cases(Track.CLAIMCHECK, CaseScope.EXAMPLES)
    with pytest.raises(ValueError, match="batch-first"):
        preview_run.run_live([_batch_model()], _run_id("refuse"), Track.CLAIMCHECK, cases)


def test_run_refuses_a_changed_case_or_cap(sentinel_store):
    run_id = _run_id("changed")
    (case,) = preview.load_cases(Track.CLAIMCHECK, CaseScope.ALL)
    preview_run.run_live(["mock-strong"], run_id, Track.CLAIMCHECK, [case], scope=CaseScope.ALL)
    with pytest.raises(ValueError, match="output cap"):
        preview_run.run_live(["mock-strong"], run_id, Track.CLAIMCHECK, [case],
                             scope=CaseScope.ALL, max_tokens=100)
    record = _claimcheck_record()
    record["claims"][0]["label"] = "CANNOT_TELL"
    _write_cases(sentinel_store, [record])
    (changed,) = preview.load_cases(Track.CLAIMCHECK, CaseScope.ALL)
    with pytest.raises(ValueError, match="case changed"):
        preview_run.run_live(["mock-strong"], run_id, Track.CLAIMCHECK, [changed],
                             scope=CaseScope.ALL)


def _anthropic_line(cid: str, text: str, stop: str = "end_turn", out: int = 200) -> str:
    return json.dumps({"custom_id": cid, "result": {"type": "succeeded", "message": {
        "id": f"msg_{cid}", "model": "m", "stop_reason": stop,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1000, "output_tokens": out}}}})


def test_batch_prepare_ingest_and_resume(sentinel_store):
    model = _batch_model()
    run_id = _run_id("batch")
    (case,) = preview.load_cases(Track.CLAIMCHECK, CaseScope.ALL)
    out = preview_run.prepare_batch([model, "mock-strong"], run_id, Track.CLAIMCHECK,
                                    [case], scope=CaseScope.ALL, max_tokens=1000)
    assert set(out) == {model}
    manifest_path = out[model]
    manifest = json.loads(manifest_path.read_text())
    assert manifest["provider"] == "anthropic" and len(manifest["requests"]) == 2
    requests = (PY_ROOT / manifest["requests_file"]).read_text()
    assert SENTINEL not in requests and '"max_tokens":1000' in requests
    # An open stage blocks a second prepare.
    again = preview_run.prepare_batch([model], run_id, Track.CLAIMCHECK, [case],
                                      scope=CaseScope.ALL, max_tokens=1000)
    assert "open stage" in again[model]

    truth = {c["id"]: c["label"] for c in case.key["claims"]}
    answer = _answer_for(case, truth)
    first, second = (r["custom_id"] for r in manifest["requests"])
    results = manifest_path.parent / "results.jsonl"
    results.write_text(_anthropic_line(first, answer) + "\n"
                       + _anthropic_line(second, answer, stop="max_tokens", out=1000) + "\n")
    counts = preview_run.ingest(manifest_path, results)
    assert counts == {"merged": 2, "errors": 0, "unknown": 0, "missing": 0}

    paths = preview_run.RunPaths(run_id, Track.CLAIMCHECK)
    traces = json.loads(paths.trace(model, case.id).read_text())
    assert [t["truncated"] for t in traces] == [False, True]
    assert all(t["max_tokens"] == 1000 for t in traces)
    costs = json.loads(paths.costs(model).read_text())
    assert costs["truncated"] == 1 and costs["peak_share_of_cap"] == 1.0
    assert costs["estimated_cost_usd"] is not None

    done = preview_run.prepare_batch([model], run_id, Track.CLAIMCHECK, [case],
                                     scope=CaseScope.ALL, max_tokens=1000)
    assert done[model] == "complete"

    preview_run.grade_run(run_id, Track.CLAIMCHECK)
    errors, reports = preview_complete.errors_for(run_id, Track.CLAIMCHECK, [model])
    assert any("truncated without a waiver" in e for e in errors)
    assert reports[model]["truncated"] == 1 and reports[model]["peak_share_of_cap"] == 1.0
    policy = {"truncation_waivers": {model: {case.id: [1]}}}
    (paths.base / preview_complete.POLICY_FILE).write_text(json.dumps(policy))
    errors, _ = preview_complete.errors_for(run_id, Track.CLAIMCHECK, [model])
    assert errors == []


def test_batch_error_lines_stay_pending(sentinel_store):
    model = _batch_model()
    run_id = _run_id("batcherr")
    (case,) = preview.load_cases(Track.FORECAST, CaseScope.ALL)
    out = preview_run.prepare_batch([model], run_id, Track.FORECAST, [case], scope=CaseScope.ALL)
    manifest = json.loads(out[model].read_text())
    cid = manifest["requests"][0]["custom_id"]
    results = out[model].parent / "results.jsonl"
    results.write_text(json.dumps({"custom_id": cid, "result": {"type": "errored",
                                                                 "error": {"type": "x"}}}) + "\n")
    counts = preview_run.ingest(out[model], results)
    assert counts["errors"] == 1 and counts["missing"] == 1
    paths = preview_run.RunPaths(run_id, Track.FORECAST)
    assert preview_run.pending_generations(paths, model, case.id) == [0, 1]
    # The cap defaults to the model's board cap (src.run.output_cap).
    defaults, models = loader.load_models()
    cfg = next(m for m in models if m["name"] == model)
    from src import run
    assert manifest["max_tokens"] == run.output_cap(cfg, defaults)


# --- Statistics --------------------------------------------------------------------------------
def _rows(model_acc: float, n_cases: int = 20, claims: int = 5, seed: int = 0):
    rng = np.random.default_rng(seed)
    return [{"item": f"c{i}", "sub": f"k{j}", "generation": g, "weight": 1.0,
             "score": float(rng.random() < model_acc)}
            for i in range(n_cases) for j in range(claims) for g in range(2)]


def test_score_interval_and_paired_compare():
    strong, weak = _rows(0.9, seed=1), _rows(0.4, seed=2)
    mean, lo, hi = preview_report.score_interval(strong, Track.CLAIMCHECK)
    assert lo <= mean <= hi
    result = preview_report.compare(strong, weak, Track.CLAIMCHECK)
    assert result["diff"] > 0 and result["p"] < 0.05
    assert result["ci95"][0] > 0 and result["method"] == preview_report.EXACT_SIGNFLIP
    mirrored = preview_report.compare(weak, strong, Track.CLAIMCHECK)
    assert mirrored["diff"] == pytest.approx(-result["diff"])


def test_forecast_interval_is_on_the_wis_scale():
    rows = [{"item": f"c{i}", "sub": "q1", "generation": 0, "weight": 1.0, "score": 0.2 * (i % 3)}
            for i in range(9)]
    mean, lo, hi = preview_report.score_interval(rows, Track.FORECAST)
    assert mean == pytest.approx(0.2) and lo <= mean <= hi
    other = [{**r, "score": r["score"] + 0.3} for r in rows]
    result = preview_report.compare(other, rows, Track.FORECAST)
    assert result["diff"] == pytest.approx(0.3)
    assert result["method"] == preview_report.MC_SIGNFLIP_BOOTSTRAP


def test_split_half_reliability_orders_distinct_models():
    per_model = {f"m{k}": _rows(acc, n_cases=30, seed=k)
                 for k, acc in enumerate((0.2, 0.4, 0.6, 0.8, 0.95))}
    result = preview_report.split_half_order(per_model, draws=200)
    assert result["split_half"] > 0.7 and result["cases"] == 30
    assert preview_report.split_half_order(dict(list(per_model.items())[:2]))["split_half"] is None


def test_spearman_handles_ties():
    assert preview_report.spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)
    assert math.isnan(preview_report.spearman([1, 1, 1], [1, 2, 3]))


def test_confusion_counts_missing_answers():
    rows = [{"truth": "SUPPORTED", "answer": "SUPPORTED"},
            {"truth": "SUPPORTED", "answer": None},
            {"truth": "CANNOT_TELL", "answer": "CONTRADICTED"}]
    table = preview_report.confusion(rows)
    assert table["counts"]["SUPPORTED"]["MISSING"] == 1
    assert table["accuracy_by_true_label"]["SUPPORTED"] == 0.5
    assert table["accuracy_by_true_label"]["CONTRADICTED"] is None


# --- Privacy plumbing --------------------------------------------------------------------------
def test_real_preview_cases_are_gitignored():
    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=PY_ROOT).returncode == 0

    for path in ("previews/claimcheck/cc_real.yaml", "previews/forecast/fc_real.yaml",
                 "previews/notes.md", "previews/claimcheck/drafts/x.yaml"):
        assert ignored(path), path
    for path in ("previews/claimcheck/example_cc_x.yaml", "previews/forecast/example_fc_x.yaml"):
        assert not ignored(path), path


def test_export_refuses_a_real_preview_case(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("export_public",
                                                  PY_ROOT / "scripts" / "export_public.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / ".git").mkdir()

    def fake_check_output(cmd):
        if "ls-files" in cmd:
            return b"README.md\0previews/claimcheck/cc_real.yaml\0"
        return b""

    monkeypatch.setattr(module.subprocess, "check_output", fake_check_output)
    with pytest.raises(ValueError, match="private file"):
        module.export(tmp_path)


def test_gemini_typed_schema_accepts_forecast_numbers():
    types = pytest.importorskip("google.genai.types")
    case = preview.parse_case(_forecast_record(), Track.FORECAST)
    spec = providers._json_schema("forecast", preview.provider_item(case))
    typed = providers._typed_from_json_schema(spec, types)
    q1 = typed.properties["forecasts"].properties["q1"]
    assert q1.properties["median"].type == types.Type.NUMBER
