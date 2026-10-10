# -*- coding: utf-8 -*-
"""
Kōdo POS — Le jeton Shopify se renouvelle TOUT SEUL (application récente, « client credentials »).

Depuis le 1er janvier 2026 Shopify ne permet plus de créer d'application personnalisée depuis
l'admin. Une application créée dans le Dev Dashboard n'affiche aucun jeton : elle donne un
identifiant client et un secret, à échanger contre un jeton qui n'est valable QUE 24 HEURES.
Un jeton collé à la main dans l'écran Paramètres cessait donc de marcher le lendemain, la file
d'attente des ventes se bloquait (« 401 : Invalid API key or access token ») et le stock en
ligne ne bougeait plus, sans qu'aucun réglage n'ait changé.

Ces tests décrivent ce qui rend la chose DÉFINITIVE :

1. le jeton est obtenu, mémorisé et renouvelé sans que la commerçante n'y touche ;
2. un jeton refusé (révoqué, expiré plus tôt qu'annoncé) est remplacé UNE fois, et l'appel
   abouti — sans jamais appliquer deux fois un mouvement de stock ;
3. le secret ne sort jamais : ni vers un domaine qui n'est pas `*.myshopify.com`, ni dans un
   message, un journal ou une réponse de l'API locale ;
4. un seul mode d'authentification à la fois, pour qu'un vieux jeton mort ne continue pas
   d'être utilisé en silence derrière ce que l'écran affiche ;
5. la version d'API demandée est explicite, et l'écart avec celle que Shopify SERT se voit.

Aucun appel hors de la machine : un faux Shopify est monté sur 127.0.0.1 le temps du test.
"""
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.sync import shopify as shopify_sync
from kodo_core.sync.shopify import (
    MARGE_RENOUVELLEMENT_JETON_S,
    ShopifySync,
    ShopifySyncThread,
    VERSION_API_DEFAUT,
    lire_reglages_shopify,
    start_auto_sync,
    stop_auto_sync,
)

CLIENT_ID = "72ba89631efb9e4b7199fd6294cde65d"
SECRET = "shpss_secret_de_test_0123456789abcdef"


class FauxShopifyAvecJetons(BaseHTTPRequestHandler):
    """L'API Admin de Shopify, avec son point d'échange identifiant + secret → jeton."""

    etat = None

    def log_message(self, *args):
        pass

    def _repondre(self, code, charge, entetes=None):
        corps = json.dumps(charge).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def _version(self):
        # Shopify dit quelle version il a SERVIE (qui peut différer de celle demandée).
        return {"X-Shopify-API-Version": self.etat.get("version_servie") or self._version_demandee()}

    def _version_demandee(self):
        morceaux = self.path.split("/")
        return morceaux[3] if len(morceaux) > 3 and morceaux[1] == "admin" and morceaux[2] == "api" else ""

    def _authentifie(self):
        jeton = self.headers.get("X-Shopify-Access-Token")
        if jeton in self.etat["jetons_valides"] and not self.etat.get("refuser_tous_les_jetons"):
            return True
        self.etat["refus_401"] += 1
        self._repondre(401, {"errors": "[API] Invalid API key or access token "
                                       "(unrecognized login or wrong password)"})
        return False

    def do_GET(self):
        self.etat["chemins"].append(self.path)
        if not self._authentifie():
            return
        if "/locations.json" in self.path:
            return self._repondre(200, {"locations": [
                {"id": 111, "name": "Boutique", "active": True}]}, self._version())
        if "/orders.json" in self.path:
            return self._repondre(200, {"orders": []}, self._version())
        return self._repondre(404, {"errors": "not found"})

    def do_POST(self):
        taille = int(self.headers.get("Content-Length") or 0)
        brut = self.rfile.read(taille) or b""

        if self.path == "/admin/oauth/access_token":
            self.etat["demandes_jeton"].append(brut.decode("utf-8"))
            champs = {k: v[0] for k, v in urllib.parse.parse_qs(brut.decode("utf-8")).items()}
            if self.etat.get("refuser_identifiants"):
                description = "Client authentication failed"
                code = "invalid_client"
                if self.etat.get("renvoyer_le_secret"):
                    # Un serveur défaillant (ou hostile) qui recopie ce qu'il a reçu, sous un code
                    # d'erreur que Kōdo ne connaît pas : sa description est alors affichée telle quelle.
                    code = "erreur_inconnue"
                    description += f" pour client_secret={champs.get('client_secret')}"
                return self._repondre(400, {"error": code, "error_description": description})
            if self.etat.get("autre_organisation"):
                return self._repondre(400, {"error": "shop_not_permitted"})
            if (champs.get("grant_type") != "client_credentials"
                    or champs.get("client_id") != CLIENT_ID
                    or champs.get("client_secret") != SECRET):
                return self._repondre(400, {"error": "invalid_client"})
            if self.etat.get("delai_emission"):
                time.sleep(self.etat["delai_emission"])
            self.etat["jetons_emis"] += 1
            jeton = f"shpat_emis_{self.etat['jetons_emis']}"
            self.etat["jetons_valides"].add(jeton)
            return self._repondre(200, {"access_token": jeton, "scope": "read_products,write_inventory",
                                        "expires_in": self.etat.get("expires_in", 86399)})

        self.etat["chemins"].append(self.path)
        if not self._authentifie():
            return
        charge = json.loads(brut or b"{}")
        if "/inventory_levels/adjust.json" in self.path:
            self.etat["ajustements"].append((charge["inventory_item_id"], charge["available_adjustment"]))
            return self._repondre(200, {"inventory_level": {"available": 0}}, self._version())
        return self._repondre(404, {"errors": "not found"})


