# -*- coding: utf-8 -*-
"""
Journal des réécritures ABSOLUES de stock — Kōdo POS.

Pourquoi ce journal existe
--------------------------
Une vente se retrouve toujours : `Ventes_Details` dit quoi, quand, à quelle ligne de stock. Les
autres écritures de `Stocks.quantite_actuelle` ne laissaient aucune trace : l'import du catalogue
Shopify recopie la quantité de la boutique en ligne par-dessus celle de la caisse, et l'écran
Stocks enregistre la quantité saisie. Quand une commerçante demande « mon stock a changé tout
seul », la base ne pouvait répondre ni *quand*, ni *depuis quelle valeur*, ni *par quoi*.

Ce que le journal contient
--------------------------
Une ligne par quantité qui CHANGE réellement (avant ≠ après) sous l'effet d'une de ces écritures :
l'import Shopify, l'écran Stocks et, désormais les MOUVEMENTS DE STOCK saisis avec un motif
(réception, casse, vol, inventaire…, voir `kodo_core/services/mouvements_stock.py`). Rien n'est
écrit quand la valeur est identique (sauf un recomptage, qui confirme un stock exact). Les ventes,
retours et commandes Shopify n'y figurent pas : leurs propres tables les expliquent déjà, et
l'historique des mouvements les relit à la volée sans jamais les copier.

Colonnes ajoutées pour les mouvements : `delta` (variation signée), `motif` (code court),
`utilisateur` (qui a saisi) et `reference` (bon de livraison, ticket…). Elles sont ajoutées à la
demande sur les bases existantes (`ALTER TABLE`), sans migration.

Garanties
---------
- Le journal est écrit DANS la transaction de l'écriture qu'il décrit : si celle-ci est annulée,
  la trace l'est aussi, et inversement il ne peut pas y avoir de trace d'un changement qui n'a
  pas eu lieu.
- Il ne bloque JAMAIS une écriture de stock : toute erreur de journalisation est avalée et
  signalée dans les logs. Perdre une trace est un moindre mal que refuser d'enregistrer une vente
  ou une correction.
- La table est créée à la demande (`CREATE TABLE IF NOT EXISTS`), comme les tables de
  synchronisation Shopify : aucune migration à jouer, un correctif backend à distance suffit.

Choix de noms
-------------
Les colonnes s'appellent `stock_id` et `produit_id`, et non `id_stock` / `id_produit` : plusieurs
routines du catalogue parcourent TOUTES les tables pour y chercher une colonne `id_stock`
(« cette ligne de stock est-elle référencée ? ») ou `id_produit` (rattacher les tables d'un
doublon au produit conservé). Le journal est une trace historique : il ne doit ni empêcher la
suppression d'une ligne de stock ni être réécrit lors d'une fusion de produits.
"""
import datetime
import logging
import sqlite3

logger = logging.getLogger("kodo_core.db.stock_journal")

TABLE = "Stock_Journal"

ORIGINE_IMPORT_SHOPIFY = "import_shopify"   # `ShopifySync.import_catalog` : stock recopié depuis Shopify
ORIGINE_FICHE_PRODUIT = "fiche_produit"     # écran Stocks : quantité saisie à la main
ORIGINE_MOUVEMENT = "mouvement"             # mouvement de stock saisi avec un motif

# Colonnes propres aux mouvements (absentes des bases créées avant les mouvements de stock).
_COLONNES_MOUVEMENT = (("delta", "INTEGER"), ("motif", "TEXT"), ("utilisateur", "TEXT"), ("reference", "TEXT"))


def assurer_table(cursor) -> None:
    """Crée le journal s'il n'existe pas. À appeler dans la transaction de l'écriture à tracer."""
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_heure TEXT NOT NULL,
            stock_id INTEGER NOT NULL,
            produit_id INTEGER,
            produit TEXT,
            taille TEXT,
            avant INTEGER,
            apres INTEGER,
            origine TEXT NOT NULL,
            detail TEXT,
            delta INTEGER,
            motif TEXT,
            utilisateur TEXT,
            reference TEXT
        )
    """)
    # Base créée avant les mouvements : on ajoute les colonnes manquantes, sans toucher aux lignes.
    cursor.execute(f"PRAGMA table_info({TABLE})")
    existantes = {ligne[1] for ligne in cursor.fetchall()}
    for nom_col, type_col in _COLONNES_MOUVEMENT:
        if nom_col not in existantes:
            cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {nom_col} {type_col}")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_stock_journal_stock ON {TABLE}(stock_id)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_stock_journal_date ON {TABLE}(date_heure)")


def _inserer(cursor, valeurs) -> None:
    cursor.execute(
        f"INSERT INTO {TABLE} (date_heure, stock_id, produit_id, produit, taille, avant, apres, origine, detail, "
        "delta, motif, utilisateur, reference) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", valeurs)


def noter(cursor, stock_id, avant, apres, origine, *, produit_id=None, produit=None,
          taille=None, detail="", motif=None, utilisateur=None, reference=None,
          forcer=False) -> bool:
    """
    Inscrit un changement de quantité. Retourne True si une ligne a été écrite.

    `avant` vaut None pour une ligne de stock créée, `apres` pour une ligne supprimée. Rien n'est
    écrit si les deux valeurs sont égales, sauf avec `forcer=True` (recomptage qui CONFIRME le stock :
    la trace d'un inventaire sans écart reste utile). N'échoue jamais : voir l'en-tête du module.
    """
    try:
        if avant == apres and not forcer:
            return False
        delta = (apres or 0) - (avant or 0)
        valeurs = (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), stock_id, produit_id,
                   None if produit is None else str(produit), None if taille is None else str(taille),
                   avant, apres, origine, str(detail or "")[:200],
                   delta, None if motif is None else str(motif)[:40],
                   None if utilisateur is None else str(utilisateur)[:80],
                   None if reference is None else str(reference)[:80])
        try:
            _inserer(cursor, valeurs)
        except sqlite3.OperationalError as exc:
            # Cas courant : la table (ou ses colonnes de mouvement) n'existe pas encore sur cette
            # base. On ne la (re)déclare qu'alors, pas à chaque ligne : un import écrit des
            # centaines de traces.
            message = str(exc).lower()
            if "no such table" not in message and "no column named" not in message and "has no column" not in message:
                raise
            assurer_table(cursor)
            _inserer(cursor, valeurs)
        return True
    except Exception as exc:  # noqa: BLE001 — la trace ne doit jamais empêcher l'écriture décrite
        logger.warning(f"Journal de stock non écrit (stock {stock_id}, {origine}) : {exc}")
        return False
