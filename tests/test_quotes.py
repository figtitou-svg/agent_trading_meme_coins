import httpx
import pytest

from watcher import config
from watcher import quotes


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_fetch_curve_progress_empty_input_returns_empty_without_network(monkeypatch):
    called = []
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: called.append(1))
    assert quotes.fetch_curve_progress([]) == {}
    assert called == []


def test_fetch_curve_progress_parses_market_cap_and_migration_flag(monkeypatch):
    def _fake_get(url, **kwargs):
        assert "MintAddr1" in url
        return _FakeResponse({
            "usd_market_cap": 15000.0,
            "complete": False,
            "pool_address": "PoolForMintAddr1",
        })
    monkeypatch.setattr(quotes.httpx, "get", _fake_get)
    result = quotes.fetch_curve_progress(["MintAddr1"])
    assert result == {
        "MintAddr1": {
            "market_cap_usd": 15000.0,
            "migrated": False,
            "pool_address": "PoolForMintAddr1",
        }
    }


def test_fetch_curve_progress_includes_pool_address_on_migrated_coin(monkeypatch):
    """pool_address est déjà présent dans la réponse /coins/{mint} — plus
    besoin d'un appel séparé de résolution une fois le token migré."""
    def _fake_get(url, **kwargs):
        return _FakeResponse({
            "usd_market_cap": 70000.0,
            "complete": True,
            "pool_address": "RealPoolAddrXYZ",
        })
    monkeypatch.setattr(quotes.httpx, "get", _fake_get)
    result = quotes.fetch_curve_progress(["MintMigrated"])
    assert result["MintMigrated"]["migrated"] is True
    assert result["MintMigrated"]["pool_address"] == "RealPoolAddrXYZ"


def test_fetch_curve_progress_never_raises_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(quotes.httpx, "get", _raise)
    assert quotes.fetch_curve_progress(["MintAddr2"]) == {}


def test_fetch_pool_quotes_empty_input_returns_empty_without_network(monkeypatch):
    called = []
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: called.append(1))
    assert quotes.fetch_pool_quotes([]) == {}
    assert called == []


def test_fetch_pool_quotes_parses_price_liquidity_volume(monkeypatch):
    def _fake_get(url, **kwargs):
        assert "/solana/" in url
        return _FakeResponse({
            "pairs": [{
                "pairAddress": "PoolAddrABC",
                "priceUsd": "0.0042",
                "liquidity": {"usd": 5000.0},
                "volume": {"h1": 800.0},
            }]
        })
    monkeypatch.setattr(quotes.httpx, "get", _fake_get)
    result = quotes.fetch_pool_quotes(["PoolAddrABC"])
    assert result["pooladdrabc"] == {
        "price": "0.0042", "liquidity": 5000.0, "volume_h1": 800.0,
    }


def test_fetch_pool_quotes_never_raises_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(quotes.httpx, "get", _raise)
    assert quotes.fetch_pool_quotes(["PoolAddrXYZ"]) == {}


def test_fetch_pool_quotes_caps_batch_at_30_addresses(monkeypatch):
    captured_url = {}
    def _fake_get(url, **kwargs):
        captured_url["url"] = url
        return _FakeResponse({"pairs": []})
    monkeypatch.setattr(quotes.httpx, "get", _fake_get)
    addresses = [f"Pool{i}" for i in range(40)]
    quotes.fetch_pool_quotes(addresses)
    requested = captured_url["url"].split("/")[-1].split(",")
    assert len(requested) == 30


def test_fetch_pool_quotes_never_raises_on_malformed_response(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _FakeResponse({"pairs": "not_a_list"}))
    assert quotes.fetch_pool_quotes(["PoolMalformed"]) == {}


def test_fetch_curve_progress_never_raises_on_malformed_response(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _FakeResponse("not_a_dict"))
    assert quotes.fetch_curve_progress(["MintMalformed"]) == {}


def test_resolve_ticker_mint_returns_none_for_empty_ticker(monkeypatch):
    called = []
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: called.append(1))
    assert quotes.resolve_ticker_mint("") is None
    assert called == []


def test_resolve_ticker_mint_picks_solana_match_by_symbol(monkeypatch):
    def _fake_get(url, **kwargs):
        assert kwargs["params"] == {"q": "BONK2"}
        return _FakeResponse({
            "pairs": [
                {
                    "chainId": "ethereum",
                    "baseToken": {"symbol": "BONK2", "address": "EthAddrWrongChain"},
                    "liquidity": {"usd": 999999.0},
                },
                {
                    "chainId": "solana",
                    "baseToken": {"symbol": "BONK2", "address": "SolAddrCorrect"},
                    "liquidity": {"usd": 5000.0},
                },
            ]
        })
    monkeypatch.setattr(quotes.httpx, "get", _fake_get)
    assert quotes.resolve_ticker_mint("BONK2") == "SolAddrCorrect"


def test_resolve_ticker_mint_picks_highest_liquidity_among_ambiguous_matches():
    pairs = [
        {"chainId": "solana", "baseToken": {"symbol": "PEPE2", "address": "LowLiq"},
         "liquidity": {"usd": 100.0}},
        {"chainId": "solana", "baseToken": {"symbol": "PEPE2", "address": "HighLiq"},
         "liquidity": {"usd": 50000.0}},
    ]
    from watcher import quotes as quotes_module
    result = quotes_module._pick_best_ticker_match(pairs, "PEPE2")
    assert result == "HighLiq"


def test_resolve_ticker_mint_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _FakeResponse({
        "pairs": [{
            "chainId": "solana",
            "baseToken": {"symbol": "bonk2", "address": "SolAddrLower"},
            "liquidity": {"usd": 100.0},
        }]
    }))
    assert quotes.resolve_ticker_mint("BONK2") == "SolAddrLower"


def test_resolve_ticker_mint_returns_none_when_no_solana_match(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _FakeResponse({
        "pairs": [{"chainId": "ethereum", "baseToken": {"symbol": "BONK2", "address": "EthOnly"},
                    "liquidity": {"usd": 100.0}}]
    }))
    assert quotes.resolve_ticker_mint("BONK2") is None


def test_resolve_ticker_mint_never_raises_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(quotes.httpx, "get", _raise)
    assert quotes.resolve_ticker_mint("BONK2") is None


def test_resolve_ticker_mint_never_raises_on_malformed_response(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _FakeResponse({"pairs": "not_a_list"}))
    assert quotes.resolve_ticker_mint("BONK2") is None