class BaseJeton(unittest.TestCase):
    """Base SQLite jetable + faux Shopify sur 127.0.0.1."""

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()
        FauxShopifyAvecJetons.etat = {
            "jetons_valides": set(), "jetons_emis": 0, "refus_401": 0, "demandes_jeton": [],
            "chemins": [], "ajustements": [],
        }
        self.etat = FauxShopifyAvecJetons.etat
        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), FauxShopifyAvecJetons)
        self.port = self.serveur.server_address[1]
        self.fil = threading.Thread(target=self.serveur.serve_forever, daemon=True)
        self.fil.start()
        self.domaine = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        stop_auto_sync()
        os.environ.pop("KODO_SHOPIFY_AUTOSYNC", None)
        self.serveur.shutdown()
        self.serveur.server_close()
        self.fil.join(timeout=5)
        FauxShopifyAvecJetons.etat = None
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    # --- utilitaires -------------------------------------------------------------------------

    def ecrire(self, cle, valeur):
        conn = database_manager.get_connection()
        try:
            conn.cursor().execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                                  (cle, valeur))
            conn.commit()
        finally:
            conn.close()

    def lire(self, cle):
        conn = database_manager.get_connection()
        try:
            ligne = conn.cursor().execute("SELECT valeur FROM Parametres WHERE cle = ?", (cle,)).fetchone()
            return ligne[0] if ligne else None
        finally:
            conn.close()

    def regler_application(self, domaine=None):
        """Les réglages tels que `POST /api/settings` les écrit pour une application récente."""
        self.ecrire("shopify_store_url", domaine or self.domaine)
        self.ecrire("shopify_client_id", CLIENT_ID)
        self.ecrire("shopify_client_secret", SECRET)
        self.ecrire("shopify_access_token", "")
        self.ecrire("shopify_token_expires_at", "")
        self.ecrire("shopify_auto_sync", "1")
        self.ecrire("shopify_sync_orders", "1")

    def api(self, methode, chemin, donnees=None):
        statut, corps, _ = kodo_app.handle_request(methode, chemin, {}, {}, donnees or {})
        return statut, corps


