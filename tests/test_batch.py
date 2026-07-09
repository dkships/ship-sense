import json
from pathlib import Path
import uuid

from src import batch, loader

ROOT = Path(__file__).resolve().parent.parent


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _openai_success_line(custom_id: str, model: str, text: str) -> dict:
    return {
        "id": f"batch_req_{custom_id}",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": f"req_{custom_id}",
            "body": {
                "id": f"resp_{custom_id}",
                "model": model,
                "status": "completed",
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            },
        },
        "error": None,
    }


def test_gemini_result_reads_candidate_finish_reason():
    line = {
        "key": "gemini-1",
        "response": {
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": '{"ok": true}'}]},
            }],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
        },
    }
    _, result = batch._result_from_line(
        "google", {"id": "gemini-test", "price_in": 1, "price_out": 1}, line)
    assert result.finish_reason == "MAX_TOKENS"
    assert result.text == '{"ok": true}'


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeOpenAIFiles:
    def __init__(self):
        self.contents = {
            "file-results": _Obj(text='{"custom_id":"ok"}\n'),
            "file-errors": _Obj(text='{"custom_id":"bad","error":{"code":"x"}}\n'),
        }

    def content(self, file_id):
        return self.contents[file_id]


class _FakeOpenAIClient:
    def __init__(self):
        self.files = _FakeOpenAIFiles()
        self.batches = _Obj(retrieve=lambda batch_id: _Obj(
            id=batch_id,
            status="completed",
            output_file_id="file-results",
            error_file_id="file-errors",
            request_counts={"total": 2, "completed": 1, "failed": 1},
        ))


class _FakeAnthropicBatches:
    def retrieve(self, batch_id):
        return _Obj(
            id=batch_id,
            processing_status="ended",
            request_counts={"succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
        )

    def results(self, batch_id):
        return [
            {
                "custom_id": "anthropic-1",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "id": "msg_1",
                        "model": "claude-test",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    },
                },
            }
        ]


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _Obj(batches=_FakeAnthropicBatches())


class _FakeGeminiClient:
    def __init__(self, state="JOB_STATE_SUCCEEDED"):
        self.job = {
            "name": "batches/test",
            "state": state,
            "dest": {"fileName": "files/result-jsonl"},
        }
        self.batches = _Obj(get=lambda name: self.job)
        self.files = _Obj(download=lambda file: b'{"key":"gemini-1"}\n')


def test_prepare_openai_uses_responses_batch_shape():
    manifests = batch.prepare(
        ["gpt-5.4-mini"],
        _run_id("pytest-batch-openai-shape"),
        case_scope=loader.CASE_SCOPE_EXAMPLES,
        generations=1,
        stage_id="shape",
    )
    manifest = json.loads(manifests[0].read_text())
    rows = _read_jsonl(ROOT / manifest["requests_file"])
    assert rows
    assert {row["url"] for row in rows} == {"/v1/responses"}
    assert all(row["method"] == "POST" for row in rows)
    assert all(row["custom_id"] for row in rows)
    assert all(row["body"]["text"]["format"]["type"] == "json_schema" for row in rows)
    conviction = [r for r in manifest["requests"] if r["schema"] == "conviction"]
    assert conviction and {r["turn_id"] for r in conviction} == {"setup"}


def test_prepare_provider_native_shapes_for_anthropic_and_gemini():
    anthropic_manifest = batch.prepare(
        ["claude-haiku-4-5"],
        _run_id("pytest-batch-anthropic-shape"),
        case_scope=loader.CASE_SCOPE_EXAMPLES,
        generations=1,
        stage_id="shape",
    )[0]
    anthropic_rows = _read_jsonl(ROOT / json.loads(anthropic_manifest.read_text())["requests_file"])
    assert {"custom_id", "params"} <= set(anthropic_rows[0])
    assert anthropic_rows[0]["params"]["model"] == "claude-haiku-4-5-20251001"

    gemini_manifest = batch.prepare(
        ["gemini-2.5-flash"],
        _run_id("pytest-batch-gemini-shape"),
        case_scope=loader.CASE_SCOPE_EXAMPLES,
        generations=1,
        stage_id="shape",
    )[0]
    gemini_rows = _read_jsonl(ROOT / json.loads(gemini_manifest.read_text())["requests_file"])
    assert {"key", "request"} <= set(gemini_rows[0])
    assert "contents" in gemini_rows[0]["request"]
    assert gemini_rows[0]["request"]["generation_config"]["response_mime_type"] == "application/json"


