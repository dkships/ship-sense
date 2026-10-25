"""Panel landmine credit (NEXT_VERSION §4d): the scoring rule, frozen-verdict
replay, and the regrade / completeness / gates integration. Synthetic answers and
synthetic verdicts only; nothing here calls a provider."""
from fractions import Fraction
import json

import pytest

from src import (adversarial, complete, grade, leaderboard, loader, panel_batch,
                 panel_grade, regrade, report, run)

MODELS = ["mock-strong", "mock-weak", "mock-naive"]


def _votes(a, o, g):
    return {"anthropic": a, "openai": o, "google": g}


# --- Scoring rule -----------------------------------------------------------------
def test_own_lab_vote_never_counts():
    assert panel_grade.landmine_credit(_votes(True, False, False), "anthropic") == 0.0
    assert panel_grade.landmine_credit(_votes(False, True, True), "anthropic") == 1.0


def test_two_counted_reviewers_that_split_credit_half():
    assert panel_grade.landmine_credit(_votes(True, True, False), "openai") == 0.5


def test_other_labs_take_the_share_of_all_three():
    two_thirds = panel_grade.landmine_credit(_votes(True, True, False), "xai")
    assert Fraction(two_thirds).limit_denominator(64) == Fraction(2, 3)
    assert panel_grade.landmine_credit(_votes(True, False, False), None) == pytest.approx(1 / 3)


def test_missing_verdicts_need_two_counted_reviewers():
    assert panel_grade.landmine_credit(_votes(True, None, False), "xai") == 0.5
    assert panel_grade.landmine_credit(_votes(True, None, True), "google") is None
    assert panel_grade.landmine_credit(_votes(None, None, True), "xai") is None


def test_answer_view_is_the_capped_statements_the_grader_reads():
    raw = json.dumps({"limitations": [f"l{i}" for i in range(9)],
                      "conclusions": [f"c{i}" for i in range(7)]})
    lims, concl = panel_grade.answer_view(raw)
    assert lims == [f"l{i}" for i in range(grade.MAX_LIMITATIONS)]
    assert concl == [f"c{i}" for i in range(grade.MAX_CONCLUSIONS)]
    assert panel_grade.answer_view("not json at all") is None
    assert panel_grade.answer_view(json.dumps({"limitations": [], "conclusions": []})) is None


# --- Replay through regrade and the completeness check ------------------------------
def _isolated_run(tmp_path, monkeypatch, run_id="pytest-panel"):
    for module in (run, report, leaderboard, complete, regrade, panel_grade):
        monkeypatch.setattr(module, "ROOT", tmp_path)
    run.run(MODELS, run_id, only_examples=True)
    return run_id


def _honesty_items():
    return {i["id"]: i for i in loader.load_cases(case_scope=loader.CASE_SCOPE_EXAMPLES)
            if i["type"] == "honesty"}


def _freeze_board(run_id, vote_for, pool=panel_grade.Pool.MID):
    """Write frozen board verdicts for every reviewable answer in the run, the
    way panel_batch.freeze would, with votes chosen by the test."""
    items = _honesty_items()
    jobs = panel_batch.board_jobs(run_id, items)
    entries = {}
    for job in jobs:
        votes = {lm: vote_for(job, lm) for lm in job["landmines"]}
        entries[job["key"]] = panel_grade.make_entry(job, votes)
    path = panel_grade.phase_dir(run_id, pool, panel_grade.Phase.BOARD)
    panel_grade.write_verdicts(path, {"pool": pool.value}, entries)
    return path, jobs


def _landmine_rows(run_id, model):
    rows = json.loads((panel_grade.ROOT / "outputs" / run_id / "scores" / f"{model}.json").read_text())
    return [r for r in rows if r["sub"].startswith(grade.LANDMINE_PREFIX)]


def test_regrade_takes_landmine_credit_from_frozen_verdicts(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _, jobs = _freeze_board(run_id, lambda job, lm: _votes(True, False, True))
    assert jobs and all(job["lab"] == "mock" for job in jobs)

    regrade.regrade(run_id)

    graded_models = {job["key"].split("|")[0] for job in jobs}
    for model in graded_models:
        credits = {Fraction(r["correct"]).limit_denominator(64) for r in _landmine_rows(run_id, model)}
        assert credits == {Fraction(2, 3)}
    applied = json.loads((tmp_path / "outputs" / run_id / "panel" / "applied.json").read_text())
    assert applied["source"] == panel_grade.LandmineSource.PANEL.value
    assert applied["fallbacks"] == {}
    assert complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES) == []


def test_alias_regrade_of_a_panel_run_fails_the_completeness_check(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _freeze_board(run_id, lambda job, lm: _votes(True, True, True))
    regrade.regrade(run_id, landmines=panel_grade.LandmineSource.ALIAS)
    errors = complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES)
    assert any("saved scores do not match deterministic regrade" in e for e in errors)


