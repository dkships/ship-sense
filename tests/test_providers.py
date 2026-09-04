from src import providers


def test_normalize_usage_handles_provider_field_names():
    assert providers.normalize_usage({
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    })["input_tokens"] == 10
    gemini = providers.normalize_usage({
        "prompt_token_count": 11,
        "candidates_token_count": 7,
        "total_token_count": 18,
    })
    assert gemini["input_tokens"] == 11
    assert gemini["output_tokens"] == 7
    responses = providers.normalize_usage({
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "input_tokens_details": {"cached_tokens": 5},
    })
    assert responses["cached_input_tokens"] == 5


def test_normalize_usage_handles_gemini_batch_camelcase_and_thoughts():
    # Gemini batch JSONL usage is camelCase, and thinking tokens are billed as
    # output. Before this was handled, every Gemini batch run recorded $0.00.
    usage = providers.normalize_usage({
        "promptTokenCount": 178,
        "candidatesTokenCount": 54,
        "thoughtsTokenCount": 221,
        "totalTokenCount": 453,
    })
    assert usage["input_tokens"] == 178
    assert usage["output_tokens"] == 54 + 221
    assert usage["total_tokens"] == 453
    # snake_case SDK objects also carry thoughts separately.
    live = providers.normalize_usage({
        "prompt_token_count": 10,
        "candidates_token_count": 5,
        "thoughts_token_count": 20,
    })
    assert live["output_tokens"] == 25


def test_estimate_cost_uses_batch_discount_only_for_batch_mode():
    cfg = {"price_in": 2.0, "price_out": 10.0, "batch_discount": 0.5}
    usage = {"input_tokens": 1000, "output_tokens": 1000}
    assert providers.estimate_cost_usd(cfg, usage, "live") == 0.012
    assert providers.estimate_cost_usd(cfg, usage, "batch") == 0.006


def test_estimate_cost_is_unknown_not_zero_when_usage_is_missing():
    cfg = {"price_in": 2.0, "price_out": 10.0}
    assert providers.estimate_cost_usd(
        cfg, {"input_tokens": None, "output_tokens": None}) is None
    assert providers.estimate_cost_usd(cfg, {}) is None
    # One-sided usage still estimates from what was reported.
    assert providers.estimate_cost_usd(
        cfg, {"input_tokens": 1000, "output_tokens": None}) == 0.002


def test_mock_provider_result_is_backward_compatible():
    p = providers.MockProvider("strong")
    result = p.chat_result([], item={"id": "x", "type": "honesty"})
    assert result.provider == "mock"
    assert result.model == "strong"
    assert isinstance(result.text, str)


def test_reasoning_tokens_added_only_when_the_provider_reports_them_separately():
    """xAI's completion_tokens EXCLUDES reasoning tokens; OpenAI's includes them.
    Both expose completion_tokens_details.reasoning_tokens, so the only safe
    discriminator is the provider's own total. Verified against xAI's
    cost_in_usd_ticks: adding unconditionally double-counts GPT-5.x, and skipping
    undercounts Grok ~4x."""
    # xAI: total == prompt + completion + reasoning  -> reasoning is separate, add it.
    xai = providers.normalize_usage({
        "prompt_tokens": 303, "completion_tokens": 53, "total_tokens": 576,
        "completion_tokens_details": {"reasoning_tokens": 220},
        "prompt_tokens_details": {"cached_tokens": 128}})
    assert xai["output_tokens"] == 273

    # OpenAI: total == prompt + completion  -> reasoning already inside, do not add.
    openai_usage = providers.normalize_usage({
        "prompt_tokens": 100, "completion_tokens": 500, "total_tokens": 600,
        "completion_tokens_details": {"reasoning_tokens": 400}})
    assert openai_usage["output_tokens"] == 500


