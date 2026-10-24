import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from watcher import config
import main
from watcher import storage


def test_scheduled_recap_slots_use_paris_time_regardless_of_system_tz():
    """Le serveur de prod tourne en UTC (Etc/UTC) — les créneaux 07h30/13h/18h/22h
    doivent rester basés sur l'heure de Paris (avec bascule été/hiver automatique),
    pas sur le fuseau système, sous peine de décalage silencieux de 1h à 2h."""
    now_paris = datetime.now(main._PARIS_TZ)
    assert now_paris.tzinfo is not None
    assert getattr(now_paris.tzinfo, "key", None) == "Europe/Paris"
    assert now_paris.utcoffset().total_seconds() in (3600, 7200)


def test_telegram_detector_enabled_false_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_ID", "")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "")
    assert main.telegram_detector_enabled() is False


def test_telegram_detector_enabled_false_when_only_one_credential_set(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_ID", "12345")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "")
    assert main.telegram_detector_enabled() is False


def test_telegram_detector_enabled_true_when_both_credentials_set(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_ID", "12345")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "abcd")
    assert main.telegram_detector_enabled() is True


def test_discord_detector_enabled_false_when_token_missing(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    assert main.discord_detector_enabled() is False


def test_discord_detector_enabled_true_when_token_set(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "some-bot-token")
    assert main.discord_detector_enabled() is True


def test_command_listener_enabled_false_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    assert main.command_listener_enabled() is False


def test_command_listener_enabled_true_when_credentials_set(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "7453718040")
    assert main.command_listener_enabled() is True


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def test_maybe_send_recap_initializes_state_without_sending_on_first_slot(monkeypatch):
    """Le tout premier créneau franchi après le démarrage n'a rien à
    récapituler (last_notified_slot == -1) : on initialise juste l'état,
    sans appeler alert_recap."""
    calls = []
    monkeypatch.setattr(main.notifier, "alert_recap", lambda *a, **k: calls.append((a, k)))
    last_slot, last_time = main.maybe_send_recap(735, -1, datetime.now(timezone.utc))
    assert last_slot == 730
    assert calls == []


def test_maybe_send_recap_sends_alert_recap_with_by_source_when_slot_crossed(monkeypatch):
    """Une fois qu'un premier créneau est déjà enregistré, franchir le
    créneau suivant doit déclencher alert_recap avec un découpage by_source."""
    calls = []
    monkeypatch.setattr(main.notifier, "alert_recap", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(main.notifier, "recap_enabled", lambda: True)

    storage.add_candidate("MintRecap1", "MintRecap1", "R1", "RR1", source="telegram")

    previous_digest_time = datetime.now(timezone.utc) - timedelta(hours=1)
    last_slot, last_time = main.maybe_send_recap(1305, 730, previous_digest_time)

    assert last_slot == 1300
    assert last_time > previous_digest_time
    assert len(calls) == 1
    args, kwargs = calls[0]
    by_source = args[5] if len(args) > 5 else kwargs.get("by_source")
    assert by_source is not None
    assert by_source["telegram"]["watched"] == 1


def test_maybe_send_recap_passes_portfolio_summary(monkeypatch):
    calls = []
    monkeypatch.setattr(main.notifier, "alert_recap", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(main.notifier, "recap_enabled", lambda: True)

    storage.open_virtual_position("MintRecapPortfolio", "PoolRecapPortfolio", 3.0, "RP", "RRP")

    previous_digest_time = datetime.now(timezone.utc) - timedelta(hours=1)
    main.maybe_send_recap(1305, 730, previous_digest_time)

    assert len(calls) == 1
    args, kwargs = calls[0]
    portfolio = args[6] if len(args) > 6 else kwargs.get("portfolio")
    assert portfolio is not None
    assert portfolio["invested_eur"] == 3.0


def test_maybe_send_recap_does_not_resend_within_same_slot(monkeypatch):
    """Rester dans le même créneau (ex. plusieurs cycles entre 13h00 et
    13h29) ne doit pas renvoyer le recap à chaque itération."""
    calls = []
    monkeypatch.setattr(main.notifier, "alert_recap", lambda *a, **k: calls.append((a, k)))
    last_slot, last_time = main.maybe_send_recap(1310, 1300, datetime.now(timezone.utc))
    assert last_slot == 1300
    assert calls == []


def test_maybe_send_recap_respects_recap_enabled_false(monkeypatch):
    """Le recap est désactivé via le réglage partagé : maybe_send_recap doit
    tout de même faire progresser l'état (last_notified_slot/last_digest_time)
    mais ne pas appeler alert_recap."""
    calls = []
    monkeypatch.setattr(main.notifier, "alert_recap", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(main.notifier, "recap_enabled", lambda: False)
    last_slot, _ = main.maybe_send_recap(1305, 730, datetime.now(timezone.utc) - timedelta(hours=1))
    assert last_slot == 1300
    assert calls == []


def test_evaluate_curve_candidates_stays_on_curve_before_migration(monkeypatch):
    storage.add_candidate("MintA", "MintA", "A", "AAA")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET status = 'on_curve' WHERE token_address = 'MintA'")
    monkeypatch.setattr(
        main.quotes, "fetch_curve_progress",
        lambda mints: {"MintA": {"market_cap_usd": 20000.0, "migrated": False}},
    )
    main.evaluate_curve_candidates()
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintA'"
        ).fetchone()["status"]
    assert status == "on_curve"


def test_evaluate_curve_candidates_promotes_to_pending_quote_on_migration(monkeypatch):
    """Régression du bug trouvé en revue finale : la migration seule ne
    suffit pas à promouvoir un candidat — il faut aussi que pool_address
    soit présent dans la réponse pump.fun, et pair_address doit devenir CE
    pool, jamais rester le mint placeholder posé par le détecteur. Depuis la
    vérification live du 2026-07-26, pool_address vient directement de
    fetch_curve_progress (plus de résolution séparée)."""
    storage.add_candidate("MintB", "MintB", "B", "BBB")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET status = 'on_curve' WHERE token_address = 'MintB'")
    monkeypatch.setattr(
        main.quotes, "fetch_curve_progress",
        lambda mints: {
            "MintB": {
                "market_cap_usd": 70000.0,
                "migrated": True,
                "pool_address": "RealPumpSwapPoolAddr",
            }
        },
    )
    main.evaluate_curve_candidates()
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, pair_address FROM candidates WHERE token_address = 'MintB'"
        ).fetchone()
    assert row["status"] == "pending_quote"
    assert row["pair_address"] == "RealPumpSwapPoolAddr"
    assert row["pair_address"] != "MintB"  # jamais le mint placeholder


def test_evaluate_curve_candidates_stays_on_curve_when_pool_not_yet_resolvable(monkeypatch):
    """Migration détectée par pump.fun, mais pool_address absent de la
    réponse (cas limite) : le candidat NE DOIT PAS être promu avec un
    pair_address bidon — il reste 'on_curve' pour être retenté au cycle
    suivant."""
    storage.add_candidate("MintH", "MintH", "H", "HHH")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET status = 'on_curve' WHERE token_address = 'MintH'")
    monkeypatch.setattr(
        main.quotes, "fetch_curve_progress",
        lambda mints: {"MintH": {"market_cap_usd": 70000.0, "migrated": True, "pool_address": None}},
    )
    main.evaluate_curve_candidates()
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, pair_address FROM candidates WHERE token_address = 'MintH'"
        ).fetchone()
    assert row["status"] == "on_curve"
    assert row["pair_address"] == "MintH"  # inchangé, toujours le placeholder


def test_full_lifecycle_from_detector_seed_resolves_real_pool_not_mint(monkeypatch):
    """Bout-en-bout réaliste : reproduit EXACTEMENT ce que font les vrais
    détecteurs (storage.add_candidate(mint, mint, ...) — pair_address = mint
    au départ, comme dans detectors/telegram_detector.py et
    detectors/onchain_detector.py), puis fait avancer ce candidat via le
    pipeline réel (audit -> courbe -> migration) et vérifie que pair_address
    finit par être le VRAI pool résolu, jamais le mint. C'est précisément le
    chemin que les tests précédents (qui injectaient un pair_address déjà
    correct via add_candidate("Mint", "Pool", ...)) ne parcouraient jamais —
    et où le bug de la revue finale se cachait."""
    mint = "RealMintFromDetector"
    real_pool = "RealPoolResolvedAfterMigration"

    # 1. Seed exactement comme le ferait un détecteur : pair_address == mint.
    storage.add_candidate(mint, mint, "H", "HHH", source="onchain")
    with storage._get_conn() as conn:
        seeded = conn.execute(
            "SELECT status, pair_address FROM candidates WHERE token_address = ?", (mint,)
        ).fetchone()
    assert seeded["status"] == "new"
    assert seeded["pair_address"] == mint

    # 2. Audit sécurité OK -> phase courbe.
    monkeypatch.setattr(main.security_check, "audit_token", lambda addr, holder_limit=None: (True, []))
    asyncio.run(main.process_new_candidates())
    with storage._get_conn() as conn:
        after_audit = conn.execute(
            "SELECT status FROM candidates WHERE token_address = ?", (mint,)
        ).fetchone()
    assert after_audit["status"] == "on_curve"

    # 3. Migration détectée + pool_address fourni directement par pump.fun
    #    (vérifié en direct le 2026-07-26 : /coins/{mint} renvoie déjà
    #    pool_address, pas besoin d'une résolution séparée) -> pending_quote
    #    avec le VRAI pool.
    monkeypatch.setattr(
        main.quotes, "fetch_curve_progress",
        lambda mints: {mint: {"market_cap_usd": 70000.0, "migrated": True, "pool_address": real_pool}},
    )
    main.evaluate_curve_candidates()
    with storage._get_conn() as conn:
        after_migration = conn.execute(
            "SELECT status, pair_address FROM candidates WHERE token_address = ?", (mint,)
        ).fetchone()
    assert after_migration["status"] == "pending_quote"
    assert after_migration["pair_address"] == real_pool
    assert after_migration["pair_address"] != mint

    # 4. evaluate_candidates() interroge bien fetch_pool_quotes avec le VRAI
    #    pool (pas le mint) — c'est ce qui était systématiquement cassé avant
    #    le fix (fetch_pool_quotes recevait le mint, /pairs/ ne le résout
    #    jamais, la baseline n'était donc jamais fixée).
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {
            real_pool.lower(): {"price": 0.005, "liquidity": 2000.0, "volume_h1": 300.0}
        } if real_pool in pools else {},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        after_first_quote = conn.execute(
            "SELECT status, baseline_liquidity_usd FROM candidates WHERE token_address = ?", (mint,)
        ).fetchone()
    assert after_first_quote["status"] == "watching"
    assert after_first_quote["baseline_liquidity_usd"] == 2000.0


def test_evaluate_curve_candidates_survives_missing_migrated_key(monkeypatch):
    """Réponse pump.fun malformée (champ 'migrated' absent) : le candidat ne
    doit ni planter le cycle ni être promu à tort — il reste 'on_curve'."""
    storage.add_candidate("MintZ", "MintZ", "Z", "ZZZ")
    with storage._get_conn() as conn:
        conn.execute("UPDATE candidates SET status = 'on_curve' WHERE token_address = 'MintZ'")
    monkeypatch.setattr(
        main.quotes, "fetch_curve_progress",
        lambda mints: {"MintZ": {"market_cap_usd": 20000.0}},  # pas de 'migrated'
    )
    main.evaluate_curve_candidates()  # ne doit pas lever
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintZ'"
        ).fetchone()["status"]
    assert status == "on_curve"


def test_evaluate_candidates_confirms_traction_using_solana_pool_quotes(monkeypatch):
    # Seuils fixés explicitement : le test ne doit pas dépendre de .env local.
    monkeypatch.setattr(config, "TRACTION_LIQUIDITY_MULT", 2.0)
    monkeypatch.setattr(config, "TRACTION_MIN_VOLUME_USD", 500.0)
    storage.add_candidate("MintC", "PoolC", "C", "CCC")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'watching', "
            "baseline_liquidity_usd = 1000, watched_since = ? "
            "WHERE token_address = 'MintC'",
            ((datetime.now(timezone.utc) - timedelta(minutes=config.TRACTION_WINDOW_MINUTES + 1)).isoformat(),),
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolc": {"price": 0.01, "liquidity": 3000.0, "volume_h1": 1000.0}},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintC'"
        ).fetchone()
    assert row["status"] == "confirmed"
    with storage._get_conn() as conn:
        pos = conn.execute(
            "SELECT source FROM positions WHERE token_address = 'MintC'"
        ).fetchone()
    assert pos["source"] == "onchain"


def test_evaluate_candidates_confirms_traction_preserves_telegram_source(monkeypatch):
    """Discrimine réellement le fix de get_candidates_by_status() : un
    candidat "onchain" (défaut) passerait la même assertion même sans la
    colonne 'source' dans le SELECT, à cause du repli .get(..., "onchain")
    dans evaluate_candidates() — seule une source non-défaut comme
    "telegram" ne peut venir que de la vraie colonne."""
    monkeypatch.setattr(config, "TRACTION_LIQUIDITY_MULT", 2.0)
    monkeypatch.setattr(config, "TRACTION_MIN_VOLUME_USD", 500.0)
    storage.add_candidate("MintG", "PoolG", "G", "GGG", source="telegram")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'watching', "
            "baseline_liquidity_usd = 1000, watched_since = ? "
            "WHERE token_address = 'MintG'",
            ((datetime.now(timezone.utc) - timedelta(minutes=config.TRACTION_WINDOW_MINUTES + 1)).isoformat(),),
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolg": {"price": 0.01, "liquidity": 3000.0, "volume_h1": 1000.0}},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        pos = conn.execute(
            "SELECT source FROM positions WHERE token_address = 'MintG'"
        ).fetchone()
    assert pos["source"] == "telegram"