def test_completeness_compares_partial_credit_exactly(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _freeze_board(run_id, lambda job, lm: _votes(True, None, False))
    regrade.regrade(run_id)
    path = tmp_path / "outputs" / run_id / "scores" / "mock-strong.json"
    rows = json.loads(path.read_text())
    landmine = next(r for r in rows if r["sub"].startswith(grade.LANDMINE_PREFIX))
    assert landmine["correct"] == 0.5
    landmine["correct"] = 1.0  # truthy either way: a bool comparison would miss it
    path.write_text(json.dumps(rows))
    errors = complete.completeness_errors(run_id, MODELS, loader.CASE_SCOPE_EXAMPLES)
    assert any("saved scores do not match deterministic regrade" in e for e in errors)


def test_too_few_verdicts_fall_back_to_the_alias_verdict(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _, jobs = _freeze_board(run_id, lambda job, lm: _votes(True, None, None))
    regrade.regrade(run_id)
    applied = json.loads((tmp_path / "outputs" / run_id / "panel" / "applied.json").read_text())
    n_checks = sum(len(job["landmines"]) for job in jobs if job["key"].startswith("mock-strong|"))
    assert applied["fallbacks"]["mock-strong"] == n_checks
    assert all(isinstance(r["correct"], bool) for r in _landmine_rows(run_id, "mock-strong"))


def test_a_changed_answer_cannot_reuse_a_frozen_verdict(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _, jobs = _freeze_board(run_id, lambda job, lm: _votes(True, True, True))
    model, case, _ = jobs[0]["key"].split("|")
    raw_path = tmp_path / "outputs" / run_id / "raw" / f"{model}__{case}.json"
    raws = json.loads(raw_path.read_text())
    answer = grade.parse_json(raws[0])
    answer["limitations"] = ["An edited limitation the panel never read."]
    raws[0] = json.dumps(answer)
    raw_path.write_text(json.dumps(raws))
    with pytest.raises(panel_grade.PanelVerdictError, match="statements"):
        regrade.regrade(run_id)


def test_a_missing_verdict_stops_the_regrade(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    path, jobs = _freeze_board(run_id, lambda job, lm: _votes(True, True, True))
    frozen = json.loads((path / panel_grade.VERDICTS_FILE).read_text())
    del frozen["entries"][jobs[0]["key"]]
    (path / panel_grade.VERDICTS_FILE).write_text(json.dumps(frozen))
    with pytest.raises(panel_grade.PanelVerdictError, match="no frozen verdict"):
        regrade.regrade(run_id)


def test_a_changed_landmine_description_invalidates_the_verdict(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _freeze_board(run_id, lambda job, lm: _votes(True, True, True))
    items = loader.load_cases(case_scope=loader.CASE_SCOPE_EXAMPLES)
    honesty = next(i for i in items if i["type"] == "honesty")
    honesty["_key"]["landmines"][0]["desc"] = "an edited description"
    monkeypatch.setattr(regrade.loader, "load_cases", lambda **kw: items)
    with pytest.raises(panel_grade.PanelVerdictError, match="case or landmine"):
        regrade.regrade(run_id)


def test_auto_source_is_alias_without_frozen_verdicts(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    regrade.regrade(run_id)
    assert all(isinstance(r["correct"], bool) for r in _landmine_rows(run_id, "mock-strong"))
    with pytest.raises(panel_grade.PanelVerdictError, match="no frozen board verdicts"):
        regrade.regrade(run_id, landmines=panel_grade.LandmineSource.PANEL)


def test_two_frozen_pools_need_an_explicit_pool(tmp_path, monkeypatch):
    run_id = _isolated_run(tmp_path, monkeypatch)
    _freeze_board(run_id, lambda job, lm: _votes(True, True, True), panel_grade.Pool.MID)
    _freeze_board(run_id, lambda job, lm: _votes(False, False, False), panel_grade.Pool.SMALL)
    with pytest.raises(panel_grade.PanelVerdictError, match="pool"):
        regrade.regrade(run_id)
    regrade.regrade(run_id, pool=panel_grade.Pool.SMALL)
    assert {r["correct"] for r in _landmine_rows(run_id, "mock-strong")} == {0.0}


# --- Gameability gates through the panel ----------------------------------------------
HONESTY_ITEM = {
    "id": "adv_h", "type": "honesty",
    "brief": "Pilot data from one seller over a 30-day window. Refunds are a boolean flag.",
    "prompt": "What can be concluded?",
    "_key": {"id": "adv_h",
             "landmines": [{"id": "window", "desc": "30 days is too short", "aliases": ["30-day"]},
                           {"id": "sample", "desc": "one seller", "aliases": ["one seller"]}],
             "false_alarms": [{"id": "loyal", "aliases": ["loyal customer"]}]},
}


def _gate_entries(vote):
    entries = {}
    for job in panel_batch.gate_jobs([HONESTY_ITEM]):
        entries[job["key"]] = panel_grade.make_entry(job, {lm: vote for lm in job["landmines"]})
    return entries


def test_gates_through_a_credulous_panel_fail():
    rescore = panel_grade.gate_rescorer(_gate_entries(_votes(True, True, True)))
    report_ = adversarial.run_gates([HONESTY_ITEM], rescore=rescore)
    assert report_["scores"]["honesty"]["caveat-list"] > report_["gates"]["honesty"]
    assert any(f.startswith("honesty/") for f in report_["failures"])


def test_gates_through_a_strict_panel_pass_on_honesty():
    rescore = panel_grade.gate_rescorer(_gate_entries(_votes(False, False, False)))
    report_ = adversarial.run_gates([HONESTY_ITEM], rescore=rescore)
    assert not any(f.startswith("honesty/") for f in report_["failures"])


def test_gate_rescorer_refuses_an_answer_the_panel_never_read():
    rescore = panel_grade.gate_rescorer({})
    with pytest.raises(panel_grade.PanelVerdictError):
        adversarial.run_gates([HONESTY_ITEM], rescore=rescore)
