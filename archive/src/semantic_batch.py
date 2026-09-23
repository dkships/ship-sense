"""Sealed, budgeted batch workflow with an adversarial gate before full grading."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import time

from . import batch, loader, semantic_grade as sg

ROOT = loader.ROOT
POLL_SECONDS = 45
SDK_NAMES = {"openai": "openai", "anthropic": "anthropic", "google": "gemini"}


def read(path: Path):
    return sg.parse_response(path.read_text())


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(pack: Path) -> dict:
    seal = read(pack / "seal.json")
    if pack.resolve() != (ROOT / seal["pack_path"]).resolve():
        raise ValueError("Sealed pack location differs from the supplied pack")
    for relative, digest in seal["files"].items():
        path = ROOT / relative
        if not path.resolve().is_relative_to(ROOT.resolve()) or path.is_symlink():
            raise ValueError("Sealed input escapes the checkout")
        if sha(path) != digest:
            raise ValueError("Sealed input changed: " + relative)
    config = read(pack / "config.json")
    budget = read(pack / "budget.json")
    from . import semantic_budget as money
    money.check_caps(config)
    if budget.get("accounting") != "reserved_waves_v1":
        raise ValueError("Legacy preparation is superseded by the $100 cap")
    money.authorize(pack)
    return config


def make_request(provider, model, rid, payload, limit):
    text = json.dumps(payload, ensure_ascii=False)
    if provider == "openai":
        return {"custom_id": rid, "method": "POST", "url": "/v1/responses", "body": {
            "model": model, "instructions": sg.INSTRUCTIONS,
            "input": [{"role": "user", "content": text}], "max_output_tokens": limit,
            "reasoning": {"effort": "medium"}, "store": False,
            "text": {"format": {"type": "json_schema", "name": "evidence_grading",
                                 "strict": True, "schema": sg.SCHEMA}}}}
    if provider == "anthropic":
        return {"custom_id": rid, "params": {"model": model, "system": sg.INSTRUCTIONS,
            "messages": [{"role": "user", "content": text}], "max_tokens": limit,
            "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": sg.SCHEMA}}}}
    if provider == "google":
        return {"key": rid, "request": {
            "system_instruction": {"parts": [{"text": sg.INSTRUCTIONS}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generation_config": {"candidate_count": 1, "max_output_tokens": limit, "response_mime_type": "application/json",
                "response_json_schema": sg.SCHEMA, "thinking_config": {"thinking_level": "medium"}}}}
    raise ValueError("Unknown provider")


def prepare_phase(pack, phase, records, config):
    folder = pack / phase
    if folder.exists():
        raise FileExistsError("Never replace an existing request phase")
    write(folder / "records.json", records)
    estimates = {}
    for provider in sg.PROVIDERS:
        model = config["models"][provider]
        requests = [make_request(provider, model, r["id"], r["payload"], config["max_output_tokens"]) for r in records]
        path = folder / provider / "requests.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
        write(path.with_name("manifest.json"), {"provider": provider, "model_name": model,
            "model_id": model, "stage_id": phase, "run_id": pack.name,
            "requests_file": str(path.relative_to(ROOT)), "requests": len(requests)})
        from . import semantic_budget as money
        estimates[provider] = sum(money.request_bound(r, provider, config)[1] for r in requests) / 1e6
    return {"requests_per_provider": len(records), "requests": len(records) * len(sg.PROVIDERS),
            "provider_bounds_usd": estimates, "bound_usd": sum(estimates.values())}


def _client(provider):
    # Submission retries can duplicate spend after an ambiguous network failure.
    if provider == "openai":
        import openai
        return openai.OpenAI(base_url="https://api.openai.com/v1", timeout=120, max_retries=0)
    if provider == "anthropic":
        import anthropic
        return anthropic.Anthropic(base_url="https://api.anthropic.com", timeout=120, max_retries=0)
    from google import genai
    from google.genai import types
    return genai.Client(vertexai=False, http_options=types.HttpOptions(
        base_url="https://generativelanguage.googleapis.com", timeout=120000,
        retry_options=types.HttpRetryOptions(attempts=1)))


def _credentials():
    missing = [key for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"] if not os.environ.get(key)]
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        missing.append("GEMINI_API_KEY or GOOGLE_API_KEY")
    if missing:
        raise RuntimeError("Provider credentials absent from process: " + ", ".join(missing))


def submit(pack: Path, phase: str):
    from . import semantic_budget as money
    config = verify(pack)
    if phase not in ("screen", "full"):
        raise ValueError("Unknown phase")
    if phase == "full":
        from .semantic_audit import analyze_screen
        if not analyze_screen(pack)["passed"]:
            raise ValueError("Full grading blocked by failed adversarial screen")
    with money.lock():
        state = money.account(pack)
        # A marker with no saved job may already represent a charged request.
        for marker in pack.glob("waves/*/*/submission-attempt.json"):
            provider = marker.parent.name
            if not (marker.parent / (SDK_NAMES[provider] + "-batch.json")).exists():
                raise RuntimeError("Unresolved submission attempt; no new paid request: " + provider)
        wave = None
        if state["waves"]:
            last = pack / "waves" / state["waves"][-1]["path"]
            if state["waves"][-1]["phase"] == phase and state["ends"][phase] == len(read(pack / phase / "records.json")):
                wave = last
            if not all((last / p / "results.jsonl").exists() for p in sg.PROVIDERS):
                if state["waves"][-1]["phase"] != phase:
                    raise ValueError("Previous phase is incomplete")
                wave = last
        if wave is not None and all((wave / p / (SDK_NAMES[p] + "-batch.json")).exists() for p in sg.PROVIDERS):
            return wave
        if date.today().isoformat() > config["submit_before"]:
            raise ValueError("Pricing/model verification expired; collect existing jobs only")
        _credentials()
        if wave is None:
            wave = money.reserve(pack, phase)
        if wave is None:
            return None
        # Reserve all three providers together before creating the first client.
        for provider in sg.PROVIDERS:
            folder = wave / provider
            if (folder / (SDK_NAMES[provider] + "-batch.json")).exists():
                continue
            marker = folder / "submission-attempt.json"
            with marker.open("x") as handle:
                json.dump({"state": "attempted_reconcile_if_no_job",
                    "request_sha256": sha(folder / "requests.jsonl")}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            getattr(batch, "submit_" + SDK_NAMES[provider])(folder / "manifest.json", client=_client(provider))
            print(phase, wave.name, provider, "submitted", flush=True)
        return wave


def _success(provider, state):
    if provider == "openai":
        return state.get("status") == "completed"
    if provider == "anthropic":
        return state.get("processing_status") == "ended"
    return state.get("state") == "JOB_STATE_SUCCEEDED"


def collect(pack: Path, phase: str) -> bool:
    from . import semantic_budget as money
    verify(pack)
    with money.lock():
        state = money.account(pack)
        waves = [pack / "waves" / w["path"] for w in state["waves"] if w["phase"] == phase]
        for wave in waves:
            for provider in sg.PROVIDERS:
                folder = wave / provider
                target = folder / "results.jsonl"
                if target.exists():
                    continue
                job = folder / (SDK_NAMES[provider] + "-batch.json")
                if not job.exists():
                    continue
                client = _client(provider)
                status_record = getattr(batch, "status_" + SDK_NAMES[provider])(job, client=client)
                status = status_record.get("status", status_record.get("processing_status", status_record.get("state")))
                print(phase, wave.name, provider, status, flush=True)
                if _success(provider, status_record):
                    partial = folder / "results.partial.jsonl"
                    getattr(batch, "download_" + SDK_NAMES[provider])(job, output=partial, client=client)
                    partial.replace(target)
                elif status in {"failed", "expired", "cancelled", "JOB_STATE_FAILED", "JOB_STATE_EXPIRED", "JOB_STATE_CANCELLED"}:
                    raise RuntimeError("Batch ended unsuccessfully; no automatic paid retry: " + provider)
        sb_state = money.account(pack)
        write(pack / "spend-status.json", sb_state)
        completed = bool(waves) and all((w / p / "results.jsonl").exists() for w in waves for p in sg.PROVIDERS)
        if completed and sb_state["ends"][phase] == len(read(pack / phase / "records.json")):
            for provider in sg.PROVIDERS:
                path = pack / phase / provider / "results.jsonl"
                combined = "".join((w / provider / "results.jsonl").read_text().rstrip() + "\n" for w in waves)
                if path.exists() and path.read_text() != combined:
                    raise ValueError("Aggregated results differ from original wave results")
                partial = path.with_name("results.partial.jsonl")
                partial.write_text(combined)
                partial.replace(path)
        return completed


def read_outputs(path, record_list, model, provider):
    records = {r["id"]: r for r in record_list}
    if len(records) != len(record_list):
        raise ValueError("Duplicate planned result ID")
    checked = {}
    for line in path.read_text().splitlines():
        envelope = sg.parse_response(line)
        rid, result = batch._result_from_line(provider, {"id": model}, envelope)
        if rid not in records or rid in checked:
            raise ValueError("Unknown or duplicate result ID")
        normal = {"openai": "completed", "anthropic": "end_turn", "google": "STOP"}[provider]
        if result.error or result.finish_reason != normal:
            raise ValueError("Incomplete or failed provider response: " + provider + " / " + rid)
        if result.model != model:
            raise ValueError("Provider returned an unexpected model")
        response = sg.parse_response(result.text)
        checked[rid] = sg.validate_response(records[rid]["payload"], response)
        body = envelope.get("response", {})
        version = body.get("modelVersion", body.get("model_version")) if provider == "google" else result.model
        if version and not (version == model or version.startswith(model + "-")):
            raise ValueError("Provider returned an unexpected model version")
        checked[rid].update(provider_model=result.model, returned_model_version=version,
            usage=batch._to_plain(result.usage), envelope_sha256=hashlib.sha256(line.encode()).hexdigest())
    if set(checked) != set(records):
        raise ValueError("Missing provider results; no partial grading")
    return checked


def ingest(pack: Path, phase: str) -> dict:
    from . import semantic_budget as money
    config = verify(pack)
    money.check_aggregate(pack, phase)
    records = read(pack / phase / "records.json")
    outputs = {p: read_outputs(pack / phase / p / "results.jsonl", records, config["models"][p], p)
               for p in sg.PROVIDERS}
    write(pack / phase / "validated.json", outputs)
    return outputs


def watch(pack: Path) -> None:
    from .semantic_audit import analyze_full, analyze_screen
    for phase in ("screen", "full"):
        while not all((pack / phase / p / "results.jsonl").exists() for p in sg.PROVIDERS):
            wave = submit(pack, phase)
            if wave is None:
                print("Stopped within the spending cap. Full regrade incomplete; see budget-stop.json.", flush=True)
                return
            deadline = time.monotonic() + 26 * 60 * 60
            while not collect(pack, phase):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Resume the same command to collect existing jobs")
                time.sleep(POLL_SECONDS)
        report = analyze_screen(pack) if phase == "screen" else analyze_full(pack)
        if phase == "screen" and not report["passed"]:
            print("Adversarial screen failed. Full grading was not submitted. See screen/analysis.json.", flush=True)
            return
    print("Automated review complete. See full/analysis.json for validation status and full/score-bounds.json for conditional ranges. No official publication performed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, type=Path)
    parser.add_argument("command", choices=["verify", "watch", "analyze"])
    args = parser.parse_args()
    pack = args.pack.resolve()
    if args.command == "verify":
        verify(pack)
        print(json.dumps(read(pack / "budget.json"), indent=2))
    elif args.command == "watch":
        watch(pack)
    else:
        from .semantic_audit import analyze_full, analyze_screen
        report = analyze_screen(pack)
        if report["passed"] and all((pack / "full" / p / "results.jsonl").exists() for p in sg.PROVIDERS):
            report = analyze_full(pack)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
