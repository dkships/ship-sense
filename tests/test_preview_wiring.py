"""Round two wired into the preview runner: named round-two sets, the live folder,
the no-brief mode (its own run, one generation), unresolved questions, output-cap
rule 9 for round-two sets only, bounds from a set's manifest, organisation
intervals and errors per 100 claims in the reports. v4.8's runs keep their form.
Mock models and synthetic cases only; nothing here spends."""
import copy
import json
from pathlib import Path
import uuid

import pytest
import yaml

from src import (loader, preview, preview_calibration as cal, preview_complete, preview_freeze as pf,
                 preview_gates, preview_nobrief as nb, preview_report, preview_round2 as r2,
                 preview_round2_report as rep, preview_run)
from src.preview import CaseScope, Track
from src.preview_run import Mode, RunPaths

ROOT = Path(__file__).resolve().parent.parent
SMALL_CC_BOUNDS = r2.Bounds("test-claimcheck", items_per_case=(4, 6), label_share=(0.1, 0.6),
                            min_trap_share=0.3, min_cases=3, min_orgs=2)


def _run_id(tag: str) -> str:
    return f"pytest-wire-{tag}-{uuid.uuid4().hex[:8]}"


def _examples(track: Track) -> list[dict]:
    return [yaml.safe_load(p.read_text()) for p in sorted((preview.PREVIEWS_DIR / track.value).glob("example_*.yaml"))]


def _batch_model() -> str:
    _, models = loader.load_models()
    return next(m["name"] for m in models if m["provider"] == "anthropic" and m.get("batch_supported"))


@pytest.fixture
def round_two_set(tmp_path) -> Path:
    """A round-two set: Claim check (traps, org keys in the manifest), resolved
    Forecast (a plan question and a series question) and live Forecast."""
    root = tmp_path / "sets" / "r2test"
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
        rec[r2.ORG_FIELD] = f"org-fc{n % 2}"
        for q in rec["questions"]:
            q["kind"] = "plan"
            q["naive_last"] = q["plan"] * 0.8
        if n == 1:
            for q in rec["questions"]:
                q["kind"] = "series"
                q.pop("plan")
        (root / r2.RESOLVED_FOLDER / f"{rec['id']}.yaml").write_text(yaml.safe_dump(rec))
        live = copy.deepcopy(rec)
        live["id"] = f"{rec['id']}_live"
        for q in live["questions"]:
            q.update(kind="live", resolves_on="2026-12-31")
            q.pop("actual")
            q.pop("actual_source", None)
        (root / r2.LIVE_FOLDER / f"{live['id']}.yaml").write_text(yaml.safe_dump(live))
    manifest = {r2.BOUNDS_FIELD: {"claimcheck": SMALL_CC_BOUNDS.to_json(),
                                  "forecast": r2.ROUND_TWO_FORECAST.to_json()},
                r2.ORG_KEYS_FIELD: orgs}
    (root / r2.MANIFEST_FILE).write_text(json.dumps(manifest))
    return root


def _record(run_id: str, track: Track) -> dict:
    return json.loads(RunPaths(run_id, track).run_file.read_text())


# --- v4.8's runs keep their form ---------------------------------------------------------------
@pytest.mark.parametrize("track", list(Track))
def test_v48_run_json_has_no_round_two_fields(track):
    run_id = _run_id("v48")
    cases = preview.load_cases(track, CaseScope.EXAMPLES)
    preview_run.run_live(["mock-strong"], run_id, track, cases, scope=CaseScope.EXAMPLES)
    record = _record(run_id, track)
    for field in (preview_run.CASES_DIR_FIELD, preview_run.FOLDER_FIELD, preview_run.MODE_FIELD,
                  preview_run.ROUND_TWO_FIELD):
        assert field not in record
    assert record["generations"] == 2 and record["template_fingerprint"] == preview.template_fingerprint()
    scores = preview_run.grade_answers(RunPaths(run_id, track), "mock-strong", cases)
    assert set(scores) == {"model", "track", "rows", "ungraded", "missing"}


