"""Progress over time: one run's scores against each model's release date.

Guards the frontier (best score to date), the one-run/one-version rule, the
note that names every model left off the chart and why, the geometry that
keeps labels apart, and the page, README and standalone-SVG surfaces.
"""
import re
from datetime import date
from html import escape

import pytest

from src import leaderboard as lb
from src import progression as pg


def _row(name, label, provider, released, score, dims=(0.80, 0.70, 0.90),
         eligible=True):
    """A ledger model row with real 0-1 dimension scores and a release date."""
    t = {"value": score, "lo": score - 3.0, "hi": score + 3.0}
    row = {"name": name, "label": label, "provider": provider, "released": released,
           "is_baseline": False, "ranked_eligible": eligible,
           "coverage_status": "ranked" if eligible else "provisional",
           "coverage_ratio": 1.0, "missing_dimensions": [], "score": t,
           "n_items": 50, "n_atomic": 700}
    for d, v in zip(lb.DIMENSIONS, dims):
        row[d] = {"value": v, "lo": v - 0.04, "hi": v + 0.04}
    return row


def _run(run_id, version, models):
    return {"run_id": run_id, "run_date": run_id[:10], "version": version,
            "version_note": "note",
            "bank": {"n_items": 50, "by_dimension": {"restraint": 18, "honesty": 18,
                                                     "conviction": 14},
                     "items_hash": "sha256:" + "ab" * 32, "includes_examples": False},
            "naive_floor": 37.0, "models": models}


def _points(rows, previous=()):
    dates = {r["name"]: r.get("released") for r in rows}
    return pg.plot_points(rows, set(previous), dates)


def _ledger():
    """Two versions: the latest run holds a current line, a superseded
    predecessor, and a model with no release date; the earlier run also scored
    a model the latest one never ran."""
    old = _run("2026-07-01", "v1.0", [
        _row("gone-1", "Gone Model", "google", "2025-11-01", 70.0)])
    latest = _run("2026-10-23", "v2.0", [
        _row("lab-a-2", "Lab A 2", "anthropic", "2026-09-01", 88.0, (0.95, 0.80, 0.90)),
        dict(_row("lab-a-1", "Lab A 1", "anthropic", "2025-12-01", 79.0,
                  (0.80, 0.82, 0.75)), superseded_by="lab-a-2"),
        _row("lab-b", "Lab B Pro", "openai", "2026-03-15", 83.0, (0.90, 0.66, 0.93)),
        _row("lab-c", "Lab C Max", "qwen", "2026-06-20", 81.0, (0.85, 0.70, 0.88)),
        _row("undated", "Undated Model", "xai", None, 80.0)])
    return {"schema_version": 3, "eval": "ship-sense", "runs": [old, latest]}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def test_frontier_steps_up_only_on_a_new_best():
    rows = [_row("a", "A", "openai", "2025-01-10", 70.0),
            _row("b", "B", "openai", "2025-03-01", 68.0),
            _row("c", "C", "google", "2025-03-01", 75.0),
            _row("d", "D", "google", "2025-06-01", 74.0),
            _row("e", "E", "xai", "2025-09-01", 80.0)]
    steps = pg.frontier(_points(rows), pg.Metric.SCORE)
    # Same-day releases make one step (the day's best); a later model that
    # does not beat the best so far never moves the line.
    assert [p.name for p in steps] == ["a", "c", "e"]


def test_frontier_is_computed_per_panel():
    rows = [_row("a", "A", "openai", "2025-01-10", 70.0, (0.90, 0.60, 0.80)),
            _row("b", "B", "google", "2025-05-01", 75.0, (0.85, 0.75, 0.70))]
    points = _points(rows)
    assert [p.name for p in pg.frontier(points, pg.Metric.RESTRAINT)] == ["a"]
    assert [p.name for p in pg.frontier(points, pg.Metric.HONESTY)] == ["a", "b"]


