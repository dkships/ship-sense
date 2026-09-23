"""Retrieve existing revision jobs despite billing errors; never submit inference."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import time

from . import revision_audit as review, revision_budget as budget
from . import semantic_batch as sb, semantic_budget as money, semantic_collect as collect
from . import semantic_grade as sg


def saved_waves(pack, phase):
    config = budget.verify(pack)
    if phase not in {"screen", "full"}:
        raise ValueError("Unknown phase")
    journal = [sg.parse_response(line) for line in (sb.ROOT / budget.JOURNAL).read_text().splitlines()]
    paths = sorted((pack / "waves").glob("*"))
    if len(paths) != len(journal):
        raise ValueError("Reserved wave differs from append-only journal")
    ends, waves = {"screen": 0, "full": 0}, []
    for index, wave in enumerate(paths):
        if wave.name != f"{index:04d}" or not wave.is_dir() or wave.is_symlink():
            raise ValueError("Unexpected revision wave path")
        reservation, records, requests = budget.wave_inputs(pack, wave)
        event = {"pack_path": str(pack.relative_to(sb.ROOT)), "wave": wave.name, **reservation,
                 "request_sha256": {p: sb.sha(wave / p / "requests.jsonl") for p in sg.PROVIDERS}}
        name = reservation["phase"]
        if event != journal[index] or reservation["start"] != ends[name]:
            raise ValueError("Reservation differs from journal or has a gap")
        ends[name] = reservation["stop"]
        for provider in sg.PROVIDERS:
            folder = wave / provider
            job = folder / (sb.SDK_NAMES[provider] + "-batch.json")
            marker = folder / "submission-attempt.json"
            if not job.is_file() or not marker.is_file():
                raise ValueError("Missing saved submission; collector cannot submit or retry")
            if any(p.is_symlink() for p in folder.iterdir()) or folder.is_symlink():
                raise ValueError("Unexpected submission symlink")
            if sb.read(marker)["request_sha256"] != sb.sha(folder / "requests.jsonl"):
                raise ValueError("Submission differs from reserved requests")
        if name == phase:
            waves.append((wave, records, requests))
    if not waves:
        raise ValueError("No submitted batches to collect")
    return config, waves


def _error_type(provider, envelope):
    value = envelope.get("result", {}).get("error") if provider == "anthropic" else envelope.get("error")
    if not isinstance(value, dict):
        return "native_request_error"
    nested = value.get("error", value)
    return nested.get("type", "native_request_error") if isinstance(nested, dict) else "native_request_error"


def _job_report(wave, records, requests, provider, config):
    folder, model = wave / provider, config["models"][provider]
    status = collect._status(folder, provider)
    successful = status in {"completed", "ended", "JOB_STATE_SUCCEEDED"}
    path = folder / "results.jsonl"
    paths = [folder / name for name in ("results.jsonl", "errors.jsonl") if (folder / name).exists()]
    bounds = {r["id"]: money.request_bound(q, provider, config)
              for r, q in zip(records, requests, strict=True)}
    row = {"wave": wave.name, "provider": provider, "requests": len(records), "mode": "native_batch",
        "last_saved_status": status, "collection_complete": (successful and bool(paths)) or status in collect.FAILED_STATES,
        "result_envelopes": 0, "native_successes": 0, "native_errors": 0, "native_error_types": {},
        "usage_records": 0, "usage_unavailable": 0, "input_tokens": 0, "output_tokens": 0,
        "usage_overruns": 0, "max_output_tokens": 0, "roster_complete": False,
        "valid_responses": 0, "invalid_responses": 0, "parse_errors": [],
        "reserved_bound_microusd": sum(v[1] for v in bounds.values()),
        "result_sha256": {p.name: sb.sha(p) for p in paths}}
    seen, error_types = Counter(), Counter()
    for source in paths:
        for line in source.read_text().splitlines():
            row["result_envelopes"] += 1
            try:
                envelope = sg.parse_response(line)
                rid, result = sb.batch._result_from_line(provider, {"id": model}, envelope)
                if rid not in bounds:
                    raise ValueError("Unknown result ID")
                seen[rid] += 1
                if result.error:
                    row["native_errors"] += 1
                    error_types[_error_type(provider, envelope)] += 1
                else:
                    row["native_successes"] += 1
                try:
                    incoming, outgoing = money.token_counts(provider, envelope)
                except (ValueError, KeyError, TypeError):
                    row["usage_unavailable"] += 1
                    continue
                row["usage_records"] += 1
                row["input_tokens"] += incoming
                row["output_tokens"] += outgoing
                row["max_output_tokens"] = max(row["max_output_tokens"], outgoing)
                row["usage_overruns"] += incoming > bounds[rid][0] or outgoing > config["max_output_tokens"]
            except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                row["parse_errors"].append(type(exc).__name__)
    row["native_error_types"] = dict(error_types)
    row["roster_complete"] = not row["parse_errors"] and seen == Counter(bounds.keys())
    if path.exists() and row["roster_complete"]:
        try:
            _, errors = review.read_outputs(path, records, provider, model)
            row["invalid_responses"] = len(errors)
            row["valid_responses"] = len(records) - len(errors)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            row["parse_errors"].append(type(exc).__name__)
    row["all_responses_valid"] = row["valid_responses"] == len(records) and not row["parse_errors"]
    return row


def inspect(pack, phase):
    config, waves = saved_waves(pack, phase)
    jobs = [_job_report(w, records, requests[p], p, config)
            for w, records, requests in waves for p in sg.PROVIDERS]
    try:
        budget.account(pack, config)
        billing_error = None
    except (ValueError, KeyError, TypeError, OSError) as exc:
        billing_error = str(exc)
    base = sb.read(pack / "seal.json")["base_reserved_microusd"]
    return {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "phase": phase,
        "status_source": "Saved provider records; inspect makes no network calls",
        "submitted_jobs": len(jobs), "submitted_requests": sum(j["requests"] for j in jobs),
        "collection_complete": all(j["collection_complete"] for j in jobs),
        "phase_roster_complete": sum(len(r) for _, r, _ in waves) == len(sb.read(pack / phase / "records.json")),
        "all_responses_valid": all(j["all_responses_valid"] for j in jobs),
        "jobs": jobs, "billing_error": billing_error,
        "usage_overruns": sum(j["usage_overruns"] for j in jobs),
        "base_reserved_microusd": base,
        "reserved_bound_microusd": base + sum(j["reserved_bound_microusd"] for j in jobs),
        "bound_scope": "Original reservation plus this phase's full reservations; no allowance released",
        "new_submissions": 0, "paid_retries": 0, "budget_allowance_released": False,
        "invoice_reconciled": False, "official": False, "human_review_required": False,
        "absolute_cap_usd": config["total_cap_usd"], "shared_submission_cap_usd": config["submission_cap_usd"],
        "input_seal_sha256": sb.sha(pack / "seal.json"), "accepted_analysis_written": False}


def collect_once(pack, phase):
    with money.lock():
        _, waves = saved_waves(pack, phase)
        errors = []
        for wave, _, _ in waves:
            for provider in sg.PROVIDERS:
                folder = wave / provider
                if (folder / "results.jsonl").exists() or collect._status(folder, provider) in collect.FAILED_STATES:
                    continue
                if (folder / "errors.jsonl").exists() and not (folder / "results.partial.jsonl").exists():
                    continue
                job = folder / (sb.SDK_NAMES[provider] + "-batch.json")
                try:
                    client = sb._client(provider)
                    state = getattr(sb.batch, "status_" + sb.SDK_NAMES[provider])(job, client=client)
                    value = state.get("status", state.get("processing_status", state.get("state")))
                    print(phase, wave.name, provider, getattr(value, "value", value), flush=True)
                    if sb._success(provider, state):
                        partial = folder / "results.partial.jsonl"
                        getattr(sb.batch, "download_" + sb.SDK_NAMES[provider])(job, output=partial, client=client)
                        if partial.exists():
                            partial.replace(folder / "results.jsonl")
                except Exception as exc:
                    errors.append({"wave": wave.name, "provider": provider, "error_type": type(exc).__name__})
        report = inspect(pack, phase)
        report["collection_errors"] = errors
        sb.write(pack / "recovery" / (phase + ".json"), report)
        return report


def watch(pack, phase):
    deadline = time.monotonic() + 26 * 60 * 60
    while True:
        report = collect_once(pack, phase)
        if report["collection_complete"]:
            return report
        if report["collection_errors"]:
            raise RuntimeError("Collection read failed; see recovery report and resume the same collection command")
        if time.monotonic() >= deadline:
            raise TimeoutError("Resume this collection command; no batch will be resubmitted")
        time.sleep(sb.POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--phase", choices=["screen", "full"], default="screen")
    parser.add_argument("command", choices=["inspect", "watch"])
    args = parser.parse_args()
    pack = args.pack.resolve()
    if args.command == "inspect":
        report = inspect(pack, args.phase)
        sb.write(pack / "recovery" / (args.phase + ".json"), report)
    else:
        report = watch(pack, args.phase)
    print(f"Collection complete={report['collection_complete']}; all responses valid={report['all_responses_valid']}. "
          "No inference submitted, no budget released, no accepted grades written.", flush=True)


if __name__ == "__main__":
    main()
