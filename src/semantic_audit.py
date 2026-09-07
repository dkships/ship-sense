"""Adversarial diagnostics and score bounds for automated semantic grading."""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from . import semantic_batch as sb, semantic_grade as sg, stats

MIN_INVARIANCE = 0.98
MIN_SOURCE_COVERAGE = 0.90
MIN_REAL_RESOLUTION = 0.85
BOOTSTRAPS = 10_000
SEED = 310905


def source_quarantine(records, outputs, providers=sg.PROVIDERS):
    uncertain = set()
    for record in records:
        if record["category"] != "source":
            continue
        for criterion in record["payload"]["criteria"]:
            cid = criterion["id"]
            for provider in providers:
                checked = outputs[provider][record["id"]]
                row = next(r for r in checked["raw_decisions"] if r["id"] == cid)
                if row["key_supported"] != "yes" or checked["votes"][cid]["issues"]:
                    uncertain.add((record["case"], record["checks"][cid]))
    return uncertain


def _panel(record, cid, outputs, quarantine, providers=sg.PROVIDERS):
    if (record.get("case"), record.get("checks", {}).get(cid)) in quarantine:
        return "unresolved"
    return sg.panel_vote({p: outputs[p][record["id"]]["votes"][cid] for p in providers}, providers)


def screen_metrics(records, outputs):
    controls = [r for r in records if r["category"] == "control"]
    real = [r for r in records if r["category"] == "real"]
    variants = [r for r in records if r["category"] == "metamorphic"]
    quarantine = source_quarantine(records, outputs)
    source_count = sum(len(r["payload"]["criteria"]) for r in records if r["category"] == "source")
    provider_metrics = {}
    for provider in sg.PROVIDERS:
        expected, actual = {}, {}
        critical_false_passes = []
        families = defaultdict(lambda: ({}, {}))
        for record in controls:
            for cid, reference in record["expected"].items():
                key = record["id"] + "/" + cid
                verdict = outputs[provider][record["id"]]["votes"][cid]["verdict"]
                expected[key], actual[key] = reference, verdict
                families[record["family"]][0][key] = reference
                families[record["family"]][1][key] = verdict
                if reference == "fail" and verdict == "pass":
                    critical_false_passes.append(key)
        invariance = defaultdict(lambda: {"same": 0, "total": 0, "resolved_pairs": 0})
        for record in variants:
            for cid, vote in outputs[provider][record["id"]]["votes"].items():
                parent = outputs[provider][record["parent"]]["votes"][cid]["verdict"]
                metric = invariance[record["variant"]]
                metric["total"] += 1
                metric["same"] += vote["verdict"] == parent
                metric["resolved_pairs"] += "unresolved" not in (vote["verdict"], parent)
        for metric in invariance.values():
            metric["agreement"] = metric["same"] / metric["total"]
            metric["resolved_coverage"] = metric["resolved_pairs"] / metric["total"]
        metric = sg.control_metrics(expected, actual)
        metric.update(critical_false_passes=critical_false_passes,
            by_family={k: sg.control_metrics(*v) for k, v in families.items()},
            invariance=dict(invariance))
        metric["passed"] = (metric["passed"] and not critical_false_passes
            and all(v["accuracy"] >= 0.90 for v in metric["by_family"].values())
            and bool(invariance) and all(v["agreement"] >= MIN_INVARIANCE
                and v["resolved_coverage"] >= MIN_REAL_RESOLUTION for v in invariance.values()))
        provider_metrics[provider] = metric
    real_votes = [_panel(r, c["id"], outputs, quarantine) for r in real for c in r["payload"]["criteria"]]
    resolution = sum(v != "unresolved" for v in real_votes) / len(real_votes)
    source_coverage = 1 - len(quarantine) / source_count
    passed = (all(m["passed"] for m in provider_metrics.values())
              and resolution >= MIN_REAL_RESOLUTION and source_coverage >= MIN_SOURCE_COVERAGE)
    return {"passed": passed, "provider_metrics": provider_metrics,
        "real_consensus_resolution": resolution, "real_checks": len(real_votes),
        "source_supported_fraction": source_coverage, "source_checks": source_count,
        "source_quarantine": sorted(quarantine), "human_review_required": False,
        "ground_truth_accuracy_established": False,
        "scope": "Constructed controls and metamorphic stability; no independent real-answer gold labels.",
        "thresholds": {"control_accuracy": sg.MIN_CONTROL_ACCURACY,
            "control_failure_recall": sg.MIN_FAILURE_RECALL, "critical_false_passes": 0,
            "minimum_control_family_accuracy": 0.90, "invariance": MIN_INVARIANCE,
            "source_supported_fraction": MIN_SOURCE_COVERAGE, "real_resolution": MIN_REAL_RESOLUTION}}


def analyze_screen(pack):
    outputs = sb.ingest(pack, "screen")
    records = sb.read(pack / "screen/records.json")
    report = screen_metrics(records, outputs)
    report["result_sha256"] = {p: sb.sha(pack / "screen" / p / "results.jsonl") for p in sg.PROVIDERS}
    sb.write(pack / "screen/analysis.json", report)
    return report


def _interval(rows):
    point, lo, hi = stats.ship_sense_score(rows, n=BOOTSTRAPS, seed=SEED)
    return {"value": point, "lo": lo, "hi": hi}


