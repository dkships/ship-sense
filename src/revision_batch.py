"""Resume the sealed revision through native batches under the cumulative cap."""
import argparse
from datetime import date
from pathlib import Path
import time

from . import semantic_batch as sb, semantic_budget as money, semantic_grade as sg
from . import revision_budget as budget


def submit(pack, phase):
    config = budget.verify(pack)
    if phase not in {"screen", "full"}:
        raise ValueError("Unknown phase")
    if phase == "full":
        from . import revision_audit
        if not revision_audit.analyze_screen(pack)["passed"]:
            raise ValueError("Full regrade blocked by revised screening")
    with money.lock():
        state = budget.account(pack, config)
        wave = None
        if state["waves"]:
            last = state["waves"][-1]
            path = pack / "waves" / last["path"]
            if not all((path / p / "results.jsonl").exists() for p in sg.PROVIDERS):
                if last["phase"] != phase:
                    raise ValueError("Previous phase has uncollected results")
                wave = path
        if wave and all((wave / p / (sb.SDK_NAMES[p] + "-batch.json")).exists() for p in sg.PROVIDERS):
            return wave
        if state["ends"][phase] == len(sb.read(pack / phase / "records.json")) and wave is None:
            return None
        if date.today().isoformat() > config["submit_before"]:
            raise ValueError("Pricing verification expired; collect existing jobs only")
        sb._credentials()
        if wave is None:
            wave = budget.reserve(pack, phase, config, state)
        if wave is None:
            return None
        clients = {p: sb._client(p) for p in sg.PROVIDERS}
        for provider in sg.PROVIDERS:
            folder = wave / provider
            if (folder / (sb.SDK_NAMES[provider] + "-batch.json")).exists():
                continue
            budget.exclusive_write(folder / "submission-attempt.json", {
                "state": "attempted_reconcile_if_no_job", "request_sha256": sb.sha(folder / "requests.jsonl")})
            getattr(sb.batch, "submit_" + sb.SDK_NAMES[provider])(folder / "manifest.json", client=clients[provider])
            print(phase, wave.name, provider, "native batch submitted", flush=True)
        return wave


def collect_once(pack, phase):
    config = budget.verify(pack)
    with money.lock():
        waves, errors, failed = [], [], []
        for wave in sorted((pack / "waves").glob("*")):
            reservation, _, _ = budget.wave_inputs(pack, wave)
            if reservation["phase"] != phase:
                continue
            waves.append(wave)
            for provider in sg.PROVIDERS:
                folder = wave / provider
                job = folder / (sb.SDK_NAMES[provider] + "-batch.json")
                if not job.exists():
                    continue
                if (folder / "results.jsonl").exists():
                    continue
                try:
                    client = sb._client(provider)
                    state = getattr(sb.batch, "status_" + sb.SDK_NAMES[provider])(job, client=client)
                    status = state.get("status", state.get("processing_status", state.get("state")))
                    print(phase, wave.name, provider, str(status), flush=True)
                    if sb._success(provider, state):
                        partial = folder / "results.partial.jsonl"
                        getattr(sb.batch, "download_" + sb.SDK_NAMES[provider])(job, output=partial, client=client)
                        if partial.exists():
                            partial.replace(folder / "results.jsonl")
                        if (folder / "errors.jsonl").exists():
                            failed.append({"wave": wave.name, "provider": provider, "status": "error_results"})
                    elif str(status) in sb.batch.GEMINI_TERMINAL_STATES | {"failed", "expired", "cancelled"}:
                        failed.append({"wave": wave.name, "provider": provider, "status": str(status)})
                except Exception as exc:
                    errors.append({"wave": wave.name, "provider": provider, "error_type": type(exc).__name__})
        # Collection of all providers precedes any billing or grading parser.
        try:
            state = budget.account(pack, config)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            report = {"phase": phase, "collection_complete": False, "collection_errors": errors,
                "failed_jobs": failed, "billing_error": str(exc), "new_submissions": 0, "paid_retries": 0}
            sb.write(pack / "collection" / (phase + ".json"), report)
            return report
        sb.write(pack / "spend-status.json", state)
        ready = bool(waves) and all((w / p / "results.jsonl").exists() for w in waves for p in sg.PROVIDERS)
        ready = ready and state["ends"][phase] == len(sb.read(pack / phase / "records.json"))
        if ready:
            for provider in sg.PROVIDERS:
                target = pack / phase / provider / "results.jsonl"
                combined = "".join((w / provider / "results.jsonl").read_text().rstrip() + "\n" for w in waves)
                if target.exists() and target.read_text() != combined:
                    raise ValueError("Aggregated output changed")
                if not target.exists():
                    partial = target.with_name("results.partial.jsonl")
                    partial.write_text(combined)
                    partial.replace(target)
        report = {"phase": phase, "collection_complete": ready, "collection_errors": errors,
                  "failed_jobs": failed, "new_submissions": 0, "paid_retries": 0}
        sb.write(pack / "collection" / (phase + ".json"), report)
        return report


def check_aggregate(pack, phase):
    config = budget.verify(pack)
    state = budget.account(pack, config)
    if state["ends"][phase] != len(sb.read(pack / phase / "records.json")):
        raise ValueError("No partial phase analysis")
    waves = [pack / "waves" / w["path"] for w in state["waves"] if w["phase"] == phase]
    for provider in sg.PROVIDERS:
        expected = "".join((w / provider / "results.jsonl").read_text().rstrip() + "\n" for w in waves)
        if (pack / phase / provider / "results.jsonl").read_text() != expected:
            raise ValueError("Aggregate differs from native wave outputs")
    return config


def watch(pack):
    from . import revision_audit
    for phase in ("screen", "full"):
        deadline = time.monotonic() + 26 * 60 * 60
        while True:
            report = collect_once(pack, phase)
            if report.get("billing_error"):
                raise RuntimeError("Billing verification stopped new spending; see the saved collection report")
            if report["failed_jobs"]:
                raise RuntimeError("Native batch failed; no automatic paid retry")
            if report["collection_complete"]:
                break
            wave = submit(pack, phase)
            if wave is None:
                print("Stopped within cumulative budget; no partial regrade published.", flush=True)
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Resume the same command; existing submissions are retained")
            time.sleep(sb.POLL_SECONDS)
        result = revision_audit.analyze_screen(pack) if phase == "screen" else revision_audit.analyze_full(pack)
        if not result["passed"]:
            print(phase, "validation failed; no further inference submitted.", flush=True)
            return
    print("Saved conditional score ranges; no official publication or ranking.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--phase", choices=["screen", "full"], default="screen")
    parser.add_argument("command", choices=["verify", "watch", "collect", "analyze"])
    args = parser.parse_args()
    pack = args.pack.resolve()
    if args.command == "verify":
        config = budget.verify(pack)
        print(budget.account(pack, config))
    elif args.command == "watch":
        watch(pack)
    elif args.command == "collect":
        collect_once(pack, args.phase)
    else:
        from . import revision_audit
        print(revision_audit.analyze_screen(pack) if args.phase == "screen" else revision_audit.analyze_full(pack))


if __name__ == "__main__":
    main()
