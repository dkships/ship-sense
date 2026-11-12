"""Seeding a partly re-answered run, the fresh-only panel read and its merge, the figures on
the new cases, the exposed set, errors per 100 checks, the cap probe and the live case
filter (NEXT_VERSION section 12e-f). Synthetic banks and runs under tmp_path; no provider
call, no private file."""
import copy
import hashlib
import json
from pathlib import Path
import uuid

import pytest
import yaml

from src import (cap_probe, headline_add, headline_run as hr, leaderboard, loader, panel_batch,
                 panel_grade, run)

LABS = panel_grade.REVIEWER_LABS
ANSWER = json.dumps({"limitations": ["The window is too short to say."], "conclusions": ["Orders grew."]})
DESIGN = {"pool": "mid", "phase": "board", "prompt_sha": "p" * 64, "seed": 1, "max_tokens": 16384,
          "reviewers": {lab: {"name": lab, "id": lab} for lab in LABS}}
MODELS = ("gpt-5", "claude-sonnet-5")


def _example(kind: str) -> tuple[dict, dict]:
    case = yaml.safe_load((loader.ROOT / "cases" / f"example_{kind}.yaml").read_text())
    key = yaml.safe_load((loader.ROOT / "keys" / f"example_{kind}.yaml").read_text())
    return case, key


def _write_bank(root: Path, cases: dict[str, tuple[dict, dict]]) -> Path:
    for case_id, (case, key) in cases.items():
        folder = root / "cases" / case["type"]
        folder.mkdir(parents=True, exist_ok=True)
        (root / "keys").mkdir(parents=True, exist_ok=True)
        (folder / f"{case_id}.yaml").write_text(yaml.safe_dump({**case, "id": case_id}))
        (root / "keys" / f"{case_id}.yaml").write_text(yaml.safe_dump({**key, "id": case_id}))
    return root


def _bank_cases(fresh_brief: str | None = None) -> dict:
    hon, hon_key = _example("honesty")
    res, res_key = _example("restraint")
    out = {"hon_a": (hon, hon_key), "hon_b": (hon, hon_key), "res_a": (res, res_key)}
    if fresh_brief:
        out["hon_b"] = ({**hon, "brief": fresh_brief}, hon_key)
    return out


def _items(bank: Path) -> dict:
    with headline_add.bank(bank):
        return {i["id"]: i for i in loader.load_cases(case_scope=loader.CASE_SCOPE_OFFICIAL)}


def _entry(model, item, generation, raw, yes=0):
    view = panel_grade.answer_view(raw)
    job = panel_grade.make_job(panel_grade.answer_key(model, item["id"], generation), item, item,
                               panel_grade.model_labs()[model], view)
    votes = {lm: {lab: i < yes for i, lab in enumerate(LABS)} for lm in job["landmines"]}
    return job["key"], panel_grade.make_entry(job, votes)


@pytest.fixture
def world(tmp_path):
    """outputs/<source> answered on the old bank; the new bank changes hon_b's brief and adds hon_c."""
    outputs = tmp_path / "outputs"
    answered = _write_bank(tmp_path / "answered", _bank_cases())
    new_cases = _bank_cases(fresh_brief="A corrected brief.")
    hon, hon_key = _example("honesty")
    new_cases["hon_c"] = (hon, hon_key)
    bank = _write_bank(tmp_path / "bank", new_cases)

    source = outputs / "src-run"
    for sub in ("raw", "traces", "costs"):
        (source / sub).mkdir(parents=True)
    items = _items(answered)
    entries = {}
    for model in MODELS:
        for case_id, item in items.items():
            raws = [ANSWER, ANSWER] if item["type"] == "honesty" else [{"x": 1}, {"x": 2}]
            (source / "raw" / f"{model}__{case_id}.json").write_text(json.dumps(raws))
            (source / "traces" / f"{model}__{case_id}.json").write_text(json.dumps([{"t": 1}, {"t": 2}]))
            if item["type"] == "honesty":
                for g, raw in enumerate(raws):
                    key, entry = _entry(model, item, g, raw)
                    entries[key] = entry
        (source / "costs" / f"{model}.json").write_text(json.dumps({"requests": 6}))
    panel_grade.write_verdicts(source / "panel" / "mid-board", DESIGN, entries)
    (source / "policy.json").write_text(json.dumps({"truncation_waivers": {
        "gpt-5": {"res_a": {"generations": [1], "reason": "r"}, "hon_b": {"generations": [0], "reason": "r"}}}}))
    with headline_add.bank(answered):
        sig = leaderboard.definition_signature(list(items.values()), loader.CASE_SCOPE_OFFICIAL)
    (source / "bank.json").write_text(json.dumps({"scopes": {loader.CASE_SCOPE_OFFICIAL: sig}}))
    return {"outputs": outputs, "answered": answered, "bank": bank, "source": source}