def test_evaluate_candidates_survives_quote_missing_liquidity_key(monkeypatch):
    """Réponse DexScreener malformée (clé 'liquidity' absente) : le candidat
    en observation ne doit ni planter le cycle ni être rejeté/confirmé à tort."""
    storage.add_candidate("MintW", "PoolW", "W", "WWW")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'watching', "
            "baseline_liquidity_usd = 1000, watched_since = ? "
            "WHERE token_address = 'MintW'",
            ((datetime.now(timezone.utc) - timedelta(minutes=config.TRACTION_WINDOW_MINUTES + 1)).isoformat(),),
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolw": {"price": 0.01}},  # pas de 'liquidity'
    )
    main.evaluate_candidates()  # ne doit pas lever
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintW'"
        ).fetchone()["status"]
    assert status == "watching"


def test_evaluate_candidates_buys_immediately_for_telegram_source_no_traction_wait(monkeypatch):
    """Un candidat source='telegram' ne doit PAS passer par la phase 'watching'
    (60min + liquidité x2) : les calls Telegram portent généralement sur des
    coins déjà établis, dont la liquidité ne double pas en 1h juste parce
    qu'un salon en parle — attendre ferait rater l'entrée."""
    storage.add_candidate("MintT", "MintT", "T", "TTT", source="telegram")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = 'PoolT' "
            "WHERE token_address = 'MintT'",
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolt": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 50.0}},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        candidate = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintT'"
        ).fetchone()
        position = conn.execute(
            "SELECT source, invested_eur, status FROM positions WHERE token_address = 'MintT'"
        ).fetchone()
    assert candidate["status"] == "confirmed"
    assert position is not None
    assert position["source"] == "telegram"
    assert position["invested_eur"] == config.MEME_BUY_EUR
    assert position["status"] == "pending"  # prix d'entrée fixé au cycle refresh_positions suivant


