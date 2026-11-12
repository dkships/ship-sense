"""A provisional current row (answers missing after the last output-cap step).

METHODOLOGY: ranking needs every item, check and dimension; an incomplete run stays
visible as a provisional estimate. These tests hold every surface to that: the row is
shown with its plain reason and no rank, the ranked counts never include it, and the
paired-test denominator counts only the models in the tests.
"""
import json
from html import escape

import pytest

from src import leaderboard as lb
from tests.test_leaderboard import _fake_model, _tied_top_ledger, _visible

REASON = "2 of 190 answers never came back: the model used its whole 65,536-token output cap thinking."


def _with_provisional(ledger: dict, k: int = 1) -> dict:
    models = ledger["runs"][-1]["models"]
    for i in range(k):
        row = dict(_fake_model(f"prov{i}", f"Provisional {i}", "google", 89.5 - i),
                   ranked_eligible=False, coverage_status="provisional",
                   provisional_reason=REASON)
        models.insert(-1, row)
    return ledger


def test_hero_counts_ranked_models_and_names_the_provisional(tmp_path, monkeypatch):
    plain = lb.render_html(_tied_top_ledger(tmp_path / "a", monkeypatch, n_models=6, n_top=1))
    assert "scores 6 current AI models, and" in plain
    assert "provisional)" not in plain
    one = lb.render_html(_with_provisional(_tied_top_ledger(tmp_path / "b", monkeypatch, 6, 1)))
    assert "scores 7 current AI models (6 ranked, 1 provisional), and" in one
    two = lb.render_html(_with_provisional(_tied_top_ledger(tmp_path / "c", monkeypatch, 6, 1), k=2))
    assert "scores 8 current AI models (6 ranked, 2 provisional), and" in two


def test_card_and_headline_count_ranked_models_only(tmp_path, monkeypatch):
    ledger = _with_provisional(_tied_top_ledger(tmp_path, monkeypatch, n_models=6, n_top=1))
    svg = _visible(lb.render_card_svg(ledger))
    assert "of 6 ranked current models" in svg and "of 7" not in svg
    assert "Provisional 0" not in svg


def test_link_preview_counts_the_provisional_row_apart(tmp_path, monkeypatch):
    ledger = _with_provisional(_tied_top_ledger(tmp_path, monkeypatch, n_models=6, n_top=1))
    run = ledger["runs"][-1]
    earlier = dict(_fake_model("old", "Old Model", "openai", 70.0), superseded_by="m0")
    run["models"].insert(-1, earlier)
    _, _, _, _, _, _, ranked = lb._board_context(ledger["runs"])
    text = lb._share_description(run, ranked)
    assert "8 models: 7 current (6 ranked, 1 provisional) and 1 earlier releases" in text
    plain = _tied_top_ledger(tmp_path / "plain", monkeypatch, n_models=6, n_top=1)
    _, _, _, _, _, _, ranked = lb._board_context(plain["runs"])
    assert "provisional" not in lb._share_description(plain["runs"][-1], ranked)


def test_row_reads_as_unranked_with_its_reason(tmp_path, monkeypatch):
    ledger = _with_provisional(_tied_top_ledger(tmp_path, monkeypatch, n_models=6, n_top=1))
    html = lb.render_html(ledger)
    row = html.split("Provisional&nbsp;0")[1].split("</tr>")[0]
    assert f"Provisional, not ranked. {escape(REASON)}" in row
    assert '<td class="rrange">&mdash;</td>' in row
    assert "<b>Provisional</b> rows (Provisional 0) are shown but not ranked" in html
    md = lb.render_markdown(ledger)
    line = next(l for l in md.splitlines() if "Provisional 0" in l)
    assert line.startswith("| prov. |")
    assert f"_Provisional, not ranked. {REASON}_" in line


def test_separation_denominator_counts_models_in_the_paired_tests(tmp_path, monkeypatch):
    ledger = _with_provisional(_tied_top_ledger(tmp_path, monkeypatch, n_models=6, n_top=6))
    md = lb.render_markdown(ledger)
    assert "separations of 5," in md                 # 6 models in the tests, not 7


def test_declared_provisional_never_unranks_or_ranks_a_row():
    snapshot = {"models": [dict(_fake_model("a", "A", "openai", 90.0)),
                           dict(_fake_model("b", "B", "google", 89.0), ranked_eligible=False)]}
    lb.mark_provisional(snapshot, {"b": {"reason": REASON}})
    assert snapshot["models"][1]["provisional_reason"] == REASON
    with pytest.raises(ValueError, match="full coverage"):
        lb.mark_provisional(snapshot, {"a": {"reason": REASON}})


def test_declared_provisional_reads_the_run_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "ROOT", tmp_path)
    folder = tmp_path / "outputs" / "r1"
    folder.mkdir(parents=True)
    assert lb.declared_provisional("r1") == {}
    (folder / "policy.json").write_text(json.dumps(
        {"truncation_waivers": {}, "provisional": {"b": {"reason": REASON}}}))
    assert lb.declared_provisional("r1") == {"b": {"reason": REASON}}


def test_first_place_line_says_ranked_when_a_provisional_row_is_on_the_board(tmp_path, monkeypatch):
    plain = _tied_top_ledger(tmp_path / "a", monkeypatch, n_models=6, n_top=1)
    assert "ranks first of 6 current models" in lb.render_html(plain)
    assert "ranked current" not in _visible(lb.render_card_svg(plain))
    prov = _with_provisional(_tied_top_ledger(tmp_path / "b", monkeypatch, 6, 1))
    html = lb.render_html(prov)
    h1 = html.split("<h1>")[1].split("</h1>")[0]
    assert "ranks first of 6 ranked current models" in h1
    assert 'content="Model A ranks first of 6 ranked current models.' in html   # meta and og
    assert "ranks first of 6 ranked current models" in _visible(lb.render_card_svg(prov))
