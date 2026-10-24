"""
watcher/detectors/message_parsing.py — Extraction d'adresses/tickers de tokens
Solana depuis un texte de message brut, partagée entre les détecteurs qui
lisent des messages humains (Telegram, Discord...).

Priorité aux liens reconnus (pump.fun, DexScreener, Birdeye) — plus fiables
qu'une regex base58 brute qui peut matcher n'importe quelle chaîne de
caractères ressemblant à une adresse. $TICKER en repli quand aucune adresse
n'est trouvée — reste une extraction pure/sans I/O : la résolution effective
en adresse se fait séparément via quotes.resolve_ticker_mint (appel réseau),
à la charge de l'appelant.
"""

import re

_BASE58_ADDRESS = r"[1-9A-HJ-NP-Za-km-z]{32,44}"

_LINK_PATTERNS = [
    re.compile(rf"pump\.fun/coin/({_BASE58_ADDRESS})"),
    re.compile(rf"dexscreener\.com/solana/({_BASE58_ADDRESS})"),
    re.compile(rf"birdeye\.so/token/({_BASE58_ADDRESS})"),
]
_RAW_ADDRESS = re.compile(rf"\b({_BASE58_ADDRESS})\b")
# Symbole après un $ : commence par une lettre (pas un chiffre) pour ne pas
# matcher des montants en dollars ("$0.05", "$50k mcap").
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")


def extract_token_address(text: str) -> str | None:
    """
    Cherche une adresse de token Solana dans un message : priorité aux liens
    reconnus (moins de faux positifs), repli sur une regex base58 brute.
    Retourne None si rien n'est trouvé.
    """
    if not text:
        return None
    for pattern in _LINK_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    match = _RAW_ADDRESS.search(text)
    return match.group(1) if match else None


def extract_ticker(text: str) -> str | None:
    """
    Repli quand extract_token_address ne trouve rien : cherche un symbole
    ($XYZ). Reste pure/sans I/O — la résolution effective en adresse se
    fait via quotes.resolve_ticker_mint, appelée séparément par l'appelant
    (nécessite un appel réseau).
    """
    if not text:
        return None
    match = _TICKER.search(text)
    return match.group(1) if match else None