def test_evaluate_candidates_buys_immediately_for_discord_source_no_traction_wait(monkeypatch):
    """Même raisonnement que pour telegram : un call Discord porte
    généralement sur un coin déjà établi, pas d'attente de traction."""
    storage.add_candidate("MintDsc", "MintDsc", "DSC", "DSCDSC", source="discord")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = 'PoolDsc' "
            "WHERE token_address = 'MintDsc'",
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"pooldsc": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 50.0}},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        candidate = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintDsc'"
        ).fetchone()
        position = conn.execute(
            "SELECT source, invested_eur, status FROM positions WHERE token_address = 'MintDsc'"
        ).fetchone()
    assert candidate["status"] == "confirmed"
    assert position is not None
    assert position["source"] == "discord"
    assert position["invested_eur"] == config.MEME_BUY_EUR
    assert position["status"] == "pending"


def test_evaluate_candidates_buys_immediately_for_momentum_source_no_traction_wait(monkeypatch):
    """Un candidat source='momentum' est par construction déjà en train de
    pumper (c'est le critère de détection) : même raisonnement que pour
    telegram, pas d'attente de traction."""
    storage.add_candidate("MintN", "MintN", "N", "NNN", source="momentum")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = 'PoolN' "
            "WHERE token_address = 'MintN'",
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"pooln": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 50.0}},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        candidate = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintN'"
        ).fetchone()
        position = conn.execute(
            "SELECT source FROM positions WHERE token_address = 'MintN'"
        ).fetchone()
    assert candidate["status"] == "confirmed"
    assert position["source"] == "momentum"