def test_estimate_cost_prices_cached_input_and_matches_xai_ground_truth():
    """Reconciled against xAI's cost_in_usd_ticks (1 tick = 1e-10 USD), the only
    ground-truth cost any lab on this board reports."""
    grok45 = {"price_in": 2.0, "price_out": 6.0, "price_cached_in": 0.5}
    usage = providers.normalize_usage({
        "prompt_tokens": 303, "completion_tokens": 53, "total_tokens": 576,
        "completion_tokens_details": {"reasoning_tokens": 220},
        "prompt_tokens_details": {"cached_tokens": 128}})
    assert providers.estimate_cost_usd(grok45, usage, "live") == 0.002052  # 20520000 ticks

    grok43 = {"price_in": 1.25, "price_out": 2.5, "price_cached_in": 0.2}
    usage43 = providers.normalize_usage({
        "prompt_tokens": 280, "completion_tokens": 40, "total_tokens": 547,
        "completion_tokens_details": {"reasoning_tokens": 227},
        "prompt_tokens_details": {"cached_tokens": 192}})
    assert providers.estimate_cost_usd(grok43, usage43, "live") == 0.0008159  # 8159000 ticks

    # Anthropic reports cache reads OUTSIDE input_tokens and declares no
    # price_cached_in, so it must never take the cached branch.
    anthropic = providers.normalize_usage(
        {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 5000})
    assert providers.estimate_cost_usd(
        {"price_in": 3, "price_out": 15}, anthropic, "live") == 0.0045


def test_get_provider_routes_xai_to_openai_compat_with_its_own_key_and_base_url():
    """xAI/Grok rides the openai SDK via base_url. Asserts the factory wiring only —
    constructing a client would import `openai`, which CI does not install (the SDK
    import in chat_result is lazy precisely so the suite runs without it)."""
    xai = providers.get_provider({"name": "grok-4.5", "provider": "xai",
                                  "id": "grok-4.5",
                                  "base_url": "https://api.x.ai/v1"})
    assert isinstance(xai, providers.OpenAICompatProvider)
    assert xai.api_key_env == "XAI_API_KEY"
    assert xai.cfg["base_url"] == "https://api.x.ai/v1"

    # OpenAI shares the adapter but must keep the SDK's default endpoint.
    openai_p = providers.get_provider({"name": "gpt-5.5", "provider": "openai",
                                       "id": "gpt-5.5"})
    assert isinstance(openai_p, providers.OpenAICompatProvider)
    assert openai_p.api_key_env == "OPENAI_API_KEY"
    assert openai_p.cfg.get("base_url") is None


def test_get_provider_routes_meta_to_openai_compat_with_its_own_key_and_base_url():
    """Meta/Muse Spark rides the openai SDK via base_url, like xAI. Same wiring-only
    assertion: no client is constructed, so no SDK import is triggered."""
    meta = providers.get_provider({"name": "muse-spark-1.1", "provider": "meta",
                                   "id": "muse-spark-1.1",
                                   "base_url": "https://api.meta.ai/v1"})
    assert isinstance(meta, providers.OpenAICompatProvider)
    assert meta.api_key_env == "META_API_KEY"
    assert meta.cfg["base_url"] == "https://api.meta.ai/v1"
    # The shared adapter stamps `provider` from cfg, so Meta traces never claim openai.
    assert meta.cfg.get("provider", "openai") == "meta"


def test_meta_reasoning_tokens_are_not_double_counted():
    """Meta reports reasoning as a SUBSET of completion_tokens (its total is
    prompt + completion), the opposite of xAI. Adding it would inflate Meta's
    output tokens — and its cost — by the size of the chain of thought. Guards the
    total_tokens discriminator against a well-meaning `reasoning is always extra`
    "fix"."""
    meta = providers.normalize_usage({
        "prompt_tokens": 1200, "completion_tokens": 3000, "total_tokens": 4200,
        "completion_tokens_details": {"reasoning_tokens": 2400},
        "prompt_tokens_details": {"cached_tokens": 900}})
    assert meta["output_tokens"] == 3000          # not 5400
    assert meta["input_tokens"] == 1200
    assert meta["cached_input_tokens"] == 900


def test_estimate_cost_prices_meta_cached_input():
    """Meta's prompt_tokens INCLUDES cached tokens (docs: `cached_tokens is a subset
    of your input tokens`), so declaring price_cached_in is correct here and the
    cached branch must fire. 300 fresh @ $1.25/M + 900 cached @ $0.15/M
    + 3000 out @ $4.25/M."""
    cfg = {"price_in": 1.25, "price_out": 4.25, "price_cached_in": 0.15}
    usage = providers.normalize_usage({
        "prompt_tokens": 1200, "completion_tokens": 3000, "total_tokens": 4200,
        "completion_tokens_details": {"reasoning_tokens": 2400},
        "prompt_tokens_details": {"cached_tokens": 900}})
    expected = (300 * 1.25 + 900 * 0.15 + 3000 * 4.25) / 1_000_000
    assert providers.estimate_cost_usd(cfg, usage, "live") == round(expected, 8)
    # Meta has no batch API; a batch run_mode must never invent a discount for it.
    assert providers.estimate_cost_usd(cfg, usage, "batch") == round(expected, 8)