# =============================================================================================
# 1. Le jeton est obtenu, mémorisé, renouvelé
# =============================================================================================

class TestRenouvellementDuJeton(BaseJeton):

    def test_le_premier_appel_obtient_un_jeton_sans_qu_on_en_ait_saisi(self):
        self.regler_application()
        moteur = ShopifySync()
        self.assertTrue(moteur.est_configure(), "identifiant + secret suffisent à être configuré")

        reponse = moteur.make_request("locations.json")

        self.assertIsNotNone(reponse, moteur.dernier_echec_detail)
        self.assertEqual(self.etat["jetons_emis"], 1)
        self.assertEqual(self.lire("shopify_access_token"), "shpat_emis_1")
        echeance = float(self.lire("shopify_token_expires_at"))
        self.assertGreater(echeance, time.time() + 80000, "l'échéance annoncée (24 h) n'est pas retenue")

    def test_la_demande_est_un_echange_client_credentials_vers_le_domaine_de_la_boutique(self):
        self.regler_application()
        ShopifySync().make_request("locations.json")
        champs = {k: v[0] for k, v in
                  urllib.parse.parse_qs(self.etat["demandes_jeton"][0]).items()}
        self.assertEqual(champs, {"grant_type": "client_credentials",
                                  "client_id": CLIENT_ID, "client_secret": SECRET})

    def test_un_jeton_encore_valable_n_est_pas_redemande(self):
        self.regler_application()
        ShopifySync().make_request("locations.json")
        for _ in range(3):
            ShopifySync().make_request("locations.json")   # d'autres moteurs, comme les routes
        self.assertEqual(self.etat["jetons_emis"], 1)
        self.assertEqual(self.etat["refus_401"], 0)

    def test_un_jeton_proche_de_l_echeance_est_renouvele_avant_l_appel(self):
        self.regler_application()
        self.etat["jetons_valides"].add("shpat_presque_expire")
        self.ecrire("shopify_access_token", "shpat_presque_expire")
        self.ecrire("shopify_token_expires_at", str(int(time.time()) + MARGE_RENOUVELLEMENT_JETON_S - 60))

        reponse = ShopifySync().make_request("locations.json")

        self.assertIsNotNone(reponse)
        self.assertEqual(self.etat["jetons_emis"], 1, "le jeton qui expire n'a pas été remplacé")
        self.assertEqual(self.etat["refus_401"], 0, "le renouvellement doit précéder le refus")
        self.assertEqual(self.lire("shopify_access_token"), "shpat_emis_1")

    def test_un_jeton_d_age_inconnu_est_renouvele(self):
        """Un jeton collé à la main avant que les identifiants soient saisis n'a pas d'échéance."""
        self.regler_application()
        self.etat["jetons_valides"].add("shpat_colle_a_la_main")
        self.ecrire("shopify_access_token", "shpat_colle_a_la_main")
        self.ecrire("shopify_token_expires_at", "")
        ShopifySync().make_request("locations.json")
        self.assertEqual(self.etat["jetons_emis"], 1)

    def test_un_jeton_revoque_est_remplace_une_fois_et_l_appel_aboutit(self):
        """Le jeton paraît valable côté caisse (échéance future) mais Shopify le refuse."""
        self.regler_application()
        self.ecrire("shopify_access_token", "shpat_revoque")
        self.ecrire("shopify_token_expires_at", str(int(time.time()) + 20000))

        moteur = ShopifySync()
        reponse = moteur.make_request("locations.json")

        self.assertIsNotNone(reponse, moteur.dernier_echec_detail)
        self.assertEqual(self.etat["refus_401"], 1, "le jeton révoqué a été essayé une fois seulement")
        self.assertEqual(self.etat["jetons_emis"], 1)
        self.assertEqual(self.lire("shopify_access_token"), "shpat_emis_1")

    def test_un_ajustement_de_stock_refuse_par_un_401_n_est_jamais_applique_deux_fois(self):
        """Le mouvement est RELATIF : le rejouer à tort décrémenterait deux fois la boutique."""
        self.regler_application()
        self.ecrire("shopify_access_token", "shpat_revoque")
        self.ecrire("shopify_token_expires_at", str(int(time.time()) + 20000))

        moteur = ShopifySync()
        ok = moteur.adjust_shopify_stock(9002, "111", -1)

        self.assertTrue(ok)
        self.assertEqual(self.etat["ajustements"], [(9002, -1)],
                         "l'ajustement doit être appliqué exactement une fois")

    def test_un_jeton_refuse_en_boucle_ne_provoque_pas_de_tempete_de_demandes(self):
        """Shopify refuse tout (secret révoqué côté Shopify) : un seul renouvellement par appel."""
        self.regler_application()
        self.etat["refuser_identifiants"] = True
        moteur = ShopifySync()
        self.assertIsNone(moteur.make_request("locations.json"))
        self.assertEqual(len(self.etat["demandes_jeton"]), 1)

    def test_un_renouvellement_refuse_ne_relance_pas_la_requete(self):
        """Jeton révoqué ET secret refusé : une seule tentative avec le jeton mort, puis l'arrêt."""
        self.regler_application()
        self.ecrire("shopify_access_token", "shpat_revoque")
        self.ecrire("shopify_token_expires_at", str(int(time.time()) + 20000))
        self.etat["refuser_identifiants"] = True

        moteur = ShopifySync()
        self.assertIsNone(moteur.make_request("locations.json"))

        self.assertEqual(self.etat["refus_401"], 1, "la requête ne doit pas être relancée à vide")
        self.assertEqual(len(self.etat["demandes_jeton"]), 1)
        self.assertIn("identifiant client ou secret refusé", moteur.dernier_echec_detail)

    def test_un_jeton_neuf_refuse_a_son_tour_n_entraine_pas_de_boucle(self):
        """Shopify refuse même le jeton qu'il vient d'émettre : un seul renouvellement, un seul rejeu."""
        self.regler_application()
        self.ecrire("shopify_access_token", "shpat_revoque")
        self.ecrire("shopify_token_expires_at", str(int(time.time()) + 20000))
        self.etat["refuser_tous_les_jetons"] = True

        moteur = ShopifySync()
        self.assertIsNone(moteur.make_request("locations.json"))

        self.assertEqual(self.etat["jetons_emis"], 1)
        self.assertEqual(self.etat["refus_401"], 2, "au plus : le jeton mort, puis le jeton neuf")

    def test_deux_moteurs_simultanes_ne_demandent_qu_un_seul_jeton(self):
        """Le fil de synchro et l'écran Paramètres : un seul jeton, l'autre moteur l'adopte."""
        self.regler_application()
        self.etat["delai_emission"] = 0.4
        moteurs = [ShopifySync(), ShopifySync()]
        resultats = []

        def appeler(m):
            resultats.append(m.make_request("locations.json"))

        fils = [threading.Thread(target=appeler, args=(m,)) for m in moteurs]
        for f in fils:
            f.start()
        for f in fils:
            f.join(timeout=20)

        self.assertEqual(len(resultats), 2)
        self.assertTrue(all(r is not None for r in resultats))
        self.assertEqual(self.etat["jetons_emis"], 1, "deux jetons émis : l'un invaliderait l'autre")


