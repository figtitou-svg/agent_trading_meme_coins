import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from watcher import storage

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
BEFORE = (NOW - timedelta(hours=2)).isoformat()
AFTER = (NOW - timedelta(minutes=30)).isoformat()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def test_count_candidates_watched_since_counts_only_after_cutoff():
    # add_candidate() horodate detected_at avec l'heure réelle : on la force
    # explicitement des deux côtés (BEFORE/AFTER) pour ne dépendre d'aucune
    # horloge système.
    storage.add_candidate("0xold", "0xpairold", "Old", "OLD")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET detected_at = ? WHERE token_address = '0xold'",
                     (BEFORE,))
    storage.add_candidate("0xnew", "0xpairnew", "New", "NEW")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET detected_at = ? WHERE token_address = '0xnew'",
                     (NOW.isoformat(),))

    assert storage.count_candidates_watched_since(AFTER) == 1


def test_count_positions_opened_since_counts_only_after_cutoff():
    storage.open_virtual_position("0xold", "0xpairold", 1.0, "Old", "OLD")
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET entry_time = ? WHERE token_address = '0xold'",
                     (BEFORE,))
    storage.open_virtual_position("0xnew", "0xpairnew", 1.0, "New", "NEW")
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET entry_time = ? WHERE token_address = '0xnew'",
                     (NOW.isoformat(),))

    assert storage.count_positions_opened_since(AFTER) == 1


def test_get_positions_closed_since_returns_only_closed_after_cutoff():
    storage.open_virtual_position("0xa", "0xpaira", 1.0, "A", "AAA")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = '0xa'"
        ).fetchone()["id"]
    storage.mark_position_closed(pos_id, 2.5, True)
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET died_at = ? WHERE id = ?", (BEFORE, pos_id))

    storage.open_virtual_position("0xb", "0xpairb", 2.0, "B", "BBB")
    with storage._get_conn() as conn:
        pos_id_b = conn.execute(
            "SELECT id FROM positions WHERE token_address = '0xb'"
        ).fetchone()["id"]
    storage.mark_position_closed(pos_id_b, 0.4, False)
    # mark_position_closed() horodate died_at avec l'heure réelle : on la force
    # à une valeur connue (> AFTER) pour que le test ne dépende pas de l'heure
    # d'exécution.
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET died_at = ? WHERE id = ?",
                     (NOW.isoformat(), pos_id_b))

    closed = storage.get_positions_closed_since(AFTER)
    assert len(closed) == 1
    assert closed[0]["token_address"] == "0xb"
    assert closed[0]["status"] == "closed_loss"
    assert closed[0]["invested_eur"] == 2.0
    assert closed[0]["exit_multiplier"] == 0.4


def test_get_positions_closed_since_excludes_still_open_positions():
    storage.open_virtual_position("0xc", "0xpairc", 1.0, "C", "CCC")
    assert storage.get_positions_closed_since(BEFORE) == []


def test_count_candidates_watched_since_excludes_row_at_exact_cutoff():
    storage.add_candidate("0xexact", "0xpairexact", "Exact", "EXA")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET detected_at = ? WHERE token_address = '0xexact'",
                     (AFTER,))
    assert storage.count_candidates_watched_since(AFTER) == 0


def test_count_positions_opened_since_excludes_row_at_exact_cutoff():
    storage.open_virtual_position("0xexact", "0xpairexact", 1.0, "Exact", "EXA")
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET entry_time = ? WHERE token_address = '0xexact'",
                     (AFTER,))
    assert storage.count_positions_opened_since(AFTER) == 0


def test_get_positions_closed_since_excludes_position_died_at_exact_cutoff():
    storage.open_virtual_position("0xexact", "0xpairexact", 1.0, "Exact", "EXA")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = '0xexact'"
        ).fetchone()["id"]
    storage.mark_position_closed(pos_id, 1.5, True)
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET died_at = ? WHERE id = ?", (AFTER, pos_id))

    assert storage.get_positions_closed_since(AFTER) == []


