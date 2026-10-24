"""
watcher/config.py — Chargement et validation centralisés de la configuration.

Toutes les valeurs sensibles (tokens API, identifiants Telegram) vivent dans
le fichier `.env`, exclu de Git. `_require()` permet d'échouer immédiatement
(fail-fast) au démarrage si une variable obligatoire venait à manquer — mieux
vaut un crash explicite au boot qu'une erreur cryptique en pleine détection.

Watcher 100% Solana/pump.fun, observation uniquement (WATCH_ONLY) : plus
aucune variable EVM (RPC, clé privée, contrats, gas) n'est nécessaire ici —
voir _archive_evm/execution.py pour l'ancien sniper Uniswap V2.
"""

import os

from dotenv import load_dotenv

# Charge le fichier .env situé à la racine du projet dans os.environ.
load_dotenv()


class ConfigError(RuntimeError):
    """Configuration manquante ou invalide."""


def _require(name: str) -> str:
    """Retourne la variable d'environnement `name`, ou lève une erreur claire."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Variable d'environnement manquante : {name}. "
            "Copiez .env.example vers .env et remplissez vos valeurs."
        )
    return value


# ==================== MODE ====================
# WATCH_ONLY=true : le bot OBSERVE seulement — détection des nouvelles paires
# et audit RugCheck, tout est enregistré en base pour le dashboard, mais AUCUN
# achat n'est envoyé. Ne nécessite ni clé privée ni portefeuille.
WATCH_ONLY: bool = os.getenv("WATCH_ONLY", "true").lower() in ("true", "1", "yes")

# ==================== PORTEFEUILLE VIRTUEL ====================
# Montant virtuel (€) "investi" sur chaque token au feu vert, pour suivre
# l'évolution du patrimoine simulé sur le dashboard. Aucun argent réel.
MEME_BUY_EUR: float = float(os.getenv("MEME_BUY_EUR", "1.0"))
# Liquidité (USD) en dessous de laquelle une position est considérée morte
# (rug / liquidité retirée) — elle disparaît de la liste affichée.
DEAD_LIQUIDITY_USD: float = float(os.getenv("DEAD_LIQUIDITY_USD", "20"))
# Si une position (ou un candidat) n'a jamais été coté(e) par DexScreener
# après ce délai, on abandonne (rug avant même l'indexation).
PENDING_TIMEOUT_MINUTES: int = int(os.getenv("PENDING_TIMEOUT_MINUTES", "30"))

# ==================== FILTRE DE TRACTION ====================
# Après l'audit RugCheck, un token passe par une période d'observation avant
# tout achat virtuel : sniper à la seconde 0 est presque toujours perdant
# (liquidité retirée en quelques minutes) — mieux vaut attendre une vraie
# traction (liquidité qui grossit, volume réel) plutôt qu'un feu vert
# purement technique.
TRACTION_WINDOW_MINUTES: int = int(os.getenv("TRACTION_WINDOW_MINUTES", "60"))
# Multiplicateur de liquidité exigé par rapport à la première cotation.
TRACTION_LIQUIDITY_MULT: float = float(os.getenv("TRACTION_LIQUIDITY_MULT", "2.0"))
# Volume (USD) sur la dernière heure exigé pour confirmer un intérêt réel.
TRACTION_MIN_VOLUME_USD: float = float(os.getenv("TRACTION_MIN_VOLUME_USD", "500"))

# ==================== NOTIFICATIONS (optionnel) ====================
# Alerte Telegram quand un token passe l'audit avec feu vert.
# Réutilisez le même bot que le projet trading_view.
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Fichier partagé de réglages de notification, écrit par le dashboard du
# projet trading_view (repo frère) ; ce watcher ne fait que le LIRE.
NOTIFICATION_SETTINGS_PATH: str = os.getenv(
    "NOTIFICATION_SETTINGS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "trading_view", "forex", "_cache", "notification_settings.json"),
)

# ==================== FILTRES DE SÉCURITÉ (RugCheck.xyz) ====================
RUGCHECK_API_BASE: str = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
RUGCHECK_TIMEOUT: float = float(os.getenv("RUGCHECK_TIMEOUT", "5"))
RUGCHECK_RETRIES: int = int(os.getenv("RUGCHECK_RETRIES", "3"))
RUGCHECK_RETRY_DELAY: float = float(os.getenv("RUGCHECK_RETRY_DELAY", "2"))
# Concentration maximale tolérée du plus gros holder hors pool de liquidité (%).
MAX_TOP_HOLDER_PERCENT: float = float(os.getenv("MAX_TOP_HOLDER_PERCENT", "30"))
# Seuil dédié pour les candidats source="telegram" — plus permissif que le
# défaut (31/07/2026, décision utilisateur) : sur les 6 premiers calls, 4
# étaient rejetés sur ce seul critère (concentration 79-99%), ne laissant
# quasiment rien passer. Reste appliqué uniquement à telegram, pas à
# onchain/momentum (voir process_new_candidates() dans main.py).
TELEGRAM_MAX_TOP_HOLDER_PERCENT: float = float(os.getenv("TELEGRAM_MAX_TOP_HOLDER_PERCENT", "60"))
# Espacement minimal entre deux appels RugCheck consécutifs (secondes) — leur
# API gratuite limite à ~1 req/s ; en dessous, on se fait 429-limiter en
# continu, ce qui fait échouer l'audit (fail-closed) sur des tokens
# potentiellement sûrs, pas juste sur les vrais dangereux.
RUGCHECK_MIN_INTERVAL_SECONDS: float = float(os.getenv("RUGCHECK_MIN_INTERVAL_SECONDS", "1.1"))
# Nombre max de candidats audités par cycle de confirmation (30s) — évite que
# le stade d'audit ne monopolise/bloque tout le cycle (et les détecteurs qui
# tournent en parallèle) si beaucoup de nouveaux tokens arrivent d'un coup ;
# le reste attend le cycle suivant (déjà géré par get_new_candidates(), qui
# re-sélectionne tout ce qui est encore status='new').
MAX_AUDITS_PER_CYCLE: int = int(os.getenv("MAX_AUDITS_PER_CYCLE", "15"))

# ==================== COTATIONS SOLANA ====================
# API publique pump.fun (frontend) pour la liste des nouveaux tokens et la
# progression de bonding curve — vérifiée en direct le 2026-07-26 contre
# frontend-api-v3.pump.fun (l'ancien domaine sans "-v3" est mort : HTTP 530
# sur tous les endpoints, quelle que soit la route).
PUMPFUN_FRONTEND_API: str = os.getenv(
    "PUMPFUN_FRONTEND_API", "https://frontend-api-v3.pump.fun",
)
QUOTES_HTTP_TIMEOUT: float = float(os.getenv("QUOTES_HTTP_TIMEOUT", "10"))

# ==================== DÉTECTEUR TELEGRAM ====================
# Identifiants d'application Telegram (my.telegram.org) — compte UTILISATEUR,
# pas un bot : la plupart des groupes d'alpha fermés refusent les bots.
TELEGRAM_API_ID: str = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_SESSION_NAME: str = os.getenv("TELEGRAM_SESSION_NAME", "telegram_detector")
# Channels à surveiller, séparés par des virgules (ex. "@groupe1,@groupe2").
# Vide par défaut — à remplir une fois les groupes identifiés.
TELEGRAM_CHANNELS: list[str] = [
    c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()
]

# ==================== DÉTECTEUR DISCORD ====================
# Token du bot (portail développeur Discord, https://discord.com/developers/
# applications) — contrairement à Telegram, Discord n'a pas d'équivalent
# "compte utilisateur" praticable : il faut un vrai bot, invité manuellement
# sur chaque serveur cible par un admin (droit "Gérer le serveur"), avec le
# "Message Content Intent" activé pour pouvoir lire le texte des messages.
DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# IDs numériques des salons à surveiller (mode développeur Discord activé ->
# clic droit sur le salon -> "Copier l'ID"), séparés par des virgules. Un ID
# de salon, pas de serveur : un bot peut être sur un serveur sans surveiller
# tous ses salons.
DISCORD_CHANNEL_IDS: list[int] = [
    int(c.strip()) for c in os.getenv("DISCORD_CHANNEL_IDS", "").split(",") if c.strip()
]

# ==================== CYCLE DE CONFIRMATION (audit/traction/positions) ====================
# À chaque cycle : évalue les candidats en attente (evaluate_candidates) et
# rafraîchit les positions ouvertes (refresh_positions) — 2 appels
# DexScreener par cycle (fetch_pool_quotes), quel que soit le nombre de
# tokens suivis (batché en un seul appel par fonction, jusqu'à 30 adresses).
# DexScreener ne documente pas de limite explicite pour cet endpoint
# (/latest/dex/pairs/) ; les endpoints qu'ils documentent explicitement sont
# à 60 req/min — valeur retenue par prudence comme plafond, avec une marge
# large. À 5s/cycle : 12 cycles/min × 2 appels = 24 req/min (40% du plafond
# prudent), largement sous la limite même si elle s'avérait plus stricte que
# prévu. fetch_pool_quotes() ne lève jamais (voir quotes.py) : un 429
# éventuel est absorbé sans crash, juste une cotation manquée ce cycle-là.
CONFIRMATION_LOOP_INTERVAL_SECONDS: float = float(os.getenv("CONFIRMATION_LOOP_INTERVAL_SECONDS", "5"))

# ==================== DÉTECTEUR ON-CHAIN (pump.fun) ====================
ONCHAIN_POLL_INTERVAL_SECONDS: float = float(os.getenv("ONCHAIN_POLL_INTERVAL_SECONDS", "5"))

# ==================== DÉTECTEUR MOMENTUM (marché large) ====================
# API publique GeckoTerminal (gratuite, sans clé) — vérifiée en direct le
# 30/07/2026 contre api.geckoterminal.com/api/v2/networks/solana/trending_pools :
# couvre tout le marché Solana (pas seulement pump.fun), contrairement au
# détecteur on-chain qui ne voit que les créations pump.fun.
GECKOTERMINAL_API_BASE: str = os.getenv(
    "GECKOTERMINAL_API_BASE", "https://api.geckoterminal.com/api/v2",
)
MOMENTUM_POLL_INTERVAL_SECONDS: float = float(os.getenv("MOMENTUM_POLL_INTERVAL_SECONDS", "60"))
# Seuils sur la fenêtre 5 minutes (m5) de la réponse GeckoTerminal — le tri
# "trending" de leur API mélange plusieurs signaux (transactions, volume,
# prix), pas seulement la hausse de prix : ces seuils filtrent le bruit
# (pools stables ou en baisse qui apparaissent quand même dans la liste).
MOMENTUM_MIN_PRICE_CHANGE_PCT: float = float(os.getenv("MOMENTUM_MIN_PRICE_CHANGE_PCT", "15"))
MOMENTUM_MIN_VOLUME_USD: float = float(os.getenv("MOMENTUM_MIN_VOLUME_USD", "2000"))
