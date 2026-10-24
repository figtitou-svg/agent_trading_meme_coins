import pytest

from watcher import command_listener
from watcher import config
from watcher import notifier
from watcher import storage


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


# ---- parse_recap_command ----

def test_parse_recap_command_true_for_allowed_chat_and_recap_text():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/recap"}}
    assert command_listener.parse_recap_command(update, "111") is True


def test_parse_recap_command_true_with_trailing_text():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/recap please"}}
    assert command_listener.parse_recap_command(update, "111") is True


def test_parse_recap_command_false_for_different_chat():
    update = {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/recap"}}
    assert command_listener.parse_recap_command(update, "111") is False


def test_parse_recap_command_false_for_other_text():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "salut"}}
    assert command_listener.parse_recap_command(update, "111") is False


def test_parse_recap_command_false_when_no_message_key():
    update = {"update_id": 1, "edited_message": {"chat": {"id": 111}, "text": "/recap"}}
    assert command_listener.parse_recap_command(update, "111") is False


def test_parse_recap_command_false_when_chat_missing():
    update = {"update_id": 1, "message": {"text": "/recap"}}
    assert command_listener.parse_recap_command(update, "111") is False


def test_parse_recap_command_false_when_text_missing():
    update = {"update_id": 1, "message": {"chat": {"id": 111}}}
    assert command_listener.parse_recap_command(update, "111") is False


# ---- parse_sell_command ----

def test_parse_sell_command_returns_argument_for_allowed_chat():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/sell SASS"}}
    assert command_listener.parse_sell_command(update, "111") == "SASS"


def test_parse_sell_command_strips_extra_whitespace():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/sell   SASS  "}}
    assert command_listener.parse_sell_command(update, "111") == "SASS"


def test_parse_sell_command_returns_empty_string_when_no_argument():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/sell"}}
    assert command_listener.parse_sell_command(update, "111") == ""


