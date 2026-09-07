import math

from src import kappa, stats


def test_constant_labels_do_not_establish_kappa():
    assert math.isnan(stats.cohen_kappa([True, True], [True, True]))


def test_partial_review_cannot_hide_missing_checks():
    result = kappa.agreement({"case:a": "SHIP", "case:b": "KILL"}, {"case:a": "SHIP"})
    assert result["n_missing"] == 1
    assert result["coverage"] == 0.5
    assert result["kappa"] is None


def test_honesty_requires_explicit_review_decisions():
    doc = {"id": "case", "type": "honesty", "landmines": ["a"]}
    assert kappa._honesty_labels(doc) == {}
    doc["check_validity"] = {"landmine:a": False, "falsealarm:b": True}
    assert kappa._honesty_labels(doc) == {"case:landmine:a": False, "case:falsealarm:b": True}
