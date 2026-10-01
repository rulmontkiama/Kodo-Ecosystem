"""
File d'attente locale `sync_queue`, session HTTP persistante et rafraîchissement manuel.

Aucun appel externe : moteur bouchonné ou serveur HTTP sur 127.0.0.1. Base jetable.
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from test_shopify_sync import (  # noqa: E402
    BaseTemporaire, MoteurBouchonne, REPONSE_LOCATIONS, reponse_graphql,
)
from kodo_core.sync import shopify as shopify_sync  # noqa: E402
from kodo_core.sync.shopify import ShopifySync, compter_file_attente  # noqa: E402


class TestFileAttente(BaseTemporaire):

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        pid = self.creer_produit("CB-1", "Article", [("Unique", 9)])
        sid = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]
        self.creer_ticket("T-1", [(sid, 2)])

    def moteur(self, adjust):
        return MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": lambda e, d: reponse_graphql(1001),
            "inventory_levels/adjust.json": adjust,
        }, store_url="boutique.myshopify.com", access_token="jeton")

    def test_la_ligne_est_inscrite_pending_puis_done_apres_envoi(self):
        envoyes = []
        moteur = self.moteur(lambda e, d: (envoyes.append(d["available_adjustment"]), {"ok": 1})[1])
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(envoyes, [-2], "ajustement RELATIF négatif uniquement")
        self.assertEqual(self.rows("SELECT statut, delta, tentatives FROM sync_queue"), [("DONE", -2, 0)])
        self.assertEqual(compter_file_attente(), {"pending": 0, "indeterminate": 0})

    def test_un_refus_shopify_laisse_pending_puis_la_passe_suivante_rejoue(self):
        etat = {"panne": True}
        envoyes = []

        def adjust(endpoint, data):
            if etat["panne"]:
                return None
            envoyes.append(data["available_adjustment"])
            return {"ok": 1}

        moteur = self.moteur(adjust)
        self.assertEqual(moteur.sync_tickets_to_shopify(), 0)
        self.assertEqual(self.rows("SELECT statut, tentatives FROM sync_queue"), [("PENDING", 1)])
        self.assertEqual(compter_file_attente()["pending"], 1)
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets"), [(0,)])

        etat["panne"] = False                      # retour de la connexion
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(envoyes, [-2], "la vente ne part qu'une seule fois")
        self.assertEqual(self.rows("SELECT statut FROM sync_queue"), [("DONE",)])

    def test_une_panne_de_la_file_ne_bloque_pas_la_synchro(self):
        """La file éclaire, elle ne gouverne pas : écriture impossible ⇒ la vente part quand même."""
        self.ecrire("CREATE TRIGGER refuse_file BEFORE INSERT ON sync_queue "
                    "BEGIN SELECT RAISE(ABORT, 'file indisponible'); END")
        envoyes = []
        moteur = self.moteur(lambda e, d: (envoyes.append(d["available_adjustment"]), {"ok": 1})[1])
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(envoyes, [-2])
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets"), [(1,)])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connexions = 0

    def setup(self):
        type(self).connexions += 1
        super().setup()

    def log_message(self, *a):
        pass

    def do_GET(self):
        corps = json.dumps({"locations": [{"id": 1, "active": True, "name": "X"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)


class TestSessionPersistante(BaseTemporaire):

    def test_plusieurs_requetes_reutilisent_la_meme_connexion(self):
        _Handler.connexions = 0
        serveur = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        fil = threading.Thread(target=serveur.serve_forever, daemon=True)
        fil.start()
        try:
            port = serveur.server_address[1]
            moteur = ShopifySync(store_url=f"http://127.0.0.1:{port}", access_token="jeton")
            for _ in range(4):
                self.assertIsNotNone(moteur.make_request("locations.json"))
            self.assertEqual(_Handler.connexions, 1, "une connexion par requête : keep-alive absent")
        finally:
            serveur.shutdown()
            serveur.server_close()


class TestRafraichissementManuel(BaseTemporaire):

    def test_refus_propre_si_shopify_n_est_pas_connecte(self):
        res = shopify_sync.lancer_rafraichissement()
        self.assertFalse(res["demarre"])
        self.assertIn("erreur", res)

    def test_le_rafraichissement_est_asynchrone_et_expose_son_resultat(self):
        self.regler_shopify("boutique.myshopify.com", "jeton")
        appels = []

        def passe(self_thread):
            appels.append(time.time())
            time.sleep(0.2)
            return True

        original = shopify_sync.ShopifySyncThread.passe_exclusive
        shopify_sync.ShopifySyncThread.passe_exclusive = passe
        try:
            t0 = time.time()
            res = shopify_sync.lancer_rafraichissement()
            self.assertTrue(res["demarre"])
            self.assertLess(time.time() - t0, 0.15, "le déclenchement doit rendre la main aussitôt")
            self.assertTrue(shopify_sync.etat_rafraichissement()["en_cours"])
            self.assertTrue(shopify_sync.lancer_rafraichissement().get("deja_en_cours"))
            for _ in range(50):
                if not shopify_sync.etat_rafraichissement()["en_cours"]:
                    break
                time.sleep(0.05)
            etat = shopify_sync.etat_rafraichissement()
            self.assertFalse(etat["en_cours"])
            self.assertTrue(etat["ok"])
            self.assertEqual(len(appels), 1)
        finally:
            shopify_sync.ShopifySyncThread.passe_exclusive = original


class TestRoutes(BaseTemporaire):

    def appeler(self, methode, chemin):
        from kodo_core.api.app import kodo_app
        statut, corps, _ = kodo_app.handle_request(methode, chemin, {}, {}, {})[:3]
        return statut, corps

    def test_refresh_refuse_sans_boutique_et_status_expose_la_file(self):
        statut, corps = self.appeler("POST", "/api/shopify/refresh")
        self.assertEqual(statut, 400)
        self.assertFalse(corps["success"])
        statut, corps = self.appeler("GET", "/api/shopify/status")
        self.assertEqual(statut, 200)
        self.assertEqual(corps["pendingCount"], 0)
        self.assertIn("en_cours", corps["refresh"])

    def test_refresh_demarre_puis_status_rend_le_resultat(self):
        self.regler_shopify("boutique.myshopify.com", "jeton")
        original = shopify_sync.ShopifySyncThread.passe_exclusive
        shopify_sync.ShopifySyncThread.passe_exclusive = lambda self_thread: True
        try:
            statut, corps = self.appeler("POST", "/api/shopify/refresh")
            self.assertEqual((statut, corps["started"]), (200, True))
            for _ in range(50):
                _, etat = self.appeler("GET", "/api/shopify/status")
                if not etat["refresh"]["en_cours"]:
                    break
                time.sleep(0.05)
            self.assertFalse(etat["refresh"]["en_cours"])
            self.assertTrue(etat["refresh"]["fin"])
        finally:
            shopify_sync.ShopifySyncThread.passe_exclusive = original


if __name__ == "__main__":
    unittest.main()
