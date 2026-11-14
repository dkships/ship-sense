"""v4.10 scaffolding: the publish-data file follows the board version, the newest
hypotheses block carries the previous one unchanged, and the gate report reads the
null answer's panel credit. Synthetic data only."""
import json

import yaml

from src import headline_run, loader, pairwise, panel_batch, panel_grade, panel_validate


# --- headline_run: the publish-data file follows the board version ------------------------
def test_publish_file_is_named_for_the_version():
    assert headline_run.publish_file("v4.9") == loader.ROOT / "headline_v49.json"
    assert headline_run.publish_file("v4.10") == loader.ROOT / "headline_v410.json"


def test_board_version_is_the_latest_ledger_run(tmp_path):
    ledger = tmp_path / "leaderboard.json"
    ledger.write_text(json.dumps({"runs": [{"run_id": "a", "version": "v4.8"},
                                           {"run_id": "b", "version": "v4.9"}]}))
    assert headline_run.board_version(ledger) == "v4.9"


def test_publish_move_writes_the_named_version_file(tmp_path, monkeypatch):
    monkeypatch.setattr(headline_run.loader, "ROOT", tmp_path)
    monkeypatch.setattr(headline_run, "outputs_root", lambda: tmp_path / "outputs")
    lineup_run = tmp_path / "outputs" / "new"
    lineup_run.mkdir(parents=True)
    (lineup_run / "pairwise.json").write_text(json.dumps({"lineup": ["m1"]}))
    rows = {"m1": {"with_case": 90.0, "without_case": 91.0, "change": 1.0}}
    effect = tmp_path / "effect.json"
    effect.write_text(json.dumps({"runs": {"new": rows, "new-alias": rows}}))
    code = headline_run.main(["publish-move", "--move-effect", str(effect),
                              "--lineup-run", "new", "--version", "v4.10"])
    assert code == 0
    written = json.loads((tmp_path / "headline_v410.json").read_text())
    assert written["version"] == "v4.10" and "move_out" in written
    assert not (tmp_path / "headline_v49.json").exists()


# --- hypotheses.yaml: the newest block carries the previous one -------------------------------
def _blocks() -> list[str]:
    return list(yaml.safe_load(pairwise.HYPOTHESES.read_text())["versions"])


def test_newest_block_carries_the_previous_registered_pairs():
    *_, previous, newest = _blocks()
    assert newest == "v4.10"
    old, new = pairwise.load_families(previous), pairwise.load_families(newest)
    for field in ("successions", "claims", "exploratory", "exploratory_only"):
        assert new[field] == old[field], field
    assert new["registered"] > old["registered"]


def test_a_succession_outside_the_registered_list_stays_its_own_family():
    family = pairwise.load_families("v4.10")
    pair = ("gemini-3.8-flash", "gemini-4-hydrogen")
    assert {s["curr"] for s in family["successions"]}.isdisjoint(pair)
    scores = {pair[0]: (85.0, 0, 0), pair[1]: (90.0, 0, 0)}
    found, _ = pairwise.confirmatory_pairs(list(reversed(pair)), scores, family)
    hypothesis = found[frozenset(pair)]
    assert hypothesis["group"] == f"{pairwise.GROUP_ADDED}:{pair[0]}->{pair[1]}"


# --- panel_validate: the null answer's panel credit -------------------------------------------
def test_gates_report_reads_the_null_answer_from_the_entry_key():
    votes = {lab: False for lab in panel_grade.REVIEWER_LABS}
    key = panel_grade.answer_key(panel_batch.NULL_POLICY, "case", 0)
    other = panel_grade.answer_key("brief-echo", "case", 0)
    entries = {key: {"case": "case", "view_sha": "a", "landmines": ["l"],
                     "votes": {"l": dict(votes)}, "lab": None},
               other: {"case": "case", "view_sha": "b", "landmines": ["l"],
                       "votes": {"l": {lab: True for lab in votes}}, "lab": None}}
    report = panel_validate.gates_report(entries, [])
    assert report["null_answer_credit"] == 0.0
