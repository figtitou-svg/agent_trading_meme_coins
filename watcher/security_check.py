"""
watcher/security_check.py — Audit anti-scam d'un token Solana via l'API RugCheck.xyz.

Politique FAIL-CLOSED : si l'API est injoignable, timeout, ou ne connaît pas
encore le token, l'achat (virtuel) est BLOQUÉ. On ne trade jamais à l'aveugle.

Critères de blocage :
  - Autorité de mint encore active   -> le créateur peut diluer le supply à volonté
  - Autorité de freeze encore active -> le créateur peut geler un wallet (honeypot)
  - Concentration des gros holders   -> > MAX_TOP_HOLDER_PERCENT hors pool = risque de dump

Remplace l'ancien audit GoPlus (EVM uniquement, ne couvre pas Solana). Les
critères sans équivalent Solana (taxe buy/sell, contrat proxy) n'ont pas de
remplaçant : les tokens SPL standards n'ont pas ces mécanismes.
"""

import logging
import time

import requests

from watcher import config

logger = logging.getLogger(__name__)

RUGCHECK_REPORT_URL = config.RUGCHECK_API_BASE + "/tokens/{token_address}/report"

# Session réutilisée : le keep-alive HTTP économise le handshake TCP/TLS
# à chaque appel — précieux quand chaque seconde compte.
_session = requests.Session()
_session.headers.update({"accept": "application/json"})


class SecurityCheckError(RuntimeError):
    """Impossible d'obtenir un rapport de sécurité exploitable."""


def _fetch_report(token_address: str) -> dict:
    """
    Interroge RugCheck et retourne le rapport brut du token.

    Un token fraîchement créé n'est parfois pas encore indexé : on retente
    RUGCHECK_RETRIES fois avant d'abandonner. Chaque appel est borné par
    RUGCHECK_TIMEOUT pour ne jamais geler le watcher.
    """
    url = RUGCHECK_REPORT_URL.format(token_address=token_address)
    last_error = "raison inconnue"

    for attempt in range(1, config.RUGCHECK_RETRIES + 1):
        try:
            response = _session.get(url, timeout=config.RUGCHECK_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.Timeout:
            last_error = f"timeout après {config.RUGCHECK_TIMEOUT}s"
        except requests.RequestException as exc:
            last_error = f"erreur réseau : {exc}"
        except ValueError as exc:  # JSON invalide
            last_error = f"réponse JSON invalide : {exc}"

        logger.warning(
            "RugCheck tentative %d/%d échouée (%s)",
            attempt, config.RUGCHECK_RETRIES, last_error,
        )
        if attempt < config.RUGCHECK_RETRIES:
            time.sleep(config.RUGCHECK_RETRY_DELAY)

    raise SecurityCheckError(
        f"Audit RugCheck impossible après {config.RUGCHECK_RETRIES} tentatives : {last_error}"
    )


def audit_token(token_address: str, max_top_holder_percent: float | None = None) -> tuple[bool, list[str]]:
    """
    Audite un token Solana et retourne (est_sûr, raisons_de_blocage).

    (True, [])          -> tous les feux sont verts, le candidat peut avancer.
    (False, [raisons])  -> bloqué ; les raisons sont humainement lisibles.

    Ne lève jamais d'exception : toute erreur se traduit par un blocage (fail-closed).

    `max_top_holder_percent` permet à l'appelant d'utiliser un seuil de
    concentration différent de config.MAX_TOP_HOLDER_PERCENT (par défaut si
    omis/None) — voir main.process_new_candidates(), qui applique un seuil
    plus permissif pour les candidats source="telegram".

    NOTE : les noms de champs JSON (`token.mintAuthority`, `token.freezeAuthority`,
    `topHolders[].pct`) suivent le format documenté de RugCheck.xyz au moment de
    l'écriture — à vérifier contre une vraie réponse d'API avant la mise en
    observation réelle, un changement de schéma tiers n'étant jamais garanti.
    """
    limit = max_top_holder_percent if max_top_holder_percent is not None else config.MAX_TOP_HOLDER_PERCENT
    try:
        report = _fetch_report(token_address)
    except SecurityCheckError as exc:
        return False, [str(exc)]

    # Le schéma JSON de RugCheck n'est pas garanti (voir NOTE ci-dessus) : un
    # champ manquant, renommé ou d'un type inattendu ne doit jamais faire
    # planter l'audit — il doit bloquer comme n'importe quelle autre panne.
    try:
        reasons: list[str] = []
        token_info = report.get("token")

        # token absent/null = on ignore l'état réel des autorités mint/freeze :
        # fail-closed, comme pour topHolders manquant ci-dessous (on ne trade
        # jamais à l'aveugle).
        if not isinstance(token_info, dict):
            reasons.append("État des autorités mint/freeze inconnu — bloqué par précaution")
            token_info = {}

        # 1. Autorité de mint : le créateur peut émettre du supply supplémentaire.
        if token_info.get("mintAuthority"):
            reasons.append("Autorité de mint encore active — supply pas figé")

        # 2. Autorité de freeze : le créateur peut geler le wallet de l'acheteur
        #    (empêche la revente, équivalent honeypot).
        if token_info.get("freezeAuthority"):
            reasons.append("Autorité de freeze encore active — risque honeypot")

        # 3. Concentration des gros holders hors pool.
        top_holders = report.get("topHolders") or []
        if top_holders:
            top_pct = top_holders[0].get("pct")
            if top_pct is None:
                reasons.append("Concentration du plus gros holder inconnue — bloqué par précaution")
            elif top_pct > limit:
                reasons.append(
                    f"Plus gros holder à {top_pct:.1f}% > {limit}%"
                )
        else:
            reasons.append("Répartition des holders inconnue — bloqué par précaution")
    except (AttributeError, TypeError, KeyError, IndexError) as exc:
        logger.warning("Rapport RugCheck dans un format inattendu (%s)", exc)
        return False, ["Rapport RugCheck dans un format inattendu — bloqué par précaution"]

    return (not reasons, reasons)
