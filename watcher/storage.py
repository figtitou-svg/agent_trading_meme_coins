"""
watcher/storage.py — Persistance SQLite des détections du watcher.

Chaque nouvelle paire détectée (et son verdict d'audit) est enregistrée
dans `memecoins.db`, que le dashboard du projet trading_view lit pour
afficher l'état des lieux : combien de tokens détectés, combien bloqués
et pourquoi, lesquels auraient été achetables.

Le chemin de la base est surchargeable via MEMECOINS_DB_PATH pour que
le dashboard (autre process, autre dossier) pointe sur le même fichier.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv(
    "MEMECOINS_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "memecoins.db"),
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Crée les tables si nécessaire. À appeler une fois au démarrage."""
    with _get_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                chain TEXT NOT NULL DEFAULT 'solana',
                token_address TEXT NOT NULL,
                token_name TEXT,
                token_symbol TEXT,
                pair_address TEXT,
                block_number INTEGER,
                verdict TEXT NOT NULL,      -- 'ok' | 'blocked' | 'bought' | 'error'
                reasons TEXT,               -- JSON : raisons de blocage (liste)
                source TEXT NOT NULL DEFAULT 'onchain'  -- 'telegram' | 'onchain'
            )
        ''')
        # Migration : ajouter les nouvelles colonnes si elles manquent.
        # Cas particulier : l'ancienne colonne `chain_id INTEGER NOT NULL`
        # (vestige EVM, sans défaut) ne peut pas être supprimée ni assouplie
        # par un simple ALTER TABLE en SQLite -> reconstruction complète de
        # la table (rename / create / copy / drop) quand elle est détectée.
        existing_det_cols = {r[1] for r in conn.execute('PRAGMA table_info(detections)')}
        if 'chain_id' in existing_det_cols:
            # DDL (RENAME/CREATE/DROP) runs in autocommit mode in sqlite3
            # unless a transaction is already open -- without this explicit
            # BEGIN, a crash mid-rebuild can leave the DB with no
            # `detections` table at all and legacy rows stranded in an
            # orphaned `detections_old`. BEGIN makes the whole rebuild one
            # real transaction that rolls back cleanly on failure.
            conn.execute('BEGIN')
            conn.execute('ALTER TABLE detections RENAME TO detections_old')
            conn.execute('''
                CREATE TABLE detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    chain TEXT NOT NULL DEFAULT 'solana',
                    token_address TEXT NOT NULL,
                    token_name TEXT,
                    token_symbol TEXT,
                    pair_address TEXT,
                    block_number INTEGER,
                    verdict TEXT NOT NULL,
                    reasons TEXT,
                    source TEXT NOT NULL DEFAULT 'onchain'
                )
            ''')
            conn.execute('''
                INSERT INTO detections
                    (id, timestamp, chain, token_address, token_name, token_symbol,
                     pair_address, block_number, verdict, reasons, source)
                SELECT
                    id, timestamp, 'solana', token_address, token_name, token_symbol,
                    pair_address, block_number, verdict, reasons, 'onchain'
                FROM detections_old
            ''')
            conn.execute('DROP TABLE detections_old')
            existing_det_cols = {r[1] for r in conn.execute('PRAGMA table_info(detections)')}
        if 'chain' not in existing_det_cols:
            conn.execute("ALTER TABLE detections ADD COLUMN chain TEXT NOT NULL DEFAULT 'solana'")
        if 'source' not in existing_det_cols:
            conn.execute("ALTER TABLE detections ADD COLUMN source TEXT NOT NULL DEFAULT 'onchain'")
        # Heartbeat : permet au dashboard de savoir si le watcher est vivant
        conn.execute('''
            CREATE TABLE IF NOT EXISTS heartbeat (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_seen TEXT NOT NULL,
                last_block INTEGER
            )
        ''')
        # Portefeuille virtuel : 1 € simulé sur chaque token au feu vert.
        # entry_price_usd est NULL tant que DexScreener n'a pas indexé la
        # paire (quelques minutes après création) — le watcher le remplit.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_time TEXT NOT NULL,
                token_address TEXT NOT NULL,
                token_name TEXT,
                token_symbol TEXT,
                pair_address TEXT NOT NULL,
                invested_eur REAL NOT NULL,
                entry_price_usd REAL,
                status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'open' | 'dead' | 'closed_profit' | 'closed_loss'
                died_at TEXT,
                peak_multiplier REAL DEFAULT 1.0,
                exit_multiplier REAL,
                source TEXT NOT NULL DEFAULT 'onchain',  -- 'telegram' | 'onchain'
                current_multiplier REAL
            )
        ''')
        # Migration : ajouter les nouvelles colonnes si elles manquent
        existing_cols = {r[1] for r in conn.execute('PRAGMA table_info(positions)')}
        if 'died_at' not in existing_cols:
            conn.execute('ALTER TABLE positions ADD COLUMN died_at TEXT')
        if 'peak_multiplier' not in existing_cols:
            conn.execute('ALTER TABLE positions ADD COLUMN peak_multiplier REAL DEFAULT 1.0')
        if 'exit_multiplier' not in existing_cols:
            conn.execute('ALTER TABLE positions ADD COLUMN exit_multiplier REAL')
        if 'source' not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN source TEXT NOT NULL DEFAULT 'onchain'")
        if 'current_multiplier' not in existing_cols:
            # Multiplicateur *actuel* (dernier prix connu / prix d'entrée),
            # distinct de peak_multiplier (le record jamais atteint) — sert
            # à calculer la valeur mark-to-market du portefeuille (recap),
            # mis à jour à chaque cycle par refresh_positions().
            conn.execute('ALTER TABLE positions ADD COLUMN current_multiplier REAL')

        # Candidats : tokens ayant passé l'audit, en observation avant achat.
        # L'achat virtuel (1€) n'a lieu qu'après confirmation de traction
        # (liquidité en croissance + volume réel), pas dès la détection —
        # les données montrent que sniper à la seconde 0 est presque
        # toujours perdant (liquidité retirée en quelques minutes).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                token_address TEXT NOT NULL,
                token_name TEXT,
                token_symbol TEXT,
                pair_address TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                    -- 'new' | 'on_curve' | 'pending_quote' | 'watching' | 'confirmed' | 'rejected'
                baseline_liquidity_usd REAL,
                baseline_price_usd REAL,
                watched_since TEXT,
                resolved_at TEXT,
                reject_reason TEXT,
                source TEXT NOT NULL DEFAULT 'onchain',  -- 'telegram' | 'onchain'
                source_channel TEXT,
                source_note TEXT
            )
        ''')
        # Migration : ajouter les nouvelles colonnes si elles manquent
        existing_cand_cols = {r[1] for r in conn.execute('PRAGMA table_info(candidates)')}
        if 'source' not in existing_cand_cols:
            conn.execute("ALTER TABLE candidates ADD COLUMN source TEXT NOT NULL DEFAULT 'onchain'")
        if 'source_channel' not in existing_cand_cols:
            conn.execute('ALTER TABLE candidates ADD COLUMN source_channel TEXT')
        if 'source_note' not in existing_cand_cols:
            conn.execute('ALTER TABLE candidates ADD COLUMN source_note TEXT')

        # NOTE sur le changement de défaut ci-dessus (status 'pending_quote' ->
        # 'new') : contrairement à la migration `chain_id` plus haut, aucune
        # migration de données n'est faite ici, volontairement.
        #   - `CREATE TABLE IF NOT EXISTS` ne touche pas aux bases déjà
        #     créées : ce défaut ne s'applique qu'aux tout nouveaux fichiers.
        #   - Sur une base pré-existante (ex. l'ancien watcher EVM en prod),
        #     d'éventuelles lignes 'pending_quote' héritées de l'ancien sens
        #     ("audité OK, en attente de cotation") portent des adresses EVM
        #     (0x...) — `fetch_pool_quotes` (Solana/DexScreener) ne les
        #     trouvera jamais. Elles suivent alors le chemin de repli déjà
        #     existant (jamais cotées -> rejetées après PENDING_TIMEOUT_MINUTES,
        #     ou liquidité jugée nulle -> rejetées immédiatement) : aucun
        #     crash, aucune fausse confirmation de traction, aucun argent réel
        #     en jeu (WATCH_ONLY). Contrairement au `chain_id NOT NULL` qui
        #     faisait planter chaque insertion, ce drift sémantique est donc
        #     sans risque et ne justifie pas de migration dédiée.


def record_detection(
    token_address: str,
    verdict: str,
    reasons: list[str] | None = None,
    token_name: str | None = None,
    token_symbol: str | None = None,
    pair_address: str | None = None,
    block_number: int | None = None,
    source: str = "onchain",
    chain: str = "solana",
) -> None:
    """Enregistre une détection et son verdict d'audit.

    `source` distingue le détecteur Telegram ('telegram') du détecteur
    on-chain ('onchain') ; `chain` est toujours 'solana' (le projet a
    abandonné l'EVM, plus de notion de chain_id)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            'INSERT INTO detections '
            '(timestamp, chain, token_address, token_name, token_symbol, '
            ' pair_address, block_number, verdict, reasons, source) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                now, chain, token_address, token_name, token_symbol,
                pair_address, block_number, verdict,
                json.dumps(reasons or [], ensure_ascii=False),
                source,
            ),
        )


def open_virtual_position(
    token_address: str,
    pair_address: str,
    invested_eur: float,
    token_name: str | None = None,
    token_symbol: str | None = None,
    source: str = "onchain",
) -> bool:
    """
    Ouvre une position virtuelle (simulation d'achat) sur un token accepté.
    Le prix d'entrée sera rempli dès que DexScreener cote la paire.

    Retourne True si une position a été réellement créée, False si elle
    existait déjà (no-op, même token détecté deux fois) — l'appelant DOIT
    vérifier cette valeur avant d'envoyer une notification d'achat : un
    candidat peut repasser par ce code plusieurs fois (re-détection par un
    autre détecteur, cycle qui retraite le même id) sans qu'un nouvel achat
    ait réellement lieu.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        # Pas de doublon si le même token est détecté deux fois
        exists = conn.execute(
            'SELECT 1 FROM positions WHERE token_address = ?', (token_address,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            'INSERT INTO positions '
            '(entry_time, token_address, token_name, token_symbol, pair_address, '
            ' invested_eur, entry_price_usd, status, source) '
            'VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)',
            (now, token_address, token_name, token_symbol, pair_address,
             invested_eur, 'pending', source),
        )
        return True


def find_open_positions_by_identifier(identifier: str) -> list[dict]:
    """
    Cherche des positions status='open' (prix d'entrée déjà connu, vendable
    tout de suite) par adresse exacte, ou par symbole/nom insensible à la
    casse — utilisé par la commande Telegram /sell. Plusieurs résultats
    possibles (même symbole sur deux tokens différents, fréquent sur les
    memecoins) : à l'appelant de désambiguïser plutôt que d'en choisir un
    au hasard.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' AND ("
            "  token_address = ? OR"
            "  LOWER(COALESCE(token_symbol, '')) = LOWER(?) OR"
            "  LOWER(COALESCE(token_name, '')) = LOWER(?)"
            ")",
            (identifier, identifier, identifier),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_positions() -> list[dict]:
    """Positions vivantes (pending ou open) — celles à recoter/surveiller."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, entry_time, token_address, token_name, token_symbol, "
            "pair_address, status, entry_price_usd, peak_multiplier, current_multiplier, "
            "invested_eur, source "
            "FROM positions WHERE status IN ('pending', 'open')"
        ).fetchall()
        return [dict(r) for r in rows]


def set_entry_price(position_id: int, price_usd: float) -> None:
    """Fixe le prix d'entrée d'une position dès la première cotation."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET entry_price_usd = ?, status = 'open' WHERE id = ?",
            (price_usd, position_id),
        )


def mark_position_dead(position_id: int) -> None:
    """
    Marque une position comme morte (liquidité disparue, ou jamais cotée
    après le délai imparti — rug avant indexation). Elle disparaît de la
    liste affichée mais reste comptée dans le P&L global du portefeuille.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET status = 'dead', died_at = ? WHERE id = ?",
            (now, position_id),
        )


def update_peak_multiplier(position_id: int, peak: float) -> None:
    """Met à jour le multiplicateur record atteint par la position."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET peak_multiplier = ? WHERE id = ?",
            (peak, position_id),
        )


def update_current_multiplier(position_id: int, current_multiplier: float) -> None:
    """Met à jour le multiplicateur *actuel* (dernier prix connu / prix
    d'entrée) — contrairement à peak_multiplier, peut redescendre. Appelé à
    chaque cycle de refresh_positions() pour que get_portfolio_summary()
    reflète la valeur mark-to-market réelle sans refaire d'appel réseau."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET current_multiplier = ? WHERE id = ?",
            (current_multiplier, position_id),
        )


def mark_position_closed(position_id: int, exit_multiplier: float, is_profit: bool) -> None:
    """Clôture une position selon la stratégie de Take Profit / Trailing Stop."""
    status = 'closed_profit' if is_profit else 'closed_loss'
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET status = ?, died_at = ?, exit_multiplier = ? WHERE id = ?",
            (status, now, exit_multiplier, position_id),
        )


def get_portfolio_summary() -> dict:
    """
    Retourne {"invested_eur": total misé (toutes positions, tout statut
    confondu), "current_value_eur": valeur actuelle du portefeuille virtuel}.

    La valeur actuelle dépend du statut de chaque position :
      - closed_profit/closed_loss : invested_eur * exit_multiplier (figée,
        déjà réalisée).
      - dead (rug / jamais cotée) : 0 (valeur perdue).
      - open : invested_eur * current_multiplier (mark-to-market, mis à
        jour à chaque cycle par refresh_positions() — pas d'appel réseau ici).
      - pending (achetée, prix d'entrée pas encore connu) : invested_eur
        (approximation raisonnable avant la première cotation).
    """
    with _get_conn() as conn:
        row = conn.execute('''
            SELECT
                COALESCE(SUM(invested_eur), 0) AS invested_eur,
                COALESCE(SUM(
                    CASE
                        WHEN status IN ('closed_profit', 'closed_loss')
                            THEN invested_eur * COALESCE(exit_multiplier, 1.0)
                        WHEN status = 'dead' THEN 0
                        WHEN status = 'open'
                            THEN invested_eur * COALESCE(current_multiplier, 1.0)
                        ELSE invested_eur
                    END
                ), 0) AS current_value_eur
            FROM positions
        ''').fetchone()
        return {"invested_eur": row["invested_eur"], "current_value_eur": row["current_value_eur"]}


def get_dead_positions_summary() -> dict:
    """Nombre et montant total investi dans les positions mortes."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(invested_eur), 0) AS total "
            "FROM positions WHERE status = 'dead'"
        ).fetchone()
        return {"count": row["n"], "invested_eur": row["total"]}


def add_candidate(
    token_address: str,
    pair_address: str,
    token_name: str | None = None,
    token_symbol: str | None = None,
    source: str = "onchain",
    source_channel: str | None = None,
    source_note: str | None = None,
) -> bool:
    """
    Ajoute un token brut à la liste d'observation, en attente d'audit
    sécurité (statut 'new' — voir process_new_candidates() dans main.py).

    Retourne True si un candidat a été réellement créé, False s'il existait
    déjà (no-op) — un même token peut être re-détecté plusieurs fois par le
    même détecteur (ex. le scan momentum le revoit tant qu'il reste dans le
    top trending) ou par un détecteur différent. L'appelant DOIT vérifier
    cette valeur avant de logger/notifier une "détection" pour éviter de
    répéter le même événement à chaque cycle.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        exists = conn.execute(
            'SELECT 1 FROM candidates WHERE token_address = ?', (token_address,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            'INSERT INTO candidates '
            '(detected_at, token_address, token_name, token_symbol, pair_address, status, '
            ' source, source_channel, source_note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (now, token_address, token_name, token_symbol, pair_address, 'new',
             source, source_channel, source_note),
        )
        return True


def get_candidates_by_status(status: str) -> list[dict]:
    """Candidats dans un statut donné, avec les champs nécessaires à l'évaluation."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, detected_at, token_address, token_name, token_symbol, "
            "pair_address, baseline_liquidity_usd, baseline_price_usd, watched_since, source "
            "FROM candidates WHERE status = ?",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def promote_candidate_to_pending_quote(candidate_id: int, pair_address: str) -> None:
    """Migration pump.fun confirmée (courbe -> pool PumpSwap) : le candidat
    passe en attente de première cotation post-migration."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'pending_quote', pair_address = ? WHERE id = ?",
            (pair_address, candidate_id),
        )


def reject_candidate_by_token(token_address: str, reason: str) -> None:
    """Rejette un candidat par son adresse (utilisé juste après l'audit
    sécurité, avant qu'il n'entre dans le cycle de confirmation)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'rejected', resolved_at = ?, reject_reason = ? "
            "WHERE token_address = ? AND status NOT IN ('rejected', 'confirmed')",
            (now, reason, token_address),
        )


def promote_candidate_to_on_curve(token_address: str) -> None:
    """Audit sécurité OK : le candidat entre en phase courbe (pré-migration)."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'on_curve' WHERE token_address = ? "
            "AND status = 'new'",
            (token_address,),
        )


def get_new_candidates() -> list[dict]:
    """Candidats bruts (pas encore audités par security_check)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, token_address, token_name, token_symbol, pair_address, source "
            "FROM candidates WHERE status = 'new'"
        ).fetchall()
        return [dict(r) for r in rows]


def set_candidate_baseline(candidate_id: int, liquidity_usd: float, price_usd: float) -> None:
    """Fixe la référence (liquidité/prix) au moment où le token est enfin coté,
    et démarre le chrono d'observation."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET baseline_liquidity_usd = ?, baseline_price_usd = ?, "
            "watched_since = ?, status = 'watching' WHERE id = ?",
            (liquidity_usd, price_usd, now, candidate_id),
        )


def reject_candidate(candidate_id: int, reason: str) -> None:
    """Sort un candidat de la liste (rug précoce ou traction insuffisante)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'rejected', resolved_at = ?, reject_reason = ? "
            "WHERE id = ?",
            (now, reason, candidate_id),
        )


def confirm_candidate(candidate_id: int) -> None:
    """Traction confirmée : le candidat devient une position (achat virtuel)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET status = 'confirmed', resolved_at = ? WHERE id = ?",
            (now, candidate_id),
        )


def get_candidates_funnel() -> dict:
    """Compte des candidats par statut, pour visualiser le tri sur le dashboard."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM candidates GROUP BY status"
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "watching": counts.get("pending_quote", 0) + counts.get("watching", 0),
            "confirmed_total": counts.get("confirmed", 0),
            "rejected_total": counts.get("rejected", 0),
        }


def touch_heartbeat(last_block: int | None = None) -> None:
    """Signale que le watcher est vivant (appelé périodiquement)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            'INSERT INTO heartbeat (id, last_seen, last_block) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET last_seen = ?, last_block = ?',
            (now, last_block, now, last_block),
        )


def count_candidates_watched_since(since_iso: str) -> int:
    """Nombre de candidats mis sous surveillance (audit OK) depuis `since_iso`."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE detected_at > ?", (since_iso,)
        ).fetchone()
        return row["n"]


def count_positions_opened_since(since_iso: str) -> int:
    """Nombre d'achats virtuels effectués depuis `since_iso`."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE entry_time > ?", (since_iso,)
        ).fetchone()
        return row["n"]


def get_positions_closed_since(since_iso: str) -> list[dict]:
    """Positions clôturées (gain ou perte) depuis `since_iso`, pour le recap périodique."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT token_address, invested_eur, status, exit_multiplier, died_at, source "
            "FROM positions "
            "WHERE status IN ('closed_profit', 'closed_loss') AND died_at > ?",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_candidates_by_source_since(since_iso: str) -> dict:
    """Compte candidats/positions par source depuis `since_iso`, pour le recap.

    Une valeur de `source` hors de 'telegram'/'onchain' (donnée corrompue ou
    évolution future du schéma — ne devrait pas arriver vu le défaut de
    colonne) est silencieusement ignorée : le recap reste sur les deux
    sources connues plutôt que de lever ou d'afficher une ligne inattendue."""
    with _get_conn() as conn:
        watched = conn.execute(
            "SELECT source, COUNT(*) AS n FROM candidates WHERE detected_at > ? GROUP BY source",
            (since_iso,),
        ).fetchall()
        bought = conn.execute(
            "SELECT source, COUNT(*) AS n FROM positions WHERE entry_time > ? GROUP BY source",
            (since_iso,),
        ).fetchall()
        result = {"telegram": {"watched": 0, "bought": 0}, "onchain": {"watched": 0, "bought": 0}}
        for r in watched:
            if r["source"] in result:
                result[r["source"]]["watched"] = r["n"]
        for r in bought:
            if r["source"] in result:
                result[r["source"]]["bought"] = r["n"]
        return result