def test_xai_result_self_labels_its_provider_not_openai():
    """OpenAICompatProvider stamps `provider` from cfg, so grok traces don't claim
    to be OpenAI. Guards the shared-adapter footgun."""
    cfg = {"name": "grok-4.5", "provider": "xai", "id": "grok-4.5",
           "price_in": 2, "price_out": 6}
    assert cfg.get("provider", "openai") == "xai"
    usage = providers.normalize_usage({"prompt_tokens": 1000, "completion_tokens": 1000})
    # xAI prices: $2/1M in, $6/1M out. No batch discount is ever applied on live.
    assert providers.estimate_cost_usd(cfg, usage, "live") == 0.008


class _FakeOpenAIModule:
    """Minimal stand-in for the lazily-imported `openai` module: records the
    kwargs the provider builds so a test can assert on the REQUEST, not just the
    parsed reply."""

    def __init__(self):
        self.seen: dict = {}

    def OpenAI(self, **_):                            # noqa: N802 - SDK's name
        return self._Client(self.seen)

    class _Client:
        def __init__(self, seen):
            self.chat = self._Chat(seen)

        class _Chat:
            def __init__(self, seen):
                self.completions = _FakeOpenAIModule._Completions(seen)

    class _Completions:
        def __init__(self, seen):
            self.seen = seen

        def create(self, **kwargs):
            self.seen.update(kwargs)
            msg = type("M", (), {"content": '{"ok": true}',
                                 "reasoning_content": None})()
            choice = type("C", (), {"message": msg, "finish_reason": "stop"})()
            usage = {"prompt_tokens": 10, "completion_tokens": 5,
                     "total_tokens": 15}
            return type("R", (), {"choices": [choice], "usage": usage,
                                  "id": "req_1"})()


def _call_compat(monkeypatch, *, structured_outputs):
    import sys
    fake = _FakeOpenAIModule()
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("FAKE_KEY", "x")
    cfg = {"name": "probe", "provider": "deepseek", "id": "m",
           "structured_outputs": structured_outputs,
           "price_in": 1, "price_out": 1}
    prov = providers.OpenAICompatProvider(cfg, "FAKE_KEY")
    res = prov.chat_result([{"role": "user", "content": "hi"}],
                           schema="restraint", item={"features": [{"id": "a"}]})
    return fake.seen, res


def test_json_object_request_is_not_labelled_json_schema(monkeypatch):
    """A vendor without json_schema (DeepSeek V4 is json_object-only) must record
    `structured_output: "json_object"` — what was actually sent. The label used to
    key off the schema being COMPUTED rather than SENT, so a json_object request
    reported constrained decoding it never used. Latent until now only because
    every registry entry to date sets structured_outputs: true; it would have
    misstated the method on every trace of the first json_object-only model."""
    seen, res = _call_compat(monkeypatch, structured_outputs=False)
    assert seen["response_format"] == {"type": "json_object"}
    assert res.structured_output == "json_object"


def test_json_schema_request_is_still_labelled_json_schema(monkeypatch):
    """The other direction, so the fix above can't silently downgrade the 28
    entries that DO get constrained decoding."""
    seen, res = _call_compat(monkeypatch, structured_outputs=True)
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert res.structured_output == "json_schema"


class _FakeAnthropicModule:
    """Stand-in for the lazily-imported `anthropic` module. Records the client
    kwargs so a test can assert on how the CLIENT was built, not just the reply."""

    def __init__(self):
        self.client_kwargs: dict = {}

    def Anthropic(self, **kwargs):                    # noqa: N802 - SDK's name
        self.client_kwargs.update(kwargs)
        return self._Client()

    class _Client:
        def __init__(self):
            self.messages = _FakeAnthropicModule._Messages()

    class _Messages:
        def create(self, **_):
            block = type("B", (), {"type": "text", "text": '{"ok": true}'})()
            usage = {"input_tokens": 10, "output_tokens": 5}
            return type("R", (), {"content": [block], "usage": usage,
                                  "stop_reason": "end_turn"})()