def test_v48_ignores_an_unanswered_registration(tmp_path):
    run_id = _run_id("v48rule")
    cases = preview.load_cases(Track.CLAIMCHECK, CaseScope.EXAMPLES)
    preview_run.run_live(["mock-strong"], run_id, Track.CLAIMCHECK, cases, scope=CaseScope.EXAMPLES)
    paths = RunPaths(run_id, Track.CLAIMCHECK)
    raw = json.loads(paths.raw("mock-strong", cases[0].id).read_text())
    paths.raw("mock-strong", cases[0].id).write_text(json.dumps(["", raw[1]]))
    (paths.base / preview_run.POLICY_FILE).write_text(json.dumps({"unanswered": [
        {"model": "mock-strong", "item": cases[0].id, "generation": 0, "attempts": [{"cap": 8192}]}]}))
    scores = preview_run.grade_answers(paths, "mock-strong", cases)
    assert scores["missing"] == [{"item": cases[0].id, "generation": 0}] and "unanswered" not in scores


# --- The round-two runner ------------------------------------------------------------------------
def test_round_two_set_records_folder_and_rules(round_two_set):
    run_id = _run_id("live")
    live = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=round_two_set, folder=r2.LIVE_FOLDER)
    preview_run.run_live(["mock-strong"], run_id, Track.FORECAST, live, scope=CaseScope.EXAMPLES)
    record = _record(run_id, Track.FORECAST)
    assert record[preview_run.FOLDER_FIELD] == r2.LIVE_FOLDER and record[preview_run.ROUND_TWO_FIELD]
    cases = preview_run.run_cases(RunPaths(run_id, Track.FORECAST))
    assert {c.id for c in cases} == {c.id for c in live}
    scores = preview_run.grade_answers(RunPaths(run_id, Track.FORECAST), "mock-strong", cases)
    assert scores["rows"] == [] and scores["ungraded"] == [] and scores["unanswered"] == []
    assert scores["unresolved_questions"] == sum(len(c.key["questions"]) for c in live)
    with pytest.raises(ValueError, match="different folder"):
        resolved = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=round_two_set)
        preview_run.run_live(["mock-strong"], run_id, Track.FORECAST, resolved, scope=CaseScope.EXAMPLES)


def test_resolved_round_two_forecast_grades_and_reports(round_two_set):
    run_id = _run_id("fc")
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=round_two_set)
    preview_run.run_live(["mock-strong", "mock-weak"], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES)
    preview_run.grade_run(run_id, Track.FORECAST)
    aggregate = preview_report.build(run_id, Track.FORECAST)
    assert aggregate["round_two"] and "organisation" in aggregate["interval"]["method"]
    m = aggregate["models"]["mock-strong"]
    assert "ci95_case" in m and m["organisations"] == 2 and m["skill_vs_haircut"] is None
    assert set(m["baselines_paired"]) == {"plan", "haircut", "hindsight", "naive_last", "naive_run_rate"}
    text = preview_report.render(preview_report._finite(aggregate), run_id)
    assert "## Round two" in text and "vs naive_last" in text and "bound cases_with_item_count" in text


