import pytest

from watcher import config
from watcher import storage
from watcher.detectors import momentum_detector


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def _pool_payload(price_change_m5="20.0", volume_m5="5000.0",
                   pool_address="PoolAddrABC", mint="MintXYZAbcdefghijklmnop",
                   name="MOONDOGE / SOL"):
    return {
        "attributes": {
            "address": pool_address,
            "name": name,
            "price_change_percentage": {"m5": price_change_m5},
            "volume_usd": {"m5": volume_m5},
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{mint}"}},
        },
    }


def test_parse_trending_pool_extracts_fields():
    result = momentum_detector.parse_trending_pool(_pool_payload())
    assert result == ("MintXYZAbcdefghijklmnop", "PoolAddrABC", "MOONDOGE", 20.0, 5000.0)


def test_parse_trending_pool_handles_name_without_separator():
    payload = _pool_payload(name="NoSeparatorHere")
    result = momentum_detector.parse_trending_pool(payload)
    assert result[2] == "NoSeparatorHere"


def test_parse_trending_pool_returns_none_when_pool_address_missing():
    payload = _pool_payload()
    del payload["attributes"]["address"]
    assert momentum_detector.parse_trending_pool(payload) is None


def test_parse_trending_pool_returns_none_when_base_token_id_not_solana():
    payload = _pool_payload()
    payload["relationships"]["base_token"]["data"]["id"] = "ethereum_0xabc"
    assert momentum_detector.parse_trending_pool(payload) is None


def test_parse_trending_pool_returns_none_when_price_change_missing():
    payload = _pool_payload()
    del payload["attributes"]["price_change_percentage"]["m5"]
    assert momentum_detector.parse_trending_pool(payload) is None


def test_parse_trending_pool_returns_none_on_malformed_payload():
    assert momentum_detector.parse_trending_pool({}) is None
    assert momentum_detector.parse_trending_pool({"attributes": None}) is None


def test_handle_trending_pool_adds_candidate_above_thresholds(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_MIN_PRICE_CHANGE_PCT", 15.0)
    monkeypatch.setattr(config, "MOMENTUM_MIN_VOLUME_USD", 2000.0)
    momentum_detector.handle_trending_pool(_pool_payload(price_change_m5="20", volume_m5="5000"))
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, token_name, pair_address FROM candidates WHERE token_address = ?",
            ("MintXYZAbcdefghijklmnop",),
        ).fetchone()
    assert row["source"] == "momentum"
    assert row["token_name"] == "MOONDOGE"
    assert row["pair_address"] == "PoolAddrABC"


def test_handle_trending_pool_skips_when_price_change_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_MIN_PRICE_CHANGE_PCT", 15.0)
    monkeypatch.setattr(config, "MOMENTUM_MIN_VOLUME_USD", 2000.0)
    momentum_detector.handle_trending_pool(_pool_payload(price_change_m5="5", volume_m5="5000"))
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_trending_pool_skips_when_volume_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_MIN_PRICE_CHANGE_PCT", 15.0)
    monkeypatch.setattr(config, "MOMENTUM_MIN_VOLUME_USD", 2000.0)
    momentum_detector.handle_trending_pool(_pool_payload(price_change_m5="20", volume_m5="500"))
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_trending_pool_does_nothing_on_unparseable_payload():
    momentum_detector.handle_trending_pool({"attributes": {}})
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_never_raises_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("no route")
    monkeypatch.setattr(momentum_detector.httpx, "get", _raise)
    momentum_detector.poll_once()  # ne doit pas lever


def test_poll_once_requests_trending_pools_endpoint(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": []}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(momentum_detector.httpx, "get", _fake_get)
    momentum_detector.poll_once()
    assert captured["url"].startswith(f"{config.GECKOTERMINAL_API_BASE}/networks/solana/trending_pools")
    assert "duration=5m" in captured["url"]


def test_poll_once_processes_pools_above_thresholds(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_MIN_PRICE_CHANGE_PCT", 15.0)
    monkeypatch.setattr(config, "MOMENTUM_MIN_VOLUME_USD", 2000.0)

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [_pool_payload(price_change_m5="30", volume_m5="8000")]}

    monkeypatch.setattr(momentum_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    momentum_detector.poll_once()
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1


def test_poll_once_handles_non_dict_response(monkeypatch):
    for bad_json in ("just a string", None, 42, ["a", "list"]):
        class _FakeResponse:
            def raise_for_status(self):
                pass

            def json(self, _bad=bad_json):
                return _bad

        monkeypatch.setattr(momentum_detector.httpx, "get", lambda *a, **k: _FakeResponse())
        momentum_detector.poll_once()  # ne doit pas lever, quel que soit le type
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_handles_non_dict_items_in_data_list(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": ["not a dict", 123, None]}

    monkeypatch.setattr(momentum_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    momentum_detector.poll_once()  # ne doit pas lever
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_poll_once_skips_bad_items_but_processes_good_ones(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_MIN_PRICE_CHANGE_PCT", 15.0)
    monkeypatch.setattr(config, "MOMENTUM_MIN_VOLUME_USD", 2000.0)

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [
                "garbage",
                _pool_payload(price_change_m5="30", volume_m5="8000"),
                None,
            ]}

    monkeypatch.setattr(momentum_detector.httpx, "get", lambda *a, **k: _FakeResponse())
    momentum_detector.poll_once()
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1
