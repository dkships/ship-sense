"""Budget failures must stop before a provider client is created."""
import json

import pytest

from src import semantic_batch as sb, semantic_budget as money, semantic_grade as sg


def test_usage_cannot_release_missing_or_malformed_allowance():
    for usage in [{}, {"input_tokens": 10}, {"input_tokens": -1, "output_tokens": 10},
                  {"input_tokens": True, "output_tokens": 10},
                  {"input_tokens": 10, "output_tokens": 1.5}]:
        with pytest.raises(ValueError, match="usage"):
            money.token_counts("openai", {"response": {"body": {"usage": usage}}})


def test_google_thinking_is_charged_as_output():
    envelope = {"response": {"usageMetadata": {"promptTokenCount": 100,
        "candidatesTokenCount": 25, "thoughtsTokenCount": 75, "totalTokenCount": 200}}}
    assert money.token_counts("google", envelope) == (100, 100)


def test_google_missing_thinking_keeps_maximum_reservation():
    envelope = {"response": {"usageMetadata": {"promptTokenCount": 100,
        "candidatesTokenCount": 25, "totalTokenCount": 200}}}
    with pytest.raises(ValueError, match="usage"):
        money.token_counts("google", envelope)


def test_anthropic_cache_tokens_cannot_disappear():
    envelope = {"result": {"message": {"usage": {"input_tokens": 100,
        "output_tokens": 20, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 10}}}}
    # Unrequested caching is unexpected billing behavior; retain the entire bound.
    with pytest.raises(ValueError, match="usage"):
        money.token_counts("anthropic", envelope)


def test_openai_reasoning_is_not_double_counted():
    envelope = {"response": {"body": {"usage": {"input_tokens": 100,
        "output_tokens": 80, "total_tokens": 180, "output_tokens_details": {"reasoning_tokens": 60}}}}}
    assert money.token_counts("openai", envelope) == (100, 80)


@pytest.mark.parametrize("cap", [100.000001, 600, -1, 0, True])
def test_invalid_cap_cannot_be_configured(cap):
    with pytest.raises(ValueError, match="cap"):
        money.check_caps({"total_cap_usd": cap, "submission_cap_usd": 90, "screen_cap_usd": 50})


def test_submission_headroom_cannot_be_removed():
    with pytest.raises(ValueError, match="cap"):
        money.check_caps({"total_cap_usd": 100, "submission_cap_usd": 100, "screen_cap_usd": 50})


def test_money_always_rounds_up():
    assert money.micros("0.00000001") == 1
    assert money.micros("100") == 100_000_000


def test_only_one_pack_can_use_this_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    pack = tmp_path / "pack"
    sb.write(pack / "seal.json", {})
    sb.write(tmp_path / money.AUTHORITY, {"pack_path": "different", "seal_sha256": sb.sha(pack / "seal.json")})
    with pytest.raises(ValueError, match="authorized pack"):
        money.authorize(pack)


def test_budget_lock_rejects_a_concurrent_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    with money.lock():
        with pytest.raises(RuntimeError, match="already running"):
            with money.lock():
                pytest.fail("Concurrent submission entered the lock")


def test_expensive_legacy_pack_cannot_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    sb.write(tmp_path / "seal.json", {"pack_path": ".", "files": {}})
    sb.write(tmp_path / "config.json", {"total_cap_usd": 600, "screen_cap_usd": 100})
    sb.write(tmp_path / "budget.json", {"total_bound_usd": 541.8821})
    with pytest.raises(ValueError, match="cap"):
        sb.verify(tmp_path)