def _seed(world, dry_run=False, fresh=("hon_b", "hon_c"), run_id="new-run"):
    return hr.seed(run_id, "src-run", world["answered"], list(fresh), world["bank"], dry_run, world["outputs"])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Seed ----------------------------------------------------------------------------------------
def test_dry_run_counts_and_writes_nothing(world):
    out = _seed(world, dry_run=True)
    assert out["refused"] == [] and out["carried_cases"] == 2 and out["fresh_cases"] == ["hon_b", "hon_c"]
    assert out["answer_files"] == 4 and out["verdicts"] == 4                 # 2 models x (hon_a, res_a); 2 x 2 gens
    assert out["per_model"]["gpt-5"] == {"answer_files": 2, "verdicts": 2, "to_answer": 2}
    assert not (world["outputs"] / "new-run").exists()


def test_seed_copies_byte_for_byte_and_leaves_fresh_cases_out(world):
    _seed(world)
    dest = world["outputs"] / "new-run"
    for sub in ("raw", "traces"):
        names = sorted(p.name for p in (dest / sub).iterdir())
        assert names == sorted(f"{m}__{c}.json" for m in MODELS for c in ("hon_a", "res_a"))
        assert all(_sha(dest / sub / n) == _sha(world["source"] / sub / n) for n in names)
    carried = panel_grade.load_verdicts(dest / "panel" / hr.CARRIED_DIR)
    source = panel_grade.load_verdicts(world["source"] / "panel" / "mid-board")
    assert {e["case"] for e in carried["entries"].values()} == {"hon_a"}
    assert all(carried["entries"][k] == source["entries"][k] for k in carried["entries"])
    policy = json.loads((dest / "policy.json").read_text())
    assert policy["truncation_waivers"] == {"gpt-5": {"res_a": {"generations": [1], "reason": "r"}}}
    assert (world["outputs"] / "new-run-alias" / "raw").resolve() == (dest / "raw").resolve()
    assert (world["outputs"] / "new-run-fresh-panel" / "raw").resolve() == (dest / "raw").resolve()
    assert sorted(p.name for p in (dest / hr.CARRIED_DIR / "costs").iterdir()) == ["claude-sonnet-5.json", "gpt-5.json"]
    with pytest.raises(hr.SeedRefused, match="exists"):
        _seed(world)


def test_a_changed_case_not_named_fresh_is_refused(world):
    out = _seed(world, fresh=("hon_c",))
    assert ("hon_b", "case text differs from what the saved answers saw") in out["refused"]
    assert not (world["outputs"] / "new-run").exists()


def test_a_changed_landmine_is_refused(world):
    key = world["bank"] / "keys" / "hon_a.yaml"
    doc = yaml.safe_load(key.read_text())
    doc["landmines"][0]["desc"] = "a rewritten description"
    key.write_text(yaml.safe_dump(doc))
    out = _seed(world)
    assert ("hon_a", "brief or landmine text differs from what the verdict read") in out["refused"]


def test_an_unchanged_case_named_fresh_is_refused(world):
    out = _seed(world, fresh=("hon_a", "hon_b", "hon_c"))
    assert ("hon_a", "named fresh but unchanged: nothing to answer again") in out["refused"]


