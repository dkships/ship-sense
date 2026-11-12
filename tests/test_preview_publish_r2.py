"""previews_round2.json and its page section (v4.9, NEXT_VERSION section 11). Every run
here is a mock run on synthetic cases under outputs/pytest-*; no test reads notes/ or a
real run, so the suite passes on the public copy."""
import copy
import json
from pathlib import Path
import uuid

import pytest
import yaml

from src import leaderboard as lb, preview, preview_publish_r2 as pub, preview_round2 as r2, preview_run
from src.preview import CaseScope, Track
from src.preview_run import Mode, RunPaths

MODELS = ["mock-strong", "mock-weak"]
SMALL_CC_BOUNDS = r2.Bounds("test-claimcheck", items_per_case=(4, 6), label_share=(0.1, 0.6),
                            min_trap_share=0.3, min_cases=3, min_orgs=2)


def _examples(track: Track) -> list[dict]:
    return [yaml.safe_load(p.read_text())
            for p in sorted((preview.PREVIEWS_DIR / track.value).glob("example_*.yaml"))]


def _set(root: Path) -> Path:
    """A round-two set built from the committed examples (renamed ids)."""
    orgs = {}
    (root / "claimcheck").mkdir(parents=True)
    for n, record in enumerate(_examples(Track.CLAIMCHECK)):
        for i, claim in enumerate(record["claims"]):
            claim["trap"] = "small_n" if i % 2 == 0 else "none"
        (root / "claimcheck" / f"{record['id']}.yaml").write_text(yaml.safe_dump(record))
        orgs[record["id"]] = f"org-cc{n % 2}"
    for folder in (r2.RESOLVED_FOLDER, r2.LIVE_FOLDER):
        (root / folder).mkdir()
    for n, record in enumerate(_examples(Track.FORECAST)):
        rec = copy.deepcopy(record)
        rec["id"] = f"{rec['id']}_r2"
        rec[r2.ORG_FIELD] = f"org-fc{n % 2}"
        for q in rec["questions"]:
            q["kind"] = "plan"
            q["naive_last"] = q["plan"] * 0.8
        (root / r2.RESOLVED_FOLDER / f"{rec['id']}.yaml").write_text(yaml.safe_dump(rec))
        live = copy.deepcopy(rec)
        live["id"] = f"{rec['id']}_live"
        for q in live["questions"]:
            q.update(kind="live", resolves_on=f"2026-12-{10 + n}")
            q.pop("actual")
            q.pop("actual_source", None)
        (root / r2.LIVE_FOLDER / f"{live['id']}.yaml").write_text(yaml.safe_dump(live))
    (root / r2.MANIFEST_FILE).write_text(json.dumps({
        r2.BOUNDS_FIELD: {"claimcheck": SMALL_CC_BOUNDS.to_json(), "forecast": r2.ROUND_TWO_FORECAST.to_json()},
        r2.ORG_KEYS_FIELD: orgs}))
    return root


def _ledger(path: Path) -> Path:
    path.write_text(json.dumps({"runs": [{"version": "v4.8", "models": [
        {"name": "mock-strong", "label": "Mock (strong)", "provider": "mock", "score": {"value": 90.0},
         "is_baseline": False, "ranked_eligible": True},
        {"name": "mock-weak", "label": "Mock (weak)", "provider": "mock", "score": {"value": 70.0},
         "is_baseline": False, "ranked_eligible": True}]}]}))
    return path


def _run(run_id, track, models, root=None, folder=None, mode=Mode.NORMAL, grade=True):
    cases = preview.load_cases(track, CaseScope.ALL, root=root, folder=folder)
    preview_run.run_live(models, run_id, track, cases, scope=CaseScope.ALL, mode=mode)
    if grade:
        preview_run.grade_run(run_id, track)


