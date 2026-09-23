from copy import deepcopy
import json
import shutil

import pytest

from src import revision_audit as review, revision_batch as runner, revision_budget as budget
from src import semantic_batch as sb, semantic_budget as money, semantic_grade as sg
from src import semantic_protocol as protocol


def payload():
    return {"brief": "An explicit source fact.", "statements": [{"id": "s0", "text": "A concern."}],
            "criteria": [{"id": "c1", "kind": "landmine", "reference": "A concern."}]}


@pytest.fixture
def pack(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    folder = tmp_path / "revision"
    config = {"total_cap_usd": 100, "submission_cap_usd": 1, "screen_cap_usd": 1,
        "max_output_tokens": 256, "submit_before": "9999-12-31",
        "models": {p: "grader-id" for p in sg.PROVIDERS},
        "batch_rate_bounds": {p: [1, 1] for p in sg.PROVIDERS}}
    sb.write(folder / "config.json", config)
    rows = [{"id": str(i), "payload": payload()} for i in range(96)]
    for phase in ["screen", "full"]:
        sb.prepare_phase(folder, phase, rows, config)
    sb.write(folder / "seal.json", {"base_reserved_microusd": 100_000})
    monkeypatch.setattr(budget, "verify", lambda _: config)
    return folder, config


def envelope(provider, rid, usage=True):
    text = '{"checks":'
    tokens = {"input_tokens": 10, "output_tokens": 10} if usage else {}
    if provider == "openai":
        return {"custom_id": rid, "response": {"status_code": 200, "body": {
            "model": "grader-id", "status": "completed", "usage": tokens,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}}}
    if provider == "anthropic":
        return {"custom_id": rid, "result": {"type": "succeeded", "message": {
            "model": "grader-id", "stop_reason": "end_turn", "usage": tokens,
            "content": [{"type": "text", "text": text}]}}}
    tokens = {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 5, "totalTokenCount": 20} if usage else {}
    return {"key": rid, "response": {"modelVersion": "grader-id", "usageMetadata": tokens,
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]}}


def complete(pack, wave, usage=True):
    _, records, _ = budget.wave_inputs(pack, wave)
    for provider in sg.PROVIDERS:
        folder = wave / provider
        sb.write(folder / "submission-attempt.json", {"request_sha256": sb.sha(folder / "requests.jsonl")})
        sb.write(folder / (sb.SDK_NAMES[provider] + "-batch.json"), {"id": "FAKE"})
        status = {"openai": {"status": "completed"}, "anthropic": {"processing_status": "ended"},
                  "google": {"state": "JOB_STATE_SUCCEEDED"}}[provider]
        sb.write(folder / (sb.SDK_NAMES[provider] + "-status.json"), status)
        (folder / "results.jsonl").write_text("".join(json.dumps(envelope(provider, r["id"], usage)) + "\n" for r in records))


def test_base_hold_survives_revision_and_usage(pack):
    folder, config = pack
    state = budget.account(folder, config)
    assert state["charged_or_reserved_microusd"] == 100_000
    wave = budget.reserve(folder, "screen", config, state)
    assert wave is not None
    before = budget.account(folder, config)
    assert before["charged_or_reserved_microusd"] <= 1_000_000
    complete(folder, wave)
    after = budget.account(folder, config)
    assert after["charged_or_reserved_microusd"] == 100_000 + after["ends"]["screen"] * 60
    assert after["charged_or_reserved_microusd"] < before["charged_or_reserved_microusd"]


def test_missing_usage_cannot_release_allowance(pack):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    before = budget.account(folder, config)["charged_or_reserved_microusd"]
    complete(folder, wave, usage=False)
    assert budget.account(folder, config)["charged_or_reserved_microusd"] == before


def test_deleted_wave_cannot_reset_spending(pack):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    shutil.rmtree(wave)
    with pytest.raises(ValueError, match="missing"):
        budget.account(folder, config)


def test_mutated_wave_is_rejected(pack):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    path = wave / "openai/requests.jsonl"
    path.write_text(path.read_text().replace("grader-id", "different"))
    with pytest.raises(ValueError, match="changed"):
        budget.account(folder, config)


def test_ambiguous_submission_cannot_be_retried(pack, monkeypatch):
    folder, config = pack
    monkeypatch.setattr(sb, "_credentials", lambda: None)
    monkeypatch.setattr(sb, "_client", lambda p: object())
    def timeout(*args, **kwargs):
        raise TimeoutError("The provider may have accepted the batch")
    monkeypatch.setattr(sb.batch, "submit_openai", timeout)
    with pytest.raises(TimeoutError):
        runner.submit(folder, "screen")
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("No retry"))
    with pytest.raises(ValueError, match="Unresolved submission"):
        runner.submit(folder, "screen")


def test_resume_never_resubmits_existing_jobs(pack, monkeypatch):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    complete(folder, wave)
    for provider in sg.PROVIDERS:
        (wave / provider / "results.jsonl").unlink()
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("Existing batch resubmitted"))
    assert runner.submit(folder, "screen") == wave


def test_missing_credentials_precede_reservation(pack, monkeypatch):
    folder, config = pack
    def absent():
        raise RuntimeError("Credentials absent")
    monkeypatch.setattr(sb, "_credentials", absent)
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("No client"))
    with pytest.raises(RuntimeError, match="absent"):
        runner.submit(folder, "screen")
    assert not list(folder.glob("waves/*"))
    assert budget.account(folder, config)["charged_or_reserved_microusd"] == 100_000


