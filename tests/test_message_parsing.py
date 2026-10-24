from watcher.detectors import message_parsing


def test_extract_token_address_from_pumpfun_link():
    text = "🚀 gros call, regardez ça https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"
    addr = message_parsing.extract_token_address(text)
    assert addr == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_extract_token_address_from_dexscreener_solana_link():
    text = "check https://dexscreener.com/solana/9yLMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXaB"
    addr = message_parsing.extract_token_address(text)
    assert addr == "9yLMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXaB"


def test_extract_token_address_from_birdeye_link():
    text = "https://birdeye.so/token/5zNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXcD?chain=solana"
    addr = message_parsing.extract_token_address(text)
    assert addr == "5zNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXcD"


def test_extract_token_address_falls_back_to_raw_base58():
    text = "CA: 4wPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXeF go go go"
    addr = message_parsing.extract_token_address(text)
    assert addr == "4wPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXeF"


def test_extract_token_address_returns_none_when_nothing_found():
    assert message_parsing.extract_token_address("juste du texte sans adresse") is None


def test_extract_token_address_returns_none_for_empty_string():
    assert message_parsing.extract_token_address("") is None


def test_extract_token_address_returns_none_for_none_input():
    assert message_parsing.extract_token_address(None) is None


def test_extract_token_address_prefers_link_over_raw_address_in_same_message():
    text = (
        f"raw fake addr: {'1' * 32} "
        "mais le vrai call est ici https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"
    )
    addr = message_parsing.extract_token_address(text)
    assert addr == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_extract_token_address_does_not_match_short_or_long_lookalike_strings():
    assert message_parsing.extract_token_address("addr: shortstring123456789") is None
    too_long = "A" * 50
    assert message_parsing.extract_token_address(f"addr: {too_long}") is None


def test_extract_ticker_finds_dollar_symbol():
    assert message_parsing.extract_ticker("gros potentiel sur $BONK2 les gars") == "BONK2"


def test_extract_ticker_returns_none_without_dollar_symbol():
    assert message_parsing.extract_ticker("gros potentiel sur BONK2 les gars") is None


def test_extract_ticker_ignores_dollar_amounts():
    assert message_parsing.extract_ticker("déjà à $0.05, objectif $50k mcap") is None


def test_extract_ticker_returns_none_for_empty_or_none():
    assert message_parsing.extract_ticker("") is None
    assert message_parsing.extract_ticker(None) is None


def test_extract_ticker_returns_first_match_when_several():
    assert message_parsing.extract_ticker("$FIRST puis $SECOND") == "FIRST"
