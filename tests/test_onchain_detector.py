import pytest

from watcher import config
from watcher import storage
from watcher.detectors import onchain_detector


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def test_parse_new_coin_event_extracts_fields():
    payload = {"mint": "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "name": "DogWifCap", "symbol": "DWC"}
    result = onchain_detector.parse_new_coin_event(payload)
    assert result == ("8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "DogWifCap", "DWC")


def test_parse_new_coin_event_returns_none_when_mint_missing():
    assert onchain_detector.parse_new_coin_event({"name": "NoMint"}) is None


def test_parse_new_coin_event_handles_missing_name_symbol():
    payload = {"mint": "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH"}
    result = onchain_detector.parse_new_coin_event(payload)
    assert result == ("8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", None, None)


def test_handle_new_coin_adds_candidate_with_onchain_source():
    payload = {"mint": "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "name": "DogWifCap", "symbol": "DWC"}
    onchain_detector.handle_new_coin(payload)
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, token_name, token_symbol FROM candidates WHERE token_address = ?",
            ("8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH",),
        ).fetchone()
    assert row["source"] == "onchain"
    assert row["token_name"] == "DogWifCap"
    assert row["token_symbol"] == "DWC"


def test_handle_new_coin_does_nothing_when_mint_missing():
    onchain_detector.handle_new_coin({"name": "NoMint"})
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_never_raises_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("no route")
    monkeypatch.setattr(onchain_detector.httpx, "get", _raise)
    onchain_detector.poll_once()  # ne doit pas lever


def test_poll_once_requests_the_verified_coins_listing_endpoint(monkeypatch):
    """Endpoint vérifié en direct le 2026-07-26 : GET /coins?limit=...&sort=
    created_timestamp&order=DESC (pas /coins/new, qui renvoie du 530)."""
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def _fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(onchain_detector.httpx, "get", _fake_get)
    onchain_detector.poll_once()
    assert captured["url"].startswith(f"{config.PUMPFUN_FRONTEND_API}/coins?")
    assert "sort=created_timestamp" in captured["url"]
    assert "order=DESC" in captured["url"]
    assert "limit=" in captured["url"]
    assert "/coins/new" not in captured["url"]


def test_poll_once_processes_new_coins_from_api(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"mint": "9yPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXhI", "name": "N", "symbol": "S"}]

    monkeypatch.setattr(onchain_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    onchain_detector.poll_once()
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1


def test_poll_once_handles_non_list_json_response(monkeypatch):
    for bad_json in ({"unexpected": "dict"}, "just a string", None, 42):
        class _FakeResponse:
            def raise_for_status(self):
                pass

            def json(self, _bad=bad_json):
                return _bad

        monkeypatch.setattr(onchain_detector.httpx, "get", lambda *a, **k: _FakeResponse())
        onchain_detector.poll_once()  # ne doit pas lever, quel que soit le type
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_handles_non_dict_items_in_list(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return ["not a dict", 123, None, ["nested", "list"]]

    monkeypatch.setattr(onchain_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    onchain_detector.poll_once()  # ne doit pas lever
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_skips_bad_items_but_processes_good_ones(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                "garbage",
                {"mint": "9yPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXhI", "name": "N", "symbol": "S"},
                None,
            ]

    monkeypatch.setattr(onchain_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    onchain_detector.poll_once()
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1


def test_parse_new_coin_event_returns_none_when_mint_is_empty_string():
    assert onchain_detector.parse_new_coin_event({"mint": "", "name": "Empty"}) is None


def test_parse_new_coin_event_returns_none_when_mint_is_not_a_string():
    assert onchain_detector.parse_new_coin_event({"mint": 12345, "name": "NotAString"}) is None


def test_handle_new_coin_does_nothing_when_mint_is_empty_string():
    onchain_detector.handle_new_coin({"mint": "", "name": "Empty"})
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_new_coin_does_nothing_when_mint_is_not_a_string():
    onchain_detector.handle_new_coin({"mint": 12345, "name": "NotAString"})
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_isolates_bad_value_type_so_later_good_items_survive(monkeypatch):
    """
    Un item est un dict de forme correcte (mint présent, string) mais avec une
    valeur imbriquée du mauvais type (symbol=liste au lieu de string) : ça
    passe le contrôle isinstance(payload, dict) et parse_new_coin_event, mais
    fait échouer storage.add_candidate (sqlite3.ProgrammingError, un type
    liste n'est pas bindable). Sans isolation par item, cette exception
    remonte hors de la boucle for et le bon item suivant n'est jamais traité.
    """
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"mint": "8xNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXgH", "name": "Bad", "symbol": ["A", "B"]},
                {"mint": "9yPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXhI", "name": "Good", "symbol": "GOOD"},
            ]

    monkeypatch.setattr(onchain_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    onchain_detector.poll_once()  # ne doit pas lever
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1
