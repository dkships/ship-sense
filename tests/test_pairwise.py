"""Pairwise head-to-head: run merging, band selection, and the win rule."""
import pytest

from src import pairwise


def _model(corrects, dim="restraint"):
    # One atomic per item, so pairs match 1:1 and each item is its own cluster.
    return [{"item": f"i{i}", "sub": "s", "dimension": dim,
             "correct": bool(c), "weight": 1.0} for i, c in enumerate(corrects)]


def test_merge_drops_baselines_and_rejects_a_model_in_two_runs():
    run_a = {"a": _model([1]), "mock-naive": _model([0])}
    run_b = {"b": _model([1])}
    merged = pairwise.merge_per_model([run_a, run_b])
    assert sorted(merged) == ["a", "b"]          # naive floor is not a competitor
    with pytest.raises(ValueError, match="more than one run"):
        pairwise.merge_per_model([run_a, {"a": _model([0])}])


def test_band_is_the_models_whose_ci_overlaps_the_leaders():
    order = ["a", "b", "c"]
    scores = {"a": (90.0, 80.0, 95.0),   # leader, lo = 80
              "b": (85.0, 79.0, 92.0),   # hi 92 >= 80 -> in band
              "c": (60.0, 55.0, 70.0)}   # hi 70  < 80 -> out
    assert pairwise.band_of(order, scores) == ["a", "b"]
    assert pairwise.band_of([], {}) == []


def test_compare_covers_each_pair_once_and_names_the_winner():
    # Twenty items leave ten discordant clusters on each adjacent comparison,
    # enough for both to survive a three-test Holm family.
    per = {"a": _model([1] * 20),                        # beats both everywhere
           "b": _model([1] * 10 + [0] * 10),
           "c": _model([0] * 20)}
    recs = pairwise.compare(per, ["a", "b", "c"], n=2000, seed=0)
    assert len(recs) == 3                                # C(3,2), one direction only
    by = {(r["a"], r["b"]): r for r in recs}
    assert by[("a", "c")]["winner"] == "a"
    assert by[("a", "b")]["winner"] == "a"
    assert by[("b", "c")]["winner"] == "b"
    assert by[("a", "c")]["lo"] > 0                      # CI excludes zero


def test_compare_reports_no_difference_rather_than_a_coin_flip_winner():
    same = [1, 0, 1, 0, 1, 0]
    per = {"a": _model(same), "b": _model(same)}
    rec = pairwise.compare(per, ["a", "b"], n=500, seed=0)[0]
    assert rec["diff"] == 0.0
    assert rec["winner"] is None                         # CI straddles zero


def test_wins_counts_only_decisive_rows():
    recs = [{"a": "x", "b": "y", "winner": "x"},
            {"a": "x", "b": "z", "winner": None}]
    assert pairwise.wins(recs) == {"x": 1, "y": 0, "z": 0}


def test_holm_adjust_controls_the_requested_family():
    adjusted = pairwise.holm_adjust([0.01, 0.03, 0.04])
    assert adjusted == pytest.approx([0.03, 0.06, 0.06])
    assert pairwise.holm_adjust([]) == []


def test_render_explains_multiplicity_control_and_the_verdicts():
    scores = {"a": (90.0, 80.0, 95.0), "b": (85.0, 79.0, 92.0)}
    recs = [{"a": "a", "b": "b", "diff": 0.10, "lo": 0.02, "hi": 0.20,
             "n_items": 50, "p_value": 0.01, "p_adjusted": 0.01,
             "winner": "a"}]
    md = pairwise.render(scores, ["a", "b"], recs, ["2026-07-07"],
                         "official_real_only", band_only=True)
    assert "Holm-corrects" in md
    assert "Holm p" in md
    assert "**a** wins" in md
    assert "beats 1 of 1" in md