def test_get_positions_closed_since_includes_source():
    storage.open_virtual_position("MintX", "PoolX", 1.0, "X", "XXX", source="telegram")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintX'"
        ).fetchone()["id"]
    storage.mark_position_closed(pos_id, 2.0, True)
    closed = storage.get_positions_closed_since(
        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    )
    assert closed[0]["source"] == "telegram"


def test_count_candidates_by_source_since_splits_watched_and_bought_by_source():
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    storage.add_candidate("MintY1", "MintY1", "Y1", "YY1", source="telegram")
    storage.add_candidate("MintY2", "MintY2", "Y2", "YY2", source="onchain")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET detected_at = ? WHERE token_address IN ('MintY1', 'MintY2')", (now_iso,))

    storage.open_virtual_position("MintY3", "PoolY3", 1.0, "Y3", "YY3", source="telegram")
    with storage._get_conn() as conn:
        conn.execute("UPDATE positions SET entry_time = ? WHERE token_address = 'MintY3'", (now_iso,))

    # Un candidat/position hors fenêtre ne doit pas être compté.
    storage.add_candidate("MintOld", "MintOld", "Old", "OLD", source="onchain")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET detected_at = ? WHERE token_address = 'MintOld'", (old_iso,))

    since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    result = storage.count_candidates_by_source_since(since)
    assert result == {
        "telegram": {"watched": 1, "bought": 1},
        "onchain": {"watched": 1, "bought": 0},
    }


def test_count_candidates_by_source_since_ignores_unexpected_source_value():
    """Si une ligne porte une valeur de `source` hors du schéma attendu
    (ne devrait pas arriver vu le défaut de colonne, mais pas impossible en cas
    de donnée corrompue ou de migration future), la fonction ne doit ni lever
    ni fausser les compteurs telegram/onchain existants."""
    storage.add_candidate("MintZ1", "MintZ1", "Z1", "ZZ1", source="onchain")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET source = 'discord' WHERE token_address = 'MintZ1'"
        )
    since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    result = storage.count_candidates_by_source_since(since)
    assert result == {
        "telegram": {"watched": 0, "bought": 0},
        "onchain": {"watched": 0, "bought": 0},
    }


def test_add_candidate_stores_source_and_channel_metadata():
    storage.add_candidate(
        "SoLmintAddr1", "PoolAddr1", "Name", "SYM",
        source="telegram", source_channel="@alpha_group", source_note="🚀 CA: SoLmintAddr1",
    )
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_channel, source_note FROM candidates WHERE token_address = ?",
            ("SoLmintAddr1",),
        ).fetchone()
    assert row["source"] == "telegram"
    assert row["source_channel"] == "@alpha_group"
    assert row["source_note"] == "🚀 CA: SoLmintAddr1"


def test_add_candidate_defaults_source_to_onchain():
    storage.add_candidate("SoLmintAddr2", "PoolAddr2", "Name2", "SYM2")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_channel, source_note FROM candidates WHERE token_address = ?",
            ("SoLmintAddr2",),
        ).fetchone()
    assert row["source"] == "onchain"
    assert row["source_channel"] is None
    assert row["source_note"] is None


def test_add_candidate_returns_true_when_created():
    assert storage.add_candidate("SoLmintNew1", "PoolNew1", "N1", "NN1") is True


def test_add_candidate_returns_false_when_already_exists():
    """L'appelant s'appuie sur cette valeur pour ne PAS reloguer/renotifier
    une 'détection' quand le token est déjà connu (bug corrigé le
    30/07/2026 : les détecteurs loggaient 'détecté' à chaque cycle de
    polling tant que le token restait visible, même sans nouvel insert)."""
    storage.add_candidate("SoLmintDup1", "PoolDup1", "D1", "DD1")
    assert storage.add_candidate("SoLmintDup1", "PoolDup1", "D1", "DD1") is False


