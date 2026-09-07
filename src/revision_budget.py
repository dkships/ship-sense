"""One append-only revision of the existing cumulative $100 authorization."""
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path

from . import semantic_batch as sb, semantic_budget as money, semantic_collect as collect
from . import semantic_grade as sg

REGISTRY = Path("notes/semantic-revision-authorization.json")
JOURNAL = Path("notes/semantic-revision-reservations.jsonl")


def exclusive_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def base_reservation(base):
    """Require the immutable failed base to be incapable of further submissions."""
    config, waves = collect.saved_waves(base, "screen")
    report = collect.inspect(base, "screen")
    if not report["collection_complete"] or not report["phase_roster_complete"] or report["all_responses_valid"]:
        raise ValueError("Revision requires a complete base with invalid responses blocking its frozen runner")
    if len(waves) != len(list((base / "waves").iterdir())):
        raise ValueError("Unexpected base spending outside its failed screen")
    total = 0
    for wave, _ in waves:
        _, requests = money.wave_inputs(base, wave, sb.read(wave / "reservation.json"))
        total += sum(money.request_bound(q, p, config)[1] for p in sg.PROVIDERS for q in requests[p])
    return total


def verify_inputs(pack):
    seal = sb.read(pack / "seal.json")
    if pack.resolve() != (sb.ROOT / seal["pack_path"]).resolve():
        raise ValueError("Revision pack location changed")
    for relative, digest in seal["files"].items():
        path = sb.ROOT / relative
        if not path.resolve().is_relative_to(sb.ROOT.resolve()) or path.is_symlink() or sb.sha(path) != digest:
            raise ValueError("Sealed revision input changed: " + relative)
    base = sb.ROOT / seal["base_pack"]
    actual = {str(p.relative_to(sb.ROOT)) for p in (base / "waves").rglob("*") if p.is_file()}
    if actual != set(seal["base_wave_files"]):
        raise ValueError("Base reservation or result roster changed")
    if base_reservation(base) != seal["base_reserved_microusd"]:
        raise ValueError("Base budget changed")
    config = sb.read(pack / "config.json")
    money.check_caps(config)
    return config


def bind(pack):
    config = verify_inputs(pack)
    seal = sb.read(pack / "seal.json")
    expected = {"pack_path": str(pack.relative_to(sb.ROOT)), "seal_sha256": sb.sha(pack / "seal.json"),
        "original_authority_sha256": sb.sha(sb.ROOT / money.AUTHORITY),
        "base_reserved_microusd": seal["base_reserved_microusd"],
        "shared_submission_cap_microusd": money.micros(config["submission_cap_usd"]),
        "absolute_cap_microusd": money.HARD_CAP, "budget_reset": False}
    path = sb.ROOT / REGISTRY
    if path.exists():
        if sb.read(path) != expected:
            raise ValueError("A different revision is already bound to this cumulative budget")
        return
    if (sb.ROOT / JOURNAL).exists():
        raise ValueError("Existing reservation journal must not be reset")
    exclusive_write(path, expected)


def verify(pack):
    config = verify_inputs(pack)
    binding = sb.read(sb.ROOT / REGISTRY)
    if (binding["pack_path"] != str(pack.relative_to(sb.ROOT))
            or binding["seal_sha256"] != sb.sha(pack / "seal.json")
            or binding["original_authority_sha256"] != sb.sha(sb.ROOT / money.AUTHORITY)
            or binding["base_reserved_microusd"] != sb.read(pack / "seal.json")["base_reserved_microusd"]
            or binding["shared_submission_cap_microusd"] != money.micros(config["submission_cap_usd"])
            or binding["absolute_cap_microusd"] != money.HARD_CAP or binding["budget_reset"] is not False):
        raise ValueError("Revision differs from cumulative budget binding")
    return config


def wave_inputs(pack, wave):
    reservation = sb.read(wave / "reservation.json")
    phase, start, stop = (reservation[k] for k in ("phase", "start", "stop"))
    if phase not in {"screen", "full"} or reservation["seal_sha256"] != sb.sha(pack / "seal.json"):
        raise ValueError("Reservation belongs to a different revision or phase")
    records = sb.read(pack / phase / "records.json")
    if type(start) is not int or type(stop) is not int or not 0 <= start < stop <= len(records):
        raise ValueError("Invalid reservation interval")
    requests = {}
    for provider in sg.PROVIDERS:
        requests[provider] = money._requests(pack, phase, provider)[start:stop]
        path = wave / provider / "requests.jsonl"
        expected = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests[provider])
        manifest = sb.read(pack / phase / provider / "manifest.json")
        manifest.update(requests_file=str(path.relative_to(sb.ROOT)), requests=stop-start)
        if path.read_text() != expected or sb.read(path.with_name("manifest.json")) != manifest:
            raise ValueError("Reserved requests or manifest changed")
    return reservation, records[start:stop], requests


