"""Adversarial anonymous-count validation using synthetic families only."""
from copy import deepcopy

import pytest

from src import stats, workflow_score


@pytest.fixture
def counts():
    return {
        "schema_version": 1, "metric_version": "workflow-v4-paired-v1", "generations": 2,
        "families": [
            {"id": "workflow-01", "fields": 2, "honesty_fields": 1, "honesty_indices": [0]},
            {"id": "workflow-02", "fields": 3, "honesty_fields": 3, "honesty_indices": [0, 1, 2]},
        ],
        "models": [{
            "name": "synthetic-model", "provider": "synthetic", "model_id": "synthetic-id",
            "eligible": True, "expected_responses": 8, "format_valid_responses": 8,
            "coverage": {"completed": 8, "provider_error": 0, "truncated": 0, "missing": 0},
            "families": [
                {"family": "workflow-01", "passes": [4, 2], "honesty_passes": [4], "honesty_pairs": [2]},
                {"family": "workflow-02", "passes": [0, 2, 4], "honesty_passes": [0, 2, 4],
                 "honesty_pairs": [0, 0, 2]},
            ],
        }],
    }


def test_family_weight_is_equal(counts):
    rows = workflow_score.expand_counts(counts)["synthetic-model"]
    weights = {family: sum(r["weight"] for r in rows if r["item"] == family)
               for family in ("workflow-01", "workflow-02")}
    assert weights["workflow-01"] == weights["workflow-02"]
    assert stats.weighted_mean(rows) == pytest.approx(2 / 3)
    assert stats.weighted_mean(workflow_score.expand_counts(counts, "honesty_passes")["synthetic-model"]) == .75
    assert stats.weighted_mean(workflow_score.expand_counts(counts, "passes")["synthetic-model"]) == .625


def test_bootstrap_resamples_families(counts):
    score = workflow_score.calculate(counts)["models"][0]["honesty"]
    assert score == pytest.approx({"value": 200 / 3, "lo": 100 / 3, "hi": 100})


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 1.0), ("generations", True),
    ("generations", 2.0), ("metric_version", "unfrozen"),
])
def test_exact_version_types(counts, key, value):
    counts[key] = value
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("path,value", [
    (("fields",), True), (("fields",), 2.0), (("honesty_fields",), True),
    (("honesty_indices",), [True]), (("honesty_indices",), [0.0]),
    (("honesty_indices",), [2]), (("honesty_indices",), [0, 0]),
    (("id",), "workflow-private-source"),
])
def test_rejects_invalid_family_metadata(counts, path, value):
    counts["families"][0][path[0]] = value
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("location", ["root", "family", "model", "counts"])
def test_unknown_fields_fail_closed(counts, location):
    target = {"root": counts, "family": counts["families"][0], "model": counts["models"][0],
              "counts": counts["models"][0]["families"][0]}[location]
    target["private_answer"] = "Sensitive source text must not pass through anonymous exports."
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("sides,pairs", [(0, 1), (0, 2), (1, 1), (2, 2), (3, 0), (3, 2), (4, 0), (4, 1)])
def test_rejects_impossible_pair_totals(counts, sides, pairs):
    family = counts["models"][0]["families"][0]
    family["passes"][0] = sides
    family["honesty_passes"] = [sides]
    family["honesty_pairs"] = [pairs]
    with pytest.raises(ValueError, match="Impossible"):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("sides,pairs", [(0, 0), (1, 0), (2, 0), (2, 1), (3, 1), (4, 2)])
def test_accepts_all_feasible_pair_totals(counts, sides, pairs):
    family = counts["models"][0]["families"][0]
    family["passes"][0] = sides
    family["honesty_passes"] = [sides]
    family["honesty_pairs"] = [pairs]
    assert workflow_score.expand_counts(counts)


def test_honesty_subset_matches_workflow_counts(counts):
    counts["models"][0]["families"][0]["honesty_passes"] = [3]
    counts["models"][0]["families"][0]["honesty_pairs"] = [1]
    with pytest.raises(ValueError, match="workflow fields"):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("field,value", [("passes", True), ("passes", 4.0),
                                          ("honesty_passes", True), ("honesty_pairs", 2.0)])
def test_count_integers_are_strict(counts, field, value):
    counts["models"][0]["families"][0][field][0] = value
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("field,value", [("eligible", 1), ("expected_responses", 8.0),
                                          ("expected_responses", 4), ("format_valid_responses", True),
                                          ("format_valid_responses", 8.0), ("format_valid_responses", 9)])
def test_model_coverage_metadata(counts, field, value):
    counts["models"][0][field] = value
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


@pytest.mark.parametrize("coverage", [
    {"completed": 7, "provider_error": 0, "truncated": 1, "missing": 0},
    {"completed": 8.0, "provider_error": 0, "truncated": 0, "missing": 0},
    {"completed": 8, "provider_error": False, "truncated": 0, "missing": 0},
    {"completed": 8, "provider_error": 0, "truncated": 0, "missing": 1},
    {"completed": 8, "truncated": 0, "missing": 0},
])
def test_rejects_untrustworthy_completed_coverage(counts, coverage):
    counts["models"][0]["coverage"] = coverage
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


def test_ineligible_model_is_visible_but_unscored(counts):
    model = counts["models"][0]
    model.update(eligible=False, coverage={"completed": 0, "provider_error": 1, "truncated": 0, "missing": 7})
    del model["families"]
    del model["format_valid_responses"]
    assert workflow_score.expand_counts(counts) == {}
    counts["models"].append(deepcopy(model))
    with pytest.raises(ValueError, match="Duplicate"):
        workflow_score.expand_counts(counts)


def test_ineligible_model_cannot_carry_hidden_counts(counts):
    counts["models"][0]["eligible"] = False
    with pytest.raises(ValueError):
        workflow_score.expand_counts(counts)


def test_zero_honesty_family_keeps_workflow_weight(counts):
    counts["families"][0].update(honesty_fields=0, honesty_indices=[])
    counts["models"][0]["families"][0].update(honesty_passes=[], honesty_pairs=[])
    assert stats.weighted_mean(workflow_score.expand_counts(counts)["synthetic-model"]) == pytest.approx(1 / 3)
    assert stats.weighted_mean(workflow_score.expand_counts(counts, "passes")["synthetic-model"]) == .625


def test_no_honesty_families_cannot_produce_nan_score(counts):
    for family in counts["families"]:
        family.update(honesty_fields=0, honesty_indices=[])
    with pytest.raises(ValueError, match="No Honesty"):
        workflow_score.expand_counts(counts)


def test_unknown_metric_cannot_reuse_other_denominator(counts):
    with pytest.raises(ValueError, match="Unknown"):
        workflow_score.expand_counts(counts, "made_up_metric")
