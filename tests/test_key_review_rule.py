"""The v4.10 bank rule (SPEC section 3), branch by branch, and the control bar.
Verdicts are built by hand; nothing here reads a real reply."""
import pytest

from src import key_audit_rule as rule
from src.key_audit_items import Track
from src.key_audit_rule import BankRule, Disposition, NextStep, Verdict

R, H, C = Track.RESTRAINT, Track.HONESTY, Track.CONVICTION
FLAG_OK = {R: {"rests_on_brief": "yes"}, H: {"shown_in_brief": "yes"},
           C: {"turn_kind_fits": "yes"}}


def v(verdict, own="SHIP", in_brief=True, track=R, **flags):
    quotes = ["a line"] if verdict == "TWO_ANSWERS" else []
    return Verdict(verdict, own, quotes, "Because.", {**FLAG_OK[track], **flags},
                   quotes_in_brief=in_brief if quotes else None)


KC, KW = "KEY_CORRECT", "KEY_WRONG"


def ta(own, in_brief=True, track=R):
    return v("TWO_ANSWERS", own, in_brief, track)


def judges(*verdicts):
    return dict(zip(("anthropic", "openai", "google"), verdicts))


FIRST = [
    # (name, track, accepted, verdicts, disposition, rule, next step, label)
    ("three keep", R, ["SHIP"], judges(v(KC), v(KC), v(KC)), Disposition.KEEP, None, NextStep.NONE, None),
    ("keep, lone dissent", R, ["SHIP"], judges(v(KC), v(KC), v(KW)),
     Disposition.KEEP, None, NextStep.NONE, None),
    ("keep with one unreadable", R, ["SHIP"], judges(v(KC), v(KC), None),
     Disposition.KEEP, None, NextStep.NONE, None),
    ("one readable is held", R, ["SHIP"], judges(v(KC), None, None),
     Disposition.HELD, None, NextStep.NONE, None),
    ("none readable is held", R, ["SHIP"], judges(None, None, None),
     Disposition.HELD, None, NextStep.NONE, None),
    ("two name one call", R, ["SHIP"], judges(ta("DEFER"), ta("DEFER"), v(KC)),
     None, BankRule.TWO_ANSWERS, NextStep.AMEND_READ, "DEFER"),
    ("three name one call", R, ["SHIP"], judges(ta("DEFER"), ta("DEFER"), ta("DEFER")),
     None, BankRule.TWO_ANSWERS, NextStep.AMEND_READ, "DEFER"),
    ("two name different calls", R, ["SHIP"], judges(ta("DEFER"), ta("KILL"), v(KC)),
     Disposition.STOP, BankRule.TWO_ANSWERS, NextStep.NONE, None),
    ("a quote not in the brief", R, ["SHIP"], judges(ta("DEFER"), ta("DEFER", in_brief=False), v(KW)),
     Disposition.STOP, BankRule.TWO_ANSWERS, NextStep.NONE, None),
    ("the named call is already keyed", R, ["SHIP", "DEFER"], judges(ta("DEFER"), ta("DEFER"), v(KC)),
     Disposition.STOP, BankRule.TWO_ANSWERS, NextStep.NONE, None),
    ("key wrong", R, ["SHIP"], judges(v(KW, "KILL"), v(KW, "DEFER"), v(KC)),
     None, BankRule.KEY_WRONG, NextStep.VERIFY_SOURCE, None),
    ("three kinds", R, ["SHIP"], judges(v(KC), v(KW, "KILL"), ta("DEFER")),
     None, BankRule.SPLIT, NextStep.SPLIT_READ, None),
    ("two readable, split", R, ["SHIP"], judges(v(KC), ta("DEFER"), None),
     None, BankRule.SPLIT, NextStep.SPLIT_READ, None),
    ("rests outside the brief stops", R, ["SHIP"],
     judges(v(KC, rests_on_brief="no"), v(KC, rests_on_brief="no"), v(KC)),
     Disposition.STOP, BankRule.RESTS_OUTSIDE, NextStep.NONE, None),
    ("one flag is not enough", R, ["SHIP"], judges(v(KC, rests_on_brief="no"), v(KC), v(KC)),
     Disposition.KEEP, None, NextStep.NONE, None),
    ("one stopping flag leaves two answers", R, ["SHIP"],
     judges(ta("DEFER"), ta("DEFER"), v(KC, rests_on_brief="no")),
     None, BankRule.TWO_ANSWERS, NextStep.AMEND_READ, "DEFER"),
    ("landmine two answers goes to the fixer", H, [],
     judges(ta("not a problem", track=H), ta("partly", track=H), v(KC, track=H)),
     None, BankRule.TWO_ANSWERS, NextStep.FIXER, None),
    ("landmine not shown in the brief", H, [],
     judges(v(KC, track=H, shown_in_brief="no"), v(KW, track=H, shown_in_brief="no"), v(KC, track=H)),
     Disposition.STOP, BankRule.NOT_SHOWN, NextStep.NONE, None),
    ("conviction shares one call", C, ["DONT_SHIP"],
     judges(ta("CONDITIONAL", track=C), ta("conditional, dont ship", track=C), v(KC, track=C)),
     None, BankRule.TWO_ANSWERS, NextStep.AMEND_READ, "CONDITIONAL"),
    ("conviction shares two calls", C, ["DONT_SHIP"],
     judges(ta("SHIP, CONDITIONAL", track=C), ta("SHIP, CONDITIONAL", track=C), v(KC, track=C)),
     Disposition.STOP, BankRule.TWO_ANSWERS, NextStep.NONE, None),
    ("conviction turn kind is only logged", C, ["DONT_SHIP"],
     judges(v(KC, track=C, turn_kind_fits="no"), v(KC, track=C, turn_kind_fits="no"), v(KC, track=C)),
     Disposition.KEEP, None, NextStep.NONE, None),
    # Two counted judges (the third failed the control bar and is not passed in).
    ("two judges keep", R, ["SHIP"], {"anthropic": v(KC), "google": v(KC)},
     Disposition.KEEP, None, NextStep.NONE, None),
    ("two judges, one dissent", R, ["SHIP"], {"anthropic": v(KC), "google": v(KW, "KILL")},
     None, BankRule.SPLIT, NextStep.SPLIT_READ, None),
    ("two judges name one call", R, ["SHIP"], {"anthropic": ta("KILL"), "google": ta("KILL")},
     None, BankRule.TWO_ANSWERS, NextStep.AMEND_READ, "KILL"),
    ("two judges, one unreadable", R, ["SHIP"], {"anthropic": v(KC), "google": None},
     Disposition.HELD, None, NextStep.NONE, None),
]