def test_evaluate_candidates_still_waits_for_traction_on_onchain_source(monkeypatch):
    """Comportement inchangé pour la source par défaut ('onchain') : premier
    quote -> 'watching' (pas d'achat immédiat), le filtre de traction garde
    tout son sens pour un token pump.fun tout juste migré."""
    storage.add_candidate("MintO", "MintO", "O", "OOO", source="onchain")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = 'PoolO' "
            "WHERE token_address = 'MintO'",
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolo": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 50.0}},
    )
    main.evaluate_candidates()
    with storage._get_conn() as conn:
        candidate = conn.execute(
            "SELECT status, baseline_liquidity_usd FROM candidates WHERE token_address = 'MintO'"
        ).fetchone()
        position = conn.execute(
            "SELECT 1 FROM positions WHERE token_address = 'MintO'"
        ).fetchone()
    assert candidate["status"] == "watching"
    assert candidate["baseline_liquidity_usd"] == 1000.0
    assert position is None


def test_refresh_positions_uses_solana_pool_quotes(monkeypatch):
    storage.open_virtual_position("MintD", "PoolD", 1.0, "D", "DDD")
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poold": {"price": 0.02, "liquidity": 500.0, "volume_h1": 100.0}},
    )
    main.refresh_positions()
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, entry_price_usd FROM positions WHERE token_address = 'MintD'"
        ).fetchone()
    assert row["status"] == "open"
    assert row["entry_price_usd"] == 0.02


