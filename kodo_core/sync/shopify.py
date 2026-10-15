"""
Kōdo POS - Synchronisation Bidirectionnelle Shopify (REST & GraphQL API)
Gestion robuste du catalogue, des variantes, du stock et des commandes avec retry et logging.
"""

import os
import sys
import threading
import time
import json
import logging
import ssl
import urllib.request
import urllib.error
import urllib.parse
import datetime
import io
import re
from decimal import Decimal, ROUND_HALF_UP
from database_manager import get_connection, signer_ticket, signer_ledger
from kodo_core.db import stock_journal, shopify_alignement
from kodo_core.sync.shopify_sku import prefixes_possibles, skus_produit

logger = logging.getLogger("kodo_core.sync.shopify")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[SHOPIFY SYNC] %(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def get_ssl_context():
    """
    Contexte TLS avec vérification du certificat ET du nom d'hôte, toujours activée.

    Avant, ce contexte forçait `check_hostname = False` / `CERT_NONE` : le jeton d'administration
    Shopify (en-tête `X-Shopify-Access-Token`, qui donne accès au catalogue, aux stocks et aux
    commandes) partait alors dans un tunnel qu'un intermédiaire pouvait présenter sans certificat
    valable. La désactivation venait d'un vrai symptôme — le magasin de certificats système n'est
    pas toujours visible depuis un Python empaqueté par PyInstaller — mais le remède était pire.
    On réutilise le helper maison `updater.build_ssl_context()` (magasin `certifi` embarqué dans
    le build via `Kodo_POS.spec` → `collect_all('certifi')`), comme `license.py` et
    `offline_engine.py` : un seul endroit à auditer pour tous les canaux sortants sensibles.
    """
    try:
        from kodo_core.services.updater import build_ssl_context
        return build_ssl_context()
    except Exception:
        # Repli local strictement équivalent, si l'updater n'est pas importable.
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()


# --- Réglages Shopify (mêmes clés et mêmes défauts que l'écran Paramètres) -------------------

CLE_URL = "shopify_store_url"
CLE_TOKEN = "shopify_access_token"
CLE_AUTO_SYNC = "shopify_auto_sync"
CLE_SYNC_ORDERS = "shopify_sync_orders"
# Repli : « 1 » rétablit l'ancien comportement (une commande en ligne devient un ticket de caisse + une
# écriture au journal de caisse). Par défaut, une commande en ligne ne passe PAS par la caisse : sa
# comptabilité se fait dans Shopify, celle du magasin dans Kōdo (décision du 2026-10-15).
CLE_COMMANDES_EN_CAISSE = "shopify_commandes_en_caisse"

# Authentification « application récente » (Dev Dashboard). Depuis le 1er janvier 2026 Shopify ne
# permet plus de créer d'application personnalisée depuis l'admin : les nouvelles applications
# n'affichent AUCUN jeton, elles donnent un identifiant client et un secret, à échanger contre un
# jeton qui n'est valable que 24 heures (« client credentials grant »). Un jeton collé à la main
# dans l'écran Paramètres cessait donc de marcher le lendemain, sans qu'aucun réglage n'ait changé.
# Le moteur garde l'identifiant et le secret, et renouvelle le jeton lui-même avant l'échéance.
CLE_CLIENT_ID = "shopify_client_id"
CLE_CLIENT_SECRET = "shopify_client_secret"
CLE_JETON_EXPIRE = "shopify_token_expires_at"      # secondes epoch ; vide = jeton saisi à la main
CLE_VERSION_API = "shopify_api_version"            # forçage de support ; vide = VERSION_API_DEFAUT
CLE_VERSION_SERVIE = "shopify_api_version_servie"  # version réellement servie par Shopify

# Version d'API demandée. Shopify ne garantit chaque version que douze mois (« 2025-01 » l'a été
# jusqu'au 16 janvier 2026) ; au-delà il sert EN SILENCE la plus ancienne encore supportée, et
# cette version glisse d'un trimestre à l'autre. La version est donc explicite, et celle que
# Shopify SERT réellement est lue dans la réponse (`X-Shopify-API-Version`) pour que le décalage
# soit visible à l'écran avant de devenir une panne. À relever au moins une fois par an.
VERSION_API_DEFAUT = "2026-01"

# Le jeton géré par l'application est remplacé quand il lui reste moins de dix minutes.
MARGE_RENOUVELLEMENT_JETON_S = 600

# Un seul renouvellement à la fois dans le processus : le fil de synchro et les routes de
# l'écran Paramètres construisent chacun leur moteur, et deux demandes simultanées donnaient
# deux jetons dont l'un invalidait l'autre.
_VERROU_JETON = threading.RLock()

# Dépôt d'inventaire visé dans la boutique. Une boutique Shopify peut en compter plusieurs
# (le magasin, une réserve, un entrepôt de préparation, un marché temporaire) et l'ajustement
# de stock s'applique à UN dépôt nommé : sans ce réglage, la caisse choisissait le premier
# que l'API renvoyait, dans un ordre que Shopify ne garantit pas. Le stock d'une vente
# pouvait donc être retiré de la réserve pendant que la boutique en ligne continuait
# d'afficher le magasin plein — sans aucune erreur nulle part.
CLE_LOCATION = "shopify_location_id"

# Ce que la commerçante doit savoir sur le choix du dépôt, restitué par
# `GET /api/shopify/status`. Un `logger.warning` ne sort pas de la machine : personne ne le lit.
CLE_DEPOT_AVERTISSEMENT = "shopify_depot_avertissement"

# État de la dernière passe, restitué à la commerçante par `GET /api/shopify/status`.
# `Parametres` étant une table clé/valeur déjà en place, aucune migration n'est nécessaire.
CLE_ETAT_DATE = "shopify_last_sync_at"
CLE_ETAT_OK = "shopify_last_sync_ok"
CLE_ETAT_MESSAGE = "shopify_last_sync_message"

# Repère de la dernière inspection des remboursements : on ne redemande à Shopify que les
# commandes modifiées DEPUIS. Sans repère il faudrait relire tout l'historique à chaque passe.
CLE_REMB_DEPUIS = "shopify_remboursements_verifies_jusqua"

# Fenêtre de rattrapage au tout premier passage (et après une longue coupure) : 90 jours
# couvrent largement les délais de retour usuels sans rapatrier des années de commandes.
FENETRE_REMBOURSEMENTS_JOURS = 90

# Recouvrement appliqué au repère : `updated_at_min` est une borne INCLUSIVE côté Shopify et
# les horloges ne sont pas parfaitement alignées. On recule d'une minute pour ne jamais rater
# un remboursement ; le rejouer est sans effet, la clé primaire du journal le refuse.
RECOUVREMENT_REMBOURSEMENTS_S = 60


def normaliser_domaine_boutique(valeur) -> str:
    """
    Réduit ce que la commerçante a collé dans les réglages au seul domaine de SA boutique.

    `/api/settings` ne retire que le protocole et les `/` de bord : un domaine copié depuis
    l'administration Shopify (« https://MaStore.myshopify.com/admin ») repartait tel quel et
    produisait `https://mastore.myshopify.com/admin/admin/api/2025-01/products.json` — un 404
    que le moteur interprétait comme « cette boutique n'a aucun produit ».

    On ne garde donc que l'hôte (chemin, identifiants, `?`, `#` et espaces retirés, casse
    normalisée), en conservant un port non standard, qui n'existe que pour un serveur local.
    Retourne "" si rien d'exploitable n'a été saisi.
    """
    texte = str(valeur or "").strip()
    if not texte:
        return ""
    texte = texte.split("#", 1)[0].split("?", 1)[0].strip()
    if "://" not in texte:
        texte = "//" + texte
    try:
        parsed = urllib.parse.urlsplit(texte)
        hote = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except Exception:
        return ""
    if not hote:
        return ""
    if port and port not in (80, 443):
        return f"{hote}:{port}"
    return hote


# Seul suffixe où répond l'API d'administration Shopify (voir `domaine_boutique_valide`).
SUFFIXE_BOUTIQUE = ".myshopify.com"


def _est_boucle_locale(domaine: str) -> bool:
    """Vrai si le domaine désigne la machine elle-même (serveur d'essai, jamais une vraie boutique)."""
    return domaine.split(":", 1)[0] in ("localhost", "127.0.0.1", "::1", "[::1]")


def domaine_boutique_valide(domaine: str) -> bool:
    """
    Vrai si le domaine normalisé peut désigner une boutique Shopify.

    L'API d'administration ne vit QUE sur `<boutique>.myshopify.com` : un domaine
    personnalisé ne sert que la vitrine et ne répond jamais `/admin/api/...`. Accepter
    n'importe quel hôte pointé revenait donc à laisser l'appelant choisir la destination de
    l'en-tête `X-Shopify-Access-Token`. Un simple `POST /api/shopify/test` avec un domaine
    quelconque et sans jeton suffisait : la route complétait le jeton depuis `Parametres` et
    le moteur l'expédiait, en HTTPS vérifié, au serveur du demandeur. Le jeton
    d'administration de la vraie boutique — catalogue, stocks, commandes, donc les données
    des clientes — partait ainsi en une requête.

    La boucle locale reste admise : c'est le serveur d'essai des tests d'intégration, jamais
    une vraie boutique.
    """
    if not domaine:
        return False
    if _est_boucle_locale(domaine):
        return True
    hote = domaine.split(":", 1)[0].lower()
    return hote.endswith(SUFFIXE_BOUTIQUE) and len(hote) > len(SUFFIXE_BOUTIQUE)


def enregistrer_etat_sync(ok: bool, message: str = ""):
    """
    Mémorise le résultat de la dernière passe dans `Parametres`.

    Sans cela, une synchronisation qui échoue en boucle (jeton révoqué, domaine mal saisi) ne
    remonte nulle part : la commerçante voit un interrupteur allumé et croit que tout circule.
    """
    try:
        conn = get_connection()
        try:
            c = _ouvrir_ecriture(conn)
            horodatage = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for cle, valeur in ((CLE_ETAT_DATE, horodatage),
                                (CLE_ETAT_OK, "1" if ok else "0"),
                                (CLE_ETAT_MESSAGE, str(message or "")[:500])):
                c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Impossible d'enregistrer l'état de synchronisation Shopify : {e}")


def lire_etat_sync() -> dict:
    """État de la dernière passe (date, succès, message, dépôt), pour l'écran Paramètres."""
    etat = {"derniere_synchro": None, "succes": None, "message": "", "avertissement_depot": "",
            "version_api_servie": ""}
    try:
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT cle, valeur FROM Parametres WHERE cle IN (?, ?, ?, ?, ?)",
                      (CLE_ETAT_DATE, CLE_ETAT_OK, CLE_ETAT_MESSAGE, CLE_DEPOT_AVERTISSEMENT,
                       CLE_VERSION_SERVIE))
            params = {row[0]: row[1] for row in c.fetchall()}
        finally:
            conn.close()
        etat["derniere_synchro"] = params.get(CLE_ETAT_DATE)
        if CLE_ETAT_OK in params:
            etat["succes"] = params.get(CLE_ETAT_OK) == "1"
        etat["message"] = params.get(CLE_ETAT_MESSAGE, "") or ""
        etat["avertissement_depot"] = params.get(CLE_DEPOT_AVERTISSEMENT, "") or ""
        etat["version_api_servie"] = params.get(CLE_VERSION_SERVIE, "") or ""
    except Exception as e:
        logger.error(f"Impossible de lire l'état de synchronisation Shopify : {e}")
    return etat


def lire_reglages_shopify(conn=None) -> dict:
    """
    Les réglages Shopify : URL, jeton, dépôt d'inventaire, push du stock, rapatriement.

    Les défauts sont EXACTEMENT ceux de `/api/settings` (clé absente → "1", donc interrupteur
    allumé à l'écran) : un défaut divergent ici ferait mentir l'interrupteur affiché à la
    commerçante — coché dans les réglages, mais sans effet réel.
    """
    reglages = {"store_url": "", "access_token": "", "location_id": "",
                "auto_sync": True, "sync_orders": True,
                "client_id": "", "client_secret": "", "token_expire_a": 0.0, "api_version": ""}
    fermer = conn is None
    try:
        conn = conn or get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT cle, valeur FROM Parametres WHERE cle IN (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CLE_URL, CLE_TOKEN, CLE_LOCATION, CLE_AUTO_SYNC, CLE_SYNC_ORDERS,
             CLE_CLIENT_ID, CLE_CLIENT_SECRET, CLE_JETON_EXPIRE, CLE_VERSION_API),
        )
        params = {row[0]: row[1] for row in c.fetchall()}
        reglages["store_url"] = str(params.get(CLE_URL) or "").strip()
        reglages["access_token"] = str(params.get(CLE_TOKEN) or "").strip()
        reglages["location_id"] = str(params.get(CLE_LOCATION) or "").strip()
        reglages["auto_sync"] = str(params.get(CLE_AUTO_SYNC, "1")) == "1"
        reglages["sync_orders"] = str(params.get(CLE_SYNC_ORDERS, "1")) == "1"
        reglages["client_id"] = str(params.get(CLE_CLIENT_ID) or "").strip()
        reglages["client_secret"] = str(params.get(CLE_CLIENT_SECRET) or "").strip()
        try:
            reglages["token_expire_a"] = float(params.get(CLE_JETON_EXPIRE) or 0)
        except (TypeError, ValueError):
            reglages["token_expire_a"] = 0.0
        reglages["api_version"] = str(params.get(CLE_VERSION_API) or "").strip()
    except Exception as e:
        logger.error(f"Erreur lors du chargement de la configuration Shopify : {e}")
    finally:
        if fermer and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return reglages


def identifiants_presents(reglages: dict) -> bool:
    """
    Vrai si la boutique a de quoi s'authentifier : un jeton saisi à la main (ancienne application
    personnalisée), ou un identifiant client ET un secret (application récente, jeton renouvelé).
    """
    return bool(reglages.get("access_token")
                or (reglages.get("client_id") and reglages.get("client_secret")))


# --- Journal de synchronisation (idempotence ligne à ligne) ---------------------------------

STATUT_EN_VOL = "EN_VOL"                # ajustement envoyé, réponse pas encore reçue
STATUT_POUSSE = "POUSSE"                # ajustement confirmé par Shopify
STATUT_ABSENT = "ABSENT_SHOPIFY"        # article inconnu de la boutique : rien à ajuster
STATUT_SANS_OBJET = "SANS_OBJET"        # quantité nulle : rien à ajuster
STATUT_INDETERMINE = "INDETERMINE"      # réponse jamais reçue : à vérifier à la main

# --- File d'attente locale `sync_queue` -----------------------------------------------------
#
# Chaque mouvement de stock de la caisse y est inscrit PENDING AVANT tout envoi, puis passe à
# DONE (confirmé par Shopify), INDETERMINE (réponse jamais reçue : jamais rejoué) ou IGNORE
# (rien à ajuster). Un échec réseau/429/5xx laisse la ligne PENDING : elle est rejouée à la passe
# suivante, au rythme (recul exponentiel jusqu'à 15 min) de la boucle de synchronisation.
# Cette table ÉCLAIRE la file (compteur « en attente », prochaine tentative) ; la garantie de
# non-double-décompte reste tenue par la clé primaire de `Shopify_Sync_Lignes`, inchangée.
from kodo_core.db.shopify_file import (  # noqa: E402 - définitions uniques de la file
    FILE_PENDING, FILE_DONE, FILE_INDETERMINE, FILE_IGNORE,
    PARAM_SEUIL_TICKETS,
    assurer_table as _assurer_table_file,
    delai_backoff, est_due, prochaine_tentative,
)
FILE_CONSERVATION_JOURS = 14  # les lignes terminées sont purgées au-delà

# `urlopen` d'origine : tant qu'il n'est pas substitué (tests, outils), les appels passent par
# la session HTTP persistante ; s'il l'est, le substitut garde la main sur le transport.
_URLOPEN_ORIGINE = urllib.request.urlopen


def _ouvrir_ecriture(conn):
    """
    Curseur en écriture exclusive (patron maison, cf. `kodo_core/domain/sales/cart_engine.py`).

    En DEFERRED, deux passes de synchronisation simultanées (thread automatique + import lancé
    depuis l'écran) lisent le même journal, se croient toutes deux autorisées à pousser la ligne,
    et le stock Shopify est décrémenté deux fois.
    """
    c = conn.cursor()
    if not conn.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    return c


def _sceller_tickets(conn, numeros):
    """Scelle dans le journal fiscal les tickets (commande web, remboursement) qui viennent d'être commités.

    Ces tickets étaient signés dans la chaîne des tickets mais absents du journal fiscal inaltérable,
    qui ne recevait que les ventes de caisse. Ne lève jamais : un incident de scellement ne doit ni
    défaire une commande importée ni bloquer la synchronisation (le ticket est alors journalisé et
    rescellé au prochain démarrage, cf. `fiscal_service.sceller_ticket`)."""
    try:
        from kodo_core.services.fiscal_service import sceller_ticket
    except Exception:
        return
    for numero in numeros or ():
        try:
            sceller_ticket(conn, numero)
        except Exception as e:  # pragma: no cover - sceller_ticket ne lève pas
            logger.warning(f"[FISCAL LEDGER] Scellement du ticket {numero} impossible : {e}")


