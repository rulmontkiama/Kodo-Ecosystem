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
from decimal import Decimal, ROUND_HALF_UP
from database_manager import get_connection, signer_ticket, signer_ledger

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
    """État de la dernière passe (date, succès, message), pour l'écran Paramètres."""
    etat = {"derniere_synchro": None, "succes": None, "message": ""}
    try:
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT cle, valeur FROM Parametres WHERE cle IN (?, ?, ?)",
                      (CLE_ETAT_DATE, CLE_ETAT_OK, CLE_ETAT_MESSAGE))
            params = {row[0]: row[1] for row in c.fetchall()}
        finally:
            conn.close()
        etat["derniere_synchro"] = params.get(CLE_ETAT_DATE)
        if CLE_ETAT_OK in params:
            etat["succes"] = params.get(CLE_ETAT_OK) == "1"
        etat["message"] = params.get(CLE_ETAT_MESSAGE, "") or ""
    except Exception as e:
        logger.error(f"Impossible de lire l'état de synchronisation Shopify : {e}")
    return etat


def lire_reglages_shopify(conn=None) -> dict:
    """
    Les quatre réglages Shopify (URL, jeton, push du stock, rapatriement des commandes).

    Les défauts sont EXACTEMENT ceux de `/api/settings` (clé absente → "1", donc interrupteur
    allumé à l'écran) : un défaut divergent ici ferait mentir l'interrupteur affiché à la
    commerçante — coché dans les réglages, mais sans effet réel.
    """
    reglages = {"store_url": "", "access_token": "", "auto_sync": True, "sync_orders": True}
    fermer = conn is None
    try:
        conn = conn or get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT cle, valeur FROM Parametres WHERE cle IN (?, ?, ?, ?)",
            (CLE_URL, CLE_TOKEN, CLE_AUTO_SYNC, CLE_SYNC_ORDERS),
        )
        params = {row[0]: row[1] for row in c.fetchall()}
        reglages["store_url"] = str(params.get(CLE_URL) or "").strip()
        reglages["access_token"] = str(params.get(CLE_TOKEN) or "").strip()
        reglages["auto_sync"] = str(params.get(CLE_AUTO_SYNC, "1")) == "1"
        reglages["sync_orders"] = str(params.get(CLE_SYNC_ORDERS, "1")) == "1"
    except Exception as e:
        logger.error(f"Erreur lors du chargement de la configuration Shopify : {e}")
    finally:
        if fermer and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return reglages


# --- Journal de synchronisation (idempotence ligne à ligne) ---------------------------------

STATUT_EN_VOL = "EN_VOL"                # ajustement envoyé, réponse pas encore reçue
STATUT_POUSSE = "POUSSE"                # ajustement confirmé par Shopify
STATUT_ABSENT = "ABSENT_SHOPIFY"        # article inconnu de la boutique : rien à ajuster
STATUT_SANS_OBJET = "SANS_OBJET"        # quantité nulle : rien à ajuster
STATUT_INDETERMINE = "INDETERMINE"      # réponse jamais reçue : à vérifier à la main


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