def test_ingest_then_prepare_next_conviction_stage_preserves_assistant_context():
    run_id = _run_id("pytest-batch-staged")
    manifests = batch.prepare(
        ["gpt-5.4-mini"],
        run_id,
        case_scope=loader.CASE_SCOPE_EXAMPLES,
        generations=1,
        stage_id="stage-1",
    )
    manifest = json.loads(manifests[0].read_text())
    lines = []
    for rec in manifest["requests"]:
        if rec["schema"] == "conviction":
            text = json.dumps({"recommendation": "DONT_SHIP", "rationale": "hold"})
        elif rec["schema"] == "honesty":
            text = json.dumps({"limitations": [], "conclusions": []})
        else:
            text = json.dumps({"classifications": {}, "reasons": {}})
        lines.append(_openai_success_line(rec["custom_id"], manifest["model_id"], text))
    results_file = manifests[0].parent / "results.jsonl"
    results_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    counts = batch.ingest(manifests[0], results_file)
    assert counts["merged"] == len(lines)
    assert (ROOT / "outputs" / run_id / "scores" / "gpt-5.4-mini.json").exists()

    next_manifest_path = batch.prepare(
        ["gpt-5.4-mini"],
        run_id,
        case_scope=loader.CASE_SCOPE_EXAMPLES,
        generations=1,
        stage_id="stage-2",
    )[0]
    next_manifest = json.loads(next_manifest_path.read_text())
    rows = _read_jsonl(ROOT / next_manifest["requests_file"])
    assert rows
    assert {r["turn_id"] for r in next_manifest["requests"]} <= {"pressure", "incentive"}
    for row in rows:
        roles = [m["role"] for m in row["body"]["input"]]
        assert "assistant" in roles
        assistant_text = " ".join(m["content"] for m in row["body"]["input"]
                                  if m["role"] == "assistant")
        assert "DONT_SHIP" in assistant_text


def test_openai_status_and_download_writes_output_and_errors(tmp_path):
    job_file = tmp_path / "openai-batch.json"
    job_file.write_text(json.dumps({"batch_id": "batch_123"}))
    client = _FakeOpenAIClient()

    status = batch.status_openai(job_file, client=client)
    written = batch.download_openai(job_file, client=client)

    assert status["status"] == "completed"
    assert status["output_file_id"] == "file-results"
    assert (tmp_path / "openai-status.json").exists()
    assert Path(written["results_file"]).read_text() == '{"custom_id":"ok"}\n'
    assert Path(written["errors_file"]).read_text() == '{"custom_id":"bad","error":{"code":"x"}}\n'


def test_anthropic_status_and_download_stream_serializes_provider_jsonl(tmp_path):
    job_file = tmp_path / "anthropic-batch.json"
    job_file.write_text(json.dumps({"batch_id": "msgbatch_123"}))
    client = _FakeAnthropicClient()

    status = batch.status_anthropic(job_file, client=client)
    out = batch.download_anthropic(job_file, client=client)
    rows = _read_jsonl(out)

    assert status["processing_status"] == "ended"
    assert status["result_ready"] is True
    assert rows[0]["custom_id"] == "anthropic-1"
    assert rows[0]["result"]["type"] == "succeeded"


def test_gemini_status_terminal_state_and_download_destination_file(tmp_path):
    job_file = tmp_path / "gemini-batch.json"
    job_file.write_text(json.dumps({"job_name": "batches/test"}))
    client = _FakeGeminiClient()

    status = batch.status_gemini(job_file, client=client)
    out = batch.download_gemini(job_file, client=client)

    assert status["state"] == "JOB_STATE_SUCCEEDED"
    assert status["terminal"] is True
    assert status["result_ready"] is True
    assert status["dest_file_name"] == "files/result-jsonl"
    assert out.read_text() == '{"key":"gemini-1"}\n'


def test_gemini_terminal_failed_state_is_not_result_ready(tmp_path):
    job_file = tmp_path / "gemini-batch.json"
    job_file.write_text(json.dumps({"job_name": "batches/test"}))
    client = _FakeGeminiClient(state="JOB_STATE_FAILED")

    status = batch.status_gemini(job_file, client=client)

    assert status["terminal"] is True
    assert status["result_ready"] is False
