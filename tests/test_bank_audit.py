from src import bank_audit


def test_no_pending_packet_is_not_human_signoff(tmp_path, monkeypatch):
    monkeypatch.setattr(bank_audit, "SIGNOFF", tmp_path / "missing-packet.md")
    monkeypatch.setattr(bank_audit, "ROOT", tmp_path)
    monkeypatch.setattr(bank_audit, "_signoff_pending_ids", lambda: set())
    assert not bank_audit.audit_bank()["ok_to_describe_as_david_signed_off"]
