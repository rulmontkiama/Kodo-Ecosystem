# -*- coding: utf-8 -*-
"""
Kōdo POS — La synchronisation Shopify parle bien à LA boutique des réglages, et dans les deux sens.

Ce fichier est la démonstration de trois choses, sans aucune boutique Shopify réelle :

1. **La boutique cible est celle de la commerçante, et elle seule.** Rien n'est codé en dur :
   le domaine et le jeton viennent de `Parametres` (écrits par l'écran Paramètres). Les tests
   vérifient l'URL exacte appelée et l'en-tête `X-Shopify-Access-Token` réellement émis.
2. **Le stock circule dans les deux sens** : une vente en caisse décrémente la boutique en ligne,
   une commande de la boutique décrémente le stock local — à la BONNE taille.
3. **Rien ne se répète et rien ne fuite** : une ligne déjà poussée ne l'est jamais deux fois, le
   certificat TLS est toujours vérifié, et rien ne démarre tant que Shopify n'est pas configuré.

Aucun appel vers un domaine externe : soit `make_request`/`urlopen` est bouchonné, soit un faux
serveur Shopify est monté sur 127.0.0.1 le temps du test, puis arrêté.
"""
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import tokenize
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.sync import shopify as shopify_sync
from kodo_core.sync.shopify import (
    ShopifySync,
    ShopifySyncThread,
    domaine_boutique_valide,
    get_ssl_context,
    lire_reglages_shopify,
    normaliser_domaine_boutique,
    start_auto_sync,
    stop_auto_sync,
)


def code_sans_commentaires_ni_litteraux(chemin):
    """
    Le code d'un fichier, débarrassé de ses commentaires et de ses chaînes.

    La maison documente ses anciens défauts dans les commentaires (« avant, CERT_NONE... ») :
    un garde-fou qui cherche dans le texte brut interdirait d'en parler. On ne regarde donc que
    ce qui s'exécute réellement.
    """
    morceaux = []
    with open(chemin, "rb") as f:
        for jeton in tokenize.tokenize(f.readline):
            if jeton.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            morceaux.append(jeton.string)
    return " ".join(morceaux)


class BaseTemporaire(unittest.TestCase):
    """Base SQLite jetable : aucun test ne touche aux données réelles de la commerçante."""

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    # --- utilitaires de base ---------------------------------------------------------------

    def rows(self, sql, params=()):
        conn = database_manager.get_connection()
        try:
            return [tuple(r) for r in conn.cursor().execute(sql, params).fetchall()]
        finally:
            conn.close()

    def ecrire(self, sql, params=()):
        conn = database_manager.get_connection()
        try:
            conn.cursor().execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def regler_shopify(self, domaine, jeton, auto_sync="1", sync_orders="1"):
        """Écrit les réglages par le même chemin que `POST /api/settings` (table `Parametres`)."""
        for cle, valeur in (("shopify_store_url", domaine),
                            ("shopify_access_token", jeton),
                            ("shopify_auto_sync", auto_sync),
                            ("shopify_sync_orders", sync_orders)):
            self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))

    def creer_produit(self, code_barre, nom, tailles):
        """Produit + lignes de stock. `tailles` : [("S", 4), ("M", 6)]."""
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac, taux_tva) "
                      "VALUES (?, ?, 'Test', '10.00', '0.21')", (code_barre, nom))
            pid = c.lastrowid
            for taille, qte in tailles:
                c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, ?, ?)",
                          (pid, taille, qte))
            conn.commit()
            return pid
        finally:
            conn.close()

    def creer_ticket(self, numero, lignes):
        """Ticket non synchronisé + ses lignes. `lignes` : [(stock_id, quantite)]."""
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, methode_paiement, synced_shopify) "
                      "VALUES (?, '2026-01-01 10:00:00', '30.00', 'CB', 0)", (numero,))
            tid = c.lastrowid
            for stock_id, qte in lignes:
                c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) "
                          "VALUES (?, ?, ?, '10.00')", (tid, stock_id, qte))
            conn.commit()
            return tid
        finally:
            conn.close()


