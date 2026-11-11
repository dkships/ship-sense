"""Output-cap rule 9 (NEXT_VERSION 10i): an answer a model never gives scores zero.

Synthetic examples and the mock provider only; no provider call.
"""
import json

import pytest

from src import (complete, grade, leaderboard, loader, pairwise, panel_grade, regrade,
                 report, run, unanswered)

MODELS = ["mock-strong", "mock-weak", "mock-naive"]


def _examples():
    return {it["type"]: it for it in loader.load_cases(case_scope=loader.CASE_SCOPE_EXAMPLES)}


def _shape(rows):
    return [(r["item"], r["dimension"], r["sub"], r["weight"], r.get("kind")) for r in rows]


@pytest.mark.parametrize("kind", ["restraint", "honesty", "conviction"])
def test_zero_rows_have_a_real_answers_shape_and_no_credit(kind):
    item = _examples()[kind]
    key = item["_key"]
    if kind == "restraint":
        real = grade.grade_restraint({"classifications": {f: "SHIP" for f in key["labels"]}}, key)
    elif kind == "honesty":
        real = grade.grade_honesty({"limitations": ["a limit"], "conclusions": ["a finding"]},
                                   key, item=item)
    else:
        recs = {grade.SETUP_TURN: "SHIP", **{t["id"]: "SHIP" for t in key["turns"]}}
        real = grade.grade_conviction(recs, key, item=item)
    zero = unanswered.zero_rows(item)
    assert _shape(zero) == _shape(real) and zero
    assert all(float(r["correct"]) == 0.0 for r in zero)


def _isolated(tmp_path, monkeypatch):
    for module in (run, report, leaderboard, complete, regrade, unanswered, panel_grade):
        monkeypatch.setattr(module, "ROOT", tmp_path)
    run_id = "pytest-unanswered"
    run.run(MODELS, run_id, only_examples=True)
    return run_id


def _blank_one(tmp_path, run_id, model="mock-strong", kind="restraint"):
    """Make one generation of `model` unreadable, as an answer that never came back."""
    item_id = _examples()[kind]["id"]
    path = tmp_path / "outputs" / run_id / "raw" / f"{model}__{item_id}.json"
    raws = json.loads(path.read_text())
    raws[0] = ""
    path.write_text(json.dumps(raws))
    return item_id


def _register(tmp_path, run_id, entries):
    path = tmp_path / "outputs" / run_id / "policy.json"
    policy = json.loads(path.read_text()) if path.exists() else {}
    policy["unanswered"] = entries
    path.write_text(json.dumps(policy))


def _snapshot(run_id):
    per_model = report.load_scores(run_id, loader.CASE_SCOPE_ALL)
    snap = leaderboard.build_snapshot(run_id, per_model, loader.model_meta(),
                                      case_scope=loader.CASE_SCOPE_EXAMPLES)
    return per_model, {m["name"]: m for m in snap["models"]}, snap


def test_an_unregistered_missing_answer_still_fails_and_stays_provisional(tmp_path, monkeypatch):
    run_id = _isolated(tmp_path, monkeypatch)
    _blank_one(tmp_path, run_id)
    regrade.regrade(run_id)
    errors = complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES)
    assert any("mock-strong: " in e and "raw generations incomplete" in e for e in errors)
    _, rows, _ = _snapshot(run_id)
    assert rows["mock-strong"]["ranked_eligible"] is False


def test_a_registered_unanswered_answer_scores_zero_and_the_model_is_ranked(tmp_path, monkeypatch):
    run_id = _isolated(tmp_path, monkeypatch)
    item_id = _blank_one(tmp_path, run_id)
    _register(tmp_path, run_id, [{"model": "mock-strong", "item": item_id, "generation": 0,
                                  "attempts": [{"cap": 65536, "result": "cut off"}]}])
    regrade.regrade(run_id)
    assert complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES) == []

    per_model, rows, snap = _snapshot(run_id)
    assert rows["mock-strong"]["ranked_eligible"] is True
    zeroed = [r for r in per_model["mock-strong"] if r["item"] == item_id]
    assert zeroed and all(float(r["correct"]) == 0.0 for r in zeroed)

    leaderboard.mark_unanswered(snap, unanswered.counts(run_id))
    assert rows["mock-strong"]["unanswered"] == {"count": 1, "answers": rows["mock-strong"]["n_items"]}
    assert leaderboard._unanswered_text(rows["mock-strong"]) == "1 of 5 answers unanswered, scored zero"

    # It enters the paired tests: identical coverage across the board.
    scored = {n: r for n, r in per_model.items() if not leaderboard._is_baseline(n)}
    order, _ = pairwise.rank(scored)
    records = pairwise.compare(scored, order, n=200)
    assert any("mock-strong" in (r["a"], r["b"]) for r in records)


def test_an_entry_without_attempts_is_refused(tmp_path, monkeypatch):
    run_id = _isolated(tmp_path, monkeypatch)
    item_id = _blank_one(tmp_path, run_id)
    _register(tmp_path, run_id, [{"model": "mock-strong", "item": item_id, "generation": 0,
                                  "attempts": []}])
    errors = complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES)
    assert any("no attempts" in e for e in errors)


def test_a_registered_generation_must_not_hold_a_readable_answer(tmp_path, monkeypatch):
    run_id = _isolated(tmp_path, monkeypatch)
    item_id = _examples()["restraint"]["id"]
    _register(tmp_path, run_id, [{"model": "mock-strong", "item": item_id, "generation": 0,
                                  "attempts": [{"cap": 65536}]}])
    regrade.regrade(run_id)
    errors = complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES)
    assert any("holds a readable answer" in e for e in errors)


def test_a_model_never_reached_stays_provisional(tmp_path, monkeypatch):
    run_id = _isolated(tmp_path, monkeypatch)
    for path in (tmp_path / "outputs" / run_id / "raw").glob("mock-weak__*.json"):
        if _examples()["honesty"]["id"] in path.name:
            path.unlink()
    regrade.regrade(run_id)
    _, rows, _ = _snapshot(run_id)
    assert rows["mock-weak"]["ranked_eligible"] is False
    errors = complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES)
    assert any(e.startswith("mock-weak: ") for e in errors)
