"""
watcher/detectors/discord_detector.py — Détecteur Discord (bot).

Écoute les messages des salons configurés (DISCORD_CHANNEL_IDS) et en
extrait les adresses/tickers de tokens Solana via detectors.message_parsing
— même logique d'extraction que telegram_detector.py, partagée entre les
deux plateformes.

Différence structurelle avec Telegram : pas de "compte utilisateur" viable
sur Discord (la plupart des serveurs interdisent les self-bots, contraire
aux CGU Discord) — il faut un vrai bot, invité manuellement par un admin de
chaque serveur cible (droit "Gérer le serveur"), avec le "Message Content
Intent" activé côté portail développeur ET côté code (intents.message_content
= True) pour pouvoir lire le texte des messages, pas seulement les
métadonnées.

La partie réseau (discord.Client, reconnexion, résolution ticker) est
volontairement isolée dans start() / _run_forever() : elle n'est pas testée
unitairement ici (nécessite un vrai token bot), seule la logique de
traitement de message (pure, sans I/O) l'est — même découpage que
telegram_detector.py.
"""

import asyncio
import logging

from watcher import config
from watcher import quotes
from watcher import storage
from watcher.detectors.message_parsing import extract_ticker, extract_token_address

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
        source="discord", source_channel=channel_name,
        source_note=f"[ticker ${ticker}] {text[:190]}",
    )
    if created:
        logger.info(
            "Candidat Discord détecté via ticker $%s → %s (channel=%s)",
            ticker, resolved_address, channel_name,
        )


def handle_message(text: str, channel_name: str) -> None:
    """
    Traite un message reçu : si une adresse token est trouvée, l'ajoute comme
    candidat taggé source="discord". Ne fait rien sinon.

    pair_address n'est pas encore connu à ce stade (aucun pool associé tant
    que le détecteur on-chain ne l'a pas résolu) — on réutilise le mint comme
    valeur temporaire, cohérent avec les détecteurs telegram/onchain.
    """
    address = extract_token_address(text)
    if address is None:
        return
    created = storage.add_candidate(
        address, address,
        source="discord", source_channel=channel_name, source_note=text[:200],
    )
    if created:
        logger.info("Candidat Discord détecté : %s (channel=%s)", address, channel_name)


class _MemecoinDiscordClient:
    """
    Enveloppe autour de discord.Client — construite dynamiquement dans
    _run_forever() (import différé de `discord`, comme Telethon dans
    telegram_detector.py : évite la dépendance dure au démarrage du module
    si le paquet n'est pas installé/configuré).
    """

    def __new__(cls):
        import discord

        intents = discord.Intents.default()
        intents.message_content = True  # requis pour lire le texte des messages

        class _Client(discord.Client):
            async def on_ready(self_client) -> None:
                logger.info(
                    "Détecteur Discord connecté (%d salons)", len(config.DISCORD_CHANNEL_IDS),
                )

            async def on_message(self_client, message) -> None:
                if message.author.bot:
                    return  # ignore les autres bots (et soi-même) — évite les boucles
                if message.channel.id not in config.DISCORD_CHANNEL_IDS:
                    return
                channel_name = f"#{getattr(message.channel, 'name', message.channel.id)}"
                text = message.content or ""

                handle_message(text, channel_name)
                if extract_token_address(text) is not None:
                    return
                ticker = extract_ticker(text)
                if ticker is None:
                    return
                # Appel réseau déporté dans un thread : ne doit jamais bloquer
                # la boucle asyncio du client Discord.
                resolved = await asyncio.to_thread(quotes.resolve_ticker_mint, ticker)
                if resolved is not None:
                    handle_ticker_match(ticker, resolved, channel_name, text)

        return _Client(intents=intents)


async def _run_forever() -> None:
    """
    Boucle de connexion avec reconnexion/backoff — isolée du reste du
    pipeline : un crash ici ne doit jamais arrêter les autres détecteurs.
    discord.py gère déjà ses propres reconnexions transitoires en interne
    (client.start(reconnect=True)) ; cette boucle couvre le cas où
    client.start() lève quand même (ex. token invalide, erreur fatale).
    """
    backoff = 5
    while True:
        try:
            client = _MemecoinDiscordClient()
            backoff = 5  # connexion réussie : on réinitialise le backoff
            await client.start(config.DISCORD_BOT_TOKEN)
        except Exception as exc:
            logger.error("Détecteur Discord déconnecté (%s) — reconnexion dans %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


def start() -> asyncio.Task:
    """Lance le détecteur Discord comme tâche asyncio isolée."""
    return asyncio.create_task(_run_forever())
