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
Une ligne par quantité qui CHANGE réellement (avant ≠ après) sous l'effet d'une de ces deux
écritures. Rien n'est écrit quand la valeur est identique. Les ventes, retours et commandes
Shopify n'y figurent pas : leurs propres tables les expliquent déjà.

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
            detail TEXT
        )
    """)
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_stock_journal_stock ON {TABLE}(stock_id)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_stock_journal_date ON {TABLE}(date_heure)")


def _inserer(cursor, valeurs) -> None:
    cursor.execute(
        f"INSERT INTO {TABLE} (date_heure, stock_id, produit_id, produit, taille, avant, apres, origine, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", valeurs)


def noter(cursor, stock_id, avant, apres, origine, *, produit_id=None, produit=None,
          taille=None, detail="") -> bool:
    """
    Inscrit un changement de quantité. Retourne True si une ligne a été écrite.

    `avant` vaut None pour une ligne de stock créée, `apres` pour une ligne supprimée. Rien n'est
    écrit si les deux valeurs sont égales. N'échoue jamais : voir l'en-tête du module.
    """
    try:
        if avant == apres:
            return False
        valeurs = (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), stock_id, produit_id,
                   None if produit is None else str(produit), None if taille is None else str(taille),
                   avant, apres, origine, str(detail or "")[:200])
        try:
            _inserer(cursor, valeurs)
        except sqlite3.OperationalError as exc:
            # Cas courant : la table n'existe pas encore (première trace de cette base). On ne la
            # (re)déclare qu'alors, pas à chaque ligne : un import écrit des centaines de traces.
            if "no such table" not in str(exc).lower():
                raise
            assurer_table(cursor)
            _inserer(cursor, valeurs)
        return True
    except Exception as exc:  # noqa: BLE001 — la trace ne doit jamais empêcher l'écriture décrite
        logger.warning(f"Journal de stock non écrit (stock {stock_id}, {origine}) : {exc}")
        return False
