"""Progress over time: one run's scores against each model's release date.

Scores compare only within one Ship Sense version (each version changes the
bank or the grader), so the chart reads ONE run: every model on it, previous
generations included, placed at the date its vendor released it. The
frontier is the best score among the plotted models released on or before
each date, so it shows the best model on this chart that existed then, not
the best model that existed then.

    score
      |              +---●  newest best       ● on the current board
      |        +--●--+   ○                    ○ left the board (history)
      |   ●----+  ○  ●                        - frontier: best to date
      +----------------------------------> release date

Data and geometry only: leaderboard.py draws the SVG and writes the copy.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class Metric(Enum):
    """The four panels, in display order."""
    SCORE = "score"
    RESTRAINT = "restraint"
    HONESTY = "honesty"
    CONVICTION = "conviction"


METRIC_TITLE = {Metric.SCORE: "Ship Sense Score", Metric.RESTRAINT: "Restraint",
                Metric.HONESTY: "Honesty", Metric.CONVICTION: "Conviction"}
# The headline is published 0-100 and the dimensions 0-1. The panels share one
# 0-1 scale (the headline is the mean of the three), so it is divided down.
SCORE_SCALE = 100.0


class Standing(Enum):
    """Where a plotted model sits on the run's board."""
    CURRENT = "current"    # on the current board
    PREVIOUS = "previous"  # superseded or replaced: history, drawn muted


class Omission(Enum):
    """Why a model is not on the chart; the page names each one."""
    RETIRED = "retired"          # its provider shut it down; it cannot be re-run
    NOT_RUN = "not_run"          # scored on an earlier version, not run on this one
    NO_DATE = "no_date"          # on this run, but no release date on record
    PROVISIONAL = "provisional"  # on this run, incomplete coverage: not ranked


@dataclass(frozen=True)
class Point:
    """One model on the chart. `values` maps each Metric to (value, lo, hi)
    on the shared 0-1 scale."""
    name: str
    label: str
    provider: str | None
    released: date
    standing: Standing
    values: dict

    def value(self, metric: Metric) -> float:
        return self.values[metric][0]


def _parse_date(text) -> date | None:
    """An ISO date, or None for a missing or malformed one."""
    if text is None:
        return None
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None


def _metric_values(row: dict) -> dict:
    """(value, lo, hi) per Metric from a ledger row, on the 0-1 scale."""
    out = {}
    for metric in Metric:
        scale = SCORE_SCALE if metric is Metric.SCORE else 1.0
        triple = row[metric.value]
        out[metric] = tuple(triple[k] / scale for k in ("value", "lo", "hi"))
    return out


def plot_points(models: list[dict], previous: set[str],
                dates: dict[str, str | None]) -> list[Point]:
    """Every ranked model of one run that has a release date, oldest first.

    `previous` names the models that left the board (superseded or replaced);
    they stay on the chart, muted, because they are the history. Baselines,
    provisional rows and undated models are left off (see omissions())."""
    points = []
    for m in models:
        if m.get("is_baseline") or not m.get("ranked_eligible", True):
            continue
        released = _parse_date(dates.get(m["name"]))
        if released is None:
            continue

        standing = Standing.PREVIOUS if m["name"] in previous else Standing.CURRENT
        points.append(Point(name=m["name"], label=m.get("label", m["name"]),
                            provider=m.get("provider"), released=released,
                            standing=standing, values=_metric_values(m)))
    return sorted(points, key=lambda p: (p.released, -p.value(Metric.SCORE)))


def frontier(points: list[Point], metric: Metric) -> list[Point]:
    """The points that set a new best among models released on or before
    their date, oldest first. Same-day releases make one step: only the
    day's best can set it, and a tie with the best so far is not a step."""
    steps, best = [], -math.inf
    for p in sorted(points, key=lambda p: (p.released, -p.value(metric))):
        if p.value(metric) <= best:
            continue
        best = p.value(metric)
        steps.append(p)
    return steps


