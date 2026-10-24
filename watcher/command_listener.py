"""
watcher/command_listener.py — Écoute les messages entrants du bot Telegram et répond
aux commandes /recap (état des lieux à la demande) et /sell (clôture
manuelle d'une position ouverte).

Long-polling getUpdates (pas de webhook, pas de serveur HTTP à exposer) —
même esprit que les détecteurs (watcher/detectors/onchain_detector.py) : une tâche
asyncio isolée, résiliente aux erreurs réseau. L'appel getUpdates est
synchrone (httpx.get, timeout=25s côté Telegram) mais exécuté via
asyncio.to_thread() pour ne jamais geler la boucle partagée pendant
l'attente — même pattern que l'audit RugCheck dans main.process_new_candidates().
"""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from watcher import config
from watcher import notifier
from watcher import quotes
from watcher import storage

logger = logging.getLogger(__name__)

_PARIS_TZ = ZoneInfo("Europe/Paris")
_GETUPDATES_TIMEOUT = 25  # secondes, long-polling côté Telegram
_HTTP_TIMEOUT = 30  # légèrement > _GETUPDATES_TIMEOUT, marge réseau


def parse_recap_command(update: dict, allowed_chat_id: str) -> bool:
    """
    True si `update` est un message texte provenant de `allowed_chat_id` et
    commençant par "/recap". Ignore silencieusement (False) tout le reste :
    updates sans "message" (edited_message, channel_post...), chat absent,
    chat_id différent, texte absent.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return False
    if str(chat.get("id")) != str(allowed_chat_id):
        return False
    text = message.get("text")
    if not isinstance(text, str):
        return False
    return text.strip().startswith("/recap")


def parse_sell_command(update: dict, allowed_chat_id: str) -> str | None:
    """
    Retourne l'argument de la commande /sell (ex: "SASS" ou l'adresse
    complète du token) si `update` est un message texte de allowed_chat_id
    commençant par "/sell", None si le message ne correspond pas du tout.
    Une chaîne vide signifie "/sell" envoyé sans argument — à l'appelant de
    répondre avec l'usage plutôt que de chercher une position vide.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    if str(chat.get("id")) != str(allowed_chat_id):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text != "/sell" and not text.startswith("/sell "):
        return None
    return text[len("/sell"):].strip()


def compute_next_offset(updates: list[dict], current_offset: int) -> int:
    """Prochain offset pour getUpdates : max(update_id)+1 parmi `updates`, ou
    `current_offset` inchangé si `updates` est vide ou sans update_id valide."""
    update_ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int)]
    if not update_ids:
        return current_offset
    return max(update_ids) + 1


def fetch_updates(offset: int | None) -> list[dict]:
    """Un appel getUpdates. Ne lève jamais : erreur réseau/JSON -> liste vide
    (le prochain tour de boucle réessaiera)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": _GETUPDATES_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = httpx.get(url, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        result = resp.json().get("result")
        return result if isinstance(result, list) else []
    except Exception as exc:
        logger.warning("getUpdates impossible : %s", exc)
        return []


def _midnight_paris_utc_iso() -> str:
    """Minuit (heure de Paris) du jour courant, converti en ISO UTC — borne de
    départ du bilan "aujourd'hui" du récap à la demande."""
    now_paris = datetime.now(_PARIS_TZ)
    midnight_paris = now_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_paris.astimezone(timezone.utc).isoformat()


def build_and_send_recap() -> None:
    """Rassemble l'état courant + le bilan du jour et envoie la réponse."""
    n_watched = storage.get_candidates_funnel()["watching"]
    active_positions = storage.get_active_positions()
    closed_today = storage.get_positions_closed_since(_midnight_paris_utc_iso())
    n_wins = sum(1 for p in closed_today if p["status"] == "closed_profit")
    n_losses = sum(1 for p in closed_today if p["status"] == "closed_loss")
    pnl_eur = sum(
        p["invested_eur"] * (p["exit_multiplier"] - 1)
        for p in closed_today if p["exit_multiplier"] is not None
    )
    portfolio = storage.get_portfolio_summary()
    message = notifier.build_ondemand_recap_message(
        n_watched, active_positions, n_wins, n_losses, pnl_eur, portfolio,
    )
    notifier.send_telegram(message)


