import itertools
import json

import pytest

from src import complete, leaderboard, loader, regrade, stats


def test_exact_test_matches_enumeration():
    a = [{"item": str(i), "sub": "s", "dimension": "honesty",
          "weight": w, "correct": flag}
         for i, (w, flag) in enumerate([(1, True), (2, True), (1, False), (3, True)])]
    b = [dict(r, correct=not r["correct"]) for r in a]
    coeff = [r["weight"] * (1 if r["correct"] else -1) for r in a]
    obs = abs(sum(coeff))
    expected = sum(abs(sum(s*c for s, c in zip(signs, coeff))) >= obs
                   for signs in itertools.product((-1, 1), repeat=len(coeff))) / 2**len(coeff)
    assert stats.paired_exact_p(a, b) == expected
    assert stats.paired_exact_p(b, a) == expected


def test_exact_test_preserves_three_generation_fractions():
    from fractions import Fraction
    a, b = [], []
    specifications = [("restraint", 2, [1, 1, 0], [0, 0, 0]),
                      ("restraint", 1, [0, 0, 0], [1, 0, 0]),
                      ("honesty", 3, [1, 1, 1], [1, 0, 0]),
                      ("conviction", 2, [0, 0, 0], [1, 1, 1])]
    coefficients = []
    for i, (dim, weight, left, right) in enumerate(specifications):
        for target, values in ((a, left), (b, right)):
            target.extend({"item": str(i), "sub": "s", "dimension": dim,
                           "weight": weight, "correct": bool(value)} for value in values)
        denominator = sum(w for d, w, _, _ in specifications if d == dim)
        coefficients.append(Fraction(weight * (sum(left) - sum(right)), 3 * denominator))
    observed = abs(sum(coefficients))
    expected = sum(abs(sum(s * c for s, c in zip(signs, coefficients))) >= observed
                   for signs in itertools.product((-1, 1), repeat=4)) / 16
    assert stats.paired_exact_p(a, b) == expected


def test_candidate_publication_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(complete, "ROOT", tmp_path)
    folder = tmp_path / "outputs" / "candidate"
    folder.mkdir(parents=True)
    (folder / "release.json").write_text('{"status": "candidate"}')
    assert complete.publication_errors("candidate")


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "length", "max_tokens", "incomplete", None])
def test_trace_gate_rejects_truncation(reason):
    assert not complete.trace_complete({"text": '{"answer": 1}', "finish_reason": reason})


@pytest.mark.parametrize("reason", ["STOP", "stop", "end_turn", "completed"])
def test_trace_gate_accepts_completion(reason):
    assert complete.trace_complete({"text": '{"answer": 1}', "finish_reason": reason})


def test_failed_regrade_preserves_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(regrade, "ROOT", tmp_path)
    monkeypatch.setattr(leaderboard, "ROOT", tmp_path)
    item = next(c for c in loader.load_cases(only_examples=True) if c["type"] == "restraint")
    monkeypatch.setattr(loader, "load_cases", lambda **kw: [item])
    folder = tmp_path / "outputs" / "r"
    (folder / "raw").mkdir(parents=True)
    (folder / "scores").mkdir()
    (folder / "raw" / f"mock__{item['id']}.json").write_text(json.dumps(['{"classifications": {}}']))
    score = folder / "scores" / "mock.json"
    score.write_text("original scores")
    monkeypatch.setattr(leaderboard, "write_run_bank_manifest",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("prompt changed")))
    with pytest.raises(ValueError, match="prompt changed"):
        regrade.regrade("r")
    assert score.read_text() == "original scores"