def test_a_wrong_answered_bank_and_a_source_named_run_are_refused(world, tmp_path):
    other = _write_bank(tmp_path / "other", _bank_cases(fresh_brief="Not what was answered."))
    with pytest.raises(hr.SeedRefused, match="case_hash"):
        hr.seed("new-run", "src-run", other, ["hon_b", "hon_c"], world["bank"], True, world["outputs"])
    with pytest.raises(hr.SeedRefused, match="overlaps"):
        _seed(world, run_id="src-run-v2")


# --- Fresh read and merge --------------------------------------------------------------------------
def _answer_fresh(world, dest):
    items = _items(world["bank"])
    for model in MODELS:
        for case_id in ("hon_b", "hon_c"):
            (dest / "raw" / f"{model}__{case_id}.json").write_text(json.dumps([ANSWER, ANSWER]))
    return items


def test_the_panel_reads_only_the_fresh_cases(world, monkeypatch):
    _seed(world)
    dest = world["outputs"] / "new-run"
    items = _answer_fresh(world, dest)
    monkeypatch.setattr(panel_grade, "ROOT", world["outputs"].parent)
    hon = {i: it for i, it in items.items() if it["type"] == "honesty"}
    jobs = panel_batch.board_jobs("new-run-fresh-panel", hon, cases=["hon_b", "hon_c"])
    assert {j["case"] for j in jobs} == {"hon_b", "hon_c"} and len(jobs) == 8
    assert len(panel_batch.board_jobs("new-run-fresh-panel", hon)) == 12
    with pytest.raises(KeyError):
        panel_batch.board_jobs("new-run-fresh-panel", hon, cases=["res_a"])


def _freeze_fresh(world, items, design=DESIGN, skip=None):
    entries = {}
    for model in MODELS:
        for case_id in ("hon_b", "hon_c"):
            for g in range(2):
                key, entry = _entry(model, items[case_id], g, ANSWER, yes=2)
                if key != skip:
                    entries[key] = entry
    fresh_dir = world["outputs"] / "new-run-fresh-panel" / "panel" / "mid-board"
    panel_grade.write_verdicts(fresh_dir, {**design, "frozen_at": "now"}, entries)


def test_merge_writes_the_frozen_format_regrade_replays(world, monkeypatch):
    _seed(world)
    dest = world["outputs"] / "new-run"
    items = _answer_fresh(world, dest)
    with pytest.raises(hr.SeedRefused, match="not frozen"):
        hr.merge("new-run", world["bank"], world["outputs"])
    _freeze_fresh(world, items)
    path = hr.merge("new-run", world["bank"], world["outputs"])
    assert path == dest / "panel" / "mid-board" / panel_grade.VERDICTS_FILE
    merged = panel_grade.load_verdicts(path.parent)
    assert merged["format"] == panel_grade.FORMAT_VERSION and len(merged["design"]["sources"]) == 2
    assert {e["case"] for e in merged["entries"].values()} == {"hon_a", "hon_b", "hon_c"}

    grader = panel_grade.PanelGrader(path.parent)
    for raw_file in sorted((dest / "raw").glob("*.json")):
        model, case_id = raw_file.stem.split("__")
        if items[case_id]["type"] != "honesty":
            continue
        for g, raw in enumerate(json.loads(raw_file.read_text())):
            assert grader(model, items[case_id], g, raw)
    with pytest.raises(FileExistsError):
        hr.merge("new-run", world["bank"], world["outputs"])


def test_merge_refuses_a_missing_verdict_and_another_design(world):
    _seed(world)
    items = _answer_fresh(world, world["outputs"] / "new-run")
    _freeze_fresh(world, items, skip="gpt-5|hon_c|1")
    with pytest.raises(hr.SeedRefused, match="1 answer"):
        hr.merge("new-run", world["bank"], world["outputs"])


def test_merge_refuses_a_different_panel(world):
    _seed(world)
    items = _answer_fresh(world, world["outputs"] / "new-run")
    _freeze_fresh(world, items, design={**DESIGN, "prompt_sha": "q" * 64})
    with pytest.raises(hr.SeedRefused, match="different panel"):
        hr.merge("new-run", world["bank"], world["outputs"])


