"""Regression tests for the batch-path schema/serialization fixes.

These pin bugs that surfaced only when the provider batch adapters were first run
end-to-end against the live APIs:

  1. `providers._json_schema` emitted schemas that Anthropic and OpenAI-strict
     structured outputs reject (`additionalProperties: true`; `required` missing
     keys). It must be strict-valid: every object sets `additionalProperties:
     false` and lists all properties in `required`.
  2. Gemini's OpenAPI-subset `response_schema` rejects `additionalProperties`
     outright; `batch._gemini_clean_schema` must strip it recursively.
  3. `batch._to_plain` must serialize `datetime` fields on provider batch-status
     objects instead of raising on `vars()`.
"""
import datetime as dt

from src import batch, loader
from src.providers import _json_schema


def _assert_strict(node, path="root"):
    if isinstance(node, dict) and node.get("type") == "object":
        props = node.get("properties", {})
        assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be False"
        assert set(node.get("required", [])) == set(props), f"{path}: required must list every property"
        for k, v in props.items():
            _assert_strict(v, f"{path}.{k}")
    elif isinstance(node, dict) and node.get("type") == "array":
        _assert_strict(node.get("items", {}), f"{path}[]")


def test_json_schema_is_strict_valid_for_every_shape():
    restraint = next(i for i in loader.load_cases() if i["type"] == "restraint" and i.get("features"))
    for shape, item in [("conviction", None), ("honesty", None), ("restraint", restraint)]:
        schema = _json_schema(shape, item)
        assert schema is not None
        _assert_strict(schema, shape)


def test_gemini_clean_schema_strips_additional_properties():
    schema = _json_schema("restraint",
                          next(i for i in loader.load_cases()
                               if i["type"] == "restraint" and i.get("features")))
    cleaned = batch._gemini_clean_schema(schema)
    assert "additionalProperties" not in _flatten_keys(cleaned)
    # required is preserved (Gemini tolerates it)
    assert "required" in cleaned


def _flatten_keys(node):
    keys = set()
    if isinstance(node, dict):
        keys |= set(node.keys())
        for v in node.values():
            keys |= _flatten_keys(v)
    elif isinstance(node, list):
        for v in node:
            keys |= _flatten_keys(v)
    return keys


def test_to_plain_serializes_datetime():
    out = batch._to_plain({"created_at": dt.datetime(2026, 6, 30, 12, 0, 0), "n": 1})
    assert out == {"created_at": "2026-06-30T12:00:00", "n": 1}