@pytest.fixture
def world(tmp_path):
    """Every run the publish step reads, as mock runs on synthetic sets."""
    tag = uuid.uuid4().hex[:8]
    root = _set(tmp_path / "sets" / "r2")
    runs = pub.Runs(**{f: f"pytest-pub-{f}-{tag}" for f in pub.Runs.__dataclass_fields__})
    _run(runs.claimcheck, Track.CLAIMCHECK, MODELS, root)
    _run(runs.forecast, Track.FORECAST, MODELS, root)
    _run(runs.live, Track.FORECAST, MODELS, root, r2.LIVE_FOLDER)
    _run(runs.nobrief, Track.FORECAST, MODELS, root, mode=Mode.NO_BRIEF)
    _run(runs.nobrief_live, Track.FORECAST, MODELS, root, r2.LIVE_FOLDER, mode=Mode.NO_BRIEF)
    _run(runs.v48, Track.FORECAST, MODELS)                                  # v4.8-format examples
    _run(runs.nobrief_v48, Track.FORECAST, MODELS, mode=Mode.NO_BRIEF)
    _run(runs.calibration, Track.CLAIMCHECK, MODELS, root)
    late = {r["id"] for r in _examples(Track.FORECAST)}
    v48_orgs = {cid: "org-old" for cid in late}
    return runs, late, v48_orgs, _ledger(tmp_path / "leaderboard.json"), root


def test_build_covers_section_11f(world):
    runs, late, orgs, ledger, _ = world
    data = pub.build(runs, late, orgs, ledger=ledger)
    cc, fc, live = (data["tracks"][k] for k in ("claimcheck", "forecast", "live"))
    assert not data["partial"] and data["version"] == "v4.9" and data["in_headline"] is False
    m = cc["models"]["mock-strong"]
    for key in ("accuracy", "ci95_org", "ci95_case", "errors_per_100", "errors_per_100_ci95_org",
                "accuracy_by_true_label", "ungraded_generations", "unanswered_generations"):
        assert key in m
    assert cc["gates"] and cc["bounds"] and cc["reliability"] is not None and cc["headline_agreement"]
    assert set(cc["findings_by_trap_type"]["mock-strong"]) <= set(preview.TRAPS)
    assert set(fc["views"]) == {"pooled", "new_only", "pooled_without_late_context"}
    for view in fc["views"].values():
        assert "reliability" in view and "headline_agreement" in view
    pooled = fc["views"]["pooled"]["models"]["mock-strong"]
    assert {"score", "ci95_org", "ci95_case", "typical_miss_factor", "coverage_80"} <= set(pooled)
    assert set(fc["paired_vs_baselines"]["mock-strong"]) == {"plan", "haircut", "hindsight",
                                                             "naive_last", "naive_run_rate"}
    assert set(fc["nobrief_difference"]["mock-strong"]) == {"v48", "new", "both"}
    assert live["questions"] == 6 and live["resolved"] == 0 and live["scores_on_resolved"] == {}
    assert live["answers_expected"] == live["answers_collected"] == 3 * 2 * len(MODELS)
    assert live["answers_unreadable"] == 0
    assert set(data["calibration"]) == set(MODELS) and data["key_audit"]["control"]["caught_by_each_judge"] == 40
    fits = fc["hindsight_fits"]
    assert fits["new_plan_questions"]["questions"] == 6
    assert fits["pooled_plan_questions"]["questions"] == 12 and "v4.8's grid" in fits["pooled_plan_questions"]["grid"]
    assert "questions_used" in fc["paired_vs_baselines"]["mock-strong"]["plan"]
    assert set(fc["methods"]) >= {"nobrief", "split_half", "score"} and "errors_per_100" in cc["methods"]


def test_build_refuses_an_incomplete_model_naming_model_and_run(world):
    runs, late, orgs, ledger, root = world
    fresh = pub.Runs(**{**runs.__dict__, "nobrief_live": f"pytest-pub-missing-{uuid.uuid4().hex[:6]}"})
    _run(fresh.nobrief_live, Track.FORECAST, ["mock-strong"], root, r2.LIVE_FOLDER, mode=Mode.NO_BRIEF)
    with pytest.raises(pub.Incomplete, match=f"mock-weak on {fresh.nobrief_live}: not registered"):
        pub.build(fresh, late, orgs, ledger=ledger)
    ungraded = pub.Runs(**{**runs.__dict__, "forecast": f"pytest-pub-ungraded-{uuid.uuid4().hex[:6]}"})
    _run(ungraded.forecast, Track.FORECAST, MODELS, root, grade=False)
    with pytest.raises(pub.Incomplete, match="not graded"):
        pub.build(ungraded, late, orgs, ledger=ledger)
    data = pub.build(fresh, late, orgs, allow_partial=True, ledger=ledger)
    assert data["tracks"]["claimcheck"]["models"]


