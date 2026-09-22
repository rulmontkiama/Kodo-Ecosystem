# -*- coding: utf-8 -*-
"""
Kōdo POS — La synchronisation Shopify doit être RÉELLEMENT branchée dans le produit.

Le moteur de synchro existait, complet et crédible, depuis longtemps. Il n'était démarré
qu'en `main_app.py`, l'ancienne interface Tkinter que le produit ne lance plus : dans le
serveur réellement exécuté par `launch_app.py`, le mot « shopify » n'apparaissait nulle part.
Conséquence : aucune vente ne décrémentait le stock de la boutique en ligne, aucune commande
en ligne ne décrémentait le stock de la caisse, et les deux interrupteurs de l'écran Réglages
étaient écrits en base sans que personne ne les lise jamais.

Ces tests interdisent le débranchement, et vérifient qu'un réglage saisi prend effet
immédiatement plutôt qu'au prochain redémarrage de la caisse.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database_manager
from kodo_core.api.app import kodo_app


class TestCablageShopify(unittest.TestCase):

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
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def api(self, method, path, data=None):
        status, body, _ = kodo_app.handle_request(method, path, {}, {}, data or {})
        return status, body

    def test_le_serveur_de_production_demarre_la_synchronisation(self):
        """`run_server` est le seul point d'entrée réel : le branchement doit y vivre."""
        source = (Path(__file__).resolve().parent.parent / "server_pos.py").read_text(encoding="utf-8")
        self.assertIn(
            "start_auto_sync", source,
            "server_pos.py ne démarre plus la synchro Shopify : le produit redevient muet "
            "vis-à-vis de la boutique en ligne, exactement comme avant la correction.")
        # Et le branchement doit précéder serve_forever(), qui ne rend jamais la main.
        self.assertLess(
            source.index("start_auto_sync"), source.index("httpd.serve_forever()"),
            "Le démarrage de la synchro est placé après serve_forever() : il ne s'exécuterait jamais.")

    def test_enregistrer_les_reglages_applique_la_configuration_sans_redemarrer(self):
        """Brancher la boutique doit agir tout de suite, pas au prochain lancement."""
        import kodo_core.sync.shopify as shopify

        appels = []
        original = shopify.start_auto_sync
        shopify.start_auto_sync = lambda *a, **kw: appels.append(True)
        try:
            status, body = self.api("POST", "/api/settings", {
                "shopifyDomain": "mastore.myshopify.com",
                "shopifyToken": "shpat_jeton_de_la_cliente",
                "autoSyncStock": True,
                "syncOrders": True,
            })
        finally:
            shopify.start_auto_sync = original

        self.assertEqual(status, 200, body)
        self.assertEqual(
            len(appels), 1,
            "Le réglage est enregistré mais la synchro n'est pas relancée : la commerçante "
            "voit « enregistré » et croit sa boutique reliée pour la journée.")

    def test_un_reglage_sans_shopify_ne_touche_pas_a_la_synchronisation(self):
        """Changer le nom de la boutique ne doit pas redémarrer la synchro."""
        import kodo_core.sync.shopify as shopify

        appels = []
        original = shopify.start_auto_sync
        shopify.start_auto_sync = lambda *a, **kw: appels.append(True)
        try:
            status, _ = self.api("POST", "/api/settings", {"storeName": "Boutique X"})
        finally:
            shopify.start_auto_sync = original

        self.assertEqual(status, 200)
        self.assertEqual(appels, [], "Un réglage sans rapport relance la synchro inutilement.")

    def test_le_domaine_est_nettoye_avant_d_etre_enregistre(self):
        """Un domaine collé depuis l'admin Shopify ne doit pas être stocké tel quel."""
        status, _ = self.api("POST", "/api/settings", {
            "shopifyDomain": "https://mastore.myshopify.com/admin/products?x=1",
            "shopifyToken": "shpat_jeton_de_la_cliente",
        })
        self.assertEqual(status, 200)

        conn = database_manager.get_connection()
        try:
            valeur = conn.execute(
                "SELECT valeur FROM Parametres WHERE cle = 'shopify_store_url'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            valeur, "mastore.myshopify.com",
            "Le chemin « /admin » conservé produisait des appels vers /admin/admin/api/…, "
            "un 404 que l'application lisait comme « la boutique n'a aucun produit ».")


