"""v4.10 reader (SPEC section 5): grade.parse_json takes the first complete JSON object in
a reply before any repair; only a reply with no complete object reaches the truncation
repair, which is unchanged. Synthetic replies only."""
import json

import pytest

from src import grade

FULL = {"limitations": ["the sample is one seller"], "conclusions": ["totals only"]}


def test_complete_object_then_stray_brace_keeps_every_field():
    # Before v4.10 the repair backtracked to the last comma and dropped `conclusions`.
    text = json.dumps(FULL) + "\n}"
    assert grade.parse_json(text) == FULL


def test_complete_object_then_trailing_prose_or_second_object_reads_the_first():
    assert grade.parse_json(json.dumps(FULL) + " That is my answer.") == FULL
    assert grade.parse_json(json.dumps(FULL) + '\n{"note": "x"}') == FULL


def test_prose_before_the_object_still_reads():
    text = "Here is the analysis:\n" + json.dumps(FULL) + "\nThanks."
    assert grade.parse_json(text) == FULL


@pytest.mark.parametrize("text,expected", [
    ('{"classifications": {"a": "SHIP", "b": "KI', {"classifications": {"a": "SHIP", "b": "KI"}}),
    ('{"recommendation": "DONT_SHIP", "reasons": "the test ran three da',
     {"recommendation": "DONT_SHIP", "reasons": "the test ran three da"}),
    ('{"limitations": ["a", "b"], "conclusions": [', {"limitations": ["a", "b"], "conclusions": []}),
])
def test_a_cut_off_reply_still_takes_the_repair_path(text, expected):
    assert grade.parse_json(text) == expected


@pytest.mark.parametrize("text", [
    json.dumps(FULL),
    "```json\n" + json.dumps(FULL) + "\n```",
    json.dumps(FULL, indent=2),
])
def test_valid_json_is_read_as_before(text):
    assert grade.parse_json(text) == FULL


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", None])
def test_nothing_readable_is_still_empty(text):
    assert grade.parse_json(text) == {}


def test_the_v4_9_reader_stays_callable_for_the_preview_tracks():
    # The preview rounds were registered with the old reader; it still drops the field.
    text = json.dumps(FULL) + "\n}"
    assert grade.parse_json(text, grade.Reader.V4_9) != FULL
    assert grade.parse_json(json.dumps(FULL), grade.Reader.V4_9) == FULL
