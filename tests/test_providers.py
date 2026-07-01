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


def test_estimate_cost_uses_batch_discount_only_for_batch_mode():
    cfg = {"price_in": 2.0, "price_out": 10.0, "batch_discount": 0.5}
    usage = {"input_tokens": 1000, "output_tokens": 1000}
    assert providers.estimate_cost_usd(cfg, usage, "live") == 0.012
    assert providers.estimate_cost_usd(cfg, usage, "batch") == 0.006


def test_mock_provider_result_is_backward_compatible():
    p = providers.MockProvider("strong")
    result = p.chat_result([], item={"id": "x", "type": "honesty"})
    assert result.provider == "mock"
    assert result.model == "strong"
    assert isinstance(result.text, str)