def _assurer_tables_sync(cursor):
    """
    Tables de travail de la synchronisation Shopify.

    Ces définitions sont désormais reprises à l'identique par les migrations versionnées
    2.0.5 (`Shopify_Sync_Lignes`, `Shopify_Variantes`) et 2.0.6 (`Shopify_Remboursements`),
    qui sont le chemin normal d'installation. On les conserve ici en filet : le moteur reste
    utilisable sur une base ouverte par un chemin qui n'aurait pas joué les migrations, et
    `CREATE TABLE IF NOT EXISTS` ne coûte rien quand elles existent déjà. Toute évolution de
    schéma doit être portée aux DEUX endroits.
    """
    # Journal ligne à ligne des ajustements poussés vers Shopify. La clé primaire EST la garantie
    # d'idempotence : une ligne de vente déjà poussée ne peut pas l'être une seconde fois, même si
    # le ticket entier n'a pas pu être marqué comme synchronisé.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Shopify_Sync_Lignes (
            id_vente_detail INTEGER PRIMARY KEY,
            id_ticket INTEGER NOT NULL,
            code_barre TEXT,
            inventory_item_id INTEGER,
            quantite_poussee INTEGER NOT NULL DEFAULT 0,
            statut TEXT NOT NULL,
            date_heure TEXT NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_shopify_sync_lignes_ticket ON Shopify_Sync_Lignes(id_ticket)"
    )
    # Correspondance variante Shopify → produit local. C'est la clé de réconciliation stable :
    # le SKU et le code-barres d'une variante peuvent changer côté Shopify, son id non.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Shopify_Variantes (
            variant_id INTEGER PRIMARY KEY,
            id_produit INTEGER NOT NULL,
            date_maj TEXT
        )
    """)
    # Remboursements et annulations déjà traités. Même patron que le journal de lignes : la clé
    # primaire EST la garantie d'idempotence. `cle` est préfixée ('refund:' / 'annulation:')
    # parce qu'un id de remboursement et un id de commande sont deux numérotations distinctes.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Shopify_Remboursements (
            cle TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            refund_id TEXT,
            tickets TEXT,
            montant DECIMAL,
            date_traitement TEXT NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_shopify_remboursements_order ON Shopify_Remboursements(order_id)"
    )
    # Commandes en ligne importées HORS caisse : ni ticket, ni écriture au journal de caisse, ni Z.
    # `order_id` est la clé d'idempotence ; les lignes disent ce qui a été retiré de quel stock, pour
    # qu'un remboursement ne puisse rendre que ce qui a réellement été décompté. Les colonnes
    # s'appellent `stock_id` / `produit_id` (et non `id_stock` / `id_produit`) : voir `stock_journal`.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Shopify_Commandes (
            order_id TEXT PRIMARY KEY,
            order_number TEXT,
            date_import TEXT NOT NULL,
            total TEXT,
            nb_lignes INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Shopify_Commandes_Lignes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            stock_id INTEGER NOT NULL,
            produit_id INTEGER,
            quantite INTEGER NOT NULL,
            restituee INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_shopify_commandes_lignes_order ON Shopify_Commandes_Lignes(order_id)"
    )
    # File d'attente locale des mouvements de stock (voir FILE_PENDING) : définition unique dans
    # `kodo_core.db.shopify_file` (la migration 2.1.2 y ajoute `prochaine_tentative`).
    _assurer_table_file(cursor)
    # Lignes de stock saisies à la main, à aligner sur la boutique en ligne (voir
    # `kodo_core.db.shopify_alignement` ; créée aussi à la demande, sans migration).
    shopify_alignement.assurer_table(cursor)


# --- Point de départ de la poussée des ventes ------------------------------------------------
#
# Tout ticket naît avec `synced_shopify = 0`. Sans point de départ, la première passe poussait
# TOUT l'historique de la caisse vers une boutique dont le stock (importé du CSV de Kōdo, ou copié
# depuis Shopify) en tenait déjà compte : chaque vente passée était décomptée une seconde fois.
# PARAM_SEUIL_TICKETS ("shopify_seuil_ticket") : importé de `kodo_core.db.shopify_file`.
# On ne pousse que les tickets d'id > seuil.
PARAM_ALIGNEMENT_TICKETS = "shopify_ticket_alignement"  # dernier ticket au dernier alignement des stocks


def noter_alignement_stock(conn):
    """
    Retient le dernier ticket au moment où les deux stocks sont alignés : export du CSV Shopify
    (le fichier fige le stock de Kōdo) ou import du catalogue Shopify (Kōdo copie le stock en ligne).
    Le commit revient à l'appelant.
    """
    c = conn.cursor()
    c.execute("SELECT COALESCE(MAX(id), 0) FROM Tickets")
    dernier = int(c.fetchone()[0] or 0)
    c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
              (PARAM_ALIGNEMENT_TICKETS, str(dernier)))


def _calculer_seuil_a_fixer(c) -> int:
    """
    Le premier ticket à pousser, quand aucun seuil n'est encore enregistré (lecture seule).

    Règle unique, partagée par `fixer_seuil_tickets` (qui l'enregistre) et par
    `ventes_non_envoyees` (qui s'en sert avant que la première passe ne l'ait fait).
    """
    c.execute("SELECT 1 FROM Shopify_Sync_Lignes LIMIT 1")
    if c.fetchone() is not None:
        return 0
    c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (PARAM_ALIGNEMENT_TICKETS,))
    alignement = c.fetchone()
    if alignement is not None and str(alignement[0]).strip().isdigit():
        return int(alignement[0])
    c.execute("SELECT COALESCE(MAX(id), 0) FROM Tickets")
    return int(c.fetchone()[0] or 0)


def _seuil_tickets_effectif(c) -> int:
    """Le seuil de poussée en vigueur : celui qui est enregistré, sinon celui qui le serait."""
    c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (PARAM_SEUIL_TICKETS,))
    ligne = c.fetchone()
    if ligne is not None:
        try:
            return int(ligne[0])
        except (TypeError, ValueError):
            return 0
    return _calculer_seuil_a_fixer(c)


def ventes_non_envoyees(c) -> dict:
    """
    Lignes de vente que la synchronisation poussera ENCORE vers Shopify (lecture seule).

    C'est exactement la sélection de `ShopifySync.sync_tickets_to_shopify` : tickets non marqués
    synchronisés, postérieurs au seuil, dont la ligne n'a aucune entrée au journal ligne à
    ligne. Ces ventes sont déjà décomptées en caisse mais PAS en ligne : tant qu'elles n'ont pas
    été envoyées, le stock de la boutique en ligne est plus haut que celui de la caisse, et le
    recopier par-dessus (import du catalogue) ferait réapparaître des articles déjà vendus.
    Les tables de synchronisation doivent exister (`_assurer_tables_sync`).
    """
    seuil = _seuil_tickets_effectif(c)
    c.execute("""
        SELECT COUNT(*), COUNT(DISTINCT t.id), MIN(t.id), MAX(t.id)
        FROM Tickets t
        JOIN Ventes_Details vd ON vd.id_ticket = t.id
        JOIN Stocks s ON s.id = vd.id_stock
        JOIN Produits p ON p.id = s.id_produit
        LEFT JOIN Shopify_Sync_Lignes j ON j.id_vente_detail = vd.id
        WHERE t.synced_shopify = 0 AND t.id > ? AND j.id_vente_detail IS NULL
    """, (seuil,))
    lignes, tickets, premier, dernier = c.fetchone()
    bilan = {"lignes": int(lignes or 0), "tickets": int(tickets or 0), "premier": None, "dernier": None}
    if bilan["lignes"]:
        for cle, ident in (("premier", premier), ("dernier", dernier)):
            c.execute("SELECT numero_ticket FROM Tickets WHERE id = ?", (ident,))
            ligne = c.fetchone()
            bilan[cle] = ligne[0] if ligne and ligne[0] else str(ident)
    return bilan


def _signature_activite(c) -> tuple:
    """
    Empreinte de l'activité qui rend un catalogue téléchargé périmé : dernier ticket créé, lignes
    inscrites au journal de synchronisation, dont celles confirmées par Shopify.
    Comparée avant le téléchargement puis dans la transaction d'écriture de l'import.
    """
    c.execute("SELECT COALESCE(MAX(id), 0) FROM Tickets")
    dernier_ticket = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*), COALESCE(SUM(statut = ?), 0) FROM Shopify_Sync_Lignes", (STATUT_POUSSE,))
    lignes, poussees = c.fetchone()
    # Une quantité saisie à la main pendant le téléchargement, ou une ligne alignée pendant celui-ci
    # (le site a bougé), rend le catalogue reçu périmé au même titre qu'une vente.
    try:
        c.execute(f"SELECT COUNT(*), COALESCE(SUM(version), 0), COALESCE(SUM(statut = ?), 0) "
                  f"FROM {shopify_alignement.TABLE}", (shopify_alignement.STATUT_ALIGNE,))
        n_align, v_align, f_align = c.fetchone()
    except Exception:
        n_align = v_align = f_align = 0
    return (dernier_ticket, int(lignes or 0), int(poussees or 0),
            int(n_align or 0), int(v_align or 0), int(f_align or 0))


class ImportShopifyBloque(ValueError):
    """
    L'import du catalogue est refusé par sécurité, AVANT toute modification.

    `raison` : "ventes_non_envoyees" (des ventes n'ont pas encore été envoyées à Shopify) ou
    "activite_pendant_import" (une vente ou une poussée a eu lieu pendant le téléchargement).
    Le message est destiné à être affiché tel quel à la commerçante.
    """

    def __init__(self, message: str, raison: str, bilan: dict = None):
        super().__init__(message)
        self.raison = raison
        self.bilan = bilan or {}


def fixer_seuil_tickets(conn=None) -> int:
    """
    Fixe, une fois pour toutes, le premier ticket à pousser vers Shopify. Retourne le seuil.

    - Déjà fixé : on n'y touche plus (un redémarrage ne doit pas sauter les ventes en attente).
    - La synchro a déjà poussé des lignes (installation antérieure) : seuil 0, rien ne change.
    - Sinon : le dernier ticket au dernier export/import du catalogue, à défaut le dernier ticket
      existant. Les ventes faites entre l'export et l'activation restent donc poussées.
    """
    propre = conn is None
    if propre:
        conn = get_connection()
    try:
        c = _ouvrir_ecriture(conn)
        _assurer_tables_sync(c)
        c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (PARAM_SEUIL_TICKETS,))
        ligne = c.fetchone()
        if ligne is not None:
            conn.rollback()
            try:
                return int(ligne[0])
            except (TypeError, ValueError):
                return 0
        seuil = _calculer_seuil_a_fixer(c)
        c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                  (PARAM_SEUIL_TICKETS, str(seuil)))
        conn.commit()
        logger.info(f"Synchro Shopify : seules les ventes après le ticket n°{seuil} (id) seront poussées.")
        return seuil
    finally:
        if propre:
            conn.close()


class ShopifySync:
    """Moteur principal de synchronisation REST & GraphQL pour Shopify."""

    PAGE_SIZE = 250
    MAX_PAGES = 200  # garde-fou : 50 000 produits

    # `sku` et `barcode` sont demandés pour VÉRIFIER la réponse : la recherche Shopify n'est pas
    # une égalité stricte et peut renvoyer une variante voisine (une autre taille du même produit).
    REQUETE_VARIANTE = """
    query($query: String!) {
      productVariants(first: 10, query: $query) {
        edges {
          node {
            sku
            barcode
            inventoryItem {
              id
            }
          }
        }
      }
    }
    """

    def __init__(self, store_url: str = "", access_token: str = "", api_version: str = None,
                 client_id: str = "", client_secret: str = ""):
        self.store_url = store_url.strip()
        self.access_token = access_token.strip()
        # Version imposée par l'appelant, sinon celle des réglages, sinon la version par défaut.
        self._version_imposee = bool(api_version)
        self.api_version = api_version or VERSION_API_DEFAUT
        # Application récente : identifiant client + secret, échangés contre un jeton de 24 h.
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        # Échéance (secondes epoch) du jeton géré par l'application ; 0 = inconnue.
        self.jeton_expire_a = 0.0
        # Version d'API que Shopify a réellement servie (en-tête de réponse), '' si jamais lue.
        self.version_api_servie = ""
        # Jeton émis par la dernière requête, et vrai si Shopify l'a refusé par un 401.
        self._jeton_envoye = ""
        self._refus_401 = False
        self._location_id = None
        # Dépôt d'inventaire choisi dans les réglages (vide = « laisse la caisse décider »).
        self.location_id_configure = ""
        # Dernier avertissement de dépôt écrit en base : évite de réécrire la même phrase à
        # chaque passe quand rien ne change. `object()` force l'écriture au premier passage,
        # même si la phrase à écrire est vide (cas du dépôt redevenu valide après redémarrage).
        self._dernier_avertissement_depot = object()
        # Interrupteurs de l'écran Paramètres : allumés par défaut, comme les affiche `/api/settings`.
        self.auto_sync = True
        self.sync_orders = True
        # Cache SKU → inventory_item_id : sans lui, chaque ligne de chaque ticket déclenchait un
        # appel GraphQL à chaque passe, et les plafonds de débit Shopify étaient atteints en boutique.
        self._cache_inventaire = {}
        self._rest_scan_fait = False
        # Balayage REST en échec dans la passe en cours : (nature, cause), ou None s'il n'a pas échoué.
        self._rest_scan_echec = None
        # Nature du dernier échec définitif : None, "http" (le serveur a répondu, rien n'a été
        # appliqué) ou "reseau" (aucune réponse reçue, on ne sait pas si l'appel a porté).
        self.dernier_echec = None
        # Sa CAUSE, en une ligne lisible (« 401 : Invalid API key… », « SSLError : … »). Sans elle
        # une synchro qui échoue en boucle ne disait que « http » : impossible de distinguer un
        # jeton révoqué, une portée manquante, une boutique suspendue ou un plafond de débit.
        self.dernier_echec_detail = ""
        # Erreur GraphQL de la dernière recherche de variante (réponse 200 portant `errors`).
        self._detail_graphql = ""
        # Bilan du dernier `import_catalog` (lu par la route : variantes, quantités modifiées…).
        self.dernier_import = {}
        # Session HTTP persistante (keep-alive) : évite une négociation TLS par requête.
        self._session = None
        self._verrou_session = threading.Lock()
        # Identifiants imposés par l'appelant : uniquement quand les DEUX sont fournis. Un jeton
        # passé en argument avec une URL vide donnait un état bâtard (moitié appelant, moitié
        # base) ; dans ce cas la base fait autorité sur les deux champs.
        self._config_injectee = bool(
            self.store_url and (self.access_token or (self.client_id and self.client_secret)))
        if not self._config_injectee:
            self.load_config()

    def load_config(self):
        """
        Recharge les réglages depuis la base locale : c'est la SEULE source de la boutique cible.

        Aucune URL ni aucun jeton n'est codé en dur nulle part : Kōdo POS est vendu à des
        boutiques, chacune saisit les siens dans l'écran Paramètres (`POST /api/settings`,
        clés `shopify_store_url` / `shopify_access_token`).

        L'écrasement est INCONDITIONNEL, y compris par une valeur vide : avant, effacer le jeton
        dans les réglages laissait l'ancien actif en mémoire jusqu'au redémarrage, et la caisse
        continuait de pousser ses ventes vers une boutique que la commerçante croyait débranchée.
        """
        reglages = lire_reglages_shopify()
        self.auto_sync = reglages["auto_sync"]
        self.sync_orders = reglages["sync_orders"]
        # Le dépôt est relu même quand les identifiants sont imposés par l'appelant : il désigne
        # une étagère dans la boutique, pas la boutique elle-même.
        ancien_depot, ancienne_boutique = self.location_id_configure, self.store_url
        self.location_id_configure = reglages["location_id"]
        if not self._version_imposee:
            self.api_version = reglages["api_version"] or VERSION_API_DEFAUT
        if not self._config_injectee:
            self.store_url = reglages["store_url"]
            self.access_token = reglages["access_token"]
            self.client_id = reglages["client_id"]
            self.client_secret = reglages["client_secret"]
            self.jeton_expire_a = reglages["token_expire_a"]
        if self.location_id_configure != ancien_depot or self.store_url != ancienne_boutique:
            # `_location_id` est un identifiant PROPRE à une boutique. Le thread de synchro vit
            # des heures et relit ces réglages à chaque passe : sans cet oubli, changer de dépôt
            # (ou de boutique) n'avait d'effet qu'au redémarrage de la caisse, et les ventes
            # continuaient d'être retirées de l'ancien dépôt — voire d'un dépôt qui appartient
            # à une autre boutique, où l'identifiant ne veut plus rien dire.
            self._location_id = None

    def domaine(self) -> str:
        """Domaine normalisé de la boutique configurée (voir `normaliser_domaine_boutique`)."""
        return normaliser_domaine_boutique(self.store_url)

    def est_configure(self) -> bool:
        """
        Vrai si le domaine de boutique EST exploitable ET qu'il y a de quoi s'authentifier : un
        jeton saisi, ou l'identifiant client + le secret (le jeton est alors obtenu à la demande).
        """
        return bool((self.access_token or self._a_des_identifiants_app())
                    and domaine_boutique_valide(self.domaine()))

    def make_request(self, endpoint: str, method: str = "GET", data: dict = None,
                     max_retries: int = 3, rejouable: bool = True):
        """
        Requête vers Shopify, avec jeton géré : renouvelé AVANT son échéance, et UNE fois si
        Shopify le refuse (401) malgré tout (révoqué, ou expiré plus tôt qu'annoncé).

        Rejouer après un 401 est sans danger, y compris pour l'ajustement relatif d'inventaire :
        un 401 est un refus d'authentification, Shopify n'a RIEN appliqué. Ce n'est pas le cas
        d'une coupure réseau, qui reste réglée dans `_requete_shopify`.
        """
        if self.est_configure() and self._jeton_a_renouveler():
            if not self.renouveler_jeton():
                return None
        self._refus_401 = False
        resultat = self._requete_shopify(endpoint, method, data, max_retries, rejouable)
        if resultat is None and self._refus_401 and self._a_des_identifiants_app():
            refuse = self._jeton_envoye
            self._refus_401 = False
            if self.renouveler_jeton(jeton_refuse=refuse):
                resultat = self._requete_shopify(endpoint, method, data, max_retries, rejouable)
        return resultat

    def _requete_shopify(self, endpoint: str, method: str = "GET", data: dict = None,
                         max_retries: int = 3, rejouable: bool = True):
        """
        Exécute une requête HTTP REST ou GraphQL vers Shopify avec retry exponentiel (429 Rate Limit).

        Mémorise la nature de l'échec dans `self.dernier_echec` : un 4xx/5xx signifie que Shopify a
        répondu et n'a rien appliqué (on peut retenter sans risque), une coupure réseau signifie
        qu'on ignore si l'appel a porté (retenter pourrait décrémenter deux fois le stock).

        `rejouable` dit si CET appel peut être rejoué après une coupure. Il vaut faux pour le
        seul appel non idempotent du module, l'ajustement RELATIF d'inventaire : Shopify peut
        l'avoir appliqué et la réponse s'être perdue (coupure Wi-Fi de boutique, délai de 12 s),
        auquel cas le rejouer décrémente une seconde fois. Tout le mécanisme de réservation
        `EN_VOL` → `INDETERMINE` repose sur cette promesse ; sans elle, il ne protège que du
        crash du poste, pas du rejeu interne. Le 429 garde son attente : Shopify garantit alors
        n'avoir rien appliqué.
        """
        if not self.est_configure():
            logger.warning("Configuration Shopify manquante ou invalide (domaine ou jeton).")
            self._noter_echec("config", "domaine ou jeton Shopify manquant ou invalide")
            return None

        base_url = self.domaine()
        # HTTP n'est toléré que vers la machine elle-même (serveur d'essai des tests
        # d'intégration). Vers un vrai domaine, la requête part TOUJOURS en HTTPS vérifié :
        # le jeton d'administration ne doit jamais circuler en clair.
        protocole = "https"
        if _est_boucle_locale(base_url) and str(self.store_url).strip().lower().startswith("http://"):
            protocole = "http"
        url = f"{protocole}://{base_url}/admin/api/{self.api_version}/{endpoint}"
        # Jeton réellement émis par CET appel : c'est lui qu'un 401 désigne comme refusé.
        self._jeton_envoye = self.access_token
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
            "User-Agent": "KodoPOS-SyncEngine/1.0"
        }
        ssl_ctx = get_ssl_context()

        for attempt in range(1, max_retries + 1):
            req = urllib.request.Request(url, headers=headers, method=method)
            if data:
                req.data = json.dumps(data).encode("utf-8")

            t0 = time.perf_counter()
            try:
                if self._transport_persistant():
                    resultat = self._requete_persistante(method, url, headers, data, ssl_ctx)
                    self._effacer_echec()
                    self._tracer_requete(method, endpoint, 200, t0)
                    return resultat
                with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as response:
                    self._noter_version_servie(getattr(response, "headers", None))
                    self._effacer_echec()
                    resultat = json.loads(response.read().decode("utf-8"))
                    self._tracer_requete(method, endpoint, getattr(response, "status", 200), t0)
                    return resultat
            except urllib.error.HTTPError as e:
                self._tracer_requete(method, endpoint, e.code, t0)
                if e.code == 429:
                    # Shopify annonce le délai en décimal (« 2.0 ») : `int()` levait ici, hors
                    # de toute reprise, et l'ajustement finissait classé coupure réseau.
                    try:
                        retry_after = min(max(float((e.headers or {}).get("Retry-After", 2)), 0.0), 60.0)
                    except (TypeError, ValueError):
                        retry_after = 2.0
                    logger.warning(f"Rate limited (429). Attente de {retry_after}s (essai {attempt}/{max_retries})...")
                    if attempt == max_retries:
                        # Essais épuisés sur un refus de débit : Shopify n'a rien appliqué. C'est
                        # un échec « http » (à retenter), pas une coupure au résultat inconnu.
                        self._noter_echec("http", "429 : plafond de débit Shopify atteint, nouvel essai plus tard")
                        return None
                    time.sleep(retry_after)
                    continue
                else:
                    try:
                        err_detail = e.read().decode("utf-8", errors="ignore")
                    except Exception:
                        err_detail = str(e)
                    logger.error(f"Erreur HTTP {e.code} sur {method} {endpoint}: {err_detail}")
                    self._refus_401 = (e.code == 401)
                    # Code HTTP de la dernière réponse en erreur : l'envoi d'une VENTE s'en sert pour distinguer
                    # un refus net (4xx, 500, 503) d'une issue inconnue (502/504, voir `sync_tickets_to_shopify`).
                    self._dernier_code_http = e.code
                    self._noter_echec("http", self._resumer_erreur_http(e.code, err_detail))
                    return None
            except Exception as e:
                self._tracer_requete(method, endpoint, "ERR", t0)
                # Connexion peut-être périmée : la prochaine requête repart d'une session neuve.
                self._reinitialiser_session()
                logger.error(f"Erreur réseau/API sur {method} {endpoint} (essai {attempt}/{max_retries}): {e}")
                if rejouable and attempt < max_retries:
                    time.sleep(1.5 * attempt)
                else:
                    self._noter_echec("reseau", self._resumer_exception(e))
                    return None
        self._noter_echec("reseau", "aucune réponse de Shopify")
        return None

    # --- Transport : session HTTP persistante --------------------------------------------------

    @staticmethod
    def _transport_persistant() -> bool:
        return urllib.request.urlopen is _URLOPEN_ORIGINE

    def _obtenir_session(self):
        with self._verrou_session:
            if self._session is None:
                import requests
                from requests.adapters import HTTPAdapter
                from urllib3.util.retry import Retry

                contexte = get_ssl_context()

                class _Adaptateur(HTTPAdapter):
                    # Même contexte TLS vérifié que l'ancien transport (certifi embarqué).
                    def init_poolmanager(self, *args, **kwargs):
                        kwargs["ssl_context"] = contexte
                        super().init_poolmanager(*args, **kwargs)

                # Seuls les échecs de CONNEXION sont rejoués par la couche HTTP (rien n'a été
                # envoyé, donc sans risque pour un ajustement relatif) ; jamais une lecture/réponse.
                reprise = Retry(total=None, connect=2, read=0, status=0, other=0, redirect=0,
                                allowed_methods=None, backoff_factor=0.2)
                session = requests.Session()
                adaptateur = _Adaptateur(pool_connections=2, pool_maxsize=4, max_retries=reprise)
                session.mount("https://", adaptateur)
                session.mount("http://", adaptateur)
                self._session = session
            return self._session

    def _reinitialiser_session(self):
        with self._verrou_session:
            session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def _requete_persistante(self, method, url, headers, data, ssl_ctx):
        """
        Même contrat que le transport urllib : renvoie le JSON, lève `urllib.error.HTTPError`
        pour un statut >= 400 (les branches 429 / erreur HTTP de `make_request` s'appliquent
        donc telles quelles) et toute autre exception pour une coupure.
        """
        corps = json.dumps(data).encode("utf-8") if data else None
        reponse = self._obtenir_session().request(method, url, headers=headers, data=corps, timeout=12)
        self._noter_version_servie(reponse.headers)
        if reponse.status_code >= 400:
            raise urllib.error.HTTPError(url, reponse.status_code, reponse.reason or "",
                                         reponse.headers, io.BytesIO(reponse.content or b""))
        return json.loads(reponse.content.decode("utf-8"))

    # --- Jeton géré par l'application (client credentials) ------------------------------------

    def _a_des_identifiants_app(self) -> bool:
        """Vrai si l'application récente a donné un identifiant client ET un secret."""
        return bool(self.client_id and self.client_secret)

    def _jeton_a_renouveler(self) -> bool:
        """Vrai si le jeton géré par l'application manque, est d'âge inconnu ou arrive à échéance."""
        if not self._a_des_identifiants_app():
            return False
        if not self.access_token or not self.jeton_expire_a:
            return True
        return time.time() >= self.jeton_expire_a - MARGE_RENOUVELLEMENT_JETON_S

    def _noter_version_servie(self, entetes) -> None:
        """Retient la version d'API que Shopify a réellement servie (en-tête de réponse)."""
        try:
            valeur = entetes.get("X-Shopify-API-Version") if entetes is not None else None
        except Exception:
            valeur = None
        if valeur:
            self.version_api_servie = str(valeur).strip()

    @staticmethod
    def _resumer_refus_jeton(code, corps) -> str:
        """Le refus de l'échange identifiant + secret, en une ligne lisible."""
        erreur = description = ""
        try:
            donnees = json.loads(str(corps or "").strip())
        except (ValueError, TypeError):
            donnees = None
        if isinstance(donnees, dict):
            erreur = str(donnees.get("error") or donnees.get("errors") or "").strip()
            description = str(donnees.get("error_description") or "").strip()
        explications = {
            "invalid_client": "identifiant client ou secret refusé par Shopify",
            "shop_not_permitted": ("l'application n'est pas dans la même organisation Shopify "
                                   "que la boutique"),
            "invalid_request": "demande refusée par Shopify",
            "unsupported_grant_type": "type d'autorisation non accepté par Shopify",
            "access_denied": "accès refusé par Shopify",
        }
        phrase = explications.get(erreur) or description or erreur or "refus sans précision"
        phrase = " ".join(str(phrase).split())[:120]
        return f"{code} : {phrase} [{erreur}]" if erreur else f"{code} : {phrase}"

    def _memoriser_jeton(self, jeton: str, expire_a: float) -> None:
        """Écrit le jeton et son échéance dans `Parametres` (l'autre moteur les relit à chaque passe)."""
        try:
            conn = get_connection()
            try:
                c = _ouvrir_ecriture(conn)
                c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (CLE_TOKEN, jeton))
                c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                          (CLE_JETON_EXPIRE, str(int(expire_a))))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            # Le jeton reste valable en mémoire pour ce moteur ; un redémarrage en redemandera un.
            logger.error(f"Jeton Shopify renouvelé mais non enregistré : {e}")

    def renouveler_jeton(self, jeton_refuse: str = "") -> bool:
        """
        Échange l'identifiant client + le secret contre un jeton neuf (client credentials grant).

        - Le secret n'est envoyé qu'à un domaine `*.myshopify.com` (ou à la boucle locale des
          tests), en HTTPS vérifié : jamais vers un domaine qu'un appelant aurait choisi.
        - Sous verrou : si un autre moteur vient de renouveler (la base fait foi), son jeton est
          ADOPTÉ au lieu d'en demander un second. `jeton_refuse` est celui que Shopify vient de
          rejeter : il ne peut pas être « adopté ».
        - Un échec laisse le jeton précédent en place et note sa cause lisible (jamais le secret).
        """
        if not self._a_des_identifiants_app():
            self._noter_echec("config", "identifiant client ou secret Shopify manquant")
            return False
        domaine = self.domaine()
        if not domaine_boutique_valide(domaine):
            self._noter_echec("config", f"domaine de boutique invalide ({self.store_url!r}) : "
                                        "le secret n'est pas envoyé")
            return False

        with _VERROU_JETON:
            if not self._config_injectee:
                autres = lire_reglages_shopify()
                neuf = autres["access_token"]
                if (neuf and neuf != jeton_refuse
                        and autres["client_id"] == self.client_id
                        and autres["client_secret"] == self.client_secret
                        and autres["token_expire_a"] - MARGE_RENOUVELLEMENT_JETON_S > time.time()):
                    self.access_token = neuf
                    self.jeton_expire_a = autres["token_expire_a"]
                    return True

            # HTTP n'est toléré que vers la machine elle-même (serveur d'essai des tests).
            protocole = "https"
            if _est_boucle_locale(domaine) and str(self.store_url).strip().lower().startswith("http://"):
                protocole = "http"
            url = f"{protocole}://{domaine}/admin/oauth/access_token"
            corps = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=corps, method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "KodoPOS-SyncEngine/1.0",
            })
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as reponse:
                    charge = json.loads(reponse.read().decode("utf-8"))
                    self._tracer_requete("POST", "oauth/access_token", getattr(reponse, "status", 200), t0)
            except urllib.error.HTTPError as e:
                self._tracer_requete("POST", "oauth/access_token", e.code, t0)
                try:
                    corps_err = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    corps_err = str(e)
                self._refus_401 = False
                self._noter_echec("http", "renouvellement du jeton refusé — "
                                  + self._resumer_refus_jeton(e.code, corps_err))
                return False
            except Exception as e:
                self._tracer_requete("POST", "oauth/access_token", "ERR", t0)
                self._noter_echec("reseau", "renouvellement du jeton impossible — "
                                  + self._resumer_exception(e))
                return False

            jeton = str(charge.get("access_token") or "").strip() if isinstance(charge, dict) else ""
            if not jeton:
                self._noter_echec("http", "renouvellement du jeton : réponse de Shopify sans jeton d'accès")
                return False
            try:
                duree = int(charge.get("expires_in") or 0)
            except (TypeError, ValueError):
                duree = 0
            if duree <= 0:
                duree = 3600  # durée inconnue : renouvellement dans l'heure, par prudence
            self.access_token = jeton
            self.jeton_expire_a = time.time() + duree
            self._effacer_echec()
            if not self._config_injectee:
                self._memoriser_jeton(jeton, self.jeton_expire_a)
            logger.info("Jeton Shopify renouvelé (valable %d min).", duree // 60)
            return True

    def _effacer_echec(self):
        """Remet à zéro la nature ET la cause du dernier échec (début d'une opération, ou succès)."""
        self.dernier_echec = None
        self.dernier_echec_detail = ""

    def _noter_echec(self, nature, detail=""):
        """
        Retient la nature de l'échec et sa cause. Le jeton n'est jamais conservé : Shopify ne le
        renvoie pas, mais une cause remonte jusqu'à l'écran et à la base.
        """
        self.dernier_echec = nature
        texte = " ".join(str(detail or "").split())
        for secret in (getattr(self, "access_token", ""), getattr(self, "client_secret", "")):
            if secret:
                texte = texte.replace(secret, "***")
        self.dernier_echec_detail = texte[:200]

    def _suffixe_echec(self) -> str:
        """« — cause » à accoler au message d'une ligne en échec ('' si aucune cause connue)."""
        detail = getattr(self, "dernier_echec_detail", "") or ""
        return f" — {detail}" if detail else ""

    @staticmethod
    def _resumer_erreur_http(code, corps) -> str:
        """Une réponse d'erreur Shopify réduite à « 401 : message » (JSON, HTML ou texte brut)."""
        texte = str(corps or "").strip()
        message = ""
        try:
            donnees = json.loads(texte)
        except (ValueError, TypeError):
            donnees = None
        if isinstance(donnees, dict):
            erreur = donnees.get("errors", donnees.get("error"))
            if isinstance(erreur, str):
                message = erreur
            elif isinstance(erreur, dict):
                message = "; ".join(f"{k} {v}" for k, v in erreur.items())
            elif isinstance(erreur, list):
                message = "; ".join(str(e) for e in erreur)
        elif texte.startswith("<"):
            titre = re.search(r"<title>(.*?)</title>", texte, re.IGNORECASE | re.DOTALL)
            message = titre.group(1) if titre else "réponse HTML"
        elif texte:
            message = texte
        message = " ".join(message.split())[:140]
        return f"{code} : {message}" if message else str(code)

    @staticmethod
    def _resumer_exception(exc) -> str:
        return f"{type(exc).__name__} : {exc}"[:200]

    @staticmethod
    def _resumer_erreurs_graphql(erreurs) -> str:
        """Les erreurs d'une réponse GraphQL (statut 200) : « ACCESS_DENIED Access denied for … »."""
        morceaux = []
        for erreur in (erreurs if isinstance(erreurs, list) else [erreurs])[:2]:
            if isinstance(erreur, dict):
                extensions = erreur.get("extensions") if isinstance(erreur.get("extensions"), dict) else {}
                morceaux.append(" ".join(str(x) for x in (extensions.get("code"), erreur.get("message")) if x))
            else:
                morceaux.append(str(erreur))
        return " ".join("; ".join(morceaux).split())[:160]

    def _tracer_requete(self, method, endpoint, statut, t0):
        """Journal discret : méthode, point d'accès, statut et durée en ms (jamais d'en-tête)."""
        try:
            ms = int((time.perf_counter() - t0) * 1000)
            logger.info(f"[HTTP] {method} {str(endpoint).split('?')[0]} -> {statut} en {ms} ms")
        except Exception:
            pass

    def execute_graphql(self, query: str, variables: dict = None):
        """Exécute une requête GraphQL vers l'API Admin Shopify."""
        data = {"query": query}
        if variables:
            data["variables"] = variables
        return self.make_request("graphql.json", method="POST", data=data)

    def tester_connexion(self) -> dict:
        """
        Éprouve la connexion à la boutique configurée et retourne un compte rendu prêt à afficher.

        C'est le MÊME moteur, la même normalisation de domaine et le même transport TLS que la
        synchronisation réelle : la route `/api/shopify/test` réimplémentait sa propre requête,
        si bien qu'un « Connexion réussie ! » pouvait s'afficher alors que la synchro, elle,
        n'arrivait nulle part (ou l'inverse).
        """
        domaine = self.domaine()
        if not (self.access_token or self._a_des_identifiants_app()):
            return {"success": False, "error": "Jeton d'accès Shopify requis pour le test."}
        if not domaine_boutique_valide(domaine):
            return {"success": False, "error": f"Domaine de boutique invalide : {self.store_url!r}."}

        data = self.make_request("locations.json")
        if data is None:
            return {
                "success": False,
                "domain": domaine,
                "error": (f"Aucune réponse exploitable de {domaine} (domaine ou jeton refusé)"
                          f"{self._suffixe_echec()}."),
            }
        bruts = data.get("locations") or []
        locations = [l.get("name", "Dépôt") for l in bruts]
        # `locations` ne porte que des noms : de quoi écrire une phrase, pas de quoi choisir.
        # Le choix du dépôt a besoin de l'identifiant, et l'écran a besoin de savoir lesquels
        # sont actifs (un dépôt désactivé ne peut pas recevoir d'ajustement de stock).
        depots = [{"id": str(l.get("id")), "nom": l.get("name") or "Dépôt",
                   "actif": bool(l.get("active", True))}
                  for l in bruts if l.get("id") is not None]
        return {
            "success": True,
            "domain": domaine,
            "message": (f"Connexion Shopify réussie sur {domaine} ! Dépôts : {', '.join(locations)}"
                        if locations else f"Connexion établie avec {domaine}."),
            "locations": locations,
            "depots": depots,
        }

    @staticmethod
    def _cle_de_tri_depot(depot: dict):
        """Ordre stable des dépôts : par identifiant numérique croissant, le plus ancien d'abord."""
        brut = str(depot.get("id"))
        return (0, int(brut), "") if brut.isdigit() else (1, 0, brut)

    def _avertir_depot(self, message: str):
        """
        Dépose (ou efface) l'avertissement de dépôt lu par `GET /api/shopify/status`.

        Un `logger.warning` reste dans un fichier que la commerçante n'ouvrira jamais. Le choix
        du dépôt décide de l'étagère d'où le stock est retiré : quand il est douteux, il faut
        que ça se voie à l'écran, pas dans un journal.
        """
        if message == self._dernier_avertissement_depot:
            return
        try:
            conn = get_connection()
            try:
                c = _ouvrir_ecriture(conn)
                c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                          (CLE_DEPOT_AVERTISSEMENT, str(message or "")[:500]))
                conn.commit()
            finally:
                conn.close()
            self._dernier_avertissement_depot = message
        except Exception as e:
            logger.error(f"Avertissement de dépôt Shopify non enregistré : {e}")

    def get_location_id(self) -> str:
        """
        Le dépôt d'inventaire visé dans la boutique, mis en cache pour la durée de la passe.

        Une boutique Shopify peut compter plusieurs dépôts et `available_adjustment` s'applique
        à UN dépôt nommé. Le code d'origine prenait le premier dépôt actif renvoyé par l'API,
        dans un ordre que Shopify ne garantit nulle part : sur une boutique à plusieurs dépôts,
        une vente pouvait être retirée de la réserve au lieu du magasin — et le choix pouvait
        même changer d'une passe à l'autre, éparpillant le stock sans qu'aucune erreur
        n'apparaisse. Les quatre cas sont donc traités séparément :

        1. **Dépôt choisi dans les réglages** : il est utilisé, et LUI SEUL. S'il a disparu ou
           s'il a été désactivé côté Shopify, la synchronisation s'arrête et le dit. Se rabattre
           sur un autre dépôt serait exactement le défaut d'origine, en pire : la commerçante
           aurait fait un choix explicite que la caisse contredirait en silence.
        2. **Aucun choix, un seul dépôt actif** : aucune ambiguïté, on prend celui-là.
        3. **Aucun choix, plusieurs dépôts actifs** : on ne peut pas deviner, mais refuser
           arrêterait net une boutique qui fonctionne aujourd'hui. On retient donc le dépôt le
           plus ancien (identifiant le plus petit — celui créé avec la boutique, le magasin dans
           l'immense majorité des cas), ce qui est au moins DÉTERMINISTE, et on affiche à
           l'écran quel dépôt est utilisé pour que le choix puisse être fait.
        4. **Aucun dépôt actif** : rien à ajuster, et il faut le dire.
        """
        if self._location_id:
            return self._location_id

        data = self.make_request("locations.json")
        if not data or not isinstance(data.get("locations"), list):
            return None

        actifs = [l for l in data["locations"]
                  if l.get("active", True) and l.get("id") is not None]
        if not actifs:
            self._avertir_depot(
                "Aucun dépôt actif sur la boutique Shopify : le stock en ligne ne peut pas être "
                "ajusté. Réactivez un emplacement dans Shopify (Paramètres → Emplacements).")
            return None

        choisi = str(self.location_id_configure or "").strip()
        if choisi:
            for depot in actifs:
                if str(depot["id"]) == choisi:
                    self._avertir_depot("")
                    self._location_id = depot["id"]
                    return self._location_id
            self._avertir_depot(
                f"Le dépôt Shopify choisi (n° {choisi}) n'existe plus ou a été désactivé. "
                f"La synchronisation du stock est arrêtée pour ne pas retirer les ventes d'un "
                f"autre dépôt. Choisissez-en un autre dans les Réglages. "
                f"Dépôts actifs : {', '.join(str(d.get('name') or d['id']) for d in actifs)}.")
            logger.error(f"Dépôt Shopify configuré ({choisi}) absent des emplacements actifs.")
            return None

        if len(actifs) == 1:
            self._avertir_depot("")
            self._location_id = actifs[0]["id"]
            return self._location_id

        retenu = min(actifs, key=self._cle_de_tri_depot)
        autres = [str(d.get("name") or d["id"]) for d in actifs if d["id"] != retenu["id"]]
        self._avertir_depot(
            f"Cette boutique Shopify a plusieurs dépôts et aucun n'a été choisi : les ventes "
            f"sont retirées de « {retenu.get('name') or retenu['id']} ». Si ce n'est pas le bon, "
            f"choisissez-le dans les Réglages. Autres dépôts : {', '.join(autres)}.")
        logger.warning(
            f"Dépôt Shopify non choisi parmi {len(actifs)} actifs : "
            f"utilisation de {retenu.get('name') or retenu['id']} (n° {retenu['id']}).")
        self._location_id = retenu["id"]
        return self._location_id

    # --- Résolution d'une variante Shopify --------------------------------------------------

    def _charger_cache_rest(self) -> bool:
        """
        Remplit le cache SKU/code-barres → inventory_item_id par balayage REST paginé.

        Le repli REST d'origine lisait `products.json?limit=250` sans pagination : au-delà du
        250e produit, une variante parfaitement existante était déclarée « introuvable sur
        Shopify » et la vente n'était jamais reportée. Il relançait en plus ce balayage pour
        CHAQUE ligne de ticket ; ici il n'a lieu qu'une fois par passe et sert toutes les lignes.
        """
        since_id = 0
        for _ in range(self.MAX_PAGES):
            endpoint = f"products.json?limit={self.PAGE_SIZE}&fields=id,variants"
            if since_id:
                endpoint += f"&since_id={since_id}"
            data = self.make_request(endpoint)
            if not data or "products" not in data:
                return False
            page = data["products"]
            for p in page:
                for v in p.get("variants", []):
                    iid = v.get("inventory_item_id")
                    if not iid:
                        continue
                    for cle in (v.get("sku"), v.get("barcode")):
                        if cle:
                            self._cache_inventaire.setdefault(str(cle), iid)
            last_id = page[-1].get("id") if page else None
            if len(page) < self.PAGE_SIZE or not last_id or last_id == since_id:
                break
            since_id = last_id
        self._rest_scan_fait = True
        return True

    def _resoudre_inventory_item(self, sku: str):
        """
        Retourne `(inventory_item_id | None, echec_reseau: bool)`.

        Distinguer « la variante n'existe pas » de « on n'a pas pu poser la question » n'est pas un
        luxe : confondre les deux faisait marquer un ticket comme synchronisé alors que l'appel
        avait seulement échoué — le stock Shopify n'était jamais décrémenté, et plus jamais retenté.
        """
        if not sku:
            return None, False
        if sku in self._cache_inventaire:
            return self._cache_inventaire[sku], False

        # Le terme de recherche est sérialisé en littéral JSON (guillemets + échappement) avant
        # d'entrer dans la requête Shopify : un SKU contenant une espace, un `:` ou un guillemet
        # (« ROBE "ÉTÉ" 38 ») cassait la syntaxe de recherche ou en détournait le sens.
        # `ensure_ascii=False` : les accents restent des accents. Une séquence « É » n'est
        # pas interprétée par le moteur de recherche Shopify, et « ROBE ÉTÉ » n'y serait
        # jamais retrouvée.
        terme = json.dumps(str(sku), ensure_ascii=False)
        variables = {"query": f"sku:{terme} OR barcode:{terme}"}
        res = self.execute_graphql(self.REQUETE_VARIANTE, variables)
        # Une réponse GraphQL en erreur (portée manquante, plafond de débit) arrive avec un statut
        # 200 : sans cette mémoire, sa cause ne figurait nulle part quand le repli REST échouait
        # lui aussi, et la ligne restait « recherche impossible » sans autre explication.
        self._detail_graphql = (
            "GraphQL : " + self._resumer_erreurs_graphql(res["errors"])
            if isinstance(res, dict) and res.get("errors") else "")

        if res and not res.get("errors") and isinstance(res.get("data"), dict):
            edges = (res["data"].get("productVariants") or {}).get("edges") or []
            par_sku = par_code_barres = None
            for edge in edges:
                node = edge.get("node") or {}
                gid = (node.get("inventoryItem") or {}).get("id", "")
                try:
                    item_id = int(str(gid).split("/")[-1])
                except (TypeError, ValueError):
                    continue
                if "sku" not in node and "barcode" not in node:
                    # Réponse sans les champs de contrôle : rien à vérifier, on la prend telle quelle.
                    par_sku = par_sku or item_id
                    continue
                if str(node.get("sku") or "").strip() == str(sku):
                    par_sku = par_sku or item_id
                elif str(node.get("barcode") or "").strip() == str(sku):
                    par_code_barres = par_code_barres or item_id
            item_id = par_sku or par_code_barres
            if item_id:
                self._cache_inventaire[sku] = item_id
                return item_id, False
            # Réponse valide sans variante EXACTEMENT égale : elle n'existe pas chez Shopify.
            return None, False

        # GraphQL indisponible (jeton sans portée GraphQL, panne) → balayage REST complet, UNE fois
        # par passe. Un balayage en échec n'était pas mémorisé : chaque ligne en attente le
        # relançait (dont le téléchargement de tout le catalogue), soit des dizaines d'appels par
        # passe pendant une panne — de quoi atteindre les plafonds de débit et la prolonger.
        if not self._rest_scan_fait:
            if getattr(self, "_rest_scan_echec", None) is None and not self._charger_cache_rest():
                self._rest_scan_echec = (self.dernier_echec or "reseau",
                                         getattr(self, "dernier_echec_detail", "") or "")
            if getattr(self, "_rest_scan_echec", None):
                nature, cause = self._rest_scan_echec
                self._noter_echec(nature, cause)       # la cause du balayage, pas celle du dernier appel
                # Un balayage interrompu a pu déjà rapporter cette variante : elle est exacte.
                partielle = self._cache_inventaire.get(sku)
                if partielle:
                    return partielle, False
                if self._detail_graphql:
                    reste = getattr(self, "dernier_echec_detail", "") or "échec"
                    self.dernier_echec_detail = f"{self._detail_graphql} ; REST : {reste}"[:240]
                return None, True
        item_id = self._cache_inventaire.get(sku)
        return item_id, False

    def find_inventory_item_id(self, sku: str):
        """ID de l'item d'inventaire Shopify pour un SKU/code-barres, ou None."""
        return self._resoudre_inventory_item(sku)[0]

    _MUTATION_AJUSTEMENT = """
    mutation AjusterInventaire($input: InventoryAdjustQuantitiesInput!, $key: String!) {
      inventoryAdjustQuantities(input: $input) @idempotent(key: $key) {
        userErrors { field message }
        inventoryAdjustmentGroup { createdAt }
      }
    }
    """

    def _ajuster_graphql(self, inventory_item_id, location_id, qty_change, cle):
        """
        Ajustement relatif par mutation GraphQL `@idempotent` (facultative depuis l'API 2026-01,
        obligatoire dès 2026-04 : sans elle, la poussée cesserait de fonctionner à la montée de version).

        Retourne True (appliqué), False (refusé, ou aucune réponse : `dernier_echec` le dit) ou None
        si GraphQL n'a RIEN exécuté (erreur de validation, portée absente, directive inconnue) : seul
        cas où le repli REST est permis. Après une coupure réseau on ne retombe JAMAIS sur REST : la
        mutation a pu être appliquée, REST la rejouerait sans clé et décompterait deux fois. On ne
        s'appuie pas non plus sur la clé pour rejouer une ligne indéterminée : sa rétention et son
        effet sur un doublon ne sont pas documentés (et un doublon aurait été constaté en 2026-04).
        """
        gid_item = (str(inventory_item_id) if str(inventory_item_id).startswith("gid://")
                    else f"gid://shopify/InventoryItem/{inventory_item_id}")
        gid_lieu = (str(location_id) if str(location_id).startswith("gid://")
                    else f"gid://shopify/Location/{location_id}")
        corps = {
            "query": self._MUTATION_AJUSTEMENT,
            "variables": {
                "key": str(cle),
                "input": {"reason": "correction", "name": "available",
                          "changes": [{"inventoryItemId": gid_item, "locationId": gid_lieu,
                                       "delta": int(qty_change)}]},
            },
        }
        res = self.make_request("graphql.json", method="POST", data=corps, rejouable=False)
        if res is None:
            # « reseau » : réponse jamais reçue ; autre (429, 5xx, 4xx) : Shopify a répondu, rien appliqué.
            return False if self.dernier_echec == "reseau" else None
        data = res.get("data") if isinstance(res, dict) else None
        resultat = data.get("inventoryAdjustQuantities") if isinstance(data, dict) else None
        if not isinstance(resultat, dict):
            return None
        erreurs = resultat.get("userErrors") or []
        if erreurs:
            self._noter_echec("http", "GraphQL : " + " ; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in erreurs))
            return False
        self._effacer_echec()
        return True

    def adjust_shopify_stock(self, inventory_item_id: int, location_id: str, qty_change: int,
                             idempotency_key: str = None) -> bool:
        """Ajuste (en relatif) le niveau d'inventaire sur Shopify pour un article donné."""
        # Remise à zéro AVANT le refus d'office : sans appel, aucune cause d'un échec passé ne doit
        # se coller à celui-ci.
        self._effacer_echec()
        if not location_id or not inventory_item_id:
            return False
        if idempotency_key:
            resultat = self._ajuster_graphql(inventory_item_id, location_id, qty_change, idempotency_key)
            if resultat is not None:
                return resultat
            self._effacer_echec()  # GraphQL n'a rien exécuté : le repli REST est une opération neuve
        data = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available_adjustment": qty_change
        }
        # Mouvement RELATIF : jamais rejoué après une coupure (voir `make_request`).
        res = self.make_request("inventory_levels/adjust.json", method="POST", data=data,
                                rejouable=False)
        return res is not None

    def set_shopify_stock(self, inventory_item_id: int, location_id: str, quantite: int) -> bool:
        """
        Pose une valeur ABSOLUE de stock (correction d'inventaire, pas une vente).

        L'ajustement relatif est réservé aux mouvements (vente, remboursement) ; une correction
        d'inventaire saisie en boutique doit au contraire imposer la valeur comptée.
        """
        self._effacer_echec()
        if not location_id or not inventory_item_id:
            return False
        data = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available": int(quantite)
        }
        res = self.make_request("inventory_levels/set.json", method="POST", data=data)
        return res is not None

    # --- Poussée des ventes locales vers Shopify --------------------------------------------

    @staticmethod
    def _skus_candidats(c, produit_id, code_barre, stock_id) -> list:
        """
        SKU Shopify sous lesquels chercher la ligne de stock vendue, du plus sûr au moins sûr.

        Produit décliné : UNIQUEMENT le SKU de sa taille (`BI038-32`), celui qu'écrit l'export
        CSV. Le code-barres du produit n'est jamais essayé : l'export ne le pose que sur la
        première taille, il désignerait donc le 30 pour une vente de 32.
        Produit à une seule ligne de stock : son code-barres d'abord (bases importées depuis
        Shopify, où chaque variante est un produit dont le code EST le SKU), puis le SKU exporté.
        """
        c.execute("SELECT id, taille FROM Stocks WHERE id_produit = ? ORDER BY id", (produit_id,))
        lignes = c.fetchall()
        ids = [r[0] for r in lignes]
        skus = skus_produit(code_barre, produit_id, [r[1] for r in lignes])
        attendu = skus[ids.index(stock_id)] if stock_id in ids else None
        candidats = []
        code = str(code_barre).strip() if code_barre else ""
        if len(lignes) == 1 and code:
            candidats.append(code)
        if attendu and attendu not in candidats:
            candidats.append(attendu)
        return candidats

    # --- File d'attente `sync_queue` (jamais bloquante : un défaut ici n'arrête aucune vente) ---

    def _file_inscrire(self, conn, vd_id, t_id, code_barre, delta):
        try:
            c = _ouvrir_ecriture(conn)
            maintenant = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute("""
                INSERT INTO sync_queue
                    (id_vente_detail, id_ticket, code_barre, delta, statut, tentatives, date_creation, date_maj)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(id_vente_detail) DO UPDATE SET code_barre = excluded.code_barre
                WHERE sync_queue.statut = ?
            """, (vd_id, t_id, code_barre, int(delta), FILE_PENDING, maintenant, maintenant, FILE_PENDING))
            conn.commit()
        except Exception as e:
            self._file_abandon(conn, e)

    @staticmethod
    def _file_est_due(c, vd_id) -> bool:
        """Vrai si la ligne n'est pas en attente d'un délai de recul (lecture seule, jamais bloquante)."""
        try:
            c.execute("SELECT prochaine_tentative FROM sync_queue "
                      "WHERE id_vente_detail = ? AND statut = ?", (vd_id, FILE_PENDING))
            ligne = c.fetchone()
            return est_due(ligne[0] if ligne else None)
        except Exception:
            return True

    def _file_marquer(self, conn, vd_id, statut, erreur=None):
        try:
            c = _ouvrir_ecriture(conn)
            c.execute("UPDATE sync_queue SET statut = ?, derniere_erreur = ?, "
                      "date_maj = ?, prochaine_tentative = NULL WHERE id_vente_detail = ?",
                      (statut, erreur, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), vd_id))
            conn.commit()
        except Exception as e:
            self._file_abandon(conn, e)

    def _file_echec(self, conn, vd_id, erreur):
        """
        Reste PENDING : compte l'essai, garde la cause et pose `prochaine_tentative` (recul exponentiel
        par ligne : 30 s, 1 min, 2 min… plafonné à 15 min). La passe suivante saute la ligne tant que
        le délai court ; les autres lignes ne sont pas retardées.
        """
        try:
            c = _ouvrir_ecriture(conn)
            c.execute("SELECT tentatives FROM sync_queue WHERE id_vente_detail = ?", (vd_id,))
            ligne = c.fetchone()
            essais = int(ligne[0] or 0) + 1 if ligne else 1
            c.execute("UPDATE sync_queue SET tentatives = ?, derniere_erreur = ?, date_maj = ?, "
                      "prochaine_tentative = ? WHERE id_vente_detail = ?",
                      (essais, erreur, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       prochaine_tentative(essais), vd_id))
            conn.commit()
        except Exception as e:
            self._file_abandon(conn, e)

    def _file_purger(self, conn):
        try:
            limite = (datetime.datetime.now() - datetime.timedelta(days=FILE_CONSERVATION_JOURS)
                      ).strftime("%Y-%m-%d %H:%M:%S")
            c = _ouvrir_ecriture(conn)
            c.execute("DELETE FROM sync_queue WHERE statut IN (?, ?) AND date_maj < ?",
                      (FILE_DONE, FILE_IGNORE, limite))
            conn.commit()
        except Exception as e:
            self._file_abandon(conn, e)

    @staticmethod
    def _file_abandon(conn, erreur):
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"File d'attente locale non mise à jour (sans effet sur la vente) : {erreur}")

    def _journaliser(self, conn, vd_id, t_id, code_barre, inv_item_id, quantite, statut):
        """Inscrit (ou laisse en place) la ligne de vente au journal de synchronisation."""
        c = _ouvrir_ecriture(conn)
        c.execute("""
            INSERT OR IGNORE INTO Shopify_Sync_Lignes
                (id_vente_detail, id_ticket, code_barre, inventory_item_id, quantite_poussee, statut, date_heure)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (vd_id, t_id, code_barre, inv_item_id, quantite, statut,
              datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

    def sync_tickets_to_shopify(self) -> int:
        """
        Pousse les ventes/remboursements locaux non synchronisés vers Shopify pour maj du stock.

        L'idempotence est tenue LIGNE PAR LIGNE, pas ticket par ticket. Avant, un ticket de trois
        lignes dont la deuxième échouait restait à `synced_shopify = 0` alors que les lignes 1 et 3
        avaient déjà été ajustées : à la passe suivante, elles l'étaient une seconde fois. Comme
        l'API Shopify n'ajuste qu'en relatif (`available_adjustment`), l'écart ne se rattrapait
        jamais et le stock en ligne décrochait définitivement.

        Un remboursement est enregistré avec une quantité NÉGATIVE dans `Ventes_Details`
        (cf. `database_manager.enregistrer_remboursement`) : pousser `-quantite` recrédite donc
        bien la boutique, sans traitement particulier.
        """
        location_id = self.get_location_id()
        if not location_id:
            logger.warning("Impossible d'obtenir la localisation d'inventaire Shopify.")
            return 0

        self._rest_scan_fait = False
        self._rest_scan_echec = None
        conn = None
        synced_count = 0
        try:
            conn = get_connection()
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            # Une ligne restée « en vol » vient d'un arrêt brutal entre l'envoi et la réponse :
            # on ignore si Shopify l'a appliquée. On ne la rejoue jamais (une double
            # décrémentation est irrattrapable) et on la signale pour vérification manuelle.
            c.execute("SELECT id_vente_detail FROM Shopify_Sync_Lignes WHERE statut = ?", (STATUT_EN_VOL,))
            lignes_en_vol = [r[0] for r in c.fetchall()]
            c.execute("UPDATE Shopify_Sync_Lignes SET statut = ? WHERE statut = ?",
                      (STATUT_INDETERMINE, STATUT_EN_VOL))
            en_suspens = c.rowcount
            conn.commit()
            # La file `sync_queue` est ce que le contrôle de lancement et l'écran lisent : une ligne restée
            # « en vol » n'y apparaissait pas comme à vérifier (revue du 2026-10-14).
            for vd_en_vol in lignes_en_vol:
                self._file_marquer(conn, vd_en_vol, FILE_INDETERMINE,
                                   "poste arrêté entre l'envoi et la réponse de Shopify")
            if en_suspens:
                logger.warning(
                    f"[SYNC AUDIT] {en_suspens} ligne(s) de vente restée(s) sans réponse de Shopify : "
                    f"statut {STATUT_INDETERMINE}, à vérifier dans le journal Shopify_Sync_Lignes."
                )

            self._file_purger(conn)

            # Point de départ fixé à l'activation (`fixer_seuil_tickets`) : absent, rien n'est filtré.
            c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (PARAM_SEUIL_TICKETS,))
            ligne_seuil = c.fetchone()
            try:
                seuil = int(ligne_seuil[0]) if ligne_seuil else 0
            except (TypeError, ValueError):
                seuil = 0
            c.execute("SELECT id, numero_ticket FROM Tickets WHERE synced_shopify = 0 AND id > ? ORDER BY id",
                      (seuil,))
            tickets = c.fetchall()

            for t_id, num in tickets:
                # Les produits sans code-barres ne sont plus écartés : l'export les publie sous
                # un SKU `KODO-<id>`, par lequel on les retrouve.
                c.execute("""
                    SELECT vd.id, p.id, p.code_barre, s.id, vd.quantite
                    FROM Ventes_Details vd
                    JOIN Stocks s ON vd.id_stock = s.id
                    JOIN Produits p ON s.id_produit = p.id
                    LEFT JOIN Shopify_Sync_Lignes j ON j.id_vente_detail = vd.id
                    WHERE vd.id_ticket = ?
                      AND j.id_vente_detail IS NULL
                    ORDER BY vd.id
                """, (t_id,))
                lignes = c.fetchall()

                tout_pousse = True
                for vd_id, produit_id, code_produit, stock_id, quantite in lignes:
                    candidats = self._skus_candidats(c, produit_id, code_produit, stock_id)
                    code_barre = candidats[0] if candidats else code_produit
                    qte = int(quantite or 0)
                    self._file_inscrire(conn, vd_id, t_id, code_barre, -qte)
                    if not self._file_est_due(c, vd_id):
                        # Recul exponentiel par ligne : en échec récent, pas rejouée avant son heure.
                        tout_pousse = False
                        continue
                    if qte == 0:
                        self._journaliser(conn, vd_id, t_id, code_barre, None, 0, STATUT_SANS_OBJET)
                        self._file_marquer(conn, vd_id, FILE_IGNORE, "quantité nulle")
                        continue

                    inv_item_id, echec_reseau = None, False
                    for candidat in candidats:
                        inv_item_id, echec_reseau = self._resoudre_inventory_item(candidat)
                        if inv_item_id or echec_reseau:
                            code_barre = candidat
                            break
                    if echec_reseau:
                        logger.error(f"Recherche Shopify impossible pour SKU {code_barre} : ligne retentée plus tard.")
                        self._file_echec(conn, vd_id, "recherche Shopify impossible" + self._suffixe_echec())
                        tout_pousse = False
                        continue
                    if not inv_item_id:
                        self._journaliser(conn, vd_id, t_id, code_barre, None, 0, STATUT_ABSENT)
                        self._file_marquer(conn, vd_id, FILE_IGNORE, "SKU introuvable sur Shopify")
                        logger.warning(f"SKU {code_barre} introuvable sur Shopify : ligne tracée, plus retentée.")
                        continue

                    logger.info(f"Push vente locale (Ticket {num}) - SKU {code_barre} - Qte: {-qte}")
                    # Réservation AVANT l'appel : si le poste s'éteint pendant l'échange, la ligne
                    # est retrouvée « en vol » au démarrage suivant et ne sera pas rejouée à l'aveugle.
                    self._journaliser(conn, vd_id, t_id, code_barre, inv_item_id, -qte, STATUT_EN_VOL)
                    self._dernier_code_http = None
                    try:
                        applique = self.adjust_shopify_stock(
                            inv_item_id, location_id, -qte,
                            idempotency_key=f"kodo-vd-{vd_id}-{inv_item_id}")
                    except Exception as exc:
                        logger.error(f"Exception lors de l'ajustement du stock Shopify pour SKU {code_barre}: {exc}")
                        applique = False
                        self.dernier_echec = "reseau"
                    # 502 / 504 : la PASSERELLE a échoué sans qu'on sache si l'ajustement avait déjà porté (le
                    # « délai dépassé » d'un 504 est le cas typique). Un ajustement RELATIF ne se rejoue pas à
                    # l'aveugle : c'est une issue INCONNUE, traitée comme une coupure réseau (ligne à vérifier).
                    # 4xx, 500 et 503 restent des refus nets, retentés (revue du 2026-10-14).
                    if (not applique and self.dernier_echec != "reseau"
                            and getattr(self, "_dernier_code_http", None) in (502, 504)):
                        self.dernier_echec = "reseau"

                    c2 = _ouvrir_ecriture(conn)
                    if applique:
                        c2.execute("UPDATE Shopify_Sync_Lignes SET statut = ? WHERE id_vente_detail = ?",
                                   (STATUT_POUSSE, vd_id))
                        conn.commit()
                        self._file_marquer(conn, vd_id, FILE_DONE)
                    elif self.dernier_echec == "reseau":
                        # Aucune réponse : on ne sait pas si l'ajustement a porté. On garde la trace
                        # et on ne rejoue pas — sous-décompter se corrige, sur-décompter non.
                        c2.execute("UPDATE Shopify_Sync_Lignes SET statut = ? WHERE id_vente_detail = ?",
                                   (STATUT_INDETERMINE, vd_id))
                        logger.error(f"[SYNC AUDIT] Ligne {vd_id} (SKU {code_barre}) sans réponse Shopify : à vérifier.")
                        conn.commit()
                        self._file_marquer(conn, vd_id, FILE_INDETERMINE, "aucune réponse de Shopify")
                        tout_pousse = False
                    else:
                        # Shopify a répondu en erreur : rien n'a été appliqué, la ligne reste à pousser.
                        c2.execute("DELETE FROM Shopify_Sync_Lignes WHERE id_vente_detail = ? AND statut = ?",
                                   (vd_id, STATUT_EN_VOL))
                        logger.error(f"Échec de l'ajustement du stock Shopify pour SKU {code_barre}")
                        # 404 / 422 : l'article d'inventaire n'existe plus (produit supprimé puis recréé sur le
                        # site). Le cache le gardait pour toute la durée du processus : chaque passe retentait
                        # le même identifiant périmé. On l'oublie pour le résoudre de nouveau (revue du 2026-10-14).
                        if getattr(self, "_dernier_code_http", None) in (404, 422):
                            self._cache_inventaire.pop(str(code_barre), None)
                        conn.commit()
                        self._file_echec(conn, vd_id, "Shopify a refusé l'ajustement (429/5xx/erreur)"
                                         + self._suffixe_echec())
                        tout_pousse = False
                    conn.commit()

                if tout_pousse:
                    c_verif = _ouvrir_ecriture(conn)
                    c_verif.execute("""
                        SELECT 1 FROM Shopify_Sync_Lignes
                        WHERE id_ticket = ? AND statut = ?
                    """, (t_id, STATUT_INDETERMINE))
                    if c_verif.fetchone() is None:
                        c3 = _ouvrir_ecriture(conn)
                        c3.execute("UPDATE Tickets SET synced_shopify = 1 WHERE id = ?", (t_id,))
                        conn.commit()
                        synced_count += 1
                        logger.info(f"Ticket {num} marqué comme synchronisé Shopify.")
                    else:
                        logger.warning(f"Ticket {num} non marqué synchronisé : contient des lignes à statut indéterminé.")
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(f"Erreur sync tickets vers Shopify : {e}")
        finally:
            if conn:
                conn.close()
        return synced_count

    # --- Alignement du stock en ligne sur celui de la caisse ------------------------------------
    #
    # Les ventes et les retours partent en RELATIF : une quantité retirée ou rendue. Une quantité
    # saisie à la main sur la fiche produit n'est ni l'un ni l'autre, c'est un ÉTAT voulu (« il y
    # en a 8 »). Pour l'envoyer, on lit d'abord ce que la boutique affiche puis on envoie la
    # différence. Lire d'abord rend l'opération sûre à rejouer : une coupure, une passe interrompue
    # ou un double clic recalculent l'écart à partir de l'état réel, jamais d'une valeur périmée,
    # et ne peuvent donc pas appliquer deux fois la même correction. Un ajustement relatif (et non
    # une valeur absolue posée par-dessus) laisse aussi passer ce qui survient entre la lecture et
    # l'envoi : une commande web n'est jamais « effacée ».
    #
    # Valeur visée = quantité de la caisse + ventes que la boutique n'a pas encore reçues. Ces
    # ventes sont DÉJÀ retirées de la caisse ; elles partiront en relatif à la passe suivante et
    # ramèneront alors le site sur la caisse. Sans ce terme, elles seraient décomptées deux fois.

    LIGNES_ALIGNEES_PAR_PASSE = 60
    LOT_NIVEAUX = 50   # plafond de Shopify pour `inventory_item_ids` dans une lecture de niveaux

    @staticmethod
    def _quantites_caisse(c, stock_id):
        """
        `(quantité en caisse, ventes pas encore envoyées à Shopify)` d'une ligne de stock, ou None
        si la ligne n'existe plus. UNE requête : un instantané. Une vente qui s'enregistre ensuite
        retire autant de la première qu'elle n'ajoute à la seconde ; leur somme, qui est la valeur
        visée, ne bouge donc pas. Même sélection de lignes que `sync_tickets_to_shopify`.
        """
        c.execute("""
            SELECT s.quantite_actuelle,
                   COALESCE((SELECT SUM(vd.quantite)
                             FROM Ventes_Details vd
                             JOIN Tickets t ON t.id = vd.id_ticket
                             LEFT JOIN Shopify_Sync_Lignes j ON j.id_vente_detail = vd.id
                             WHERE vd.id_stock = s.id AND t.synced_shopify = 0 AND t.id > ?
                               AND j.id_vente_detail IS NULL), 0)
            FROM Stocks s WHERE s.id = ?
        """, (_seuil_tickets_effectif(c), stock_id))
        ligne = c.fetchone()
        if not ligne:
            return None
        return int(ligne[0] or 0), int(ligne[1] or 0)

    def _lire_niveaux(self, ids, location_id):
        """
        Quantité disponible en ligne de chaque `inventory_item_id`, au dépôt choisi.

        Retourne `(niveaux, reussi)`. Un identifiant absent de `niveaux` n'a pas de niveau dans ce
        dépôt ; la valeur None désigne un stock non suivi. `reussi` est faux si une lecture a échoué :
        on ne conclut alors rien (la cause est dans `dernier_echec_detail`).
        """
        niveaux = {}
        ids = [int(i) for i in ids]
        for debut in range(0, len(ids), self.LOT_NIVEAUX):
            lot = ids[debut:debut + self.LOT_NIVEAUX]
            endpoint = ("inventory_levels.json?inventory_item_ids="
                        + ",".join(str(i) for i in lot)
                        + f"&location_ids={location_id}&limit=250")
            res = self.make_request(endpoint)
            if not isinstance(res, dict) or not isinstance(res.get("inventory_levels"), list):
                return niveaux, False
            for niveau in res["inventory_levels"]:
                try:
                    item = int(niveau.get("inventory_item_id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if str(niveau.get("location_id")) != str(location_id):
                    continue
                dispo = niveau.get("available")
                try:
                    niveaux[item] = int(dispo) if dispo is not None else None
                except (TypeError, ValueError):
                    niveaux[item] = None
        return niveaux, True

    def _alignement_cloturer(self, conn, stock_id, version, statut, erreur=None,
                             kodo=None, site=None, ajustement=None):
        """
        Termine le marquage d'une ligne. `version` est celle lue AU DÉBUT du traitement : si la
        commerçante a ressaisi la quantité entre-temps, la ligne a été re-marquée (version plus
        haute) et reste à traiter — elle n'est pas déclarée alignée sur une valeur périmée.
        """
        try:
            c = _ouvrir_ecriture(conn)
            c.execute(
                f"UPDATE {shopify_alignement.TABLE} SET statut = ?, derniere_erreur = ?, "
                "quantite_kodo = ?, quantite_site = ?, ajustement = ?, date_maj = ? "
                "WHERE stock_id = ? AND version = ? AND statut = ?",
                (statut, erreur, kodo, site, ajustement,
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 stock_id, version, shopify_alignement.STATUT_A_ALIGNER))
            conn.commit()
        except Exception as e:
            self._alignement_abandon(conn, stock_id, e)

    def _alignement_echec(self, conn, stock_id, cause):
        """Reste à aligner : compte l'essai et garde la cause. Le rythme des rejeux est celui des passes."""
        try:
            c = _ouvrir_ecriture(conn)
            c.execute(
                f"UPDATE {shopify_alignement.TABLE} SET tentatives = tentatives + 1, "
                "derniere_erreur = ?, date_maj = ? WHERE stock_id = ? AND statut = ?",
                (str(cause)[:200], datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 stock_id, shopify_alignement.STATUT_A_ALIGNER))
            conn.commit()
        except Exception as e:
            self._alignement_abandon(conn, stock_id, e)

    @staticmethod
    def _alignement_abandon(conn, stock_id, erreur):
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"Alignement du stock {stock_id} non enregistré (il sera recalculé) : {erreur}")

    def _aligner_une_ligne(self, conn, stock_id, location_id) -> str:
        """
        Aligne UNE ligne de stock sur la boutique en ligne. Retourne son issue :
        `ajuste`, `aligne` (déjà égale), `absent` (article non publié en ligne), `sans_niveau`,
        `supprimee`, `echec` (à retenter à la passe suivante) ou `ignoree` (plus à aligner).
        Aucune transaction n'est ouverte pendant un appel réseau.
        """
        c = conn.cursor()
        c.execute(f"SELECT version FROM {shopify_alignement.TABLE} WHERE stock_id = ? AND statut = ?",
                  (stock_id, shopify_alignement.STATUT_A_ALIGNER))
        marque = c.fetchone()
        if not marque:
            return "ignoree"
        version = int(marque[0])

        c.execute("SELECT s.id_produit, p.code_barre FROM Stocks s "
                  "JOIN Produits p ON p.id = s.id_produit WHERE s.id = ?", (stock_id,))
        ligne = c.fetchone()
        if not ligne:
            self._alignement_cloturer(conn, stock_id, version, shopify_alignement.STATUT_SUPPRIMEE,
                                      "ligne de stock supprimée dans Kōdo")
            return "supprimee"
        produit_id, code_produit = ligne

        # Même résolution que pour une vente : le SKU de CETTE taille, jamais celui d'une voisine.
        candidats = self._skus_candidats(c, produit_id, code_produit, stock_id)
        sku = candidats[0] if candidats else None
        inv_item_id, echec_reseau = None, False
        for candidat in candidats:
            inv_item_id, echec_reseau = self._resoudre_inventory_item(candidat)
            if inv_item_id or echec_reseau:
                sku = candidat
                break
        if echec_reseau:
            self._alignement_echec(conn, stock_id, "recherche Shopify impossible" + self._suffixe_echec())
            return "echec"
        if not inv_item_id:
            self._alignement_cloturer(
                conn, stock_id, version, shopify_alignement.STATUT_ABSENT,
                f"SKU {sku} introuvable sur Shopify : article non publié en ligne")
            logger.info(f"Stock {stock_id} : SKU {sku} introuvable sur Shopify, rien à aligner.")
            return "absent"

        niveaux, reussi = self._lire_niveaux([inv_item_id], location_id)
        if not reussi:
            self._alignement_echec(conn, stock_id,
                                   "lecture du stock en ligne impossible" + self._suffixe_echec())
            return "echec"
        if niveaux.get(inv_item_id) is None:
            self._alignement_cloturer(
                conn, stock_id, version, shopify_alignement.STATUT_SANS_NIVEAU,
                "stock non suivi ou variante absente de ce dépôt : rien à aligner")
            return "sans_niveau"
        site = niveaux[inv_item_id]

        quantites = self._quantites_caisse(c, stock_id)
        if quantites is None:
            self._alignement_cloturer(conn, stock_id, version, shopify_alignement.STATUT_SUPPRIMEE,
                                      "ligne de stock supprimée dans Kōdo")
            return "supprimee"
        kodo, en_attente = quantites
        cible = kodo + en_attente
        if cible < 0:
            self._alignement_echec(
                conn, stock_id, f"valeur visée négative ({cible}) : ligne à vérifier à la main")
            return "echec"

        ecart = cible - site
        if ecart:
            logger.info(f"Alignement stock {stock_id} (SKU {sku}) : site {site} → {cible} ({ecart:+d}).")
            try:
                applique = self.adjust_shopify_stock(inv_item_id, location_id, ecart)
            except Exception as exc:
                logger.error(f"Exception lors de l'alignement du stock Shopify pour SKU {sku}: {exc}")
                applique = False
                self.dernier_echec = "reseau"
            if not applique:
                # Sans réponse, on ignore si l'ajustement a porté : aucune importance, la prochaine
                # lecture le dira et l'écart sera recalculé à partir de l'état réel.
                nature = ("aucune réponse de Shopify" if self.dernier_echec == "reseau"
                          else "Shopify a refusé l'ajustement")
                self._alignement_echec(conn, stock_id, nature + self._suffixe_echec())
                return "echec"
        self._alignement_cloturer(conn, stock_id, version, shopify_alignement.STATUT_ALIGNE, None,
                                  kodo=kodo, site=site, ajustement=ecart)
        return "ajuste" if ecart else "aligne"

    def aligner_lignes_modifiees(self, limite: int = None) -> dict:
        """
        Aligne la boutique en ligne sur la caisse pour les lignes dont la quantité a été saisie à la
        main (`Shopify_Alignements`). Appelée à chaque passe, APRÈS la poussée des ventes et
        l'import des commandes. Ne rejoue rien d'aveugle : voir l'en-tête de cette section.
        """
        bilan = {"traitees": 0, "ajustees": 0, "alignees": 0, "absentes": 0, "sans_niveau": 0,
                 "supprimees": 0, "en_echec": 0, "restantes": 0}
        if not self.est_configure():
            return bilan
        limite = int(limite or self.LIGNES_ALIGNEES_PAR_PASSE)
        correspondance = {"ajuste": "ajustees", "aligne": "alignees", "absent": "absentes",
                          "sans_niveau": "sans_niveau", "supprimee": "supprimees", "echec": "en_echec"}
        conn = get_connection()
        try:
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            c.execute(f"SELECT stock_id FROM {shopify_alignement.TABLE} WHERE statut = ? "
                      "ORDER BY date_maj, stock_id LIMIT ?",
                      (shopify_alignement.STATUT_A_ALIGNER, limite))
            ids = [r[0] for r in c.fetchall()]
            c.execute(f"SELECT COUNT(*) FROM {shopify_alignement.TABLE} WHERE statut = ?",
                      (shopify_alignement.STATUT_A_ALIGNER,))
            total = int(c.fetchone()[0] or 0)
            conn.commit()
            bilan["restantes"] = max(0, total - len(ids))
            if not ids:
                return bilan

            location_id = self.get_location_id()
            if not location_id:
                logger.warning("Alignement du stock impossible : dépôt d'inventaire Shopify introuvable.")
                bilan["en_echec"] = len(ids)
                return bilan

            self._rest_scan_fait = False
            self._rest_scan_echec = None
            for stock_id in ids:
                issue = self._aligner_une_ligne(conn, stock_id, location_id)
                if issue == "ignoree":
                    continue
                bilan["traitees"] += 1
                bilan[correspondance[issue]] += 1
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"Erreur lors de l'alignement du stock sur Shopify : {e}")
            bilan["en_echec"] += 1
        finally:
            conn.close()
        return bilan

    # --- Comparaison caisse ↔ boutique en ligne (lecture seule) puis alignement demandé ---------

    def _scanner_variantes_en_ligne(self):
        """
        Le catalogue en ligne, indexé par SKU puis par code-barres : `(par_sku, par_code)`, chacun
        `{clé: {inventory_item_id, ...}}`.

        Contrairement au cache de résolution (`_charger_cache_rest`, qui garde la PREMIÈRE variante
        rencontrée), celui-ci garde TOUTES celles qui portent la même clé : deux variantes au même
        SKU rendent une vente ambiguë, et c'est précisément ce que la comparaison doit montrer.
        Retourne aussi la liste à plat des variantes (titre, déclinaison, SKU, ids), qui sert à
        suggérer à quoi correspond un article que Kōdo ne retrouve pas par son SKU.
        Lève `RuntimeError` si le balayage échoue : un catalogue tronqué ne se compare pas.
        """
        par_sku, par_code, a_plat = {}, {}, []
        since_id = 0
        for _ in range(self.MAX_PAGES):
            endpoint = f"products.json?limit={self.PAGE_SIZE}&fields=id,title,variants"
            if since_id:
                endpoint += f"&since_id={since_id}"
            data = self.make_request(endpoint)
            if not isinstance(data, dict) or "products" not in data:
                raise RuntimeError("Lecture du catalogue Shopify impossible"
                                   + (f" ({self.dernier_echec_detail})" if self.dernier_echec_detail else "")
                                   + " : rien n'a été comparé.")
            page = data["products"]
            for produit in page:
                for v in produit.get("variants", []):
                    iid = v.get("inventory_item_id")
                    if not iid:
                        continue
                    for index, cle in ((par_sku, v.get("sku")), (par_code, v.get("barcode"))):
                        cle = str(cle or "").strip()
                        if cle:
                            index.setdefault(cle, set()).add(int(iid))
                    declinaison = self._taille_variante(v)
                    a_plat.append({
                        "variantId": v.get("id"), "item": int(iid), "titre": produit.get("title") or "",
                        "taille": declinaison, "sku": str(v.get("sku") or "").strip(),
                        "barcode": str(v.get("barcode") or "").strip(),
                        "_titre": self._normaliser_libelle(produit.get("title")),
                        "_taille": self._normaliser_libelle(declinaison),
                    })
            last_id = page[-1].get("id") if page else None
            if len(page) < self.PAGE_SIZE or not last_id or last_id == since_id:
                break
            since_id = last_id
        return par_sku, par_code, a_plat

    @staticmethod
    def _normaliser_libelle(valeur) -> str:
        """Nom ou taille comparable : sans accents, sans casse, espaces réduits."""
        import unicodedata
        texte = unicodedata.normalize("NFKD", str(valeur if valeur is not None else ""))
        texte = "".join(ch for ch in texte if not unicodedata.combining(ch))
        return " ".join(texte.casefold().split())

    def _suggerer_variante(self, nom, taille, ids_memorises, a_plat):
        """
        À quelle variante en ligne correspond une ligne que Kōdo ne retrouve pas par son SKU ?
        Une réponse n'est donnée que si elle est UNIQUE. Par ordre de sûreté : une variante déjà
        reliée à cet article par un import (id Shopify mémorisé), puis le même nom et la même taille.
        C'est une INFORMATION pour la commerçante : ni la synchronisation ni l'alignement ne s'en
        servent pour agir.
        """
        voulue = self._normaliser_libelle(taille)
        if self._est_taille_unique(taille):
            voulue = self._normaliser_libelle("Unique")

        def meme_taille(v):
            return v["_taille"] == voulue or (self._est_taille_unique(v["taille"])
                                              and self._est_taille_unique(taille))

        candidates = [v for v in a_plat if v["variantId"] in ids_memorises and meme_taille(v)]
        via = "variante déjà reliée à cet article par un import"
        if not candidates:
            titre = self._normaliser_libelle(nom)
            candidates = [v for v in a_plat if v["_titre"] == titre and meme_taille(v)]
            via = "même nom et même taille"
        if len(candidates) != 1:
            return None
        v = candidates[0]
        return {"via": via, "titre": v["titre"], "taille": v["taille"], "skuEnLigne": v["sku"],
                "codeBarresEnLigne": v["barcode"], "variantId": v["variantId"]}

    def comparer_stock(self) -> dict:
        """
        Compare, ligne de stock par ligne de stock, la caisse et la boutique en ligne. LECTURE
        SEULE : rien n'est modifié ni côté Kōdo ni côté Shopify.

        Pour chaque ligne : la quantité en caisse, les ventes pas encore reçues par le site, la
        valeur que le site DEVRAIT afficher (leur somme), ce qu'il affiche, et l'écart. `etat` :
        `identique`, `ecart`, `absent` (non publié en ligne), `ambigu` (plusieurs variantes en ligne
        portent ce SKU : aucune n'est choisie) ou `sans_niveau` (stock non suivi dans ce dépôt).
        """
        self.load_config()
        if not self.est_configure():
            raise ValueError("Configuration Shopify manquante (URL ou jeton).")
        location_id = self.get_location_id()
        if not location_id:
            raise RuntimeError("Dépôt d'inventaire Shopify introuvable"
                               + (f" ({self.dernier_echec_detail})" if self.dernier_echec_detail else "")
                               + " : rien n'a été comparé.")
        par_sku, par_code, a_plat = self._scanner_variantes_en_ligne()

        conn = get_connection()
        try:
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            conn.commit()
            memorisees = {}
            c.execute("SELECT variant_id, id_produit FROM Shopify_Variantes")
            for variant_id, id_produit in c.fetchall():
                memorisees.setdefault(id_produit, set()).add(variant_id)
            c.execute("""
                SELECT s.id, p.id, p.nom, p.code_barre, s.taille, s.quantite_actuelle,
                       COALESCE((SELECT SUM(vd.quantite)
                                 FROM Ventes_Details vd
                                 JOIN Tickets t ON t.id = vd.id_ticket
                                 LEFT JOIN Shopify_Sync_Lignes j ON j.id_vente_detail = vd.id
                                 WHERE vd.id_stock = s.id AND t.synced_shopify = 0 AND t.id > ?
                                   AND j.id_vente_detail IS NULL), 0)
                FROM Stocks s JOIN Produits p ON p.id = s.id_produit
                ORDER BY p.nom COLLATE NOCASE, s.id
            """, (_seuil_tickets_effectif(c),))
            lignes = c.fetchall()

            rapport, a_lire = [], set()
            for sid, pid, nom, code, taille, kodo, attente in lignes:
                candidats = self._skus_candidats(c, pid, code, sid)
                sku, items = (candidats[0] if candidats else None), set()
                for candidat in candidats:
                    trouves = par_sku.get(candidat) or par_code.get(candidat)
                    if trouves:
                        sku, items = candidat, set(trouves)
                        break
                kodo, attente = int(kodo or 0), int(attente or 0)
                ligne = {"stockId": sid, "produitId": pid, "produit": nom, "taille": taille,
                         "sku": sku, "quantiteKodo": kodo, "ventesEnAttente": attente,
                         "cible": kodo + attente, "quantiteSite": None, "ecart": None,
                         "etat": "absent", "suggestion": None, "_item": None}
                if len(items) > 1:
                    ligne["etat"] = "ambigu"
                elif items:
                    ligne["_item"] = next(iter(items))
                    a_lire.add(ligne["_item"])
                else:
                    ligne["suggestion"] = self._suggerer_variante(
                        nom, taille, memorisees.get(pid, set()), a_plat)
                rapport.append(ligne)
        finally:
            conn.close()

        niveaux, reussi = self._lire_niveaux(sorted(a_lire), location_id)
        if not reussi:
            raise RuntimeError("Lecture des quantités en ligne impossible"
                               + (f" ({self.dernier_echec_detail})" if self.dernier_echec_detail else "")
                               + " : rien n'a été comparé.")

        resume = {"lignes": len(rapport), "identiques": 0, "ecarts": 0, "absentes": 0,
                  "ambigues": 0, "sansNiveau": 0}
        for ligne in rapport:
            item = ligne.pop("_item")
            if ligne["etat"] == "ambigu":
                resume["ambigues"] += 1
                continue
            if item is None:
                resume["absentes"] += 1
                continue
            if niveaux.get(item) is None:
                ligne["etat"] = "sans_niveau"
                resume["sansNiveau"] += 1
                continue
            ligne["quantiteSite"] = niveaux[item]
            ligne["ecart"] = ligne["cible"] - niveaux[item]
            ligne["etat"] = "ecart" if ligne["ecart"] else "identique"
            resume["ecarts" if ligne["ecart"] else "identiques"] += 1
            # La comparaison vient de résoudre ces SKU : l'alignement qui suit n'a pas à les
            # rechercher un par un (une requête GraphQL de moins par ligne).
            if ligne["sku"] and item:
                self._cache_inventaire.setdefault(str(ligne["sku"]), item)
        return {"lignes": rapport, "resume": resume, "depot": str(location_id)}

    def aligner_stock(self, stock_ids=None) -> dict:
        """
        Aligne la boutique en ligne sur la caisse pour les lignes en écart (toutes, ou celles de
        `stock_ids`). Les valeurs sont RELUES ligne par ligne au moment d'écrire : le rapport de
        comparaison n'est qu'un point de départ, jamais une valeur envoyée telle quelle.
        """
        rapport = self.comparer_stock()
        voulues = None if stock_ids is None else {int(s) for s in stock_ids}
        retenues = [l for l in rapport["lignes"]
                    if l["etat"] == "ecart" and (voulues is None or l["stockId"] in voulues)]
        if retenues:
            conn = get_connection()
            try:
                c = _ouvrir_ecriture(conn)
                shopify_alignement.assurer_table(c)
                for l in retenues:
                    shopify_alignement.marquer(
                        c, l["stockId"], origine=shopify_alignement.ORIGINE_COMPARAISON,
                        produit_id=l["produitId"], produit=l["produit"], taille=l["taille"],
                        forcer=True)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        bilan = self.aligner_lignes_modifiees(limite=10 ** 6)

        resultats = []
        if retenues:
            conn = get_connection()
            try:
                c = conn.cursor()
                for l in retenues:
                    c.execute(f"SELECT statut, quantite_kodo, quantite_site, ajustement, derniere_erreur "
                              f"FROM {shopify_alignement.TABLE} WHERE stock_id = ?", (l["stockId"],))
                    r = c.fetchone()
                    resultats.append({
                        "stockId": l["stockId"], "produit": l["produit"], "taille": l["taille"],
                        "sku": l["sku"], "statut": r[0] if r else None,
                        "quantiteSiteAvant": r[2] if r else None,
                        "ajustement": r[3] if r else None, "erreur": r[4] if r else None})
            finally:
                conn.close()
        return {"comparaison": rapport["resume"], "bilan": bilan, "lignes": resultats}

    # --- Rapatriement des commandes Shopify -------------------------------------------------

    @staticmethod
    def _est_taille_unique(taille) -> bool:
        """Libellés désignant l'absence de déclinaison (« Taille Unique » côté POS, « Unique » côté import)."""
        return str(taille or "").strip().casefold() in ("", "unique", "taille unique", "default title")

    @staticmethod
    def _extraire_taille(item: dict):
        """
        Déclinaison portée par une ligne de commande Shopify (`variant_title`, sinon `properties`).

        Sans elle, la commande d'un M décrémentait la PREMIÈRE ligne de `Stocks` venue — le S dans
        la quasi-totalité des cas, puisque les tailles sont créées dans l'ordre S, M, L.
        """
        titre = item.get("variant_title")
        if titre and str(titre).strip().casefold() != "default title":
            return str(titre).strip()
        for prop in item.get("properties") or []:
            nom = str((prop or {}).get("name", "")).strip().casefold()
            if nom in ("taille", "size", "pointure"):
                valeur = str(prop.get("value", "")).strip()
                if valeur:
                    return valeur
        return None

    def _resoudre_ligne_stock(self, c, sku, titre_article, taille, variant_id=None, barcode=None):
        """
        Retourne `(stock_id | None, produit_id | None, motif | None)`.

        Motifs : "INTROUVABLE" (aucun produit local), "SANS_STOCK" (produit sans ligne de stock),
        "AMBIGU" (produit trouvé mais impossible de désigner la bonne déclinaison sans deviner).
        On préfère ne RIEN décrémenter et le signaler plutôt que de retirer une pièce d'une taille
        qui n'a pas été vendue : un stock faux dans les deux sens est pire qu'un stock à vérifier.

        L'ordre de résolution est le même que celui de `_retrouver_produit`, et pour la même
        raison : l'`id` de variante Shopify est la SEULE clé stable. Le SKU et le code-barres
        sont modifiables à tout moment depuis l'admin Shopify, et l'un des deux seulement est
        recopié dans `code_barre` à l'import — lequel dépend de la variante. Chercher uniquement
        `code_barre = sku`, comme ici avant, laissait donc une commande légitime « introuvable »
        (aucune quantité retirée, stock en ligne qui s'éloigne du stock réel) dès qu'un article
        avait été importé par son code-barres ou que son SKU avait été renommé depuis.
        C'est aussi ce qui rend inutile l'inversion `sku`/`barcode` autrefois envisagée : plutôt
        que de choisir un champ et de dédoubler le catalogue des bases déjà synchronisées, on
        accepte les deux et on privilégie la clé qui ne bouge pas.
        """
        produit_id = None

        if variant_id:
            try:
                vid = int(variant_id)
            except (TypeError, ValueError):
                vid = None
            if vid:
                c.execute(
                    "SELECT v.id_produit FROM Shopify_Variantes v "
                    "JOIN Produits p ON p.id = v.id_produit WHERE v.variant_id = ?",
                    (vid,),
                )
                row = c.fetchone()
                if row:
                    produit_id = row[0]

        if produit_id is None:
            for cle in (sku, barcode):
                cle = str(cle).strip() if cle else ""
                if not cle:
                    continue
                c.execute("SELECT id FROM Produits WHERE code_barre = ?", (cle,))
                row = c.fetchone()
                if row:
                    produit_id = row[0]
                    break

        if produit_id is None:
            # SKU de déclinaison écrit par l'export CSV (`BI038-32`) : il désigne le produit ET la
            # taille. C'est la seule clé d'une variante exportée sans code-barres (toutes sauf la
            # première) dès que son titre a été modifié dans l'admin Shopify.
            ligne = self._resoudre_sku_export(c, sku)
            if ligne:
                return ligne[0], ligne[1], None

        if produit_id is None and titre_article:
            # Repli par nom accepté UNIQUEMENT s'il ne désigne qu'un seul article : avec un
            # `LIMIT 1`, deux homonymes (« Robe été » de deux marques) faisaient décrémenter
            # le stock du mauvais produit, en silence.
            c.execute("SELECT id FROM Produits WHERE nom = ? LIMIT 2", (titre_article,))
            rows = c.fetchall()
            if len(rows) == 1:
                produit_id = rows[0][0]
            elif len(rows) > 1:
                logger.warning(f"Plusieurs produits locaux nommés « {titre_article} » : résolution par nom refusée.")

        if produit_id is None:
            return None, None, "INTROUVABLE"

        c.execute("SELECT id, taille FROM Stocks WHERE id_produit = ? ORDER BY id", (produit_id,))
        lignes = c.fetchall()
        if not lignes:
            return None, produit_id, "SANS_STOCK"

        if taille:
            voulue = str(taille).strip().casefold()
            for sid, t in lignes:
                if str(t or "").strip().casefold() == voulue:
                    return sid, produit_id, None
            # Shopify concatène les options (« M / Rouge ») : on retente sur la première seule,
            # qui est la taille dans la convention Kōdo.
            premiere = voulue.split("/")[0].strip()
            for sid, t in lignes:
                if str(t or "").strip().casefold() == premiere:
                    return sid, produit_id, None
            if self._est_taille_unique(voulue):
                for sid, t in lignes:
                    if self._est_taille_unique(t):
                        return sid, produit_id, None
            # Une seule ligne locale « Taille Unique » : le POS ne décline pas cet article,
            # la taille annoncée par Shopify n'a pas d'équivalent à choisir.
            if len(lignes) == 1 and self._est_taille_unique(lignes[0][1]):
                return lignes[0][0], produit_id, None
            return None, produit_id, "AMBIGU"

        for sid, t in lignes:
            if self._est_taille_unique(t):
                return sid, produit_id, None
        if len(lignes) == 1:
            return lignes[0][0], produit_id, None
        return None, produit_id, "AMBIGU"

    @staticmethod
    def _resoudre_sku_export(c, sku):
        """`(stock_id, produit_id)` de la ligne de stock dont l'export a produit ce SKU, ou None."""
        sku = str(sku or "").strip()
        if not sku:
            return None
        for prefixe in prefixes_possibles(sku):
            produits = [r[0] for r in c.execute(
                "SELECT id FROM Produits WHERE code_barre = ?", (prefixe,)).fetchall()]
            if prefixe.startswith("KODO-") and prefixe[5:].isdigit():
                produits.append(int(prefixe[5:]))
            for pid in produits:
                c.execute("SELECT id, taille FROM Stocks WHERE id_produit = ? ORDER BY id", (pid,))
                lignes = c.fetchall()
                code = c.execute("SELECT code_barre FROM Produits WHERE id = ?", (pid,)).fetchone()
                if not code:
                    continue
                skus = skus_produit(code[0], pid, [r[1] for r in lignes])
                if sku in skus:
                    return lignes[skus.index(sku)][0], pid
        return None

    @staticmethod
    def _marquer_audit(c, stock_id, produit_id):
        """Pose le drapeau d'audit de stock (patron maison, cf. `database_manager.enregistrer_vente`)."""
        try:
            if stock_id:
                c.execute("UPDATE Stocks SET requires_stock_audit = 1 WHERE id = ?", (stock_id,))
            if produit_id:
                c.execute("UPDATE Produits SET requires_stock_audit = 1 WHERE id = ?", (produit_id,))
        except Exception as e:
            # On ne fait pas échouer toute la passe de synchronisation pour un drapeau : sur une
            # base ancienne, la colonne `requires_stock_audit` peut simplement ne pas exister.
            # Mais se taire était pire que le défaut : le drapeau est le SEUL signalement d'un
            # écart de stock, et l'avaler en silence le rendait indétectable, y compris quand la
            # cause n'était pas la colonne absente (un déclencheur refusant l'UPDATE, par ex.).
            logger.error(
                f"[SYNC AUDIT] Drapeau d'audit de stock NON posé (stock={stock_id}, "
                f"produit={produit_id}) : {e}. L'écart de stock ne remontera pas à l'écran."
            )

    def _prix_unitaire_tvac(self, order: dict, item: dict, repli):
        """
        Prix unitaire TVA comprise d'une ligne de commande, tel que la cliente l'a payé.

        Shopify exprime `line_items[].price` TVA comprise ou non selon le réglage `taxes_included`
        de la boutique : on rétablit la TVA à partir de `tax_lines` quand elle en est exclue. Sans
        ça, le détail du ticket ne totalisait pas l'en-tête (qui vient de `total_price`, toujours TTC).
        """
        brut = item.get("price")
        if brut is None:
            return repli
        try:
            prix = Decimal(str(brut))
        except Exception:
            return repli
        if not order.get("taxes_included", True):
            taux = Decimal("0")
            for tl in item.get("tax_lines") or []:
                try:
                    taux += Decimal(str(tl.get("rate") or 0))
                except Exception:
                    continue
            prix = prix * (Decimal("1") + taux)
        return prix.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _fetch_all_orders(self):
        """
        Commandes payées et non honorées, paginées par `since_id`.

        Sans pagination, l'API n'en renvoyait que 50 : passé ce seuil, les commandes les plus
        anciennes n'étaient jamais rapatriées. Une page suivante en échec n'annule pas la passe :
        chaque commande est importée indépendamment et de façon idempotente (`shopify_order_id`),
        le reste sera repris à la passe suivante.
        """
        commandes, since_id = [], 0
        for page_no in range(1, self.MAX_PAGES + 1):
            endpoint = (f"orders.json?status=any&fulfillment_status=unfulfilled"
                        f"&financial_status=paid&limit={self.PAGE_SIZE}")
            if since_id:
                endpoint += f"&since_id={since_id}"
            data = self.make_request(endpoint)
            if not data or "orders" not in data:
                if page_no == 1:
                    return None
                logger.warning(f"Récupération des commandes interrompue à la page {page_no} : reprise à la passe suivante.")
                break
            page = data["orders"]
            commandes.extend(page)
            last_id = page[-1].get("id") if page else None
            if len(page) < self.PAGE_SIZE or not last_id or last_id == since_id:
                break
            since_id = last_id
        return commandes

    @staticmethod
    def _commandes_en_caisse(conn) -> bool:
        """Repli : le réglage `shopify_commandes_en_caisse` = 1 rétablit l'ancien import en tickets de caisse."""
        try:
            c = conn.cursor()
            c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (CLE_COMMANDES_EN_CAISSE,))
            row = c.fetchone()
        except Exception:
            return False
        return bool(row) and str(row[0] or "").strip().lower() in ("1", "true", "oui")

    def sync_orders_from_shopify(self) -> int:
        """
        Rapatrie les commandes Shopify payées et non traitées.

        Une commande en ligne n'est PAS une vente de caisse : la comptabilité du web se tient dans
        Shopify, celle du magasin dans Kōdo. L'import ne fait donc que décompter le stock (avec le
        même garde-fou et la même trace des surventes) et mémoriser la commande
        (`Shopify_Commandes`) : aucun ticket, aucune écriture au journal de caisse, rien au Z.
        Le réglage `shopify_commandes_en_caisse` = 1 rétablit l'ancien comportement (tickets signés).
        """
        orders = self._fetch_all_orders()
        if not orders:
            return 0

        conn = None
        imported_orders = 0
        try:
            conn = get_connection()
            # La résolution d'une ligne lit `Shopify_Variantes` : la table doit exister avant
            # la première commande, y compris sur une base ouverte sans passer par les migrations.
            _assurer_tables_sync(_ouvrir_ecriture(conn))
            conn.commit()
            c = conn.cursor()
            en_caisse = self._commandes_en_caisse(conn)

            for order in orders:
                order_id = str(order["id"])
                order_number = order.get("order_number", order_id)

                if order.get("cancelled_at"):
                    # Une commande annulée ne doit pas devenir une vente : l'importer créerait un
                    # ticket NF525 pour un encaissement qui n'existe pas.
                    logger.info(f"Commande Shopify #{order_number} annulée : ignorée.")
                    continue

                try:
                    c2 = _ouvrir_ecriture(conn)
                    # Deux registres : une commande déjà importée comme ticket par une version
                    # précédente ne doit JAMAIS être décomptée une seconde fois.
                    c2.execute("SELECT id FROM Tickets WHERE shopify_order_id = ?", (order_id,))
                    deja = c2.fetchone()
                    if not deja:
                        c2.execute("SELECT 1 FROM Shopify_Commandes WHERE order_id = ?", (order_id,))
                        deja = c2.fetchone()
                    if deja:
                        conn.rollback()
                        continue

                    logger.info(f"Traitement de la commande Shopify #{order_number} (ID: {order_id})")

                    stock_changes = []
                    for item in order.get("line_items", []):
                        sku = item.get("sku")
                        titre = item.get("title")
                        variant_id = item.get("variant_id")
                        barcode = item.get("barcode")
                        qty = int(item.get("quantity", 0) or 0)
                        if qty <= 0 or not (sku or titre or variant_id or barcode):
                            continue

                        taille = self._extraire_taille(item)
                        sid, pid, motif = self._resoudre_ligne_stock(
                            c2, sku, titre, taille, variant_id=variant_id, barcode=barcode
                        )

                        if motif == "INTROUVABLE":
                            logger.warning(f"SKU local introuvable pour {sku} / {titre} : ligne non décomptée.")
                        elif motif in ("AMBIGU", "SANS_STOCK"):
                            logger.warning(
                                f"[STOCK AUDIT] Commande #{order_number} : impossible de désigner la déclinaison "
                                f"« {taille} » de {sku or titre} ({motif}) — aucune quantité retirée, produit à vérifier."
                            )
                            self._marquer_audit(c2, None, pid)

                        prix_local = Decimal("0.00")
                        if sid:
                            c2.execute("SELECT p.prix_vente_tvac FROM Stocks s JOIN Produits p ON p.id = s.id_produit WHERE s.id = ?", (sid,))
                            row_px = c2.fetchone()
                            if row_px and row_px[0] is not None:
                                prix_local = Decimal(str(row_px[0]))
                        prix_unitaire = self._prix_unitaire_tvac(order, item, prix_local)

                        if sid:
                            # Décompte gardé (patron maison) : le compteur ne passe jamais sous zéro,
                            # et une survente est TRACÉE au lieu d'être rabotée en silence par un
                            # `min(qty, quantite_actuelle)` dont personne n'aurait jamais eu trace.
                            c2.execute(
                                "UPDATE Stocks SET quantite_actuelle = quantite_actuelle - ? "
                                "WHERE id = ? AND quantite_actuelle >= ?",
                                (qty, sid, qty),
                            )
                            if c2.rowcount == 0:
                                c2.execute("UPDATE Stocks SET quantite_actuelle = 0 WHERE id = ?", (sid,))
                                self._marquer_audit(c2, sid, pid)
                                logger.warning(
                                    f"[STOCK AUDIT] Survente détectée sur stock_id={sid} (commande #{order_number}, "
                                    f"demandé={qty}) — stock plafonné à 0."
                                )

                        stock_changes.append({"stock_id": sid, "produit_id": pid, "qty": qty,
                                              "prix_unitaire_tvac": prix_unitaire})

                    if en_caisse:
                        total_price = Decimal(str(order.get("total_price", "0.00")))
                        total_tax = Decimal(str(order.get("total_tax", "0.00")))
                        total_htva = total_price - total_tax

                        safe_order_num = "".join(char for char in str(order_number) if char.isalnum() or char in "-_")
                        num_ticket = f"SHPF-{safe_order_num}"
                        date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                        sig_ticket, hash_prec_ticket = signer_ticket(c2, num_ticket, total_price, date_heure)

                        c2.execute("""
                            INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement, signature, hash_precedent, shopify_order_id, synced_shopify)
                            VALUES (?, ?, ?, ?, ?, 'Shopify', ?, ?, ?, 1)
                        """, (num_ticket, date_heure, total_price, total_htva, total_tax, sig_ticket, hash_prec_ticket, order_id))

                        ticket_id = c2.lastrowid

                        for sc in stock_changes:
                            c2.execute("""
                                INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
                                VALUES (?, ?, ?, ?)
                            """, (ticket_id, sc["stock_id"], sc["qty"], sc["prix_unitaire_tvac"]))

                        sig_ledger, hash_ledger = signer_ledger(c2, "VENTE", total_price, "Shopify", num_ticket, date_heure)
                        c2.execute("""
                            INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent)
                            VALUES ('Shopify Sync', 'VENTE', ?, 'Shopify', ?, ?, ?, ?)
                        """, (total_price, num_ticket, date_heure, sig_ledger, hash_ledger))

                        conn.commit()
                        _sceller_tickets(conn, [num_ticket])
                        imported_orders += 1
                        logger.info(f"Commande #{order_number} synchronisée avec succès.")
                    else:
                        total_price = Decimal(str(order.get("total_price", "0.00")))
                        date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        # Le décompte de stock est déjà fait (ci-dessus, avec garde-fou et audit des
                        # surventes). Il ne reste qu'à se souvenir de la commande : sans elle, la
                        # passe suivante la décompterait une seconde fois.
                        c2.execute(
                            "INSERT INTO Shopify_Commandes (order_id, order_number, date_import, total, nb_lignes) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (order_id, str(order_number), date_heure, str(total_price), len(stock_changes)),
                        )
                        for sc in stock_changes:
                            if not sc["stock_id"]:
                                continue
                            c2.execute(
                                "INSERT INTO Shopify_Commandes_Lignes (order_id, stock_id, produit_id, quantite) "
                                "VALUES (?, ?, ?, ?)",
                                (order_id, sc["stock_id"], sc.get("produit_id"), sc["qty"]),
                            )
                        conn.commit()
                        imported_orders += 1
                        logger.info(f"Commande #{order_number} importée hors caisse : stock décompté, comptabilité tenue dans Shopify.")

                except Exception as ex_order:
                    if conn:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    logger.error(f"Erreur traitement commande #{order_number} : {ex_order}")

        except Exception as e:
            logger.error(f"Erreur générale synchro commandes de Shopify : {e}")
        finally:
            if conn:
                conn.close()
        return imported_orders

    # --- Remboursements et annulations survenus APRÈS l'import -------------------------------

    @staticmethod
    def _horodatage_shopify(valeur):
        """Convertit un horodatage ISO 8601 Shopify en `datetime` aware, ou None."""
        texte = str(valeur or "").strip()
        if not texte:
            return None
        if texte.endswith("Z"):
            texte = texte[:-1] + "+00:00"
        try:
            moment = datetime.datetime.fromisoformat(texte)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment

    def _repere_remboursements(self, conn) -> str:
        """
        Borne basse de l'inspection : dernier point vérifié, ou 90 jours en arrière au premier
        passage. On la relit en base plutôt que de la garder en mémoire pour que l'application
        redémarrée ne reparte pas de zéro et ne rate pas non plus la période d'arrêt.
        """
        valeur = None
        try:
            c = conn.cursor()
            c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (CLE_REMB_DEPUIS,))
            row = c.fetchone()
            valeur = row[0] if row else None
        except Exception:
            valeur = None
        moment = self._horodatage_shopify(valeur)
        if moment is None:
            moment = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=FENETRE_REMBOURSEMENTS_JOURS))
        return moment.isoformat()

    def _ecrire_repere_remboursements(self, conn, moment_iso):
        try:
            c = _ouvrir_ecriture(conn)
            c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                      (CLE_REMB_DEPUIS, moment_iso))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"Repère des remboursements non enregistré : {e}")

    def _fetch_commandes_modifiees(self, depuis_iso):
        """
        Commandes modifiées depuis `depuis_iso`, tous statuts confondus.

        `_fetch_all_orders` ne peut PAS servir ici : il filtre `financial_status=paid` et
        `fulfillment_status=unfulfilled`. Or une commande remboursée passe justement en
        `refunded` / `partially_refunded`, et une commande honorée sort de `unfulfilled` —
        les deux disparaissent donc de cette liste au moment précis où il faudrait les revoir.
        C'est la raison pour laquelle un remboursement fait en ligne n'a jamais été vu par la
        caisse. On interroge donc sur la date de modification, que Shopify met à jour à chaque
        remboursement, et on borne par le repère pour ne pas relire tout l'historique.
        """
        commandes, since_id = [], 0
        borne = urllib.parse.quote(str(depuis_iso), safe="")
        for page_no in range(1, self.MAX_PAGES + 1):
            endpoint = (f"orders.json?status=any&updated_at_min={borne}"
                        f"&limit={self.PAGE_SIZE}")
            if since_id:
                endpoint += f"&since_id={since_id}"
            data = self.make_request(endpoint)
            if not data or "orders" not in data:
                if page_no == 1:
                    return None
                logger.warning(
                    f"Inspection des remboursements interrompue à la page {page_no} : "
                    f"le repère n'avance pas, la passe suivante reprendra au même point."
                )
                return commandes, False
            page = data["orders"]
            commandes.extend(page)
            last_id = page[-1].get("id") if page else None
            if len(page) < self.PAGE_SIZE or not last_id or last_id == since_id:
                break
            since_id = last_id
        return commandes, True

    def sync_refunds_from_shopify(self) -> int:
        """
        Rapatrie les remboursements et annulations survenus APRÈS l'import de la commande.

        Sans cette passe, une commande en ligne remboursée restait comptée comme une vente
        pleine : elle pesait dans le rapport Z et dans la TVA déclarée, et l'article remboursé
        ne revenait jamais en rayon — l'écart de stock se creusait à chaque retour.

        Règle de prudence, appliquée sans exception : **seuls les remboursements déclarés par
        Shopify créent une écriture financière.** Une commande annulée sans remboursement
        (paiement conservé, geste commercial traité hors caisse) n'est JAMAIS transformée en
        remboursement d'office : on ne fabrique pas un mouvement d'argent que la boutique n'a
        pas fait. Elle est signalée dans le journal et ses produits sont marqués à vérifier.

        Retourne le nombre de remboursements traités.
        """
        conn = None
        traites = 0
        try:
            conn = get_connection()
            _assurer_tables_sync(_ouvrir_ecriture(conn))
            conn.commit()

            depuis = self._repere_remboursements(conn)
            resultat = self._fetch_commandes_modifiees(depuis)
            if not resultat:
                return 0
            commandes, complet = resultat
            if not commandes:
                return 0

            plus_recent = None
            for order in commandes:
                try:
                    traites += self._appliquer_remboursements_commande(conn, order)
                except Exception as ex:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    complet = False
                    logger.error(f"Remboursements de la commande {order.get('id')} non traités : {ex}")
                    continue
                moment = self._horodatage_shopify(order.get("updated_at"))
                if moment and (plus_recent is None or moment > plus_recent):
                    plus_recent = moment

            # Le repère n'avance QUE si toute la fenêtre a été traitée sans incident : avancer
            # après un échec partiel ferait sauter définitivement les remboursements manqués.
            if complet and plus_recent is not None:
                nouveau = plus_recent - datetime.timedelta(seconds=RECOUVREMENT_REMBOURSEMENTS_S)
                self._ecrire_repere_remboursements(conn, nouveau.isoformat())
        except Exception as e:
            logger.error(f"Inspection des remboursements Shopify interrompue : {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return traites

    def _lignes_remboursables(self, c, ticket_id, stock_id):
        """
        Lignes de vente du ticket portant ce stock, avec la quantité ENCORE remboursable.

        La correspondance passe par le stock et non par l'identifiant de ligne Shopify : les
        commandes importées par les versions précédentes n'ont jamais mémorisé cet identifiant,
        et un remboursement doit pouvoir les atteindre aussi.
        """
        c.execute(
            "SELECT id, quantite FROM Ventes_Details "
            "WHERE id_ticket = ? AND id_stock = ? AND quantite > 0 AND refund_of_vd_id IS NULL "
            "ORDER BY id ASC",
            (ticket_id, stock_id),
        )
        lignes = []
        for vd_id, qte in c.fetchall():
            c.execute("SELECT COALESCE(SUM(-quantite), 0) FROM Ventes_Details WHERE refund_of_vd_id = ?", (vd_id,))
            deja = c.fetchone()[0] or 0
            restant = int(qte) - int(deja)
            if restant > 0:
                lignes.append((vd_id, restant))
        return lignes

    def _appliquer_remboursements_commande(self, conn, order) -> int:
        order_id = str(order.get("id") or "").strip()
        if not order_id:
            return 0

        remboursements = order.get("refunds") or []
        annulee = bool(order.get("cancelled_at"))
        if not remboursements and not annulee:
            return 0

        c = conn.cursor()
        c.execute(
            "SELECT id, numero_ticket, caisse_id FROM Tickets "
            "WHERE shopify_order_id = ? AND total_tvac > 0 ORDER BY id ASC LIMIT 1",
            (order_id,),
        )
        row = c.fetchone()
        if not row:
            c.execute("SELECT 1 FROM Shopify_Commandes WHERE order_id = ?", (order_id,))
            if c.fetchone():
                return self._appliquer_remboursements_hors_caisse(conn, order, order_id, remboursements, annulee)
            # Commande jamais importée localement : il n'y a aucune vente à corriger.
            return 0
        ticket_id, numero_ticket, caisse_id = row
        caisse_id = caisse_id or "POS-01"

        traites = 0
        for remb in remboursements:
            cle = f"refund:{remb.get('id')}"
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if c.fetchone():
                continue
            if self._traiter_un_remboursement(conn, order, remb, cle, ticket_id, numero_ticket, caisse_id):
                traites += 1

        if annulee:
            cle = f"annulation:{order_id}"
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if not c.fetchone():
                self._signaler_annulation(conn, order, cle, ticket_id, numero_ticket, bool(remboursements))
        return traites

    def _traiter_un_remboursement(self, conn, order, remb, cle, ticket_id, numero_ticket, caisse_id) -> bool:
        from database_manager import enregistrer_remboursement

        order_number = order.get("order_number", order.get("id"))
        c = _ouvrir_ecriture(conn)
        try:
            # Relecture sous verrou : deux passes simultanées (thread automatique + synchro
            # lancée depuis l'écran) verraient toutes deux le remboursement comme non traité.
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if c.fetchone():
                conn.rollback()
                return False

            date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tickets_crees, montant_total = [], Decimal("0.00")

            for rli in remb.get("refund_line_items") or []:
                quantite = int(rli.get("quantity") or 0)
                if quantite <= 0:
                    continue
                li = rli.get("line_item") or {}
                # `no_restock` : la cliente est remboursée mais l'article ne revient pas au
                # stock vendable. Remettre la pièce en rayon créerait une unité fantôme qui
                # serait ensuite poussée vers Shopify et vendue une seconde fois.
                recrediter = str(rli.get("restock_type") or "").strip().lower() != "no_restock"

                sid, pid, motif = self._resoudre_ligne_stock(
                    c, li.get("sku"), li.get("title"), self._extraire_taille(li),
                    variant_id=li.get("variant_id"), barcode=li.get("barcode"),
                )
                if not sid:
                    logger.warning(
                        f"[REMBOURSEMENT] Commande #{order_number} : ligne « {li.get('title')} » "
                        f"non rattachée à une déclinaison locale ({motif or 'INTROUVABLE'}) — "
                        f"remboursement non enregistré pour cette ligne, à traiter à la main."
                    )
                    self._marquer_audit(c, None, pid)
                    continue

                reste = quantite
                for vd_id, remboursable in self._lignes_remboursables(c, ticket_id, sid):
                    if reste <= 0:
                        break
                    part = min(reste, remboursable)
                    num_ref, total = enregistrer_remboursement(
                        c, numero_ticket, vd_id, sid, None, "Shopify", "Shopify Sync",
                        date_heure, quantite=part, caisse_id=caisse_id,
                        recrediter_stock=recrediter,
                    )
                    tickets_crees.append(num_ref)
                    # Le remboursement a eu lieu EN LIGNE : Shopify a déjà appliqué son propre
                    # recrédit (`restock_type`), ou délibérément pas (`no_restock`). Ce ticket
                    # porte une ligne `Ventes_Details` négative ; laissé à `synced_shopify = 0`,
                    # il serait ramassé par la passe montante, qui pousserait `-(-1) = +1` vers
                    # Shopify. L'ajustement d'inventaire est RELATIF : l'écart ne se rattrape
                    # jamais, et en `no_restock` il crée une unité vendable qui n'existe pas.
                    # Même principe que les commandes importées, insérées à `synced_shopify = 1`.
                    c.execute("UPDATE Tickets SET synced_shopify = 1 WHERE numero_ticket = ?",
                              (num_ref,))
                    montant_total += Decimal(str(total))
                    reste -= part

                if reste > 0:
                    # Shopify rembourse plus d'unités que la vente locale n'en porte encore :
                    # on ne force RIEN, on le dit. Inventer la quantité manquante ferait
                    # remonter un stock qui n'a jamais été vendu ici.
                    logger.warning(
                        f"[REMBOURSEMENT] Commande #{order_number} : {reste} unité(s) de "
                        f"« {li.get('title')} » non remboursables localement (déjà remboursées "
                        f"ou jamais décomptées) — écart à vérifier."
                    )
                    self._marquer_audit(c, sid, pid)

            c.execute(
                "INSERT INTO Shopify_Remboursements (cle, order_id, refund_id, tickets, montant, date_traitement) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cle, str(order.get("id")), str(remb.get("id")),
                 ",".join(tickets_crees), str(montant_total), date_heure),
            )
            conn.commit()
            _sceller_tickets(conn, tickets_crees)
            if tickets_crees:
                logger.info(
                    f"Remboursement Shopify {remb.get('id')} (commande #{order_number}) enregistré : "
                    f"{', '.join(tickets_crees)} pour {montant_total} €."
                )
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def _signaler_annulation(self, conn, order, cle, ticket_id, numero_ticket, a_des_remboursements):
        """
        Une commande annulée est signalée, jamais remboursée d'office.

        Shopify permet d'annuler SANS rembourser (paiement conservé, arrangement hors caisse).
        Fabriquer ici un remboursement reviendrait à inventer un mouvement d'argent qui n'a pas
        eu lieu et à le signer dans la chaîne fiscale. Le remboursement, quand il existe, arrive
        par `refunds` et a déjà été traité ci-dessus. Il reste à le rendre VISIBLE.
        """
        order_number = order.get("order_number", order.get("id"))
        c = _ouvrir_ecriture(conn)
        try:
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if c.fetchone():
                conn.rollback()
                return
            date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not a_des_remboursements:
                c.execute(
                    "SELECT DISTINCT s.id, s.id_produit FROM Ventes_Details vd "
                    "JOIN Stocks s ON s.id = vd.id_stock WHERE vd.id_ticket = ? AND vd.quantite > 0",
                    (ticket_id,),
                )
                for sid, pid in c.fetchall():
                    self._marquer_audit(c, sid, pid)
                logger.warning(
                    f"[ANNULATION] Commande Shopify #{order_number} annulée sans remboursement : "
                    f"le ticket {numero_ticket} reste en l'état (aucune écriture inventée). "
                    f"Ses articles sont marqués à vérifier."
                )
            else:
                logger.info(
                    f"[ANNULATION] Commande Shopify #{order_number} annulée : "
                    f"ses remboursements ont été enregistrés sur le ticket {numero_ticket}."
                )
            c.execute(
                "INSERT INTO Shopify_Remboursements (cle, order_id, refund_id, tickets, montant, date_traitement) "
                "VALUES (?, ?, NULL, ?, '0.00', ?)",
                (cle, str(order.get("id")), numero_ticket, date_heure),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # --- Commandes en ligne hors caisse : stock seulement ------------------------------------

    def _appliquer_remboursements_hors_caisse(self, conn, order, order_id, remboursements, annulee) -> int:
        """
        Remboursements d'une commande importée HORS caisse : aucune écriture financière (la
        comptabilité du web est dans Shopify), seulement le stock — rendu ou non selon `restock_type`,
        et jamais au-delà de ce que l'import avait décompté.
        """
        c = conn.cursor()
        traites = 0
        for remb in remboursements:
            cle = f"refund:{remb.get('id')}"
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if c.fetchone():
                continue
            if self._traiter_remboursement_hors_caisse(conn, order, remb, cle, order_id):
                traites += 1

        if annulee:
            cle = f"annulation:{order_id}"
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if not c.fetchone():
                self._signaler_annulation_hors_caisse(conn, order, cle, order_id, bool(remboursements))
        return traites

    def _traiter_remboursement_hors_caisse(self, conn, order, remb, cle, order_id) -> bool:
        order_number = order.get("order_number", order.get("id"))
        c = _ouvrir_ecriture(conn)
        try:
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if c.fetchone():
                conn.rollback()
                return False

            date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rendues = 0
            for rli in remb.get("refund_line_items") or []:
                quantite = int(rli.get("quantity") or 0)
                if quantite <= 0:
                    continue
                li = rli.get("line_item") or {}
                recrediter = str(rli.get("restock_type") or "").strip().lower() != "no_restock"

                sid, pid, motif = self._resoudre_ligne_stock(
                    c, li.get("sku"), li.get("title"), self._extraire_taille(li),
                    variant_id=li.get("variant_id"), barcode=li.get("barcode"),
                )
                if not sid:
                    logger.warning(
                        f"[REMBOURSEMENT] Commande #{order_number} : ligne « {li.get('title')} » "
                        f"non rattachée à une déclinaison locale ({motif or 'INTROUVABLE'}) — "
                        f"stock non rendu pour cette ligne, à traiter à la main."
                    )
                    self._marquer_audit(c, None, pid)
                    continue

                reste = quantite
                c.execute(
                    "SELECT id, quantite - restituee FROM Shopify_Commandes_Lignes "
                    "WHERE order_id = ? AND stock_id = ? AND quantite - restituee > 0 ORDER BY id ASC",
                    (order_id, sid),
                )
                for ligne_id, disponible in c.fetchall():
                    if reste <= 0:
                        break
                    part = min(reste, int(disponible))
                    c.execute("UPDATE Shopify_Commandes_Lignes SET restituee = restituee + ? WHERE id = ?",
                              (part, ligne_id))
                    # `no_restock` : la cliente est remboursée mais l'article ne revient pas au stock
                    # vendable (une unité fantôme serait ensuite poussée vers Shopify et revendue).
                    if recrediter:
                        c.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle + ? WHERE id = ?",
                                  (part, sid))
                        rendues += part
                    reste -= part

                if reste > 0:
                    logger.warning(
                        f"[REMBOURSEMENT] Commande #{order_number} : {reste} unité(s) de "
                        f"« {li.get('title')} » non rendables localement (déjà rendues ou jamais "
                        f"décomptées) — écart à vérifier."
                    )
                    self._marquer_audit(c, sid, pid)

            c.execute(
                "INSERT INTO Shopify_Remboursements (cle, order_id, refund_id, tickets, montant, date_traitement) "
                "VALUES (?, ?, ?, '', '0.00', ?)",
                (cle, str(order.get("id")), str(remb.get("id")), date_heure),
            )
            conn.commit()
            logger.info(f"Remboursement Shopify {remb.get('id')} (commande #{order_number}, hors caisse) : "
                        f"{rendues} unité(s) remise(s) en stock.")
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def _signaler_annulation_hors_caisse(self, conn, order, cle, order_id, a_des_remboursements):
        """Annulation d'une commande hors caisse : jamais d'écriture inventée, articles à vérifier si rien n'a été rendu."""
        order_number = order.get("order_number", order.get("id"))
        c = _ouvrir_ecriture(conn)
        try:
            c.execute("SELECT 1 FROM Shopify_Remboursements WHERE cle = ?", (cle,))
            if c.fetchone():
                conn.rollback()
                return
            date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not a_des_remboursements:
                c.execute(
                    "SELECT DISTINCT l.stock_id, l.produit_id FROM Shopify_Commandes_Lignes l WHERE l.order_id = ?",
                    (order_id,),
                )
                for sid, pid in c.fetchall():
                    self._marquer_audit(c, sid, pid)
                logger.warning(
                    f"[ANNULATION] Commande Shopify #{order_number} annulée sans remboursement : "
                    f"le stock décompté n'est pas rendu d'office, ses articles sont marqués à vérifier."
                )
            c.execute(
                "INSERT INTO Shopify_Remboursements (cle, order_id, refund_id, tickets, montant, date_traitement) "
                "VALUES (?, ?, NULL, '', '0.00', ?)",
                (cle, str(order.get("id")), date_heure),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # --- Import du catalogue ----------------------------------------------------------------

    def _fetch_all_products(self, progress_callback=None):
        """
        Tous les produits Shopify. L'API n'en renvoie que 250 par requête : on pagine avec `since_id`
        (avant, tout ce qui dépassait les 250 premiers produits était ignoré sans avertissement).
        Retourne None si la toute première page échoue ; lève une erreur si une page suivante échoue
        (un catalogue tronqué ne doit jamais être présenté comme un import réussi).
        """
        produits, since_id = [], 0
        for page_no in range(1, self.MAX_PAGES + 1):
            endpoint = f"products.json?limit={self.PAGE_SIZE}"
            if since_id:
                endpoint += f"&since_id={since_id}"
            data = self.make_request(endpoint)
            if not data or "products" not in data:
                if page_no == 1:
                    return None
                raise RuntimeError(
                    f"Récupération Shopify interrompue à la page {page_no} : import annulé, aucune donnée modifiée."
                )
            page = data["products"]
            produits.extend(page)
            if progress_callback:
                progress_callback(f"Récupération des produits Shopify ({len(produits)})...", 10)
            last_id = page[-1].get("id") if page else None
            if len(page) < self.PAGE_SIZE or not last_id or last_id == since_id:
                break
            since_id = last_id
        return produits

    @staticmethod
    def _nettoyer_code(valeur):
        """Code-barres/SKU nettoyé avec l'outil maison du catalogue (caractères de contrôle, espaces)."""
        try:
            from kodo_core.domain.catalog.inventory_manager import InventoryManager
            return InventoryManager.clean_barcode(valeur)
        except Exception:
            texte = str(valeur or "").strip()
            return texte or None

    def _retrouver_produit(self, c, variant_id, code, code_brut):
        """
        Retrouve le produit local correspondant à une variante Shopify, dans cet ordre :
        1. la correspondance d'id de variante (clé stable, insensible aux renommages de SKU) ;
        2. le code-barres nettoyé, puis le code brut (bases synchronisées avant le nettoyage) ;
        3. l'ancienne clé fabriquée `SHPF-<variant_id>`, pour adopter sans le dupliquer un produit
           importé par les versions précédentes.
        """
        if variant_id:
            c.execute("SELECT id_produit FROM Shopify_Variantes WHERE variant_id = ?", (variant_id,))
            row = c.fetchone()
            if row:
                c.execute("SELECT id FROM Produits WHERE id = ?", (row[0],))
                if c.fetchone():
                    return row[0]

        for cle in (code, code_brut):
            if cle:
                c.execute("SELECT id FROM Produits WHERE code_barre = ?", (cle,))
                row = c.fetchone()
                if row:
                    return row[0]

        if variant_id:
            c.execute("SELECT id FROM Produits WHERE code_barre = ?", (f"SHPF-{variant_id}",))
            row = c.fetchone()
            if row:
                return row[0]
        return None

    @staticmethod
    def _taille_variante(v) -> str:
        """Libellé de taille d'une variante Shopify (« Unique » quand elle n'est pas déclinée)."""
        opt1 = v.get("option1", "Unique")
        opt2 = v.get("option2")
        taille = opt1 if opt1 and opt1 != "Default Title" else "Unique"
        if opt2 and opt2 != "Default Title":
            taille = f"{taille} / {opt2}"
        return taille

    def _requalifier_ligne_unique(self, c, pid, tailles) -> None:
        """
        Article local sans déclinaison (une seule ligne « Taille Unique ») rapproché d'un produit
        Shopify décliné : la ligne devient la première taille au lieu de rester en plus des
        nouvelles, sinon son stock serait compté en double (et son SKU `…-Unique` n'existe pas en
        ligne). Elle garde son id, donc son historique de ventes.
        """
        if not tailles or any(self._est_taille_unique(t) for t in tailles):
            return
        c.execute("SELECT id, taille FROM Stocks WHERE id_produit=?", (pid,))
        lignes = c.fetchall()
        if len(lignes) == 1 and self._est_taille_unique(lignes[0][1]):
            c.execute("UPDATE Stocks SET taille=? WHERE id=?", (tailles[0], lignes[0][0]))

    def _produits_locaux_du_produit(self, c, nom, variantes) -> list:
        """
        Ids des produits locaux qui correspondent à UN produit Shopify, le plus ancien d'abord
        (c'est lui qui est conservé : il porte le code-barres imprimé, la TVA et la marque).

        Plusieurs ids = l'article a été dédoublé par une version précédente (une fiche par taille) :
        l'appelant les refond. Clés, de la plus sûre à la moins sûre : id de variante, code-barres
        ou SKU, SKU de déclinaison exporté (`BI038-M` → `BI038`), enfin le nom, seulement s'il est
        unique et ne désigne pas déjà un AUTRE produit Shopify.
        """
        trouves = []

        def ajouter(pid):
            if pid and pid not in trouves:
                trouves.append(pid)

        for v, code, code_brut, variant_id in variantes:
            ajouter(self._retrouver_produit(c, variant_id, code, code_brut))
            if code_brut:
                for prefixe in prefixes_possibles(code_brut):
                    c.execute("SELECT id FROM Produits WHERE code_barre = ?", (prefixe,))
                    row = c.fetchone()
                    if row:
                        ajouter(row[0])
                        break

        if not trouves and nom:
            c.execute("SELECT id FROM Produits WHERE nom = ? LIMIT 2", (nom,))
            rows = c.fetchall()
            if len(rows) == 1:
                ids_variantes = {vid for _, _, _, vid in variantes if vid}
                c.execute("SELECT variant_id FROM Shopify_Variantes WHERE id_produit = ?", (rows[0][0],))
                connues = {r[0] for r in c.fetchall()}
                if not connues or connues & ids_variantes:
                    ajouter(rows[0][0])

        trouves.sort()
        return trouves

    @staticmethod
    def _code_du_produit(c, variantes):
        """Code-barres du produit créé : le code commun des tailles (`BI038` pour `BI038-S`, `BI038-M`), sinon celui de la première variante libre."""
        codes = [cb for _, cb, _, _ in variantes if cb]
        if not codes:
            return None
        candidats = []
        if len(codes) > 1:
            communs = set(prefixes_possibles(codes[0]))
            for autre in codes[1:]:
                communs &= set(prefixes_possibles(autre))
            candidats = sorted(communs, key=len, reverse=True)
        candidats += codes
        for cand in candidats:
            c.execute("SELECT 1 FROM Produits WHERE code_barre = ?", (cand,))
            if not c.fetchone():
                return cand
        return None

    @staticmethod
    def _ligne_de_stock_referencee(c, stock_id) -> bool:
        """Vrai si une table (ventes, mouvements…) est accrochée à cette ligne de stock."""
        for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if table == "Stocks":
                continue
            cols = [r[1] for r in c.execute(f'PRAGMA table_info("{table}")').fetchall()]
            if "id_stock" in cols and c.execute(
                    f'SELECT 1 FROM "{table}" WHERE id_stock=? LIMIT 1', (stock_id,)).fetchone():
                return True
        return False

    def _fusionner_produit(self, c, garder, absorbe) -> None:
        """
        Refond le produit `absorbe` (doublon par taille) dans `garder`, sans perdre d'historique.

        Chaque ligne de stock du doublon est rattachée au produit conservé ; si celui-ci a déjà
        la même taille, la ligne du doublon disparaît (sa quantité est une copie du stock Shopify,
        recopiée juste après par l'import : l'additionner doublerait le stock) — sauf si des
        ventes y sont accrochées, auquel cas elle est gardée à 0.
        """
        c.execute("SELECT id, taille FROM Stocks WHERE id_produit=? ORDER BY id", (garder,))
        gardees = c.fetchall()
        c.execute("SELECT id, taille FROM Stocks WHERE id_produit=? ORDER BY id", (absorbe,))
        for sid, taille in c.fetchall():
            voulue = str(taille or "").strip().casefold()
            cible = next((g[0] for g in gardees if str(g[1] or "").strip().casefold() == voulue), None)
            if cible is None and self._est_taille_unique(taille):
                cible = next((g[0] for g in gardees if self._est_taille_unique(g[1])), None)
            if cible is None:
                c.execute("UPDATE Stocks SET id_produit=? WHERE id=?", (garder, sid))
                gardees.append((sid, taille))
                continue
            # Les lignes de vente sont SCELLÉES (piste d'audit) : on ne peut pas les réattribuer à
            # une autre ligne de stock. Une ligne déjà vendue est donc gardée, rattachée au produit
            # conservé et ramenée à 0 (même règle que `_ecrire_lignes_de_stock`) ; une ligne
            # jamais vendue disparaît.
            if self._ligne_de_stock_referencee(c, sid):
                c.execute("UPDATE Stocks SET id_produit=?, quantite_actuelle=0 WHERE id=?", (garder, sid))
                continue
            c.execute("DELETE FROM Stocks WHERE id=?", (sid,))
        for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if table in ("Stocks", "Produits"):
                continue
            cols = [r[1] for r in c.execute(f'PRAGMA table_info("{table}")').fetchall()]
            if "id_produit" in cols:
                c.execute(f'UPDATE "{table}" SET id_produit=? WHERE id_produit=?', (garder, absorbe))
        c.execute("DELETE FROM Produits WHERE id=?", (absorbe,))

    # Sauvegardes « avant import » gardées à côté des sauvegardes de migration (les plus anciennes
    # sont supprimées au-delà) : une copie intégrale de la base, quelques centaines de Ko chacune.
    SAUVEGARDES_IMPORT_CONSERVEES = 10

    @staticmethod
    def _chemin_base(conn) -> str:
        """Fichier SQLite réellement ouvert par `conn` ('' pour une base en mémoire)."""
        chemin = getattr(conn, "db_path", "") or ""
        if chemin:
            return str(chemin)
        try:
            for ligne in conn.cursor().execute("PRAGMA database_list").fetchall():
                if ligne[1] == "main":
                    return str(ligne[2] or "")
        except Exception:
            pass
        return ""

    def _sauvegarder_avant_import(self) -> str:
        """
        Copie vérifiée de la base juste avant que l'import n'écrase des quantités.

        Un import remplace des centaines de quantités d'un coup ; sans point de retour, la seule
        trace de l'état d'avant était la mémoire de la commerçante. La copie passe par l'API de
        sauvegarde SQLite (cohérente en mode WAL) et est relue avant d'être retenue. Si elle
        échoue, l'import est annulé : on n'écrase pas sans pouvoir revenir en arrière.
        Retourne le chemin de la copie, ou '' quand il n'y a aucun fichier à copier.
        """
        conn = get_connection()
        try:
            source = self._chemin_base(conn)
        finally:
            conn.close()
        if not source or not os.path.exists(source):
            return ""
        try:
            from kodo_core.config import ShopConfig
            from kodo_core.db.sanctuary_shield import copier_base_sqlite
            dossier = ShopConfig.get_snapshots_dir()
            horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            cible = os.path.join(dossier, f"kodo_pos_pre_import_{horodatage}.db")
            copier_base_sqlite(source, cible)
        except Exception as exc:
            raise RuntimeError(
                f"Sauvegarde de sécurité impossible avant l'import ({exc}). "
                "Import annulé : aucune quantité n'a été modifiée."
            ) from exc
        self._purger_sauvegardes_import(dossier)
        return cible

    def _purger_sauvegardes_import(self, dossier) -> None:
        try:
            noms = sorted(n for n in os.listdir(dossier)
                          if n.startswith("kodo_pos_pre_import_") and n.endswith(".db"))
            for ancien in noms[:-self.SAUVEGARDES_IMPORT_CONSERVEES]:
                os.remove(os.path.join(dossier, ancien))
        except OSError as exc:
            logger.warning(f"Purge des anciennes sauvegardes d'import impossible : {exc}")

    @staticmethod
    def _message_ventes_non_envoyees(bilan) -> str:
        lignes, tickets = bilan["lignes"], bilan["tickets"]
        if bilan["premier"] == bilan["dernier"]:
            portee = f"le ticket {bilan['premier']}"
        else:
            portee = f"{tickets} tickets, du {bilan['premier']} au {bilan['dernier']}"
        texte = (
            f"Import du catalogue refusé : {lignes} ligne(s) de vente ({portee}) n'ont pas encore "
            "été envoyées à Shopify. Shopify ne les contient pas : l'import recopierait ses anciennes "
            "quantités par-dessus celles de la caisse et ferait réapparaître en stock des articles "
            "déjà vendus. Cliquez sur « Rafraîchir la synchronisation », attendez qu'elle réussisse, "
            "puis relancez l'import."
        )
        derniere = str(lire_etat_sync().get("message") or "").strip()
        if derniere:
            texte += f" Dernier message de la synchronisation : {derniere}"
        return texte

    @staticmethod
    def _message_stock_non_envoye(lignes) -> str:
        texte = (
            f"Import du catalogue refusé : {lignes} ligne(s) de stock saisie(s) à la main dans Kōdo "
            "n'ont pas encore été envoyées à la boutique en ligne. L'import recopierait les anciennes "
            "quantités du site par-dessus celles de la caisse et effacerait ces corrections. "
            "Cliquez sur « Rafraîchir la synchronisation », attendez qu'elle réussisse, puis "
            "relancez l'import."
        )
        derniere = str(lire_etat_sync().get("message") or "").strip()
        if derniere:
            texte += f" Dernier message de la synchronisation : {derniere}"
        return texte

    def _etat_avant_import(self, forcer):
        """
        Ce qui conditionne un import sûr, lu AVANT tout appel réseau, dans une courte transaction :
        les ventes que Shopify ne connaît pas encore, et l'empreinte d'activité (voir
        `_signature_activite`) à comparer au moment d'écrire. Retourne (signature, non_envoyees).

        Le refus ne vaut que si la poussée des ventes est allumée : poussée éteinte, ces ventes ne
        partiront jamais et Shopify fait volontairement autorité.
        """
        conn = get_connection()
        try:
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            non_envoyees = ventes_non_envoyees(c)
            a_aligner = shopify_alignement.compter_a_aligner(c)
            signature = _signature_activite(c)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if self.auto_sync and non_envoyees["lignes"] and not forcer:
            raise ImportShopifyBloque(self._message_ventes_non_envoyees(non_envoyees),
                                      "ventes_non_envoyees", non_envoyees)
        if self.auto_sync and a_aligner and not forcer:
            raise ImportShopifyBloque(self._message_stock_non_envoye(a_aligner),
                                      "stock_non_envoye", {"lignes": a_aligner, "tickets": 0})
        return signature, non_envoyees

    def import_catalog(self, progress_callback=None, forcer: bool = False) -> int:
        """
        Importe le catalogue complet de Shopify vers la base de données locale Kōdo POS.

        Un produit déjà connu (même variante Shopify, même code-barres/SKU) est MIS À JOUR sur place :
        son id, sa TVA, sa marque, son seuil d'alerte, son image, son prix d'achat ET son code-barres
        sont conservés, et ses lignes de stock existantes sont réutilisées. Avant, `INSERT OR REPLACE`
        supprimait puis recréait le produit sous un nouvel id à chaque import (stocks orphelins,
        réglages remis à zéro, écran de caisse pointant sur des ids morts).

        Ce qui est VOLONTAIREMENT écrasé à chaque import : le nom, la catégorie et les prix de vente
        (normal et soldé). Shopify fait autorité sur la vitrine ; le reste appartient à la boutique.

        Les QUANTITÉS sont recopiées depuis Shopify, ce qui efface des ventes que Shopify ne connaît
        pas encore. Trois garde-fous, nés d'un cas client où le stock « remontait » :
        - refus (`ImportShopifyBloque`) tant que des ventes n'ont pas été envoyées à Shopify, sauf
          `forcer=True` (réservé au support) ;
        - copie de sécurité de la base juste avant d'écrire, et import annulé si elle échoue ;
        - refus si une vente ou une poussée a eu lieu PENDANT le téléchargement du catalogue : les
          quantités reçues sont alors périmées. Rien n'est modifié dans ces trois cas.
        Chaque quantité modifiée est consignée au journal de stock (`Stock_Journal`) ; le bilan de
        l'import est laissé dans `self.dernier_import`. Retourne le nombre de variantes importées.
        """
        self.load_config()
        if not self.est_configure():
            raise ValueError("Configuration Shopify manquante (URL ou Token).")

        self.dernier_import = {"echec": False}
        logger.info("Début de l'importation du catalogue...")

        # Le refus se décide avant tout appel réseau : inutile de télécharger un catalogue qu'on
        # n'a pas le droit d'appliquer.
        signature, non_envoyees = self._etat_avant_import(forcer)

        if progress_callback:
            progress_callback("Récupération des produits Shopify...", 10)

        shopify_products = self._fetch_all_products(progress_callback)
        if shopify_products is None:
            logger.warning("Aucun produit trouvé sur Shopify ou erreur de connexion.")
            # `0` reste la valeur de retour (contrat historique), mais l'échec est consigné : la
            # route ne doit pas annoncer « importation réussie : 0 variante » sur un refus du serveur.
            self.dernier_import = {"echec": True, "nature": self.dernier_echec,
                                   "detail": getattr(self, "dernier_echec_detail", "") or ""}
            return 0

        total_p = len(shopify_products)
        logger.info(f"{total_p} produits récupérés de Shopify.")

        sauvegarde = self._sauvegarder_avant_import()

        conn = get_connection()
        imported_count = modifiees = creees = 0
        try:
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            # Dernier contrôle DANS la transaction d'écriture (plus aucune vente ne peut s'y glisser).
            if _signature_activite(c) != signature:
                raise ImportShopifyBloque(
                    "Une vente ou une synchronisation Shopify a eu lieu pendant le téléchargement du "
                    "catalogue : les quantités reçues sont périmées. Par sécurité, rien n'a été modifié. "
                    "Relancez l'import.", "activite_pendant_import")
            maintenant = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for idx, p in enumerate(shopify_products):
                if progress_callback:
                    pct = 10 + int((idx / total_p) * 80)
                    progress_callback(f"Importation : {p.get('title')} ({idx+1}/{total_p})...", pct)

                nom = p.get("title", "Sans nom")
                cat = p.get("product_type", "Général") or "Général"

                c.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))

                # UN produit Shopify = UN produit local, ses variantes devenant les lignes de stock
                # (tailles). Avant, chaque variante créait son propre produit : « Robe » en S/M/L
                # devenait trois produits « Robe » à une taille, à côté de l'article d'origine,
                # et le stock comme sa valeur étaient comptés plusieurs fois.
                variantes = []
                for v in p.get("variants", []):
                    # Le SKU reste prioritaire sur le code-barres : c'est la clé avec laquelle les
                    # bases déjà synchronisées ont été écrites, en changer l'ordre dédoublerait tout
                    # leur catalogue au premier import (voir le rapport : migration à part entière).
                    code_brut = str(v.get("sku") or v.get("barcode") or "").strip() or None
                    code = self._nettoyer_code(v.get("sku")) or self._nettoyer_code(v.get("barcode"))
                    try:
                        variant_id = int(v.get("id"))
                    except (TypeError, ValueError):
                        variant_id = None
                    if not code and not variant_id:
                        continue
                    variantes.append((v, code, code_brut, variant_id))
                if not variantes:
                    continue

                # Prix : ceux de la première variante portent le produit (les tailles d'un même
                # article se vendent au même prix ; un prix par taille n'existe pas côté Kōdo).
                v0 = variantes[0][0]
                try:
                    # `str()` d'abord : Shopify envoie ses prix en texte, mais rien ne le
                    # garantit et `Decimal(3.30)` scellerait la valeur binaire du flottant
                    # (3.2999999999999998...) au lieu du prix écrit.
                    price_val = Decimal(str(v0.get("price", "0.00")))
                except Exception:
                    price_val = Decimal("0.00")

                en_solde = 0
                prix_solde_tvac = None
                prix_vente_tvac = price_val
                if v0.get("compare_at_price"):
                    try:
                        compare_val = Decimal(str(v0.get("compare_at_price")))
                        if compare_val > price_val:
                            en_solde = 1
                            prix_vente_tvac = compare_val
                            prix_solde_tvac = price_val
                    except Exception:
                        pass

                locaux = self._produits_locaux_du_produit(c, nom, variantes)
                if locaux:
                    pid = locaux[0]
                    # Un article déjà dédoublé par d'anciennes versions est refondu en un seul.
                    for doublon in locaux[1:]:
                        self._fusionner_produit(c, pid, doublon)
                    # Le code-barres n'est JAMAIS réécrit : une étiquette déjà imprimée et collée
                    # en rayon doit continuer de désigner le même article.
                    c.execute("""
                        UPDATE Produits
                        SET nom=?, categorie=?, prix_vente_tvac=?, en_solde=?, prix_solde_tvac=?
                        WHERE id=?
                    """, (nom, cat, prix_vente_tvac, en_solde, prix_solde_tvac, pid))
                else:
                    # `prix_achat_htva` reste NULL : le prix de vente divisé par 2,5 était une
                    # marge INVENTÉE, qui alimentait ensuite les statistiques et les rapports
                    # comme une donnée comptable. Shopify ne transmet pas le prix d'achat ;
                    # tant que la commerçante ne l'a pas saisi, il n'existe pas.
                    # Le code-barres reste NULL quand aucune variante n'en porte : fabriquer
                    # un « SHPF-<id> » produisait une valeur non scannable et impossible à
                    # imprimer. La correspondance de variante suffit à retrouver le produit.
                    c.execute("""
                        INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, en_solde, prix_solde_tvac)
                        VALUES (?, ?, ?, NULL, ?, ?, ?)
                    """, (self._code_du_produit(c, variantes), nom, cat, prix_vente_tvac, en_solde, prix_solde_tvac))
                    pid = c.lastrowid

                self._requalifier_ligne_unique(c, pid, [self._taille_variante(v) for v, _, _, _ in variantes])

                for v, code, code_brut, variant_id in variantes:
                    if variant_id:
                        c.execute("""
                            INSERT INTO Shopify_Variantes (variant_id, id_produit, date_maj) VALUES (?, ?, ?)
                            ON CONFLICT(variant_id) DO UPDATE SET id_produit=excluded.id_produit, date_maj=excluded.date_maj
                        """, (variant_id, pid, maintenant))

                    taille = self._taille_variante(v)

                    qty = int(v.get("inventory_quantity") or 0)

                    c.execute("SELECT id, taille FROM Stocks WHERE id_produit=? ORDER BY id", (pid,))
                    stock_rows = c.fetchall()
                    wanted = str(taille).strip().casefold()
                    cible = next((r[0] for r in stock_rows if str(r[1] or "").strip().casefold() == wanted), None)
                    if cible is None and self._est_taille_unique(taille):
                        cible = next((r[0] for r in stock_rows if self._est_taille_unique(r[1])), None)
                    if cible is not None:
                        c.execute("SELECT quantite_actuelle FROM Stocks WHERE id=?", (cible,))
                        ancienne = c.fetchone()
                        avant = ancienne[0] if ancienne else None
                        c.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (qty, cible))
                        if avant != qty:
                            modifiees += 1
                        stock_journal.noter(c, cible, avant, qty, stock_journal.ORIGINE_IMPORT_SHOPIFY,
                                            produit_id=pid, produit=nom, taille=taille,
                                            detail="quantité recopiée depuis Shopify")
                    else:
                        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, NULL)", (pid, taille, qty))
                        creees += 1
                        stock_journal.noter(c, c.lastrowid, None, qty, stock_journal.ORIGINE_IMPORT_SHOPIFY,
                                            produit_id=pid, produit=nom, taille=taille,
                                            detail="taille créée par l'import Shopify")

                    imported_count += 1

            # Kōdo vient de copier le stock en ligne : les ventes antérieures y sont déjà comptées,
            # et les quantités saisies à la main (qu'un import forcé vient d'écraser) n'ont plus
            # rien à aligner.
            c.execute(
                f"UPDATE {shopify_alignement.TABLE} SET statut = ?, derniere_erreur = ?, date_maj = ? "
                "WHERE statut = ?",
                (shopify_alignement.STATUT_ALIGNE, "remplacé par l'import du catalogue", maintenant,
                 shopify_alignement.STATUT_A_ALIGNER))
            noter_alignement_stock(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.dernier_import = {
            "echec": False,
            "variantes": imported_count,
            "modifiees": modifiees,
            "creees": creees,
            "sauvegarde": sauvegarde,
            # Ventes non envoyées effacées du stock parce que le support a forcé l'import.
            "ecrasees": non_envoyees["lignes"] if (forcer and self.auto_sync) else 0,
        }
        if progress_callback:
            progress_callback("Importation terminée !", 100)
        logger.info(f"Importation terminée : {imported_count} variantes importées, {modifiees} quantité(s) modifiée(s).")
        return imported_count


_VERROU_PASSE = threading.Lock()


class ShopifySyncThread(threading.Thread):
    """Thread d'arrière-plan gérant la synchronisation periodique Shopify."""

    INTERVALLE_BASE_S = 60
    INTERVALLE_MAX_S = 900  # 15 min : plafond du recul exponentiel

    def __init__(self, store_url: str = "", access_token: str = "", intervalle_s: int = None):
        super().__init__()
        self.daemon = True
        self.name = "KodoShopifySync"
        self.running = True
        self.engine = ShopifySync(store_url=store_url, access_token=access_token)
        self.intervalle_base_s = int(intervalle_s or self.INTERVALLE_BASE_S)
        self.echecs_consecutifs = 0
        # Réveil interruptible : avec un `time.sleep(60)`, l'arrêt de l'application attendait
        # jusqu'à une minute avant que le thread ne veuille bien mourir.
        self._reveil = threading.Event()

    @property
    def store_url(self):
        return self.engine.store_url

    @store_url.setter
    def store_url(self, val):
        self.engine.store_url = val

    @property
    def access_token(self):
        return self.engine.access_token

    @access_token.setter
    def access_token(self, val):
        self.engine.access_token = val

    def _load_config(self):
        self.engine.load_config()

    def _make_request(self, endpoint, method="GET", data=None):
        return self.engine.make_request(endpoint, method=method, data=data)

    def _get_shopify_location_id(self):
        return self.engine.get_location_id()

    def _find_inventory_item_id(self, sku):
        return self.engine.find_inventory_item_id(sku)

    def _adjust_shopify_stock(self, inventory_item_id, location_id, qty_change):
        return self.engine.adjust_shopify_stock(inventory_item_id, location_id, qty_change)

    def _sync_tickets_to_shopify(self):
        return self.engine.sync_tickets_to_shopify()

    def _sync_orders_from_shopify(self):
        return self.engine.sync_orders_from_shopify()

    def stop(self, timeout: float = 5.0):
        """Demande l'arrêt du thread et attend sa sortie."""
        self.running = False
        self._reveil.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=timeout)

    def executer_une_passe(self) -> bool:
        """
        Une passe complète de synchronisation. Retourne False si un échec justifie un recul.

        La configuration est relue à CHAQUE passe : allumer un interrupteur dans les réglages
        prend effet au cycle suivant, sans redémarrer l'application.
        """
        self.engine.load_config()
        if not self.engine.est_configure():
            # Rien à faire n'est pas un échec : pas de recul exponentiel pour une boutique
            # simplement pas encore configurée.
            return True

        tout_va_bien = True
        incidents = []
        tickets = commandes = remboursements = 0
        alignement = {}
        # Deux affectations simples (et non un appel de méthode) : le moteur peut être substitué.
        self.engine.dernier_echec = None
        self.engine.dernier_echec_detail = ""
        if self.engine.auto_sync:
            try:
                # Filet si la poussée est allumée pendant que le fil tourne déjà : jamais
                # l'historique. Sans effet une fois le seuil fixé.
                fixer_seuil_tickets()
                tickets = self.engine.sync_tickets_to_shopify()
            except Exception as e:
                logger.error(f"Poussée des ventes vers Shopify interrompue : {e}")
                incidents.append(f"ventes : {e}")
                tout_va_bien = False
        if self.engine.sync_orders:
            try:
                commandes = self.engine.sync_orders_from_shopify()
            except Exception as e:
                logger.error(f"Rapatriement des commandes Shopify interrompu : {e}")
                incidents.append(f"commandes : {e}")
                tout_va_bien = False
            try:
                # Même interrupteur que le rapatriement des commandes : un remboursement est
                # la suite de la vie d'une commande importée, pas un réglage à part. Il est
                # traité APRÈS l'import pour qu'une commande arrivée et remboursée dans le
                # même intervalle trouve son ticket d'origine dès la première passe.
                remboursements = self.engine.sync_refunds_from_shopify()
            except Exception as e:
                logger.error(f"Inspection des remboursements Shopify interrompue : {e}")
                incidents.append(f"remboursements : {e}")
                tout_va_bien = False
        if self.engine.auto_sync:
            try:
                # Une quantité saisie à la main sur la fiche produit ne part pas avec les ventes :
                # elle est alignée ici, après elles, pour que la valeur visée tienne compte de ce
                # qui vient d'être envoyé et des commandes web qui viennent d'être rapatriées.
                alignement = self.engine.aligner_lignes_modifiees()
                if alignement.get("en_echec"):
                    incidents.append(
                        f"stock saisi à la main : {alignement['en_echec']} ligne(s) pas encore "
                        "alignée(s) sur la boutique en ligne")
                    tout_va_bien = False
            except Exception as e:
                logger.error(f"Alignement du stock sur Shopify interrompu : {e}")
                incidents.append(f"alignement du stock : {e}")
                tout_va_bien = False
        if self.engine.dernier_echec:
            tout_va_bien = False
            cause = getattr(self.engine, "dernier_echec_detail", "") or ""
            nature = f"{self.engine.dernier_echec} — {cause}" if cause else self.engine.dernier_echec
            incidents.append(f"échec de communication ({nature}) avec {self.engine.domaine()}")

        message = " ; ".join(incidents) if incidents else (
            f"{tickets} ticket(s) poussé(s), {commandes} commande(s) rapatriée(s), "
            f"{remboursements} remboursement(s) repris vers {self.engine.domaine()}"
        )
        if alignement.get("ajustees") and not incidents:
            message += f" ; {alignement['ajustees']} ligne(s) de stock alignée(s) sur la caisse"
        # Version d'API : Shopify sert en silence une autre version quand celle demandée est
        # retirée. Le dire ici, tant que tout marche, laisse le temps d'une mise à jour.
        servie = str(getattr(self.engine, "version_api_servie", "") or "")
        demandee = str(getattr(self.engine, "api_version", "") or "")
        if servie and demandee and servie != demandee:
            message += (f" Attention : Shopify sert la version d'API {servie} et non {demandee} : "
                        "une mise à jour de Kōdo est à prévoir.")
        if servie:
            try:
                conn = get_connection()
                try:
                    c = _ouvrir_ecriture(conn)
                    c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                              (CLE_VERSION_SERVIE, servie))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                logger.error(f"Version d'API servie non enregistrée : {e}")
        enregistrer_etat_sync(tout_va_bien, message)
        return tout_va_bien

    def passe_exclusive(self) -> bool:
        """
        Une passe, jamais en même temps qu'une autre (boucle automatique et rafraîchissement
        manuel partagent ce verrou : deux passes simultanées pousseraient deux fois la même ligne).
        """
        with _VERROU_PASSE:
            return self.executer_une_passe()

    def run(self):
        logger.info("Démarrage du thread Shopify en arrière-plan...")
        while self.running:
            try:
                tout_va_bien = self.passe_exclusive()
            except Exception as e:
                logger.error(f"Passe de synchronisation Shopify interrompue : {e}")
                tout_va_bien = False

            # Recul exponentiel : marteler l'API toutes les 60 s pendant une panne (ou avec un
            # jeton révoqué) faisait tomber la boutique dans les plafonds de débit Shopify.
            self.echecs_consecutifs = 0 if tout_va_bien else min(self.echecs_consecutifs + 1, 8)
            delai = min(self.intervalle_base_s * (2 ** self.echecs_consecutifs), self.INTERVALLE_MAX_S)
            if not tout_va_bien:
                logger.warning(f"Synchronisation Shopify en échec : nouvelle tentative dans {delai} s.")

            if not self.running:
                break
            self._reveil.wait(delai)
            self._reveil.clear()
        logger.info("Thread Shopify arrêté.")

    def import_shopify_catalog(self, progress_callback=None):
        return self.engine.import_catalog(progress_callback=progress_callback)