def test_refresh_positions_persists_current_multiplier(monkeypatch):
    """current_multiplier doit être mis à jour à CHAQUE cycle (pas seulement
    quand un nouveau peak est atteint) — c'est ce que lit
    get_portfolio_summary() pour la valeur mark-to-market du recap."""
    storage.open_virtual_position("MintCM2", "PoolCM2", 1.0, "CM2", "CM2CM2")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintCM2'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)  # status -> 'open', peak toujours 1.0
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        # multiplicateur 0.8 : sous le peak actuel (1.0) donc PAS un nouveau
        # peak, mais current_multiplier doit quand même être mis à jour.
        lambda pools: {"poolcm2": {"price": 0.008, "liquidity": 500.0, "volume_h1": 100.0}},
    )
    main.refresh_positions()
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT current_multiplier, peak_multiplier FROM positions WHERE id = ?", (pos_id,)
        ).fetchone()
    assert row["current_multiplier"] == pytest.approx(0.8)
    assert row["peak_multiplier"] == 1.0


def test_refresh_positions_survives_quote_missing_liquidity_key(monkeypatch):
    """Réponse DexScreener malformée (clé 'liquidity' absente) pour une position
    déjà ouverte : ne doit ni planter le cycle ni marquer la position morte
    à tort, faute de pouvoir vérifier la liquidité."""
    storage.open_virtual_position("MintV", "PoolV", 1.0, "V", "VVV")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintV'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)  # status -> 'open'
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolv": {"price": 0.02}},  # pas de 'liquidity'
    )
    main.refresh_positions()  # ne doit pas lever
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM positions WHERE token_address = 'MintV'"
        ).fetchone()["status"]
    assert status == "open"


def test_evaluate_candidates_sends_buy_alert_for_telegram_source_direct_buy(monkeypatch):
    storage.add_candidate("MintAlertT", "MintAlertT", "AT", "ATAT", source="telegram")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = 'PoolAlertT' "
            "WHERE token_address = 'MintAlertT'",
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolalertt": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 50.0}},
    )
    calls = []
    monkeypatch.setattr(main.notifier, "alert_virtual_buy", lambda *a, **k: calls.append((a, k)))
    main.evaluate_candidates()
    assert len(calls) == 1
    args = calls[0][0]
    assert args[0] == "MintAlertT"
    assert args[3] == "PoolAlertT"
    assert args[4] == config.MEME_BUY_EUR
    assert args[5] == "telegram"


