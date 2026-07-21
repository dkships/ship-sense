"""Verify a run's request settings against saved artifacts.

The shipped-defaults policy (METHODOLOGY "Model settings") says the only
request parameter the harness sets is the output-token ceiling. This script
proves it for a given run directory instead of assuming it:

- Every archived batch request body is scanned for sampler and reasoning
  parameters (temperature, top_p, reasoning, thinking, effort, budgets).
- Traces are tallied per model by run_mode, so a model that quietly mixed
  live and batch calls shows up.

Live-lane request bodies are not archived; their behavior is pinned by
src/providers.py at the run's commit. This script flags which models ran
live so that code review knows where to look.

Usage: python scripts/verify_run_settings.py <run_id> [<run_id> ...]
Exit code 1 if any forbidden parameter is found in any batch request.
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

FORBIDDEN = re.compile(
    r'"(temperature|top_p|top_k|reasoning|reasoning_effort|thinking'
    r'|thinkingConfig|thinking_config|budget_tokens|effort)"'
)
CEILING_KEYS = ("max_tokens", "max_output_tokens", "max_completion_tokens")


def check_run(run_dir: Path) -> bool:
    ok = True
    hits: list[str] = []
    ceilings: collections.Counter = collections.Counter()
    for req_file in sorted(run_dir.glob("batch/*/**/requests.jsonl")):
        model = req_file.relative_to(run_dir / "batch").parts[0]
        for i, line in enumerate(req_file.read_text().splitlines(), 1):
            m = FORBIDDEN.search(line)
            if m:
                hits.append(f"{model} {req_file.name}:{i} sets {m.group(1)}")
                ok = False
            body = json.loads(line)
            for key in CEILING_KEYS:
                val = _find_key(body, key)
                if val is not None:
                    ceilings[(model, key, val)] += 1

    modes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for trace_file in sorted(run_dir.glob("traces/*.json")):
        model = trace_file.name.split("__")[0]
        entries = json.loads(trace_file.read_text())
        for entry in entries if isinstance(entries, list) else [entries]:
            mode = entry.get("run_mode")
            if mode:
                modes[model][mode] += 1

    print(f"== {run_dir.name}")
    if hits:
        print(f"  FAIL: {len(hits)} batch request(s) set forbidden params")
        for h in hits[:20]:
            print(f"    {h}")
    else:
        print("  ok: no sampler/reasoning params in any archived batch request")
    for (model, key, val), n in sorted(ceilings.items()):
        print(f"  {model}: {key}={val} ({n} requests)")
    for model, counter in sorted(modes.items()):
        tag = " <== MIXED MODES" if len(counter) > 1 else ""
        live = " (live: params pinned by src/providers.py at this run's commit)" \
            if "live" in counter else ""
        print(f"  {model}: {dict(counter)}{tag}{live}")
    return ok


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    all_ok = True
    for run_id in sys.argv[1:]:
        run_dir = Path("outputs") / run_id
        if not run_dir.is_dir():
            print(f"== {run_id}: no such run directory")
            all_ok = False
            continue
        all_ok = check_run(run_dir) and all_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