def test_points_share_one_scale_and_keep_previous_generations():
    rows = [_row("cur", "Current", "openai", "2026-01-01", 84.0),
            _row("old", "Old", "openai", "2025-06-01", 80.0),
            _row("prov", "Provisional", "google", "2026-02-01", 90.0, eligible=False),
            _row("nodate", "No Date", "google", None, 82.0),
            dict(_row("base", "Baseline", "mock", "2026-01-01", 40.0), is_baseline=True)]
    points = _points(rows, previous={"old"})
    # Oldest first; provisional, undated and baseline rows are not plotted.
    assert [p.name for p in points] == ["old", "cur"]
    assert points[0].standing is pg.Standing.PREVIOUS
    assert points[1].standing is pg.Standing.CURRENT
    # The headline is divided onto the dimensions' 0-1 scale.
    assert points[1].values[pg.Metric.SCORE] == pytest.approx((0.84, 0.81, 0.87))
    assert points[1].values[pg.Metric.RESTRAINT] == pytest.approx((0.80, 0.76, 0.84))


def test_omissions_name_every_model_left_off_and_why():
    ledger = _ledger()
    ledger["runs"][-1]["models"].append(
        _row("prov", "Partial Model", "google", "2026-08-01", 77.0, eligible=False))
    ledger["runs"][0]["models"].append(
        _row("shut", "Shut Model", "openai", "2025-08-01", 72.0))
    dates = {m["name"]: m.get("released")
             for run in ledger["runs"] for m in run["models"]}
    out = pg.omissions(ledger["runs"], dates, {"shut": "2026-09-30"})
    assert [e["label"] for e in out[pg.Omission.RETIRED]] == ["Shut Model"]
    assert out[pg.Omission.RETIRED][0]["retired"] == "2026-09-30"
    assert [e["label"] for e in out[pg.Omission.NOT_RUN]] == ["Gone Model"]
    assert out[pg.Omission.NOT_RUN][0]["version"] == "v1.0"
    assert [e["label"] for e in out[pg.Omission.NO_DATE]] == ["Undated Model"]
    assert [e["label"] for e in out[pg.Omission.PROVISIONAL]] == ["Partial Model"]


def test_omissions_skip_baselines_and_models_still_on_the_run():
    ledger = _ledger()
    ledger["runs"][0]["models"] += [
        dict(_row("base", "Naive baseline", "mock", None, 35.0), is_baseline=True),
        _row("lab-b", "Lab B Pro", "openai", "2026-03-15", 81.0)]
    dates = {m["name"]: m.get("released")
             for run in ledger["runs"] for m in run["models"]}
    out = pg.omissions(ledger["runs"], dates, {})
    assert [e["label"] for e in out[pg.Omission.NOT_RUN]] == ["Gone Model"]


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def test_month_ticks_stay_few_and_name_each_year():
    ticks = pg.month_ticks(date(2025, 9, 28), date(2026, 10, 30))
    assert [label for _, label in ticks] == ["Oct 2025", "Jan 2026", "Apr", "Jul", "Oct"]
    long = pg.month_ticks(date(2024, 1, 15), date(2026, 10, 30))
    assert 3 <= len(long) <= pg.MAX_DATE_TICKS
    for when, label in long:
        assert when.day == 1
        if when.month == 1:
            assert label.endswith(str(when.year))


def test_value_domain_covers_every_panel_on_snapped_ticks():
    rows = [_row("a", "A", "openai", "2025-01-10", 70.0, (0.62, 0.81, 0.97)),
            _row("b", "B", "google", "2025-05-01", 88.0, (0.90, 0.55, 0.80))]
    frame = pg.chart_frame(_points(rows), date(2025, 10, 1))
    assert frame.lo == pytest.approx(0.50)   # Honesty 0.55 - 0.04 = 0.51
    assert frame.hi == pytest.approx(1.00)   # Conviction 0.97 + 0.04, capped
    assert frame.value_ticks[0] >= frame.lo and frame.value_ticks[-1] <= frame.hi
    assert len(frame.value_ticks) <= pg.MAX_VALUE_TICKS
    # The time axis runs to the run date, past the last release.
    assert frame.start < date(2025, 1, 10) and frame.end >= date(2025, 10, 1)


def test_same_day_releases_are_dodged_apart():
    rows = [_row("a", "A", "openai", "2026-07-09", 85.0),
            _row("b", "B", "openai", "2026-07-09", 83.0),
            _row("c", "C", "openai", "2026-07-09", 78.0),
            _row("d", "D", "google", "2026-08-01", 80.0)]
    offsets = pg.dodge_offsets(_points(rows))
    assert sorted(offsets[n] for n in "abc") == [-1.0, 0.0, 1.0]
    assert offsets["d"] == 0.0


