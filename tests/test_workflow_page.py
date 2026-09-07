"""Anonymous count fixtures only; no task text or provider calls."""
from copy import deepcopy
import hashlib
import json
from xml.etree import ElementTree

import pytest

from src import decision_scores, workflow_page, workflow_score


@pytest.fixture
def pack(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_scores, "BOOTSTRAPS", 100)
    families = [{"id": f"workflow-{i + 1:02}", "fields": 2,
                 "honesty_fields": 1, "honesty_indices": [0]} for i in range(5)]
    models = []
    for i, provider in enumerate(("openai", "anthropic", "google")):
        models.append({"name": f"model-{i}", "provider": provider, "model_id": f"snapshot-{i}",
                       "expected_responses": 20, "eligible": True,
                       "coverage": {"completed": 20, "missing": 0, "truncated": 0, "provider_error": 0},
                       "format_valid_responses": 20 - i,
                       "families": [{"family": f["id"], "passes": [4 - 2 * i, 4],
                                     "honesty_passes": [4 - 2 * i], "honesty_pairs": [2 - i]} for f in families]})
    inputs = {"schema_version": 1, "metric_version": "workflow-v4-paired-v1", "generations": 2,
              "families": families, "models": models}
    old = {"schema_version": 1, "checks": [
        {"case": "case-r", "dimension": "restraint", "weight": 1},
        {"case": "case-c", "dimension": "conviction", "weight": 1}], "models": [
            {"name": m["name"], "provider": m["provider"], "label": f"Example model {i}",
             "collected_on": "2026-01-01", "is_baseline": False, "generations": 2,
             "passes": [2, 2]} for i, m in enumerate(models)]}
    data = workflow_score.combine(old, inputs)
    scores = workflow_score.calculate(inputs)
    values = {"decision-inputs.json": old, "workflow-inputs.json": inputs,
              "workflow-scores.json": scores, "v4-scores.json": data}
    for name, value in values.items():
        (tmp_path / name).write_text(json.dumps(value))
    release = {"version": "v4.0", "ranking_eligible": True,
               "expected_models": [m["name"] for m in models],
               "qualification": {"passed": True, "accepted": 60, "expected": 60,
                                 "providers": ["openai", "anthropic", "google"], "repetitions": 2},
               "data_files": {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in values},
               "scoring_code_sha256": {name: hashlib.sha256((workflow_page.ROOT / name).read_bytes()).hexdigest()
                                       for name in workflow_page.SCORING_FILES}}
    (tmp_path / "v4-release.json").write_text(json.dumps(release))
    return data, scores, inputs, tmp_path, release


def test_render_shows_each_dimension_and_denominator(pack):
    data, scores, inputs, _, _ = pack
    html = workflow_page.render(data, scores, inputs)
    assert html.count('data-model="') == 3
    assert "5 source families" in html
    assert "All scores are on a 0–100 scale" in html
    assert "(Restraint + Honesty + Conviction) / 3" in html
    assert "95% CI 100.0–100.0" in html
    assert "19/20" in html and "95.0%" in html
    assert 'href="decision.html"' in html
    assert "different metric" in html
    assert "3 paired comparisons" in html
    assert "independent holdout" in html


@pytest.mark.parametrize("change", ["missing", "wrong_h", "wrong_overall", "comparison", "nonfinite"])
def test_render_refuses_misleading_scores(pack, change):
    data, scores, inputs, _, _ = deepcopy(pack)
    if change == "missing":
        scores["models"].pop()
    elif change == "wrong_h":
        data["models"][0]["dimensions"]["honesty"]["value"] = 99
    elif change == "wrong_overall":
        data["models"][0]["score"]["value"] = 90
    elif change == "comparison":
        data["comparisons"].pop()
    else:
        data["models"][0]["score"]["lo"] = float("nan")
    with pytest.raises(ValueError):
        workflow_page.render(data, scores, inputs)


def test_chart_has_separate_score_column_and_accessible_labels(pack):
    data, scores, inputs, _, _ = pack
    data["models"][0]["label"] = '<script>alert("x")</script> ' + "Long model name " * 5
    html = workflow_page.render(data, scores, inputs)
    assert '<script>alert' not in html
    chart = ElementTree.fromstring('<svg' + html.split('<svg', 1)[1].split('</svg>', 1)[0] + '</svg>')
    assert chart.attrib["role"] == "img"
    assert chart.find("title") is not None and chart.find("desc") is not None
    rows = chart.findall("g[@class='wf-chart-row']")
    assert len(rows) == 3
    assert all(float(r.find("text[@class='wf-value']").get("x")) == 980 for r in rows)
    assert len(rows[0].findall("text[@class='wf-label']/tspan")) > 1


def test_public_pack_reproduces_before_rendering(pack):
    data, scores, inputs, folder, _ = pack
    assert workflow_page.load_pack(folder / "v4-release.json") == (data, scores, inputs)


@pytest.mark.parametrize("change", ["hash", "roster", "qualification", "provider", "repetitions", "code", "path", "recomputed"])
def test_publication_gate(pack, change):
    _, _, _, folder, release = pack
    if change == "hash":
        release["data_files"]["workflow-inputs.json"] = "0" * 64
    elif change == "roster":
        release["expected_models"].pop()
    elif change == "qualification":
        release["qualification"]["passed"] = False
    elif change == "provider":
        release["qualification"]["providers"][-1] = "openai"
    elif change == "repetitions":
        release["qualification"]["repetitions"] = 1
    elif change == "code":
        release["scoring_code_sha256"].pop(next(iter(release["scoring_code_sha256"])))
    elif change == "path":
        release["data_files"]["../outside.json"] = "0" * 64
    else:
        path = folder / "v4-scores.json"
        scores = json.loads(path.read_text())
        scores["models"][0]["score"]["lo"] = 98
        path.write_text(json.dumps(scores))
        release["data_files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (folder / "v4-release.json").write_text(json.dumps(release))
    with pytest.raises(ValueError):
        workflow_page.load_pack(folder / "v4-release.json")


def test_share_card_uses_new_scores_and_dynamic_roster(pack):
    data, _, _, _, _ = pack
    card = workflow_page.render_card(data)
    chart = ElementTree.fromstring(card)
    text = " ".join(chart.itertext())
    assert "Example model 0" in text and "100.0" in text
    assert "95% CI 100.0–100.0" in text and "3 models" in text
    assert "Honesty" in text and "v4.0" in text


def test_failed_cli_preserves_live_files(pack, monkeypatch):
    _, _, _, folder, release = pack
    release['qualification']['passed'] = False
    (folder / 'v4-release.json').write_text(json.dumps(release))
    for name in ('index.html', 'card.svg'):
        (folder / name).write_text('Existing published artifact')
    monkeypatch.setattr('sys.argv', ['workflow_page', '--release', str(folder / 'v4-release.json')])
    with pytest.raises(ValueError, match='qualification'):
        workflow_page.main()
    for name in ('index.html', 'card.svg'):
        assert (folder / name).read_text() == 'Existing published artifact'
