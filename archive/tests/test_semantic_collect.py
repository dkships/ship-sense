"""Recovery must collect existing batches despite invalid grader responses."""
import json

import pytest

from src import semantic_batch as sb, semantic_collect as collect, semantic_grade as sg


def record():
    return {"id": "one", "payload": {"brief": "An explicit fact", "statements": [],
        "criteria": [{"id": "c1", "kind": "landmine", "reference": "A concern"}]}}


def envelope(text, finish="end_turn"):
    return {"custom_id": "one", "result": {"type": "succeeded", "message": {
        "model": "grader", "stop_reason": finish,
        "content": [{"type": "text", "text": text}], "usage": {}}}}


def test_missing_check_is_reported_without_repair(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(envelope('{"checks": [], "extra_findings": []}')) + "\n")
    original = path.read_bytes()
    report = collect.result_report([path], [record()], "grader", "anthropic")
    assert report["valid_responses"] == 0
    assert report["invalid_responses"] == 1
    assert "coverage" in report["errors"][0]["reason"]
    assert path.read_bytes() == original


def test_truncated_response_cannot_be_repaired_or_retried(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(envelope('{"checks": [', "max_tokens")) + "\n")
    report = collect.result_report([path], [record()], "grader", "anthropic")
    assert report["invalid_responses"] == 1
    assert "max_tokens" in report["errors"][0]["reason"]


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "malformed"])
def test_broken_result_roster_never_passes(tmp_path, kind):
    path = tmp_path / "results.jsonl"
    row = envelope('{"checks": [], "extra_findings": []}')
    lines = [json.dumps(row)]
    if kind == "missing":
        lines = []
    elif kind == "duplicate":
        lines *= 2
    elif kind == "unknown":
        row["custom_id"] = "unknown"
        lines = [json.dumps(row)]
    else:
        lines = ['{"custom_id":']
    path.write_text("\n".join(lines))
    report = collect.result_report([path], [record()], "grader", "anthropic")
    assert report["errors"]
    assert report["complete_and_valid"] is False


def test_read_only_collector_skips_bad_completed_results(tmp_path, monkeypatch):
    wave = tmp_path / "waves/0000"
    for provider in sg.PROVIDERS:
        folder = wave / provider
        sb.write(folder / (sb.SDK_NAMES[provider] + "-batch.json"), {"batch_id": "EXISTING"})
    bad = wave / "anthropic/results.jsonl"
    bad.write_text("bad output already downloaded\n")
    calls = []
    monkeypatch.setattr(collect, "saved_waves", lambda *args: ({"models": {}}, [(wave, [])]))
    monkeypatch.setattr(collect, "inspect", lambda *args: {"collection_complete": True})
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    monkeypatch.setattr(sb, "_client", lambda p: calls.append(p))
    def status(job, client):
        return {"status": "completed", "state": "JOB_STATE_SUCCEEDED"}
    def download(job, output, client):
        output.write_text("existing batch results\n")
    for provider in sg.PROVIDERS:
        sdk = sb.SDK_NAMES[provider]
        monkeypatch.setattr(sb.batch, "status_" + sdk, status)
        monkeypatch.setattr(sb.batch, "download_" + sdk, download)
        monkeypatch.setattr(sb.batch, "submit_" + sdk, lambda *a, **kw: pytest.fail("No new batch"))
    assert collect.collect_once(tmp_path, "screen")["collection_complete"]
    assert calls == ["openai", "google"]
    assert bad.read_text() == "bad output already downloaded\n"


def test_provider_failure_does_not_block_other_collection(tmp_path, monkeypatch):
    wave = tmp_path / "waves/0000"
    for provider in sg.PROVIDERS:
        sb.write(wave / provider / (sb.SDK_NAMES[provider] + "-batch.json"), {"id": "EXISTING"})
    calls = []
    monkeypatch.setattr(collect, "saved_waves", lambda *args: ({}, [(wave, [])]))
    monkeypatch.setattr(collect, "inspect", lambda *args: {"collection_complete": False})
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    monkeypatch.setattr(sb, "_client", lambda p: p)
    def status(job, client):
        calls.append(client)
        if client == "openai":
            raise TimeoutError("Temporary status timeout")
        return {"processing_status": "in_progress", "state": "JOB_STATE_RUNNING"}
    for provider in sg.PROVIDERS:
        monkeypatch.setattr(sb.batch, "status_" + sb.SDK_NAMES[provider], status)
    report = collect.collect_once(tmp_path, "screen")
    assert calls == list(sg.PROVIDERS)
    assert report["collection_errors"][0]["provider"] == "openai"