class ShopifySync:
    """Moteur principal de synchronisation REST & GraphQL pour Shopify."""

    PAGE_SIZE = 250
    MAX_PAGES = 200  # garde-fou : 50 000 produits

    REQUETE_VARIANTE = """
    query($query: String!) {
      productVariants(first: 1, query: $query) {
        edges {
          node {
            inventoryItem {
              id
            }
          }
        }
      }
    }
    """

    def __init__(self, store_url: str = "", access_token: str = "", api_version: str = "2025-01"):
        self.store_url = store_url.strip()
        self.access_token = access_token.strip()
        self.api_version = api_version
        self._location_id = None
        # Interrupteurs de l'écran Paramètres : allumés par défaut, comme les affiche `/api/settings`.
        self.auto_sync = True
        self.sync_orders = True
        # Cache SKU → inventory_item_id : sans lui, chaque ligne de chaque ticket déclenchait un
        # appel GraphQL à chaque passe, et les plafonds de débit Shopify étaient atteints en boutique.
        self._cache_inventaire = {}
        self._rest_scan_fait = False
        # Nature du dernier échec définitif : None, "http" (le serveur a répondu, rien n'a été
        # appliqué) ou "reseau" (aucune réponse reçue, on ne sait pas si l'appel a porté).
        self.dernier_echec = None
        # Identifiants imposés par l'appelant : uniquement quand les DEUX sont fournis. Un jeton
        # passé en argument avec une URL vide donnait un état bâtard (moitié appelant, moitié
        # base) ; dans ce cas la base fait autorité sur les deux champs.
        self._config_injectee = bool(self.store_url and self.access_token)
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
        if self._config_injectee:
            return
        self.store_url = reglages["store_url"]
        self.access_token = reglages["access_token"]

    def domaine(self) -> str:
        """Domaine normalisé de la boutique configurée (voir `normaliser_domaine_boutique`)."""
        return normaliser_domaine_boutique(self.store_url)

    def est_configure(self) -> bool:
        """Vrai si le domaine de boutique EST exploitable ET le jeton d'accès renseigné."""
        return bool(self.access_token and domaine_boutique_valide(self.domaine()))

    def make_request(self, endpoint: str, method: str = "GET", data: dict = None, max_retries: int = 3):
        """
        Exécute une requête HTTP REST ou GraphQL vers Shopify avec retry exponentiel (429 Rate Limit).

        Mémorise la nature de l'échec dans `self.dernier_echec` : un 4xx/5xx signifie que Shopify a
        répondu et n'a rien appliqué (on peut retenter sans risque), une coupure réseau signifie
        qu'on ignore si l'appel a porté (retenter pourrait décrémenter deux fois le stock).
        """
        if not self.est_configure():
            logger.warning("Configuration Shopify manquante ou invalide (domaine ou jeton).")
            self.dernier_echec = "config"
            return None

        base_url = self.domaine()
        # HTTP n'est toléré que vers la machine elle-même (serveur d'essai des tests
        # d'intégration). Vers un vrai domaine, la requête part TOUJOURS en HTTPS vérifié :
        # le jeton d'administration ne doit jamais circuler en clair.
        protocole = "https"
        if _est_boucle_locale(base_url) and str(self.store_url).strip().lower().startswith("http://"):
            protocole = "http"
        url = f"{protocole}://{base_url}/admin/api/{self.api_version}/{endpoint}"
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

            try:
                with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as response:
                    self.dernier_echec = None
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", 2))
                    logger.warning(f"Rate limited (429). Attente de {retry_after}s (essai {attempt}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                else:
                    try:
                        err_detail = e.read().decode("utf-8", errors="ignore")
                    except Exception:
                        err_detail = str(e)
                    logger.error(f"Erreur HTTP {e.code} sur {method} {endpoint}: {err_detail}")
                    self.dernier_echec = "http"
                    return None
            except Exception as e:
                logger.error(f"Erreur réseau/API sur {method} {endpoint} (essai {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(1.5 * attempt)
                else:
                    self.dernier_echec = "reseau"
                    return None
        self.dernier_echec = "reseau"
        return None

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
        if not self.access_token:
            return {"success": False, "error": "Jeton d'accès Shopify requis pour le test."}
        if not domaine_boutique_valide(domaine):
            return {"success": False, "error": f"Domaine de boutique invalide : {self.store_url!r}."}

        data = self.make_request("locations.json")
        if data is None:
            return {
                "success": False,
                "domain": domaine,
                "error": f"Aucune réponse exploitable de {domaine} (domaine ou jeton refusé).",
            }
        locations = [l.get("name", "Dépôt") for l in (data.get("locations") or [])]
        return {
            "success": True,
            "domain": domaine,
            "message": (f"Connexion Shopify réussie sur {domaine} ! Dépôts : {', '.join(locations)}"
                        if locations else f"Connexion établie avec {domaine}."),
            "locations": locations,
        }

    def get_location_id(self) -> str:
        """Récupère et met en cache le location_id actif de l'inventaire Shopify."""
        if self._location_id:
            return self._location_id

        data = self.make_request("locations.json")
        if data and "locations" in data and len(data["locations"]) > 0:
            active_locs = [l for l in data["locations"] if l.get("active", True)]
            if active_locs:
                self._location_id = active_locs[0]["id"]
                return self._location_id
        return None

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

        if res and not res.get("errors") and isinstance(res.get("data"), dict):
            edges = (res["data"].get("productVariants") or {}).get("edges") or []
            for edge in edges:
                gid = ((edge.get("node") or {}).get("inventoryItem") or {}).get("id", "")
                try:
                    item_id = int(str(gid).split("/")[-1])
                except (TypeError, ValueError):
                    continue
                self._cache_inventaire[sku] = item_id
                return item_id, False
            # Réponse valide et vide : la variante n'existe vraiment pas chez Shopify.
            return None, False

        # GraphQL indisponible (jeton sans portée GraphQL, panne) → balayage REST complet.
        if not self._rest_scan_fait and not self._charger_cache_rest():
            return None, True
        item_id = self._cache_inventaire.get(sku)
        return item_id, False

    def find_inventory_item_id(self, sku: str):
        """ID de l'item d'inventaire Shopify pour un SKU/code-barres, ou None."""
        return self._resoudre_inventory_item(sku)[0]

    def adjust_shopify_stock(self, inventory_item_id: int, location_id: str, qty_change: int) -> bool:
        """Ajuste (en relatif) le niveau d'inventaire sur Shopify pour un article donné."""
        if not location_id or not inventory_item_id:
            return False
        data = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available_adjustment": qty_change
        }
        self.dernier_echec = None
        res = self.make_request("inventory_levels/adjust.json", method="POST", data=data)
        return res is not None

    def set_shopify_stock(self, inventory_item_id: int, location_id: str, quantite: int) -> bool:
        """
        Pose une valeur ABSOLUE de stock (correction d'inventaire, pas une vente).

        L'ajustement relatif est réservé aux mouvements (vente, remboursement) ; une correction
        d'inventaire saisie en boutique doit au contraire imposer la valeur comptée.
        """
        if not location_id or not inventory_item_id:
            return False
        data = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available": int(quantite)
        }
        self.dernier_echec = None
        res = self.make_request("inventory_levels/set.json", method="POST", data=data)
        return res is not None

    # --- Poussée des ventes locales vers Shopify --------------------------------------------

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
        conn = None
        synced_count = 0
        try:
            conn = get_connection()
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            # Une ligne restée « en vol » vient d'un arrêt brutal entre l'envoi et la réponse :
            # on ignore si Shopify l'a appliquée. On ne la rejoue jamais (une double
            # décrémentation est irrattrapable) et on la signale pour vérification manuelle.
            c.execute("UPDATE Shopify_Sync_Lignes SET statut = ? WHERE statut = ?",
                      (STATUT_INDETERMINE, STATUT_EN_VOL))
            en_suspens = c.rowcount
            conn.commit()
            if en_suspens:
                logger.warning(
                    f"[SYNC AUDIT] {en_suspens} ligne(s) de vente restée(s) sans réponse de Shopify : "
                    f"statut {STATUT_INDETERMINE}, à vérifier dans le journal Shopify_Sync_Lignes."
                )

            c.execute("SELECT id, numero_ticket FROM Tickets WHERE synced_shopify = 0 ORDER BY id")
            tickets = c.fetchall()

            for t_id, num in tickets:
                c.execute("""
                    SELECT vd.id, p.code_barre, vd.quantite
                    FROM Ventes_Details vd
                    JOIN Stocks s ON vd.id_stock = s.id
                    JOIN Produits p ON s.id_produit = p.id
                    LEFT JOIN Shopify_Sync_Lignes j ON j.id_vente_detail = vd.id
                    WHERE vd.id_ticket = ?
                      AND j.id_vente_detail IS NULL
                      AND p.code_barre IS NOT NULL AND p.code_barre != ''
                    ORDER BY vd.id
                """, (t_id,))
                lignes = c.fetchall()

                tout_pousse = True
                for vd_id, code_barre, quantite in lignes:
                    qte = int(quantite or 0)
                    if qte == 0:
                        self._journaliser(conn, vd_id, t_id, code_barre, None, 0, STATUT_SANS_OBJET)
                        continue

                    inv_item_id, echec_reseau = self._resoudre_inventory_item(code_barre)
                    if echec_reseau:
                        logger.error(f"Recherche Shopify impossible pour SKU {code_barre} : ligne retentée plus tard.")
                        tout_pousse = False
                        continue
                    if not inv_item_id:
                        self._journaliser(conn, vd_id, t_id, code_barre, None, 0, STATUT_ABSENT)
                        logger.warning(f"SKU {code_barre} introuvable sur Shopify : ligne tracée, plus retentée.")
                        continue

                    logger.info(f"Push vente locale (Ticket {num}) - SKU {code_barre} - Qte: {-qte}")
                    # Réservation AVANT l'appel : si le poste s'éteint pendant l'échange, la ligne
                    # est retrouvée « en vol » au démarrage suivant et ne sera pas rejouée à l'aveugle.
                    self._journaliser(conn, vd_id, t_id, code_barre, inv_item_id, -qte, STATUT_EN_VOL)
                    applique = self.adjust_shopify_stock(inv_item_id, location_id, -qte)

                    c2 = _ouvrir_ecriture(conn)
                    if applique:
                        c2.execute("UPDATE Shopify_Sync_Lignes SET statut = ? WHERE id_vente_detail = ?",
                                   (STATUT_POUSSE, vd_id))
                    elif self.dernier_echec == "reseau":
                        # Aucune réponse : on ne sait pas si l'ajustement a porté. On garde la trace
                        # et on ne rejoue pas — sous-décompter se corrige, sur-décompter non.
                        c2.execute("UPDATE Shopify_Sync_Lignes SET statut = ? WHERE id_vente_detail = ?",
                                   (STATUT_INDETERMINE, vd_id))
                        logger.error(f"[SYNC AUDIT] Ligne {vd_id} (SKU {code_barre}) sans réponse Shopify : à vérifier.")
                        tout_pousse = False
                    else:
                        # Shopify a répondu en erreur : rien n'a été appliqué, la ligne reste à pousser.
                        c2.execute("DELETE FROM Shopify_Sync_Lignes WHERE id_vente_detail = ? AND statut = ?",
                                   (vd_id, STATUT_EN_VOL))
                        logger.error(f"Échec de l'ajustement du stock Shopify pour SKU {code_barre}")
                        tout_pousse = False
                    conn.commit()

                if tout_pousse:
                    c3 = _ouvrir_ecriture(conn)
                    c3.execute("UPDATE Tickets SET synced_shopify = 1 WHERE id = ?", (t_id,))
                    conn.commit()
                    synced_count += 1
                    logger.info(f"Ticket {num} marqué comme synchronisé Shopify.")
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
    def _marquer_audit(c, stock_id, produit_id):
        """Pose le drapeau d'audit de stock (patron maison, cf. `database_manager.enregistrer_vente`)."""
        try:
            if stock_id:
                c.execute("UPDATE Stocks SET requires_stock_audit = 1 WHERE id = ?", (stock_id,))
            if produit_id:
                c.execute("UPDATE Produits SET requires_stock_audit = 1 WHERE id = ?", (produit_id,))
        except Exception:
            # Colonne absente sur une base ancienne : l'incident reste tracé dans le journal.
            pass

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

    def sync_orders_from_shopify(self) -> int:
        """
        Rapatrie les commandes Shopify payées et non traitées, puis génère des tickets conformes localement.
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
                    c2.execute("SELECT id FROM Tickets WHERE shopify_order_id = ?", (order_id,))
                    if c2.fetchone():
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

                        stock_changes.append({"stock_id": sid, "qty": qty, "prix_unitaire_tvac": prix_unitaire})

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
                    imported_orders += 1
                    logger.info(f"Commande #{order_number} synchronisée avec succès.")

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

    def import_catalog(self, progress_callback=None) -> int:
        """
        Importe le catalogue complet de Shopify vers la base de données locale Kōdo POS.

        Un produit déjà connu (même variante Shopify, même code-barres/SKU) est MIS À JOUR sur place :
        son id, sa TVA, sa marque, son seuil d'alerte, son image, son prix d'achat ET son code-barres
        sont conservés, et ses lignes de stock existantes sont réutilisées. Avant, `INSERT OR REPLACE`
        supprimait puis recréait le produit sous un nouvel id à chaque import (stocks orphelins,
        réglages remis à zéro, écran de caisse pointant sur des ids morts).

        Ce qui est VOLONTAIREMENT écrasé à chaque import : le nom, la catégorie et les prix de vente
        (normal et soldé). Shopify fait autorité sur la vitrine ; le reste appartient à la boutique.
        """
        self.load_config()
        if not self.est_configure():
            raise ValueError("Configuration Shopify manquante (URL ou Token).")

        logger.info("Début de l'importation du catalogue...")
        if progress_callback:
            progress_callback("Récupération des produits Shopify...", 10)

        shopify_products = self._fetch_all_products(progress_callback)
        if shopify_products is None:
            logger.warning("Aucun produit trouvé sur Shopify ou erreur de connexion.")
            return 0

        total_p = len(shopify_products)
        logger.info(f"{total_p} produits récupérés de Shopify.")

        conn = get_connection()
        imported_count = 0
        try:
            c = _ouvrir_ecriture(conn)
            _assurer_tables_sync(c)
            maintenant = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for idx, p in enumerate(shopify_products):
                if progress_callback:
                    pct = 10 + int((idx / total_p) * 80)
                    progress_callback(f"Importation : {p.get('title')} ({idx+1}/{total_p})...", pct)

                nom = p.get("title", "Sans nom")
                cat = p.get("product_type", "Général") or "Général"

                c.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))

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

                    price_str = v.get("price", "0.00")
                    compare_str = v.get("compare_at_price")

                    try:
                        # `str()` d'abord : Shopify envoie ses prix en texte, mais rien ne le
                        # garantit et `Decimal(3.30)` scellerait la valeur binaire du flottant
                        # (3.2999999999999998...) au lieu du prix écrit.
                        price_val = Decimal(str(price_str))
                    except Exception:
                        price_val = Decimal("0.00")

                    en_solde = 0
                    prix_solde_tvac = None
                    prix_vente_tvac = price_val

                    if compare_str:
                        try:
                            compare_val = Decimal(str(compare_str))
                            if compare_val > price_val:
                                en_solde = 1
                                prix_vente_tvac = compare_val
                                prix_solde_tvac = price_val
                        except Exception:
                            pass

                    pid = self._retrouver_produit(c, variant_id, code, code_brut)
                    if pid:
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
                        # Le code-barres reste NULL quand la variante n'en porte aucun : fabriquer
                        # un « SHPF-<id> » produisait une valeur non scannable et impossible à
                        # imprimer, présentée comme un vrai code-barres. La correspondance de
                        # variante ci-dessous suffit à retrouver le produit au prochain import.
                        c.execute("""
                            INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, en_solde, prix_solde_tvac)
                            VALUES (?, ?, ?, NULL, ?, ?, ?)
                        """, (code, nom, cat, prix_vente_tvac, en_solde, prix_solde_tvac))
                        pid = c.lastrowid

                    if pid and variant_id:
                        c.execute("""
                            INSERT INTO Shopify_Variantes (variant_id, id_produit, date_maj) VALUES (?, ?, ?)
                            ON CONFLICT(variant_id) DO UPDATE SET id_produit=excluded.id_produit, date_maj=excluded.date_maj
                        """, (variant_id, pid, maintenant))

                    if pid:
                        opt1 = v.get("option1", "Unique")
                        opt2 = v.get("option2")
                        taille = opt1 if opt1 and opt1 != "Default Title" else "Unique"
                        if opt2 and opt2 != "Default Title":
                            taille = f"{taille} / {opt2}"

                        qty = int(v.get("inventory_quantity") or 0)

                        c.execute("SELECT id, taille FROM Stocks WHERE id_produit=? ORDER BY id", (pid,))
                        stock_rows = c.fetchall()
                        wanted = str(taille).strip().casefold()
                        cible = next((r[0] for r in stock_rows if str(r[1] or "").strip().casefold() == wanted), None)
                        if cible is None and self._est_taille_unique(taille):
                            cible = next((r[0] for r in stock_rows if self._est_taille_unique(r[1])), None)
                        if cible is not None:
                            c.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (qty, cible))
                        else:
                            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, NULL)", (pid, taille, qty))

                    imported_count += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if progress_callback:
            progress_callback("Importation terminée !", 100)
        logger.info(f"Importation terminée : {imported_count} variantes importées.")
        return imported_count


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
        self.engine.dernier_echec = None
        if self.engine.auto_sync:
            try:
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
        if self.engine.dernier_echec:
            tout_va_bien = False
            incidents.append(f"échec de communication ({self.engine.dernier_echec}) avec {self.engine.domaine()}")

        message = " ; ".join(incidents) if incidents else (
            f"{tickets} ticket(s) poussé(s), {commandes} commande(s) rapatriée(s), "
            f"{remboursements} remboursement(s) repris vers {self.engine.domaine()}"
        )
        enregistrer_etat_sync(tout_va_bien, message)
        return tout_va_bien

    def run(self):
        logger.info("Démarrage du thread Shopify en arrière-plan...")
        while self.running:
            try:
                tout_va_bien = self.executer_une_passe()
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
        if _thread_auto is not None and _thread_auto.is_alive():
            return _thread_auto

        reglages = lire_reglages_shopify()
        if not reglages["store_url"] or not reglages["access_token"]:
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


def import_shopify_catalog(progress_callback=None):
    engine = ShopifySync()
    return engine.import_catalog(progress_callback=progress_callback)
