"""Fail-closed coverage check for a preview-track run, like src.complete for
the bank: every model x case x generation present, its trace complete and
matching the raw text, the case set unchanged since the run started, and the
saved scores equal to a fresh regrade. Parse failures and truncations are
counted. Messages carry counts, never case ids.

    python -m src.preview_complete --run-id R --track claimcheck --models A B ...
"""
from __future__ import annotations

import argparse
import json
import sys

from . import complete, preview_grade, preview_run
from .preview_run import GENERATIONS, MOCK, RunPaths, read_json
from .preview import Track

POLICY_FILE = "policy.json"
HEADROOM_KEYS = ("max_tokens", "peak_output_tokens", "peak_share_of_cap")


def truncation_waivers(paths: RunPaths) -> dict:
    """{model: {case: [generation, ...]}} from the track's policy.json: a
    readable cut-off waived under the output-cap rules counts as complete."""
    policy = read_json(paths.base / POLICY_FILE, {}) or {}
    return policy.get("truncation_waivers", {}) or {}


def _trace_ok(text, trace, provider: str, waived: bool) -> bool:
    if not isinstance(trace, dict) or trace.get("text") != text:
        return False
    if provider == MOCK or waived:
        return bool(text) and not trace.get("error")
    return complete.trace_complete(trace)


def _rows_key(rows: list[dict]) -> list[tuple]:
    return sorted((r["item"], r["sub"], r["generation"], round(float(r["score"]), 12))
                  for r in rows)


def model_report(paths: RunPaths, model: str, provider: str, cases, waivers: dict) -> dict:
    counts = {"expected": len(cases) * GENERATIONS, "missing": 0, "bad_trace": 0,
              "unparseable": 0, "truncated": 0, "waived": 0, "extra": 0}
    for case in cases:
        raws = read_json(paths.raw(model, case.id), [])
        traces = read_json(paths.trace(model, case.id), [])
        counts["extra"] += max(0, len(raws) - GENERATIONS)
        waived_gens = waivers.get(model, {}).get(case.id, [])
        for g in range(GENERATIONS):
            text = raws[g] if g < len(raws) else ""
            trace = traces[g] if g < len(traces) else None
            if not isinstance(text, str) or not text.strip():
                counts["missing"] += 1
                continue
            truncated = isinstance(trace, dict) and bool(trace.get("truncated"))
            counts["truncated"] += int(truncated)
            counts["waived"] += int(truncated and g in waived_gens)
            if not _trace_ok(text, trace, provider, g in waived_gens):
                counts["bad_trace"] += 1
            if preview_grade.grade_generation(case, text, g) is None:
                counts["unparseable"] += 1
    return counts


def errors_for(run_id: str, track: Track, model_names: list[str]) -> tuple[list[str], dict]:
    """(blocking errors, per-model counts)."""
    paths = RunPaths(run_id, track)
    try:
        cases = preview_run.run_cases(paths)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)], {}
    _, by_name = preview_run._registry()
    record = read_json(paths.run_file, {})
    errors, reports = [], {}
    waivers = truncation_waivers(paths)
    for model in model_names:
        if model not in record.get("models", {}):
            errors.append(f"{model}: not registered in this run")
            continue
        counts = model_report(paths, model, by_name[model]["provider"], cases, waivers)
        costs = preview_run.refresh_costs(paths, model)
        counts.update({key: costs[key] for key in HEADROOM_KEYS})
        reports[model] = counts
        if counts["missing"]:
            errors.append(f"{model}: {counts['missing']}/{counts['expected']} generations missing")
        if counts["bad_trace"]:
            errors.append(f"{model}: {counts['bad_trace']}/{counts['expected']} traces "
                          "incomplete, truncated without a waiver, or differ from raw")
        if counts["extra"]:
            errors.append(f"{model}: {counts['extra']} unexpected extra generations")

        saved = read_json(paths.scores(model), None)
        if saved is None:
            errors.append(f"{model}: score file missing; run the grade step")
            continue
        regraded = preview_run.grade_answers(paths, model, cases)["rows"]
        if _rows_key(saved["rows"]) != _rows_key(regraded):
            errors.append(f"{model}: saved scores do not match a fresh regrade")
    return errors, reports


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify a preview-track run is complete.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--track", required=True, choices=[t.value for t in Track])
    ap.add_argument("--models", nargs="+", required=True)
    args = ap.parse_args(argv)
    errors, reports = errors_for(args.run_id, Track(args.track), args.models)
    print(json.dumps(reports, indent=2))
    if errors:
        print("Run is incomplete:\n  - " + "\n  - ".join(errors), file=sys.stderr)
        return 1
    print(f"Complete: {len(reports)} models", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