# --- Figures ----------------------------------------------------------------------------------------
def _rows(item, dim, credits):
    return [{"item": item, "dimension": dim, "sub": f"s{n}", "weight": 1.0, "correct": c}
            for n, c in enumerate(credits)]


def test_new_cases_score_alone_with_its_width_said_plainly():
    per_model = {}
    for n, model in enumerate(("a", "b", "c")):
        rows = []
        for k in range(8):
            rows += _rows(f"old_h{k}", "honesty", [1, 0.5]) + _rows(f"old_r{k}", "restraint", [1, 0])
            rows += _rows(f"old_c{k}", "conviction", [1])
        rows += _rows("new_h", "honesty", [n / 3, 1]) + _rows("new_c", "conviction", [1, 0])
        per_model[model] = rows
    out = hr.new_cases(per_model, ["new_h", "new_c"])
    assert out["dimensions"] == ["conviction", "honesty"] and set(out["models"]) == {"a", "b", "c"}
    row = out["models"]["a"]
    assert row["cases"] == 2 and row["checks"] == 4 and row["errors_per_100"] == 50.0
    note = hr.width_note(out)
    assert "2 cases" in note and "no Restraint case" in note and "mean of 2 dimensions" in note
    assert "| Model |" in hr.new_cases_md(out, {})


def test_errors_per_100_counts_missed_credit_on_every_row():
    rows = _rows("x", "honesty", [1, 0, 2 / 3]) + _rows("y", "restraint", [True, False])
    assert leaderboard.errors_per_100(rows) == round(100 * (0 + 1 + 1 / 3 + 0 + 1) / 5, 1)


def test_errors_show_on_the_page_and_in_the_readme_table():
    row = {"name": "m", "label": "M", "provider": "openai", "pos": 1, "ranked_eligible": True,
           "score": {"value": 90.0, "lo": 88.0, "hi": 92.0}, "errors_per_100": 7.25, "n_items": 3,
           **{d: {"value": 0.9} for d in leaderboard.DIMENSIONS}}
    assert "errors per 100 checks" in leaderboard._model_row(row)
    cols = leaderboard._board_md_columns([row], None)
    assert cols.errors and "Errors /100 checks" in leaderboard._board_md_head(cols)[0]
    md = leaderboard._board_md_row({**row, "rank_set": [1, 1], "p_first": 1.0}, "1", cols, None, {})
    assert "| 7.2 |" in md or "| 7.3 |" in md
    old = {k: v for k, v in row.items() if k != "errors_per_100"}
    assert "errors per 100" not in leaderboard._model_row(old)
    assert not leaderboard._board_md_columns([old], None).errors


def test_the_exposed_set_grades_saved_answers_by_the_deterministic_rules(tmp_path):
    hon, key = _example("honesty")
    _write_bank(tmp_path / "retired" / "v4.9", {"gone": (hon, key)})
    items = hr.retired_items([tmp_path / "retired" / "v4.9"])
    run_dir = tmp_path / "run"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "raw" / "m__gone.json").write_text(json.dumps([ANSWER]))
    rows = hr.exposed_rows(items, {"gone": run_dir})
    from src import grade
    assert rows == {"m": grade.grade_item(items["gone"], ANSWER)}


def test_exposed_cases_answered_by_different_runs_are_never_pooled(tmp_path):
    hon, key = _example("honesty")
    _write_bank(tmp_path / "retired", {"old_a": (hon, key), "old_b": (hon, key), "moved": (hon, key)})
    items = hr.retired_items([tmp_path / "retired"])
    outputs = tmp_path / "outputs"
    for run_id, cases, models in (("v45", ("old_a", "old_b"), ("m1",)), ("v48", ("moved",), ("m1", "m2"))):
        (outputs / run_id / "raw").mkdir(parents=True)
        for case_id in cases:
            for model in models:
                (outputs / run_id / "raw" / f"{model}__{case_id}.json").write_text(json.dumps([ANSWER]))
    groups = hr.exposed_groups(items, {"old_a": "v45", "old_b": "v45", "moved": "v48"}, outputs)
    assert [(g["run"], g["cases"], sorted(g["models"])) for g in groups] == [
        ("v45", 2, ["m1"]), ("v48", 1, ["m1", "m2"])]
    assert groups[0]["models"]["m1"]["cases"] == 2 and groups[1]["models"]["m1"]["cases"] == 1