def test_open_virtual_position_returns_true_when_created():
    assert storage.open_virtual_position("MintPosNew1", "PoolPosNew1", 1.0, "P1", "PP1") is True


def test_open_virtual_position_returns_false_when_already_exists():
    """L'appelant (main.evaluate_candidates) s'appuie sur cette valeur pour
    ne PAS envoyer d'alerte Telegram d'achat quand aucune position n'a été
    réellement créée."""
    storage.open_virtual_position("MintPosDup1", "PoolPosDup1", 1.0, "D1", "DD1")
    assert storage.open_virtual_position("MintPosDup1", "PoolPosDup1", 1.0, "D1", "DD1") is False


def test_record_detection_stores_source_and_chain():
    storage.record_detection(
        token_address="SoLmintAddr3", verdict="ok", source="telegram", chain="solana",
    )
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, chain FROM detections WHERE token_address = ?",
            ("SoLmintAddr3",),
        ).fetchone()
    assert row["source"] == "telegram"
    assert row["chain"] == "solana"


def test_record_detection_defaults_source_onchain_and_chain_solana():
    storage.record_detection(token_address="SoLmintAddr4", verdict="ok")
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source, chain FROM detections WHERE token_address = ?",
            ("SoLmintAddr4",),
        ).fetchone()
    assert row["source"] == "onchain"
    assert row["chain"] == "solana"


def test_open_virtual_position_stores_source():
    storage.open_virtual_position(
        "SoLmintAddr5", "PoolAddr5", 1.0, "Name5", "SYM5", source="telegram",
    )
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT source FROM positions WHERE token_address = ?", ("SoLmintAddr5",)
        ).fetchone()
    assert row["source"] == "telegram"


def test_find_open_positions_by_identifier_matches_address():
    storage.open_virtual_position("MintFind1", "PoolFind1", 1.0, "F1", "FF1")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintFind1'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    results = storage.find_open_positions_by_identifier("MintFind1")
    assert len(results) == 1
    assert results[0]["token_address"] == "MintFind1"


def test_find_open_positions_by_identifier_matches_symbol_case_insensitive():
    storage.open_virtual_position("MintFind2", "PoolFind2", 1.0, "Find Two", "FIND2")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintFind2'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    assert len(storage.find_open_positions_by_identifier("find2")) == 1
    assert len(storage.find_open_positions_by_identifier("FIND2")) == 1


def test_find_open_positions_by_identifier_matches_name_case_insensitive():
    storage.open_virtual_position("MintFind3", "PoolFind3", 1.0, "Find Three", None)
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintFind3'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    assert len(storage.find_open_positions_by_identifier("find three")) == 1


def test_find_open_positions_by_identifier_excludes_pending_and_closed():
    storage.open_virtual_position("MintFind4", "PoolFind4", 1.0, "F4", "FF4")
    # reste 'pending' : pas de set_entry_price
    storage.open_virtual_position("MintFind5", "PoolFind5", 1.0, "F5", "FF5")
    with storage._get_conn() as conn:
        pos_id5 = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintFind5'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id5, 0.01)
    storage.mark_position_closed(pos_id5, 2.0, True)

    assert storage.find_open_positions_by_identifier("FF4") == []
    assert storage.find_open_positions_by_identifier("FF5") == []


def test_find_open_positions_by_identifier_returns_empty_for_no_match():
    assert storage.find_open_positions_by_identifier("NOPE") == []


def test_update_current_multiplier_persists_value():
    storage.open_virtual_position("MintCM", "PoolCM", 1.0, "CM", "CMCM")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintCM'"
        ).fetchone()["id"]
    storage.update_current_multiplier(pos_id, 2.35)
    with storage._get_conn() as conn:
        value = conn.execute(
            "SELECT current_multiplier FROM positions WHERE id = ?", (pos_id,)
        ).fetchone()["current_multiplier"]
    assert value == 2.35