# --- Démarrage / arrêt de la synchronisation automatique ------------------------------------

_thread_auto = None
_verrou_auto = threading.Lock()


def _tests_en_cours() -> bool:
    """
    Vrai si le processus est une exécution de tests.

    La synchronisation automatique ouvre des connexions réseau et écrit dans la base : elle ne
    doit jamais démarrer toute seule pendant la suite. `KODO_SHOPIFY_AUTOSYNC=1` permet aux tests
    qui visent précisément ce démarrage de lever le garde-fou.
    """
    if os.environ.get("KODO_SHOPIFY_AUTOSYNC") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules or "unittest" in sys.modules


def start_auto_sync(force: bool = False):
    """
    Démarre la synchronisation Shopify en arrière-plan si — et seulement si — elle a un sens.

    Ne démarre pas : en contexte de test, si l'URL ou le jeton manquent, si les deux interrupteurs
    (`shopify_auto_sync`, `shopify_sync_orders`) sont éteints, ou si un thread tourne déjà.
    Retourne le thread démarré (ou déjà en place), sinon None.
    """
    global _thread_auto
    if not force and _tests_en_cours():
        logger.info("Contexte de test détecté : synchronisation automatique Shopify non démarrée.")
        return None

    with _verrou_auto:
        reglages = lire_reglages_shopify()
        if reglages["store_url"] and identifiants_presents(reglages) and reglages["auto_sync"]:
            # Branchement de la poussée des ventes : l'historique déjà vendu n'est pas repoussé.
            # Sans effet si le seuil est déjà fixé.
            try:
                fixer_seuil_tickets()
            except Exception as e:
                logger.error(f"Point de départ de la synchro Shopify non fixé : {e}")
                return None

        if _thread_auto is not None and _thread_auto.is_alive():
            return _thread_auto

        if not reglages["store_url"] or not identifiants_presents(reglages):
            logger.info("Shopify non configuré (URL ou jeton absent) : synchronisation automatique non démarrée.")
            return None
        if not reglages["auto_sync"] and not reglages["sync_orders"]:
            logger.info("Interrupteurs Shopify éteints : synchronisation automatique non démarrée.")
            return None

        # Aucun identifiant n'est injecté : le moteur relit `Parametres` à chaque passe, donc
        # changer de boutique (ou effacer le jeton) prend effet sans redémarrer l'application.
        _thread_auto = ShopifySyncThread()
        _thread_auto.start()
        logger.info(
            "Synchronisation Shopify démarrée (stock : %s, commandes : %s).",
            "oui" if reglages["auto_sync"] else "non",
            "oui" if reglages["sync_orders"] else "non",
        )
        return _thread_auto


