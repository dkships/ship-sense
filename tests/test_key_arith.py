"""Machine check of arithmetic keys (src.key_arith): the safe evaluator, the
basis_compare shapes the drafts use, literal provenance, and the CLI. Every
case here is synthetic."""
import pytest
import yaml

from src import key_arith as ka
from src.key_arith import ArithError, Kind, Outcome


# --- The evaluator ------------------------------------------------------------------------
@pytest.mark.parametrize("expr, value", [
    ("(530 - 500) / 500", 30 / 500),
    ("2 ** 3", 8),
    ("-(5 - 7)", 2),
    ("1.05 ** (12 / 7) - 1", 1.05 ** (12 / 7) - 1),
    ("abs(3 - 10)", 7),
    ("round(2.567, 2)", 2.57),
    ("min(4, 2, 9) + max(1, 3)", 5),
    ("  10 * (2 + 3)  ", 50),
])
def test_evaluate_allowed_forms(expr, value):
    assert ka.evaluate(expr) == pytest.approx(value)


@pytest.mark.parametrize("expr", [
    "x + 1",                          # a name
    "__import__('os').getcwd()",      # a call outside the list
    "eval('1')",
    "(1).real",                       # an attribute
    "abs.__call__(1)",
    "3 < 4",                          # a comparison
    "(5 < 6) + 1",
    "[1, 2][0]",                      # a subscript and a list
    "'1' + '2'",                      # strings
    "True + 1",                       # a boolean literal
    "round(2.5, ndigits=1)",          # keyword arguments
    "+5",                             # unary plus
    "not 1",
    "~1",
    "7 // 2",                         # floor division and modulo
    "7 % 2",
    "1 << 4",
    "(lambda: 1)()",
    "10 ** 10 ** 10",                 # a runaway power
    "1 / 0",
    "1 +",                            # syntax
    "abs()",
    "1e400",                          # not finite
])
def test_evaluate_refuses_everything_else(expr):
    with pytest.raises(ArithError):
        ka.evaluate(expr)


def test_evaluate_refuses_non_text():
    with pytest.raises(ArithError):
        ka.evaluate(0.5)


def test_literals_keep_repeats():
    assert sorted(ka.literals("(150 - 120) / 150")) == [120, 150, 150]


# --- basis_compare -------------------------------------------------------------------------
@pytest.mark.parametrize("compare, value, expected", [
    ({"claim": 0.2, "tol": 0.02}, 0.25, False),         # no op: within tol
    ({"claim": 0.2, "tol": 0.02}, 0.21, True),
    ({"claim": 3, "op": "abs<=", "tol": 0.01}, 3.0, True),
    ({"claim": 3, "op": "==", "tol": 0}, 0.1 + 2.9, True),
    ({"claim": 0.5, "op": ">"}, 0.2, False),
    ({"claim": 0.05, "op": ">", "tol": 0}, 0.07, True),  # tol ignored on a threshold
    ({"claim": 4, "op": ">="}, 4, True),
    ({"claim": 0.3, "op": "<"}, 0.35, False),
    ({"claim": 10, "op": "<=", "tol": 0}, 14, False),
])
def test_holds_on_every_shape_in_use(compare, value, expected):
    assert ka.holds(value, compare) is expected


@pytest.mark.parametrize("compare", [
    {"claim": 1, "op": "~="},           # not used by any draft, so not supported
    {"claim": 1, "op": "abs<="},        # a tolerance op without tol
    {"claim": 1, "tol": 0.1, "unit": "%"},
    {"tol": 0.1},
    {"claim": "1", "tol": 0},
    "0.5",
])
def test_holds_refuses_unknown_shapes(compare):
    with pytest.raises(ArithError):
        ka.holds(1.0, compare)


# --- Literal provenance --------------------------------------------------------------------
def test_brief_numbers_read_separators_signs_scales_and_words():
    shown = ka.brief_numbers("Revenue $1,700 on 40% margin; ARR $2.5 million; "
                             "spend $64k; three months; a third of users; target $700,000.")
    for n in (1700, 40, 0.4, 2.5, 2_500_000, 64, 64_000, 3, 700_000, 700):
        assert ka._key(n) in shown, n


