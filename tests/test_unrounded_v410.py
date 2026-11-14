"""v4.10 (SPEC section 5): scores are stored unrounded and rounded once when printed.
Earlier ledger runs keep their four-decimal values byte for byte. Synthetic runs only."""
import json

from src import leaderboard as lb
from src import loader, run, stats

LATER_RUN = "2099-12-31-pytest-unrounded"
# v4.9's leader: stored as round(96.55001, 4) = 96.55, whose nearest double is
# 96.5499..., so one-decimal printing gave 96.5. Unrounded it prints 96.6.
EXACT = 96.55001
PRINTED = "96.6"


def _mock_per_model():
    return run.run(["mock-strong", "mock-weak", "mock-naive"],
                   run_id="pytest-unrounded", only_examples=True)


def _snapshot():
    snap = lb.build_snapshot("pytest-unrounded", _mock_per_model(), loader.model_meta())
    snap["run_id"], snap["run_date"] = LATER_RUN, LATER_RUN[:10]
    return snap


def test_a_new_snapshot_stores_scores_unrounded():
    per = _mock_per_model()
    snap = lb.build_snapshot("pytest-unrounded", per, loader.model_meta())
    row = next(m for m in snap["models"] if m["name"] == "mock-weak")
    score, lo, hi = stats.ship_sense_score(loader.filter_results(per["mock-weak"]))
    assert row["score"] == {"value": float(score), "lo": float(lo), "hi": float(hi)}
    assert json.loads(json.dumps(snap)) == snap


def test_rewriting_the_ledger_keeps_every_earlier_run_byte_identical():
    committed = lb.LEDGER.read_text()
    ledger = json.loads(committed)
    assert json.dumps(ledger, indent=2) + "\n" == committed
    before = [json.dumps(r, indent=2) for r in ledger["runs"]]

    lb.append_snapshot(ledger, _snapshot())
    after = json.dumps(ledger, indent=2) + "\n"

    assert ledger["runs"][-1]["run_id"] == LATER_RUN
    assert [json.dumps(r, indent=2) for r in ledger["runs"][:-1]] == before
    # The earlier runs' text is unchanged in place, not merely equal as values.
    head = committed[:committed.rindex("\n  ]\n}")]
    assert after.startswith(head)


def _ledger_with_leader_at(value: float) -> dict:
    ledger = lb.load_ledger("/tmp/does-not-exist-ship-sense.json")
    snap = _snapshot()
    leader = max((m for m in snap["models"] if not m["is_baseline"]),
                 key=lambda m: m["score"]["value"])
    leader["score"] = {"value": value, "lo": value - 1.0, "hi": min(value + 1.0, 100.0)}
    lb.append_snapshot(ledger, snap)
    return ledger


def test_an_unrounded_score_prints_once_rounded_on_row_card_and_readme(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "ROOT", tmp_path)
    monkeypatch.setattr(lb, "DOCS", tmp_path / "docs")
    assert f"{round(EXACT, 4):.1f}" != PRINTED     # the double rounding v4.10 removes
    ledger = _ledger_with_leader_at(EXACT)
    assert f">{PRINTED}<" in lb.render_html(ledger)
    assert PRINTED in lb.render_card_svg(ledger)
    assert f"**{PRINTED}**" in lb.render_markdown(ledger)