def test_failed_screen_blocks_full_inference(pack, monkeypatch):
    folder, _ = pack
    monkeypatch.setattr(review, "analyze_screen", lambda _: {"passed": False})
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("No full grading"))
    with pytest.raises(ValueError, match="blocked"):
        runner.submit(folder, "full")


def test_collection_survives_malformed_grade(pack, monkeypatch):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    complete(folder, wave)
    saved = {}
    for provider in ["openai", "google"]:
        path = wave / provider / "results.jsonl"
        saved[provider] = path.read_text()
        path.unlink()
    calls = []
    monkeypatch.setattr(sb, "_client", lambda p: p)
    def status(job, client):
        calls.append(client)
        return {"status": "completed", "state": "JOB_STATE_SUCCEEDED"}
    def download(job, output, client):
        output.write_text(saved[client])
    for provider in ["openai", "google"]:
        monkeypatch.setattr(sb.batch, "status_" + sb.SDK_NAMES[provider], status)
        monkeypatch.setattr(sb.batch, "download_" + sb.SDK_NAMES[provider], download)
    runner.collect_once(folder, "screen")
    assert calls == ["openai", "google"]
    _, rows, _ = budget.wave_inputs(folder, wave)
    votes, errors = review.read_outputs(wave / "anthropic/results.jsonl", rows, "anthropic", "grader-id")
    assert len(errors) == len(rows)
    assert all(v["votes"]["c1"]["verdict"] == "unresolved" for v in votes.values())


def test_one_revision_binding_cannot_reset(pack, monkeypatch):
    folder, config = pack
    monkeypatch.setattr(budget, "verify_inputs", lambda _: config)
    sb.write(sb.ROOT / money.AUTHORITY, {"original": "unchanged"})
    budget.bind(folder)
    original = (sb.ROOT / budget.REGISTRY).read_bytes()
    budget.bind(folder)
    other = sb.ROOT / "other"
    sb.write(other / "seal.json", {"base_reserved_microusd": 100_000})
    with pytest.raises(ValueError, match="different revision"):
        budget.bind(other)
    assert (sb.ROOT / budget.REGISTRY).read_bytes() == original


def test_usage_overrun_stops_spending_and_saves_report(pack, monkeypatch):
    folder, config = pack
    wave = budget.reserve(folder, "screen", config, budget.account(folder, config))
    complete(folder, wave)
    path = wave / "openai/results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]['response']['body']['usage']['output_tokens'] = config['max_output_tokens'] + 1
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    monkeypatch.setattr(sb, '_client', lambda _: pytest.fail('No new provider call'))
    report = runner.collect_once(folder, 'screen')
    assert 'exceeds reservation' in report['billing_error']
    assert sb.read(folder / 'collection/screen.json') == report
    with pytest.raises(ValueError, match='exceeds reservation'):
        runner.submit(folder, 'screen')


def test_unaffordable_wave_never_creates_client(pack, monkeypatch):
    folder, config = pack
    config['submission_cap_usd'] = 0.1
    config['screen_cap_usd'] = 0.1
    monkeypatch.setattr(sb, '_credentials', lambda: None)
    monkeypatch.setattr(sb, '_client', lambda _: pytest.fail('Budget cannot fit'))
    assert runner.submit(folder, 'screen') is None
    assert not list(folder.glob('waves/*'))


def test_wire_schema_keeps_strict_ids_local():
    wire = protocol.wire_schema(payload())
    assert 'maxLength' not in json.dumps(wire)
    assert 'maxItems' not in json.dumps(wire)
    assert wire['properties']['checks']['required'] == ['c1']
    assert wire['properties']['checks']['additionalProperties'] is False
    row = wire['properties']['checks']['properties']['c1']['properties']
    assert row['coverage']['enum'] == ['yes', 'no', 'uncertain']
    assert row['evidence_ids']['items'] == {'type': 'string'}


def test_replicates_have_identical_inference_bodies():
    record = {'id': 'original', 'category': 'real', 'payload': payload()}
    repeat = protocol.repeat_record(record)
    for provider in sg.PROVIDERS:
        a = protocol.make_request(provider, 'grader-id', record['id'], record['payload'], 8192)
        b = protocol.make_request(provider, 'grader-id', repeat['id'], repeat['payload'], 8192)
        assert a != b
        assert protocol.request_body(a, provider) == protocol.request_body(b, provider)


def screen_fixture():
    from test_semantic_batch import _screen_fixture
    records, outputs = _screen_fixture()
    replicate = {**deepcopy(records[1]), 'id': 'replicate', 'category': 'replicate', 'parent': 'real'}
    fresh = [{**deepcopy(r), 'id': 'fresh_' + r['id'], 'category': 'fresh_control'} for r in records if r['category'] == 'control']
    for provider in sg.PROVIDERS:
        outputs[provider]['replicate'] = deepcopy(outputs[provider]['real'])
        for row in fresh:
            outputs[provider][row['id']] = deepcopy(outputs[provider][row['id'][6:]])
    return records + [replicate] + fresh, outputs


@pytest.mark.parametrize('failure', ['none', 'repeat', 'fresh', 'invalid'])
def test_added_gates_cannot_be_skipped(failure):
    records, outputs = screen_fixture()
    errors = {p: [] for p in sg.PROVIDERS}
    if failure == 'repeat':
        outputs['openai']['replicate']['votes']['c']['verdict'] = 'fail'
    elif failure == 'fresh':
        outputs['anthropic']['fresh_bad']['votes']['c']['verdict'] = 'pass'
    elif failure == 'invalid':
        errors['google'] = [{'reason': 'malformed'}]
    result = review.screen_report(records, outputs, errors)
    assert result['passed'] == (failure == 'none')
    assert not result['ground_truth_accuracy_established']
