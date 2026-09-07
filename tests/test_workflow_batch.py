from copy import deepcopy
import json
from pathlib import Path

import pytest

from src import task_score, workflow_batch as runner, workflow_score as scoring


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    source = runner.ROOT
    for name in runner.SOURCE_FILES:
        target = tmp_path / "src" / (name + ".py")
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((source / "src" / (name + ".py")).read_bytes())
    (tmp_path / "docs/history/v3.5").mkdir(parents=True)
    (tmp_path / "docs/history/v3.5/decision-inputs.json").write_text('{"synthetic":"sealed"}')
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.batch, "ROOT", tmp_path)
    monkeypatch.setattr(runner.transport, "ROOT", tmp_path)
    legacy = tmp_path / "prior-budget.json"
    legacy.write_text("immutable prior spending")
    monkeypatch.setattr(runner, "legacy_snapshot", lambda: {"held_microusd": runner.OLD_HOLD, "sha256": runner.sha(legacy)})
    tasks = []
    for index in range(6):
        prompt = {"instruction": "Return JSON using only the supplied value.", "inputs": {"value": True},
            "questions": [{"id": "known", "type": "string", "question": "Is value true?", "choices": ["yes", "no"]}]}
        control = deepcopy(prompt)
        control["inputs"]["value"] = False
        tasks.append({"id": f"example-{index}", "subject_prompt": prompt, "key": {"known": "yes"},
            "source_family": f"synthetic-{index}", "evidence_inputs": {"known": ["value"]}, "field_metrics": {"known": "honesty"}, "honesty_fields": ["known"],
            "synthetic_control": {"synthetic": True, "subject_prompt": control, "key": {"known": "no"}, "evidence_inputs": {"known": ["value"]}}})
    panel = [{"name": p, "id": p + "-exact", "provider": p, "batch_rates": [0.1, 0.2],
              "price_source": "https://provider.example/pricing", "price_verified": "2026-09-06"}
             for p in sorted(runner.PROVIDERS)]
    plan = {"schema_version": 1, "old_hold_microusd": runner.OLD_HOLD, "max_output_tokens": 128,
            "qualification_max_output_tokens": 256, "submit_before": "9999-12-31", "pilot_pair_id": "example-0", "panel": panel, "roster": [panel[0]]}
    tasks_path, plan_path = tmp_path / "tasks.json", tmp_path / "plan.json"
    runner.write(tasks_path, {"tasks": tasks})
    runner.write(plan_path, plan)
    folder = tmp_path / "notes" / "workflow-pack"
    return folder, tasks_path, plan_path, plan


def prepare(fixture):
    folder, tasks_path, plan_path, _ = fixture
    runner.prepare(tasks_path, plan_path, folder)
    return folder


def fake_submit(monkeypatch, fail=None):
    monkeypatch.setattr(runner, "credentials", lambda _: None)
    monkeypatch.setattr(runner.transport, "_client", lambda provider: provider)
    calls = []
    def submit(manifest, client):
        intent_path = runner.ROOT / runner.INTENTS
        intents = intent_path.read_text().splitlines()
        authority = runner.read(runner.ROOT / runner.AUTHORITY)
        assert authority["submission_count"] == len(intents)
        assert authority["submission_sha256"] == runner.sha(intent_path)
        assert json.loads(intents[-1])["provider"] == client
        assert (manifest.parent / "submission-attempt.json").exists()
        calls.append(client)
        if client == fail:
            raise TimeoutError("Acceptance may already have occurred")
        runner.write(manifest.parent / (runner.transport.SDK_NAMES[client] + "-batch.json"), {"batch_id": "synthetic-job"})
    for provider in runner.PROVIDERS:
        monkeypatch.setattr(runner.batch, "submit_" + runner.transport.SDK_NAMES[provider], submit)
    return calls


def envelope(provider, record, text, model=None, finish=None):
    model = model or record["requested_model_id"]
    usage = {"input_tokens": 10, "output_tokens": 10}
    if provider == "openai":
        return {"custom_id": record["request_id"], "response": {"status_code": 200, "body": {
            "model": model, "status": finish or "completed", "usage": usage, "output_text": text}}}
    if provider == "anthropic":
        return {"custom_id": record["request_id"], "result": {"type": "succeeded", "message": {
            "model": model, "stop_reason": finish or "end_turn", "usage": usage,
            "content": [{"type": "text", "text": text}]}}}
    return {"key": record["request_id"], "response": {"modelVersion": model,
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10, "totalTokenCount": 20},
        "candidates": [{"finishReason": finish or "STOP", "content": {"parts": [{"text": text}]}}]}}