def test_forecast_report_labels_count_what_was_used(round_two_set):
    """The header counts the report's own organisations, and a paired line the questions
    the model's comparison used (a case with no graded answer drops out)."""
    run_id = _run_id("labels")
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=round_two_set)
    preview_run.run_live(["mock-strong", "mock-weak"], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES)
    paths = RunPaths(run_id, Track.FORECAST)
    broken = next(c for c in cases if any(q.get("plan") for q in c.key["questions"]))
    paths.raw("mock-weak", broken.id).write_text(json.dumps(["not json", "not json"]))
    preview_run.grade_run(run_id, Track.FORECAST)
    aggregate = preview_report._finite(preview_report.build(run_id, Track.FORECAST))
    assert aggregate["interval"]["organisations"] == len({c.key[r2.ORG_FIELD] for c in cases}) == 2
    weak = aggregate["models"]["mock-weak"]["baselines_paired"]["plan"]
    plan_qs = sum(1 for c in cases for q in c.key["questions"] if q.get("plan"))
    assert weak["questions"] == plan_qs and weak["questions_used"] == plan_qs - len(broken.key["questions"])
    text = preview_report.render(aggregate, run_id)
    assert "Main interval: resampling 2 organisations" in text
    assert f"- mock-weak vs plan ({weak['questions_used']} questions)" in text
    assert f"- mock-strong vs plan ({plan_qs} questions)" in text
    fit = aggregate["checks"]["baselines"]["hindsight"]
    assert fit["multiplier"] > 0 and fit["factor"] >= 1 and "301 points" in fit["grid"]
    assert "Hindsight constant on this set's" in text and "How these are computed" in text
    assert set(aggregate["methods"]) == {"score", "split_half", "headline_agreement", "hindsight"}


def test_nobrief_mode_is_its_own_run_at_one_generation(round_two_set):
    model = _batch_model()
    run_id = _run_id("nobrief")
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=round_two_set)
    out = preview_run.prepare_batch([model], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES,
                                    mode=Mode.NO_BRIEF)
    record = _record(run_id, Track.FORECAST)
    assert record[preview_run.MODE_FIELD] == "no-brief" and record["generations"] == 1
    assert record["template_fingerprint"] == nb.template_fingerprint()
    manifest = json.loads(out[model].read_text())
    assert len(manifest["requests"]) == len(cases)
    requests = (ROOT / manifest["requests_file"]).read_text()
    for case in cases:
        assert case.model_view["brief"][:40] not in requests
    assert "Their plan for this value" in requests
    with pytest.raises(ValueError, match="mode"):
        preview_run.prepare_batch([model], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES)


def test_nobrief_runs_on_v48_frozen_cases_without_touching_them(tmp_path):
    """v4.8-format cases (no org key) go through the no-brief path read-only."""
    frozen = tmp_path / "frozen"
    (frozen / "forecast").mkdir(parents=True)
    for record in _examples(Track.FORECAST):
        (frozen / "forecast" / f"{record['id']}.yaml").write_text(yaml.safe_dump(record))
    before = {p.name: p.read_bytes() for p in (frozen / "forecast").iterdir()}
    run_id = _run_id("v48nobrief")
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=frozen)
    preview_run.run_live(["mock-strong"], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES,
                         mode=Mode.NO_BRIEF)
    record = _record(run_id, Track.FORECAST)
    assert record["generations"] == 1 and preview_run.ROUND_TWO_FIELD not in record
    raws = json.loads(RunPaths(run_id, Track.FORECAST).raw("mock-strong", cases[0].id).read_text())
    assert len(raws) == 1
    assert {p.name: p.read_bytes() for p in (frozen / "forecast").iterdir()} == before
    preview_run.grade_run(run_id, Track.FORECAST)
    assert preview_complete.errors_for(run_id, Track.FORECAST, ["mock-strong"])[0] == []


# --- Output-cap rule 9, round two only ------------------------------------------------------------
def _cc_round_two_run(round_two_set, tag):
    run_id = _run_id(tag)
    cases = preview.load_cases(Track.CLAIMCHECK, CaseScope.EXAMPLES, root=round_two_set)
    preview_run.run_live(["mock-strong"], run_id, Track.CLAIMCHECK, cases, scope=CaseScope.EXAMPLES)
    return run_id, cases, RunPaths(run_id, Track.CLAIMCHECK)


def _register(paths, case_id, generation):
    (paths.base / preview_run.POLICY_FILE).write_text(json.dumps({"unanswered": [
        {"model": "mock-strong", "item": case_id, "generation": generation,
         "attempts": [{"cap": 8192, "lane": "live"}, {"cap": 8192, "lane": "live"}]}]}))


