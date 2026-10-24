"""
watcher/detectors/telegram_detector.py — Détecteur Telegram (compte utilisateur).

Écoute les messages des channels configurés (TELEGRAM_CHANNELS) et en extrait
les adresses de tokens Solana mentionnées via detectors.message_parsing
(extract_token_address/extract_ticker, partagé avec discord_detector.py —
même logique d'extraction, peu importe la plateforme qui porte le message).

Certains messages ne donnent qu'un ticker ($XYZ) sans lien ni adresse — repli
géré séparément (extract_ticker + quotes.resolve_ticker_mint) car ça demande
un appel réseau, contrairement à extract_token_address qui reste pure.

La partie réseau (TelegramClient, reconnexion, résolution ticker) est
volontairement isolée dans start() / _run_forever() : elle n'est pas testée
unitairement ici (nécessite une vraie session Telegram ou un vrai réseau),
seule la logique de traitement de message (pure, sans I/O) l'est.
"""

import asyncio
import logging

from watcher import storage
from watcher import config
from watcher import quotes
from watcher.detectors.message_parsing import extract_token_address, extract_ticker

logger = logging.getLogger(__name__)


def handle_ticker_match(ticker: str, resolved_address: str, channel_name: str, text: str) -> None:
    """
    Ajoute un candidat déjà résolu depuis un ticker ($XYZ) plutôt qu'une
    adresse explicite dans le message. resolved_address est connue en amont
    (résolution réseau faite par l'appelant, voir quotes.resolve_ticker_mint)
    : cette fonction reste pure/sans I/O, comme handle_message.
    """
    created = storage.add_candidate(
        resolved_address, resolved_address,
        source="telegram", source_channel=channel_name,
        source_note=f"[ticker ${ticker}] {text[:190]}",
    )
    if created:
        logger.info(
            "Candidat Telegram détecté via ticker $%s → %s (channel=%s)",
            ticker, resolved_address, channel_name,
        )


def handle_message(text: str, channel_name: str) -> None:
    """
    Traite un message reçu : si une adresse token est trouvée, l'ajoute comme
    candidat taggé source="telegram". Ne fait rien sinon.

    pair_address n'est pas encore connu à ce stade (aucun pool associé tant
    que le détecteur on-chain ne l'a pas résolu) — on réutilise le mint comme
    valeur temporaire, cohérent avec le fait que candidates.pair_address est
    NOT NULL.
    """
    address = extract_token_address(text)
    if address is None:
        return
    created = storage.add_candidate(
        address, address,
        source="telegram", source_channel=channel_name, source_note=text[:200],
    )
    if created:
        logger.info("Candidat Telegram détecté : %s (channel=%s)", address, channel_name)


async def _run_forever() -> None:
    """
    Boucle de connexion Telethon avec reconnexion/backoff — isolée du reste
    du pipeline : un crash ici ne doit jamais arrêter le détecteur on-chain.
    """
    from telethon import TelegramClient, events

    backoff = 5
    while True:
        try:
            client = TelegramClient(
                config.TELEGRAM_SESSION_NAME,
                int(config.TELEGRAM_API_ID),
                config.TELEGRAM_API_HASH,
            )

            @client.on(events.NewMessage(chats=config.TELEGRAM_CHANNELS or None))
            async def _on_message(event):
                channel_name = getattr(event.chat, "username", None) or str(event.chat_id)
                text = event.raw_text or ""
                handle_message(text, channel_name)
                if extract_token_address(text) is not None:
                    return
                ticker = extract_ticker(text)
                if ticker is None:
                    return
                # Appel réseau déporté dans un thread : ne doit jamais bloquer
                # la boucle asyncio de Telethon (sinon on rate des messages
                # sur les 3 channels pendant la requête DexScreener).
                resolved = await asyncio.to_thread(quotes.resolve_ticker_mint, ticker)
                if resolved is not None:
                    handle_ticker_match(ticker, resolved, channel_name, text)

            async with client:
                logger.info("Détecteur Telegram connecté (%d channels)", len(config.TELEGRAM_CHANNELS))
                backoff = 5  # connexion réussie : on réinitialise le backoff
                await client.run_until_disconnected()
        except Exception as exc:
            logger.error("Détecteur Telegram déconnecté (%s) — reconnexion dans %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


def start() -> asyncio.Task:
    """Lance le détecteur Telegram comme tâche asyncio isolée."""
    return asyncio.create_task(_run_forever())
