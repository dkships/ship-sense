import json

import pytest

from test_revision import pack, complete
from src import revision_budget as budget, revision_recovery as recovery
from src import semantic_batch as sb, semantic_grade as sg


@pytest.fixture
def failed_batch(pack):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    complete(folder, wave)
    path = wave / "anthropic/results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0] = {"custom_id": rows[0]["custom_id"], "result": {"type": "errored", "error": {
        "type": "error", "error": {"type": "invalid_request_error",
            "message": "The compiled grammar is too large"}}}}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return folder, config, wave


def test_native_error_keeps_reservation(failed_batch, monkeypatch):
    folder, config, wave = failed_batch
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("Offline inspection"))
    with pytest.raises(ValueError, match="Result roster or model"):
        budget.account(folder, config)
    report = recovery.inspect(folder, "screen")
    assert report["collection_complete"]
    assert report["billing_error"] == "Result roster or model differs from reserved inference"
    assert report["new_submissions"] == report["paid_retries"] == 0
    assert report["budget_allowance_released"] is False
    assert report["reserved_bound_microusd"] > config["max_output_tokens"]
    row = next(j for j in report["jobs"] if j["provider"] == "anthropic")
    assert row["native_errors"] == row["usage_unavailable"] == 1
    assert row["native_error_types"] == {"invalid_request_error": 1}
    assert not (folder / "screen/analysis.json").exists()


def test_watch_collects_after_billing_error(failed_batch, monkeypatch):
    folder, _, wave = failed_batch
    path = wave / "google/results.jsonl"
    saved = path.read_text()
    path.unlink()
    frozen = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    journal = (sb.ROOT / budget.JOURNAL).read_bytes()
    states = iter(["JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"])
    calls = []
    monkeypatch.setattr(sb, "_client", lambda p: p)
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    def status(job, client):
        assert client == "google"
        state = {"state": next(states)}
        sb.write(job.with_name("gemini-status.json"), state)
        calls.append(client)
        return state
    monkeypatch.setattr(sb.batch, "status_gemini", status)
    monkeypatch.setattr(sb.batch, "download_gemini", lambda job, output, client: output.write_text(saved))
    for provider in sg.PROVIDERS:
        monkeypatch.setattr(sb.batch, "submit_" + sb.SDK_NAMES[provider], lambda *a, **kw: pytest.fail("No submissions"))
    report = recovery.watch(folder, "screen")
    assert report["collection_complete"] and report["billing_error"]
    assert calls == ["google", "google"]
    assert path.read_text() == saved
    assert (sb.ROOT / budget.JOURNAL).read_bytes() == journal
    assert all(p.read_bytes() == blob for p, blob in frozen.items())
    assert not (folder / "screen/analysis.json").exists()


@pytest.mark.parametrize("damage", ["journal", "marker", "request", "missing_job"])
def test_bad_provenance_blocks_io(failed_batch, monkeypatch, damage):
    folder, _, wave = failed_batch
    if damage == "journal":
        (sb.ROOT / budget.JOURNAL).write_text("")
    elif damage == "marker":
        sb.write(wave / "google/submission-attempt.json", {"request_sha256": "wrong"})
    elif damage == "request":
        (wave / "google/requests.jsonl").write_text("{}\n")
    else:
        (wave / "google/gemini-batch.json").unlink()
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("Provenance checked first"))
    with pytest.raises(ValueError):
        recovery.collect_once(folder, "screen")


def test_missing_result_blocks_roster(failed_batch):
    folder, _, wave = failed_batch
    path = wave / "anthropic/results.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[1:]) + "\n")
    report = recovery.inspect(folder, "screen")
    row = next(j for j in report["jobs"] if j["provider"] == "anthropic")
    assert row["roster_complete"] is False
    assert report["all_responses_valid"] is False


def test_usage_overrun_is_explicit(failed_batch):
    folder, config, wave = failed_batch
    path = wave / "openai/results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["response"]["body"]["usage"]["output_tokens"] = config["max_output_tokens"] + 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = recovery.inspect(folder, "screen")
    assert report["usage_overruns"] == 1
    assert report["budget_allowance_released"] is False


def test_terminal_failure_stops_polling(failed_batch, monkeypatch):
    folder, _, wave = failed_batch
    (wave / "google/results.jsonl").unlink()
    sb.write(wave / "google/gemini-status.json", {"state": "JOB_STATE_FAILED"})
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("Terminal job"))
    report = recovery.watch(folder, "screen")
    assert report["collection_complete"] and not report["all_responses_valid"]
