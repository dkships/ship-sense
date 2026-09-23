"""Collect existing sealed batches without submissions, retries, or grade repair."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import time

from . import semantic_batch as sb, semantic_budget as money, semantic_grade as sg

FAILED_STATES = {"failed", "expired", "cancelled", "JOB_STATE_FAILED",
                 "JOB_STATE_EXPIRED", "JOB_STATE_CANCELLED"}
NORMAL_END = {"openai": "completed", "anthropic": "end_turn", "google": "STOP"}


def saved_waves(pack, phase):
    config = sb.verify(pack)
    ends, waves = {"screen": 0, "full": 0}, []
    for index, wave in enumerate(sorted((pack / "waves").glob("*"))):
        if wave.name != f"{index:04d}" or not wave.is_dir() or wave.is_symlink():
            raise ValueError("Unexpected reservation path")
        reservation = sb.read(wave / "reservation.json")
        name, start, stop = (reservation[k] for k in ("phase", "start", "stop"))
        if name not in ends or type(start) is not int or type(stop) is not int:
            raise ValueError("Invalid reservation")
        size = len(sb.read(pack / name / "records.json"))
        if start != ends[name] or not start < stop <= size:
            raise ValueError("Overlapping or missing reserved records")
        records, _ = money.wave_inputs(pack, wave, reservation)
        ends[name] = stop
        for provider in sg.PROVIDERS:
            folder = wave / provider
            job = folder / (sb.SDK_NAMES[provider] + "-batch.json")
            marker = folder / "submission-attempt.json"
            if not job.exists() or not marker.exists():
                raise ValueError("Missing saved submission; collector cannot submit or retry")
            if sb.read(marker)["request_sha256"] != sb.sha(folder / "requests.jsonl"):
                raise ValueError("Submission differs from the reserved requests")
        if name == phase:
            waves.append((wave, records))
    if not waves:
        raise ValueError("No submitted batches to collect")
    return config, waves


def _check_result(envelope, record, model, provider):
    _, result = sb.batch._result_from_line(provider, {"id": model}, envelope)
    if result.error or result.finish_reason != NORMAL_END[provider]:
        raise ValueError("Failed or incomplete response: " + str(result.finish_reason))
    if result.model != model:
        raise ValueError("Unexpected reviewer model")
    if provider == "google":
        body = envelope.get("response", {})
        version = body.get("modelVersion", body.get("model_version"))
        if version and not (version == model or version.startswith(model + "-")):
            raise ValueError("Unexpected reviewer model version")
    sg.validate_response(record["payload"], sg.parse_response(result.text))


def result_report(paths, record_list, model, provider):
    records = {r["id"]: r for r in record_list}
    if len(records) != len(record_list):
        raise ValueError("Duplicate planned record")
    seen, errors, valid, invalid = Counter(), [], 0, 0
    for path in paths:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            rid = None
            try:
                envelope = sg.parse_response(line)
                rid = envelope.get("custom_id", envelope.get("key"))
                if not isinstance(rid, str) or rid not in records:
                    raise ValueError("Unknown or missing request ID")
                seen[rid] += 1
                if seen[rid] > 1:
                    raise ValueError("Duplicate request result")
                _check_result(envelope, records[rid], model, provider)
                valid += 1
            except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                invalid += 1
                errors.append({"file": path.name, "line": number, "request_id": rid, "reason": str(exc)})
    missing = sorted(set(records) - set(seen))
    if missing:
        errors.append({"reason": "Missing request results", "request_ids": missing})
    return {"planned_responses": len(records), "valid_responses": valid,
        "invalid_responses": invalid, "missing_responses": len(missing),
        "complete_and_valid": not errors, "errors": errors,
        "result_sha256": {p.name: sb.sha(p) for p in paths}}


def _status(folder, provider):
    path = folder / (sb.SDK_NAMES[provider] + "-status.json")
    record = sb.read(path) if path.exists() else {}
    value = record.get("status", record.get("processing_status", record.get("state")))
    return getattr(value, "value", value)


def inspect(pack, phase):
    config, waves = saved_waves(pack, phase)
    jobs, valid, complete = [], True, True
    for wave, records in waves:
        for provider in sg.PROVIDERS:
            folder = wave / provider
            status = _status(folder, provider)
            paths = [folder / name for name in ("results.jsonl", "errors.jsonl") if (folder / name).exists()]
            terminal = status == NORMAL_END[provider] or (provider == "anthropic" and status == "ended")
            terminal = terminal or (provider == "google" and status == "JOB_STATE_SUCCEEDED")
            finished = (terminal and bool(paths)) or status in FAILED_STATES
            report = result_report(paths, records, config["models"][provider], provider) if paths else None
            valid = valid and bool(report and report["complete_and_valid"]) and status not in FAILED_STATES
            complete = complete and finished
            jobs.append({"wave": wave.name, "provider": provider, "requests": len(records),
                "mode": "native_batch", "last_saved_status": status, "collection_complete": finished,
                "results": report})
    roster_complete = sum(len(records) for _, records in waves) == len(sb.read(pack / phase / "records.json"))
    return {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "phase": phase,
        "status_source": "Saved provider status; inspect does not query providers",
        "submitted_jobs": len(jobs), "submitted_requests": sum(j["requests"] for j in jobs),
        "collection_complete": complete, "all_responses_valid": valid,
        "phase_roster_complete": roster_complete,
        "validation_status": "ready_for_frozen_analysis" if complete and valid and roster_complete else "incomplete_or_failed_validation",
        "jobs": jobs, "new_submissions": 0, "paid_retries": 0, "budget_allowance_released": False,
        "absolute_cap_usd": config["total_cap_usd"], "shared_submission_cap_usd": config["submission_cap_usd"],
        "input_seal_sha256": sb.sha(pack / "seal.json"), "grading_policy_changed": False,
        "official": False, "human_review_required": False}


def collect_once(pack, phase):
    with money.lock():
        _, waves = saved_waves(pack, phase)
        errors = []
        for wave, _ in waves:
            for provider in sg.PROVIDERS:
                folder = wave / provider
                error_only = (folder / "errors.jsonl").exists() and not (folder / "results.partial.jsonl").exists()
                if (folder / "results.jsonl").exists() or error_only:
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
                    # A read failure must not strand other already-paid jobs.
                    errors.append({"wave": wave.name, "provider": provider, "error_type": type(exc).__name__})
        report = inspect(pack, phase)
        report["collection_errors"] = errors
        sb.write(pack / "collection" / (phase + ".json"), report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--phase", choices=["screen", "full"], default="screen")
    parser.add_argument("command", choices=["inspect", "watch"])
    args = parser.parse_args()
    pack = args.pack.resolve()
    if args.command == "inspect":
        report = inspect(pack, args.phase)
        sb.write(pack / "collection" / (args.phase + ".json"), report)
    else:
        deadline = time.monotonic() + 26 * 60 * 60
        while True:
            report = collect_once(pack, args.phase)
            if report["collection_complete"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Resume this collection command; existing jobs will not be resubmitted")
            time.sleep(sb.POLL_SECONDS)
    print(f"Saved {args.phase} collection report. Complete={report['collection_complete']}; "
          f"all responses valid={report['all_responses_valid']}. No new batches or paid retries submitted.", flush=True)


if __name__ == "__main__":
    main()