def stop_auto_sync(timeout: float = 5.0):
    """Arrête la synchronisation automatique si elle tourne."""
    global _thread_auto
    with _verrou_auto:
        thread = _thread_auto
        _thread_auto = None
    if thread is not None:
        thread.stop(timeout=timeout)
    return thread is not None


def auto_sync_actif() -> bool:
    """Vrai si le thread de synchronisation automatique tourne."""
    return _thread_auto is not None and _thread_auto.is_alive()


def reveiller_sync():
    """
    Réveille immédiatement le thread de synchronisation automatique Shopify
    afin de pousser les nouvelles ventes ou retours en temps réel (< 2 secondes)
    sans attendre l'échéance du cycle périodique de 60 secondes.
    """
    global _thread_auto
    if _thread_auto is not None and _thread_auto.is_alive():
        _thread_auto._reveil.set()


def compter_file_attente() -> dict:
    """Mouvements de stock encore à pousser (`pending`) ou à vérifier à la main (`indeterminate`)."""
    resultat = {"pending": 0, "indeterminate": 0}
    try:
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT statut, COUNT(*) FROM sync_queue WHERE statut IN (?, ?) GROUP BY statut",
                      (FILE_PENDING, FILE_INDETERMINE))
            for statut, n in c.fetchall():
                resultat["pending" if statut == FILE_PENDING else "indeterminate"] = int(n)
            # Une quantité saisie à la main que le site n'a pas encore rattrapée est, pour la
            # commerçante, un mouvement en attente comme un autre.
            resultat["pending"] += shopify_alignement.compter_a_aligner(c)
        finally:
            conn.close()
    except Exception:
        pass  # table absente (base ancienne) ou illisible : rien à signaler
    return resultat