# --- Cap probe ----------------------------------------------------------------------------------------
class _Status(Exception):
    def __init__(self, code):
        super().__init__(f"status {code}")
        self.status_code = code


def _provider_for(outcomes):
    class Fake:
        def __init__(self, cfg):
            self.cfg = cfg

        def chat_result(self, messages, json_mode, max_tokens):
            code = outcomes.get(self.cfg["name"])
            if code:
                raise _Status(code)
            return type("R", (), {"finish_reason": "stop", "usage": {"output_tokens": 2}, "cost_usd": 0.0})()
    return Fake


def test_raised_caps_compare_two_registries():
    base = {"defaults": {"max_tokens": 8192}, "models": [{"name": "a"}, {"name": "b", "max_tokens": 32768},
                                                          {"name": "c"}]}
    now = copy.deepcopy(base)
    now["models"][0]["max_tokens"] = 32768
    now["models"][1]["max_tokens"] = 65536
    assert cap_probe.raised(base, now) == [{"model": "a", "old": 8192, "new": 32768},
                                           {"model": "b", "old": 32768, "new": 65536}]


def test_probe_logs_and_a_rejected_value_goes_back_to_its_old_cap(tmp_path, monkeypatch):
    registry = {"models": [{"name": n, "provider": "openai", "id": n} for n in ("gpt-5", "gpt-5.5", "o3")]}
    monkeypatch.setattr(cap_probe, "current_registry", lambda: registry)
    rows = [{"model": "gpt-5", "old": 8192, "new": 32768}, {"model": "gpt-5.5", "old": 8192, "new": 32768},
            {"model": "o3", "old": 8192, "new": 32768}]
    record = cap_probe.probe(rows, tmp_path, _provider_for({"gpt-5.5": 400, "o3": 503}))
    assert [r["outcome"] for r in record["results"]] == ["accepted", "rejected", "error"]
    policy = json.loads((tmp_path / "policy.json").read_text())["cap_probe"]
    assert policy["rejected"] == ["gpt-5.5"] and policy["errors"] == ["o3"]

    models = tmp_path / "models.yaml"
    models.write_text("models:\n  - name: gpt-5.5\n    max_tokens: 32768   # raised\n  - name: o3\n")
    with pytest.raises(SystemExit, match="error"):
        cap_probe.revert(tmp_path, models)
    cap_probe.probe(rows[:2], tmp_path, _provider_for({"gpt-5.5": 422}))
    assert cap_probe.revert(tmp_path, models) == ["gpt-5.5"]
    text = models.read_text()
    assert "max_tokens: 8192" in text and "rejected by the vendor" in text
    assert yaml.safe_load(text)["models"][0]["max_tokens"] == 8192
    assert json.loads((tmp_path / "policy.json").read_text())["cap_probe"]["reverted"] == ["gpt-5.5"]


# --- Live case filter ------------------------------------------------------------------------------------
def test_live_runs_answer_only_the_named_cases():
    run_id = f"pytest-cases-{uuid.uuid4().hex[:8]}"
    run.run(["mock-strong"], run_id, only_examples=True, cases=["example_honesty"])
    out = loader.ROOT / "outputs" / run_id
    assert sorted(p.name for p in (out / "raw").iterdir()) == ["mock-strong__example_honesty.json"]
    bank = json.loads((out / "bank.json").read_text())["scopes"][loader.CASE_SCOPE_EXAMPLES]
    assert bank["n_items"] == len(loader.load_cases(only_examples=True))
    with pytest.raises(ValueError, match="not in scope"):
        run.run(["mock-strong"], run_id, only_examples=True, cases=["nope"])


