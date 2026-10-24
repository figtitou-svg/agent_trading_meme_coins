"""
watcher/detectors/onchain_detector.py — Détecteur on-chain pump.fun.

Poll l'API publique de pump.fun (celle utilisée par leur propre frontend)
pour repérer les nouveaux tokens créés, et les pousse comme candidats
source="onchain". Remplace la logique PairCreated/Uniswap V2 de l'ancien
sniper EVM.

NOTE : endpoint et noms de champs (`mint`, `name`, `symbol`) vérifiés en
direct le 2026-07-26 contre frontend-api-v3.pump.fun (réponse HTTP 200 réelle
inspectée, sans authentification). L'API reste non documentée officiellement
par pump.fun eux-mêmes, donc sa forme peut encore changer sans préavis — si
elle devient trop instable, le repli prévu est une souscription RPC brute
(logsSubscribe) au programme pump.fun — cf. spec de design, section
"Détecteur on-chain".

Ne lève jamais d'exception vers l'appelant : une erreur réseau isolée, une
réponse JSON de forme inattendue (dict/str/None au lieu d'une liste,
éléments non-dict dans la liste), ou un item dict dont un champ imbriqué a
un type inattendu (ex. symbol=liste au lieu de string, qui ferait échouer
l'écriture en base) sont loggés et ignorés individuellement, sans faire
perdre les autres items du même lot — l'API pump.fun n'étant pas documentée
officiellement, sa forme peut changer sans préavis.
"""

import asyncio
import logging

import httpx

from watcher import config
from watcher import storage

logger = logging.getLogger(__name__)

# Taille de page raisonnable pour le polling périodique (pas de pagination
# multi-pages nécessaire : on ne cherche que les tokens les plus récents à
# chaque cycle, dédupliqués par storage.add_candidate).
_NEW_COINS_PAGE_SIZE = 50

PUMPFUN_NEW_COINS_ENDPOINT = (
    "{base}/coins?limit=" + str(_NEW_COINS_PAGE_SIZE) + "&offset=0"
    "&sort=created_timestamp&order=DESC"
)


def parse_new_coin_event(payload: dict) -> tuple[str, str | None, str | None] | None:
    """
    Extrait (mint_address, name, symbol) d'un événement brut de l'API
    pump.fun. Retourne None si le mint est absent, vide ou n'est pas une
    chaîne (événement inexploitable).
    """
    mint = payload.get("mint")
    if not isinstance(mint, str) or not mint:
        return None
    return mint, payload.get("name"), payload.get("symbol")


def handle_new_coin(payload: dict) -> None:
    """Traite un événement de création de token : ajoute un candidat si le
    parsing réussit, ne fait rien sinon."""
    parsed = parse_new_coin_event(payload)
    if parsed is None:
        return
    mint, name, symbol = parsed
    # pair_address inconnu tant que le token n'a pas migré vers un pool
    # PumpSwap (l'AMM de pump.fun) — on réutilise le mint, cohérent avec le
    # détecteur Telegram.
    created = storage.add_candidate(mint, mint, name, symbol, source="onchain")
    if created:
        logger.info("Candidat on-chain détecté : %s (%s)", mint, symbol or "symbole inconnu")


def poll_once() -> None:
    """Un cycle de polling : récupère les nouveaux tokens et les traite.
    Ne lève jamais d'exception, quelle que soit la forme de la réponse."""
    url = PUMPFUN_NEW_COINS_ENDPOINT.format(base=config.PUMPFUN_FRONTEND_API)
    try:
        resp = httpx.get(url, timeout=config.QUOTES_HTTP_TIMEOUT, headers={"accept": "application/json"})
        resp.raise_for_status()
        coins = resp.json()
        if not isinstance(coins, list):
            logger.warning("Réponse pump.fun inattendue (pas une liste) : %r", type(coins).__name__)
            return
        for payload in coins:
            if not isinstance(payload, dict):
                logger.warning("Élément pump.fun inattendu (pas un objet) ignoré : %r", payload)
                continue
            try:
                handle_new_coin(payload)
            except Exception as exc:
                # Isolation par item : un champ imbriqué du mauvais type
                # (ex. symbol=liste au lieu de string) ne doit pas faire
                # perdre les bons items restants du même lot.
                logger.warning("Événement pump.fun ignoré (%s) : %r", exc, payload)
    except Exception as exc:
        logger.warning("Polling pump.fun impossible : %s", exc)


async def _run_forever() -> None:
    """Boucle de polling isolée — un souci réseau prolongé ne doit jamais
    arrêter le détecteur Telegram."""
    while True:
        poll_once()
        await asyncio.sleep(config.ONCHAIN_POLL_INTERVAL_SECONDS)


def start() -> asyncio.Task:
    """Lance le détecteur on-chain comme tâche asyncio isolée."""
    return asyncio.create_task(_run_forever())