def test_late_context_list_must_name_three(world):
    runs, late, orgs, ledger, _ = world
    with pytest.raises(ValueError, match="three"):
        pub.build(runs, set(list(late)[:2]), orgs, ledger=ledger)


def test_live_scores_appear_only_on_resolved_questions(world, tmp_path):
    runs, late, orgs, ledger, root = world
    live = preview.load_cases(Track.FORECAST, CaseScope.ALL, root=root, folder=r2.LIVE_FOLDER)
    res = tmp_path / "res.yaml"
    res.write_text(yaml.safe_dump([{"case": live[0].id, "question": "q1", "value": 10,
                                    "read_on": "2026-12-11", "read_by": "owner"}]))
    preview_run.main(["resolve", "--track", "forecast", "--run-id", runs.live, "--resolutions", str(res)])
    data = pub.build(runs, late, orgs, ledger=ledger)
    assert data["tracks"]["live"]["resolved"] == 1 and set(data["tracks"]["live"]["scores_on_resolved"]) == set(MODELS)


# --- Privacy ------------------------------------------------------------------------------------
def test_published_data_and_page_carry_no_case_content(world, monkeypatch, tmp_path):
    runs, late, orgs, ledger, root = world
    data = pub.build(runs, late, orgs, ledger=ledger)
    text = json.dumps(data)
    cases = [c for t, f in ((Track.CLAIMCHECK, None), (Track.FORECAST, None), (Track.FORECAST, r2.LIVE_FOLDER))
             for c in preview.load_cases(t, CaseScope.ALL, root=root, folder=f)]
    private = pub.private_texts(cases, ["org-cc0", "org-fc1"])
    assert pub.scan(text, private) == []
    path = tmp_path / "previews_round2.json"
    path.write_text(text)
    monkeypatch.setattr(lb, "PREVIEWS_R2_FILE", path)
    html = lb.current_previews_section()
    assert "errors per 100 claims" in html and "live questions" in html
    assert pub.scan(html, private) == []


@pytest.mark.parametrize("text, hits", [
    ("model cc_pricing_page scored", ["cc_pricing_page"]),
    ('"fc_launch_signups_r2"', ["fc_launch_signups_r2"]),
    ("h" "l_synthetic_case", ["h" "l_synthetic_case"]),   # built so the prefix scan stays clean
    ("accuracy and acc_x and abc_y", []),
    ("wrong_denominator and small_n", []),
])
def test_scan_finds_case_ids_only(text, hits):
    assert pub.scan(text, []) == hits


def test_scan_finds_private_text():
    assert pub.scan("the brief opens: Revenue rose in May.", ["Revenue rose in May"]) == ["Revenue rose in May"]


def test_committed_round_two_file_is_clean_and_complete():
    data = lb.load_previews_r2()
    if data is None:
        pytest.skip("no previews_round2.json committed yet")
    assert data["in_headline"] is False and not data["partial"]
    assert pub.scan(json.dumps(data), []) == []


# --- The README block -------------------------------------------------------------------------------
def test_readme_block_shows_round_two_and_carries_no_case_content(world, monkeypatch, tmp_path):
    runs, late, orgs, ledger, root = world
    data = pub.build(runs, late, orgs, ledger=ledger)
    path = tmp_path / "previews_round2.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(lb, "PREVIEWS_R2_FILE", path)
    block = lb.current_previews_md()
    assert "Errors per 100 claims" in block and "live questions" in block and "pooled" in block
    assert "| Mock (strong) |" in block
    cases = [c for t, f in ((Track.CLAIMCHECK, None), (Track.FORECAST, None), (Track.FORECAST, r2.LIVE_FOLDER))
             for c in preview.load_cases(t, CaseScope.ALL, root=root, folder=f)]
    assert pub.scan(block, pub.private_texts(cases, ["org-cc0", "org-fc1"])) == []

    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{lb.README_PREVIEWS_START}\nold\n{lb.README_PREVIEWS_END}\noutro\n")
    assert lb.inject_previews_readme(lb.load_previews(), readme, body=block)
    text = readme.read_text()
    assert text.startswith("intro\n") and text.endswith("outro\n") and block in text and "old" not in text