def test_parse_sell_command_returns_none_for_different_chat():
    update = {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/sell SASS"}}
    assert command_listener.parse_sell_command(update, "111") is None


def test_parse_sell_command_returns_none_for_other_text():
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/recap"}}
    assert command_listener.parse_sell_command(update, "111") is None


def test_parse_sell_command_returns_none_when_no_message_key():
    update = {"update_id": 1, "edited_message": {"chat": {"id": 111}, "text": "/sell SASS"}}
    assert command_listener.parse_sell_command(update, "111") is None


def test_parse_sell_command_does_not_match_unrelated_command_with_same_prefix():
    # "/sellout" ne doit pas matcher "/sell" (juste un préfixe commun).
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/sellout"}}
    assert command_listener.parse_sell_command(update, "111") is None


# ---- handle_sell_command ----

def test_handle_sell_command_sends_usage_when_identifier_empty(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    command_listener.handle_sell_command("")
    assert len(sent) == 1
    assert "Usage" in sent[0]


def test_handle_sell_command_reports_not_found(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    command_listener.handle_sell_command("NOPE")
    assert len(sent) == 1
    assert "Aucune position" in sent[0]


def test_handle_sell_command_disambiguates_multiple_matches(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    storage.open_virtual_position("MintX1", "PoolX1", 1.0, "Same", "SAME")
    with storage._get_conn() as conn:
        pos_id1 = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintX1'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id1, 0.01)
    storage.open_virtual_position("MintX2", "PoolX2", 1.0, "Same", "SAME")
    with storage._get_conn() as conn:
        pos_id2 = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintX2'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id2, 0.02)

    command_listener.handle_sell_command("SAME")
    assert len(sent) == 1
    assert "Plusieurs positions" in sent[0]
    assert "MintX1" in sent[0]
    assert "MintX2" in sent[0]


def test_handle_sell_command_closes_position_by_symbol(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    storage.open_virtual_position("MintSell1", "PoolSell1", 1.0, "SellMe", "SELLME", source="momentum")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintSell1'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    monkeypatch.setattr(
        command_listener.quotes, "fetch_pool_quotes",
        lambda pools: {"poolsell1": {"price": 0.025, "liquidity": 1000.0, "volume_h1": 100.0}},
    )
    command_listener.handle_sell_command("SELLME")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, exit_multiplier FROM positions WHERE id = ?", (pos_id,)
        ).fetchone()
    assert row["status"] == "closed_profit"
    assert row["exit_multiplier"] == pytest.approx(2.5)
    assert len(sent) == 1
    assert "SELLME" in sent[0] or "SellMe" in sent[0]
    assert "Take Profit" in sent[0]


def test_handle_sell_command_closes_position_by_address(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    storage.open_virtual_position("MintSell2", "PoolSell2", 1.0, "X", "XX")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintSell2'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.02)
    monkeypatch.setattr(
        command_listener.quotes, "fetch_pool_quotes",
        lambda pools: {"poolsell2": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 100.0}},
    )
    command_listener.handle_sell_command("MintSell2")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, exit_multiplier FROM positions WHERE id = ?", (pos_id,)
        ).fetchone()
    assert row["status"] == "closed_loss"
    assert row["exit_multiplier"] == pytest.approx(0.5)
    assert len(sent) == 1
    assert "Stop Loss" in sent[0]


def test_handle_sell_command_reports_when_quote_unavailable(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    storage.open_virtual_position("MintSell3", "PoolSell3", 1.0, "Y", "YY")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintSell3'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    monkeypatch.setattr(command_listener.quotes, "fetch_pool_quotes", lambda pools: {})
    command_listener.handle_sell_command("YY")
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (pos_id,)
        ).fetchone()["status"]
    assert status == "open"  # inchangé, pas de clôture sans cotation fiable
    assert len(sent) == 1
    assert "Cotation indisponible" in sent[0]


def test_handle_sell_command_ignores_pending_positions_without_entry_price(monkeypatch):
    """Une position 'pending' (jamais cotée) n'est pas vendable : /sell ne
    doit trouver que des positions 'open'."""
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: sent.append(msg))
    storage.open_virtual_position("MintPend", "PoolPend", 1.0, "Pend", "PEND")
    command_listener.handle_sell_command("PEND")
    assert "Aucune position" in sent[0]


# ---- compute_next_offset ----

def test_compute_next_offset_returns_max_plus_one():
    updates = [{"update_id": 5}, {"update_id": 7}, {"update_id": 6}]
    assert command_listener.compute_next_offset(updates, 0) == 8


def test_compute_next_offset_unchanged_when_no_updates():
    assert command_listener.compute_next_offset([], 42) == 42


def test_compute_next_offset_ignores_non_int_update_id():
    updates = [{"update_id": "not-an-int"}, {"no_id": True}]
    assert command_listener.compute_next_offset(updates, 42) == 42


# ---- fetch_updates ----

def test_fetch_updates_returns_result_list(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": [{"update_id": 1}]}

    monkeypatch.setattr(command_listener.httpx, "get", lambda *a, **k: _FakeResponse())
    assert command_listener.fetch_updates(0) == [{"update_id": 1}]


def test_fetch_updates_returns_empty_list_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("no route")
    monkeypatch.setattr(command_listener.httpx, "get", _raise)
    assert command_listener.fetch_updates(0) == []


def test_fetch_updates_returns_empty_list_on_non_list_result(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": "not-a-list"}

    monkeypatch.setattr(command_listener.httpx, "get", lambda *a, **k: _FakeResponse())
    assert command_listener.fetch_updates(0) == []


# ---- build_and_send_recap ----

def test_build_and_send_recap_sends_message_with_current_state(monkeypatch):
    captured = {}
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: captured.setdefault("msg", msg) or True)

    storage.add_candidate("TOKEN_A", "PAIR_A", "TokenA", "TKA", source="onchain")
    storage.promote_candidate_to_on_curve("TOKEN_A")
    row = storage.get_candidates_by_status("on_curve")[0]
    storage.promote_candidate_to_pending_quote(row["id"], "PAIR_A")

    storage.open_virtual_position("TOKEN_B", "PAIR_B", 1.0, "TokenB", "TKB", source="onchain")

    command_listener.build_and_send_recap()

    assert "msg" in captured
    assert "1 candidat" in captured["msg"]
    assert "TKB" in captured["msg"]
    assert "Patrimoine" in captured["msg"]
    assert "1.00" in captured["msg"]  # invested_eur du portefeuille (1 position à 1€)


def test_build_and_send_recap_shows_current_multiplier_not_just_peak(monkeypatch):
    """Bug du 02/08/2026 : get_active_positions() manquait current_multiplier,
    donc /recap n'affichait jamais 'pic -> actuel (stop)', seulement le pic.
    Exerce le vrai chemin bout en bout (storage réel, pas un dict à la main)."""
    captured = {}
    monkeypatch.setattr(notifier, "send_telegram", lambda msg: captured.setdefault("msg", msg) or True)

    storage.open_virtual_position("TOKEN_LIVE", "PAIR_LIVE", 1.0, "Live", "LIVE", source="momentum")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'TOKEN_LIVE'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    storage.update_peak_multiplier(pos_id, 2.5)
    storage.update_current_multiplier(pos_id, 2.1)

    command_listener.build_and_send_recap()

    assert "pic x2.50" in captured["msg"]
    assert "actuel x2.10" in captured["msg"]
    assert "stop x1.20" in captured["msg"]