def fake_collection(folder, phase, plan, monkeypatch, fail=None, transform=None):
    tasks = {t["id"]: t for t in scoring.expand_tasks(runner.read(folder / "tasks.json"))}
    rows = runner.read(folder / phase / "records.json")
    calls = []
    def status(job, client):
        calls.append(client)
        if client == fail:
            raise ConnectionError("Synthetic read failure")
        value = {"openai": {"status": "completed"}, "anthropic": {"processing_status": "ended"},
                 "google": {"state": "JOB_STATE_SUCCEEDED"}}[client]
        runner.transport.write(job.parent / (runner.transport.SDK_NAMES[client] + "-status.json"), value)
        return value
    def download(job, output, client):
        replies = []
        for row in rows:
            if row["provider"] != client:
                continue
            task = tasks[row["task_id"]]
            value = task["key"]
            if phase == "qualification":
                value = {"answers": value, "evidence": {k: ["value"] for k in task["key"]}, "ambiguous": []}
            reply = envelope(client, row, json.dumps(value))
            if transform:
                reply = transform(client, reply)
            replies.append(reply)
        output.write_text("".join(json.dumps(r) + "\n" for r in replies))
    for provider in runner.PROVIDERS:
        monkeypatch.setattr(runner.batch, "status_" + runner.transport.SDK_NAMES[provider], status)
        monkeypatch.setattr(runner.batch, "download_" + runner.transport.SDK_NAMES[provider], download)
    return calls


def test_preparation_blinds_keys_and_seals_both_phases(prepared):
    folder = prepare(prepared)
    plan, seal = runner.verify(folder)
    assert runner.OLD_HOLD + sum(seal["costs_microusd"].values()) < 90_000_000
    assert len(runner.read(folder / "qualification/records.json")) == 72
    assert len(runner.read(folder / "pilot/records.json")) == 4
    assert len(runner.read(folder / "subject/records.json")) == 20
    for phase in runner.PHASES:
        for path in (folder / phase).glob("*/requests.jsonl"):
            raw = path.read_text()
            assert '"key": {"known"' not in raw
            assert "source_family" not in raw
            assert "synthetic-0" not in raw
    assert not (runner.ROOT / runner.JOURNAL).exists()
    assert runner.read(folder / "qualification/records.json")[0]["output_bound"] == 256
    assert runner.read(folder / "subject/records.json")[0]["output_bound"] == 128


@pytest.mark.parametrize("target", ["tasks.json", "plan.json", "src/workflow_score.py", "notes/workflow-pack/subject/anthropic/requests.jsonl", "prior-budget.json", "docs/history/v3.5/decision-inputs.json"])
def test_sealed_inputs_and_old_holds_cannot_change(prepared, target):
    folder = prepare(prepared)
    path = runner.ROOT / target
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="changed"):
        runner.verify(folder)


def test_rejects_second_pack_and_budget_reset(prepared):
    folder = prepare(prepared)
    with pytest.raises(ValueError, match="already holds"):
        runner.prepare(prepared[1], prepared[2], folder.with_name("another"))
    assert runner.read(runner.ROOT / runner.AUTHORITY)["old_hold_microusd"] == 76_760_474


@pytest.mark.parametrize("change", [lambda p: p.update(schema_version=True), lambda p: p.update(old_hold_microusd=1),
    lambda p: p["panel"][0].update(provider="openrouter"), lambda p: p["panel"][1].update(provider=p["panel"][0]["provider"]),
    lambda p: p["roster"][0].update(batch_rates=[float("nan"), 1]), lambda p: p.update(qualification_max_output_tokens=True)])
def test_invalid_authorizations_fail_before_preparation(prepared, change):
    plan = deepcopy(prepared[3])
    change(plan)
    with pytest.raises(ValueError):
        runner.validate_plan(plan)


def test_unaffordable_pack_is_cleaned_without_authorization(prepared):
    folder, tasks, path, plan = prepared
    for cfg in plan["panel"] + plan["roster"]:
        cfg["batch_rates"] = [999, 999]
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="ceiling"):
        runner.prepare(tasks, path, folder)
    assert not folder.exists()
    assert not (runner.ROOT / runner.AUTHORITY).exists()


def test_missing_credentials_precede_reservation(prepared, monkeypatch):
    folder = prepare(prepared)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner.transport, "_client", lambda _: pytest.fail("No client without credentials"))
    with pytest.raises(RuntimeError, match="Credentials absent"):
        runner.submit(folder, "qualification")
    assert not (runner.ROOT / runner.JOURNAL).exists()