def _version_key(version: str | None) -> tuple[int, ...]:
    """'v4.10' -> (4, 10), so versions sort numerically, newest last."""
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


def omissions(runs: list[dict], dates: dict[str, str | None],
              retired: dict[str, object]) -> dict[Omission, list[dict]]:
    """Every model the chart leaves off, by reason, so none goes missing
    silently. Each entry is {label, version} (for an earlier-version model,
    the last version that scored it), plus `retired` when the registry
    records the provider's shutdown date.

    `retired` maps a model name to its registry `api_retired` value."""
    latest = runs[-1]
    out: dict[Omission, list[dict]] = {reason: [] for reason in Omission}
    on_run = set()
    for m in latest["models"]:
        if m.get("is_baseline"):
            continue
        on_run.add(m["name"])
        entry = {"label": m.get("label", m["name"]), "version": latest.get("version")}
        if not m.get("ranked_eligible", True):
            out[Omission.PROVISIONAL].append(entry)
            continue
        if _parse_date(dates.get(m["name"])) is None:
            out[Omission.NO_DATE].append(entry)

    # An earlier version's model keeps the label and version it was last scored under.
    last_seen: dict[str, dict] = {}
    for run in runs[:-1]:
        for m in run["models"]:
            if m.get("is_baseline") or m["name"] in on_run:
                continue
            last_seen[m["name"]] = {"label": m.get("label", m["name"]),
                                    "version": run.get("version")}

    for name, entry in last_seen.items():
        if not retired.get(name):
            out[Omission.NOT_RUN].append(entry)
            continue
        shutdown = _parse_date(retired[name])
        out[Omission.RETIRED].append(
            dict(entry, retired=shutdown.isoformat() if shutdown else None))

    # Newest version first, alphabetical within one (both sorts are stable).
    for reason in (Omission.RETIRED, Omission.NOT_RUN):
        out[reason].sort(key=lambda e: e["label"])
        out[reason].sort(key=lambda e: _version_key(e["version"]), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Axes
# --------------------------------------------------------------------------- #
VALUE_SNAP = 0.05
_VALUE_STEPS = (0.05, 0.1, 0.2, 0.25)
MAX_VALUE_TICKS = 6
MAX_DATE_TICKS = 6
_MONTH_STEPS = (1, 3, 6, 12)  # months, quarters, halves, years
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_NAME = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")
# Room left of the oldest release and right of the run date, as a share of
# the span, so no mark sits on the plot edge.
_DATE_PAD_SHARE = 0.04
_DATE_PAD_MIN_DAYS = 10
# Guards float noise when snapping (0.85 / 0.05 is 16.999...).
_EPS = 1e-9


@dataclass(frozen=True)
class Frame:
    """The shared axes of all four panels. `through` is the run date (or the
    newest release, if later): the frontier runs to it."""
    start: date
    end: date
    through: date
    lo: float
    hi: float
    date_ticks: list
    value_ticks: list


def _snap_down(x: float) -> float:
    return round(math.floor(x / VALUE_SNAP + _EPS) * VALUE_SNAP, 4)


def _snap_up(x: float) -> float:
    return round(math.ceil(x / VALUE_SNAP - _EPS) * VALUE_SNAP, 4)


def value_ticks(lo: float, hi: float) -> list[float]:
    """Evenly stepped ticks inside [lo, hi], no more than MAX_VALUE_TICKS."""
    for step in _VALUE_STEPS:
        first = math.ceil(lo / step - _EPS)
        last = math.floor(hi / step + _EPS)
        if last - first + 1 <= MAX_VALUE_TICKS:
            return [round(i * step, 4) for i in range(first, last + 1)]
    return [lo, hi]


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def _months_from(start: date, end: date, step: int) -> list[date]:
    """First-of-month dates in [start, end] whose month index is a multiple
    of `step` (step 3 -> Jan/Apr/Jul/Oct, step 6 -> Jan/Jul)."""
    index = _month_index(start) + (0 if start.day == 1 else 1)
    index += (-index) % step
    out = []
    while True:
        when = date(index // 12, index % 12 + 1, 1)
        if when > end:
            return out
        out.append(when)
        index += step


def month_ticks(start: date, end: date,
                limit: int = MAX_DATE_TICKS) -> list[tuple[date, str]]:
    """(date, label) ticks: the finest month step that keeps the count at
    `limit` or fewer. The first tick and every January name the year;
    the rest name only the month ("Oct 2025", "Jan 2026", "Apr", ...)."""
    whens: list[date] = []
    for step in _MONTH_STEPS:
        whens = _months_from(start, end, step)
        if len(whens) <= limit:
            break
    out = []
    for i, when in enumerate(whens):
        label = _MONTH_ABBR[when.month - 1]
        if i == 0 or when.month == 1:
            label += f" {when.year}"
        out.append((when, label))
    return out


def chart_frame(points: list[Point], run_end: date,
                metrics: tuple[Metric, ...] = tuple(Metric)) -> Frame:
    """Shared axes: time from the oldest release to the run date, values on
    one snapped 0-1 range covering the intervals of `metrics` (every panel by
    default; the share card plots the score alone)."""
    first = points[0].released
    last = max(run_end, points[-1].released)
    span = max(1, (last - first).days)
    pad = timedelta(days=max(_DATE_PAD_MIN_DAYS, round(span * _DATE_PAD_SHARE)))
    start, end = first - pad, last + pad

    los = [p.values[m][1] for p in points for m in metrics]
    his = [p.values[m][2] for p in points for m in metrics]
    lo = max(0.0, _snap_down(min(los)))
    hi = min(1.0, _snap_up(max(his)))
    if hi - lo < VALUE_SNAP:
        # Every interval on one value: open one snap step around it.
        lo, hi = max(0.0, hi - VALUE_SNAP), max(hi, VALUE_SNAP)
    return Frame(start=start, end=end, through=last, lo=lo, hi=hi,
                 date_ticks=month_ticks(start, end),
                 value_ticks=value_ticks(lo, hi))


def dodge_offsets(points: list[Point]) -> dict[str, float]:
    """Horizontal slot per model, in dodge steps: same-day releases spread
    symmetrically (-1, 0, +1, best score leftmost) so their marks and
    whiskers stay apart; a model alone on its day sits at 0."""
    by_day: dict[date, list[Point]] = {}
    for p in points:
        by_day.setdefault(p.released, []).append(p)
    out = {}
    for group in by_day.values():
        middle = (len(group) - 1) / 2
        for i, p in enumerate(sorted(group, key=lambda p: -p.value(Metric.SCORE))):
            out[p.name] = i - middle
    return out


# --------------------------------------------------------------------------- #
# Direct labels
# --------------------------------------------------------------------------- #
class Anchor(Enum):
    """SVG text-anchor of a placed label."""
    START = "start"
    END = "end"


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: Box) -> bool:
        return (self.x0 < other.x1 and other.x0 < self.x1
                and self.y0 < other.y1 and other.y0 < self.y1)

    def inside(self, other: Box) -> bool:
        return (self.x0 >= other.x0 and self.x1 <= other.x1
                and self.y0 >= other.y0 and self.y1 <= other.y1)


@dataclass(frozen=True)
class Label:
    """A placed label. `leader` is a connector segment (x0, y0, x1, y1) from
    just above the point to the label, set only when the label had to stack
    away from its point."""
    key: str
    text: str
    x: float
    y: float  # baseline
    anchor: Anchor
    leader: tuple[float, float, float, float] | None = None


# A conservative average advance for a 600-weight sans glyph, in ems: an
# over-wide estimate only costs a label, an under-wide one costs a collision.
_CHAR_EM = 0.6
_ASCENT, _DESCENT = 0.8, 0.25
# A label beside its point sits this share of the size below the point's
# centre, so the text's middle lines up with the mark.
_LEVEL_DROP = 0.35
_STACK_LEADING = 2.0
# Candidate spots around a point, tried in order: (x side, y side, stack).
# Above-left comes first: a frontier point is a new best, so every earlier
# mark lies below it and the frontier line runs below-left of it. Stacked
# spots (stack > 0) rise one line at a time and draw a leader to the point.
_SPOTS = ((-1, -1, 0), (1, -1, 0), (-1, 0, 0), (1, 1, 0), (-1, 1, 0),
          (-1, -1, 1), (1, -1, 1), (-1, -1, 2), (1, -1, 2), (-1, -1, 3))


def text_box(x: float, y: float, text: str, size: float, anchor: Anchor) -> Box:
    """The estimated box of a one-line label at baseline (x, y)."""
    width = len(text) * size * _CHAR_EM
    x0 = x - width if anchor is Anchor.END else x
    return Box(x0, y - size * _ASCENT, x0 + width, y + size * _DESCENT)


def _spot(x: float, y: float, spot: tuple[int, int, int], size: float,
          gap: float) -> tuple[float, float, Anchor]:
    """Baseline position and anchor of one candidate spot around (x, y)."""
    side_x, side_y, stack = spot
    anchor = Anchor.END if side_x < 0 else Anchor.START
    lx = x + side_x * gap
    if side_y < 0:
        return lx, y - gap - stack * (size + _STACK_LEADING), anchor
    if side_y == 0:
        return lx, y + size * _LEVEL_DROP, anchor
    return lx, y + gap + size * _ASCENT, anchor


def _leader(x: float, y: float, lx: float, ly: float, size: float,
            gap: float) -> tuple[float, float, float, float]:
    """A connector from just above the point's mark to the label's near
    bottom corner."""
    side = -1 if lx < x else 1
    return (x + side, y - gap + 1, lx - side, ly + size * _DESCENT)


def _segment_box(segment: tuple[float, float, float, float]) -> Box:
    x0, y0, x1, y1 = segment
    return Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def place_labels(targets: list[tuple[str, float, float, str]], obstacles: list[Box],
                 bounds: Box, size: float, gap: float) -> list[Label]:
    """Greedy direct labels, in priority order. Each target (key, x, y, text)
    takes the first spot that stays inside `bounds` and clears every obstacle,
    every label already placed and every leader already drawn, so no label or
    leader ever crosses another. A target with no clear spot is left
    unlabelled: its tooltip and the table still name it."""
    placed: list[Label] = []
    taken: list[Box] = []
    for key, x, y, text in targets:
        for spot in _SPOTS:
            lx, ly, anchor = _spot(x, y, spot, size, gap)
            box = text_box(lx, ly, text, size, anchor)
            leader = _leader(x, y, lx, ly, size, gap) if spot[2] else None
            reserved = [box] + ([_segment_box(leader)] if leader else [])
            if not box.inside(bounds):
                continue
            if any(r.overlaps(b) for r in reserved for b in obstacles + taken):
                continue
            placed.append(Label(key, text, lx, ly, anchor, leader))
            taken += reserved
            break
    return placed


def label_frontier(targets: list[tuple[str, float, float, str]], obstacles: list[Box],
                   bounds: Box, size: float, gap: float) -> list[Label]:
    """Labels for a frontier, targets given oldest first. The placer is greedy,
    so the order decides who gets the near spots: oldest first lets a tight
    cluster stack up with leaders, newest first guarantees the current best a
    spot. Each order is tried and the best kept: the newest best labelled
    first of all, then the most labels; a tie keeps the earlier order."""
    if not targets:
        return []
    newest = targets[-1][0]
    orders = (targets, targets[::-1], targets[-1:] + targets[:-1])
    best: list[Label] = []
    best_rank = (-1, -1)
    for order in orders:
        placed = place_labels(order, obstacles, bounds, size, gap)
        rank = (int(any(lab.key == newest for lab in placed)), len(placed))
        if rank > best_rank:
            best, best_rank = placed, rank
    return best