def test_labels_never_overlap_each_other_or_leave_the_bounds():
    bounds = pg.Box(0, 0, 300, 200)
    # Five targets crowded into one corner: whatever is placed must be clear.
    targets = [(f"k{i}", 150 + 6 * i, 100 - 2 * i, f"Model number {i}") for i in range(5)]
    placed = pg.place_labels(targets, [], bounds, size=12, gap=7)
    assert placed and placed[0].key == "k0"          # top priority always lands
    boxes = [pg.text_box(lab.x, lab.y, lab.text, 12, lab.anchor) for lab in placed]
    for i, a in enumerate(boxes):
        assert a.inside(bounds)
        for b in boxes[i + 1:]:
            assert not a.overlaps(b)


def test_labels_avoid_obstacles_and_drop_when_no_spot_is_clear():
    bounds = pg.Box(0, 0, 300, 200)
    wall = [pg.Box(0, 0, 300, 200)]                  # every spot is blocked
    assert pg.place_labels([("k", 150, 100, "Blocked")], wall, bounds,
                           size=12, gap=7) == []
    # A mark up-left of the point pushes the label to another side.
    mark = [pg.Box(60, 80, 140, 98)]
    placed = pg.place_labels([("k", 150, 100, "Model")], mark, bounds, size=12, gap=7)
    assert placed and placed[0].anchor is pg.Anchor.START


def test_a_label_pushed_off_its_point_gets_a_leader():
    bounds = pg.Box(0, 0, 300, 200)
    # Everything beside and below the point (150, 100) is taken except a thin
    # strip straight above it: the label stacks up one line and connects back.
    walls = [pg.Box(0, 83, 142, 200), pg.Box(152, 83, 300, 200)]
    placed = pg.place_labels([("k", 150, 100, "Model")], walls, bounds, size=12, gap=7)
    assert len(placed) == 1 and placed[0].leader is not None
    x0, y0, x1, y1 = placed[0].leader
    assert y1 < y0 < 100                              # rises from above the mark
    box = pg.text_box(placed[0].x, placed[0].y, "Model", 12, placed[0].anchor)
    assert box.y1 == pytest.approx(y1)                # and ends at the label


def test_frontier_labels_keep_the_current_best():
    # Room for exactly one label: greedy oldest-first would spend it on the
    # older point; the frontier labeller keeps the newest best instead.
    bounds = pg.Box(60, 0, 100, 31)
    targets = [("old", 100, 30, "AAAA"), ("new", 104, 30, "BBBB")]
    assert [lab.key for lab in pg.place_labels(targets, [], bounds, 12, 7)] == ["old"]
    assert [lab.key for lab in pg.label_frontier(targets, [], bounds, 12, 7)] == ["new"]


# --------------------------------------------------------------------------- #
# Surfaces
# --------------------------------------------------------------------------- #
def test_hollow_mark_keeps_each_labs_shape():
    for provider in ("anthropic", "minimax", "mistral", "reflection"):
        solid = lb._svg_mark(provider, 10, 10, 4)
        hollow = lb._svg_mark(provider, 10, 10, 4, lb._MarkFill.HOLLOW)
        assert solid.split(" ")[0] == hollow.split(" ")[0]   # same element
        assert 'fill="var(--paper)"' in hollow
        assert f'stroke="{lb._provider_color(provider)}"' in hollow


