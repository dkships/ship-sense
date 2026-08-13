"""Statistical layer: CIs, paired test, kappa, difficulty."""
import math

from src import stats


def _items(corrects, weight=1.0, item="i", dim="restraint"):
    return [{"item": item, "sub": f"s{i}", "dimension": dim,
             "correct": bool(c), "weight": weight} for i, c in enumerate(corrects)]


def _paired_items(corrects, dim="restraint"):
    return [{"item": f"i{i}", "sub": "s", "dimension": dim,
             "correct": bool(c), "weight": 1.0} for i, c in enumerate(corrects)]


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
    a = _paired_items([1, 1, 1, 1, 1, 1])
    b = _paired_items([0, 0, 0, 0, 0, 0])
    res = stats.paired_bootstrap(a, b, n=2000, seed=0)
    assert res["n_pairs"] == 6
    assert res["n_items"] == 6
    assert math.isclose(res["diff"], 1.0)
    assert res["ci"][0] > 0  # significant


def test_paired_bootstrap_no_difference_crosses_zero():
    a = _paired_items([1, 0, 1, 0, 1, 0])
    b = _paired_items([1, 0, 1, 0, 1, 0])
    res = stats.paired_bootstrap(a, b, n=2000, seed=0)
    assert math.isclose(res["diff"], 0.0)
    assert res["ci"][0] <= 0 <= res["ci"][1]  # CI crosses 0 => no difference
    assert "p_value" not in res  # p-value deliberately dropped (CI is the rule)


def test_paired_bootstrap_uses_all_generations():
    # Two graded generations per check (generations: 2 in the registry) — the
    # test must average each side's generations, not keep whichever row a dict
    # saw last. b's generations disagree (gen1 right, gen2 wrong) on every
    # check, so dropping b's first generation reads b as all-wrong (diff 0.5
    # instead of 0.0). This bug shipped in the first published matrix.
    a = _paired_items([1, 0] * 3) + _paired_items([1, 0] * 3)      # mean 0.5
    b = _paired_items([1] * 6) + _paired_items([0] * 6)            # mean 0.5
    res = stats.paired_bootstrap(a, b, n=500, seed=0)
    assert res["n_pairs"] == 6            # matched checks, not raw rows
    assert math.isclose(res["diff"], 0.0)


def test_paired_bootstrap_is_antisymmetric():
    # paired(a, b) must mirror paired(b, a) exactly: same magnitude, mirrored
    # CI. The dropped-generation bug broke this (each direction discarded a
    # different model's first generation).
    a = _paired_items([1, 1, 0, 1, 0, 1]) + _paired_items([1, 0, 0, 1, 1, 1])
    b = _paired_items([0, 1, 1, 1, 0, 0]) + _paired_items([1, 1, 0, 0, 0, 1])
    ab = stats.paired_bootstrap(a, b, n=2000, seed=0)
    ba = stats.paired_bootstrap(b, a, n=2000, seed=0)
    assert math.isclose(ab["diff"], -ba["diff"])
    assert math.isclose(ab["ci"][0], -ba["ci"][1])
    assert math.isclose(ab["ci"][1], -ba["ci"][0])


def test_paired_bootstrap_matches_the_equal_dimension_headline_estimand():
    # Restraint has many more checks, but each dimension is one-third of Ship
    # Sense. Pooling atomics would make A look worse (-0.6); equal dimensions
    # correctly produce (+1 - 1 + 1) / 3 = +1/3.
    a = (_items([1] * 8, item="r", dim="restraint")
         + _items([0], item="h", dim="honesty")
         + _items([1], item="c", dim="conviction"))
    b = (_items([0] * 8, item="r", dim="restraint")
         + _items([1], item="h", dim="honesty")
         + _items([0], item="c", dim="conviction"))
    res = stats.paired_bootstrap(a, b, n=500, seed=0)
    headline_gap = (stats.ship_sense_score(a, n=100)[0]
                    - stats.ship_sense_score(b, n=100)[0]) / 100
    assert math.isclose(res["diff"], 1 / 3)
    assert math.isclose(res["diff"], headline_gap)


def test_paired_signflip_is_seeded_and_detects_a_clear_difference():
    a = _paired_items([1] * 12)
    b = _paired_items([0] * 12)
    p = stats.paired_signflip_p(a, b, n=4000, seed=7)
    assert p < 0.01
    assert stats.paired_signflip_p(a, b, n=4000, seed=7) == p
    assert stats.paired_signflip_p(a, a, n=500, seed=0) == 1.0


def test_paired_bootstrap_single_shared_item_collapses_ci():
    a = _items([1, 1, 0, 0])
    b = _items([0, 0, 1, 1])
    res = stats.paired_bootstrap(a, b, n=2000, seed=0)
    assert res["n_items"] == 1
    assert res["ci"] == (res["diff"], res["diff"])


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


def _dim_model(means):
    # 5 subs per dimension; each `mean` must be a multiple of 0.2 to land exactly.
    res = []
    for dim, mean in means.items():
        k = round(mean * 5)
        res += [{"item": f"{dim}_x", "sub": f"s{i}", "dimension": dim,
                 "correct": i < k, "weight": 1.0} for i in range(5)]
    return res


def test_dimension_structure_exposes_effective_weighting():
    # Restraint and Conviction move together; Honesty moves opposite. Equal weight
    # then does not mean equal influence: the correlated pair drives the headline
    # and Honesty is pushed negative despite being one of three equal terms.
    per_model = {f"m{i}": _dim_model({"restraint": i / 5, "conviction": i / 5,
                                      "honesty": 1 - i / 5}) for i in range(6)}
    ds = stats.dimension_structure(per_model, min_models=5)
    assert ds["n_models"] == 6
    dims = ds["dims"]
    ri, hi, ci = dims.index("restraint"), dims.index("honesty"), dims.index("conviction")
    assert math.isclose(ds["corr"][ri][ci], 1.0, abs_tol=1e-9)    # move together
    assert math.isclose(ds["corr"][ri][hi], -1.0, abs_tol=1e-9)   # move opposite
    assert ds["influence"]["honesty"] < 0 < ds["influence"]["restraint"]
    assert ds["pc1_share"] > 0.99                                 # one latent factor


def test_dimension_structure_needs_enough_models_and_spread():
    few = {f"m{i}": _dim_model({"restraint": i / 5, "honesty": i / 5,
                                "conviction": i / 5}) for i in range(4)}
    assert stats.dimension_structure(few, min_models=5) == {}     # too few models
    flat = {f"m{i}": _dim_model({"restraint": 0.6, "honesty": 0.6,
                                 "conviction": 0.6}) for i in range(6)}
    assert stats.dimension_structure(flat, min_models=5) == {}    # no spread to correlate
