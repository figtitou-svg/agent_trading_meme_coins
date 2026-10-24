"""
watcher/quotes.py — Cotations Solana en deux phases.

Un token pump.fun ne vit pas dans un pool AMM classique dès sa création : il
vit sur une courbe de prix interne (bonding curve) jusqu'à ~69k$ de market
cap, puis migre vers un pool PumpSwap (l'AMM propre à pump.fun — ce n'est PAS
Raydium, contrairement à ce que suggérait la spec de design initiale).
Avant la migration, DexScreener n'a souvent aucune cotation fiable — c'est
pour ça qu'on interroge deux endpoints différents selon la phase du
candidat :

  - fetch_curve_progress() : progression de la courbe ET adresse du pool
                             (déjà fournie par ce même endpoint pump.fun,
                             pré- comme post-migration — voir NOTE
                             ci-dessous), via l'API pump.fun.
  - fetch_pool_quotes()    : prix/liquidité/volume post-migration, via
                             l'endpoint /pairs/ de DexScreener (chain
                             "solana") — celui-ci attend des adresses de
                             POOL, jamais de mint.

NOTE (vérifié en direct le 2026-07-26 contre frontend-api-v3.pump.fun) :
la réponse de /coins/{mint} contient déjà `pool_address`, aussi bien avant
qu'après migration (pré-migration c'est le pool interne à la bonding curve,
post-migration c'est le vrai pool PumpSwap) — DexScreener indexe les deux
sous /pairs/solana/{pool_address} de la même façon. Il n'y a donc PAS besoin
d'une résolution séparée via l'endpoint /tokens/ de DexScreener (l'ancienne
fonction resolve_pool_address a été supprimée pour cette raison) : le pool
se lit directement dans le résultat de fetch_curve_progress().

Aucune de ces fonctions ne lève jamais d'exception : un souci réseau
retourne un dict vide (ou None) plutôt que de faire tomber le watcher.
"""

import logging

import httpx

from watcher import config

logger = logging.getLogger(__name__)

DEXSCREENER_SOLANA_PAIRS_API = "https://api.dexscreener.com/latest/dex/pairs/solana/{pairs}"
DEXSCREENER_SEARCH_API = "https://api.dexscreener.com/latest/dex/search"


def fetch_curve_progress(mint_addresses: list[str]) -> dict:
    """
    Interroge l'API pump.fun pour la progression de bonding curve de chaque
    mint. Retourne
    {mint: {"market_cap_usd": float, "migrated": bool, "pool_address": str | None}}.

    NOTE : endpoint et noms de champs (`usd_market_cap`, `complete`,
    `pool_address`) vérifiés en direct le 2026-07-26 contre
    frontend-api-v3.pump.fun. pool_address est lu directement depuis cette
    même réponse — aucun appel de résolution séparé n'est nécessaire (voir
    docstring de module).
    """
    if not mint_addresses:
        return {}
    result: dict[str, dict] = {}
    for mint in mint_addresses:
        try:
            resp = httpx.get(
                f"{config.PUMPFUN_FRONTEND_API}/coins/{mint}",
                timeout=config.QUOTES_HTTP_TIMEOUT,
                headers={"accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                logger.warning("Réponse pump.fun inattendue pour %s : pas un objet JSON", mint)
                continue
            market_cap = data.get("usd_market_cap")
            if market_cap is not None:
                result[mint] = {
                    "market_cap_usd": float(market_cap),
                    "migrated": bool(data.get("complete", False)),
                    "pool_address": data.get("pool_address"),
                }
        except Exception as exc:
            logger.warning("Progression pump.fun impossible pour %s : %s", mint, exc)
    return result


def _pick_best_ticker_match(pairs: list[dict], ticker: str) -> str | None:
    """
    Parmi des résultats de recherche DexScreener, retient l'adresse solana
    dont le symbole correspond exactement au ticker (insensible à la casse)
    avec la plus grosse liquidité — le plus probable candidat "réel" quand
    plusieurs tokens partagent le même symbole. None si aucun ne matche.
    """
    matches = [
        pair for pair in pairs
        if pair.get("chainId") == "solana"
        and (pair.get("baseToken") or {}).get("symbol", "").upper() == ticker.upper()
    ]
    if not matches:
        return None
    best = max(matches, key=lambda pair: (pair.get("liquidity") or {}).get("usd") or 0)
    return (best.get("baseToken") or {}).get("address")


def resolve_ticker_mint(ticker: str) -> str | None:
    """
    Résout un ticker mentionné sans lien ni adresse (ex: "$BONK 🚀") en
    adresse de mint Solana, via la recherche DexScreener. N'importe qui peut
    créer un token avec un symbole donné : ce n'est pas une identification
    fiable à 100%, voir _pick_best_ticker_match pour l'heuristique de
    désambiguïsation. Ne lève jamais d'exception : None si rien ne matche,
    réponse malformée, ou échec réseau.
    """
    if not ticker:
        return None
    try:
        resp = httpx.get(
            DEXSCREENER_SEARCH_API,
            params={"q": ticker},
            timeout=config.QUOTES_HTTP_TIMEOUT,
            headers={"accept": "application/json"},
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs")
        if not isinstance(pairs, list):
            return None
        return _pick_best_ticker_match(pairs, ticker)
    except Exception as exc:
        logger.warning("Résolution du ticker $%s impossible : %s", ticker, exc)
        return None


def fetch_pool_quotes(pair_addresses: list[str]) -> dict:
    """
    Interroge DexScreener (chain Solana) pour une liste de pools PumpSwap
    (max 30, limite de l'API) et retourne
    {pool_address_en_minuscules: {"price", "liquidity", "volume_h1"}}.
    Ne lève jamais d'exception : dict vide en cas d'échec réseau.
    """
    if not pair_addresses:
        return {}
    try:
        resp = httpx.get(
            DEXSCREENER_SOLANA_PAIRS_API.format(pairs=",".join(pair_addresses[:30])),
            timeout=config.QUOTES_HTTP_TIMEOUT,
            headers={"accept": "application/json"},
        )
        resp.raise_for_status()
        quoted = {}
        for pair in resp.json().get("pairs") or []:
            addr = (pair.get("pairAddress") or "").lower()
            if addr:
                quoted[addr] = {
                    "price": pair.get("priceUsd"),
                    "liquidity": (pair.get("liquidity") or {}).get("usd"),
                    "volume_h1": (pair.get("volume") or {}).get("h1"),
                }
        return quoted
    except Exception as exc:
        logger.warning("Cotation DexScreener (Solana) impossible : %s", exc)
        return {}