class MoteurBouchonne(ShopifySync):
    """Moteur dont les échanges HTTP sont remplacés par des réponses écrites dans le test."""

    def __init__(self, reponses, **kwargs):
        super().__init__(**kwargs)
        self.reponses = reponses          # endpoint (ou préfixe) -> réponse ou callable
        self.appels = []                  # historique (endpoint, corps)

    def make_request(self, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
        self.appels.append((endpoint, data))
        self.dernier_echec = None
        for cle, valeur in self.reponses.items():
            if endpoint.startswith(cle):
                reponse = valeur(endpoint, data) if callable(valeur) else valeur
                if reponse is None:
                    self.dernier_echec = "http"
                return reponse
        self.dernier_echec = "http"
        return None


REPONSE_LOCATIONS = {"locations": [{"id": 111, "active": True, "name": "Boutique"}]}


def reponse_graphql(item_id):
    return {"data": {"productVariants": {"edges": [
        {"node": {"inventoryItem": {"id": f"gid://shopify/InventoryItem/{item_id}"}}}
    ]}}}


# =============================================================================================
# 1. La boutique cible vient des réglages, et de nulle part ailleurs
# =============================================================================================

class TestBoutiqueCible(BaseTemporaire):

    def test_le_domaine_colle_depuis_l_admin_shopify_est_normalise(self):
        """« https://MaStore.myshopify.com/admin » doit désigner la boutique, pas un sous-chemin."""
        for saisi in ("https://MaStore.myshopify.com/admin",
                      "  mastore.myshopify.com/  ",
                      "http://mastore.myshopify.com/admin/products?x=1",
                      "MASTORE.myshopify.com#section"):
            self.assertEqual(normaliser_domaine_boutique(saisi), "mastore.myshopify.com",
                             msg=f"domaine mal normalisé pour {saisi!r}")
        self.assertEqual(normaliser_domaine_boutique(""), "")
        self.assertFalse(domaine_boutique_valide(normaliser_domaine_boutique("pas-un-domaine")))

    def test_l_url_appelee_et_le_jeton_emis_sont_ceux_des_reglages(self):
        """Le fil complet : réglages → load_config → make_request → requête réellement émise."""
        self.regler_shopify("https://boutique-de-la-cliente.myshopify.com/admin", "shpat_JETON_DE_LA_CLIENTE")

        emises = []

        class ReponseBidon:
            def read(self_inner):
                return json.dumps(REPONSE_LOCATIONS).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        def faux_urlopen(req, *a, **k):
            emises.append((req.full_url, dict(req.headers), k.get("context")))
            return ReponseBidon()

        original = urllib.request.urlopen
        urllib.request.urlopen = faux_urlopen
        try:
            moteur = ShopifySync()                 # aucun argument : tout vient de la base
            self.assertTrue(moteur.est_configure())
            self.assertEqual(moteur.get_location_id(), 111)
        finally:
            urllib.request.urlopen = original

        self.assertEqual(len(emises), 1, "une seule requête devait partir")
        url, entetes, contexte = emises[0]
        self.assertEqual(url, "https://boutique-de-la-cliente.myshopify.com/admin/api/2025-01/locations.json")
        # urllib capitalise les noms d'en-têtes.
        self.assertEqual(entetes.get("X-shopify-access-token"), "shpat_JETON_DE_LA_CLIENTE")
        self.assertIsInstance(contexte, ssl.SSLContext)

    def test_effacer_le_jeton_dans_les_reglages_debranche_la_boutique(self):
        """Un jeton effacé ne doit pas rester actif en mémoire jusqu'au redémarrage."""
        self.regler_shopify("boutique.myshopify.com", "shpat_ancien")
        moteur = ShopifySync()
        self.assertTrue(moteur.est_configure())

        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_access_token', '')")
        moteur.load_config()
        self.assertFalse(moteur.est_configure(), "l'ancien jeton est resté actif après effacement")
        self.assertIsNone(moteur.make_request("locations.json"))

    def test_jeton_seul_sans_url_ne_cree_pas_d_etat_batard(self):
        """Un seul identifiant fourni en argument : la base fait autorité sur les deux champs."""
        self.regler_shopify("boutique.myshopify.com", "shpat_de_la_base")
        moteur = ShopifySync(access_token="shpat_orphelin")
        self.assertEqual(moteur.store_url, "boutique.myshopify.com")
        self.assertEqual(moteur.access_token, "shpat_de_la_base")

    def test_les_interrupteurs_sont_lus_avec_les_defauts_de_l_ecran_parametres(self):
        """Clé absente → interrupteur allumé, exactement comme l'affiche `/api/settings`."""
        self.ecrire("DELETE FROM Parametres WHERE cle IN ('shopify_auto_sync', 'shopify_sync_orders')")
        reglages = lire_reglages_shopify()
        self.assertTrue(reglages["auto_sync"])
        self.assertTrue(reglages["sync_orders"])
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_auto_sync', '0')")
        self.assertFalse(lire_reglages_shopify()["auto_sync"])

    def test_aucune_valeur_de_boutique_codee_en_dur_dans_le_module(self):
        """
        Garde-fou : le module ne doit contenir ni domaine ni jeton en dur.

        Kōdo POS est vendu à des boutiques : chacune saisit les siens. Une valeur par défaut
        pointerait toutes les caisses vers la même adresse.
        """
        code = code_sans_commentaires_ni_litteraux(shopify_sync.__file__)
        for motif in ("myshopify", "shpat_", "shpca_"):
            self.assertNotIn(motif, code, f"valeur de boutique codée en dur dans le module : {motif}")


# =============================================================================================
# 2. Transport : le certificat est vérifié
# =============================================================================================

class TestTransport(BaseTemporaire):

    def test_le_contexte_tls_verifie_certificat_et_nom_d_hote(self):
        """Le jeton d'administration ne doit jamais partir dans un tunnel non vérifié."""
        ctx = get_ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_aucune_desactivation_residuelle_de_tls(self):
        """
        Garde-fou contre un retour en arrière, dans le moteur ET dans la route de test Shopify.

        Les commentaires qui racontent l'ancien défaut sont ignorés : seul le code exécuté compte.
        """
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for relatif in ("kodo_core/sync/shopify.py", "kodo_core/api/routes/system_routes.py"):
            code = code_sans_commentaires_ni_litteraux(os.path.join(racine, relatif))
            for motif in ("CERT_NONE", "check_hostname=False"):
                self.assertNotIn(motif.replace(" ", ""), code.replace(" ", ""),
                                 f"{relatif} neutralise à nouveau TLS ({motif})")

    def _compter_essais(self, appel):
        """Exécute `appel` en coupant le réseau à chaque essai, et compte les envois réels."""
        essais = []

        def urlopen_coupe(req, *a, **k):
            essais.append(req.full_url)
            raise urllib.error.URLError("connexion interrompue")

        original, dormir = urllib.request.urlopen, time.sleep
        urllib.request.urlopen = urlopen_coupe
        time.sleep = lambda _s: None          # l'attente entre essais n'a rien à prouver ici
        try:
            resultat = appel()
        finally:
            urllib.request.urlopen = original
            time.sleep = dormir
        return len(essais), resultat

    def test_un_ajustement_relatif_n_est_jamais_rejoue_apres_une_coupure(self):
        """
        Shopify peut avoir appliqué l'ajustement et la réponse s'être perdue.

        Le rejeu renverrait la MÊME charge `{"available_adjustment": -1}` : Shopify
        décrémenterait une seconde fois, sans qu'aucun statut ne le signale. Toute la
        réservation `EN_VOL` → `INDETERMINE` repose sur le fait que cet appel-là n'est pas
        rejoué — sous-décompter se corrige, sur-décompter non.
        """
        self.regler_shopify("boutique.myshopify.com", "jeton")
        moteur = ShopifySync()

        essais, applique = self._compter_essais(
            lambda: moteur.adjust_shopify_stock(777, 111, -1))

        self.assertEqual(essais, 1,
                         f"L'ajustement relatif est parti {essais} fois : le stock en ligne peut "
                         f"avoir été décrémenté autant de fois.")
        self.assertFalse(applique)
        self.assertEqual(moteur.dernier_echec, "reseau",
                         "Sans `dernier_echec = 'reseau'`, la ligne n'est pas marquée INDETERMINE.")

    def test_une_lecture_garde_ses_essais(self):
        """Le verrou ne doit pas rendre la synchro fragile : relire est sans conséquence."""
        self.regler_shopify("boutique.myshopify.com", "jeton")
        moteur = ShopifySync()

        essais, resultat = self._compter_essais(
            lambda: moteur.make_request("locations.json"))

        self.assertEqual(essais, 3, "Une simple lecture doit encore être retentée.")
        self.assertIsNone(resultat)

    def test_un_ajustement_absolu_garde_ses_essais(self):
        """`inventory_levels/set.json` fixe une valeur : le rejouer ne change rien."""
        self.regler_shopify("boutique.myshopify.com", "jeton")
        moteur = ShopifySync()

        essais, _ = self._compter_essais(lambda: moteur.set_shopify_stock(777, 111, 5))

        self.assertEqual(essais, 3, "Un mouvement absolu est idempotent : il reste rejouable.")

    def test_un_domaine_externe_est_toujours_appele_en_https(self):
        """Même si la commerçante a saisi « http:// », le jeton ne part jamais en clair."""
        # Domaine en `.myshopify.com` : c'est le seul suffixe où répond l'API d'administration,
        # et donc la seule destination que `domaine_boutique_valide` accepte hors boucle locale.
        self.regler_shopify("http://boutique-de-la-cliente.myshopify.com", "jeton")
        urls = []

        class ReponseBidon:
            def read(self_inner):
                return b'{"locations": []}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, *a, **k: (urls.append(req.full_url), ReponseBidon())[1]
        try:
            ShopifySync().make_request("locations.json")
        finally:
            urllib.request.urlopen = original
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("https://boutique-de-la-cliente.myshopify.com/"), urls[0])


