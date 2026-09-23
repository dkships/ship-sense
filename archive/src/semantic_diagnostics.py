"""Offline failure analysis; never submits, repairs grades, or releases funds."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from . import semantic_audit as audit, semantic_batch as sb
from . import semantic_budget as money, semantic_collect as collect
from . import semantic_grade as sg, stats


def diagnostic_rows(path, records, model, provider):
    """Keep every planned criterion; an invalid response is entirely unresolved."""
    roster = {r["id"]: r for r in records}
    if len(roster) != len(records):
        raise ValueError("Duplicate planned record")
    outputs, envelopes, errors = {}, {}, []
    for line in path.read_text().splitlines():
        envelope = sg.parse_response(line)
        rid = envelope.get("custom_id", envelope.get("key"))
        if rid not in roster or rid in envelopes:
            raise ValueError("Unknown or duplicate result; cannot establish diagnostic coverage")
        envelopes[rid] = envelope
        record = roster[rid]
        try:
            collect._check_result(envelope, record, model, provider)
            _, result = sb.batch._result_from_line(provider, {"id": model}, envelope)
            outputs[rid] = sg.validate_response(record["payload"], sg.parse_response(result.text))
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            errors.append({"request_id": rid, "reason": str(exc)})
            ids = [c["id"] for c in record["payload"]["criteria"]]
            outputs[rid] = {
                "votes": {cid: {"verdict": "unresolved", "issues": ["invalid_response"]} for cid in ids},
                "raw_decisions": [{"id": cid, "key_supported": "uncertain"} for cid in ids],
                "extra_findings": [],
            }
    if set(envelopes) != set(roster):
        raise ValueError("Missing result; collect the complete roster first")
    return outputs, envelopes, errors


def usage_report(envelopes, requests, records, provider, config):
    bounds = {r["id"]: money.request_bound(q, provider, config)
              for r, q in zip(records, requests, strict=True)}
    rates = [Decimal(str(v)) for v in config["batch_rate_bounds"][provider]]
    incoming, outgoing, cost, errors = 0, 0, Decimal(0), []
    for rid, envelope in envelopes.items():
        try:
            a, b = money.token_counts(provider, envelope)
            if a > bounds[rid][0] or b > config["max_output_tokens"]:
                raise ValueError("Usage exceeds the reserved token bound")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"request_id": rid, "reason": str(exc)})
            continue
        incoming += a
        outgoing += b
        cost += a * rates[0] + b * rates[1]
    return {"input_tokens": incoming, "output_tokens_including_reasoning": outgoing,
        "usage_errors": errors, "usage_complete": not errors and set(envelopes) == set(bounds),
        "conservative_usage_usd": str(cost / 1_000_000) if not errors else None,
        "original_reservation_usd": str(Decimal(sum(b[1] for b in bounds.values())) / 1_000_000),
        "budget_allowance_released": False, "invoice_reconciled": False}


def interval(pairs):
    rows = [{"item": cluster, "correct": correct, "weight": 1.0} for cluster, correct in pairs]
    if not rows:
        return {"count": 0, "total": 0, "clusters": 0, "value": None, "lo": None, "hi": None}
    value, lo, hi = stats.bootstrap_ci(rows, n=audit.BOOTSTRAPS, seed=audit.SEED)
    return {"count": sum(correct for _, correct in pairs), "total": len(pairs),
        "clusters": len({c for c, _ in pairs}), "value": value, "lo": lo, "hi": hi}


def provider_details(records, outputs):
    correct, recalled, clear, critical, failures = [], [], [], [], []
    same, resolved, binary = defaultdict(list), defaultdict(list), defaultdict(list)
    changes, issues = Counter(), Counter()
    for record in records:
        rid = record["id"]
        for cid, vote in outputs[rid]["votes"].items():
            issues.update(vote["issues"])
            label = vote["verdict"]
            if record["category"] == "control":
                expected, family = record["expected"][cid], record["family"]
                correct.append((family, label == expected))
                clear.append((family, label != "unresolved"))
                if expected == "fail":
                    recalled.append((family, label == "fail"))
                    critical.append((family, label == "pass"))
                if label != expected:
                    failures.append({"record": record, "criterion_id": cid,
                                     "review": outputs[rid], "failure": "control"})
            if record["category"] != "metamorphic":
                continue
            parent = outputs[record["parent"]]["votes"][cid]["verdict"]
            name, case = record["variant"], record["case"]
            is_clear = "unresolved" not in (parent, label)
            same[name].append((case, label == parent))
            resolved[name].append((case, is_clear))
            binary[name].append((case, is_clear and label != parent))
            if label != parent:
                changes[parent + "->" + label] += 1
                failures.append({"record": record, "criterion_id": cid, "review": outputs[rid],
                                 "parent_review": outputs[record["parent"]], "failure": "changed_label"})
    return {"control_accuracy": interval(correct), "failure_recall": interval(recalled),
        "control_clear_coverage": interval(clear), "critical_false_pass_rate": interval(critical),
        "invariance": {name: {"agreement": interval(same[name]),
            "resolved_pairs": interval(resolved[name]), "clear_binary_flips": interval(binary[name])}
            for name in sorted(same)}, "transitions": dict(changes), "issue_counts": dict(issues)}, failures


def diagnose(pack):
    config, waves = collect.saved_waves(pack, "screen")
    records = sb.read(pack / "screen/records.json")
    if sum(len(rows) for _, rows in waves) != len(records):
        raise ValueError("The screen roster is incomplete")
    outputs = {p: {} for p in sg.PROVIDERS}
    errors, usage, hashes = defaultdict(list), defaultdict(list), {}
    for wave, rows in waves:
        reservation = sb.read(wave / "reservation.json")
        _, requests = money.wave_inputs(pack, wave, reservation)
        for provider in sg.PROVIDERS:
            path = wave / provider / "results.jsonl"
            status = sb.read(path.parent / (sb.SDK_NAMES[provider] + "-status.json"))
            if not sb._success(provider, status) or (path.parent / "errors.jsonl").exists():
                raise ValueError("Successful batch with a complete result file required")
            votes, envelopes, invalid = diagnostic_rows(path, rows, config["models"][provider], provider)
            outputs[provider].update(votes)
            errors[provider].extend(invalid)
            usage[provider].append(usage_report(envelopes, requests[provider], rows, provider, config))
            hashes[str(path.relative_to(pack))] = sb.sha(path)
    metrics = audit.screen_metrics(records, outputs)
    details, evidence = {}, {}
    for provider in sg.PROVIDERS:
        details[provider], evidence[provider] = provider_details(records, outputs[provider])
        details[provider].update(valid_responses=len(records) - len(errors[provider]),
                                 invalid_responses=len(errors[provider]))
    quarantine = audit.source_quarantine(records, outputs)
    real = [(r["case"], audit._panel(r, c["id"], outputs, quarantine) != "unresolved")
            for r in records if r["category"] == "real" for c in r["payload"]["criteria"]]
    source = [(r["case"], (r["case"], r["checks"][c["id"]]) not in quarantine)
              for r in records if r["category"] == "source" for c in r["payload"]["criteria"]]
    all_usage = [row for rows in usage.values() for row in rows]
    usage_complete = all(row["usage_complete"] for row in all_usage)
    full_submitted = any(sb.read(w / "reservation.json")["phase"] == "full"
                         for w in (pack / "waves").iterdir())
    public = {"status": "diagnostic_only", "official": False, "full_regrade_submitted": full_submitted,
        "acceptance_enabled": False, "diagnostic_thresholds_passed": metrics["passed"],
        "frozen_gates_passed": metrics["passed"] and not any(errors.values()),
        "all_responses_valid": not any(errors.values()), "providers": details,
        "real_consensus_resolution": interval(real), "source_support": interval(source),
        "thresholds": metrics["thresholds"], "bootstrap_replicates": audit.BOOTSTRAPS,
        "bootstrap_seed": audit.SEED, "control_cluster": "family", "real_cluster": "case",
        "invalid_response_policy": "All its planned criteria remain unresolved; no partial JSON repair",
        "ground_truth_accuracy_established": False, "causal_identity_bias_established": False,
        "unchanged_input_replicates": 0, "human_review_required": False,
        "usage_complete": usage_complete,
        "conservative_usage_usd": str(sum(Decimal(r["conservative_usage_usd"]) for r in all_usage)) if usage_complete else None,
        "original_reservation_usd": str(sum(Decimal(r["original_reservation_usd"]) for r in all_usage)),
        "budget_allowance_released": False, "invoice_reconciled": False,
        "new_provider_calls": 0, "paid_retries": 0}
    private = {"public_summary": public, "frozen_metrics": metrics, "usage": dict(usage),
        "invalid_responses": dict(errors), "result_sha256": hashes,
        "input_seal_sha256": sb.sha(pack / "seal.json")}
    return private, outputs, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pack, output = args.pack.resolve(), args.output.resolve()
    if output == pack or pack in output.parents:
        raise ValueError("Diagnostics must stay outside the frozen pack")
    report, outputs, evidence = diagnose(pack)
    for name, value in [("report.json", report), ("public.json", report["public_summary"]),
                        ("votes.json", outputs), ("failure-evidence.json", evidence)]:
        sb.write(output / name, value)
    print("Saved offline diagnostics; no grades, reservations, or provider jobs changed.")


if __name__ == "__main__":
    main()
