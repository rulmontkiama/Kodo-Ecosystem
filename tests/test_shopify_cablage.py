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


class TestLeJetonNePartQueVersLaBoutiqueEnregistree(TestCablageShopify):
    """
    Le jeton d'administration ne doit jamais partir vers un domaine choisi par l'appelant.

    `POST /api/shopify/test` complétait le couple champ par champ : un appel local sans jeton,
    avec un domaine quelconque, faisait relire le jeton en base et l'expédiait — en HTTPS
    vérifié, donc proprement — au serveur du demandeur, dans l'en-tête
    `X-Shopify-Access-Token`. Ce jeton ouvre le catalogue, les stocks et les COMMANDES de la
    boutique, donc les données des clientes, depuis n'importe où et longtemps après.

    Deux verrous, testés séparément parce qu'ils protègent deux chemins distincts :
    la route refuse de mélanger appelant et base ; le moteur refuse toute destination qui
    n'est pas une boutique Shopify.
    """

    JETON_REEL = "shpat_JETON_REEL_DE_LA_CLIENTE"

    def setUp(self):
        super().setUp()
        for cle, valeur in (("shopify_store_url", "boutique-de-la-cliente.myshopify.com"),
                            ("shopify_access_token", self.JETON_REEL)):
            conn = database_manager.get_connection()
            try:
                conn.cursor().execute(
                    "INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
                conn.commit()
            finally:
                conn.close()

    def _appeler_en_espionnant_le_reseau(self, charge):
        """Exécute la route en interceptant TOUTE sortie réseau du moteur Shopify."""
        import urllib.request
        emises = []

        def urlopen_espion(req, *args, **kwargs):
            emises.append((req.full_url, dict(req.headers)))
            raise AssertionError(
                f"Le jeton est parti sur le réseau vers {req.full_url!r} : "
                f"en-têtes {dict(req.headers)!r}")

        vrai_urlopen = urllib.request.urlopen
        urllib.request.urlopen = urlopen_espion
        try:
            status, body = self.api("POST", "/api/shopify/test", charge)
        finally:
            urllib.request.urlopen = vrai_urlopen
        return status, body, emises

    def test_un_domaine_d_attaquant_sans_jeton_ne_fait_rien_sortir(self):
        """Le scénario complet : `{"domain": "collecte.attaquant.tld"}`, sans jeton."""
        status, body, emises = self._appeler_en_espionnant_le_reseau(
            {"domain": "collecte.attaquant.tld"})

        self.assertEqual(emises, [],
                         "Une requête est partie vers le domaine de l'appelant.")
        self.assertEqual(status, 400,
                         "La route a accepté de tester un domaine étranger avec le jeton de la base.")
        self.assertNotIn(self.JETON_REEL, repr(body),
                         "Le jeton enregistré ressort dans la réponse de la route.")

    def test_une_autre_boutique_shopify_n_obtient_pas_le_jeton_enregistre(self):
        """
        Le cas que le verrou de destination NE couvre PAS : une boutique Shopify concurrente.

        `attaquant.myshopify.com` est une destination parfaitement valable pour le moteur.
        Seul le refus de mélanger appelant et base empêche le jeton de la cliente d'y partir.
        """
        status, body, emises = self._appeler_en_espionnant_le_reseau(
            {"domain": "attaquant.myshopify.com"})

        self.assertEqual(emises, [],
                         "Le jeton de la cliente est parti vers la boutique Shopify de l'appelant.")
        self.assertEqual(status, 400)

    def test_un_domaine_d_attaquant_avec_un_jeton_fourni_ne_part_pas_non_plus(self):
        """Fournir les deux champs contourne le premier verrou : le moteur doit tenir le second."""
        status, body, emises = self._appeler_en_espionnant_le_reseau(
            {"domain": "collecte.attaquant.tld", "token": "shpat_FOURNI_PAR_L_APPELANT"})

        self.assertEqual(emises, [],
                         "Le moteur a émis une requête vers un domaine qui n'est pas une boutique Shopify.")
        self.assertEqual(status, 400)

    def test_retester_la_boutique_enregistree_reste_possible(self):
        """Le repli complet — aucun champ fourni — sert vraiment, il ne doit pas disparaître."""
        import urllib.request
        emises = []

        class ReponseBidon:
            def read(self_inner):
                return b'{"locations": [{"id": 1, "name": "Depot", "active": true}]}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        def urlopen_bidon(req, *args, **kwargs):
            emises.append((req.full_url, dict(req.headers)))
            return ReponseBidon()

        vrai_urlopen = urllib.request.urlopen
        urllib.request.urlopen = urlopen_bidon
        try:
            status, body = self.api("POST", "/api/shopify/test", {})
        finally:
            urllib.request.urlopen = vrai_urlopen

        self.assertEqual(status, 200, f"Le retest de la boutique enregistrée échoue : {body}")
        self.assertEqual(len(emises), 1)
        url, entetes = emises[0]
        self.assertTrue(url.startswith("https://boutique-de-la-cliente.myshopify.com/"),
                        f"La requête ne vise pas la boutique enregistrée : {url}")
        self.assertEqual(entetes.get("X-shopify-access-token"), self.JETON_REEL)

    def test_seules_les_vraies_boutiques_shopify_sont_des_destinations(self):
        """Le verrou de destination, pris isolément."""
        from kodo_core.sync.shopify import domaine_boutique_valide

        for refuse in ("collecte.attaquant.tld",
                       "boutique.myshopify.com.attaquant.tld",
                       "myshopify.com",
                       ".myshopify.com",
                       "attaquant.tld:443",
                       ""):
            self.assertFalse(domaine_boutique_valide(refuse),
                             f"{refuse!r} est accepté comme destination du jeton d'administration.")

        for accepte in ("boutique-de-la-cliente.myshopify.com",
                        "MaStore.myshopify.com",
                        "localhost:8123",
                        "127.0.0.1:8123"):
            self.assertTrue(domaine_boutique_valide(accepte),
                            f"{accepte!r} devrait être une destination valable.")