def prochaine_tentative_file():
    """Plus proche horodatage de rejeu d'une ligne PENDING en recul exponentiel, ou None."""
    try:
        conn = get_connection()
        try:
            ligne = conn.cursor().execute(
                "SELECT MIN(prochaine_tentative) FROM sync_queue "
                "WHERE statut = ? AND prochaine_tentative IS NOT NULL", (FILE_PENDING,)).fetchone()
            return ligne[0] if ligne and ligne[0] else None
        finally:
            conn.close()
    except Exception:
        return None  # base d'avant la 2.1.2 (colonne absente) ou illisible


_etat_rafraichissement = {"en_cours": False, "debut": None, "fin": None, "ok": None, "message": ""}
_verrou_rafraichissement = threading.Lock()


def etat_rafraichissement() -> dict:
    with _verrou_rafraichissement:
        return dict(_etat_rafraichissement)


def _executer_rafraichissement():
    t0 = time.perf_counter()
    ok, message = False, ""
    try:
        thread = _thread_auto if (_thread_auto is not None and _thread_auto.is_alive()) else ShopifySyncThread()
        ok = bool(thread.passe_exclusive())
        etat = lire_etat_sync()
        message = etat.get("message") or ""
        file_attente = compter_file_attente()
        if file_attente["pending"]:
            message += f" — {file_attente['pending']} mouvement(s) en attente de connexion."
        ok = ok and etat.get("succes") is not False
    except Exception as e:
        logger.error(f"Rafraîchissement manuel Shopify interrompu : {e}")
        message = f"Rafraîchissement interrompu : {e}"
    finally:
        logger.info(f"[REFRESH] rafraîchissement manuel terminé en {int((time.perf_counter() - t0) * 1000)} ms (ok={ok})")
        with _verrou_rafraichissement:
            _etat_rafraichissement.update(en_cours=False, fin=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                          ok=ok, message=message)


