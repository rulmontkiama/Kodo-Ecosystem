# -*- coding: utf-8 -*-
"""
Lignes de stock à ALIGNER sur la boutique en ligne — Kōdo POS.

Pourquoi ce module existe
-------------------------
Vers Shopify, la caisse n'envoyait que les VENTES et les retours : des mouvements relatifs. Une
quantité saisie à la main sur la fiche produit (réassort, correction, inventaire) ne partait
nulle part. Kōdo affichait le bon stock, la boutique en ligne gardait l'ancien — « le site ne met
pas les bonnes tailles » — sans qu'aucun écran ni aucun message ne le dise.

Ce que le module garde
----------------------
Une ligne par ligne de stock dont la quantité a été saisie à la main et que la boutique en ligne
n'a pas encore rattrapée (`Shopify_Alignements`). La synchronisation la traite à la passe
suivante : elle LIT la quantité réellement affichée en ligne puis envoie la différence (un
ajustement relatif), de sorte que le site égale la caisse sans « écraser » un mouvement survenu
entre-temps (commande web, vente en cours d'envoi).

Garanties
---------
- Marquer ne bloque JAMAIS l'écriture d'un stock : toute erreur est avalée et signalée dans les
  logs. Perdre un marquage est un moindre mal que refuser d'enregistrer une correction.
- Le marquage est écrit DANS la transaction de l'écriture qu'il décrit : si elle est annulée,
  il l'est aussi.
- Rien n'est marqué tant que Shopify n'est pas configuré et la poussée du stock allumée : ce qui
  a été saisi avant la connexion n'est pas « rattrapé » dans le dos de la commerçante (l'outil de
  comparaison existe pour cela, sur demande explicite).
- La table est créée à la demande, comme `Stock_Journal` : aucune migration à jouer, un correctif
  backend à distance suffit.

Choix de noms
-------------
Les colonnes s'appellent `stock_id` et `produit_id`, et non `id_stock` / `id_produit` : des
routines du catalogue parcourent TOUTES les tables pour y chercher ces colonnes (« cette ligne de
stock est-elle référencée ? », rattachement des tables d'un doublon fusionné). Cette table est
un état de travail : elle ne doit ni empêcher la suppression d'une ligne de stock ni être
réécrite lors d'une fusion de produits.
"""
import datetime
import logging
import sqlite3

logger = logging.getLogger("kodo_core.db.shopify_alignement")

TABLE = "Shopify_Alignements"

STATUT_A_ALIGNER = "PENDING"        # à traiter à la prochaine passe (ou en échec, à retenter)
STATUT_ALIGNE = "DONE"              # le site égalait la caisse après cette lecture
STATUT_ABSENT = "ABSENT"            # article inconnu de la boutique en ligne : rien à aligner
STATUT_SANS_NIVEAU = "SANS_NIVEAU"  # variante sans niveau de stock dans ce dépôt (stock non suivi)
STATUT_SUPPRIMEE = "SUPPRIMEE"      # ligne de stock disparue de Kōdo avant l'alignement

ORIGINE_FICHE_PRODUIT = "fiche_produit"   # écran Stocks : quantité saisie à la main
ORIGINE_COMPARAISON = "comparaison"       # outil « comparer / aligner le stock en ligne »

# Mêmes clés de réglages que `kodo_core.sync.shopify` (un test vérifie qu'elles ne divergent pas) :
# ce module est volontairement sans dépendance, pour pouvoir être appelé depuis le catalogue.
_CLE_URL = "shopify_store_url"
_CLE_TOKEN = "shopify_access_token"
_CLE_CLIENT_ID = "shopify_client_id"
_CLE_CLIENT_SECRET = "shopify_client_secret"
_CLE_AUTO_SYNC = "shopify_auto_sync"