def test_unanswered_scores_zero_and_unreadable_stays_ungraded(round_two_set):
    run_id, cases, paths = _cc_round_two_run(round_two_set, "rule9")
    first, second = cases[0], cases[1]
    raw = json.loads(paths.raw("mock-strong", first.id).read_text())
    paths.raw("mock-strong", first.id).write_text(json.dumps(['{"labels": {"c1": "SUPP', raw[1]]))
    raw2 = json.loads(paths.raw("mock-strong", second.id).read_text())
    paths.raw("mock-strong", second.id).write_text(json.dumps(["not json at all", raw2[1]]))
    traces = json.loads(paths.trace("mock-strong", second.id).read_text())
    traces[0]["text"] = "not json at all"                # a complete reply, unreadable
    paths.trace("mock-strong", second.id).write_text(json.dumps(traces))
    _register(paths, first.id, 0)

    scores = preview_run.grade_answers(paths, "mock-strong", cases)
    zero = [r for r in scores["rows"] if r["item"] == first.id and r["generation"] == 0]
    assert len(zero) == len(first.key["claims"]) and all(r["score"] == 0.0 for r in zero)
    assert scores["unanswered"] == [{"item": first.id, "generation": 0}]
    assert scores["ungraded"] == [{"item": second.id, "generation": 0}]       # arrived, unreadable

    preview_run.grade_run(run_id, Track.CLAIMCHECK)
    errors, reports = preview_complete.errors_for(run_id, Track.CLAIMCHECK, ["mock-strong"])
    assert errors == [] and reports["mock-strong"]["unanswered"] == 1
    aggregate = preview_report.build(run_id, Track.CLAIMCHECK)
    m = aggregate["models"]["mock-strong"]
    assert m["unanswered_generations"] == 1 and m["ungraded_generations"] == 1
    assert m["errors_per_100"] == pytest.approx(100 * (1 - m["score"]))
    lo, hi = m["errors_per_100_ci95"]
    assert lo <= m["errors_per_100"] <= hi
    assert "Errors per 100 claims" in preview_report.render(preview_report._finite(aggregate), run_id)


def test_a_registered_generation_with_a_readable_answer_is_refused(round_two_set):
    _, cases, paths = _cc_round_two_run(round_two_set, "rule9bad")
    _register(paths, cases[0].id, 1)
    with pytest.raises(ValueError, match="readable answer"):
        preview_run.grade_answers(paths, "mock-strong", cases)


def test_an_unanswered_entry_needs_its_attempts(round_two_set):
    from src.unanswered import UnansweredError
    _, cases, paths = _cc_round_two_run(round_two_set, "rule9attempts")
    (paths.base / preview_run.POLICY_FILE).write_text(json.dumps({"unanswered": [
        {"model": "mock-strong", "item": cases[0].id, "generation": 0, "attempts": []}]}))
    with pytest.raises(UnansweredError):
        preview_run.grade_answers(paths, "mock-strong", cases)


def test_unanswered_forecast_scores_the_cap(round_two_set):
    run_id = _run_id("rule9fc")
    cases = preview.load_cases(Track.FORECAST, CaseScope.EXAMPLES, root=round_two_set)
    preview_run.run_live(["mock-strong"], run_id, Track.FORECAST, cases, scope=CaseScope.EXAMPLES)
    paths = RunPaths(run_id, Track.FORECAST)
    raw = json.loads(paths.raw("mock-strong", cases[0].id).read_text())
    paths.raw("mock-strong", cases[0].id).write_text(json.dumps(["", raw[1]]))
    (paths.base / preview_run.POLICY_FILE).write_text(json.dumps({"unanswered": [
        {"model": "mock-strong", "item": cases[0].id, "generation": 0, "attempts": [{"cap": 8192}]}]}))
    rows = [r for r in preview_run.grade_answers(paths, "mock-strong", cases)["rows"]
            if r["item"] == cases[0].id and r["generation"] == 0]
    assert rows and all(r["score"] == 2.0 and not r["valid"] for r in rows)