def test_unseen_literals_allow_only_conversion_constants():
    brief = "Signups were 530 then 500."
    assert ka.unseen_literals("(530 - 500) / 500 * 100", brief) == []
    assert ka.unseen_literals("530 / 500 - 1", brief) == []
    assert ka.unseen_literals("530 * 12 / 52", brief) == []
    assert ka.unseen_literals("(530 - 500) / 41", brief) == [41]


# --- One item -------------------------------------------------------------------------------
BRIEF = "Trial starts: 120 in May (vs. 150 usual). MRR $410,000 then $446,000."


def _claim(label, expr=None, compare=None, text="A claim."):
    return ka.check_item("cc_x", "c1", Kind.CLAIM, label, expr, compare, f"{BRIEF}\n{text}")


def test_claim_label_follows_from_the_comparison():
    ok = _claim("CONTRADICTED", "(150 - 120) / 150", {"claim": 0.5, "op": ">"})
    assert ok.outcome is Outcome.PASS and ok.derived == "CONTRADICTED" and not ok.failed
    ok = _claim("SUPPORTED", "446000 / 410000 - 1", {"claim": 0.05, "op": ">"})
    assert ok.outcome is Outcome.PASS


def test_wrong_label_fails():
    bad = _claim("SUPPORTED", "(150 - 120) / 150", {"claim": 0.5, "op": ">"})
    assert bad.outcome is Outcome.WRONG_LABEL and bad.failed


def test_unseen_number_fails_even_when_the_label_follows():
    bad = _claim("CONTRADICTED", "(150 - 120) / 41", {"claim": 5, "op": ">"})
    assert bad.outcome is Outcome.UNSEEN_NUMBER and bad.unseen == [41] and bad.failed


def test_a_claims_own_figure_may_appear_in_the_expression():
    ok = _claim("SUPPORTED", "150 / 120 - 1.25", {"claim": 0, "op": "abs<=", "tol": 0.001},
                text="Usual trial starts were 1.25 times May's.")
    assert ok.outcome is Outcome.PASS


def test_shape_problems():
    assert _claim("SUPPORTED").outcome is Outcome.NO_EXPR
    assert not _claim("SUPPORTED").failed
    assert _claim("CANNOT_TELL", "150 / 120", {"claim": 1, "op": ">"}).outcome is Outcome.BAD_SHAPE
    assert _claim("SUPPORTED", "150 / 120").outcome is Outcome.BAD_SHAPE
    assert _claim("SUPPORTED", "x / 120", {"claim": 1, "op": ">"}).outcome is Outcome.REFUSED


def test_landmine_figure_must_hold():
    ok = ka.check_item("new_x", "lm", Kind.LANDMINE, None, "120 / 150", {"claim": 0.8, "tol": 0.001}, BRIEF)
    assert ok.outcome is Outcome.PASS and ok.derived == "holds"
    bad = ka.check_item("new_x", "lm", Kind.LANDMINE, None, "120 / 150", {"claim": 0.7, "tol": 0.001}, BRIEF)
    assert bad.outcome is Outcome.WRONG_LABEL


# --- CLI --------------------------------------------------------------------------------------
def _write_case(root, name, claims, status=None):
    case = {"id": name, "type": "claimcheck", "product_key": "Examplecorp", "brief": BRIEF,
            "claims": claims}
    if status:
        case["status"] = status
    (root / "claimcheck").mkdir(parents=True, exist_ok=True)
    (root / "claimcheck" / f"{name}.yaml").write_text(yaml.safe_dump(case))


def test_cli_exit_codes_and_held_scope(tmp_path, capsys):
    good = [{"id": "c1", "text": "t", "label": "CONTRADICTED", "basis_expr": "(150 - 120) / 150",
             "basis_compare": {"claim": 0.5, "op": ">"}}]
    bad = [{"id": "c1", "text": "t", "label": "SUPPORTED", "basis_expr": "(150 - 120) / 150",
            "basis_compare": {"claim": 0.5, "op": ">"}}]
    _write_case(tmp_path, "cc_good", good)
    _write_case(tmp_path, "cc_held_bad", bad, status="held_employer_drive")
    (tmp_path / "dropped" / "claimcheck").mkdir(parents=True)
    (tmp_path / "dropped" / "claimcheck" / "cc_dropped.yaml").write_text("not: [valid")

    assert ka.main(["--drafts", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "cc_good" in out and "cc_held_bad" not in out and "1 cases" in out
    assert ka.main(["--drafts", str(tmp_path), "--include-held"]) == 1
    assert "wrong_label" in capsys.readouterr().out