def assurer_table(cursor) -> None:
    """Crée la table si elle n'existe pas. À appeler dans la transaction de l'écriture à tracer."""
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            stock_id INTEGER PRIMARY KEY,
            produit_id INTEGER,
            produit TEXT,
            taille TEXT,
            origine TEXT NOT NULL,
            statut TEXT NOT NULL DEFAULT '{STATUT_A_ALIGNER}',
            version INTEGER NOT NULL DEFAULT 1,
            tentatives INTEGER NOT NULL DEFAULT 0,
            derniere_erreur TEXT,
            quantite_kodo INTEGER,
            quantite_site INTEGER,
            ajustement INTEGER,
            date_creation TEXT NOT NULL,
            date_maj TEXT NOT NULL
        )
    """)
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_shopify_alignements_statut ON {TABLE}(statut)")


def poussee_stock_active(cursor) -> bool:
    """
    Vrai si la boutique en ligne est reliée (domaine + jeton, ou identifiant client + secret) ET
    que la synchronisation du stock est allumée. Absence de la clé = allumée, comme à l'écran.
    """
    cursor.execute(
        "SELECT cle, valeur FROM Parametres WHERE cle IN (?, ?, ?, ?, ?)",
        (_CLE_URL, _CLE_TOKEN, _CLE_CLIENT_ID, _CLE_CLIENT_SECRET, _CLE_AUTO_SYNC),
    )
    p = {row[0]: str(row[1] if row[1] is not None else "").strip() for row in cursor.fetchall()}
    relie = bool(p.get(_CLE_URL)) and bool(
        p.get(_CLE_TOKEN) or (p.get(_CLE_CLIENT_ID) and p.get(_CLE_CLIENT_SECRET)))
    return relie and p.get(_CLE_AUTO_SYNC, "1") == "1"


_SQL_MARQUER = f"""
    INSERT INTO {TABLE}
        (stock_id, produit_id, produit, taille, origine, statut, version, tentatives,
         derniere_erreur, date_creation, date_maj)
    VALUES (?, ?, ?, ?, ?, '{STATUT_A_ALIGNER}', 1, 0, NULL, ?, ?)
    ON CONFLICT(stock_id) DO UPDATE SET
        produit_id = excluded.produit_id,
        produit = excluded.produit,
        taille = excluded.taille,
        origine = excluded.origine,
        statut = '{STATUT_A_ALIGNER}',
        version = {TABLE}.version + 1,
        tentatives = 0,
        derniere_erreur = NULL,
        date_maj = excluded.date_maj
"""


def marquer(cursor, stock_id, *, origine=ORIGINE_FICHE_PRODUIT, produit_id=None, produit=None,
            taille=None, forcer=False) -> bool:
    """
    Note qu'une ligne de stock est à aligner. Retourne True si une ligne a été écrite.

    `forcer` saute le contrôle « Shopify relié et poussée allumée » : réservé à l'outil de
    comparaison, dont l'appelant a déjà établi que la boutique répond. N'échoue jamais.

    Chaque nouveau marquage incrémente `version` : une passe qui traite la ligne pendant que la
    commerçante la modifie encore ne peut pas la déclarer « alignée » sur une valeur périmée.
    """
    try:
        if not forcer and not poussee_stock_active(cursor):
            return False
        maintenant = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        valeurs = (stock_id, produit_id, None if produit is None else str(produit),
                   None if taille is None else str(taille), origine, maintenant, maintenant)
        try:
            cursor.execute(_SQL_MARQUER, valeurs)
        except sqlite3.OperationalError as exc:
            # Cas courant : première trace de cette base. On ne déclare la table qu'alors.
            if "no such table" not in str(exc).lower():
                raise
            assurer_table(cursor)
            cursor.execute(_SQL_MARQUER, valeurs)
        return True
    except Exception as exc:  # noqa: BLE001 — marquer ne doit jamais empêcher l'écriture décrite
        logger.warning(f"Ligne de stock non marquée à aligner (stock {stock_id}) : {exc}")
        return False


def retirer(cursor, stock_id) -> None:
    """Oublie le marquage d'une ligne de stock supprimée. N'échoue jamais, table absente comprise."""
    try:
        cursor.execute(f"DELETE FROM {TABLE} WHERE stock_id = ?", (stock_id,))
    except sqlite3.OperationalError:
        pass  # table jamais créée : rien à retirer
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Marquage d'alignement non retiré (stock {stock_id}) : {exc}")


def compter_a_aligner(cursor) -> int:
    """Nombre de lignes encore à aligner (0 si la table n'existe pas)."""
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE statut = ?", (STATUT_A_ALIGNER,))
        return int(cursor.fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0