def lancer_rafraichissement() -> dict:
    """
    Rafraîchissement manuel (bouton des Paramètres) : vide la file d'attente vers Shopify,
    rapatrie les nouvelles commandes payées, puis rend la main. Asynchrone : retourne aussitôt,
    l'avancement se lit via `etat_rafraichissement()` (exposé par `GET /api/shopify/status`).
    """
    reglages = lire_reglages_shopify()
    if not reglages["store_url"] or not identifiants_presents(reglages):
        return {"demarre": False, "erreur": "Shopify n'est pas connecté : renseignez le domaine et le jeton "
                                            "(ou l'identifiant client et le secret)."}
    with _verrou_rafraichissement:
        if _etat_rafraichissement["en_cours"]:
            return {"demarre": False, "deja_en_cours": True}
        _etat_rafraichissement.update(en_cours=True, ok=None, message="", fin=None,
                                      debut=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    threading.Thread(target=_executer_rafraichissement, daemon=True, name="KodoShopifyRefresh").start()
    return {"demarre": True}


def comparer_stock_boutique() -> dict:
    """Comparaison caisse ↔ boutique en ligne (lecture seule), pour la route de support."""
    return ShopifySync().comparer_stock()


def aligner_stock_boutique(stock_ids=None) -> dict:
    """
    Aligne la boutique en ligne sur la caisse (lignes en écart, ou `stock_ids`).

    Sous le verrou des passes : une passe automatique ne peut pas pousser une vente pendant que
    l'alignement lit puis ajuste les mêmes quantités (la valeur visée tient compte des ventes
    pas encore envoyées, elle ne doit pas changer sous ses pieds).
    """
    moteur = ShopifySync()
    with _VERROU_PASSE:
        return moteur.aligner_stock(stock_ids)


def rapport_comparaison_texte(rapport: dict) -> str:
    """Le rapport de `comparer_stock` en texte brut, lisible dans un Terminal sans outil JSON."""
    resume = rapport.get("resume", {})
    lignes = rapport.get("lignes", [])
    sortie = [
        f"Comparaison caisse ↔ boutique en ligne (dépôt {rapport.get('depot', '?')})",
        (f"  {resume.get('lignes', 0)} ligne(s) de stock : {resume.get('identiques', 0)} identique(s), "
         f"{resume.get('ecarts', 0)} en écart, {resume.get('absentes', 0)} non publiée(s) en ligne, "
         f"{resume.get('ambigues', 0)} ambiguë(s), {resume.get('sansNiveau', 0)} sans stock suivi"),
        "",
    ]

    def nom(l):
        return f"{l['produit']} — {l['taille']}"

    ecarts = [l for l in lignes if l["etat"] == "ecart"]
    if ecarts:
        sortie.append("EN ÉCART (le site devrait afficher la valeur visée = caisse + ventes pas encore envoyées)")
        for l in ecarts:
            attente = f" (+{l['ventesEnAttente']} en attente)" if l["ventesEnAttente"] else ""
            sortie.append(f"  {nom(l)} [{l['sku']}] : caisse {l['quantiteKodo']}{attente}, "
                          f"site {l['quantiteSite']} → site à ajuster de {l['ecart']:+d}")
        sortie.append("")
    ambigues = [l for l in lignes if l["etat"] == "ambigu"]
    if ambigues:
        sortie.append("SKU AMBIGUS (plusieurs variantes en ligne portent ce SKU : à corriger dans Shopify, ignorés)")
        for l in ambigues:
            sortie.append(f"  {nom(l)} [{l['sku']}]")
        sortie.append("")
    absentes = [l for l in lignes if l["etat"] == "absent"]
    if absentes:
        sortie.append("SKU INTROUVABLE EN LIGNE (rien n'est aligné ni poussé pour ces lignes)")
        for l in absentes:
            sortie.append(f"  {nom(l)} [{l['sku']}] : caisse {l['quantiteKodo']}")
            s = l.get("suggestion")
            if s:
                sku_en_ligne = s["skuEnLigne"] or s["codeBarresEnLigne"] or "sans SKU"
                sortie.append(f"      → en ligne : « {s['titre']} », taille {s['taille']}, "
                              f"SKU « {sku_en_ligne} » ({s['via']})")
        sortie.append("")
    if ecarts:
        sortie.append("Pour aligner le site sur la caisse : POST /api/shopify/stock-align "
                      "avec {\"confirmer\": true}")
    else:
        sortie.append("Aucun écart : la boutique en ligne affiche ce que la caisse attend.")
    return "\n".join(sortie)


def import_shopify_catalog(progress_callback=None):
    engine = ShopifySync()
    return engine.import_catalog(progress_callback=progress_callback)
