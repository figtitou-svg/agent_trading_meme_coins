"""
main.py — Orchestrateur du watcher memecoins Solana/pump.fun.

Lance deux détecteurs indépendants en tâches asyncio isolées (Telegram +
on-chain), et fait progresser les candidats qu'ils produisent via un cycle de
confirmation à deux phases :

  1. Audit sécurité (RugCheck) sur les candidats bruts ('new').
  2. Phase courbe ('on_curve') : suivi de la progression pump.fun jusqu'à la
     migration vers un pool PumpSwap (l'AMM de pump.fun).
  3. Phase post-migration ('watching') : traction confirmée par liquidité +
     volume réels (DexScreener), comme l'ancien filtre EVM mais sur des
     cotations Solana.

Remplace entièrement l'ancien sniper Uniswap V2 (EVM) — aucune exécution
réelle dans cette phase : observation uniquement (positions virtuelles).
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from watcher import command_listener
from watcher import config
from watcher import notifier
from watcher import quotes
from watcher import security_check
from watcher import storage
from watcher.detectors import discord_detector, momentum_detector, onchain_detector, telegram_detector

_PARIS_TZ = ZoneInfo("Europe/Paris")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("watcher")


async def process_new_candidates() -> None:
    """
    Audite chaque candidat brut ('new') via RugCheck : rejeté si l'audit
    échoue, promu en 'on_curve' sinon. Ne lève jamais d'exception : un souci
    réseau sur un candidat ne doit jamais bloquer les autres.

    Débit volontairement limité (RUGCHECK_MIN_INTERVAL_SECONDS entre chaque
    appel, MAX_AUDITS_PER_CYCLE candidats max par cycle) : l'API RugCheck
    gratuite limite à ~1 req/s, et cette fonction tourne dans la boucle
    asyncio partagée avec les détecteurs — un burst d'appels bloquants
    déclencherait des 429 en continu ET figerait les détecteurs pendant la
    durée du burst. asyncio.sleep() (pas time.sleep()) laisse la boucle
    servir les autres tâches pendant l'attente entre deux audits.

    L'appel audit_token() lui-même reste synchrone (requests + jusqu'à
    RUGCHECK_RETRIES tentatives, chacune espacée de RUGCHECK_RETRY_DELAY en
    time.sleep() bloquant côté security_check.py) — dans le pire cas (réseau
    lent/RugCheck qui timeout), ça peut bloquer plusieurs secondes. Il est
    donc exécuté via asyncio.to_thread() dans un thread worker séparé, pour
    que la boucle asyncio continue de servir les détecteurs pendant TOUTE la
    durée de l'audit, pas seulement entre deux candidats.
    """
    candidates = storage.get_new_candidates()[:config.MAX_AUDITS_PER_CYCLE]
    for i, candidate in enumerate(candidates):
        if i > 0:
            await asyncio.sleep(config.RUGCHECK_MIN_INTERVAL_SECONDS)
        try:
            # Seuil de concentration plus permissif pour telegram (31/07/2026,
            # décision utilisateur) — voir config.TELEGRAM_MAX_TOP_HOLDER_PERCENT.
            holder_limit = (
                config.TELEGRAM_MAX_TOP_HOLDER_PERCENT
                if candidate.get("source") == "telegram"
                else None
            )
            is_safe, reasons = await asyncio.to_thread(
                security_check.audit_token, candidate["token_address"], holder_limit
            )
            if is_safe:
                if candidate.get("source") == "momentum":
                    # Le pool est déjà connu (voir watcher/detectors/momentum_detector.py) :
                    # pas de phase 'on_curve' à attendre, passage direct en
                    # attente de première cotation.
                    storage.promote_candidate_to_pending_quote(candidate["id"], candidate["pair_address"])
                    logger.info(
                        "Candidat #%d (momentum) audité OK — pool déjà connu, attente de cotation",
                        candidate["id"],
                    )
                else:
                    storage.promote_candidate_to_on_curve(candidate["token_address"])
                    logger.info("Candidat #%d audité OK — phase courbe", candidate["id"])
            else:
                storage.reject_candidate_by_token(candidate["token_address"], "; ".join(reasons))
                logger.info("Candidat #%d rejeté à l'audit : %s", candidate["id"], reasons)
        except Exception as exc:
            logger.warning("Candidat #%d ignoré ce cycle (audit) : %s", candidate["id"], exc)


def evaluate_curve_candidates() -> None:
    """
    Fait progresser les candidats en phase courbe ('on_curve') :
      - reste 'on_curve' tant que la migration PumpSwap n'est pas confirmée.
      - dès que pump.fun signale la migration, lit l'adresse du pool
        directement dans la même réponse (quotes.fetch_curve_progress —
        vérifié en direct le 2026-07-26, pool_address est déjà fourni par
        /coins/{mint}, plus besoin d'une résolution séparée) et passe à
        'pending_quote' avec cette adresse comme pair_address. Si
        pool_address est absent de la réponse (cas limite), le candidat
        reste 'on_curve' pour être retenté au cycle suivant plutôt que
        d'être promu avec un pair_address bidon (le mint), qui ne serait
        jamais interrogeable par fetch_pool_quotes (endpoint /pairs/, qui
        attend une adresse de POOL, pas de mint).

    Ne lève jamais d'exception : un souci réseau ne doit pas tuer le watcher.
    """
    on_curve = storage.get_candidates_by_status("on_curve")
    if not on_curve:
        return
    progress = quotes.fetch_curve_progress([c["token_address"] for c in on_curve])
    for c in on_curve:
        try:
            info = progress.get(c["token_address"])
            if info and info["migrated"]:
                pool_address = info.get("pool_address")
                if pool_address:
                    storage.promote_candidate_to_pending_quote(c["id"], pool_address)
                    logger.info(
                        "Candidat #%d migré (pool %s) — phase post-migration",
                        c["id"], pool_address,
                    )
                else:
                    logger.info(
                        "Candidat #%d migré mais pool_address absent de la réponse — reste en observation",
                        c["id"],
                    )
        except Exception as exc:
            logger.warning("Candidat #%d (on_curve) ignoré ce cycle : %s", c["id"], exc)


def evaluate_candidates() -> None:
    """
    Fait progresser les candidats post-migration (mêmes règles que l'ancien
    filtre EVM, sur des cotations Solana) :
      pending_quote → watching : première cotation DexScreener disponible.
      watching → rejected : liquidité effondrée, ou jamais coté après
        PENDING_TIMEOUT_MINUTES.
      watching → confirmed : après TRACTION_WINDOW_MINUTES, si liquidité
        x TRACTION_LIQUIDITY_MULT et volume 1h >= TRACTION_MIN_VOLUME_USD.

    Ne lève jamais d'exception : un souci réseau ne doit pas tuer le watcher.
    """
    pending = storage.get_candidates_by_status("pending_quote")
    watching = storage.get_candidates_by_status("watching")
    candidates = pending + watching
    if not candidates:
        return

    pool_quotes = quotes.fetch_pool_quotes([c["pair_address"] for c in candidates])
    now = datetime.now(timezone.utc)

    for c in pending:
        try:
            info = pool_quotes.get(c["pair_address"].lower())
            if info and info["price"] and info["liquidity"]:
                if c.get("source") in ("telegram", "discord", "momentum"):
                    # telegram/discord : un call porte généralement sur un
                    # coin déjà établi, pas un token pump.fun tout juste
                    # migré. momentum : par construction, le token est DÉJÀ
                    # en train de pumper (c'est le critère de détection
                    # lui-même). Dans tous les cas, le filtre de traction
                    # (60min, liquidité x2) n'a pas de sens : attendre
                    # ferait rater l'entrée. Achat direct dès la première
                    # cotation disponible.
                    source = c.get("source", "telegram")
                    storage.confirm_candidate(c["id"])
                    position_created = storage.open_virtual_position(
                        c["token_address"], c["pair_address"], config.MEME_BUY_EUR,
                        c["token_name"], c["token_symbol"], source=source,
                    )
                    # position_created est False si une position existe déjà
                    # pour ce token_address (ex. déjà achetée via un autre
                    # candidat/détecteur) : ne JAMAIS notifier un achat qui
                    # n'a pas réellement eu lieu.
                    if position_created:
                        notifier.alert_virtual_buy(
                            c["token_address"], c["token_name"], c["token_symbol"],
                            c["pair_address"], config.MEME_BUY_EUR, source,
                        )
                        logger.info(
                            "🚀 Achat virtuel direct #%d (%s, source %s, sans traction) : %.2f€",
                            c["id"], c["token_symbol"] or c["token_address"], source, config.MEME_BUY_EUR,
                        )
                    continue
                storage.set_candidate_baseline(c["id"], float(info["liquidity"]), float(info["price"]))
                logger.info(
                    "Candidat #%d (%s) : baseline fixée, liquidité %.0f$ — observation %d min démarrée",
                    c["id"], c["token_symbol"] or c["token_address"], info["liquidity"],
                    config.TRACTION_WINDOW_MINUTES,
                )
            else:
                age_min = (now - datetime.fromisoformat(c["detected_at"])).total_seconds() / 60
                if age_min > config.PENDING_TIMEOUT_MINUTES:
                    storage.reject_candidate(c["id"], "jamais coté (rug avant indexation)")
                    logger.info("Candidat #%d rejeté : jamais coté après %d min", c["id"], config.PENDING_TIMEOUT_MINUTES)
        except Exception as exc:
            logger.warning("Candidat #%d (pending_quote) ignoré ce cycle : %s", c["id"], exc)

    for c in watching:
        try:
            info = pool_quotes.get(c["pair_address"].lower())
            liquidity = info["liquidity"] if info else None

            if not info or not info["price"] or (liquidity or 0) < config.DEAD_LIQUIDITY_USD:
                storage.reject_candidate(c["id"], f"liquidité effondrée pendant l'observation ({liquidity}$)")
                logger.info("Candidat #%d rejeté : liquidité effondrée", c["id"])
                continue

            watched_min = (now - datetime.fromisoformat(c["watched_since"])).total_seconds() / 60
            if watched_min < config.TRACTION_WINDOW_MINUTES:
                continue  # encore en observation

            liquidity_growth = (
                liquidity / c["baseline_liquidity_usd"] if c["baseline_liquidity_usd"] else 0
            )
            volume_h1 = info.get("volume_h1") or 0

            if liquidity_growth >= config.TRACTION_LIQUIDITY_MULT and volume_h1 >= config.TRACTION_MIN_VOLUME_USD:
                source = c.get("source", "onchain")
                storage.confirm_candidate(c["id"])
                position_created = storage.open_virtual_position(
                    c["token_address"], c["pair_address"], config.MEME_BUY_EUR,
                    c["token_name"], c["token_symbol"], source=source,
                )
                if position_created:
                    notifier.alert_virtual_buy(
                        c["token_address"], c["token_name"], c["token_symbol"],
                        c["pair_address"], config.MEME_BUY_EUR, source,
                    )
                    logger.info(
                        "🚀 Traction confirmée #%d (%s) : liquidité x%.1f, volume 1h %.0f$ — achat virtuel %.2f€",
                        c["id"], c["token_symbol"] or c["token_address"],
                        liquidity_growth, volume_h1, config.MEME_BUY_EUR,
                    )
            else:
                storage.reject_candidate(
                    c["id"],
                    f"pas de traction après {config.TRACTION_WINDOW_MINUTES}min "
                    f"(liquidité x{liquidity_growth:.1f}, volume 1h {volume_h1:.0f}$)",
                )
                logger.info(
                    "Candidat #%d rejeté : traction insuffisante (liquidité x%.1f, volume 1h %.0f$)",
                    c["id"], liquidity_growth, volume_h1,
                )
        except Exception as exc:
            logger.warning("Candidat #%d (watching) ignoré ce cycle : %s", c["id"], exc)


def refresh_positions() -> None:
    """
    Met à jour les positions virtuelles à chaque cycle (logique de trailing
    stop / take profit inchangée depuis l'ancien sniper EVM, sur des
    cotations Solana). Ne lève jamais d'exception.
    """
    positions = storage.get_active_positions()
    if not positions:
        return

    pool_quotes = quotes.fetch_pool_quotes([p["pair_address"] for p in positions])
    now = datetime.now(timezone.utc)

    for pos in positions:
        try:
            info = pool_quotes.get(pos["pair_address"].lower())

            if pos["status"] == "pending":
                if info and info["price"]:
                    storage.set_entry_price(pos["id"], float(info["price"]))
                    logger.info("Prix d'entrée fixé #%d : %s $", pos["id"], info["price"])
                else:
                    age_min = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60
                    if age_min > config.PENDING_TIMEOUT_MINUTES:
                        storage.mark_position_dead(pos["id"])
                        logger.info(
                            "Position #%d abandonnée : jamais cotée après %d min (rug précoce)",
                            pos["id"], config.PENDING_TIMEOUT_MINUTES,
                        )
                continue

            liquidity = info["liquidity"] if info else None
            if not info or not info["price"] or (liquidity or 0) < config.DEAD_LIQUIDITY_USD:
                storage.mark_position_dead(pos["id"])
                logger.info("Position #%d marquée morte (liquidité : %s$)", pos["id"], liquidity)
                continue

            current_price = float(info["price"])
            entry_price = float(pos["entry_price_usd"])
            current_multiplier = current_price / entry_price if entry_price > 0 else 0
            storage.update_current_multiplier(pos["id"], current_multiplier)

            old_peak = pos.get("peak_multiplier")
            old_peak = old_peak if old_peak is not None else 1.0
            peak = max(old_peak, current_multiplier)

            if peak > old_peak:
                storage.update_peak_multiplier(pos["id"], peak)

            stop_multiplier = notifier.compute_stop_multiplier(peak)

            if current_multiplier <= stop_multiplier:
                is_profit = current_multiplier > 1.0
                storage.mark_position_closed(pos["id"], current_multiplier, is_profit)
                notifier.alert_virtual_sell(
                    pos["token_address"], pos["token_name"], pos["token_symbol"],
                    current_multiplier, pos["invested_eur"], is_profit,
                    pos.get("source", "onchain"),
                )
                action = "Take Profit" if is_profit else "Stop Loss"
                logger.info(
                    "Position #%d clôturée (%s) : x%.2f (Record atteint: x%.2f)",
                    pos["id"], action, current_multiplier, peak,
                )
        except Exception as exc:
            logger.warning("Position #%d ignorée ce cycle : %s", pos["id"], exc)


_SCHEDULED_RECAP_SLOTS = [730, 1300, 1800, 2200]


def maybe_send_recap(
    current_time_val: int, last_notified_slot: int, last_digest_time: datetime,
) -> tuple[int, datetime]:
    """Décide si un créneau de recap programmé (07h30/13h/18h/22h, heure de
    Paris) vient d'être franchi et, si oui, envoie le recap et renvoie l'état
    mis à jour (last_notified_slot, last_digest_time). Extrait de
    `confirmation_loop()` pour être testable sans boucle asyncio infinie ni
    mock de `asyncio.sleep`.

    `current_time_val` est l'heure locale encodée en HHMM (ex. 1305 pour
    13h05) ; `last_notified_slot` vaut -1 avant le premier créneau franchi
    depuis le démarrage — dans ce cas précis, aucun recap n'est envoyé (rien
    à récapituler avant le tout premier créneau), seul l'état est initialisé.
    """
    triggered_slot = -1
    for slot in _SCHEDULED_RECAP_SLOTS:
        if current_time_val >= slot:
            triggered_slot = slot
    if triggered_slot == -1 or triggered_slot == last_notified_slot:
        return last_notified_slot, last_digest_time

    if last_notified_slot != -1:
        since_iso = last_digest_time.isoformat()
        n_watched = storage.count_candidates_watched_since(since_iso)
        n_bought = storage.count_positions_opened_since(since_iso)
        closed = storage.get_positions_closed_since(since_iso)
        n_wins = sum(1 for p in closed if p["status"] == "closed_profit")
        n_losses = sum(1 for p in closed if p["status"] == "closed_loss")
        pnl_eur = sum(
            p["invested_eur"] * (p["exit_multiplier"] - 1)
            for p in closed if p["exit_multiplier"] is not None
        )
        by_source = storage.count_candidates_by_source_since(since_iso)
        portfolio = storage.get_portfolio_summary()
        if notifier.recap_enabled():
            notifier.alert_recap(n_watched, n_bought, n_wins, n_losses, pnl_eur, by_source, portfolio)

    return triggered_slot, datetime.now(timezone.utc)


async def confirmation_loop() -> None:
    """Cycle de confirmation périodique (audit -> courbe -> post-migration ->
    positions), isolé du crash d'un détecteur individuel. Envoie aussi le
    recap Telegram condensé aux créneaux programmés (07h30/13h/18h/22h,
    heure de Paris)."""
    last_notified_slot = -1
    last_digest_time = datetime.now(timezone.utc)

    while True:
        try:
            storage.touch_heartbeat()
            await process_new_candidates()
            evaluate_curve_candidates()
            evaluate_candidates()
            refresh_positions()

            now_local = datetime.now(_PARIS_TZ)
            current_time_val = now_local.hour * 100 + now_local.minute
            last_notified_slot, last_digest_time = maybe_send_recap(
                current_time_val, last_notified_slot, last_digest_time,
            )
        except Exception as exc:
            logger.error("Erreur dans le cycle de confirmation : %s", exc)
        await asyncio.sleep(config.CONFIRMATION_LOOP_INTERVAL_SECONDS)


def telegram_detector_enabled() -> bool:
    """
    Le détecteur Telegram ne démarre que si des identifiants d'application
    sont configurés (my.telegram.org) — sans ça, Telethon échoue à chaque
    tentative et le détecteur boucle indéfiniment en reconnexion pour rien.
    Pas besoin de TELEGRAM_CHANNELS ici : on peut avoir des identifiants
    prêts avant d'avoir choisi des groupes (mis de côté volontairement tant
    qu'aucun groupe de confiance n'a été identifié — cf. design spec).
    """
    return bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH)


def command_listener_enabled() -> bool:
    """Le listener de commandes ne démarre que si le bot de notifications est
    configuré (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) — sans ça, personne à
    qui répondre."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def discord_detector_enabled() -> bool:
    """
    Le détecteur Discord ne démarre que si un token de bot est configuré —
    sans ça, discord.py échoue à chaque tentative et le détecteur boucle
    indéfiniment en reconnexion pour rien. Pas besoin de DISCORD_CHANNEL_IDS
    ici : on peut avoir un bot prêt avant d'avoir choisi des salons.
    """
    return bool(config.DISCORD_BOT_TOKEN)


async def run() -> None:
    """Lance les détecteurs actifs et le cycle de confirmation en parallèle,
    indéfiniment."""
    logger.info("Démarrage du watcher — mode OBSERVATION (aucun achat réel)")
    tasks = [
        onchain_detector.start(),
        momentum_detector.start(),
        asyncio.create_task(confirmation_loop()),
    ]
    if telegram_detector_enabled():
        tasks.append(telegram_detector.start())
    else:
        logger.info(
            "Détecteur Telegram désactivé (TELEGRAM_API_ID/TELEGRAM_API_HASH non configurés)"
        )
    if discord_detector_enabled():
        tasks.append(discord_detector.start())
    else:
        logger.info("Détecteur Discord désactivé (DISCORD_BOT_TOKEN non configuré)")
    if command_listener_enabled():
        tasks.append(command_listener.start())
    else:
        logger.info(
            "Listener de commandes désactivé (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID non configurés)"
        )
    await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watcher memecoins Solana/pump.fun — observation uniquement."
    )
    parser.add_argument(
        "--listen", action="store_true",
        help="Démarre les détecteurs Telegram + on-chain et le cycle de confirmation.",
    )
    args = parser.parse_args()

    storage.init_db()

    if args.listen:
        asyncio.run(run())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
