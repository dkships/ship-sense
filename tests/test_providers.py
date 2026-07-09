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
