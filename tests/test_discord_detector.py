import pytest

from watcher import storage
from watcher.detectors import discord_detector


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def test_handle_message_adds_candidate_with_discord_source():
    text = "🚀 https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"
    discord_detector.handle_message(text, "#alpha-calls")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_channel, source_note, pair_address FROM candidates "
            "WHERE token_address = ?",
            ("7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",),
        ).fetchone()
    assert row["source"] == "discord"
    assert row["source_channel"] == "#alpha-calls"
    assert row["source_note"] == text[:200]
    assert row["pair_address"] == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_handle_message_does_nothing_when_no_address_found():
    discord_detector.handle_message("rien à voir ici", "#alpha-calls")
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_message_never_raises_on_empty_or_none_text():
    discord_detector.handle_message("", "#alpha-calls")
    discord_detector.handle_message(None, "#alpha-calls")
    with storage._get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_handle_message_truncates_long_note_to_200_chars():
    long_text = "https://pump.fun/coin/7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ " + ("x" * 500)
    discord_detector.handle_message(long_text, "#alpha-calls")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source_note FROM candidates WHERE token_address = ?",
            ("7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",),
        ).fetchone()
    assert row["source_note"] == long_text[:200]
    assert len(row["source_note"]) == 200


def test_handle_ticker_match_adds_candidate_with_ticker_note():
    discord_detector.handle_ticker_match(
        "BONK2", "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",
        "#alpha-calls", "gros potentiel sur $BONK2 les gars",
    )
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_channel, source_note, pair_address FROM candidates "
            "WHERE token_address = ?",
            ("7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ",),
        ).fetchone()
    assert row["source"] == "discord"
    assert row["source_channel"] == "#alpha-calls"
    assert row["source_note"] == "[ticker $BONK2] gros potentiel sur $BONK2 les gars"
    assert row["pair_address"] == "7xKXtg2CW3wJ4vD9tRzL1nQeR8mF2sYpB3hN6cA9wXyZ"


def test_handle_ticker_match_is_idempotent():
    discord_detector.handle_ticker_match("BONK2", "AddrX", "#alpha", "$BONK2 encore")
    discord_detector.handle_ticker_match("BONK2", "AddrX", "#alpha", "$BONK2 toujours")
    with storage._get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE token_address = ?", ("AddrX",)
        ).fetchone()["n"]
    assert count == 1