# =============================================================================================
# 3. Ventes locales poussées vers Shopify : jamais deux fois
# =============================================================================================

class TestPousseeDesVentes(BaseTemporaire):

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        pid = self.creer_produit("CB-A", "Article A", [("Unique", 10)])
        self.stock_ids = [r[0] for r in self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))]

    def ticket_trois_lignes(self):
        """Un ticket de 3 lignes portant 3 SKU distincts."""
        stocks = []
        for num, qte in (("1", 1), ("2", 2), ("3", 3)):
            pid = self.creer_produit(f"CB-{num}", f"Article {num}", [("Unique", 5)])
            stocks.append((self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0], qte))
        self.creer_ticket("T-001", stocks)

    def moteur_trois_lignes(self, ligne_en_echec=None):
        """Moteur bouchonné servant les 3 SKU, dont un peut échouer à l'ajustement."""
        ajustements = []

        def graphql(endpoint, data):
            requete = (data or {}).get("variables", {}).get("query", "")
            for num in ("1", "2", "3"):
                if f'"CB-{num}"' in requete:
                    return reponse_graphql(1000 + int(num))
            return {"data": {"productVariants": {"edges": []}}}

        def adjust(endpoint, data):
            item = data["inventory_item_id"]
            if ligne_en_echec is not None and item == 1000 + ligne_en_echec:
                return None                      # Shopify répond en erreur : rien n'est appliqué
            ajustements.append((item, data["available_adjustment"]))
            return {"inventory_level": {"available": 0}}

        moteur = MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": graphql,
            "inventory_levels/adjust.json": adjust,
        }, store_url="boutique.myshopify.com", access_token="jeton")
        return moteur, ajustements

    def test_une_vente_decremente_le_stock_shopify(self):
        self.ticket_trois_lignes()
        moteur, ajustements = self.moteur_trois_lignes()
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(sorted(ajustements), [(1001, -1), (1002, -2), (1003, -3)])
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets"), [(1,)])

    def test_echec_partiel_puis_nouvelle_passe_ne_pousse_chaque_ligne_qu_une_fois(self):
        """LA non-régression : la 2e ligne échoue, les lignes 1 et 3 ne repartent pas au tour suivant."""
        self.ticket_trois_lignes()
        moteur, ajustements = self.moteur_trois_lignes(ligne_en_echec=2)
        self.assertEqual(moteur.sync_tickets_to_shopify(), 0, "le ticket ne doit pas être marqué synchronisé")
        self.assertEqual(sorted(ajustements), [(1001, -1), (1003, -3)])
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets"), [(0,)])

        # Deuxième passe, cette fois sans panne : SEULE la ligne 2 doit repartir.
        moteur2, ajustements2 = self.moteur_trois_lignes()
        moteur2.sync_tickets_to_shopify()
        self.assertEqual(ajustements2, [(1002, -2)],
                         "les lignes déjà poussées ont été poussées une seconde fois : "
                         "le stock Shopify décrocherait définitivement")
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets WHERE numero_ticket = 'T-001'"), [(1,)])

    def test_un_sku_absent_de_shopify_est_trace_et_plus_jamais_redemande(self):
        self.creer_ticket("T-002", [(self.stock_ids[0], 1)])
        appels = {"n": 0}

        def graphql(endpoint, data):
            appels["n"] += 1
            return {"data": {"productVariants": {"edges": []}}}

        moteur = MoteurBouchonne({"locations.json": REPONSE_LOCATIONS, "graphql.json": graphql},
                                 store_url="boutique.myshopify.com", access_token="jeton")
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(appels["n"], 1)
        self.assertEqual(self.rows("SELECT statut FROM Shopify_Sync_Lignes"), [("ABSENT_SHOPIFY",)])

        moteur2 = MoteurBouchonne({"locations.json": REPONSE_LOCATIONS, "graphql.json": graphql},
                                  store_url="boutique.myshopify.com", access_token="jeton")
        moteur2.sync_tickets_to_shopify()
        self.assertEqual(appels["n"], 1, "le SKU absent a été redemandé à Shopify à chaque passe")

    def test_un_remboursement_recredite_la_boutique(self):
        """Une ligne de remboursement porte une quantité négative : elle doit CRÉDITER Shopify."""
        self.creer_ticket("REF-T-003", [(self.stock_ids[0], -2)])
        ajustements = []
        moteur = MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": lambda e, d: reponse_graphql(2001),
            "inventory_levels/adjust.json": lambda e, d: ajustements.append(d["available_adjustment"]) or {"ok": 1},
        }, store_url="boutique.myshopify.com", access_token="jeton")
        moteur.sync_tickets_to_shopify()
        self.assertEqual(ajustements, [2], "un retour doit rendre la pièce à la boutique en ligne")

    def test_un_sku_a_caracteres_speciaux_ne_casse_pas_la_requete_graphql(self):
        """Un SKU contenant une espace, un guillemet ou un `:` doit être échappé, pas interprété."""
        pid = self.creer_produit('ROBE "ÉTÉ": 38', "Robe", [("Unique", 3)])
        sid = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]
        self.creer_ticket("T-004", [(sid, 1)])

        requetes = []

        def graphql(endpoint, data):
            requetes.append(data["variables"]["query"])
            return reponse_graphql(3001)

        moteur = MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": graphql,
            "inventory_levels/adjust.json": {"ok": 1},
        }, store_url="boutique.myshopify.com", access_token="jeton")
        moteur.sync_tickets_to_shopify()

        self.assertEqual(len(requetes), 1)
        attendu = 'sku:"ROBE \\"ÉTÉ\\": 38" OR barcode:"ROBE \\"ÉTÉ\\": 38"'
        self.assertEqual(requetes[0], attendu)
        # La requête reste analysable : les guillemets du SKU sont échappés, pas ouverts.
        self.assertEqual(requetes[0].count('"') - requetes[0].count('\\"'), 4)

    def test_le_repli_rest_pagine_au_dela_des_250_premiers_produits(self):
        """Sans pagination, la 300e variante était déclarée « introuvable sur Shopify »."""
        pid = self.creer_produit("CB-300", "Trois cents", [("Unique", 2)])
        sid = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]
        self.creer_ticket("T-005", [(sid, 1)])

        def produits(endpoint, data):
            since = 0
            for kv in endpoint.split("?", 1)[1].split("&"):
                k, _, v = kv.partition("=")
                if k == "since_id":
                    since = int(v)
            page = [{"id": i, "variants": [{"sku": f"CB-{i}", "inventory_item_id": 7000 + i}]}
                    for i in range(since + 1, min(since + 251, 301))]
            return {"products": page}

        ajustements = []
        moteur = MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": None,                 # GraphQL indisponible → repli REST
            "products.json": produits,
            "inventory_levels/adjust.json": lambda e, d: ajustements.append(d["inventory_item_id"]) or {"ok": 1},
        }, store_url="boutique.myshopify.com", access_token="jeton")
        moteur.sync_tickets_to_shopify()
        self.assertEqual(ajustements, [7300])