def test_readme_block_is_v48s_without_round_two_file(monkeypatch, tmp_path):
    monkeypatch.setattr(lb, "PREVIEWS_R2_FILE", tmp_path / "absent.json")
    assert lb.current_previews_md() == lb.render_previews_md(lb.load_previews())


def test_committed_readme_block_matches_the_committed_data():
    readme = lb.README.read_text()
    block = readme.split(lb.README_PREVIEWS_START)[1].split(lb.README_PREVIEWS_END)[0]
    assert block.strip("\n") == lb.current_previews_md().strip("\n")


# --- The page -----------------------------------------------------------------------------------
def test_page_keeps_v48_section_without_round_two_file(monkeypatch, tmp_path):
    monkeypatch.setattr(lb, "PREVIEWS_R2_FILE", tmp_path / "absent.json")
    assert lb.current_previews_section() == lb._previews_section(lb.load_previews())


def test_text_slots_are_listed_and_plain():
    for name in lb.PREVIEW_R2_SLOTS:
        value = getattr(lb, name)
        assert isinstance(value, str) and value and "TODO" not in value and "cc_" not in value


def test_reliability_by_group_marks_each_group_exploratory():
    """Four groups from accuracy alone, each run through the registered split-half."""
    accuracy = {"low": 0.6, "a": 0.96, "b": 0.97, "c": 0.98, "d": 0.99, "e": 0.995, "f": 1.0}
    rows = {}
    for n, (name, acc) in enumerate(accuracy.items()):
        rows[name] = [{"item": f"case{k}", "score": float((k + n) % 3 != 0) * acc, "weight": 1.0}
                      for k in range(12)]
    out = pub.reliability_by_group(rows, accuracy)
    assert [g["group"] for g in out] == ["all models", "without the lowest-scoring model",
                                         "accuracy 0.99 or higher", "accuracy from 0.95 up to 0.99"]
    assert [g["models"] for g in out] == [7, 6, 3, 3]
    assert all(g["status"] == pub.EXPLORATORY_AFTER_RESULTS for g in out)
    assert out[0]["split_half"] is not None and len(out[0]["range95"]) == 2


def test_zero_error_rows_carry_the_exact_bound_and_a_dagger():
    bound = pub.zero_error_bound(285)
    assert round(bound["zero_error_upper_per_100"], 2) == 1.05 and "not part of the registration" in bound["zero_error_status"]
    zero = {"accuracy": 1.0, "ci95_org": [1.0, 1.0], "errors_per_100": 0.0,
            "errors_per_100_ci95_org": [0.0, 0.0], **bound}
    other = {"accuracy": 0.99, "ci95_org": [0.98, 1.0], "errors_per_100": 1.0,
             "errors_per_100_ci95_org": [0.0, 2.0]}
    assert lb._zero_mark(zero) == "†" and lb._zero_mark(other) == ""
    note = lb._zero_note({"models": {"a": zero, "b": other}})
    assert note == ("† No error in 285 claims. A resampled interval cannot be wider than the data, so it "
                    "prints as zero. An exact one-sided 95% bound is 1.05 errors per 100 claims.")
    assert lb._zero_note({"models": {"b": other}}) == ""


def test_round_two_caption_names_the_board_it_ran_on():
    assert (lb._r2_caption({"board_version": "v4.8"}, "v4.9")
            == "In v4.8's Ship Sense Score order, the board when this round ran. Not a ranking.")
    assert lb._r2_caption({"board_version": "v4.9"}, "v4.9") == lb.PREVIEW_TABLE_CAPTION
    assert lb._r2_caption({"board_version": "v4.8"}, None) == lb.PREVIEW_TABLE_CAPTION
    data = lb.load_previews_r2()
    if data:
        later = lb.render_previews_r2_md(data, "v9.9")
        assert f"In {data['board_version']}'s Ship Sense Score order" in later
        same = lb.render_previews_r2_md(data, data["board_version"])
        assert f"_{lb.PREVIEW_TABLE_CAPTION}_" in same
        html = lb._previews_r2_section(data, "v9.9")
        assert html.count(f"In {data['board_version']}&#x27;s Ship Sense Score order") == 2
    # Round one's tables keep the plain caption.
    assert f"_{lb.PREVIEW_TABLE_CAPTION}_" in lb.render_previews_md(lb.load_previews())