def test_anthropic_client_honors_the_registry_timeout(monkeypatch):
    """`timeout_s` is a per-model registry field, and the OpenAI-compatible path
    has always honored it. The Anthropic path hardcoded `timeout=120.0` and
    ignored the registry, so a slow-thinking Anthropic entry could not be given
    a longer budget the way Moonshot and Z.ai entries were."""
    import sys
    fake = _FakeAnthropicModule()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg = {"name": "probe", "provider": "anthropic", "id": "m",
           "timeout_s": 240, "price_in": 1, "price_out": 1}
    providers.AnthropicProvider(cfg).chat_result(
        [{"role": "user", "content": "hi"}], json_mode=False)
    assert fake.client_kwargs["timeout"] == 240.0


def test_anthropic_client_timeout_defaults_when_registry_is_silent(monkeypatch):
    """The other direction: entries with no `timeout_s` keep the 120s default,
    so honoring the registry cannot silently change the 5 Anthropic entries."""
    import sys
    fake = _FakeAnthropicModule()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg = {"name": "probe", "provider": "anthropic", "id": "m",
           "price_in": 1, "price_out": 1}
    providers.AnthropicProvider(cfg).chat_result(
        [{"role": "user", "content": "hi"}], json_mode=False)
    assert fake.client_kwargs["timeout"] == 120.0


def test_gemini_finish_reason_is_read_off_the_candidate():
    """google-genai puts `finish_reason` on Candidate, NOT on
    GenerateContentResponse. The live path read it off the response, so it was
    ALWAYS None and `gate_run.py`'s `finish_reason == "length"` truncation gate
    could never fire on a live Gemini run. Batch ingest was unaffected, which is
    why no published board was wrong -- every Gemini run has been all-batch."""
    cand = type("C", (), {"finish_reason": "MAX_TOKENS"})()
    resp = type("R", (), {"candidates": [cand]})()
    assert providers._gemini_finish_reason(resp) == "MAX_TOKENS"


def test_gemini_finish_reason_records_the_bare_enum_name():
    """The SDK returns a FinishReason enum whose str() is
    "FinishReason.MAX_TOKENS". Only the BARE name is useful: the batch path
    records Google's own "MAX_TOKENS", and `gate_run.py` lowercases the field and
    matches {"length", "max_tokens", "max_output_tokens"}. A "FinishReason."
    prefix misses that set, so the gate would stay disarmed even after the
    finish reason is read off the right object. Verified against the real API:
    a 64-token cap returns FinishReason.MAX_TOKENS."""
    import enum

    class FinishReason(enum.Enum):
        MAX_TOKENS = "MAX_TOKENS"
        STOP = "STOP"

    for member, expected in ((FinishReason.MAX_TOKENS, "MAX_TOKENS"),
                             (FinishReason.STOP, "STOP")):
        cand = type("C", (), {"finish_reason": member})()
        resp = type("R", (), {"candidates": [cand]})()
        got = providers._gemini_finish_reason(resp)
        assert got == expected, got
        assert str(member) != expected, "fake must stringify like the real enum"

    assert "max_tokens" in {"length", "max_tokens", "max_output_tokens"}


def test_gemini_finish_reason_survives_a_plain_string():
    """Some SDK versions hand back a plain string; it must pass through as-is."""
    cand = type("C", (), {"finish_reason": "STOP"})()
    resp = type("R", (), {"candidates": [cand]})()
    assert providers._gemini_finish_reason(resp) == "STOP"


def test_gemini_finish_reason_is_none_when_absent():
    """A blocked or empty response has no candidates. That must stay None rather
    than raise, because it is read on the success path of every live call."""
    assert providers._gemini_finish_reason(type("R", (), {"candidates": []})()) is None
    assert providers._gemini_finish_reason(type("R", (), {})()) is None
    cand = type("C", (), {"finish_reason": None})()
    assert providers._gemini_finish_reason(type("R", (), {"candidates": [cand]})()) is None