# =============================================================================================
# 2. Les échecs sont lisibles, et le secret ne fuit nulle part
# =============================================================================================

class TestSecretEtEchecs(BaseJeton):

    def test_des_identifiants_refuses_donnent_une_cause_lisible_sans_le_secret(self):
        self.regler_application()
        self.etat["refuser_identifiants"] = True
        moteur = ShopifySync()

        self.assertIsNone(moteur.make_request("locations.json"))

        self.assertEqual(moteur.dernier_echec, "http")
        self.assertIn("identifiant client ou secret refusé", moteur.dernier_echec_detail)
        self.assertNotIn(SECRET, moteur.dernier_echec_detail)

    def test_meme_si_le_serveur_recopie_le_secret_il_n_est_jamais_conserve(self):
        """Défense en profondeur : une réponse d'erreur qui recopie le secret ne doit pas le garder."""
        self.regler_application()
        self.etat["refuser_identifiants"] = True
        self.etat["renvoyer_le_secret"] = True
        moteur = ShopifySync()
        self.assertIsNone(moteur.make_request("locations.json"))
        self.assertNotIn(SECRET, moteur.dernier_echec_detail)

        ShopifySyncThread().executer_une_passe()
        self.assertNotIn(SECRET, self.lire("shopify_last_sync_message") or "")

    def test_une_autre_organisation_est_expliquee(self):
        self.regler_application()
        self.etat["autre_organisation"] = True
        moteur = ShopifySync()
        self.assertIsNone(moteur.make_request("locations.json"))
        self.assertIn("même organisation", moteur.dernier_echec_detail)

    def test_le_secret_n_apparait_ni_dans_l_etat_de_synchro_ni_dans_les_journaux(self):
        self.regler_application()
        self.etat["refuser_identifiants"] = True
        messages = []

        class Collecteur(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        collecteur = Collecteur(level=logging.DEBUG)
        logger = logging.getLogger("kodo_core.sync.shopify")
        logger.addHandler(collecteur)
        try:
            ShopifySyncThread().executer_une_passe()
        finally:
            logger.removeHandler(collecteur)

        self.assertTrue(self.lire("shopify_last_sync_message"), "l'état de la passe n'est pas écrit")
        self.assertNotIn(SECRET, self.lire("shopify_last_sync_message"))
        self.assertIn("identifiant client ou secret refusé", self.lire("shopify_last_sync_message"))
        self.assertFalse([m for m in messages if SECRET in m], "le secret a été journalisé")

    def test_le_secret_n_est_jamais_envoye_hors_d_un_domaine_myshopify(self):
        """Un domaine saisi à la main ne doit jamais recevoir le secret de l'application."""
        self.regler_application(domaine="boutique-pirate.example.com")
        moteur = ShopifySync()
        self.assertFalse(moteur.est_configure())

        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("requête réseau interdite")) as faux:
            self.assertFalse(moteur.renouveler_jeton())
            self.assertIsNone(moteur.make_request("locations.json"))
        faux.assert_not_called()

    def test_les_reglages_ne_renvoient_jamais_le_secret_ni_le_jeton(self):
        self.regler_application()
        ShopifySync().make_request("locations.json")        # un jeton est maintenant en base

        statut, reglages = self.api("GET", "/api/settings")

        self.assertEqual(statut, 200)
        texte = json.dumps(reglages)
        self.assertNotIn(SECRET, texte)
        self.assertNotIn("shpat_emis_1", texte)
        self.assertEqual(reglages["shopifyClientId"], CLIENT_ID)
        self.assertTrue(reglages["shopifyClientSecretEnregistre"])
        self.assertEqual(reglages["shopifyAuthMode"], "app")
        self.assertTrue(reglages["shopifyConnected"])

    def test_l_etat_de_synchro_n_expose_ni_secret_ni_jeton(self):
        self.regler_application()
        ShopifySync().make_request("locations.json")
        statut, etat = self.api("GET", "/api/shopify/status")
        self.assertEqual(statut, 200)
        texte = json.dumps(etat)
        self.assertNotIn(SECRET, texte)
        self.assertNotIn("shpat_emis_1", texte)
        self.assertEqual(etat["authMode"], "app")
        self.assertTrue(etat["configured"])
        self.assertTrue(etat["tokenExpiresAt"])


