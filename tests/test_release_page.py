"""Evidence publication must preserve the failed ranking gate and frozen scores."""
from copy import deepcopy
import json

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


def test_release_has_no_winner():
    page = release_page.render(release())
    assert 'No validated model ranking' in page
    assert 'Both model-grading screens failed' in page
    assert 'candidate.html' in page
    assert 'No model has answered' in page or 'no model has answered' in page
