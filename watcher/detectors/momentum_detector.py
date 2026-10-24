"""
watcher/detectors/momentum_detector.py — Détecteur momentum (hausse rapide prix+volume).

Poll l'API publique GeckoTerminal (gratuite, sans clé — vérifiée en direct le
30/07/2026 contre api.geckoterminal.com/api/v2/networks/solana/trending_pools)
pour repérer, sur l'ENSEMBLE du marché Solana (pas seulement pump.fun, ni les
salons Telegram surveillés), les pools dont le prix et le volume accélèrent
brutalement sur une fenêtre de 5 minutes. Contrairement aux deux autres
détecteurs (nouveau token pump.fun / mention dans un salon), celui-ci repère
des tokens déjà en mouvement, indépendamment de leur origine ou de leur âge.

Le pool retourné par GeckoTerminal est déjà un pool réel et coté (le tri
"trending" ne porte que sur des pools avec de l'activité) : contrairement aux
candidats "onchain" (bonding curve pump.fun, pool encore inconnu tant que la
migration n'est pas confirmée), un candidat "momentum" connaît déjà son
pair_address dès la détection — voir process_new_candidates() dans main.py,
qui le fait passer directement en 'pending_quote' après l'audit, sans phase
'on_curve'.

Ne lève jamais d'exception vers l'appelant : réponse réseau/JSON de forme
inattendue loggée et ignorée, item par item si besoin, comme les deux autres
détecteurs.
"""

import asyncio
import logging

import httpx

from watcher import config
from watcher import storage

logger = logging.getLogger(__name__)

TRENDING_POOLS_ENDPOINT = "{base}/networks/solana/trending_pools?duration=5m"


def parse_trending_pool(payload: dict) -> tuple[str, str, str | None, float, float] | None:
    """
    Extrait (mint_address, pool_address, token_name, price_change_pct_5m,
    volume_usd_5m) d'un pool brut de la réponse GeckoTerminal. Retourne None
    si un champ requis est absent ou d'un type inexploitable — n'empêche pas
    de traiter les autres pools du même lot.
    """
    try:
        attributes = payload.get("attributes") or {}
        pool_address = attributes.get("address")
        if not isinstance(pool_address, str) or not pool_address:
            return None

        base_token_id = (
            ((payload.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        ).get("id")
        if not isinstance(base_token_id, str) or not base_token_id.startswith("solana_"):
            return None
        mint = base_token_id.removeprefix("solana_")
        if not mint:
            return None

        price_change_pct = float((attributes.get("price_change_percentage") or {}).get("m5"))
        volume_usd = float((attributes.get("volume_usd") or {}).get("m5"))

        name = attributes.get("name")
        token_name = name.split(" / ")[0].strip() if isinstance(name, str) and " / " in name else name

        return mint, pool_address, token_name, price_change_pct, volume_usd
    except (TypeError, ValueError):
        return None


def handle_trending_pool(payload: dict) -> None:
    """
    Traite un pool trending : ajoute un candidat source="momentum" seulement
    si la hausse de prix ET le volume sur 5 minutes dépassent les seuils
    configurés (MOMENTUM_MIN_PRICE_CHANGE_PCT, MOMENTUM_MIN_VOLUME_USD) — la
    réponse GeckoTerminal inclut aussi des pools stables ou en baisse (le tri
    "trending" mélange plusieurs signaux, pas seulement la hausse de prix).
    """
    parsed = parse_trending_pool(payload)
    if parsed is None:
        return
    mint, pool_address, token_name, price_change_pct, volume_usd = parsed
    if price_change_pct < config.MOMENTUM_MIN_PRICE_CHANGE_PCT:
        return
    if volume_usd < config.MOMENTUM_MIN_VOLUME_USD:
        return
    created = storage.add_candidate(mint, pool_address, token_name, None, source="momentum")
    if created:
        logger.info(
            "Candidat momentum détecté : %s (%s) — prix +%.1f%% / volume %.0f$ (5min)",
            mint, token_name or "nom inconnu", price_change_pct, volume_usd,
        )


def poll_once() -> None:
    """Un cycle de polling : récupère les pools trending et les traite.
    Ne lève jamais d'exception, quelle que soit la forme de la réponse."""
    url = TRENDING_POOLS_ENDPOINT.format(base=config.GECKOTERMINAL_API_BASE)
    try:
        resp = httpx.get(url, timeout=config.QUOTES_HTTP_TIMEOUT, headers={"accept": "application/json"})
        resp.raise_for_status()
        pools = (resp.json() or {}).get("data")
        if not isinstance(pools, list):
            logger.warning("Réponse GeckoTerminal inattendue (pas de liste 'data') : %r", type(pools).__name__)
            return
        for payload in pools:
            if not isinstance(payload, dict):
                logger.warning("Élément GeckoTerminal inattendu (pas un objet) ignoré : %r", payload)
                continue
            try:
                handle_trending_pool(payload)
            except Exception as exc:
                logger.warning("Pool GeckoTerminal ignoré (%s) : %r", exc, payload)
    except Exception as exc:
        logger.warning("Polling GeckoTerminal impossible : %s", exc)


async def _run_forever() -> None:
    """Boucle de polling isolée — un souci réseau prolongé ne doit jamais
    arrêter les autres détecteurs."""
    while True:
        poll_once()
        await asyncio.sleep(config.MOMENTUM_POLL_INTERVAL_SECONDS)


def start() -> asyncio.Task:
    """Lance le détecteur momentum comme tâche asyncio isolée."""
    return asyncio.create_task(_run_forever())