def test_evaluate_candidates_sends_buy_alert_for_onchain_traction_confirmed(monkeypatch):
    monkeypatch.setattr(config, "TRACTION_LIQUIDITY_MULT", 2.0)
    monkeypatch.setattr(config, "TRACTION_MIN_VOLUME_USD", 500.0)
    storage.add_candidate("MintAlertO", "PoolAlertO", "AO", "AOAO", source="onchain")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'watching', "
            "baseline_liquidity_usd = 1000, watched_since = ? "
            "WHERE token_address = 'MintAlertO'",
            ((datetime.now(timezone.utc) - timedelta(minutes=config.TRACTION_WINDOW_MINUTES + 1)).isoformat(),),
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"poolalerto": {"price": 0.01, "liquidity": 3000.0, "volume_h1": 1000.0}},
    )
    calls = []
    monkeypatch.setattr(main.notifier, "alert_virtual_buy", lambda *a, **k: calls.append((a, k)))
    main.evaluate_candidates()
    assert len(calls) == 1
    args = calls[0][0]
    assert args[0] == "MintAlertO"
    assert args[5] == "onchain"


def test_evaluate_candidates_does_not_renotify_when_position_already_exists_telegram(monkeypatch):
    """Bug corrigé le 30/07/2026 : si une position existe déjà pour ce
    token_address (ex. déjà achetée via un autre candidat/détecteur pour le
    même token), open_virtual_position() est un no-op — mais le code
    envoyait quand même une alerte d'achat, laissant croire à un 2e achat
    réel qui n'a jamais eu lieu."""
    storage.open_virtual_position("MintDupT", "PoolExisting", 1.0, "DT", "DTDT", source="onchain")

    storage.add_candidate("MintDupT", "MintDupT", "DT", "DTDT", source="telegram")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = 'PoolDupT' "
            "WHERE token_address = 'MintDupT' AND source = 'telegram'",
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"pooldupt": {"price": 0.01, "liquidity": 1000.0, "volume_h1": 50.0}},
    )
    calls = []
    monkeypatch.setattr(main.notifier, "alert_virtual_buy", lambda *a, **k: calls.append((a, k)))
    main.evaluate_candidates()
    assert calls == []


def test_evaluate_candidates_does_not_renotify_when_position_already_exists_onchain(monkeypatch):
    monkeypatch.setattr(config, "TRACTION_LIQUIDITY_MULT", 2.0)
    monkeypatch.setattr(config, "TRACTION_MIN_VOLUME_USD", 500.0)
    storage.open_virtual_position("MintDupO", "PoolExistingO", 1.0, "DO", "DODO", source="onchain")

    storage.add_candidate("MintDupO", "PoolDupO", "DO", "DODO", source="onchain")
    with storage._get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'watching', "
            "baseline_liquidity_usd = 1000, watched_since = ? "
            "WHERE token_address = 'MintDupO'",
            ((datetime.now(timezone.utc) - timedelta(minutes=config.TRACTION_WINDOW_MINUTES + 1)).isoformat(),),
        )
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        lambda pools: {"pooldupo": {"price": 0.01, "liquidity": 3000.0, "volume_h1": 1000.0}},
    )
    calls = []
    monkeypatch.setattr(main.notifier, "alert_virtual_buy", lambda *a, **k: calls.append((a, k)))
    main.evaluate_candidates()
    assert calls == []
    # Le candidat doit quand même être marqué 'confirmed' (traction réelle),
    # même si aucune 2e position/alerte n'est créée.
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintDupO'"
        ).fetchone()["status"]
    assert status == "confirmed"


def test_refresh_positions_sends_sell_alert_on_stop_loss(monkeypatch):
    storage.open_virtual_position("MintAlertS", "PoolAlertS", 1.0, "AS", "ASAS", source="momentum")
    with storage._get_conn() as conn:
        pos_id = conn.execute(
            "SELECT id FROM positions WHERE token_address = 'MintAlertS'"
        ).fetchone()["id"]
    storage.set_entry_price(pos_id, 0.01)  # status -> 'open'
    monkeypatch.setattr(
        main.quotes, "fetch_pool_quotes",
        # multiplicateur 0.3, sous le stop par défaut (0.5) -> clôture
        lambda pools: {"poolalerts": {"price": 0.003, "liquidity": 500.0, "volume_h1": 100.0}},
    )
    calls = []
    monkeypatch.setattr(main.notifier, "alert_virtual_sell", lambda *a, **k: calls.append((a, k)))
    main.refresh_positions()
    assert len(calls) == 1
    args = calls[0][0]
    assert args[0] == "MintAlertS"
    assert args[5] is False  # is_profit
    assert args[6] == "momentum"


