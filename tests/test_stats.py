"""Statistical layer: CIs, paired test, kappa, difficulty."""
import math

from src import stats


def _items(corrects, weight=1.0, item="i", dim="restraint"):
    return [{"item": item, "sub": f"s{i}", "dimension": dim,
             "correct": bool(c), "weight": weight} for i, c in enumerate(corrects)]


def test_weighted_mean_basic():
    assert stats.weighted_mean(_items([1, 1, 0, 0])) == 0.5


def test_weighted_mean_respects_weights():
    res = [{"item": "i", "sub": "a", "dimension": "d", "correct": True, "weight": 3.0},
           {"item": "i", "sub": "b", "dimension": "d", "correct": False, "weight": 1.0}]
    assert stats.weighted_mean(res) == 0.75


def test_bootstrap_ci_brackets_mean_and_is_seeded():
    # Distinct items so the clustered bootstrap has >1 resampling unit (spread).
    res = [{"item": f"i{i}", "sub": "s", "dimension": "restraint",
            "correct": i < 7, "weight": 1.0} for i in range(10)]
    m, lo, hi = stats.bootstrap_ci(res, n=2000, seed=1)
    assert math.isclose(m, 0.7)
    assert lo < m < hi  # genuine spread across item clusters
    # deterministic under fixed seed
    assert stats.bootstrap_ci(res, n=2000, seed=1) == (m, lo, hi)


def test_all_correct_ci_collapses_to_one():
    m, lo, hi = stats.bootstrap_ci(_items([1, 1, 1, 1]), n=1000, seed=0)
    assert m == lo == hi == 1.0


def test_paired_bootstrap_detects_clear_difference():
    a = _items([1, 1, 1, 1, 1, 1])
    b = _items([0, 0, 0, 0, 0, 0])
    res = stats.paired_bootstrap(a, b, n=2000, seed=0)
    assert res["n_pairs"] == 6
    assert math.isclose(res["diff"], 1.0)
    assert res["ci"][0] > 0  # significant


def test_paired_bootstrap_no_difference_crosses_zero():
    a = _items([1, 0, 1, 0, 1, 0])
    b = _items([1, 0, 1, 0, 1, 0])
    res = stats.paired_bootstrap(a, b, n=2000, seed=0)
    assert math.isclose(res["diff"], 0.0)
    assert res["ci"][0] <= 0 <= res["ci"][1]  # CI crosses 0 => no difference
    assert "p_value" not in res  # p-value deliberately dropped (CI is the rule)


def test_ship_sense_score_clusters_by_item():
    # Two items per dimension, fully correlated within item (one all-right, one
    # all-wrong). The resampling unit is the item, so the CI must be wide (n=2
    # clusters/dim), not collapsed as it would be resampling 8 sub-results.
    res = []
    for dim in ("restraint", "honesty", "conviction"):
        res += [{"item": f"{dim}_a", "sub": f"s{i}", "dimension": dim,
                 "correct": True, "weight": 1.0} for i in range(4)]
        res += [{"item": f"{dim}_b", "sub": f"s{i}", "dimension": dim,
                 "correct": False, "weight": 1.0} for i in range(4)]
    m, lo, hi = stats.ship_sense_score(res, n=4000, seed=0)
    assert math.isclose(m, 50.0)        # half right
    assert hi - lo > 15                 # clustered CI is honestly wide
    assert stats.ship_sense_score(res, n=4000, seed=0) == (m, lo, hi)  # seeded


def test_cohen_kappa_perfect_and_chance():
    assert math.isclose(stats.cohen_kappa(["a", "b", "a"], ["a", "b", "a"]), 1.0)
    # systematic disagreement on a 2-class balanced set => kappa < 0
    assert stats.cohen_kappa(["a", "b", "a", "b"], ["b", "a", "b", "a"]) < 0


def test_difficulty_flags_dead_items():
    per_model = {
        "m1": _items([1, 1, 0]),
        "m2": _items([1, 0, 0]),
    }
    rep = {d["sub"]: d for d in stats.difficulty_report(per_model)}
    assert rep["s0"]["dead"] is True    # both pass
    assert rep["s2"]["dead"] is True    # both fail
    assert rep["s1"]["dead"] is False   # splits
