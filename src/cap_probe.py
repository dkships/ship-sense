"""Check each raised output cap with one tiny request before any scored answer (output-cap
rule 6; NEXT_VERSION section 12e). A value the vendor rejects leaves that model at its old
cap: `revert` writes the old value back into models.yaml, and both the probe and the
revert are logged in the run's policy.json.

    raised caps = models.yaml now against models.yaml at a base commit (before the raises)

    python -m src.cap_probe list   --base-ref REF                      (no call)
    ./scripts/with_env.sh .venv/bin/python -m src.cap_probe probe --base-ref REF --run-id RUN
    python -m src.cap_probe revert --run-id RUN                         (no call)

The probe asks for one word, so a model answers in a few tokens; the upper bound printed
assumes the worst case, a model that spends its whole new cap thinking.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

from . import loader, providers, run

PROBE_TEXT = "Reply with the single word OK."
PROBE_FILE = "cap_probe.json"
POLICY_FILE = "policy.json"
REJECTED, ACCEPTED, ERROR = "rejected", "accepted", "error"
# A request the vendor refuses as invalid (HTTP 400 or 422) is a rejection of the value;
# anything else (timeouts, 5xx, rate limits) says nothing about the cap and is retried.
REJECTION_STATUS = frozenset({400, 422})
ERROR_TEXT_LIMIT = 300


def _caps(registry: dict) -> dict[str, int]:
    defaults = registry.get("defaults", {})
    return {m["name"]: run.output_cap(m, defaults) for m in registry.get("models", [])}


def raised(base: dict, now: dict) -> list[dict]:
    """Every model whose cap is higher now than in the base registry."""
    before, after = _caps(base), _caps(now)
    return [{"model": name, "old": before[name], "new": cap}
            for name, cap in sorted(after.items()) if name in before and cap > before[name]]


def base_registry(ref: str) -> dict:
    text = subprocess.run(["git", "show", f"{ref}:models.yaml"], cwd=loader.ROOT, check=True,
                          capture_output=True, text=True).stdout
    return yaml.safe_load(text)


def current_registry() -> dict:
    return yaml.safe_load(loader.MODELS_FILE.read_text())


def upper_usd(rows: list[dict], registry: dict) -> float:
    by_name = {m["name"]: m for m in registry.get("models", [])}
    return sum(r["new"] * float(by_name[r["model"]].get("price_out") or 0) / 1e6 for r in rows)


def _status(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def probe_one(cfg: dict, cap: int, provider_for=providers.get_provider) -> dict:
    try:
        result = provider_for(cfg).chat_result([{"role": "user", "content": PROBE_TEXT}],
                                               json_mode=False, max_tokens=cap)
    except Exception as exc:  # the outcome is the record; nothing is raised past here
        outcome = REJECTED if _status(exc) in REJECTION_STATUS else ERROR
        return {"outcome": outcome, "status": _status(exc),
                "error": f"{type(exc).__name__}: {str(exc)[:ERROR_TEXT_LIMIT]}"}
    return {"outcome": ACCEPTED, "finish_reason": result.finish_reason, "usage": result.usage,
            "cost_usd": result.cost_usd}


def probe(rows: list[dict], run_dir: Path, provider_for=providers.get_provider,
          now=lambda: dt.datetime.now(dt.timezone.utc)) -> dict:
    by_name = {m["name"]: m for m in current_registry()["models"]}
    results = []
    for row in rows:
        results.append({**row, **probe_one(by_name[row["model"]], row["new"], provider_for),
                        "at": now().isoformat()})
    record = {"probe": PROBE_TEXT, "results": results}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / PROBE_FILE).write_text(json.dumps(record, indent=1, default=str))
    _log(run_dir, {"probed": len(results),
                   "rejected": [r["model"] for r in results if r["outcome"] == REJECTED],
                   "errors": [r["model"] for r in results if r["outcome"] == ERROR]})
    return record


def _log(run_dir: Path, entry: dict) -> None:
    path = run_dir / POLICY_FILE
    policy = json.loads(path.read_text()) if path.exists() else {}
    policy.setdefault("cap_probe", {}).update(entry)
    path.write_text(json.dumps(policy, indent=1))


def revert_text(text: str, model: str, old: int, new: int, note: str) -> str:
    """models.yaml with one model's raised cap set back to its old value (text-level, so
    every other line and comment stays as it is)."""
    lines = text.split("\n")
    start = lines.index(f"  - name: {model}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("  - name: ")), len(lines))
    for i in range(start, end):
        if re.match(rf"^    max_tokens: {new}\b", lines[i]):
            lines[i] = f"    max_tokens: {old}             # {note}"
            return "\n".join(lines)
    raise ValueError(f"{model} has no max_tokens: {new} line")


def revert(run_dir: Path, models_file: Path = loader.MODELS_FILE) -> list[str]:
    record = json.loads((run_dir / PROBE_FILE).read_text())
    if any(r["outcome"] == ERROR for r in record["results"]):
        raise SystemExit("a probe ended in an error, not a verdict: probe that model again first")
    text, reverted = models_file.read_text(), []
    for r in record["results"]:
        if r["outcome"] != REJECTED:
            continue
        note = (f"rule 6 raise to {r['new']:,} rejected by the vendor on {r['at'][:10]} "
                f"({run_dir.name}/{PROBE_FILE}); stays at its old cap")
        text = revert_text(text, r["model"], r["old"], r["new"], note)
        reverted.append(r["model"])
    models_file.write_text(text)
    _log(run_dir, {"reverted": reverted})
    return reverted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe raised output caps (rule 6).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--base-ref", required=True, help="a commit before the cap raises")
        if name == "probe":
            p.add_argument("--run-id", required=True)
            p.add_argument("--models", nargs="*", help="only these (a re-probe after an error)")
    p = sub.add_parser("revert")
    p.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "revert":
        print("reverted:", revert(loader.ROOT / "outputs" / args.run_id) or "none")
        return 0
    now = current_registry()
    rows = raised(base_registry(args.base_ref), now)
    if args.cmd == "list":
        for r in rows:
            print(f"{r['model']:24s} {r['old']:>7,} -> {r['new']:>7,}")
        print(f"{len(rows)} raised caps; worst-case probe spend ${upper_usd(rows, now):.2f}")
        return 0
    if args.models:
        rows = [r for r in rows if r["model"] in args.models]
    record = probe(rows, loader.ROOT / "outputs" / args.run_id)
    for r in record["results"]:
        print(f"{r['model']:24s} {r['new']:>7,} {r['outcome']}")
    return 0 if all(r["outcome"] == ACCEPTED for r in record["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