def test_get_active_positions_includes_current_multiplier():
    """Bug du 02/08/2026 : get_active_positions() ne sélectionnait pas
    current_multiplier, donc /recap retombait toujours sur l'affichage
    'peak seulement' même une fois la valeur mise à jour — les tests de
    notifier.build_ondemand_recap_message passaient quand même car ils
    construisaient leurs propres dicts de test, sans jamais passer par la
    vraie requête SQL. Ce test exerce le vrai chemin bout en bout."""
    storage.open_virtual_position("MintActive", "PoolActive", 1.0, "A", "AA")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintActive'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    storage.update_current_multiplier(pos_id, 1.75)

    positions = storage.get_active_positions()
    assert len(positions) == 1
    assert positions[0]["current_multiplier"] == 1.75


def test_get_portfolio_summary_empty_portfolio():
    summary = storage.get_portfolio_summary()
    assert summary == {"invested_eur": 0, "current_value_eur": 0}


def test_get_portfolio_summary_mixes_all_statuses():
    # open : valeur mark-to-market via current_multiplier
    storage.open_virtual_position("MintOpen", "PoolOpen", 1.0, "O", "OOO")
    with storage._get_conn() as conn:
        open_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintOpen'"
        ).fetchone()["id"]
    storage.set_entry_price(open_id, 0.01)
    storage.update_current_multiplier(open_id, 2.0)

    # closed_profit : valeur figée = invested * exit_multiplier
    storage.open_virtual_position("MintClosed", "PoolClosed", 1.0, "C", "CCC")
    with storage._get_conn() as conn:
        closed_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintClosed'"
        ).fetchone()["id"]
    storage.mark_position_closed(closed_id, 3.0, True)

    # dead : valeur nulle, peu importe invested_eur
    storage.open_virtual_position("MintDead", "PoolDead", 1.0, "D", "DDD")
    with storage._get_conn() as conn:
        dead_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintDead'"
        ).fetchone()["id"]
    storage.mark_position_dead(dead_id)

    # pending : prix d'entrée pas encore connu -> valeur ~= invested_eur
    storage.open_virtual_position("MintPending", "PoolPending", 1.0, "P", "PPP")

    summary = storage.get_portfolio_summary()
    assert summary["invested_eur"] == 4.0
    # 2.0 (open) + 3.0 (closed) + 0.0 (dead) + 1.0 (pending) = 6.0
    assert summary["current_value_eur"] == 6.0


def test_get_portfolio_summary_open_position_without_current_multiplier_yet():
    """Une position tout juste ouverte (entry_price fixé mais
    refresh_positions() n'a pas encore tourné) n'a pas de current_multiplier
    -> repli sur 1.0 (valeur ~= montant investi), pas une erreur SQL sur NULL."""
    storage.open_virtual_position("MintFresh", "PoolFresh", 2.0, "F", "FFF")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintFresh'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)
    summary = storage.get_portfolio_summary()
    assert summary["current_value_eur"] == 2.0


def test_migration_adds_new_columns_to_legacy_db(tmp_path, monkeypatch):
    """Simule une base créée avant cette migration (sans les nouvelles colonnes) :
    init_db() doit les ajouter sans lever d'erreur ni perdre les tables existantes."""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    with storage._get_conn() as conn:
        conn.execute("CREATE TABLE detections (id INTEGER PRIMARY KEY, token_address TEXT)")
        conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, token_address TEXT)")
        conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, token_address TEXT)")
    storage.init_db()
    with storage._get_conn() as conn:
        det_cols = {r[1] for r in conn.execute("PRAGMA table_info(detections)")}
        cand_cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
        pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    assert {"source", "chain"} <= det_cols
    assert {"source", "source_channel", "source_note"} <= cand_cols
    assert {"source", "current_multiplier"} <= pos_cols


