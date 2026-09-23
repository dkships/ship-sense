"""Reserve batch token charges before submission; never infer missing usage is free."""
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import fcntl
import json
from pathlib import Path

from . import semantic_batch as sb, semantic_grade as sg

AUTHORITY = Path("notes/semantic-spend-authorization.json")
HARD_CAP = 100_000_000
SUBMISSION_CAP = 90_000_000
OVERHEAD_TOKENS = 2048
MIN_WAVE_RECORDS = 32


def micros(value):
    if isinstance(value, bool):
        raise ValueError("Invalid dollar amount")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid dollar amount")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def check_caps(config):
    try:
        cap = micros(config["total_cap_usd"])
        submit = micros(config.get("submission_cap_usd", config["total_cap_usd"]))
        screen = micros(config["screen_cap_usd"])
        if not 0 < screen <= submit <= SUBMISSION_CAP or not submit <= cap <= HARD_CAP:
            raise ValueError()
    except (ValueError, KeyError):
        raise ValueError("Invalid spending cap; absolute $100 cap and $90 submission cap apply") from None


def authorize(pack):
    authority = sb.read(sb.ROOT / AUTHORITY)
    if ((sb.ROOT / authority["pack_path"]).resolve() != pack.resolve()
            or authority["seal_sha256"] != sb.sha(pack / "seal.json")):
        raise ValueError("This is not the authorized pack for the shared $100 budget")


@contextmanager
def lock():
    path = (sb.ROOT / AUTHORITY).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A budgeted submission or collection is already running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def request_bound(request, provider, config):
    tokens = len(json.dumps(request, ensure_ascii=False).encode()) + OVERHEAD_TOKENS
    incoming, outgoing = map(lambda v: Decimal(str(v)), config["batch_rate_bounds"][provider])
    if any(not v.is_finite() or v <= 0 for v in (incoming, outgoing)):
        raise ValueError("Invalid token price bound")
    if type(config["max_output_tokens"]) is not int or config["max_output_tokens"] <= 0:
        raise ValueError("Invalid output token limit")
    cost = tokens * incoming + config["max_output_tokens"] * outgoing
    return tokens, int(cost.to_integral_value(rounding=ROUND_CEILING))


def token_counts(provider, envelope):
    if provider == "openai":
        usage = envelope["response"]["body"].get("usage", {})
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        total = usage.get("total_tokens")
    elif provider == "anthropic":
        usage = envelope["result"]["message"].get("usage", {})
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        total = None
        if any(usage.get(key, 0) != 0 for key in ("cache_creation_input_tokens", "cache_read_input_tokens")):
            raise ValueError("Unexpected cache usage; retain the full reservation")
    else:
        usage = envelope["response"].get("usageMetadata", envelope["response"].get("usage_metadata", {}))
        incoming = usage.get("promptTokenCount", usage.get("prompt_token_count"))
        visible = usage.get("candidatesTokenCount", usage.get("candidates_token_count"))
        thinking = usage.get("thoughtsTokenCount", usage.get("thoughts_token_count"))
        total = usage.get("totalTokenCount", usage.get("total_token_count"))
        if thinking is None and total == (incoming or 0) + (visible or 0):
            thinking = 0
        if any(type(v) is not int or v < 0 for v in (visible, thinking)):
            raise ValueError("Missing or invalid thinking usage; retain the full reservation")
        outgoing = visible + thinking
    if any(type(v) is not int or v < 0 for v in (incoming, outgoing)) or incoming == 0:
        raise ValueError("Missing or invalid token usage; retain the full reservation")
    if total is not None and (type(total) is not int or total != incoming + outgoing):
        raise ValueError("Inconsistent total usage; retain the full reservation")
    return incoming, outgoing


def _requests(pack, phase, provider):
    return [sg.parse_response(line) for line in (pack / phase / provider / "requests.jsonl").read_text().splitlines()]


def wave_inputs(pack, wave, reservation):
    phase, start, stop = (reservation[k] for k in ("phase", "start", "stop"))
    expected_seal = sb.sha(pack / "seal.json")
    if reservation["seal_sha256"] != expected_seal:
        raise ValueError("Reservation belongs to a different sealed pack")
    records = sb.read(pack / phase / "records.json")[start:stop]
    requests = {p: _requests(pack, phase, p)[start:stop] for p in sg.PROVIDERS}
    for provider in sg.PROVIDERS:
        folder = wave / provider
        path = folder / "requests.jsonl"
        expected = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests[provider])
        manifest = sb.read(pack / phase / provider / "manifest.json")
        manifest.update(requests_file=str(path.relative_to(sb.ROOT)), requests=len(records))
        if path.read_text() != expected or sb.read(folder / "manifest.json") != manifest:
            raise ValueError("Wave requests or manifest differ from the sealed roster")
    return records, requests