# =============================================================================================
# 3. Un seul mode d'authentification à la fois
# =============================================================================================

class TestExclusiviteDesModes(BaseJeton):

    def test_enregistrer_des_identifiants_efface_le_jeton_saisi(self):
        self.ecrire("shopify_access_token", "shpat_ancien_jeton")
        statut, _ = self.api("POST", "/api/settings", {
            "shopifyDomain": "boutique.myshopify.com",
            "shopifyClientId": CLIENT_ID, "shopifyClientSecret": SECRET})
        self.assertEqual(statut, 200)
        self.assertEqual(self.lire("shopify_client_id"), CLIENT_ID)
        self.assertEqual(self.lire("shopify_client_secret"), SECRET)
        self.assertEqual(self.lire("shopify_access_token"), "",
                         "un vieux jeton mort resterait utilisé derrière l'écran")

    def test_enregistrer_un_jeton_efface_les_identifiants(self):
        self.ecrire("shopify_client_id", CLIENT_ID)
        self.ecrire("shopify_client_secret", SECRET)
        statut, _ = self.api("POST", "/api/settings", {
            "shopifyDomain": "boutique.myshopify.com", "shopifyToken": "shpat_nouveau"})
        self.assertEqual(statut, 200)
        self.assertEqual(self.lire("shopify_access_token"), "shpat_nouveau")
        self.assertEqual(self.lire("shopify_client_id"), "")
        self.assertEqual(self.lire("shopify_client_secret"), "")

    def test_deconnecter_la_boutique_debranche_aussi_les_identifiants(self):
        self.regler_application()
        statut, _ = self.api("POST", "/api/settings", {"shopifyDomain": "boutique.myshopify.com",
                                                       "shopifyToken": ""})
        self.assertEqual(statut, 200)
        reglages = lire_reglages_shopify()
        self.assertEqual((reglages["access_token"], reglages["client_id"], reglages["client_secret"]),
                         ("", "", ""))
        self.assertFalse(self.api("GET", "/api/settings")[1]["shopifyConnected"])

    def test_un_masque_affiche_ne_remplace_jamais_le_secret(self):
        self.regler_application()
        self.api("POST", "/api/settings", {"shopifyClientSecret": "••••••••"})
        self.assertEqual(self.lire("shopify_client_secret"), SECRET)

    def test_un_secret_avec_un_espace_est_refuse(self):
        statut, corps = self.api("POST", "/api/settings", {
            "shopifyClientId": CLIENT_ID, "shopifyClientSecret": "shpss_abc def"})
        self.assertEqual(statut, 400, corps)
        self.assertIn("espace", corps["error"])

    def test_l_ecran_de_test_accepte_les_identifiants_sans_rien_enregistrer(self):
        statut, corps = self.api("POST", "/api/shopify/test", {
            "domain": self.domaine, "clientId": CLIENT_ID, "clientSecret": SECRET})
        self.assertEqual(statut, 200, corps)
        self.assertTrue(corps["success"])
        self.assertEqual(self.lire("shopify_client_secret") or "", "",
                         "tester ne doit rien écrire : l'enregistrement est un geste à part")
        self.assertEqual(self.lire("shopify_access_token") or "", "")

    def test_l_ecran_de_test_refuse_un_secret_faux_avec_la_cause(self):
        self.etat["refuser_identifiants"] = True
        statut, corps = self.api("POST", "/api/shopify/test", {
            "domain": self.domaine, "clientId": CLIENT_ID, "clientSecret": "shpss_faux"})
        self.assertEqual(statut, 400)
        self.assertFalse(corps["success"])
        self.assertIn("identifiant client ou secret refusé", corps["error"])

    def test_domaine_et_identifiant_sans_secret_est_refuse(self):
        statut, corps = self.api("POST", "/api/shopify/test", {
            "domain": self.domaine, "clientId": CLIENT_ID})
        self.assertEqual(statut, 400)
        self.assertFalse(corps["success"])


