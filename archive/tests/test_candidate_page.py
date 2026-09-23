"""Archived (Sept 2026): regression test for the frozen v3.1 validation-candidate
page. Extracted from tests/test_leaderboard.py when candidate_page.py moved to
archive/src/ — kept for the record, not collected by pytest (see archive/README.md).
"""
import json
from pathlib import Path

from archive.src import candidate_page

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_committed_candidate_page_matches_data():
    candidate = json.loads((DOCS / "history" / "v3.5" / "candidate.json").read_text())
    assert candidate["status"] == "candidate"
    assert all(not m["ranked_eligible"] for m in candidate["models"])
    assert (DOCS / "history" / "v3.5" / "candidate.html").read_text() == candidate_page.render(candidate)
    assert (DOCS / "history" / "v3.5" / "candidate-card.svg").read_text() == candidate_page.render_card()