def test_migration_rebuilds_legacy_detections_table_with_chain_id_not_null(tmp_path, monkeypatch):
    """Reproduit le schéma exact pré-migration de `detections` (chain_id INTEGER
    NOT NULL, sans défaut). init_db() doit réussir à migrer cette table réelle
    (pas un stand-in simplifié), conserver les lignes existantes, et laisser le
    module pleinement fonctionnel (record_detection() ne doit plus planter avec
    une IntegrityError sur chain_id, colonne qui n'existe même plus)."""
    db_path = tmp_path / "legacy_detections.db"
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    with storage._get_conn() as conn:
        conn.execute('''
            CREATE TABLE detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                token_address TEXT NOT NULL,
                token_name TEXT,
                token_symbol TEXT,
                pair_address TEXT,
                block_number INTEGER,
                verdict TEXT NOT NULL,
                reasons TEXT
            )
        ''')
        conn.execute(
            "INSERT INTO detections "
            "(timestamp, chain_id, token_address, token_name, token_symbol, "
            " pair_address, block_number, verdict, reasons) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (BEFORE, 8453, "0xLegacyToken", "Legacy", "LEG", "0xLegacyPair",
             12345, "ok", "[]"),
        )

    storage.init_db()

    # La ligne pré-existante doit avoir survécu à la migration.
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM detections WHERE token_address = ?", ("0xLegacyToken",)
        ).fetchone()
    assert row is not None
    assert row["verdict"] == "ok"
    assert row["timestamp"] == BEFORE

    # Le module doit rester fonctionnel après la migration d'une vraie base
    # legacy : plus de colonne chain_id, plus d'IntegrityError.
    storage.record_detection(token_address="0xNewToken", verdict="ok")
    with storage._get_conn() as conn:
        new_row = conn.execute(
            "SELECT source, chain FROM detections WHERE token_address = ?",
            ("0xNewToken",),
        ).fetchone()
    assert new_row["source"] == "onchain"
    assert new_row["chain"] == "solana"


def test_migration_rolls_back_cleanly_if_interrupted_mid_rebuild(tmp_path, monkeypatch):
    """Régression : le rebuild de `detections` (RENAME/CREATE/INSERT/DROP) doit
    être une transaction unique (BEGIN explicite). Sans ça, DDL sqlite tourne en
    autocommit et une interruption entre le RENAME et la suite laisse la base
    sans table `detections` et les données legacy orphelines dans
    `detections_old`. On simule un crash juste après le RENAME et on vérifie
    que tout est annulé proprement : la table `detections` legacy d'origine est
    intacte, avec sa ligne."""
    db_path = tmp_path / "legacy_detections_crash.db"
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))

    class _Boom(Exception):
        pass

    class _FaultInjectingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if "RENAME TO detections_old" in sql:
                raise _Boom("simulated crash right after RENAME committed")
            return result

    def _fault_injecting_get_conn():
        conn = sqlite3.connect(str(db_path), factory=_FaultInjectingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    with storage._get_conn() as conn:
        conn.execute('''
            CREATE TABLE detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                token_address TEXT NOT NULL,
                token_name TEXT,
                token_symbol TEXT,
                pair_address TEXT,
                block_number INTEGER,
                verdict TEXT NOT NULL,
                reasons TEXT
            )
        ''')
        conn.execute(
            "INSERT INTO detections "
            "(timestamp, chain_id, token_address, token_name, token_symbol, "
            " pair_address, block_number, verdict, reasons) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (BEFORE, 8453, "0xLegacyToken", "Legacy", "LEG", "0xLegacyPair",
             12345, "ok", "[]"),
        )

    real_get_conn = storage._get_conn
    storage._get_conn = _fault_injecting_get_conn
    try:
        with pytest.raises(_Boom):
            storage.init_db()
    finally:
        storage._get_conn = real_get_conn

    # Après le "crash", la table detections d'origine doit être intacte
    # (rollback complet), et il ne doit rester aucune table orpheline.
    with storage._get_conn() as conn:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "detections_old" not in tables
        assert "detections" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(detections)")}
        assert "chain_id" in cols  # rollback -> toujours le schéma legacy
        row = conn.execute(
            "SELECT * FROM detections WHERE token_address = ?", ("0xLegacyToken",)
        ).fetchone()
    assert row is not None
    assert row["verdict"] == "ok"