def test_page_section_draws_four_panels_from_one_run():
    html = lb.render_html(_ledger())
    section = html.split('<section id="progress">')[1].split("</section>")[0]
    assert '<a href="#progress">' in html
    # The score panel full width (plus its phone drawing, hidden from assistive
    # tech so it is read once), then the three dimensions as small multiples.
    assert section.count('<svg class="field prog pwide"') == 1
    assert section.count('<svg class="field prog pnarrow"') == 1
    assert section.count('<svg class="field prog pdim"') == 3
    for title in ("Ship Sense Score", "Restraint", "Honesty", "Conviction"):
        assert f">{title}</text>" in section
    assert section.count('role="img"') == 5 and section.count("aria-label=") == 4
    assert section.count('aria-hidden="true">') >= 1
    # One run, one version, said in words.
    assert "v2.0" in section and "2026-10-23" in section
    assert "not with any other version" in section
    # Every plotted model is in the table twin; the previous generation is
    # drawn hollow and named as having left the board.
    for label in ("Lab A 2", "Lab A 1", "Lab B Pro", "Lab C Max"):
        assert f'<span class="label">{label}</span>' in section
    assert 'fill="var(--paper)" stroke="#c15f3c"' in section
    assert "left the board" in section
    # One run only: a model from the earlier version is never plotted, but
    # nothing is silently missing either: both omissions are named with why.
    panels = section.split('<div class="phero">')[1].split('<div class="legend pkey">')[0]
    assert "Gone Model" not in panels and "Undated Model" not in panels
    assert "Gone Model" in section and "Undated Model" in section
    assert "not run on v2.0" in section


def test_frontier_labels_and_steps_appear_on_the_page():
    section = lb.render_html(_ledger()).split('<section id="progress">')[1]
    assert section.count('class="pfront"') == 5
    # The newest best on the headline panel is labelled beside its point.
    assert 'class="plabel">Lab A 2</text>' in section


def test_standalone_svg_is_self_contained():
    svg = lb.render_progress_svg(_ledger())
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "var(--" not in svg and "<style>" in svg
    assert "<script" not in svg and 'href="http' not in svg
    for name in ("Anthropic", "OpenAI"):
        assert f">{name}</text>" in svg              # lab legend travels with it
    # Only record-setting labs wear a lab's ink, so only they are keyed; the
    # rest of the field is one quiet swatch.
    assert ">Qwen</text>" not in svg and ">other models</text>" in svg
    for label in ("Lab A 2", "Lab A 1", "Lab B Pro", "Lab C Max"):
        assert f"<title>{label} " in svg              # every point has a tooltip
    assert "Undated Model" not in svg


def test_readme_block_points_to_the_progress_chart():
    md = lb.render_markdown(_ledger())
    block = md.split("### Progress over time")[1].split("###")[0]
    assert "(docs/progression.svg)" in block
    assert "not with any other version" in block
    assert "Gone Model" in block
    # Placed after the generations view and before the score history.
    assert md.index("### Progress over time") < md.index("### Score history")


def test_write_pages_writes_and_clears_the_progress_svg(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "DOCS", tmp_path / "docs")
    monkeypatch.setattr(lb, "README", tmp_path / "README.md")
    lb.write_pages(_ledger())
    assert (tmp_path / "docs" / "progression.svg").exists()
    # A board with nothing to plot leaves no stale chart behind.
    empty = _ledger()
    for m in empty["runs"][-1]["models"]:
        m["released"] = None
    lb.write_pages(empty)
    assert not (tmp_path / "docs" / "progression.svg").exists()
    assert 'id="progress"' not in (tmp_path / "docs" / "index.html").read_text()


# --------------------------------------------------------------------------- #
# Page order and the share card
# --------------------------------------------------------------------------- #
def test_page_puts_the_board_first_and_progress_second():
    """After the hero: the leaderboard, then progress over time, then what we
    measure and the rest. The jump links follow the sections' order."""
    html = lb.render_html(_ledger())
    sections = re.findall(r'<section id="([\w-]+)"', html)
    assert sections[:3] == ["leaderboard", "progress", "measure"]
    assert html.index('<header class="hero">') < html.index('<section id="leaderboard"')
    nav = html.split('<nav class="jump"')[1].split("</nav>")[0]
    assert re.findall(r'<a href="#([\w-]+)">', nav) == sections


def test_progress_section_leads_with_a_takeaway_and_its_unit():
    section = lb.render_html(_ledger()).split('<section id="progress">')[1]
    section = section.split("</section>")[0]
    charts = section.index('<div class="phero">')
    assert 0 <= section.index(escape(lb.PROGRESS_TAKEAWAY)) < charts
    assert 0 <= section.index(escape(lb.PROGRESS_UNIT)) < charts


