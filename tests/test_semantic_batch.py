"""Adversarial workflow checks use fake results and never contact providers."""
import json
from pathlib import Path

import pytest

from src import semantic_audit as sa, semantic_batch as sb, semantic_grade as sg


@pytest.mark.parametrize("text", ['{"checks": [], "checks": []}', '{"cost": NaN}', '{"cost": Infinity}'])
def test_ambiguous_or_nonfinite_json_is_rejected(text):
    with pytest.raises(ValueError):
        sg.parse_response(text)


def test_repeated_provider_cannot_form_panel():
    with pytest.raises(ValueError, match="provider"):
        sg.panel_vote({"openai": {"verdict": "pass", "issues": []},
                       "anthropic": {"verdict": "pass", "issues": []}},
                      ("openai", "openai", "anthropic"))


def test_failed_screen_cannot_trigger_any_full_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "verify", lambda _: {})
    monkeypatch.setattr(sa, "analyze_screen", lambda _: {"passed": False})
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("No client may be created"))
    with pytest.raises(ValueError, match="failed adversarial"):
        sb.submit(tmp_path, "full")


def test_unresolved_attempt_blocks_all_new_submissions(tmp_path, monkeypatch):
    from src import semantic_budget as money
    monkeypatch.setattr(sb, "verify", lambda _: {"submit_before": "9999-12-31"})
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    monkeypatch.setattr(money, "account", lambda _: {"waves": []})
    monkeypatch.setattr(sb, "_credentials", lambda: None)
    monkeypatch.setattr(sb, "_client", lambda _: pytest.fail("No new paid request"))
    folder = tmp_path / "waves/0000/anthropic"
    folder.mkdir(parents=True)
    (folder / "submission-attempt.json").write_text("{}")
    with pytest.raises(RuntimeError, match="Unresolved submission"):
        sb.submit(tmp_path, "screen")
    assert not (tmp_path / "waves/0000/openai/submission-attempt.json").exists()


def test_sealed_input_tamper_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    (tmp_path / "requests.jsonl").write_text("original")
    sb.write(tmp_path / "seal.json", {"pack_path": ".", "files": {"requests.jsonl": sb.sha(tmp_path / "requests.jsonl")}})
    (tmp_path / "requests.jsonl").write_text("changed")
    with pytest.raises(ValueError, match="Sealed input changed"):
        sb.verify(tmp_path)


def test_seal_cannot_validate_a_different_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    elsewhere = tmp_path / "elsewhere"
    sb.write(elsewhere / "seal.json", {"pack_path": "original", "files": {}})
    sb.write(elsewhere / "config.json", {"total_cap_usd": 10, "screen_cap_usd": 10})
    sb.write(elsewhere / "budget.json", {"total_bound_usd": 1, "phases": {"screen": {"bound_usd": 1}}})
    with pytest.raises(ValueError, match="pack location"):
        sb.verify(elsewhere)


def test_unknown_check_keeps_full_score_denominator():
    fixed = [{"item": "r", "dimension": "restraint", "weight": 1.0, "correct": True},
             {"item": "c", "dimension": "conviction", "weight": 1.0, "correct": False}]
    semantic = [{"item": "h", "dimension": "honesty", "weight": 1.0, "verdict": v}
                for v in ["pass", "unresolved", "fail"]]
    bounds = sa.score_bounds(fixed, semantic)
    assert bounds["lower_assignment"]["value"] == pytest.approx(100 * (1 + 1/3) / 3)
    assert bounds["upper_assignment"]["value"] == pytest.approx(100 * (1 + 2/3) / 3)
    assert bounds["honesty_checks"] == 3
    assert bounds["unresolved"] == 1


def _screen_fixture():
    criterion = {"id": "c", "kind": "landmine", "reference": "A concern"}
    def record(rid, category):
        return {"id": rid, "category": category, "case": "example", "checks": {"c": "landmine:concern"},
                "payload": {"criteria": [criterion]}}
    source = record("source", "source")
    real = record("real", "real")
    variant = {**record("variant", "metamorphic"), "parent": "real", "variant": "reordered"}
    good = {**record("good", "control"), "expected": {"c": "pass"}, "family": "causality"}
    bad = {**record("bad", "control"), "expected": {"c": "fail"}, "family": "causality"}
    records = [source, real, variant, good, bad]
    outputs = {}
    for provider in sg.PROVIDERS:
        outputs[provider] = {r["id"]: {"votes": {"c": {"verdict": "fail" if r["id"] == "bad" else "pass", "issues": []}},
            "raw_decisions": [{"id": "c", "key_supported": "yes"}], "extra_findings": []} for r in records}
    return records, outputs


def test_perfect_fake_transport_passes_mechanics_only():
    metrics = sa.screen_metrics(*_screen_fixture())
    assert metrics["passed"]
    assert metrics["ground_truth_accuracy_established"] is False


@pytest.mark.parametrize("impostor", ["pass", "fail", "unresolved"])
def test_constant_judge_cannot_pass(impostor):
    records, outputs = _screen_fixture()
    for checked in outputs["google"].values():
        checked["votes"]["c"]["verdict"] = impostor
    assert not sa.screen_metrics(records, outputs)["passed"]


def test_style_flip_blocks_expansion_even_with_perfect_controls():
    records, outputs = _screen_fixture()
    outputs["anthropic"]["variant"]["votes"]["c"]["verdict"] = "fail"
    assert not sa.screen_metrics(records, outputs)["passed"]


def test_source_dispute_is_common_to_every_answer():
    records, outputs = _screen_fixture()
    outputs["openai"]["source"]["raw_decisions"][0]["key_supported"] = "uncertain"
    uncertain = sa.source_quarantine(records, outputs)
    assert uncertain == {("example", "landmine:concern")}
    assert sa._panel(records[1], "c", outputs, uncertain) == "unresolved"
    assert not sa.screen_metrics(records, outputs)["passed"]


def test_provider_requests_hide_expected_labels_and_metadata():
    payload = {"brief": "A brief", "statements": [], "criteria": []}
    for provider in sg.PROVIDERS:
        request = sb.make_request(provider, "grader-id", "opaque-id", payload, 1000)
        text = json.dumps(request)
        assert "expected" not in text
        assert "previous_score" not in text
        assert "author_model" not in text