def _charge(pack, wave, reservation, config):
    records, requests = wave_inputs(pack, wave, reservation)
    charges, reconciled = {}, {}
    for provider in sg.PROVIDERS:
        bounds = {r["id"]: request_bound(q, provider, config) for r, q in zip(records, requests[provider], strict=True)}
        charges[provider] = sum(v[1] for v in bounds.values())
        reconciled[provider] = False
        path = wave / provider / "results.jsonl"
        if not path.exists():
            continue
        folder = path.parent
        job = folder / (sb.SDK_NAMES[provider] + "-batch.json")
        status = folder / (sb.SDK_NAMES[provider] + "-status.json")
        marker = folder / "submission-attempt.json"
        if not job.exists() or not marker.exists() or not status.exists():
            raise ValueError("Results lack a recorded submission and terminal status")
        if not sb._success(provider, sb.read(status)):
            raise ValueError("Results do not come from a successfully completed batch")
        if sb.read(marker)["request_sha256"] != sb.sha(folder / "requests.jsonl"):
            raise ValueError("Submission marker refers to different requests")
        # Duplicate, missing, truncated, or substituted responses cannot free funds.
        sb.read_outputs(path, records, config["models"][provider], provider)
        cost = Decimal(0)
        try:
            for line in path.read_text().splitlines():
                envelope = sg.parse_response(line)
                rid = envelope.get("custom_id", envelope.get("key"))
                incoming, outgoing = token_counts(provider, envelope)
                if incoming > bounds[rid][0] or outgoing > config["max_output_tokens"]:
                    raise RuntimeError("Provider usage exceeds its reserved token bound; stop spending")
                rate_in, rate_out = map(lambda v: Decimal(str(v)), config["batch_rate_bounds"][provider])
                cost += incoming * rate_in + outgoing * rate_out
        except ValueError:
            continue
        charges[provider] = int(cost.to_integral_value(rounding=ROUND_CEILING))
        reconciled[provider] = True
    return charges, reconciled


def account(pack):
    authorize(pack)
    config = sb.read(pack / "config.json")
    check_caps(config)
    used, phase_used, ends, waves = 0, {"screen": 0, "full": 0}, {"screen": 0, "full": 0}, []
    for index, wave in enumerate(sorted((pack / "waves").glob("*"))):
        if wave.name != f"{index:04d}" or not wave.is_dir() or wave.is_symlink():
            raise ValueError("Unexpected budget reservation path")
        reservation = sb.read(wave / "reservation.json")
        phase = reservation["phase"]
        if phase not in ends:
            raise ValueError("Unknown reserved phase")
        start, stop = reservation["start"], reservation["stop"]
        size = len(sb.read(pack / phase / "records.json"))
        if type(start) is not int or type(stop) is not int or start != ends[phase] or not start < stop <= size:
            raise ValueError("Overlapping, missing, or invalid reserved records")
        charges, reconciled = _charge(pack, wave, reservation, config)
        charge = sum(charges.values())
        used += charge
        phase_used[phase] += charge
        ends[phase] = stop
        waves.append({"path": wave.name, "phase": phase, "start": start, "stop": stop,
            "charged_or_reserved_microusd": charges, "usage_reconciled": reconciled})
    if used > micros(config["submission_cap_usd"]) or phase_used["screen"] > micros(config["screen_cap_usd"]):
        raise ValueError("Reserved or reconciled spending exceeds cap")
    return {"charged_or_reserved_microusd": used, "phase_microusd": phase_used,
        "remaining_microusd": micros(config["submission_cap_usd"]) - used,
        "ends": ends, "waves": waves, "absolute_cap_usd": 100,
        "submission_cap_usd": config["submission_cap_usd"], "invoice_reconciled": False}


def check_aggregate(pack, phase):
    state = account(pack)
    if state["ends"][phase] != len(sb.read(pack / phase / "records.json")):
        raise ValueError("Phase is incomplete; no partial score calculation")
    waves = [pack / "waves" / w["path"] for w in state["waves"] if w["phase"] == phase]
    for provider in sg.PROVIDERS:
        combined = "".join((w / provider / "results.jsonl").read_text().rstrip() + "\n" for w in waves)
        if (pack / phase / provider / "results.jsonl").read_text() != combined:
            raise ValueError("Aggregate differs from the original wave results")


def reserve(pack, phase):
    state = account(pack)
    config = sb.read(pack / "config.json")
    records = sb.read(pack / phase / "records.json")
    start = state["ends"][phase]
    remaining = state["remaining_microusd"]
    if phase == "screen":
        remaining = min(remaining, micros(config["screen_cap_usd"]) - state["phase_microusd"][phase])
    requests = {p: _requests(pack, phase, p) for p in sg.PROVIDERS}
    stop, total = start, 0
    for index in range(start, len(records)):
        bound = sum(request_bound(requests[p][index], p, config)[1] for p in sg.PROVIDERS)
        if total + bound > remaining:
            break
        total += bound
        stop = index + 1
    if stop == start or (stop < len(records) and stop - start < MIN_WAVE_RECORDS):
        sb.write(pack / "budget-stop.json", {**state, "status": "stopped_at_budget_cap",
            "phase": phase, "planned_records": len(records), "reserved_records": start,
            "full_regrade_complete": False, "human_review_required": False,
            "reason": "Remaining allowance cannot reserve the next wave; no paid retry or cap expansion."})
        return None
    wave = pack / "waves" / f"{len(state['waves']):04d}"
    wave.mkdir(parents=True, exist_ok=False)
    sb.write(wave / "reservation.json", {"phase": phase, "start": start, "stop": stop,
        "seal_sha256": sb.sha(pack / "seal.json")})
    for provider in sg.PROVIDERS:
        folder = wave / provider
        folder.mkdir()
        path = folder / "requests.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests[provider][start:stop]))
        manifest = sb.read(pack / phase / provider / "manifest.json")
        manifest.update(requests_file=str(path.relative_to(sb.ROOT)), requests=stop-start)
        sb.write(folder / "manifest.json", manifest)
    sb.write(pack / "spend-status.json", account(pack))
    return wave