def test_page_title_is_one_named_constant():
    assert lb.PAGE_TITLE == "Ship Sense: which AI models know when to stop"
    html = lb.render_html(_ledger())
    assert f"<title>{escape(lb.PAGE_TITLE)}</title>" in html


def test_card_draws_the_best_score_by_release_date(tmp_path, monkeypatch):
    """The card's right half: best score to date by release date, read from
    the same points as the page chart; the first and newest best are labelled
    with their values, and the axis names its unit."""
    monkeypatch.setattr(lb, "ROOT", tmp_path)
    monkeypatch.setattr(lb, "DOCS", tmp_path / "docs")
    svg = lb.render_card_svg(_ledger())
    chart = svg.split('<g id="card-progress">')[1].split("</g><!-- card-progress -->")[0]
    assert 'id="card-frontier"' in chart
    assert escape(lb.CARD_CHART_TITLE) in chart and escape(lb.CARD_CHART_UNIT) in chart
    # Frontier: Lab A 1 (79.0) -> Lab B Pro (83.0) -> Lab A 2 (88.0).
    assert ">79.0<" in chart and ">88.0<" in chart
    # Compact: marks and values only, no model names in the chart.
    for label in ("Lab A 2", "Lab A 1", "Lab B Pro", "Lab C Max"):
        assert label not in chart
    assert "var(--" not in svg                     # literal colors: it is a standalone file


def test_card_leaves_a_clean_slot_without_release_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "ROOT", tmp_path)
    monkeypatch.setattr(lb, "DOCS", tmp_path / "docs")
    ledger = _ledger()
    for m in ledger["runs"][-1]["models"]:
        m["released"] = None
    svg = lb.render_card_svg(ledger)
    assert 'id="card-progress"' not in svg and "card-frontier" not in svg
    assert escape(lb.CARD_TITLE) in svg            # the left half still draws


# --------------------------------------------------------------------------- #
# v4.6 design: emphasis, the record holder's band, the label rule
# --------------------------------------------------------------------------- #
def _panel(metric=pg.Metric.SCORE, detail=None):
    run = _ledger()["runs"][-1]
    _, previous = lb.split_generations(run["models"])
    points, _ = lb._progress_data(_ledger()["runs"], run["models"], previous)
    frame = pg.chart_frame(points, date(2026, 10, 23))
    slots = pg.dodge_offsets(points)
    detail = detail or lb._PanelDetail.FULL
    return lb._progress_panel(points, metric, frame, slots, lb._PROG_HERO, detail), points


def test_only_record_setters_wear_a_labs_ink():
    """12 lab hues on unlabelled marks are noise: a model that set a new best
    keeps its lab mark; every other model is a quiet dot."""
    svg, _ = _panel()
    groups = svg.split('<g class="prow">')[1:]
    lab_c = next(g for g in groups if g.startswith("<title>Lab C Max"))   # never a record
    lab_b = next(g for g in groups if g.startswith("<title>Lab B Pro"))   # a score record
    assert 'fill="var(--quiet)"' in lab_c and "#5910c6" not in lab_c
    assert lb._provider_color("openai") in lab_b


def test_record_holders_interval_is_drawn_as_a_band():
    svg, points = _panel()
    band = re.search(r'<path class="pband" d="M([^"]+)Z"/>', svg)
    assert band
    # Drawn under the best-to-date line, so the line reads over it.
    assert svg.index('class="pband"') < svg.index('class="pfront"')


def test_labels_name_steps_worth_naming_newest_first():
    """The newest best always comes first; a step smaller than half a score
    point is left unnamed; BRIEF panels name only the first and newest."""
    rows = [_row("a", "A", "openai", "2025-01-10", 70.0),
            _row("b", "B", "openai", "2025-03-01", 80.0),
            _row("c", "C", "google", "2025-06-01", 80.3),
            _row("d", "D", "xai", "2025-09-01", 81.0)]
    steps = pg.frontier(_points(rows), pg.Metric.SCORE)
    full = lb._label_order(steps, pg.Metric.SCORE, lb._PanelDetail.FULL)
    assert [p.name for p in full] == ["d", "a", "b"]          # c gained 0.3
    brief = lb._label_order(steps, pg.Metric.SCORE, lb._PanelDetail.BRIEF)
    assert [p.name for p in brief] == ["d", "a"]