# --- Gates with the bank and verdicts as arguments ----------------------------------------------------------
def test_gates_take_the_bank_and_both_verdict_sets():
    items = [i for i in loader.load_cases(case_scope=loader.CASE_SCOPE_EXAMPLES) if i["type"] == "honesty"]
    jobs = panel_batch.gate_jobs(items)
    frozen = {j["key"]: panel_grade.make_entry(j, {lm: {lab: False for lab in LABS} for lm in j["landmines"]})
              for j in jobs}
    out = headline_add.gates(items, frozen)
    assert out["pass"] and out["honesty_gate"]["formula"].startswith("0.12 x ")
    with pytest.raises(ValueError):
        headline_add.gates(items, frozen, {"k": {"case": "example_honesty"}}, set())


# --- Registered verdicts, old against new ---------------------------------------------------------------------
def _pw(comparisons, untested=()):
    return {"comparisons": [{"a": a, "b": b, "family": fam, "winner": w} for a, b, fam, w in comparisons],
            "untested_claims": list(untested)}


def test_verdict_changes_count_winners_and_report_untested_pairs():
    old = _pw([("x", "y", "confirmatory", "x"), ("p", "q", "confirmatory", None),
               ("m", "n", "confirmatory", "m"), ("e", "f", "exploratory", "e")])
    new = _pw([("y", "x", "confirmatory", "x"), ("p", "q", "confirmatory", "q"),
               ("e", "f", "exploratory", None)], untested=[{"a": "n", "b": "m"}])
    out = hr.verdict_changes(old, new)
    assert out["tested_in_both"] == 2
    assert out["changed"] == [{"pair": ["p", "q"], "old": None, "new": "q"}]
    assert out["untested_in_new"] == [["m", "n"]] and out["only_in_new"] == []


# --- The move's own effect -----------------------------------------------------------------------------------
def test_move_table_summarises_each_grader_and_finds_rank_swaps():
    def runs(values):
        return {m: {"with_case": w, "without_case": wo, "change": round(wo - w, 3)}
                for m, (w, wo) in values.items()}
    effect = {"runs": {"old-run": runs({"a": (95.0, 94.9), "b": (94.0, 94.95), "c": (90.0, 90.0),
                                        "old": (80.0, 80.5)}),
                       "old-run-alias": runs({"a": (93.0, 93.0), "b": (92.0, 91.9), "c": (91.0, 91.0),
                                              "old": (79.0, 78.0)})}}
    out = hr.move_table(effect, {"a": "Model A"}, ["a", "b", "c"])
    panel, alias = out["graders"]["panel"]["summary"], out["graders"]["alias"]["summary"]
    assert panel["largest"] == {"model": "b", "change": 0.95} and panel["smallest"]["model"] == "a"
    assert panel["mean_current"] == round((-0.1 + 0.95 + 0.0) / 3, 3) and panel["current_models"] == 3
    assert panel["point_rank_changes"] == [{"model": "a", "before": 1, "after": 2},
                                           {"model": "b", "before": 2, "after": 1}]
    assert panel["first"] == {"model": "a", "change": -0.1}
    assert alias["point_rank_changes"] == [] and alias["smallest"] == {"model": "old", "change": -1.0}
    assert out["graders"]["panel"]["models"]["a"]["label"] == "Model A"


def test_a_one_case_exposed_group_is_published_without_an_interval():
    out = {"groups": [{"run": "a", "cases": 2, "models": {"m": {"score": [80.0, 70.0, 90.0], "cases": 2}}},
                      {"run": "b", "cases": 1, "models": {"m": {"score": [100.0, 100.0, 100.0], "cases": 1},
                                                          "n": {"score": [40.0, 40.0, 40.0], "cases": 1}}}]}
    pub = hr.publish_exposed(out)
    assert pub["groups"][0]["per_model"]["m"]["ci95"] == [70.0, 90.0]
    second = pub["groups"][1]
    assert second["models"] == 2 and second["range"] == [40.0, 100.0] and second["median"] == 70.0
    assert all("ci95" not in v for v in second["per_model"].values()) and not pub["pooled"]
    assert "run" not in json.dumps(pub)