def score_bounds(fixed, semantic):
    if any(r["verdict"] not in {v.value for v in sg.Verdict} for r in semantic):
        raise ValueError("Invalid semantic verdict")
    lower = fixed + [{**row, "correct": row["verdict"] == "pass"} for row in semantic]
    upper = fixed + [{**row, "correct": row["verdict"] != "fail"} for row in semantic]
    if {r["dimension"] for r in lower} != set(stats.DIMENSIONS):
        raise ValueError("Every score requires all dimensions")
    for row in lower + upper:
        if not math.isfinite(row["weight"]) or row["weight"] <= 0:
            raise ValueError("Invalid score weight")
    result = {"lower_assignment": _interval(lower), "upper_assignment": _interval(upper),
              "unresolved": sum(r["verdict"] == "unresolved" for r in semantic),
              "honesty_checks": len(semantic), "dimensions": {}}
    for dim in stats.DIMENSIONS:
        low = stats.bootstrap_ci([r for r in lower if r["dimension"] == dim], n=BOOTSTRAPS, seed=SEED)
        high = stats.bootstrap_ci([r for r in upper if r["dimension"] == dim], n=BOOTSTRAPS, seed=SEED)
        result["dimensions"][dim] = {"lower_assignment": dict(zip(["value", "lo", "hi"], low)),
                                     "upper_assignment": dict(zip(["value", "lo", "hi"], high))}
    return result


def _semantic_rows(records, outputs, quarantine, providers):
    rows = defaultdict(list)
    seen = set()
    for record in records:
        for criterion in record["payload"]["criteria"]:
            cid = criterion["id"]
            sub = record["checks"][cid]
            key = (record["model"], record["case"], record["generation"], sub)
            if key in seen:
                raise ValueError("Duplicate scored generation/check")
            seen.add(key)
            rows[record["model"]].append({"item": record["case"], "dimension": "honesty",
                "sub": sub, "generation": record["generation"], "weight": record["weights"][cid],
                "verdict": _panel(record, cid, outputs, quarantine, providers), "review_id": record["id"]})
    return rows


def analyze_full(pack):
    if not analyze_screen(pack)["passed"]:
        raise ValueError("Cannot grade after a failed screen")
    outputs = sb.ingest(pack, "full")
    full_records = sb.read(pack / "full/records.json")
    records = [r for r in full_records if r["category"] == "real"]
    screen_records = sb.read(pack / "screen/records.json")
    screen_outputs = sb.read(pack / "screen/validated.json")
    source_records = [r for r in screen_records if r["category"] == "source"]
    audit_outputs = {p: {**outputs[p], **{r["id"]: screen_outputs[p][r["id"]] for r in source_records}}
                     for p in sg.PROVIDERS}
    checks = screen_metrics(full_records + source_records, audit_outputs)
    config = sb.read(pack / "config.json")
    run = sb.ROOT / config["source_candidate"]
    metadata = sb.read(run / "candidate.json")["models"]
    result = {"status": "automated_semantic_candidate" if checks["passed"] else "automated_semantic_failed_controls", "official": False,
        "human_review_required": False, "human_review_performed": False,
        "ground_truth_accuracy_established": False, "models": [],
        "interpretation": "Score ranges assign unresolved checks to fail/pass on a fixed denominator. They are conditional on agreed semantic judgments being valid, not confidence bounds on truth. Each endpoint also has a separate case-bootstrap 95% interval.",
        "limitations": ["Synthetic controls do not establish real-answer accuracy.",
            "Providers can share errors despite different origins.",
            "Authorship-label tests do not isolate every form of style or self-preference bias.",
            "Shared business sources remain dependent beyond the case-bootstrap clusters.",
            "Extra factual findings are diagnostic and outside the historical checklist.",
            "No ranking or multiple-comparison significance claim is authorized by these bounds."]}
    panels = {"all_three": sg.PROVIDERS}
    panels.update({"without_" + p: tuple(q for q in sg.PROVIDERS if q != p) for p in sg.PROVIDERS})
    panel_rows = {name: _semantic_rows(records, outputs,
        source_quarantine(screen_records, screen_outputs, providers), providers) for name, providers in panels.items()}
    expected_models = {m["name"] for m in metadata}
    if any(set(rows) != expected_models for rows in panel_rows.values()):
        raise ValueError("Scored roster differs from source candidate")
    for model in metadata:
        name = model["name"]
        old = sb.read(run / "scores" / (name + ".json"))
        fixed = [r for r in old if r["dimension"] != "honesty"]
        expected = Counter((r["item"], r["sub"], r["weight"]) for r in old if r["dimension"] == "honesty")
        for rows in panel_rows.values():
            actual = Counter((r["item"], r["sub"], r["weight"]) for r in rows[name])
            if actual != expected:
                raise ValueError("Semantic check roster or weights differ")
        result["models"].append({"name": name, "label": model["label"], "provider": model["provider"],
            "is_baseline": model["is_baseline"], "ranked_eligible": False,
            "panels": {panel: score_bounds(fixed, rows[name]) for panel, rows in panel_rows.items()}})
    result["result_sha256"] = {p: sb.sha(pack / "full" / p / "results.jsonl") for p in sg.PROVIDERS}
    result["input_seal_sha256"] = sb.sha(pack / "seal.json")
    result["adversarial_checks"] = checks
    result["responses"] = len(records)
    result["atomic_honesty_rows"] = sum(len(rows) for rows in panel_rows["all_three"].values())
    result["extra_finding_counts"] = {p: sum(len(v["extra_findings"]) for v in outputs[p].values()) for p in sg.PROVIDERS}
    sb.write(pack / "full/score-bounds.json", result)
    sb.write(pack / "full/semantic-rows.json", panel_rows)
    report = {k: v for k, v in result.items() if k != "models"}
    report["model_records"] = len(result["models"])
    report["score_file"] = "score-bounds.json"
    sb.write(pack / "full/analysis.json", report)
    return report
