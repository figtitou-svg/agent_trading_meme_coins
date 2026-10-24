import pytest
import requests

from watcher import config
from watcher import security_check


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _report(mint_authority=None, freeze_authority=None, top_holder_pct=5.0):
    return {
        "token": {
            "mintAuthority": mint_authority,
            "freezeAuthority": freeze_authority,
        },
        "topHolders": [{"pct": top_holder_pct}],
    }


def test_audit_token_passes_when_authorities_revoked_and_holders_spread(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(mint_authority=None, freeze_authority=None)),
    )
    is_safe, reasons = security_check.audit_token("SoLmintOk")
    assert is_safe is True
    assert reasons == []


def test_audit_token_blocks_when_mint_authority_active(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(mint_authority="SomeAuthorityPubkey")),
    )
    is_safe, reasons = security_check.audit_token("SoLmintBad")
    assert is_safe is False
    assert any("mint" in r.lower() for r in reasons)


def test_audit_token_blocks_when_freeze_authority_active(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(freeze_authority="SomeAuthorityPubkey")),
    )
    is_safe, reasons = security_check.audit_token("SoLmintBad2")
    assert is_safe is False
    assert any("freeze" in r.lower() for r in reasons)


def test_audit_token_blocks_when_top_holder_concentration_too_high(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(top_holder_pct=config.MAX_TOP_HOLDER_PERCENT + 1)),
    )
    is_safe, reasons = security_check.audit_token("SoLmintBad3")
    assert is_safe is False
    assert any("holder" in r.lower() for r in reasons)


def test_audit_token_uses_default_threshold_when_no_override_given(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(top_holder_pct=config.MAX_TOP_HOLDER_PERCENT + 1)),
    )
    is_safe, reasons = security_check.audit_token("SoLmintNoOverride")
    assert is_safe is False
    assert f"> {config.MAX_TOP_HOLDER_PERCENT}%" in reasons[0]


def test_audit_token_custom_threshold_passes_where_default_would_reject(monkeypatch):
    """Un holder à 45% échoue le seuil par défaut (30%) mais passe avec un
    seuil personnalisé plus permissif (60%) — cas telegram."""
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(top_holder_pct=45.0)),
    )
    is_safe_default, _ = security_check.audit_token("SoLmintCustom1")
    assert is_safe_default is False

    is_safe_custom, reasons_custom = security_check.audit_token("SoLmintCustom1", max_top_holder_percent=60.0)
    assert is_safe_custom is True
    assert reasons_custom == []


def test_audit_token_custom_threshold_still_rejects_above_it(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report(top_holder_pct=75.0)),
    )
    is_safe, reasons = security_check.audit_token("SoLmintCustom2", max_top_holder_percent=60.0)
    assert is_safe is False
    assert "> 60.0%" in reasons[0]


def test_audit_token_fails_closed_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(security_check._session, "get", _raise)
    monkeypatch.setattr(config, "RUGCHECK_RETRIES", 1)
    monkeypatch.setattr(config, "RUGCHECK_RETRY_DELAY", 0)
    is_safe, reasons = security_check.audit_token("SoLmintUnreachable")
    assert is_safe is False
    assert len(reasons) == 1


def test_audit_token_fails_closed_when_token_not_indexed(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse({}, status_code=404),
    )
    monkeypatch.setattr(config, "RUGCHECK_RETRIES", 1)
    monkeypatch.setattr(config, "RUGCHECK_RETRY_DELAY", 0)
    is_safe, reasons = security_check.audit_token("SoLmintNotIndexed")
    assert is_safe is False


def test_audit_token_fails_closed_when_report_is_not_a_dict(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse([1, 2, 3]),
    )
    is_safe, reasons = security_check.audit_token("SoLmintMalformed1")
    assert is_safe is False
    assert len(reasons) == 1


def test_audit_token_fails_closed_when_token_field_is_not_a_dict(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse({"token": "not_a_dict", "topHolders": [{"pct": 5.0}]}),
    )
    is_safe, reasons = security_check.audit_token("SoLmintMalformed2")
    assert is_safe is False
    assert len(reasons) == 1


def test_audit_token_fails_closed_when_top_holder_entry_is_not_a_dict(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse(_report() | {"topHolders": ["not_a_dict"]}),
    )
    is_safe, reasons = security_check.audit_token("SoLmintMalformed3")
    assert is_safe is False
    assert len(reasons) == 1


def test_audit_token_fails_closed_when_token_field_is_missing(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse({"topHolders": [{"pct": 5.0}]}),
    )
    is_safe, reasons = security_check.audit_token("SoLmintNoTokenField")
    assert is_safe is False
    assert any("autorit" in r.lower() for r in reasons)


def test_audit_token_fails_closed_when_token_field_is_null(monkeypatch):
    monkeypatch.setattr(
        security_check._session, "get",
        lambda *a, **k: _FakeResponse({"token": None, "topHolders": [{"pct": 5.0}]}),
    )
    is_safe, reasons = security_check.audit_token("SoLmintNullTokenField")
    assert is_safe is False
    assert any("autorit" in r.lower() for r in reasons)
