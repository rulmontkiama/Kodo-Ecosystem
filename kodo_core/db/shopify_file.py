# -*- coding: utf-8 -*-
"""
File d'attente locale `sync_queue` (mouvements de stock vers Shopify) : schéma, inscription dans la
transaction de la vente et calcul du délai entre deux tentatives.

La garantie de non-double-décompte reste tenue par la clé primaire de `Shopify_Sync_Lignes`
(voir `kodo_core/sync/shopify.py`) : cette file la complète, elle ne la remplace pas.

  - `inscrire_ticket` : appelé DANS la transaction qui crée la vente (ou le retour). La ligne naît
    donc avec le ticket ou n'existe pas : un rollback l'emporte, un crash ne la perd pas.
  - `prochaine_tentative` : recul exponentiel PAR LIGNE (30 s, 1 min, 2 min… plafonné à 15 min).
    Une ligne en échec n'est plus rejouée à chaque passe, et ne retarde pas les autres.

Ce module n'ouvre ni ne commite aucune transaction : l'appelant en est propriétaire.
"""

import datetime
import logging

logger = logging.getLogger("kodo_core.db.shopify_file")

TABLE = "sync_queue"

# Mêmes valeurs que celles que `kodo_core.sync.shopify` ré-exporte (FILE_*).
FILE_PENDING = "PENDING"
FILE_DONE = "DONE"
FILE_INDETERMINE = "INDETERMINE"
FILE_IGNORE = "IGNORE"

# Paramètre posé à l'activation de la poussée des ventes (`fixer_seuil_tickets`). Absent : la
# synchronisation n'a jamais été activée, rien ne doit s'accumuler dans la file.
PARAM_SEUIL_TICKETS = "shopify_seuil_ticket"

BACKOFF_BASE_S = 30
BACKOFF_PLAFOND_S = 15 * 60

_FORMAT = "%Y-%m-%d %H:%M:%S"


def assurer_table(cursor) -> None:
    """
    Crée la file si elle manque et y ajoute les colonnes apparues après la 2.0.9.

    Toujours additif : aucune ligne n'est touchée, et la fonction est sans effet sur une base à jour.
    """
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_vente_detail INTEGER NOT NULL UNIQUE,
            id_ticket INTEGER NOT NULL,
            code_barre TEXT,
            delta INTEGER NOT NULL,
            statut TEXT NOT NULL DEFAULT 'PENDING',
            tentatives INTEGER NOT NULL DEFAULT 0,
            derniere_erreur TEXT,
            date_creation TEXT NOT NULL,
            date_maj TEXT NOT NULL,
            prochaine_tentative TEXT
        )
    """)
    cursor.execute(f"PRAGMA table_info({TABLE})")
    if "prochaine_tentative" not in {r[1] for r in cursor.fetchall()}:
        cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN prochaine_tentative TEXT")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_sync_queue_statut ON {TABLE}(statut)")


def delai_backoff(tentatives: int) -> int:
    """Secondes d'attente après `tentatives` échecs (>= 1) : 30, 60, 120… plafonné à 15 minutes."""
    n = max(1, int(tentatives or 1))
    return min(BACKOFF_PLAFOND_S, BACKOFF_BASE_S * (2 ** min(n - 1, 16)))


def prochaine_tentative(tentatives: int, maintenant: datetime.datetime = None) -> str:
    """Horodatage local (même format que `date_maj`) avant lequel la ligne ne doit pas être rejouée."""
    maintenant = maintenant or datetime.datetime.now()
    return (maintenant + datetime.timedelta(seconds=delai_backoff(tentatives))).strftime(_FORMAT)


def est_due(prochaine: str, maintenant: datetime.datetime = None) -> bool:
    """Vrai si la ligne peut être (re)tentée : aucun délai posé, ou délai écoulé."""
    if not prochaine:
        return True
    maintenant = maintenant or datetime.datetime.now()
    return str(prochaine) <= maintenant.strftime(_FORMAT)


def inscrire_ticket(cursor, ticket_id: int) -> int:
    """
    Inscrit PENDING, dans la transaction de l'appelant, les lignes de stock du ticket à pousser.

    N'inscrit rien si la poussée n'a jamais été activée, si le ticket est déjà marqué synchronisé
    (commande web importée) ou si une ligne n'a pas de stock. Ne lève jamais : un défaut ici ne
    doit arrêter aucune vente (la passe de synchronisation inscrit de toute façon ce qui manque).
    Retourne le nombre de lignes inscrites.
    """
    try:
        cursor.execute("SELECT 1 FROM Parametres WHERE cle = ?", (PARAM_SEUIL_TICKETS,))
        if cursor.fetchone() is None:
            return 0
        cursor.execute("SELECT COALESCE(synced_shopify, 0) FROM Tickets WHERE id = ?", (ticket_id,))
        ligne = cursor.fetchone()
        if ligne is None or int(ligne[0] or 0) != 0:
            return 0

        assurer_table(cursor)
        maintenant = datetime.datetime.now().strftime(_FORMAT)
        cursor.execute(f"""
            INSERT OR IGNORE INTO {TABLE}
                (id_vente_detail, id_ticket, code_barre, delta, statut, tentatives,
                 date_creation, date_maj)
            SELECT vd.id, vd.id_ticket, p.code_barre, -vd.quantite, ?, 0, ?, ?
            FROM Ventes_Details vd
            JOIN Stocks s ON vd.id_stock = s.id
            JOIN Produits p ON s.id_produit = p.id
            WHERE vd.id_ticket = ? AND vd.quantite != 0
        """, (FILE_PENDING, maintenant, maintenant, ticket_id))
        return max(0, cursor.rowcount)
    except Exception as e:  # noqa: BLE001 - jamais bloquant pour l'encaissement
        logger.warning(f"File Shopify : ticket {ticket_id} non inscrit dans la transaction ({e}).")
        return 0