def test_process_new_candidate_blocks_when_audit_fails(monkeypatch):
    storage.add_candidate("MintE", "MintE", "E", "EEE")
    monkeypatch.setattr(
        main.security_check, "audit_token",
        lambda addr, holder_limit=None: (False, ["autorité de mint active"]),
    )
    asyncio.run(main.process_new_candidates())
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, reject_reason FROM candidates WHERE token_address = 'MintE'"
        ).fetchone()
    assert row["status"] == "rejected"
    assert "mint" in row["reject_reason"]


def test_process_new_candidate_moves_to_on_curve_when_audit_passes(monkeypatch):
    storage.add_candidate("MintF", "MintF", "F", "FFF")
    monkeypatch.setattr(main.security_check, "audit_token", lambda addr, holder_limit=None: (True, []))
    asyncio.run(main.process_new_candidates())
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintF'"
        ).fetchone()
    assert row["status"] == "on_curve"


def test_process_new_candidates_survives_audit_exception(monkeypatch):
    """Un audit qui lève (timeout réseau, etc.) ne doit ni planter le cycle
    ni faire disparaître le candidat : il reste 'new' pour être réessayé."""
    storage.add_candidate("MintX", "MintX", "X", "XXX")

    def _boom(addr, holder_limit=None):
        raise RuntimeError("RugCheck injoignable")

    monkeypatch.setattr(main.security_check, "audit_token", _boom)
    asyncio.run(main.process_new_candidates())  # ne doit pas lever
    with storage._get_conn() as conn:
        status = conn.execute(
            "SELECT status FROM candidates WHERE token_address = 'MintX'"
        ).fetchone()["status"]
    assert status == "new"


def test_process_new_candidates_passes_telegram_threshold_for_telegram_source(monkeypatch):
    storage.add_candidate("MintHolderT", "MintHolderT", "HT", "HTHT", source="telegram")
    calls = []
    monkeypatch.setattr(
        main.security_check, "audit_token",
        lambda addr, holder_limit=None: (calls.append((addr, holder_limit)) or True, []),
    )
    asyncio.run(main.process_new_candidates())
    assert len(calls) == 1
    assert calls[0] == ("MintHolderT", config.TELEGRAM_MAX_TOP_HOLDER_PERCENT)


def test_process_new_candidates_uses_default_threshold_for_onchain_source(monkeypatch):
    storage.add_candidate("MintHolderO", "MintHolderO", "HO", "HOHO", source="onchain")
    calls = []
    monkeypatch.setattr(
        main.security_check, "audit_token",
        lambda addr, holder_limit=None: (calls.append((addr, holder_limit)) or True, []),
    )
    asyncio.run(main.process_new_candidates())
    assert len(calls) == 1
    assert calls[0] == ("MintHolderO", None)


def test_process_new_candidates_uses_default_threshold_for_momentum_source(monkeypatch):
    storage.add_candidate("MintHolderMo", "PoolHolderMo", "HM", "HMHM", source="momentum")
    calls = []
    monkeypatch.setattr(
        main.security_check, "audit_token",
        lambda addr, holder_limit=None: (calls.append((addr, holder_limit)) or True, []),
    )
    asyncio.run(main.process_new_candidates())
    assert len(calls) == 1
    assert calls[0] == ("MintHolderMo", None)


def test_process_new_candidates_promotes_momentum_source_directly_to_pending_quote(monkeypatch):
    """Un candidat source='momentum' connaît déjà son pool réel (voir
    detectors/momentum_detector.py) : après audit OK, il doit passer
    directement en 'pending_quote' avec ce pool, sans phase 'on_curve'
    (contrairement à onchain/telegram, qui attendent une migration
    pump.fun)."""
    storage.add_candidate("MintM", "PoolM", "M", "MMM", source="momentum")
    monkeypatch.setattr(main.security_check, "audit_token", lambda addr, holder_limit=None: (True, []))
    asyncio.run(main.process_new_candidates())
    with storage._get_conn() as conn:
        row = conn.execute(
            "SELECT status, pair_address FROM candidates WHERE token_address = 'MintM'"
        ).fetchone()
    assert row["status"] == "pending_quote"
    assert row["pair_address"] == "PoolM"