if __name__ == "__main__":
    unittest.main()


class TestJetonShopifyNeSortPas(TestCablageShopify):
    """
    Le jeton d'administration Shopify ouvre le catalogue, les stocks et les commandes de la
    boutique. `GET /api/settings` le renvoyait en clair à chaque ouverture de l'écran Réglages,
    et l'interface le recopiait dans le stockage du navigateur : hors de la base, hors de toute
    sauvegarde chiffrée, sans expiration, lisible par quiconque ouvre la caisse.
    """

    def enregistrer_jeton(self, jeton="shpat_secret_de_la_boutique"):
        status, _ = self.api("POST", "/api/settings",
                             {"shopifyDomain": "boutique.myshopify.com", "shopifyToken": jeton})
        self.assertEqual(status, 200)

    def jeton_en_base(self):
        conn = database_manager.get_connection()
        try:
            row = conn.cursor().execute(
                "SELECT valeur FROM Parametres WHERE cle = 'shopify_access_token'").fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_le_jeton_enregistre_n_est_jamais_renvoye_par_l_api(self):
        self.enregistrer_jeton()
        _, body = self.api("GET", "/api/settings")
        self.assertEqual(body.get("shopifyToken"), "",
                         "le jeton d'administration ne doit plus transiter vers l'écran")
        self.assertNotIn("shpat_secret_de_la_boutique", str(body),
                         "aucune trace du jeton, sous aucune clé, dans la réponse")

    def test_l_ecran_sait_quand_meme_qu_une_cle_est_enregistree(self):
        """Ne plus servir le jeton ne doit pas rendre l'écran aveugle."""
        _, avant = self.api("GET", "/api/settings")
        self.assertFalse(avant.get("shopifyTokenEnregistre"))
        self.assertFalse(avant.get("shopifyConnected"))

        self.enregistrer_jeton()
        _, apres = self.api("GET", "/api/settings")
        self.assertTrue(apres.get("shopifyTokenEnregistre"))
        self.assertTrue(apres.get("shopifyConnected"))

    def test_renvoyer_le_masque_affiche_ne_remplace_pas_la_vraie_cle(self):
        """
        Un écran qui renverrait les puces qu'il affiche débrancherait la boutique en silence :
        le jeton serait remplacé par des points, et la synchro échouerait sans que rien ne
        dise pourquoi. La valeur est reconnue comme un affichage et ignorée.
        """
        self.enregistrer_jeton()
        for masque in ("\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022", "********", "\u2022\u2022\u2022 \u2022\u2022\u2022"):
            self.api("POST", "/api/settings", {"shopifyToken": masque})
            self.assertEqual(self.jeton_en_base(), "shpat_secret_de_la_boutique",
                             f"le masque {masque!r} a écrasé la vraie clé")

    def test_la_chaine_vide_reste_un_ordre_de_deconnexion(self):
        """La déconnexion volontaire envoie bien '' : elle ne doit pas être confondue avec un masque."""
        self.enregistrer_jeton()
        self.api("POST", "/api/settings", {"shopifyToken": ""})
        self.assertEqual(self.jeton_en_base(), "")
        _, body = self.api("GET", "/api/settings")
        self.assertFalse(body.get("shopifyConnected"))

    def test_l_interface_ne_conserve_plus_le_jeton_dans_le_navigateur(self):
        """Garde textuelle : le jeton ne doit revenir sous AUCUNE forme dans le stockage local."""
        racine = Path("/Users/kiamarulmont/Desktop/kōdo-pos-3/src")
        if not racine.exists():
            self.skipTest("interface non présente sur cette machine")
        coupables = []
        for fichier in racine.rglob("*.ts*"):
            texte = fichier.read_text(encoding="utf-8")
            for numero, ligne in enumerate(texte.splitlines(), 1):
                if "kodo_shopify_token" in ligne and "removeItem" not in ligne:
                    coupables.append(f"{fichier.name}:{numero}")
        self.assertEqual(coupables, [],
                         "le jeton d'administration Shopify est de nouveau écrit ou lu "
                         "dans le stockage du navigateur : " + ", ".join(coupables))