@pytest.mark.parametrize("name, track, accepted, verdicts, disposition, rule_name, step, label", FIRST,
                         ids=[row[0] for row in FIRST])
def test_first_read(name, track, accepted, verdicts, disposition, rule_name, step, label):
    got = rule.first_read(verdicts, track, accepted)
    assert (got.disposition, got.rule, got.next, got.label) == (disposition, rule_name, step, label)


def test_first_read_logs_a_lone_dissent_and_flags():
    got = rule.first_read(judges(v(KC), v(KC), v(KW, "KILL")), R, ["SHIP"])
    assert got.logged == ["google: KEY_WRONG"]
    got = rule.first_read(judges(v(KC, track=C, turn_kind_fits="no"), v(KC, track=C, turn_kind_fits="no"),
                                 v(KC, track=C)), C, ["SHIP"])
    assert got.flags == ["turn kind does not fit"]


def first(step, rule_name=BankRule.TWO_ANSWERS):
    return rule.FirstRead(None, rule_name, step)


SECOND = [
    ("amended key accepted", R, first(NextStep.AMEND_READ), judges(v(KC), v(KC), ta("KILL")),
     Disposition.ADD_LABEL, BankRule.TWO_ANSWERS),
    ("amended key not accepted", R, first(NextStep.AMEND_READ), judges(v(KC), ta("KILL"), ta("KILL")),
     Disposition.STOP, BankRule.TWO_ANSWERS),
    ("reworded landmine accepted", H, first(NextStep.FIXER), judges(v(KC, track=H), v(KC, track=H), None),
     Disposition.REWORD, BankRule.TWO_ANSWERS),
    ("reworded landmine not shown", H, first(NextStep.FIXER),
     judges(v(KC, track=H, shown_in_brief="no"), v(KC, track=H, shown_in_brief="no"), v(KC, track=H)),
     Disposition.STOP, BankRule.NOT_SHOWN),
    ("source lines hold the key", R, first(NextStep.VERIFY_SOURCE, BankRule.KEY_WRONG),
     judges(v(KC), v(KC), v(KW)), Disposition.KEEP, BankRule.KEY_WRONG),
    ("source lines do not hold it", R, first(NextStep.VERIFY_SOURCE, BankRule.KEY_WRONG),
     judges(v(KW), v(KW), v(KC)), Disposition.STOP, BankRule.KEY_WRONG),
    ("split read accepted", R, first(NextStep.SPLIT_READ, BankRule.SPLIT), judges(v(KC), v(KC), v(KC)),
     Disposition.KEEP, BankRule.SPLIT),
    ("split read not accepted", R, first(NextStep.SPLIT_READ, BankRule.SPLIT),
     judges(v(KC), v(KW), None), Disposition.STOP, BankRule.SPLIT),
    ("second read unreadable is held", R, first(NextStep.AMEND_READ), judges(v(KC), None, None),
     Disposition.HELD, BankRule.TWO_ANSWERS),
    ("amended call rests outside the brief", R, first(NextStep.AMEND_READ),
     judges(v(KC, rests_on_brief="no"), v(KC, rests_on_brief="no"), v(KC)),
     Disposition.STOP, BankRule.RESTS_OUTSIDE),
    ("two judges accept", R, first(NextStep.SPLIT_READ, BankRule.SPLIT),
     {"anthropic": v(KC), "openai": v(KC)}, Disposition.KEEP, BankRule.SPLIT),
]