# =============================================================================================
# 4. Commandes Shopify rapatriées : la bonne taille, et rien en silence
# =============================================================================================

class TestCommandesEntrantes(BaseTemporaire):

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.pid = self.creer_produit("CB-ROBE", "Robe", [("S", 4), ("M", 6), ("L", 2)])
        self.stocks = dict((t, i) for i, t in self.rows(
            "SELECT id, taille FROM Stocks WHERE id_produit = ?", (self.pid,)))

    def commande(self, **surcharges):
        base = {
            "id": 9001, "order_number": 1042, "total_price": "30.00", "total_tax": "5.21",
            "taxes_included": True,
            "line_items": [{"sku": "CB-ROBE", "title": "Robe", "quantity": 1,
                            "variant_title": "M", "price": "30.00"}],
        }
        base.update(surcharges)
        return base

    def moteur(self, commandes):
        return MoteurBouchonne({"orders.json": {"orders": commandes}},
                               store_url="boutique.myshopify.com", access_token="jeton")

    def test_une_commande_decremente_la_taille_reellement_commandee(self):
        """Avant, la commande d'un M retirait une pièce du S (première ligne de stock venue)."""
        self.assertEqual(self.moteur([self.commande()]).sync_orders_from_shopify(), 1)
        stocks = dict(self.rows("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ?", (self.pid,)))
        self.assertEqual(stocks, {"S": 4, "M": 5, "L": 2})

    def test_la_taille_peut_venir_des_proprietes_de_la_ligne(self):
        cmd = self.commande(line_items=[{"sku": "CB-ROBE", "title": "Robe", "quantity": 2,
                                         "properties": [{"name": "Taille", "value": "L"}],
                                         "price": "30.00"}])
        self.moteur([cmd]).sync_orders_from_shopify()
        stocks = dict(self.rows("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ?", (self.pid,)))
        self.assertEqual(stocks, {"S": 4, "M": 6, "L": 0})

    def test_une_taille_indechiffrable_ne_decremente_rien_et_leve_un_drapeau_d_audit(self):
        """Mieux vaut un stock à vérifier qu'une pièce retirée d'une taille qui n'a pas été vendue."""
        cmd = self.commande(line_items=[{"sku": "CB-ROBE", "title": "Robe", "quantity": 1,
                                         "variant_title": "XXL", "price": "30.00"}])
        self.moteur([cmd]).sync_orders_from_shopify()
        stocks = dict(self.rows("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ?", (self.pid,)))
        self.assertEqual(stocks, {"S": 4, "M": 6, "L": 2})
        self.assertEqual(self.rows("SELECT requires_stock_audit FROM Produits WHERE id = ?", (self.pid,)), [(1,)])

    def test_une_survente_est_tracee_et_le_stock_ne_passe_pas_sous_zero(self):
        """Avant, `min(qty, stock)` rabotait la quantité en silence : aucune trace de l'incident."""
        cmd = self.commande(line_items=[{"sku": "CB-ROBE", "title": "Robe", "quantity": 5,
                                         "variant_title": "L", "price": "30.00"}])
        self.moteur([cmd]).sync_orders_from_shopify()
        self.assertEqual(self.rows("SELECT quantite_actuelle, requires_stock_audit FROM Stocks WHERE id = ?",
                                   (self.stocks["L"],)), [(0, 1)])

    def test_un_homonyme_n_est_jamais_decremente_au_hasard(self):
        """Deux produits du même nom : la résolution par nom doit renoncer, pas tirer au sort."""
        self.creer_produit("CB-AUTRE", "Robe", [("Unique", 9)])
        cmd = self.commande(line_items=[{"sku": "", "title": "Robe", "quantity": 1, "price": "30.00"}])
        self.moteur([cmd]).sync_orders_from_shopify()
        self.assertEqual(self.rows("SELECT SUM(quantite_actuelle) FROM Stocks"), [(4 + 6 + 2 + 9,)])

    def test_une_commande_annulee_ne_devient_pas_une_vente(self):
        self.assertEqual(self.moteur([self.commande(cancelled_at="2026-01-02T10:00:00Z")]).sync_orders_from_shopify(), 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Tickets"), [(0,)])

    def test_une_commande_deja_importee_ne_l_est_pas_deux_fois(self):
        self.moteur([self.commande()]).sync_orders_from_shopify()
        self.moteur([self.commande()]).sync_orders_from_shopify()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Tickets"), [(1,)])
        stocks = dict(self.rows("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ?", (self.pid,)))
        self.assertEqual(stocks["M"], 5, "le stock a été décrémenté deux fois pour une seule commande")

    def test_le_prix_de_la_ligne_est_celui_paye_pas_celui_du_catalogue_local(self):
        cmd = self.commande(line_items=[{"sku": "CB-ROBE", "title": "Robe", "quantity": 1,
                                         "variant_title": "M", "price": "24.00"}])
        self.moteur([cmd]).sync_orders_from_shopify()
        self.assertEqual(self.rows("SELECT prix_unitaire_tvac FROM Ventes_Details"), [(Decimal("24.00"),)],
                         "le détail du ticket doit refléter ce que la cliente a réellement payé")

    def test_les_commandes_sont_paginees(self):
        """Sans pagination, l'API n'en renvoyait que 50 : au-delà, plus rien ne remontait."""
        pages = []

        def orders(endpoint, data):
            since = 0
            for kv in endpoint.split("?", 1)[1].split("&"):
                k, _, v = kv.partition("=")
                if k == "since_id":
                    since = int(v)
            pages.append(since)
            lot = [self.commande(id=i, order_number=i) for i in range(since + 1, min(since + 251, 301))]
            return {"orders": lot}

        moteur = MoteurBouchonne({"orders.json": orders},
                                 store_url="boutique.myshopify.com", access_token="jeton")
        self.assertEqual(moteur.sync_orders_from_shopify(), 300)
        self.assertEqual(pages, [0, 250])


# =============================================================================================
# 5. Démarrage automatique : rien ne tourne tant que rien n'est configuré
# =============================================================================================

class TestDemarrageAutomatique(BaseTemporaire):

    def tearDown(self):
        stop_auto_sync()
        os.environ.pop("KODO_SHOPIFY_AUTOSYNC", None)
        super().tearDown()

    def test_rien_ne_demarre_si_shopify_n_est_pas_configure(self):
        os.environ["KODO_SHOPIFY_AUTOSYNC"] = "1"     # on lève le garde-fou « tests » exprès
        self.regler_shopify("", "")
        self.assertIsNone(start_auto_sync())
        self.assertFalse(shopify_sync.auto_sync_actif())

    def test_rien_ne_demarre_si_les_deux_interrupteurs_sont_eteints(self):
        os.environ["KODO_SHOPIFY_AUTOSYNC"] = "1"
        self.regler_shopify("boutique.myshopify.com", "jeton", auto_sync="0", sync_orders="0")
        self.assertIsNone(start_auto_sync())

    def test_rien_ne_demarre_pendant_la_suite_de_tests(self):
        """Garde-fou : même parfaitement configurée, la synchro ne s'invite pas dans les tests."""
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.assertIsNone(start_auto_sync())

    def test_une_fois_configure_le_thread_demarre_et_s_arrete_proprement(self):
        os.environ["KODO_SHOPIFY_AUTOSYNC"] = "1"
        self.regler_shopify("boutique.myshopify.com", "jeton")
        thread = start_auto_sync()
        self.assertIsNotNone(thread)
        self.assertTrue(shopify_sync.auto_sync_actif())
        self.assertTrue(stop_auto_sync())
        self.assertFalse(thread.is_alive(), "le thread n'est pas sorti : l'arrêt de l'app bloquerait")

    def test_chaque_interrupteur_commande_son_sens_de_synchronisation(self):
        self.regler_shopify("boutique.myshopify.com", "jeton", auto_sync="1", sync_orders="0")
        thread = ShopifySyncThread()
        faits = []
        thread.engine.sync_tickets_to_shopify = lambda: faits.append("ventes") or 0
        thread.engine.sync_orders_from_shopify = lambda: faits.append("commandes") or 0
        thread.executer_une_passe()
        self.assertEqual(faits, ["ventes"])

        self.regler_shopify("boutique.myshopify.com", "jeton", auto_sync="0", sync_orders="1")
        faits.clear()
        thread.executer_une_passe()
        self.assertEqual(faits, ["commandes"], "la configuration n'est pas relue à chaud")

    def test_le_recul_exponentiel_espace_les_tentatives_apres_un_echec(self):
        """Marteler l'API toutes les 60 s pendant une panne fait tomber la boutique sous quota."""
        self.regler_shopify("boutique.myshopify.com", "jeton")
        thread = ShopifySyncThread(intervalle_s=60)
        thread.engine.sync_tickets_to_shopify = lambda: (_ for _ in ()).throw(RuntimeError("panne"))
        thread.engine.sync_orders_from_shopify = lambda: 0
        delais = []
        for _ in range(5):
            ok = thread.executer_une_passe()
            thread.echecs_consecutifs = 0 if ok else min(thread.echecs_consecutifs + 1, 8)
            delais.append(min(thread.intervalle_base_s * (2 ** thread.echecs_consecutifs), thread.INTERVALLE_MAX_S))
        self.assertEqual(delais, [120, 240, 480, 900, 900])

    def test_l_etat_de_la_derniere_passe_est_consultable(self):
        """Une synchro qui échoue en boucle doit pouvoir être constatée depuis les réglages."""
        self.regler_shopify("boutique.myshopify.com", "jeton")
        thread = ShopifySyncThread()
        thread.engine.sync_tickets_to_shopify = lambda: (_ for _ in ()).throw(RuntimeError("jeton refusé"))
        thread.engine.sync_orders_from_shopify = lambda: 0
        thread.executer_une_passe()
        etat = shopify_sync.lire_etat_sync()
        self.assertFalse(etat["succes"])
        self.assertIn("jeton refusé", etat["message"])
        self.assertIsNotNone(etat["derniere_synchro"])


# =============================================================================================
# 6. Import du catalogue : plus rien d'inventé
# =============================================================================================

class TestImportCatalogue(BaseTemporaire):

    def moteur(self, produits):
        return MoteurBouchonne({"products.json": lambda e, d: {"products": produits}},
                               store_url="boutique.myshopify.com", access_token="jeton")

    def test_le_prix_d_achat_n_est_plus_invente(self):
        """`prix_vente / 2,5` fabriquait une marge qui alimentait ensuite les statistiques."""
        self.moteur([{"id": 1, "title": "Pull", "variants": [
            {"id": 11, "sku": "CB-PULL", "price": "50.00", "inventory_quantity": 3, "option1": "M"}]}]).import_catalog()
        self.assertEqual(self.rows("SELECT prix_achat_htva FROM Produits"), [(None,)])

    def test_une_variante_sans_code_barres_n_en_recoit_pas_un_faux(self):
        """`SHPF-<id>` était un code non scannable, présenté comme un vrai code-barres."""
        produits = [{"id": 2, "title": "Bougie", "variants": [
            {"id": 22, "price": "9.00", "inventory_quantity": 4, "option1": "Default Title"}]}]
        self.moteur(produits).import_catalog()
        self.assertEqual(self.rows("SELECT code_barre FROM Produits"), [(None,)])
        # Et la variante reste reconnue au ré-import : pas de doublon.
        self.moteur(produits).import_catalog()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Produits"), [(1,)])
        self.assertEqual(self.rows("SELECT variant_id, id_produit FROM Shopify_Variantes"), [(22, 1)])

    def test_un_produit_importe_par_l_ancienne_version_est_adopte_sans_doublon(self):
        """Les bases déjà synchronisées portent des `SHPF-<id>` : on les reprend, on ne les duplique pas."""
        self.ecrire("INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac) "
                    "VALUES ('SHPF-33', 'Ancien', 'Test', '5.00')")
        self.moteur([{"id": 3, "title": "Nouveau nom", "variants": [
            {"id": 33, "price": "9.00", "inventory_quantity": 1, "option1": "Default Title"}]}]).import_catalog()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Produits"), [(1,)])
        self.assertEqual(self.rows("SELECT code_barre, nom FROM Produits"), [("SHPF-33", "Nouveau nom")],
                         "le code-barres existant ne doit pas être réécrit sous les pieds de la boutique")

    def test_un_sku_renomme_cote_shopify_ne_cree_pas_un_second_produit(self):
        """La correspondance de variante est la clé stable : le SKU, lui, peut changer."""
        produits = [{"id": 4, "title": "Sac", "variants": [
            {"id": 44, "sku": "SAC-V1", "price": "20.00", "inventory_quantity": 2, "option1": "Unique"}]}]
        self.moteur(produits).import_catalog()
        produits[0]["variants"][0]["sku"] = "SAC-V2"
        self.moteur(produits).import_catalog()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Produits"), [(1,)])
        self.assertEqual(self.rows("SELECT code_barre FROM Produits"), [("SAC-V1",)])

    def test_les_montants_sont_ecrits_sans_passer_par_un_flottant(self):
        self.moteur([{"id": 5, "title": "Écharpe", "variants": [
            {"id": 55, "sku": "CB-ECH", "price": "19.99", "compare_at_price": "29.99",
             "inventory_quantity": 1, "option1": "Unique"}]}]).import_catalog()
        # Lues en Decimal exact (adaptateur maison) : aucun flottant ne s'est glissé dans la chaîne.
        self.assertEqual(self.rows("SELECT prix_vente_tvac, prix_solde_tvac, en_solde FROM Produits"),
                         [(Decimal("29.99"), Decimal("19.99"), 1)])


# =============================================================================================
# 7. Démonstration de bout en bout, sans aucune boutique Shopify
# =============================================================================================
#
# Ce dernier test est la preuve destinée à quelqu'un qui ne lit pas le code.
#
# On monte, le temps du test, un FAUX Shopify sur la machine elle-même (127.0.0.1) : un petit
# serveur qui répond comme l'API d'administration Shopify (dépôts, produits, recherche de
# variante, ajustement de stock, commandes) et qui REFUSE toute requête ne portant pas le bon
# jeton. On saisit ensuite son adresse et un jeton dans les réglages, exactement comme une
# commerçante le ferait dans l'écran Paramètres, et on laisse Kōdo POS travailler.
#
# À la fin, on vérifie trois choses :
#   - Kōdo a bien tapé sur CE serveur, avec CE jeton (aucune autre adresse n'est jointe) ;
#   - une vente encaissée à la caisse a retiré la pièce de la boutique en ligne ;
#   - une commande passée sur la boutique a retiré la pièce du stock local, à la bonne taille ;
#   - et les deux stocks finissent d'accord.

JETON_DE_TEST = "shpat_jeton_de_la_cliente"

CATALOGUE_FAUSSE_BOUTIQUE = {
    "id": 501,
    "title": "Robe en lin",
    "product_type": "Femme",
    "variants": [
        {"id": 601, "sku": "ROBE-S", "price": "49.00", "option1": "S",
         "inventory_quantity": 4, "inventory_item_id": 9001},
        {"id": 602, "sku": "ROBE-M", "price": "49.00", "option1": "M",
         "inventory_quantity": 6, "inventory_item_id": 9002},
    ],
}


class FauxShopify(BaseHTTPRequestHandler):
    """Répond comme l'API Admin de Shopify. Aucun octet ne sort de la machine."""

    etat = None   # renseigné par le test (stock « distant », journal des requêtes)

    def _repondre(self, code, charge):
        corps = json.dumps(charge).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _jeton_valide(self):
        jeton = self.headers.get("X-Shopify-Access-Token")
        self.etat["jetons_recus"].append(jeton)
        if jeton != JETON_DE_TEST:
            self._repondre(401, {"errors": "[API] Invalid API key or access token"})
            return False
        return True

    def log_message(self, *args):
        pass  # pas de bruit dans la sortie des tests

    def do_GET(self):
        self.etat["chemins"].append(self.path)
        if not self._jeton_valide():
            return
        if self.path.startswith("/admin/api/2025-01/locations.json"):
            return self._repondre(200, {"locations": [{"id": 777, "name": "Dépôt principal", "active": True}]})
        if self.path.startswith("/admin/api/2025-01/products.json"):
            if "since_id=" in self.path:            # deuxième page : plus rien
                return self._repondre(200, {"products": []})
            produit = json.loads(json.dumps(CATALOGUE_FAUSSE_BOUTIQUE))
            for v in produit["variants"]:
                v["inventory_quantity"] = self.etat["stock"][v["inventory_item_id"]]
            return self._repondre(200, {"products": [produit]})
        if self.path.startswith("/admin/api/2025-01/orders.json"):
            if "updated_at_min=" in self.path:
                # Relecture des commandes MODIFIÉES : c'est par là que passent les
                # remboursements, invisibles de la liste filtrée `financial_status=paid`.
                if "since_id=" in self.path or not self.etat["commande_servie"]:
                    return self._repondre(200, {"orders": []})
                commande = dict(self.etat["commande"], updated_at="2026-03-02T09:00:00+01:00")
                if self.etat["remboursee"]:
                    commande["financial_status"] = "refunded"
                    commande["refunds"] = [{
                        "id": 6601, "created_at": "2026-03-02T09:00:00+01:00",
                        "refund_line_items": [{
                            "id": 5501, "quantity": 1, "restock_type": "return",
                            "line_item": {"id": 4401, "sku": "ROBE-M", "title": "Robe en lin",
                                          "variant_title": "M"},
                        }],
                    }]
                return self._repondre(200, {"orders": [commande]})
            if "since_id=" in self.path or self.etat["commande_servie"]:
                return self._repondre(200, {"orders": []})
            self.etat["commande_servie"] = True
            # La boutique a vendu une robe en M : elle a déjà décrémenté SON stock.
            self.etat["stock"][9002] -= 1
            return self._repondre(200, {"orders": [self.etat["commande"]]})
        return self._repondre(404, {"errors": "not found"})

    def do_POST(self):
        self.etat["chemins"].append(self.path)
        if not self._jeton_valide():
            return
        taille = int(self.headers.get("Content-Length") or 0)
        charge = json.loads(self.rfile.read(taille) or b"{}")

        if self.path.startswith("/admin/api/2025-01/graphql.json"):
            recherche = charge.get("variables", {}).get("query", "")
            for v in CATALOGUE_FAUSSE_BOUTIQUE["variants"]:
                if f'"{v["sku"]}"' in recherche:
                    return self._repondre(200, {"data": {"productVariants": {"edges": [
                        {"node": {"inventoryItem": {
                            "id": f"gid://shopify/InventoryItem/{v['inventory_item_id']}"}}}
                    ]}}})
            return self._repondre(200, {"data": {"productVariants": {"edges": []}}})

        if self.path.startswith("/admin/api/2025-01/inventory_levels/adjust.json"):
            item = charge["inventory_item_id"]
            self.etat["stock"][item] += charge["available_adjustment"]
            self.etat["ajustements"].append((item, charge["available_adjustment"]))
            return self._repondre(200, {"inventory_level": {
                "inventory_item_id": item, "available": self.etat["stock"][item]}})

        return self._repondre(404, {"errors": "not found"})


class TestBoutEnBoutSansBoutique(BaseTemporaire):
    """Kōdo POS parle à la boutique des réglages, et le stock circule dans les deux sens."""

    def setUp(self):
        super().setUp()
        FauxShopify.etat = {
            "stock": {9001: 4, 9002: 6}, "jetons_recus": [], "chemins": [],
            "ajustements": [], "commande_servie": False, "remboursee": False,
            "commande": {
                "id": 8801, "order_number": 1001, "total_price": "49.00", "total_tax": "8.50",
                "taxes_included": True, "financial_status": "paid",
                "updated_at": "2026-03-01T09:00:00+01:00",
                "line_items": [{"id": 4401, "sku": "ROBE-M", "title": "Robe en lin",
                                "quantity": 1, "variant_title": "M", "price": "49.00"}],
            },
        }
        self.etat = FauxShopify.etat
        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), FauxShopify)
        self.port = self.serveur.server_address[1]
        self.fil = threading.Thread(target=self.serveur.serve_forever, daemon=True)
        self.fil.start()

    def tearDown(self):
        self.serveur.shutdown()
        self.serveur.server_close()
        self.fil.join(timeout=5)
        FauxShopify.etat = None
        super().tearDown()

    def test_la_caisse_et_la_boutique_finissent_d_accord(self):
        # 1. La commerçante saisit l'adresse de SA boutique et SON jeton dans les réglages.
        self.regler_shopify(f"http://127.0.0.1:{self.port}", JETON_DE_TEST)

        moteur = ShopifySync()          # aucun argument : tout vient des réglages
        self.assertTrue(moteur.est_configure())
        self.assertTrue(moteur.tester_connexion()["success"], "la boutique configurée ne répond pas")

        # 2. Import du catalogue : les deux tailles arrivent avec leur stock.
        self.assertEqual(moteur.import_catalog(), 2)
        stocks = dict(self.rows("SELECT p.code_barre, s.quantite_actuelle FROM Stocks s "
                                "JOIN Produits p ON p.id = s.id_produit"))
        self.assertEqual(stocks, {"ROBE-S": 4, "ROBE-M": 6})

        # 3. Une vente est encaissée à la caisse (vrai parcours de vente du logiciel).
        from kodo_core.api.app import kodo_app
        _, produits = kodo_app.handle_request("GET", "/api/products", {}, {}, {})[:2]
        robe_m = next(p for p in produits if p["barcode"] == "ROBE-M")
        statut, reponse, _ = kodo_app.handle_request("POST", "/api/sales", {}, {}, {
            "items": [{"product": robe_m, "quantity": 1}], "totalTTC": 49.0,
            "paymentMethod": "CB", "cashierName": "Test", "printReceipt": False})
        self.assertEqual(statut, 200, reponse)
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks s JOIN Produits p ON p.id = s.id_produit "
                                   "WHERE p.code_barre = 'ROBE-M'"), [(5,)])

        # 4. La vente est poussée : la boutique en ligne perd la même pièce.
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(self.etat["ajustements"], [(9002, -1)])
        self.assertEqual(self.etat["stock"][9002], 5)

        # 5. La boutique en ligne vend à son tour une robe en M : le stock local suit.
        self.assertEqual(moteur.sync_orders_from_shopify(), 1)
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks s JOIN Produits p ON p.id = s.id_produit "
                                   "WHERE p.code_barre = 'ROBE-M'"), [(4,)],
                         "la commande en ligne n'a pas été retirée du stock local, ou pas de la bonne taille")
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks s JOIN Produits p ON p.id = s.id_produit "
                                   "WHERE p.code_barre = 'ROBE-S'"), [(4,)],
                         "c'est le stock d'une AUTRE taille qui a bougé")

        # 6. La cliente en ligne renvoie sa robe : la boutique la rembourse et la remet en
        #    rayon. La caisse doit le savoir — c'est exactement ce qu'elle ne voyait pas.
        self.etat["remboursee"] = True
        self.etat["stock"][9002] += 1
        self.assertEqual(moteur.sync_refunds_from_shopify(), 1,
                         "le remboursement fait en ligne n'est pas redescendu jusqu'à la caisse")
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks s JOIN Produits p ON p.id = s.id_produit "
                                   "WHERE p.code_barre = 'ROBE-M'"), [(5,)],
                         "l'article rendu n'est pas revenu au stock de la caisse")
        remboursements = self.rows("SELECT numero_ticket, total_tvac FROM Tickets WHERE total_tvac < 0")
        self.assertEqual(len(remboursements), 1, "la vente reste comptée en entier dans le Z")
        self.assertEqual(remboursements[0][1], Decimal("-49.00"))

        # Et la passe suivante n'y revient pas : on ne rembourse pas deux fois.
        self.assertEqual(moteur.sync_refunds_from_shopify(), 0)
        self.assertEqual(len(self.rows("SELECT id FROM Tickets WHERE total_tvac < 0")), 1)

        # 7. Les deux stocks sont d'accord, et toutes les requêtes portaient le bon jeton.
        self.assertEqual(self.etat["stock"][9002], 5)
        self.assertEqual(set(self.etat["jetons_recus"]), {JETON_DE_TEST})
        self.assertTrue(self.etat["chemins"], "aucune requête n'a atteint la boutique configurée")
        for chemin in self.etat["chemins"]:
            self.assertTrue(chemin.startswith("/admin/api/2025-01/"), chemin)

    def test_un_mauvais_jeton_ne_fait_pas_croire_a_une_connexion_reussie(self):
        """Le test de connexion des réglages doit refuser ce que la synchro refuserait aussi."""
        self.regler_shopify(f"http://127.0.0.1:{self.port}", "shpat_mauvais_jeton")
        resultat = ShopifySync().tester_connexion()
        self.assertFalse(resultat["success"])
        self.assertIn("127.0.0.1", resultat["error"])


if __name__ == "__main__":
    unittest.main()