# =============================================================================================
# 4. Démarrage automatique et version d'API
# =============================================================================================

class TestDemarrageEtVersion(BaseJeton):

    def test_la_synchro_demarre_avec_les_seuls_identifiants_de_l_application(self):
        os.environ["KODO_SHOPIFY_AUTOSYNC"] = "1"
        self.regler_application()
        thread = start_auto_sync()
        self.assertIsNotNone(thread, "identifiant + secret suffisent à lancer la synchro")
        self.assertTrue(stop_auto_sync())

    def test_une_passe_obtient_le_jeton_puis_pousse(self):
        self.regler_application()
        passe = ShopifySyncThread()
        self.assertTrue(passe.executer_une_passe())
        self.assertEqual(self.etat["jetons_emis"], 1)
        self.assertEqual(self.lire("shopify_last_sync_ok"), "1")

    def test_le_rafraichissement_manuel_fonctionne_avec_les_seuls_identifiants(self):
        """Le bouton « Rafraîchir la synchronisation » refusait : il exigeait un jeton saisi."""
        self.regler_application()
        resultat = shopify_sync.lancer_rafraichissement()
        self.assertTrue(resultat.get("demarre"), resultat)
        fin = time.time() + 20
        while time.time() < fin and shopify_sync.etat_rafraichissement()["en_cours"]:
            time.sleep(0.1)
        etat = shopify_sync.etat_rafraichissement()
        self.assertFalse(etat["en_cours"])
        self.assertTrue(etat["ok"], etat)
        self.assertEqual(self.etat["jetons_emis"], 1)

    def test_la_version_d_api_est_explicite_et_n_est_plus_la_version_retiree(self):
        self.assertEqual(VERSION_API_DEFAUT, "2026-01")
        self.assertNotEqual(VERSION_API_DEFAUT, "2025-01")
        self.regler_application()
        ShopifySync().make_request("locations.json")
        self.assertTrue(any(f"/admin/api/{VERSION_API_DEFAUT}/" in c for c in self.etat["chemins"]),
                        self.etat["chemins"])

    def test_la_version_peut_etre_forcee_par_le_support_sans_nouvelle_version(self):
        self.regler_application()
        self.ecrire("shopify_api_version", "2026-04")
        ShopifySync().make_request("locations.json")
        self.assertTrue(any("/admin/api/2026-04/" in c for c in self.etat["chemins"]))

    def test_un_ecart_entre_version_demandee_et_servie_est_signale(self):
        self.regler_application()
        self.etat["version_servie"] = "2026-07"
        moteur = ShopifySync()
        moteur.make_request("locations.json")
        self.assertEqual(moteur.version_api_servie, "2026-07")

        passe = ShopifySyncThread()
        passe.executer_une_passe()
        message = self.lire("shopify_last_sync_message")
        self.assertIn("2026-07", message)
        self.assertIn("mise à jour de Kōdo", message)
        self.assertEqual(self.lire("shopify_api_version_servie"), "2026-07")
        statut, etat = self.api("GET", "/api/shopify/status")
        self.assertEqual(etat["apiVersion"], VERSION_API_DEFAUT)
        self.assertEqual(etat["apiVersionServed"], "2026-07")

    def test_aucun_avertissement_quand_la_version_servie_est_celle_demandee(self):
        self.regler_application()
        ShopifySyncThread().executer_une_passe()
        self.assertNotIn("mise à jour de Kōdo", self.lire("shopify_last_sync_message") or "")

    def test_le_mode_jeton_saisi_reste_inchange(self):
        """Une ancienne application personnalisée (shpat_ permanent) continue de marcher telle quelle."""
        self.etat["jetons_valides"].add("shpat_permanent")
        self.ecrire("shopify_store_url", self.domaine)
        self.ecrire("shopify_access_token", "shpat_permanent")
        moteur = ShopifySync()
        self.assertFalse(moteur._a_des_identifiants_app())
        self.assertIsNotNone(moteur.make_request("locations.json"))
        self.assertEqual(self.etat["jetons_emis"], 0, "aucun échange ne doit avoir lieu")
        self.assertEqual(self.etat["demandes_jeton"], [])


if __name__ == "__main__":
    unittest.main()