def test_process_new_candidates_respects_max_audits_per_cycle(monkeypatch):
    """Plus de candidats 'new' que MAX_AUDITS_PER_CYCLE ne doit auditer que
    le nombre plafonné en un cycle — le reste doit rester 'new' pour être
    retenté au cycle suivant (get_new_candidates() les re-sélectionnera)."""
    monkeypatch.setattr(config, "MAX_AUDITS_PER_CYCLE", 2)
    monkeypatch.setattr(config, "RUGCHECK_MIN_INTERVAL_SECONDS", 0)
    for i in range(4):
        storage.add_candidate(f"MintCap{i}", f"MintCap{i}", f"C{i}", f"CC{i}")

    calls = []
    monkeypatch.setattr(
        main.security_check, "audit_token",
        lambda addr, holder_limit=None: (calls.append(addr) or True, []),
    )
    asyncio.run(main.process_new_candidates())

    assert len(calls) == 2

    with storage._get_conn() as conn:
        rows = conn.execute(
            "SELECT token_address, status FROM candidates WHERE token_address LIKE 'MintCap%'"
        ).fetchall()
    statuses = {r["token_address"]: r["status"] for r in rows}
    n_on_curve = sum(1 for s in statuses.values() if s == "on_curve")
    n_new = sum(1 for s in statuses.values() if s == "new")
    assert n_on_curve == 2
    assert n_new == 2


def test_process_new_candidates_rate_limits_between_calls(monkeypatch):
    """Les appels audit_token() successifs doivent être espacés d'au moins
    RUGCHECK_MIN_INTERVAL_SECONDS, MAIS sans délai avant le tout premier
    appel (le sleep est ENTRE les candidats, pas avant le premier)."""
    interval = 0.05
    monkeypatch.setattr(config, "RUGCHECK_MIN_INTERVAL_SECONDS", interval)
    monkeypatch.setattr(config, "MAX_AUDITS_PER_CYCLE", 10)
    n = 3
    for i in range(n):
        storage.add_candidate(f"MintRate{i}", f"MintRate{i}", f"R{i}", f"RR{i}")

    monkeypatch.setattr(main.security_check, "audit_token", lambda addr, holder_limit=None: (True, []))

    start = time.perf_counter()
    asyncio.run(main.process_new_candidates())
    elapsed = time.perf_counter() - start

    # Au moins (n-1) intervalles doivent avoir été attendus, avec une petite
    # tolérance pour ne pas rendre le test flaky.
    assert elapsed >= (n - 1) * interval * 0.8
    # Ne doit pas non plus ressembler à n intervalles (ce qui indiquerait un
    # sleep avant le premier appel en plus des sleeps entre appels).
    assert elapsed < n * interval + 0.5


def test_process_new_candidates_audit_call_does_not_block_event_loop(monkeypatch):
    """audit_token() est un appel bloquant côté security_check.py (requests +
    time.sleep() en retry) — exécuté via asyncio.to_thread(), il ne doit PAS
    geler la boucle asyncio pendant sa propre durée, pas seulement entre deux
    candidats. On le prouve en faisant tourner une tâche 'ticker' concurrente
    (via asyncio.gather) pendant l'audit simulé (time.sleep(0.2) synchrone
    côté mock) et en vérifiant qu'elle progresse PENDANT la fenêtre de
    l'audit — impossible si l'event loop était gelé par un appel synchrone
    direct."""
    storage.add_candidate("MintBlock", "MintBlock", "BL", "BLBL")

    audit_window = {}

    def _slow_audit(addr, holder_limit=None):
        audit_window["start"] = time.perf_counter()
        time.sleep(0.2)  # simule un appel réseau lent bloquant
        audit_window["end"] = time.perf_counter()
        return True, []

    monkeypatch.setattr(main.security_check, "audit_token", _slow_audit)

    tick_times = []

    async def _ticker():
        for _ in range(15):
            await asyncio.sleep(0.02)
            tick_times.append(time.perf_counter())

    async def _run_both():
        await asyncio.gather(main.process_new_candidates(), _ticker())

    asyncio.run(_run_both())

    assert "start" in audit_window and "end" in audit_window
    ticks_during_audit = [
        t for t in tick_times if audit_window["start"] <= t <= audit_window["end"]
    ]
    # Si l'event loop avait été gelé par un appel bloquant direct (pas de
    # asyncio.to_thread), aucun tick du ticker n'aurait pu s'exécuter pendant
    # cette fenêtre — on en attend plusieurs.
    assert len(ticks_during_audit) >= 3