# --- Gates from the manifest, calibration, freeze helpers -------------------------------------------
def test_gates_use_the_manifests_bounds(round_two_set, capsys):
    assert preview_gates.main(["--track", "claimcheck", "--only-examples", "--cases-dir",
                               str(round_two_set)]) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["bounds"]["name"] == "test-claimcheck"
    assert {c["check"] for c in out["balance"]} >= {"organisations", "trap_share_in_set"}
    assert "best label-by-tone mapping" in captured.err
    assert preview_gates.main(["--track", "forecast", "--only-examples", "--cases-dir",
                               str(round_two_set), "--folder", r2.LIVE_FOLDER]) == 0
    assert preview_gates.main(["--track", "claimcheck", "--only-examples"]) in (0, 1)   # v4.8 path


def test_v48_gates_unchanged_without_bounds():
    cases = preview.load_cases(Track.CLAIMCHECK, CaseScope.EXAMPLES)
    out = preview_gates.evaluate(Track.CLAIMCHECK, cases)
    assert "bounds" not in out and {c["check"] for c in out["balance"]} >= {"supported_favourable_share"}


def test_calibration_report_gives_errors_per_100_on_a_round_two_set(round_two_set):
    run_id, cases, _ = _cc_round_two_run(round_two_set, "cal")
    preview_run.grade_run(run_id, Track.CLAIMCHECK)
    out = cal.report(run_id)
    m = out["models"]["mock-strong"]
    assert m["organisations"] == 2 and m["errors_per_100"] == pytest.approx(100 * (1 - m["accuracy"]))
    text = (RunPaths(run_id, Track.CLAIMCHECK).base / cal.CALIBRATION_TABLE).read_text()
    assert "Errors per 100 claims" in text


def test_round_two_manifest_fields():
    extra = pf.round_two_manifest({"cc_b": "org-2", "cc_a": "org-1"})
    assert extra[r2.BOUNDS_FIELD]["claimcheck"] == r2.ROUND_TWO_CLAIMCHECK.to_json()
    assert list(extra[r2.ORG_KEYS_FIELD]) == ["cc_a", "cc_b"]


# --- No-brief against normal, paired ----------------------------------------------------------------
def test_nobrief_report_pairs_normal_and_control_per_model(round_two_set, tmp_path):
    frozen = tmp_path / "v48"
    (frozen / "forecast").mkdir(parents=True)
    old_orgs = {}
    for record in _examples(Track.FORECAST):
        record["id"] = f"old_{record['id']}"
        (frozen / "forecast" / f"{record['id']}.yaml").write_text(yaml.safe_dump(record))
        old_orgs[record["id"]] = "org-old"
    orgs_file = tmp_path / "v48_orgs.json"
    orgs_file.write_text(json.dumps(old_orgs))
    runs = {}
    for name, root, mode in (("v48", frozen, Mode.NORMAL), ("new", round_two_set, Mode.NORMAL),
                             ("v48nb", frozen, Mode.NO_BRIEF), ("newnb", round_two_set, Mode.NO_BRIEF)):
        run_id = _run_id(name)
        cases = preview.load_cases(Track.FORECAST, CaseScope.ALL, root=root)
        preview_run.run_live(["mock-strong", "mock-weak"], run_id, Track.FORECAST, cases,
                             scope=CaseScope.ALL, mode=mode)
        preview_run.grade_run(run_id, Track.FORECAST)
        runs[name] = run_id
    out = rep.nobrief_report({"v48": runs["v48"], "new": runs["new"]},
                             {"v48": runs["v48nb"], "new": runs["newnb"]}, round_two_set, orgs_file)
    assert set(out) == {"mock-strong", "mock-weak"}
    for parts in out.values():
        assert set(parts) == {"v48", "new", "both"}
        assert "org" in parts["both"] and "case" in parts["both"]
    pooled = rep.pooled_report(runs["v48"], runs["new"], round_two_set, orgs_file)
    assert pooled["pooled"]["models"]["mock-strong"]["cases"] == 6