@pytest.fixture
def pack(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    folder = tmp_path / "pack"
    config = {"total_cap_usd": 100, "submission_cap_usd": 1, "screen_cap_usd": 1,
        "max_output_tokens": 256, "submit_before": "9999-12-31",
        "models": {p: "grader-id" for p in sg.PROVIDERS},
        "batch_rate_bounds": {p: [1, 1] for p in sg.PROVIDERS}}
    records = [{"id": str(i), "payload": {"brief": "Explicit facts", "statements": [], "criteria": []}}
               for i in range(96)]
    sb.write(folder / "config.json", config)
    sb.prepare_phase(folder, "screen", records, config)
    sb.write(folder / "seal.json", {})
    sb.write(tmp_path / money.AUTHORITY, {"pack_path": "pack", "seal_sha256": sb.sha(folder / "seal.json")})
    monkeypatch.setattr(sb, "verify", lambda _: config)
    monkeypatch.setattr(sb, "read_outputs", lambda *args: {})
    return folder


def complete_wave(pack, wave, usage=True):
    reservation = sb.read(wave / "reservation.json")
    records = sb.read(pack / "screen/records.json")[reservation["start"]:reservation["stop"]]
    for provider in sg.PROVIDERS:
        folder = wave / provider
        sb.write(folder / "submission-attempt.json", {"request_sha256": sb.sha(folder / "requests.jsonl")})
        sb.write(folder / (sb.SDK_NAMES[provider] + "-batch.json"), {"id": "FAKE"})
        status = {"openai": {"status": "completed"}, "anthropic": {"processing_status": "ended"},
                  "google": {"state": "JOB_STATE_SUCCEEDED"}}[provider]
        sb.write(folder / (sb.SDK_NAMES[provider] + "-status.json"), status)
        envelopes = []
        for record in records:
            tokens = {"input_tokens": 10, "output_tokens": 10} if usage else {}
            if provider == "openai":
                envelope = {"custom_id": record["id"], "response": {"body": {"usage": tokens}}}
            elif provider == "anthropic":
                envelope = {"custom_id": record["id"], "result": {"message": {"usage": tokens}}}
            else:
                tokens = {"promptTokenCount": 10, "candidatesTokenCount": 10, "thoughtsTokenCount": 0} if usage else {}
                envelope = {"key": record["id"], "response": {"usageMetadata": tokens}}
            envelopes.append(envelope)
        (folder / "results.jsonl").write_text("".join(json.dumps(e) + "\n" for e in envelopes))


def test_pending_reservations_cannot_overspend(pack):
    wave = money.reserve(pack, "screen")
    assert wave is not None
    state = money.account(pack)
    assert state["charged_or_reserved_microusd"] <= 1_000_000
    assert state["ends"]["screen"] < 96
    assert money.reserve(pack, "screen") is None
    assert sb.read(pack / "budget-stop.json")["full_regrade_complete"] is False


def test_complete_usage_frees_only_provably_unused_allowance(pack):
    wave = money.reserve(pack, "screen")
    before = money.account(pack)["charged_or_reserved_microusd"]
    complete_wave(pack, wave)
    after = money.account(pack)
    assert after["charged_or_reserved_microusd"] == after["ends"]["screen"] * 3 * 20
    assert after["charged_or_reserved_microusd"] < before
    second = money.reserve(pack, "screen")
    assert second is not None
    assert money.account(pack)["charged_or_reserved_microusd"] <= 1_000_000


def test_missing_usage_keeps_entire_reservation(pack):
    wave = money.reserve(pack, "screen")
    before = money.account(pack)["charged_or_reserved_microusd"]
    complete_wave(pack, wave, usage=False)
    assert money.account(pack)["charged_or_reserved_microusd"] == before
    assert money.reserve(pack, "screen") is None


def test_overlapping_records_cannot_evade_budget(pack):
    wave = money.reserve(pack, "screen")
    reservation = sb.read(wave / "reservation.json")
    reservation["start"] = -1
    sb.write(wave / "reservation.json", reservation)
    with pytest.raises(ValueError, match="reserved records"):
        money.account(pack)


def test_modified_wave_requests_cannot_change_cost_or_model(pack):
    wave = money.reserve(pack, "screen")
    path = wave / "openai/requests.jsonl"
    path.write_text(path.read_text().replace("grader-id", "expensive-grader"))
    with pytest.raises(ValueError, match="sealed roster"):
        money.account(pack)


def test_timeout_after_create_blocks_all_future_submissions(pack, monkeypatch):
    monkeypatch.setattr(sb, "_credentials", lambda: None)
    monkeypatch.setattr(sb, "_client", lambda p: object())
    def timeout(*args, **kwargs):
        raise TimeoutError("Provider may already have accepted this batch")
    monkeypatch.setattr(sb.batch, "submit_openai", timeout)
    with pytest.raises(TimeoutError):
        sb.submit(pack, "screen")
    charged = money.account(pack)["charged_or_reserved_microusd"]
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("Duplicate provider call"))
    with pytest.raises(RuntimeError, match="Unresolved submission"):
        sb.submit(pack, "screen")
    assert money.account(pack)["charged_or_reserved_microusd"] == charged


def test_successful_resume_never_submits_existing_jobs(pack, monkeypatch):
    wave = money.reserve(pack, "screen")
    for provider in sg.PROVIDERS:
        folder = wave / provider
        sb.write(folder / "submission-attempt.json", {"request_sha256": sb.sha(folder / "requests.jsonl")})
        sb.write(folder / (sb.SDK_NAMES[provider] + "-batch.json"), {"id": "FAKE"})
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("Existing jobs were resubmitted"))
    assert sb.submit(pack, "screen") == wave


def test_results_without_submission_cannot_free_money(pack):
    wave = money.reserve(pack, "screen")
    complete_wave(pack, wave)
    (wave / "google/gemini-batch.json").unlink()
    with pytest.raises(ValueError, match="recorded submission"):
        money.account(pack)
