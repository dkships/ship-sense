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
from src.providers import GoogleProvider, _json_schema


class _StubSchema:
    """Stand-in for google.genai types.Schema — the SDK isn't a core dep."""

    def __init__(self, type=None, required=None, properties=None, items=None):
        self.type = type
        self.required = required
        self.properties = properties
        self.items = items


class _StubTypes:
    Schema = _StubSchema

    class Type:
        OBJECT = "object"
        ARRAY = "array"
        STRING = "string"


def _stub_to_dict(node):
    out = {"type": node.type}
    if node.required:
        out["required"] = list(node.required)
    if node.properties:
        out["properties"] = {k: _stub_to_dict(v) for k, v in node.properties.items()}
    if node.items is not None:
        out["items"] = _stub_to_dict(node.items)
    return out


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


def test_google_typed_schema_matches_batch_schema():
    """The Gemini live path must send the same effective schema as the batch path
    (the schema the published runs used). Pins the drift class where the typed
    literals and providers._json_schema disagreed on `required`."""
    restraint = next(i for i in loader.load_cases()
                     if i["type"] == "restraint" and i.get("features"))
    for shape, item in [("conviction", None), ("honesty", None), ("restraint", restraint)]:
        typed = GoogleProvider._schema(shape, _StubTypes, item)
        expected = batch._gemini_clean_schema(_json_schema(shape, item))
        assert _stub_to_dict(typed) == expected, shape
    assert GoogleProvider._schema(None, _StubTypes, None) is None


def test_to_plain_serializes_datetime():
    out = batch._to_plain({"created_at": dt.datetime(2026, 6, 30, 12, 0, 0), "n": 1})
    assert out == {"created_at": "2026-06-30T12:00:00", "n": 1}