def usage_charge(folder, records, requests, provider, config):
    bounds = {r["id"]: money.request_bound(q, provider, config)
              for r, q in zip(records, requests, strict=True)}
    held = sum(v[1] for v in bounds.values())
    job = folder / (sb.SDK_NAMES[provider] + "-batch.json")
    marker = folder / "submission-attempt.json"
    if marker.exists() and (not job.exists() or sb.read(marker)["request_sha256"] != sb.sha(folder / "requests.jsonl")):
        raise ValueError("Unresolved submission attempt; do not retry")
    if job.exists() and not marker.exists():
        raise ValueError("Saved job lacks its submission attempt")
    path = folder / "results.jsonl"
    if not path.exists():
        return held, False
    if not job.exists() or not marker.exists():
        raise ValueError("Results lack a reserved submission")
    status = sb.read(folder / (sb.SDK_NAMES[provider] + "-status.json"))
    if not sb._success(provider, status) or (folder / "errors.jsonl").exists():
        return held, False
    rates = [Decimal(str(v)) for v in config["batch_rate_bounds"][provider]]
    seen, cost, complete = set(), Decimal(0), True
    for line in path.read_text().splitlines():
        envelope = sg.parse_response(line)
        rid, result = sb.batch._result_from_line(provider, {"id": config["models"][provider]}, envelope)
        if rid not in bounds or rid in seen or result.error or result.model != config["models"][provider]:
            raise ValueError("Result roster or model differs from reserved inference")
        seen.add(rid)
        if provider == "google":
            body = envelope.get("response", {})
            version = body.get("modelVersion", body.get("model_version"))
            if version and not (version == result.model or version.startswith(result.model + "-")):
                raise ValueError("Unexpected billed model version")
        try:
            incoming, outgoing = money.token_counts(provider, envelope)
        except ValueError:
            complete = False
            continue
        if incoming > bounds[rid][0] or outgoing > config["max_output_tokens"]:
            raise ValueError("Usage exceeds reservation; stop spending")
        cost += incoming * rates[0] + outgoing * rates[1]
    if seen != set(bounds):
        raise ValueError("Missing billing records; stop spending")
    # Billing evidence is independent of valid grading JSON. No grade is repaired.
    return (int(cost.to_integral_value(rounding=ROUND_CEILING)), True) if complete else (held, False)


def account(pack, config):
    base = sb.read(pack / "seal.json")["base_reserved_microusd"]
    used, ends, waves = base, {"screen": 0, "full": 0}, []
    phase_used = {"screen": 0, "full": 0}
    path = sb.ROOT / JOURNAL
    journal = [sg.parse_response(line) for line in path.read_text().splitlines()] if path.exists() else []
    for index, wave in enumerate(sorted((pack / "waves").glob("*"))):
        if wave.name != f"{index:04d}" or not wave.is_dir() or wave.is_symlink():
            raise ValueError("Unexpected revision wave path")
        reservation, records, requests = wave_inputs(pack, wave)
        event = {"pack_path": str(pack.relative_to(sb.ROOT)), "wave": wave.name, **reservation,
                 "request_sha256": {p: sb.sha(wave / p / "requests.jsonl") for p in sg.PROVIDERS}}
        if index >= len(journal) or journal[index] != event:
            raise ValueError("Reservation differs from the append-only journal")
        phase = reservation["phase"]
        if reservation["start"] != ends[phase]:
            raise ValueError("Overlapping or missing reservations")
        charges, reconciled = {}, {}
        for provider in sg.PROVIDERS:
            charges[provider], reconciled[provider] = usage_charge(wave / provider, records, requests[provider], provider, config)
        charge = sum(charges.values())
        used += charge
        phase_used[phase] += charge
        ends[phase] = reservation["stop"]
        waves.append({"path": wave.name, **reservation, "charges_microusd": charges, "usage_reconciled": reconciled})
    if len(waves) != len(journal):
        raise ValueError("A reserved wave is missing; spending cannot reset")
    if used > money.micros(config["submission_cap_usd"]) or phase_used["screen"] > money.micros(config["screen_cap_usd"]):
        raise ValueError("Cumulative spending cap exceeded")
    return {"base_held_microusd": base, "charged_or_reserved_microusd": used,
        "remaining_microusd": money.micros(config["submission_cap_usd"]) - used,
        "phase_microusd": phase_used, "ends": ends, "waves": waves, "invoice_reconciled": False}


def reserve(pack, phase, config, state):
    records = sb.read(pack / phase / "records.json")
    requests = {p: money._requests(pack, phase, p) for p in sg.PROVIDERS}
    start, total = state["ends"][phase], 0
    remaining = state["remaining_microusd"]
    if phase == "screen":
        remaining = min(remaining, money.micros(config["screen_cap_usd"]) - state["phase_microusd"][phase])
    stop = start
    for index in range(start, len(records)):
        amount = sum(money.request_bound(requests[p][index], p, config)[1] for p in sg.PROVIDERS)
        if total + amount > remaining:
            break
        total += amount
        stop += 1
    if stop == start or (stop < len(records) and stop-start < money.MIN_WAVE_RECORDS):
        sb.write(pack / "budget-stop.json", {**state, "phase": phase, "full_regrade_complete": False})
        return None
    wave = pack / "waves" / f"{len(state['waves']):04d}"
    wave.mkdir(parents=True, exist_ok=False)
    exclusive_write(wave / "reservation.json", {"phase": phase, "start": start, "stop": stop,
                    "seal_sha256": sb.sha(pack / "seal.json")})
    for provider in sg.PROVIDERS:
        path = wave / provider / "requests.jsonl"
        path.parent.mkdir()
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests[provider][start:stop]))
        manifest = sb.read(pack / phase / provider / "manifest.json")
        manifest.update(requests_file=str(path.relative_to(sb.ROOT)), requests=stop-start)
        exclusive_write(path.with_name("manifest.json"), manifest)
    event = {"pack_path": str(pack.relative_to(sb.ROOT)), "wave": wave.name,
             **sb.read(wave / "reservation.json"),
             "request_sha256": {p: sb.sha(wave / p / "requests.jsonl") for p in sg.PROVIDERS}}
    journal_path = sb.ROOT / JOURNAL
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a") as handle:
        handle.write(json.dumps(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return wave