def handle_sell_command(identifier: str) -> None:
    """
    Traite la commande /sell <symbole ou adresse> : clôture manuellement la
    position ouverte correspondante au prix actuel (cotation live), avec la
    même alerte que pour un Take Profit/Stop Loss automatique. Répond
    toujours par un message Telegram — usage, position introuvable,
    ambiguïté (plusieurs matches), ou confirmation de vente.
    """
    if not identifier:
        notifier.send_telegram(
            "Usage : `/sell <symbole ou adresse>`\n"
            "Envoie `/recap` pour voir la liste des positions ouvertes."
        )
        return

    matches = storage.find_open_positions_by_identifier(identifier)
    if not matches:
        notifier.send_telegram(f"Aucune position ouverte trouvée pour \"{identifier}\".")
        return

    if len(matches) > 1:
        lines = [f"Plusieurs positions correspondent à \"{identifier}\", précise l'adresse complète :"]
        for p in matches:
            display = p.get("token_symbol") or p.get("token_name") or p["token_address"][:10]
            lines.append(f"  • {display} : `{p['token_address']}`")
        notifier.send_telegram("\n".join(lines))
        return

    position = matches[0]
    pool_quotes = quotes.fetch_pool_quotes([position["pair_address"]])
    quote = pool_quotes.get(position["pair_address"].lower())
    if not quote or not quote.get("price"):
        notifier.send_telegram(
            f"Cotation indisponible pour \"{identifier}\" — réessaie dans quelques secondes."
        )
        return

    entry_price = float(position["entry_price_usd"])
    current_multiplier = float(quote["price"]) / entry_price if entry_price > 0 else 0.0
    is_profit = current_multiplier > 1.0

    storage.mark_position_closed(position["id"], current_multiplier, is_profit)
    notifier.alert_virtual_sell(
        position["token_address"], position.get("token_name"), position.get("token_symbol"),
        current_multiplier, position["invested_eur"], is_profit,
        position.get("source", "onchain"),
    )
    logger.info(
        "Position #%d vendue manuellement via /sell (%s) : x%.2f",
        position["id"], identifier, current_multiplier,
    )


async def _run_forever() -> None:
    """Boucle isolée : ignore le backlog de messages en attente au démarrage
    (évite de répondre à un /recap vieux de plusieurs jours), puis répond au
    fil de l'eau. Une erreur isolée (réseau ou envoi) n'arrête jamais la
    tâche ; l'offset avance après chaque batch, matché ou non, pour ne jamais
    reboucler indéfiniment sur le même message."""
    backlog = await asyncio.to_thread(fetch_updates, None)
    offset = compute_next_offset(backlog, 0)

    while True:
        try:
            updates = await asyncio.to_thread(fetch_updates, offset)
            for update in updates:
                if parse_recap_command(update, config.TELEGRAM_CHAT_ID):
                    try:
                        build_and_send_recap()
                    except Exception as exc:
                        logger.error("Échec de l'envoi du récap à la demande : %s", exc)
                sell_arg = parse_sell_command(update, config.TELEGRAM_CHAT_ID)
                if sell_arg is not None:
                    try:
                        await asyncio.to_thread(handle_sell_command, sell_arg)
                    except Exception as exc:
                        logger.error("Échec de la commande /sell : %s", exc)
            offset = compute_next_offset(updates, offset)
        except Exception as exc:
            logger.error("Erreur dans le listener de commandes : %s", exc)
            await asyncio.sleep(5)


def start() -> asyncio.Task:
    """Lance le listener de commandes comme tâche asyncio isolée."""
    return asyncio.create_task(_run_forever())
