"""Scores stay visible while the failed semantic grading status stays truthful."""
from copy import deepcopy
import json
from xml.etree import ElementTree

import pytest

from src import release_page


def release():
    return json.loads((release_page.DOCS / 'release.json').read_text())


def test_release_matches_data():
    data = release()
    release_page.validate(data)
    assert (release_page.DOCS / 'index.html').read_text() == release_page.render(data)
    assert (release_page.DOCS / 'card.svg').read_text() == release_page.render_card(data)


@pytest.mark.parametrize('field,value', [
    ('ranking_eligible', True), ('score_status', 'validated'),
    ('new_subject_calls', 1), ('subject_prompts_changed', 1),
    ('accepted_grades_changed', 1), ('release_type', 'scored_release'),
    ('model_count', 999), ('retained_cases', 999), ('source_amendments', 999),
])
def test_false_release_is_blocked(field, value):
    data = deepcopy(release())
    data[field] = value
    with pytest.raises(ValueError):
        release_page.validate(data)


def test_altered_data_is_blocked():
    data = release()
    data['data_files']['candidate.json'] = '0' * 64
    with pytest.raises(ValueError, match='changed'):
        release_page.validate(data)


def test_scores_visible_without_certifying_honesty():
    page = release_page.render(release())
    assert 'Decision score = ½ Restraint + ½ Conviction' in page
    assert 'Honesty failed semantic validation' in page
    assert page.count('<tr data-model=') == 31
    assert 'id="previous-scores"' in page
    assert 'class="hero"' in page
    assert 'class="gcards"' in page
    assert 'class="matrix"' in page
    assert 'experimental' in page
    assert 'candidate.html' in page


def test_scores_in_readme_match_generated_data():
    scores = json.loads((release_page.DOCS / 'decision-scores.json').read_text())
    text = (release_page.ROOT / 'README.md').read_text()
    assert release_page.readme_table(scores) in text


def test_display_escapes_model_text():
    scores = json.loads((release_page.DOCS / 'decision-scores.json').read_text())
    scores['models'][0]['label'] = '<script>alert(1)</script>'
    page = release_page.render(release(), scores=scores)
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page


def test_charts_use_corrected_scores():
    scores = json.loads((release_page.DOCS / 'decision-scores.json').read_text())
    models = release_page.display_models(scores)
    current, previous = release_page.leaderboard.split_generations(models)
    ranked = release_page.leaderboard.rank_with_ties(current)
    assert len(ranked) == 17 and len(previous) == 14
    field = release_page._standalone(release_page._field(ranked))
    assert (release_page.DOCS / 'field.svg').read_text() == field
    pairs = release_page.leaderboard._generation_pairs(models, previous, release_page._comparisons(scores))
    chart = release_page._standalone(release_page._generation_svg(pairs))
    assert (release_page.DOCS / 'generations.svg').read_text() == chart
    assert release_page.readme_table(scores, previous) in (release_page.ROOT / 'README.md').read_text()
    assert len(pairs) == 14
    for pair in pairs:
        expected = pair['curr']['score']['value'] - pair['prev']['score']['value']
        assert pair['delta'] == pytest.approx(expected)


def test_generation_labels_have_room():
    scores = json.loads((release_page.DOCS / 'decision-scores.json').read_text())
    models = release_page.display_models(scores)
    _, previous = release_page.leaderboard.split_generations(models)
    pairs = release_page.leaderboard._generation_pairs(models, previous, release_page._comparisons(scores))
    chart = ElementTree.fromstring(release_page._generation_svg(pairs))
    for row in chart.findall('g'):
        label_end = float(row.find("text[@class='flabel']").get('x'))
        delta_start = float(row.find("text[@class='fnum']").get('x'))
        for selector in ('fsprev', 'fscurr'):
            score = row.find(f"text[@class='{selector}']")
            x = float(score.get('x'))
            width = len(score.text) * 8  # Conservative bound for 11.5px monospace.
            left = x - width if score.get('text-anchor') == 'end' else x
            right = left + width
            assert left >= label_end + 8
            assert right <= delta_start - 8


def test_generation_verdicts_require_corrected_test():
    scores = json.loads((release_page.DOCS / 'decision-scores.json').read_text())
    models = release_page.display_models(scores)
    _, previous = release_page.leaderboard.split_generations(models)
    pairs = release_page.leaderboard._generation_pairs(models, previous, release_page._comparisons(scores))
    chart = ElementTree.fromstring(release_page._generation_svg(pairs))
    for pair, row in zip(pairs, chart.findall('g'), strict=True):
        verdict = ''.join(row.find("text[@class='fverd']").itertext())
        if pair['decisive']:
            assert ('Measured gain' if pair['winner'] == 'curr' else 'Measured loss') in verdict
        else:
            assert 'No detected difference' in verdict
        assert 'slight' not in row.find('title').text


def test_historical_honesty_does_not_set_primary_score():
    scores = json.loads((release_page.DOCS / 'decision-scores.json').read_text())
    candidate = json.loads((release_page.DOCS / 'candidate.json').read_text())
    before = release_page.display_models(scores, candidate)
    for model in candidate['models']:
        model['scenarios']['candidate']['dimensions']['honesty']['value'] = 0
        model['scenarios']['candidate']['score']['value'] = 0
    after = release_page.display_models(scores, candidate)
    assert [m['score'] for m in before] == [m['score'] for m in after]