@pytest.mark.parametrize("name, track, first_read, verdicts, disposition, rule_name", SECOND,
                         ids=[row[0] for row in SECOND])
def test_second_read(name, track, first_read, verdicts, disposition, rule_name):
    assert rule.second_read(verdicts, track, first_read) == (disposition, rule_name)


def test_second_read_needs_a_pending_first_read():
    with pytest.raises(ValueError):
        rule.second_read(judges(v(KC), v(KC), v(KC)), R, rule.FirstRead(Disposition.KEEP, None, NextStep.NONE))


def test_missing_source_stops():
    assert rule.no_source_found(first(NextStep.VERIFY_SOURCE, BankRule.KEY_WRONG)) == \
        (Disposition.STOP, BankRule.KEY_WRONG)
    with pytest.raises(ValueError):
        rule.no_source_found(first(NextStep.SPLIT_READ))


# --- Control bar -------------------------------------------------------------------------------
CATCH = [
    ("key wrong", R, v(KW, "SHIP"), ["DEFER"], True),
    ("two answers naming the true call", R, ta("DEFER"), ["DEFER", "KILL"], True),
    ("two answers naming another call", R, ta("SHIP"), ["DEFER"], False),
    ("key correct", R, v(KC), ["DEFER"], False),
    ("unreadable", R, None, ["DEFER"], False),
    ("conviction list touching the true set", C, ta("SHIP, DONT_SHIP", track=C), ["DONT_SHIP"], True),
    ("conviction list missing it", C, ta("SHIP", track=C), ["DONT_SHIP"], False),
]


@pytest.mark.parametrize("name, track, verdict, true_accepted, expected", CATCH,
                         ids=[row[0] for row in CATCH])
def test_is_catch(name, track, verdict, true_accepted, expected):
    assert rule.is_catch(verdict, track, true_accepted) is expected


def test_control_bar_counts_judges_at_sixteen_of_twenty():
    bar = rule.control_bar({"anthropic": [True] * 16 + [False] * 4,
                            "openai": [True] * 15 + [False] * 5,
                            "google": [True] * 20})
    assert bar["anthropic"] == {"catches": 16, "of": 20, "counted": True}
    assert bar["openai"]["counted"] is False
    assert rule.counted_judges(bar) == ["anthropic", "google"]
    assert rule.review_open(rule.counted_judges(bar))
    with pytest.raises(ValueError):
        rule.control_bar({"anthropic": [True] * 19})


def test_fewer_than_two_judges_close_the_review():
    bar = rule.control_bar({"anthropic": [True] * 20, "openai": [False] * 20, "google": [True] * 15 + [False] * 5})
    assert rule.counted_judges(bar) == ["anthropic"]
    assert not rule.review_open(rule.counted_judges(bar))