def _model_rows(credit_new, credit_old):
    rows = []
    for k in range(6):
        rows += _rows(f"old_h{k}", "honesty", [credit_old, 1]) + _rows(f"old_c{k}", "conviction", [1])
        rows += _rows(f"old_r{k}", "restraint", [1, 0])
    return rows + _rows("new_h", "honesty", [credit_new, 1]) + _rows("new_c", "conviction", [credit_new])


def test_publish_new_cases_keeps_every_model_and_names_each_population():
    per_model = {"cur": _model_rows(1, 1), "old": _model_rows(0, 1), "naive-floor": _model_rows(0, 0)}
    pub = hr.publish_new_cases(per_model, ["new_h", "new_c"], ["cur"])
    assert set(pub["models"]) == {"cur", "old"}
    assert pub["models"]["cur"]["current"] and not pub["models"]["old"]["current"]
    assert set(pub["widths"]) == {"1 current models", "all 2 models"}
    assert all(note.endswith(f"(over {name})") for name, note in pub["notes"].items())


def test_bank_change_parts_sum_to_the_total():
    old = _rows("keep", "honesty", [1, 0]) + _rows("moved", "honesty", [0]) + _rows("fixed", "honesty", [1])
    old += _rows("r", "restraint", [1]) + _rows("c", "conviction", [1])
    new = _rows("keep", "honesty", [1, 0]) + _rows("fixed", "honesty", [0]) + _rows("added", "honesty", [1, 1])
    new += _rows("r", "restraint", [1]) + _rows("c", "conviction", [1])
    parts = hr.bank_change_parts(old, new, "moved", "fixed")
    assert parts["move"] > 0 and parts["corrected_brief"] < 0 and parts["additions"] > 0
    assert parts["move"] + parts["corrected_brief"] + parts["additions"] == pytest.approx(parts["total"])


def test_lab_adjustment_recovers_a_planted_lab_effect():
    carried = {f"m{i}": 70.0 + i for i in range(20)}
    flag = {m: i % 4 == 0 for i, m in enumerate(carried)}
    diff = {m: 0.5 * carried[m] - 40 + (3.0 if flag[m] else 0.0) for m in carried}
    out = hr.lab_adjustment(diff, carried, flag, shuffles=200, seed=1)
    assert out["residual"] == pytest.approx(3.0) and out["slope"] == pytest.approx(0.5)
    assert out["flagged"] == 5 and 0 <= out["p_one_sided"] < 0.05


# --- Estimate and the spend stop ------------------------------------------------------------------------------
def test_the_answer_estimate_scales_each_models_saved_cost(world):
    out = hr.estimate_answers(world["source"], 2)
    assert out["unpriced"] == ["claude-sonnet-5", "gpt-5"]          # {"requests": 6} has no cost
    (world["source"] / "costs" / "gpt-5.json").write_text(json.dumps({"estimated_cost_usd": 0.9}))
    assert hr.estimate_answers(world["source"], 2)["per_model"]["gpt-5"] == 0.6   # 0.9 / 3 cases x 2


def test_spend_adds_saved_records_and_stops_at_sixty_dollars(world):
    _seed(world)
    outputs, dest = world["outputs"], world["outputs"] / "new-run"
    (dest / "costs").mkdir()
    (dest / "costs" / "gpt-5.json").write_text(json.dumps({"estimated_cost_usd": 20.0}))
    usage = {lab: {"cost_usd": 1.0} for lab in LABS}
    panel_grade.write_verdicts(outputs / "checks" / "panel" / "mid-known", DESIGN, {}, {"usage": usage})
    probe = outputs / f"new-run{hr.CAP_PROBE_SUFFIX}"
    probe.mkdir()
    (probe / "cap_probe.json").write_text(json.dumps({"results": [{"cost_usd": 0.5}]}))
    out = hr.spend("new-run", ["checks"], outputs)
    assert out == {"answers_usd": 20.0, "panel_usd": 3.0, "cap_probe_usd": 0.5, "total_usd": 23.5,
                   "stop_usd": 60.0, "under_stop": True}
    (dest / "costs" / "o3.json").write_text(json.dumps({"estimated_cost_usd": 36.5}))
    assert not hr.spend("new-run", ["checks"], outputs)["under_stop"]
