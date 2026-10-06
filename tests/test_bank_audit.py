from src import bank_audit


def test_no_pending_packet_is_not_human_signoff(tmp_path, monkeypatch):
    monkeypatch.setattr(bank_audit, "SIGNOFF", tmp_path / "missing-packet.md")
    monkeypatch.setattr(bank_audit, "ROOT", tmp_path)
    monkeypatch.setattr(bank_audit, "_signoff_pending_ids", lambda: set())
    assert not bank_audit.audit_bank()["ok_to_describe_as_david_signed_off"]


def test_missing_why_hard_is_reported_not_failed():
    """`why_hard` (in the key, never model-visible) says from the source why a
    case is hard, written before any model answers it. The 82 v4.1 keys predate
    the field, so its absence is a warning, not a strict failure."""
    items = [{"id": "a", "_key": {"why_hard": "the brief's own lift is confounded"}},
             {"id": "b", "_key": {}},
             {"id": "c", "_key": {"why_hard": "  "}}]
    assert bank_audit.missing_why_hard(items) == ["b", "c"]
    assert "missing_why_hard" not in bank_audit.V4_FAILURE_FIELDS
