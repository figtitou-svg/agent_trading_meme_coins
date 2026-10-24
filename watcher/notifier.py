"""
watcher/notifier.py — Alertes Telegram du watcher memecoins.

Watcher 100% observation (WATCH_ONLY) : pas d'alerte pour chaque candidat
détecté/audité (des dizaines sont traités chaque jour, un audit RugCheck OK
ou raté n'a rien d'exceptionnel — ça resterait juste du bruit). En revanche,
chaque ACHAT et VENTE virtuel (un événement bien plus rare, un par position
réellement ouverte) déclenche sa propre alerte (alert_virtual_buy /
alert_virtual_sell), en plus du récap périodique condensé (alert_recap,
07h30/13h/18h/22h) — demande explicite de l'utilisateur le 30/07/2026.
Contrôlables séparément via trade_alerts_enabled()/recap_enabled() (réglage
partagé, écrit par le dashboard trading_view). `alert_real_purchase` est
conservée pour une éventuelle phase d'exécution réelle future
(WATCH_ONLY=false) mais n'est appelée par aucun code actif.
"""

import json
import logging

import requests

from watcher import config

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    """Envoie un message Telegram (Markdown). Ne lève jamais d'exception."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Alerte Telegram envoyée")
        return True
    except Exception as exc:
        logger.error("Échec alerte Telegram : %s", exc)
        return False


def alert_real_purchase(
    token_address: str,
    name: str | None,
    symbol: str | None,
    pair_address: str | None,
) -> None:
    """
    Alerte pour un achat RÉEL (WATCH_ONLY=false) — argent réellement engagé.

    N'est appelée par aucun code actif dans cette phase 100% observation :
    conservée pour une éventuelle phase d'exécution réelle Solana future.
    """
    display = symbol or token_address[:10]
    full_name = f" ({name})" if name and name != symbol else ""
    link = (
        f"https://dexscreener.com/solana/{pair_address}"
        if pair_address
        else f"https://solscan.io/token/{token_address}"
    )
    send_telegram(
        f"💰 *MEMECOIN ACHETÉ (ordre réel)*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{display}*{full_name}\n"
        f"⛓ Réseau : Solana\n"
        f"📈 [Voir le cours en direct]({link})\n"
        f"`{token_address}`"
    )


def alert_virtual_buy(
    token_address: str,
    name: str | None,
    symbol: str | None,
    pair_address: str,
    invested_eur: float,
    source: str,
) -> None:
    """Alerte à chaque achat virtuel (position simulée, aucun argent réel) —
    un message par trade, en plus du récap périodique condensé."""
    if not trade_alerts_enabled():
        return
    display = symbol or name or token_address[:10]
    full_name = f" ({name})" if name and name != symbol else ""
    source_label = {"telegram": "salon Telegram", "discord": "salon Discord", "onchain": "pump.fun (nouveau)", "momentum": "momentum"}.get(source, source)
    link = f"https://dexscreener.com/solana/{pair_address}"
    send_telegram(
        f"🟢 *Achat virtuel*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{display}*{full_name}\n"
        f"📡 Source : {source_label}\n"
        f"💵 Montant : {invested_eur:.2f}€ (virtuel)\n"
        f"📈 [Voir le cours]({link})\n"
        f"`{token_address}`"
    )


def alert_virtual_sell(
    token_address: str,
    name: str | None,
    symbol: str | None,
    exit_multiplier: float,
    invested_eur: float,
    is_profit: bool,
    source: str,
) -> None:
    """Alerte à chaque clôture de position virtuelle (Take Profit ou Stop
    Loss) — un message par trade, en plus du récap périodique condensé."""
    if not trade_alerts_enabled():
        return
    display = symbol or name or token_address[:10]
    full_name = f" ({name})" if name and name != symbol else ""
    pnl_eur = invested_eur * (exit_multiplier - 1)
    emoji = "✅" if is_profit else "❌"
    action = "Take Profit" if is_profit else "Stop Loss"
    source_label = {"telegram": "salon Telegram", "discord": "salon Discord", "onchain": "pump.fun (nouveau)", "momentum": "momentum"}.get(source, source)
    send_telegram(
        f"{emoji} *Vente virtuelle ({action})*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{display}*{full_name}\n"
        f"📡 Source : {source_label}\n"
        f"📊 Sortie : x{exit_multiplier:.2f} · P&L {pnl_eur:+.2f}€\n"
        f"`{token_address}`"
    )


def compute_stop_multiplier(peak: float) -> float:
    """
    Seuil de sortie (stop suiveur) pour un multiplicateur peak donné — même
    barème que la logique de clôture de main.refresh_positions() (qui
    importe cette fonction plutôt que de dupliquer les seuils). Vit ici
    (plutôt que dans main.py) pour que command_listener.py puisse aussi
    l'utiliser côté affichage (/recap) sans import circulaire avec main.py.

    Au-delà de x5, le stop suit désormais le peak proportionnellement
    (tolérance de repli de 12,5%, ex: peak x16 -> stop x14) au lieu d'un
    plafond fixe à x3,5 — décision utilisateur du 01/08/2026 : un plafond
    fixe laissait un gagnant à x16 redonner jusqu'à 78% de son pic avant de
    vendre. Plancher (peak < 2) relevé à x0,75 le 02/08/2026 (était x0,5) —
    décision utilisateur : limiter la perte max à -25% plutôt que -50% sur
    une position qui n'a jamais vraiment décollé.
    """
    if peak >= 5.0:
        return peak * 0.875
    if peak >= 3.0:
        return 2.0
    if peak >= 2.0:
        return 1.2
    return 0.75


def _format_portfolio_line(portfolio: dict) -> str:
    """Ligne 'patrimoine investi vs. réel' partagée entre les deux formats de
    récap. `portfolio` vient de storage.get_portfolio_summary() —
    {"invested_eur", "current_value_eur"}. Le pourcentage n'est affiché que
    si un montant a réellement été investi (évite une division par zéro
    avant le tout premier achat)."""
    invested = portfolio.get("invested_eur", 0) or 0
    current = portfolio.get("current_value_eur", 0) or 0
    if invested > 0:
        pct = (current / invested - 1) * 100
        return f"💼 Patrimoine : {invested:.2f}€ investis → {current:.2f}€ actuellement ({pct:+.1f}%)"
    return f"💼 Patrimoine : {invested:.2f}€ investis → {current:.2f}€ actuellement"


def build_recap_message(n_watched: int, n_bought: int, n_wins: int, n_losses: int,
                        pnl_eur: float, by_source: dict | None = None,
                        portfolio: dict | None = None) -> str:
    """Recap condensé (mêmes horaires que le digest forex) : pas d'alerte par
    token, juste un état des lieux depuis le dernier recap. Le découpage par
    source (telegram/onchain) est optionnel — absent, seul le total s'affiche.
    Une source inconnue dans `by_source` (ni 'telegram' ni 'onchain') est
    ignorée : `.get(..., {})` retombe alors sur un dict vide plutôt que de
    lever ou d'afficher une ligne inattendue. `portfolio` (optionnel) ajoute
    la ligne patrimoine investi/réel — absent, la ligne est omise."""
    pnl_str = f"{pnl_eur:+.2f}€"
    lines = [
        "📋 *Recap memecoins*",
        f"🔍 {n_watched} sous surveillance · 💰 {n_bought} achetés (virtuel)",
        f"✅ {n_wins} gain(s) · ❌ {n_losses} perte(s) · P&L {pnl_str}",
    ]
    if portfolio:
        lines.append(_format_portfolio_line(portfolio))
    if by_source:
        tg, oc = by_source.get("telegram", {}), by_source.get("onchain", {})
        lines.append(
            f"↳ Telegram : {tg.get('watched', 0)} surveillés, {tg.get('bought', 0)} achetés"
        )
        lines.append(
            f"↳ On-chain : {oc.get('watched', 0)} surveillés, {oc.get('bought', 0)} achetés"
        )
    return "\n".join(lines)


def alert_recap(n_watched: int, n_bought: int, n_wins: int, n_losses: int, pnl_eur: float,
                 by_source: dict | None = None, portfolio: dict | None = None) -> None:
    send_telegram(build_recap_message(n_watched, n_bought, n_wins, n_losses, pnl_eur, by_source, portfolio))


def build_ondemand_recap_message(n_watched: int, active_positions: list[dict],
                                  n_wins: int, n_losses: int, pnl_eur: float,
                                  portfolio: dict | None = None) -> str:
    """Récap répondu à la commande /recap : état courant détaillé (contrairement
    au recap périodique condensé), positions ouvertes listées nommément, bilan
    limité à aujourd'hui (depuis minuit heure de Paris). `portfolio`
    (optionnel) ajoute la ligne patrimoine investi/réel."""
    pnl_str = f"{pnl_eur:+.2f}€"
    lines = [
        "📋 *Récap à la demande*",
        f"🔍 {n_watched} candidat(s) en surveillance",
    ]
    if portfolio:
        lines.append(_format_portfolio_line(portfolio))
    if active_positions:
        lines.append(f"💰 {len(active_positions)} position(s) virtuelle(s) ouverte(s) :")
        for pos in active_positions:
            display = (
                pos.get("token_symbol")
                or pos.get("token_name")
                or pos["token_address"][:10]
            )
            peak = pos.get("peak_multiplier") or 1.0
            current = pos.get("current_multiplier")
            if current is None:
                # Position 'pending' : prix d'entrée pas encore connu, donc
                # pas encore de valeur actuelle ni de stop calculable.
                lines.append(f"  • {display} : x{peak:.2f}")
            else:
                stop = compute_stop_multiplier(peak)
                lines.append(
                    f"  • {display} : pic x{peak:.2f} → actuel x{current:.2f} (stop x{stop:.2f})"
                )
    else:
        lines.append("💰 Aucune position virtuelle ouverte")
    lines.append(
        f"📅 Aujourd'hui : ✅ {n_wins} gain(s) · ❌ {n_losses} perte(s) · P&L {pnl_str}"
    )
    return "\n".join(lines)


def _notification_setting(key: str) -> bool:
    """Lit un réglage booléen du fichier partagé (écrit par le dashboard
    trading_view) ; True par défaut si le fichier est absent, illisible,
    invalide, ou si la clé en est absente — comportement fail-open tant que
    personne n'a explicitement désactivé le réglage."""
    try:
        with open(config.NOTIFICATION_SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Réglages de notification illisibles (%s) : défaut appliqué", exc)
        return True
    if not isinstance(settings, dict):
        return True
    return bool(settings.get(key, True))


def recap_enabled() -> bool:
    """Récap périodique (07h30/13h/18h/22h + /recap à la demande)."""
    return _notification_setting("memecoin_recap_enabled")


def trade_alerts_enabled() -> bool:
    """Alerte à chaque achat/vente virtuel (un message par trade)."""
    return _notification_setting("memecoin_trade_alerts_enabled")