def test_subjects_blocked_without_qualified_panel(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    with pytest.raises(ValueError, match="blocked"):
        runner.submit(folder, "subject")
    assert not (runner.ROOT / runner.JOURNAL).exists()


def test_full_hold_precedes_ambiguous_attempt_and_no_retry(prepared, monkeypatch):
    folder = prepare(prepared)
    calls = fake_submit(monkeypatch, "anthropic")
    with pytest.raises(TimeoutError):
        runner.submit(folder, "qualification")
    held = runner.read(folder / "qualification/reservation.json")["reserved_microusd"]
    assert held == runner.read(folder / "seal.json")["costs_microusd"]["qualification"]
    with pytest.raises(ValueError, match="Unresolved"):
        runner.submit(folder, "qualification")
    assert calls == ["anthropic"]


def test_deleted_journal_does_not_release_reservation(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    (runner.ROOT / runner.JOURNAL).unlink()
    with pytest.raises(ValueError, match="Orphaned"):
        runner.submit(folder, "qualification")


def test_read_failure_does_not_prevent_other_providers(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    calls = fake_collection(folder, "qualification", prepared[3], monkeypatch, fail="anthropic")
    report = runner.collect(folder, "qualification")
    assert calls == ["anthropic", "google", "openai"]
    assert report["record_statuses"] == {"completed": 48, "provider_error": 0, "truncated": 0, "missing": 24}
    assert not report["collection_complete"]


def test_qualified_panel_unlocks_only_fresh_subject_calls(prepared, monkeypatch):
    folder = prepare(prepared)
    calls = fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    fake_collection(folder, "qualification", prepared[3], monkeypatch)
    report = runner.collect(folder, "qualification")
    assert report["collection_complete"]
    before = (runner.ROOT / runner.JOURNAL).read_text()
    assert runner.submit(folder, "qualification")["submitted"] == 0
    assert (runner.ROOT / runner.JOURNAL).read_text() == before
    assert runner.submit(folder, "pilot")["submitted"] == 1
    fake_collection(folder, "pilot", prepared[3], monkeypatch)
    assert runner.collect(folder, "pilot")["collection_complete"]
    assert runner.submit(folder, "subject")["submitted"] == 1
    assert calls == ["anthropic", "google", "openai", "anthropic", "anthropic"]
    journal = (runner.ROOT / runner.JOURNAL).read_text().splitlines()
    assert len(journal) == 3
    qualification = {r["request_id"] for r in runner.read(folder / "qualification/records.json")}
    subjects = {r["request_id"] for r in runner.read(folder / "subject/records.json")}
    pilot = {r["request_id"] for r in runner.read(folder / "pilot/records.json")}
    assert not qualification & subjects and not pilot & subjects and not pilot & qualification
    assert runner.OLD_HOLD + sum(json.loads(r)["reserved_microusd"] for r in journal) < 90_000_000


def test_modified_native_result_cannot_qualify(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    fake_collection(folder, "qualification", prepared[3], monkeypatch)
    runner.collect(folder, "qualification")
    path = folder / "qualification/openai/results.jsonl"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="results changed"):
        runner.submit(folder, "subject")


@pytest.mark.parametrize("variation", ["missing_model", "wrong_model", "truncated", "invalid_json"])
def test_native_identity_and_status_remain_distinct(prepared, monkeypatch, variation):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    def transform(provider, reply):
        if provider == "openai":
            body = reply["response"]["body"]
            if variation == "missing_model":
                del body["model"]
            elif variation == "wrong_model":
                body["model"] = "unrequested-model"
            elif variation == "truncated":
                body["status"] = "incomplete"
                body["incomplete_details"] = {"reason": "max_output_tokens"}
            else:
                body["output_text"] = '{"broken"'
        return reply
    fake_collection(folder, "qualification", prepared[3], monkeypatch, transform=transform)
    report = runner.collect(folder, "qualification")
    expected = "provider_error" if "model" in variation else "truncated" if variation == "truncated" else "completed"
    rows = runner.records(folder, "qualification", prepared[3])
    assert {r["status"] for r in rows if r["provider"] == "openai"} == {expected}
    assert report["collection_complete"]
    with pytest.raises(ValueError, match="blocked"):
        runner.submit(folder, "subject")


def test_pilot_wrong_answers_pass_operational_gate(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    fake_collection(folder, "qualification", prepared[3], monkeypatch)
    runner.collect(folder, "qualification")
    runner.submit(folder, "pilot")
    def wrong(provider, reply):
        reply["result"]["message"]["content"][0]["text"] = '{"known":"no"}'
        return reply
    fake_collection(folder, "pilot", prepared[3], monkeypatch, transform=wrong)
    runner.collect(folder, "pilot")
    assert runner.submit(folder, "subject")["submitted"] == 1


@pytest.mark.parametrize("usage", [None, 97])
def test_pilot_missing_usage_or_low_headroom_blocks_expansion(prepared, monkeypatch, usage):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    fake_collection(folder, "qualification", prepared[3], monkeypatch)
    runner.collect(folder, "qualification")
    runner.submit(folder, "pilot")
    def transform(provider, reply):
        reply["result"]["message"]["usage"] = {} if usage is None else {"input_tokens": 10, "output_tokens": usage}
        return reply
    fake_collection(folder, "pilot", prepared[3], monkeypatch, transform=transform)
    runner.collect(folder, "pilot")
    with pytest.raises(ValueError, match="headroom"):
        runner.submit(folder, "subject")
    assert len((runner.ROOT / runner.JOURNAL).read_text().splitlines()) == 2


def test_score_requires_every_planned_native_subject(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    for phase in ("qualification", "pilot"):
        runner.submit(folder, phase)
        fake_collection(folder, phase, prepared[3], monkeypatch)
        runner.collect(folder, phase)
    runner.submit(folder, "subject")
    with pytest.raises(ValueError, match="Every planned subject"):
        runner.score(folder)
    assert not (folder / "v4-scores.json").exists()


def test_score_reuses_pilot_answers_and_writes_idempotently(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    for phase in runner.PHASES:
        runner.submit(folder, phase)
        fake_collection(folder, phase, prepared[3], monkeypatch)
        runner.collect(folder, phase)
    called = []
    def combine(old, new):
        called.append((old, new))
        return {"synthetic_composite": True}
    monkeypatch.setattr(scoring, "combine", combine, raising=False)
    assert runner.score(folder)["models"] == 1
    assert all(model["eligible"] for model in called[0][1]["models"])
    digest = runner.sha(folder / "v4-scores.json")
    runner.score(folder)
    assert runner.sha(folder / "v4-scores.json") == digest
    assert not list(folder.glob("*.partial.json"))


def test_unexpected_request_id_stops_scores_after_collecting_all(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    def transform(provider, reply):
        if provider == "anthropic":
            reply["custom_id"] = "outside-frozen-roster"
        return reply
    calls = fake_collection(folder, "qualification", prepared[3], monkeypatch, transform=transform)
    report = runner.collect(folder, "qualification")
    assert calls == ["anthropic", "google", "openai"]
    assert report["harness_error_type"] == "ValueError"
    assert not report["collection_complete"]
    assert not (folder / "qualification/answers.json").exists()


@pytest.mark.parametrize("mutation", ["journal", "request", "sequence", "hold"])
def test_legacy_journal_guards_without_usage_reconciliation(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    base, previous = tmp_path / "base", tmp_path / "previous"
    wave = previous / "waves/0000"
    base.mkdir()
    runner.write(base / "seal.json", {"files": {}})
    runner.write(previous / "seal.json", {"files": {}})
    runner.write(tmp_path / runner.money.AUTHORITY, {"pack_path": "base"})
    runner.write(tmp_path / runner.revision.REGISTRY, {"pack_path": "previous"})
    cfg = {"max_output_tokens": 1, "batch_rate_bounds": {p: [1, 1] for p in runner.PROVIDERS}}
    requests = {p: [{"synthetic": "request"}] for p in runner.PROVIDERS}
    hold = sum(runner.money.request_bound(q, p, cfg)[1] for p, rows in requests.items() for q in rows)
    for provider in runner.PROVIDERS:
        (wave / provider).mkdir(parents=True)
        (wave / provider / "requests.jsonl").write_text(json.dumps(requests[provider][0]) + "\n")
    reservation = {"phase": "screen", "start": 0, "stop": 1, "seal_sha256": "synthetic"}
    event = {"pack_path": "previous", "wave": "0000", **reservation,
             "request_sha256": {p: runner.sha(wave / p / "requests.jsonl") for p in runner.PROVIDERS}}
    journal = tmp_path / runner.revision.JOURNAL
    journal.write_text(json.dumps(event) + "\n")
    monkeypatch.setattr(runner.money, "authorize", lambda _: None)
    monkeypatch.setattr(runner.revision, "verify", lambda _: cfg)
    monkeypatch.setattr(runner.revision, "account", lambda *_: pytest.fail("No old-usage reconciliation"))
    monkeypatch.setattr(runner.revision, "base_reservation", lambda _: runner.OLD_HOLD - hold)
    monkeypatch.setattr(runner.revision, "wave_inputs", lambda *_: (reservation, [], requests))
    assert runner.legacy_snapshot()["held_microusd"] == runner.OLD_HOLD
    if mutation == "journal":
        journal.write_text("")
    elif mutation == "request":
        (wave / "openai/requests.jsonl").write_text("changed")
    elif mutation == "sequence":
        reservation["start"] = 1
    else:
        monkeypatch.setattr(runner.revision, "base_reservation", lambda _: runner.OLD_HOLD)
    with pytest.raises(ValueError):
        runner.legacy_snapshot()


def test_unattended_run_preserves_phase_order_and_polls_reads(monkeypatch, tmp_path):
    calls, polled = [], set()
    monkeypatch.setattr(runner, "submit", lambda pack, phase: calls.append(("submit", phase)))
    def collect(pack, phase):
        calls.append(("collect", phase))
        complete = phase in polled
        polled.add(phase)
        return {"collection_complete": complete}
    monkeypatch.setattr(runner, "collect", collect)
    monkeypatch.setattr(runner.time, "sleep", lambda duration: calls.append(("sleep", duration)))
    monkeypatch.setattr(runner, "score", lambda pack: {"finished": True})
    assert runner.run(tmp_path) == {"finished": True}
    assert [call for call in calls if call[0] == "submit"] == [("submit", phase) for phase in runner.PHASES]
    assert sum(call == ("sleep", 60) for call in calls) == 3


@pytest.mark.parametrize("failure", ["submission", "evidence"])
def test_unattended_run_stops_without_paid_retries(monkeypatch, tmp_path, failure):
    calls = []
    def submit(pack, phase):
        calls.append(phase)
        if failure == "submission":
            raise TimeoutError("Ambiguous native acceptance")
    monkeypatch.setattr(runner, "submit", submit)
    monkeypatch.setattr(runner, "collect", lambda *_: {"collection_complete": False, "harness_error_type": "ValueError"})
    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("No retry after terminal evidence failure"))
    with pytest.raises((TimeoutError, RuntimeError)):
        runner.run(tmp_path)
    assert calls == ["qualification"]


def test_cli_does_not_print_provider_exception_details(monkeypatch, tmp_path, capsys):
    import sys
    monkeypatch.setattr(sys, "argv", ["workflow_batch", "run", "--pack", str(tmp_path)])
    def failed(pack):
        raise RuntimeError("sensitive transport detail")
    monkeypatch.setattr(runner, "run", failed)
    with pytest.raises(SystemExit):
        runner.main()
    output = capsys.readouterr()
    assert "sensitive transport detail" not in output.err + output.out
    assert "RuntimeError" in output.err


@pytest.mark.parametrize("deleted", ["both_local_files", "intent_journal", "last_intent"])
def test_deleted_submission_evidence_cannot_resubmit(prepared, monkeypatch, deleted):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    authority_before = (runner.ROOT / runner.AUTHORITY).read_bytes()
    path = runner.ROOT / runner.INTENTS
    if deleted == "both_local_files":
        (folder / "qualification/openai/submission-attempt.json").unlink()
        (folder / "qualification/openai/openai-batch.json").unlink()
    elif deleted == "intent_journal":
        path.unlink()
    else:
        path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    monkeypatch.setattr(runner.transport, "_client", lambda _: pytest.fail("Never resubmit a durable intent"))
    with pytest.raises(ValueError):
        runner.submit(folder, "qualification")
    assert (runner.ROOT / runner.AUTHORITY).read_bytes() == authority_before


def test_duplicate_intent_fails_even_with_matching_checkpoint(prepared, monkeypatch):
    folder = prepare(prepared)
    fake_submit(monkeypatch)
    runner.submit(folder, "qualification")
    path = runner.ROOT / runner.INTENTS
    rows = path.read_text().splitlines()
    rows[1] = rows[0]
    path.write_text("\n".join(rows) + "\n")
    authority = runner.read(runner.ROOT / runner.AUTHORITY)
    authority["submission_sha256"] = runner.sha(path)
    (runner.ROOT / runner.AUTHORITY).write_text(json.dumps(authority))
    monkeypatch.setattr(runner.transport, "_client", lambda _: pytest.fail("No duplicate paid intent"))
    with pytest.raises(ValueError, match="intent differs"):
        runner.submit(folder, "qualification")
