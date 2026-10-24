import pytest

from watcher import storage
from watcher.detectors import telegram_detector


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def test_extract_token_address_from_pumpfun_link():
    text = "🚀 gros call, regardez ça https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"
    addr = telegram_detector.extract_token_address(text)
    assert addr == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_extract_token_address_from_dexscreener_solana_link():
    text = "check https://dexscreener.com/solana/9yLMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXaB"
    addr = telegram_detector.extract_token_address(text)
    assert addr == "9yLMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXaB"


def test_extract_token_address_from_birdeye_link():
    text = "https://birdeye.so/token/5zNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXcD?chain=solana"
    addr = telegram_detector.extract_token_address(text)
    assert addr == "5zNMtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXcD"


def test_extract_token_address_falls_back_to_raw_base58():
    text = "CA: 4wPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXeF go go go"
    addr = telegram_detector.extract_token_address(text)
    assert addr == "4wPQtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXeF"


def test_extract_token_address_returns_none_when_nothing_found():
    assert telegram_detector.extract_token_address("juste du texte sans adresse") is None


def test_extract_token_address_returns_none_for_empty_string():
    assert telegram_detector.extract_token_address("") is None


def test_extract_token_address_returns_none_for_none_input():
    assert telegram_detector.extract_token_address(None) is None


def test_extract_token_address_returns_none_for_emoji_only_text():
    assert telegram_detector.extract_token_address("🚀🚀🚀🔥🔥🔥") is None


def test_extract_token_address_prefers_link_over_raw_address_in_same_message():
    # Le message contient une adresse "brute" plausible ET un lien reconnu
    # avec une adresse différente : le lien doit gagner (plus fiable).
    text = (
        f"raw fake addr: {'1' * 32} "
        "mais le vrai call est ici https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"
    )
    addr = telegram_detector.extract_token_address(text)
    assert addr == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_extract_token_address_does_not_match_short_or_long_lookalike_strings():
    # Trop court (< 32) : ne doit pas matcher.
    assert telegram_detector.extract_token_address("addr: shortstring123456789") is None
    # Trop long (> 44) mais base58-valide : ne doit pas matcher tel quel.
    too_long = "A" * 50
    assert telegram_detector.extract_token_address(f"addr: {too_long}") is None


def test_handle_message_adds_candidate_with_telegram_source():
    text = "🚀 https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"
    telegram_detector.handle_message(text, "@alpha_group")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_channel, source_note, pair_address FROM candidates "
            "WHERE token_address = ?",
            ("7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",),
        ).fetchone()
    assert row["source"] == "telegram"
    assert row["source_channel"] == "@alpha_group"
    assert row["source_note"] == text[:200]
    assert row["pair_address"] == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_handle_message_does_nothing_when_no_address_found():
    telegram_detector.handle_message("rien à voir ici", "@alpha_group")
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_message_never_raises_on_empty_or_none_text():
    # Un message vide ou sans texte (média seul) ne doit jamais faire planter
    # le détecteur.
    telegram_detector.handle_message("", "@alpha_group")
    telegram_detector.handle_message(None, "@alpha_group")
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_message_truncates_long_note_to_200_chars():
    long_text = "https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ " + ("x" * 500)
    telegram_detector.handle_message(long_text, "@alpha_group")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source_note FROM candidates WHERE token_address = ?",
            ("7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",),
        ).fetchone()
    assert row["source_note"] == long_text[:200]
    assert len(row["source_note"]) == 200


def test_extract_ticker_finds_dollar_symbol():
    assert telegram_detector.extract_ticker("gros potentiel sur $BONK2 les gars") == "BONK2"


def test_extract_ticker_returns_none_without_dollar_symbol():
    assert telegram_detector.extract_ticker("gros potentiel sur BONK2 les gars") is None


def test_extract_ticker_ignores_dollar_amounts():
    # "$0.05" et "$50k" ne sont pas des tickers : le premier caractère après
    # le $ doit être une lettre, pas un chiffre.
    assert telegram_detector.extract_ticker("déjà à $0.05, objectif $50k mcap") is None


def test_extract_ticker_returns_none_for_empty_or_none():
    assert telegram_detector.extract_ticker("") is None
    assert telegram_detector.extract_ticker(None) is None


def test_extract_ticker_returns_first_match_when_several():
    assert telegram_detector.extract_ticker("$FIRST puis $SECOND") == "FIRST"


def test_handle_ticker_match_adds_candidate_with_ticker_note():
    telegram_detector.handle_ticker_match(
        "BONK2", "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",
        "@alpha_group", "gros potentiel sur $BONK2 les gars",
    )
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_channel, source_note, pair_address FROM candidates "
            "WHERE token_address = ?",
            ("7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",),
        ).fetchone()
    assert row["source"] == "telegram"
    assert row["source_channel"] == "@alpha_group"
    assert row["source_note"] == "[ticker $BONK2] gros potentiel sur $BONK2 les gars"
    assert row["pair_address"] == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_handle_ticker_match_is_idempotent_like_handle_message():
    # add_candidate est un no-op si le token_address existe déjà : deux
    # mentions du même ticker résolu vers la même adresse ne doivent pas
    # planter ni dupliquer la ligne.
    telegram_detector.handle_ticker_match("BONK2", "AddrX", "@a", "$BONK2 encore")
    telegram_detector.handle_ticker_match("BONK2", "AddrX", "@a", "$BONK2 toujours")
    with storage._get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE token_address = ?", ("AddrX",)
        ).fetchone()["n"]
    assert count == 1
