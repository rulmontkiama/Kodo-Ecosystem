# -*- coding: utf-8 -*-
"""
Kōdo POS — Une quantité saisie dans Kōdo arrive sur le site, à la BONNE taille.

Cas client (2.0.7 → 2.0.8) : « Le stock de Kōdo est le bon mais le site ne met pas les bonnes
tailles. » La caisse n'envoyait à Shopify que les VENTES. Un réassort ou une correction saisi sur la
fiche produit (écran Stocks) ne partait nulle part : Kōdo affichait le bon stock, la boutique en
ligne gardait l'ancien, sans qu'aucun message ne le dise.

Ce fichier démontre, sans boutique réelle (faux Shopify sur 127.0.0.1, base temporaire) :

1. une quantité saisie à la main est marquée « à aligner » — et SEULEMENT celle-là — sans jamais
   bloquer l'enregistrement de la fiche ;
2. la passe suivante LIT ce que le site affiche et envoie la différence : le site égale la caisse,
   même si l'écart datait d'avant ; rejouée, elle ne change plus rien ;
3. les ventes pas encore envoyées ne sont jamais décomptées deux fois ;
4. une coupure, un refus, une saisie pendant l'envoi n'appliquent jamais deux fois une correction
   et n'en perdent aucune ;
5. l'import du catalogue (qui recopie le site PAR-DESSUS la caisse) refuse d'effacer ces saisies ;
6. l'outil de comparaison montre les écarts SANS rien modifier, et n'aligne que sur demande explicite.
"""
import json
import os
import socket
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.db import shopify_alignement
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.sync import shopify as shopify_sync
from kodo_core.sync.shopify import (
    ImportShopifyBloque,
    ShopifySync,
    ShopifySyncThread,
    compter_file_attente,
    rapport_comparaison_texte,
)
from tests.test_shopify_sync import BaseTemporaire

JETON = "shpat_jeton_de_test"


class FauxStockShopify(BaseHTTPRequestHandler):
    """L'API Admin de Shopify, avec le stock par variante. Aucun octet ne sort de la machine."""

    etat = None

    def log_message(self, *args):
        pass

    def _repondre(self, code, charge):
        corps = json.dumps(charge).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _autorise(self):
        if self.headers.get("X-Shopify-Access-Token") != JETON:
            self._repondre(401, {"errors": "[API] Invalid API key or access token"})
            return False
        return True

    def do_GET(self):
        e = self.etat
        e["chemins"].append(self.path)
        if not self._autorise():
            return
        url = urllib.parse.urlparse(self.path)
        parametres = urllib.parse.parse_qs(url.query)
        if url.path.endswith("/locations.json"):
            return self._repondre(200, {"locations": [{"id": 777, "name": "Dépôt", "active": True}]})
        if url.path.endswith("/orders.json"):
            return self._repondre(200, {"orders": []})
        if url.path.endswith("/products.json"):
            if e.get("catalogue_en_panne"):
                return self._repondre(500, {"errors": "Internal Server Error"})
            if "since_id" in parametres:
                return self._repondre(200, {"products": []})
            if e.get("produits") is not None:
                return self._repondre(200, {"products": e["produits"]})
            variantes = []
            for sku, item in e["variantes"].items():
                variantes.append({"sku": sku, "barcode": "", "inventory_item_id": item})
            for sku, items in e.get("doublons", {}).items():
                for item in items:
                    variantes.append({"sku": sku, "barcode": "", "inventory_item_id": item})
            return self._repondre(200, {"products": [{"id": 1, "variants": variantes}]})
        if url.path.endswith("/inventory_levels.json"):
            demandes = [int(i) for i in parametres.get("inventory_item_ids", [""])[0].split(",") if i]
            niveaux = []
            for item in demandes:
                if item in e["sans_niveau"] or item not in e["niveaux"]:
                    continue
                niveaux.append({"inventory_item_id": item, "location_id": 777,
                                "available": e["niveaux"][item]})
            return self._repondre(200, {"inventory_levels": niveaux})
        return self._repondre(404, {"errors": "not found"})

    def do_POST(self):
        e = self.etat
        e["chemins"].append(self.path)
        taille = int(self.headers.get("Content-Length") or 0)
        brut = self.rfile.read(taille) or b"{}"
        if not self._autorise():
            return
        charge = json.loads(brut)
        if self.path.endswith("/graphql.json"):
            recherche = charge.get("variables", {}).get("query", "")
            noeuds = []
            for sku, item in e["variantes"].items():
                if f'"{sku}"' in recherche:
                    noeuds.append({"node": {"sku": sku, "barcode": "", "inventoryItem": {
                        "id": f"gid://shopify/InventoryItem/{item}"}}})
            return self._repondre(200, {"data": {"productVariants": {"edges": noeuds}}})
        if self.path.endswith("/inventory_levels/adjust.json"):
            item, delta = charge["inventory_item_id"], charge["available_adjustment"]
            e["tentatives_ajustement"] += 1
            if e["reussites_avant_echec"] > 0:
                e["reussites_avant_echec"] -= 1
            elif e["echecs_ajustement"] > 0:
                e["echecs_ajustement"] -= 1
                return self._repondre(500, {"errors": "Internal Server Error"})
            e["niveaux"][item] += delta
            e["ajustements"].append((item, delta))
            if e["quand_ajustement"]:
                e["quand_ajustement"]()
            if e["coupure_apres_ajustement"] > 0:
                # Appliqué côté Shopify, mais la réponse n'arrive jamais : le cas qui fait doubler.
                e["coupure_apres_ajustement"] -= 1
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            return self._repondre(200, {"inventory_level": {
                "inventory_item_id": item, "available": e["niveaux"][item]}})
        return self._repondre(404, {"errors": "not found"})


class BaseAlignement(BaseTemporaire):
    """Un pull en M/L (SKU `CF-7907-1-M`, `CF-7907-1-L`) dont le site affiche M 5, L 3."""

    def setUp(self):
        super().setUp()
        FauxStockShopify.etat = {
            "chemins": [], "ajustements": [], "variantes": {"CF-7907-1-M": 101, "CF-7907-1-L": 102},
            "niveaux": {101: 5, 102: 3}, "sans_niveau": set(), "doublons": {},
            "echecs_ajustement": 0, "reussites_avant_echec": 0, "tentatives_ajustement": 0,
            "coupure_apres_ajustement": 0, "quand_ajustement": None, "catalogue_en_panne": False,
        }
        self.etat = FauxStockShopify.etat
        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), FauxStockShopify)
        self.port = self.serveur.server_address[1]
        self.fil = threading.Thread(target=self.serveur.serve_forever, daemon=True)
        self.fil.start()

        self.regler_shopify(f"http://127.0.0.1:{self.port}", JETON)
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                    (shopify_sync.PARAM_SEUIL_TICKETS, "0"))
        self.pid = self.creer_produit("CF-7907-1", "Pull noir col demi-montant", [("M", 5), ("L", 3)])
        self.stock = {t: i for i, t in self.rows(
            "SELECT id, taille FROM Stocks WHERE id_produit = ?", (self.pid,))}

    def tearDown(self):
        self.serveur.shutdown()
        self.serveur.server_close()
        self.fil.join(timeout=5)
        FauxStockShopify.etat = None
        super().tearDown()

    # --- outils -------------------------------------------------------------------------------

    def saisir(self, **tailles):
        """La commerçante enregistre la fiche produit avec ces quantités (vrai chemin du logiciel)."""
        sizes = " | ".join(f"{t}:{q}" for t, q in tailles.items())
        return InventoryManager.save_product({
            "id": self.pid, "name": "Pull noir col demi-montant", "barcode": "CF-7907-1",
            "price": 50, "sizes": sizes})

    def vendre(self, taille, quantite=1, numero="TCK-2026-0001"):
        """Une vente en caisse : la quantité est DÉJÀ retirée, le ticket n'est pas encore envoyé."""
        self.ecrire("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - ? WHERE id = ?",
                    (quantite, self.stock[taille]))
        return self.creer_ticket(numero, [(self.stock[taille], quantite)])

    def passe(self):
        return ShopifySyncThread().executer_une_passe()

    def marques(self):
        return self.rows("SELECT stock_id, statut, version, tentatives, ajustement, quantite_site, "
                         "derniere_erreur FROM Shopify_Alignements ORDER BY stock_id")

    def quantite(self, taille):
        return self.rows("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (self.stock[taille],))[0][0]

    def ajustements_site(self):
        return list(self.etat["ajustements"])


# =============================================================================================
# 1. Une quantité saisie est marquée — elle seule — et l'enregistrement n'échoue jamais pour ça
# =============================================================================================

class TestMarquage(BaseAlignement):

    def test_une_quantite_modifiee_est_marquee_a_aligner(self):
        self.saisir(M=8, L=3)
        lignes = self.marques()
        self.assertEqual(len(lignes), 1, "seule la ligne dont la quantité a changé doit être marquée")
        self.assertEqual(lignes[0][0], self.stock["M"])
        self.assertEqual(lignes[0][1], shopify_alignement.STATUT_A_ALIGNER)

    def test_une_fiche_enregistree_sans_changement_ne_marque_rien(self):
        self.saisir(M=5, L=3)
        existe = self.rows("SELECT name FROM sqlite_master WHERE name = 'Shopify_Alignements'")
        self.assertTrue(not existe or self.marques() == [])

    def test_ressaisir_incremente_la_version(self):
        self.saisir(M=8, L=3)
        self.saisir(M=9, L=3)
        ligne = self.marques()[0]
        self.assertEqual(ligne[1], shopify_alignement.STATUT_A_ALIGNER)
        self.assertEqual(ligne[2], 2, "chaque saisie doit se voir : la passe en cours ne peut pas la rater")

    def test_une_taille_ajoutee_est_marquee(self):
        self.saisir(M=5, L=3, XL=2)
        ids = [r[0] for r in self.marques()]
        xl = self.rows("SELECT id FROM Stocks WHERE id_produit = ? AND taille = 'XL'", (self.pid,))[0][0]
        self.assertEqual(ids, [xl])

    def test_un_produit_neuf_n_est_pas_marque(self):
        """Il n'est pas encore en ligne (l'export en fixe le stock) : le chercher serait vain."""
        InventoryManager.save_product({"name": "Veste", "barcode": "VST1", "price": 60, "sizes": "M:3 | L:2"})
        existe = self.rows("SELECT name FROM sqlite_master WHERE name = 'Shopify_Alignements'")
        self.assertTrue(not existe or self.marques() == [])

    def test_une_taille_vendue_puis_retiree_reste_a_zero_et_suit_le_site(self):
        self.vendre("L", 1)
        self.saisir(M=5)                      # L retirée de la fiche, mais déjà vendue : gardée à 0
        self.assertEqual(self.quantite("L"), 0)
        self.assertIn(self.stock["L"], [r[0] for r in self.marques()],
                      "« mettez son stock à 0 pour le retirer de la vente » : le site doit suivre")

    def test_une_taille_jamais_vendue_et_retiree_n_a_plus_de_marquage(self):
        self.saisir(M=8, L=4)                 # M et L marquées
        self.assertIn(self.stock["L"], [r[0] for r in self.marques()])
        self.saisir(M=8)                      # L jamais vendue : supprimée
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Stocks WHERE id = ?", (self.stock["L"],))[0][0], 0)
        self.assertNotIn(self.stock["L"], [r[0] for r in self.marques()],
                         "le marquage d'une ligne supprimée ne doit pas survivre à la ligne")

    def test_rien_n_est_marque_tant_que_shopify_n_est_pas_relie(self):
        self.ecrire("DELETE FROM Parametres WHERE cle IN ('shopify_store_url', 'shopify_access_token')")
        self.saisir(M=8, L=3)
        existe = self.rows("SELECT name FROM sqlite_master WHERE name = 'Shopify_Alignements'")
        self.assertTrue(not existe or self.marques() == [],
                        "avant la connexion, rien n'est « rattrapé » dans le dos de la commerçante")
        self.assertEqual(self.quantite("M"), 8, "l'enregistrement du stock reste intact")

    def test_rien_n_est_marque_quand_la_synchro_du_stock_est_eteinte(self):
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_auto_sync', '0')")
        self.saisir(M=8, L=3)
        existe = self.rows("SELECT name FROM sqlite_master WHERE name = 'Shopify_Alignements'")
        self.assertTrue(not existe or self.marques() == [])

    def test_un_defaut_du_marquage_n_empeche_jamais_d_enregistrer_le_stock(self):
        with mock.patch.object(shopify_alignement, "poussee_stock_active", side_effect=RuntimeError("boum")):
            self.saisir(M=8, L=3)
        self.assertEqual(self.quantite("M"), 8)
        journal = self.rows("SELECT avant, apres FROM Stock_Journal WHERE stock_id = ?", (self.stock["M"],))
        self.assertEqual(journal, [(5, 8)], "la trace d'audit reste écrite")

    def test_le_marquage_est_annule_avec_l_ecriture_qu_il_decrit(self):
        """Écrit DANS la transaction de la fiche : annulée, la quantité ET le marquage disparaissent."""
        from kodo_core.domain.catalog.inventory_manager import _norm_taille
        conn = database_manager.get_connection()
        try:
            InventoryManager._ecrire_lignes_de_stock(
                conn.cursor(), self.pid,
                {_norm_taille("M"): ["M", 8], _norm_taille("L"): ["L", 3]}, None)
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(self.quantite("M"), 5)
        existe = self.rows("SELECT name FROM sqlite_master WHERE name = 'Shopify_Alignements'")
        self.assertTrue(not existe or self.marques() == [])

    def test_les_cles_de_reglage_sont_celles_du_moteur(self):
        self.assertEqual(shopify_alignement._CLE_URL, shopify_sync.CLE_URL)
        self.assertEqual(shopify_alignement._CLE_TOKEN, shopify_sync.CLE_TOKEN)
        self.assertEqual(shopify_alignement._CLE_CLIENT_ID, shopify_sync.CLE_CLIENT_ID)
        self.assertEqual(shopify_alignement._CLE_CLIENT_SECRET, shopify_sync.CLE_CLIENT_SECRET)
        self.assertEqual(shopify_alignement._CLE_AUTO_SYNC, shopify_sync.CLE_AUTO_SYNC)

    def test_le_mode_application_recente_compte_comme_relie(self):
        self.ecrire("DELETE FROM Parametres WHERE cle = 'shopify_access_token'")
        for cle, valeur in (("shopify_client_id", "id"), ("shopify_client_secret", "secret")):
            self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
        self.saisir(M=8, L=3)
        self.assertEqual(len(self.marques()), 1)


# =============================================================================================
# 2. La passe aligne le site sur la caisse, une seule fois
# =============================================================================================

class TestAlignement(BaseAlignement):

    def test_un_reassort_arrive_sur_le_site(self):
        self.saisir(M=8, L=3)                              # +3 en M
        self.assertTrue(self.passe())
        self.assertEqual(self.ajustements_site(), [(101, 3)])
        self.assertEqual(self.etat["niveaux"], {101: 8, 102: 3})
        ligne = self.marques()[0]
        self.assertEqual((ligne[1], ligne[4], ligne[5]), (shopify_alignement.STATUT_ALIGNE, 3, 5))

    def test_rejouee_la_passe_ne_change_plus_rien(self):
        self.saisir(M=8, L=3)
        self.passe()
        avant = list(self.etat["ajustements"])
        self.assertTrue(self.passe())
        self.assertTrue(self.passe())
        self.assertEqual(self.etat["ajustements"], avant)
        self.assertEqual(self.etat["niveaux"][101], 8)

    def test_un_ecart_ancien_est_gueri_par_la_lecture(self):
        """Le site affichait 2 (des réassorts jamais envoyés) : il rejoint la caisse, pas « +3 »."""
        self.etat["niveaux"][101] = 2
        self.saisir(M=8, L=3)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 8)
        self.assertEqual(self.ajustements_site(), [(101, 6)])

    def test_un_site_deja_correct_ne_recoit_aucun_ajustement(self):
        self.etat["niveaux"][101] = 8                      # quelqu'un l'a corrigé dans Shopify
        self.saisir(M=8, L=3)
        self.passe()
        self.assertEqual(self.ajustements_site(), [])
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_ALIGNE)

    def test_chaque_taille_va_a_sa_variante(self):
        self.saisir(M=7, L=9)
        self.passe()
        self.assertEqual(sorted(self.ajustements_site()), [(101, 2), (102, 6)])
        self.assertEqual(self.etat["niveaux"], {101: 7, 102: 9})

    def test_une_correction_a_la_baisse(self):
        self.saisir(M=2, L=3)                              # inventaire : il n'y en a que 2
        self.passe()
        self.assertEqual(self.ajustements_site(), [(101, -3)])
        self.assertEqual(self.etat["niveaux"][101], 2)

    def test_une_vente_pas_encore_envoyee_n_est_jamais_decomptee_deux_fois(self):
        self.vendre("M", 1)                                # caisse 4, site 5 : la vente n'est pas partie
        self.saisir(M=7, L=3)                              # réassort : « il y en a 7 »
        self.passe()                                       # ventes d'abord (−1), alignement ensuite
        self.assertEqual(self.etat["niveaux"][101], 7, "le site doit égaler la caisse, ni 6 ni 8")
        self.assertEqual(self.quantite("M"), 7)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 7)

    def test_vente_en_attente_dont_l_envoi_echoue_la_valeur_visee_la_compte(self):
        """Envoi de la vente refusé (500) : le site doit alors afficher caisse + vente en attente,
        puis retomber sur la caisse quand la vente partira."""
        self.vendre("M", 1)
        self.saisir(M=7, L=3)
        self.etat["echecs_ajustement"] = 1                 # le 1er ajustement (la vente) est refusé
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 8, "caisse 7 + 1 vente pas encore reçue")
        self.ecrire("UPDATE sync_queue SET prochaine_tentative = '2000-01-01 00:00:00'")  # délai de recul écoulé
        self.passe()                                       # la vente part, l'alignement est déjà fait
        self.assertEqual(self.etat["niveaux"][101], 7)
        self.assertEqual(self.quantite("M"), 7)

    def test_une_ligne_deja_envoyee_d_un_ticket_partiel_n_est_pas_recomptee(self):
        """Ticket M + L dont seule la ligne M est partie : M est au journal, elle n'est plus « en attente »."""
        self.ecrire("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - 1")
        self.creer_ticket("TCK-2026-0002", [(self.stock["M"], 1), (self.stock["L"], 1)])
        self.etat["reussites_avant_echec"] = 1            # la ligne M part, la ligne L est refusée
        self.etat["echecs_ajustement"] = 1
        self.saisir(M=6, L=2)                              # réassort de M seulement (caisse 4 → 6)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 6, "ni 7 (ligne M recomptée) ni 5")
        self.ecrire("UPDATE sync_queue SET prochaine_tentative = '2000-01-01 00:00:00'")  # délai de recul écoulé
        self.passe()                                       # la ligne L part à son tour
        self.assertEqual(self.etat["niveaux"], {101: 6, 102: 2})

    def test_les_ventes_d_avant_le_seuil_sont_deja_comptees_par_le_site(self):
        ticket = self.vendre("M", 1)
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                    (shopify_sync.PARAM_SEUIL_TICKETS, str(ticket)))
        self.etat["niveaux"][101] = 4                      # le site tient déjà compte de cette vente
        self.saisir(M=6, L=3)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 6)

    def test_enregistrer_une_fiche_reveille_la_synchronisation(self):
        with mock.patch.object(shopify_sync, "reveiller_sync") as reveil:
            statut, _, _ = kodo_app.handle_request("POST", "/api/products", {}, {}, {
                "id": self.pid, "name": "Pull noir col demi-montant", "barcode": "CF-7907-1",
                "price": 50, "sizes": "M:8 | L:3"})
        self.assertEqual(statut, 200)
        reveil.assert_called_once()

    def test_article_non_publie_en_ligne_rien_a_aligner(self):
        del self.etat["variantes"]["CF-7907-1-M"]
        self.saisir(M=8, L=3)
        self.passe()
        ligne = self.marques()[0]
        self.assertEqual(ligne[1], shopify_alignement.STATUT_ABSENT)
        self.assertIn("introuvable", ligne[6])
        self.assertEqual(self.ajustements_site(), [])

    def test_stock_non_suivi_en_ligne_rien_a_aligner(self):
        self.etat["sans_niveau"].add(101)
        self.saisir(M=8, L=3)
        self.passe()
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_SANS_NIVEAU)
        self.assertEqual(self.ajustements_site(), [])

    def test_un_refus_de_shopify_est_retente_et_applique_une_seule_fois(self):
        self.etat["echecs_ajustement"] = 1
        self.saisir(M=8, L=3)
        self.assertFalse(self.passe(), "l'échec doit se voir : la passe n'est pas « tout va bien »")
        ligne = self.marques()[0]
        self.assertEqual((ligne[1], ligne[3]), (shopify_alignement.STATUT_A_ALIGNER, 1))
        self.assertIn("refusé", ligne[6])
        self.assertTrue(self.passe())
        self.assertEqual(self.ajustements_site(), [(101, 3)])
        self.assertEqual(self.etat["niveaux"][101], 8)

    def test_reponse_perdue_apres_application_aucun_doublon(self):
        """L'ajustement a porté mais la réponse n'est jamais arrivée : la relecture le constate."""
        self.etat["coupure_apres_ajustement"] = 1
        self.saisir(M=8, L=3)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 8, "appliqué une fois côté Shopify")
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_A_ALIGNER)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 8, "la relecture voit 8 : rien n'est renvoyé")
        self.assertEqual(self.ajustements_site(), [(101, 3)])
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_ALIGNE)

    def test_une_saisie_pendant_l_envoi_n_est_pas_perdue(self):
        """Ressaisie entre la lecture et la clôture : la ligne reste à traiter, puis rejoint 9."""
        declenchee = []

        def pendant_l_envoi():
            if not declenchee:
                declenchee.append(1)
                self.saisir(M=9, L=3)

        self.etat["quand_ajustement"] = pendant_l_envoi
        self.saisir(M=8, L=3)
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 8)
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_A_ALIGNER,
                         "la ressaisie ne doit pas être déclarée alignée sur 8")
        self.passe()
        self.assertEqual(self.etat["niveaux"][101], 9)

    def test_une_ligne_supprimee_avant_la_passe_n_appelle_pas_shopify(self):
        self.saisir(M=8, L=3)
        self.ecrire("DELETE FROM Stocks WHERE id = ?", (self.stock["M"],))
        appels = len(self.etat["chemins"])
        moteur = ShopifySync()
        bilan = moteur.aligner_lignes_modifiees()
        self.assertEqual(bilan["supprimees"], 1)
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_SUPPRIMEE)
        self.assertEqual(self.ajustements_site(), [])
        # un seul appel est toléré : la recherche du dépôt, qui précède le traitement des lignes
        self.assertLessEqual(len(self.etat["chemins"]) - appels, 1)

    def test_la_synchro_du_stock_eteinte_n_aligne_rien(self):
        self.saisir(M=8, L=3)
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_auto_sync', '0')")
        self.passe()
        self.assertEqual(self.ajustements_site(), [])
        self.assertEqual(self.marques()[0][1], shopify_alignement.STATUT_A_ALIGNER)

    def test_une_passe_traite_un_nombre_borne_de_lignes(self):
        self.saisir(M=8, L=9)
        moteur = ShopifySync()
        bilan = moteur.aligner_lignes_modifiees(limite=1)
        self.assertEqual((bilan["traitees"], bilan["restantes"]), (1, 1))
        bilan = moteur.aligner_lignes_modifiees(limite=1)
        self.assertEqual((bilan["traitees"], bilan["restantes"]), (1, 0))

    def test_la_commercante_voit_les_quantites_en_attente(self):
        self.saisir(M=8, L=3)
        self.assertEqual(compter_file_attente()["pending"], 1)
        _, statut, _ = kodo_app.handle_request("GET", "/api/shopify/status", {}, {}, {})
        self.assertEqual(statut["alignmentsPending"], 1)
        self.assertEqual(statut["pendingCount"], 1)
        self.passe()
        self.assertEqual(compter_file_attente()["pending"], 0)
        _, statut, _ = kodo_app.handle_request("GET", "/api/shopify/status", {}, {}, {})
        self.assertEqual(statut["alignmentsPending"], 0)

    def test_la_route_de_fiche_produit_marque_aussi(self):
        """Le vrai chemin de l'écran Stocks : POST /api/products."""
        statut, reponse, _ = kodo_app.handle_request("POST", "/api/products", {}, {}, {
            "id": self.pid, "name": "Pull noir col demi-montant", "barcode": "CF-7907-1",
            "price": 50, "sizes": "M:8 | L:3"})
        self.assertEqual(statut, 200, reponse)
        self.assertEqual(len(self.marques()), 1)


# =============================================================================================
# 3. L'import du catalogue (site → caisse) n'efface pas ces saisies
# =============================================================================================

class TestImportProtege(BaseAlignement):

    def test_l_import_est_refuse_avant_tout_appel_reseau(self):
        self.saisir(M=8, L=3)
        moteur = ShopifySync()
        appels = len(self.etat["chemins"])
        with self.assertRaises(ImportShopifyBloque) as ctx:
            moteur.import_catalog()
        self.assertEqual(ctx.exception.raison, "stock_non_envoye")
        self.assertIn("1 ligne(s) de stock saisie(s)", str(ctx.exception))
        self.assertIn("Rafraîchir la synchronisation", str(ctx.exception))
        self.assertEqual(len(self.etat["chemins"]), appels, "le refus précède tout appel réseau")
        self.assertEqual(self.quantite("M"), 8, "la saisie n'a pas été écrasée par le site")

    def test_la_route_d_import_repond_409(self):
        self.saisir(M=8, L=3)
        statut, reponse, _ = kodo_app.handle_request("POST", "/api/shopify/import", {}, {}, {})
        self.assertEqual(statut, 409)
        self.assertEqual(reponse["reason"], "stock_non_envoye")
        self.assertEqual(reponse["unsentLines"], 1)

    def test_apres_alignement_l_import_n_est_plus_refuse(self):
        self.saisir(M=8, L=3)
        self.passe()
        statut, _, _ = kodo_app.handle_request("POST", "/api/shopify/import", {}, {}, {})
        self.assertNotEqual(statut, 409)

    def test_un_import_force_remplace_les_saisies_et_clot_les_marquages(self):
        self.saisir(M=8, L=3)
        moteur = ShopifySync()
        with mock.patch.object(ShopifySync, "_fetch_all_products", return_value=[
                {"id": 1, "title": "Pull noir col demi-montant", "product_type": "Femme", "variants": [
                    {"id": 11, "sku": "CF-7907-1-M", "price": "50.00", "inventory_quantity": 5, "option1": "M"},
                    {"id": 12, "sku": "CF-7907-1-L", "price": "50.00", "inventory_quantity": 3, "option1": "L"},
                ]}]), mock.patch.object(ShopifySync, "_sauvegarder_avant_import", return_value=""):
            moteur.import_catalog(forcer=True)
        self.assertEqual(self.quantite("M"), 5, "l'import forcé recopie le site, comme avant")
        ligne = self.marques()[0]
        self.assertEqual(ligne[1], shopify_alignement.STATUT_ALIGNE)
        self.assertIn("import", ligne[6])


# =============================================================================================
# 4. La comparaison est en lecture seule ; l'alignement demandé n'agit que sur confirmation
# =============================================================================================

class TestComparaison(BaseAlignement):

    def setUp(self):
        super().setUp()
        # Le site affiche M 5 (juste) et L 1 (la caisse en attend 3) ; un troisième article n'est
        # pas publié, un quatrième a deux variantes au même SKU, un cinquième n'est pas suivi.
        self.etat["niveaux"][102] = 1
        self.creer_produit("HORS-LIGNE", "Écharpe", [("Unique", 4)])
        self.creer_produit("DOUBLE", "Bonnet", [("Unique", 2)])
        self.creer_produit("NONSUIVI", "Gants", [("Unique", 6)])
        self.etat["doublons"] = {"DOUBLE": [201, 202]}
        self.etat["variantes"]["NONSUIVI"] = 301
        self.etat["niveaux"][301] = 6
        self.etat["sans_niveau"].add(301)

    def comparer(self):
        return ShopifySync().comparer_stock()

    def test_les_etats_sont_distingues(self):
        rapport = self.comparer()
        etats = {(l["produit"], l["taille"]): l["etat"] for l in rapport["lignes"]}
        self.assertEqual(etats[("Pull noir col demi-montant", "M")], "identique")
        self.assertEqual(etats[("Pull noir col demi-montant", "L")], "ecart")
        self.assertEqual(etats[("Écharpe", "Unique")], "absent")
        self.assertEqual(etats[("Bonnet", "Unique")], "ambigu")
        self.assertEqual(etats[("Gants", "Unique")], "sans_niveau")
        self.assertEqual(rapport["resume"], {"lignes": 5, "identiques": 1, "ecarts": 1, "absentes": 1,
                                             "ambigues": 1, "sansNiveau": 1})

    def test_l_ecart_est_calcule_sur_caisse_plus_ventes_en_attente(self):
        self.vendre("L", 1)                                # caisse 2, site 1, une vente pas partie
        ligne = next(l for l in self.comparer()["lignes"] if l["taille"] == "L" and l["etat"] != "absent")
        self.assertEqual((ligne["quantiteKodo"], ligne["ventesEnAttente"], ligne["cible"],
                          ligne["quantiteSite"], ligne["ecart"]), (2, 1, 3, 1, 2))

    def test_la_comparaison_ne_modifie_rien(self):
        avant = self.rows("SELECT id, quantite_actuelle FROM Stocks ORDER BY id")
        self.comparer()
        self.assertEqual(self.ajustements_site(), [])
        self.assertFalse([c for c in self.etat["chemins"] if "adjust" in c])
        self.assertEqual(self.rows("SELECT id, quantite_actuelle FROM Stocks ORDER BY id"), avant)
        existe = self.rows("SELECT name FROM sqlite_master WHERE name = 'Shopify_Alignements'")
        self.assertTrue(not existe or self.marques() == [])

    def test_le_rapport_en_texte_nomme_les_lignes(self):
        texte = rapport_comparaison_texte(self.comparer())
        self.assertIn("Pull noir col demi-montant — L [CF-7907-1-L]", texte)
        self.assertIn("caisse 3", texte)
        self.assertIn("site 1", texte)
        self.assertIn("+2", texte)
        self.assertIn("Écharpe", texte)
        self.assertIn("SKU AMBIGUS", texte)
        self.assertIn("confirmer", texte)

    def test_la_route_texte(self):
        statut, corps, entetes = kodo_app.handle_request(
            "GET", "/api/shopify/stock-comparison", {"format": ["text"]}, {}, {})
        self.assertEqual(statut, 200)
        self.assertIsInstance(corps, bytes)
        self.assertIn("text/plain", entetes["Content-Type"])
        self.assertIn("Pull noir col demi-montant", corps.decode("utf-8"))

    def test_la_route_json(self):
        statut, corps, _ = kodo_app.handle_request("GET", "/api/shopify/stock-comparison", {}, {}, {})
        self.assertEqual(statut, 200)
        self.assertTrue(corps["success"])
        self.assertEqual(corps["resume"]["ecarts"], 1)

    def test_l_alignement_exige_une_confirmation_explicite(self):
        for charge in ({}, {"confirmer": "oui"}, {"confirmer": 1}, {"confirmer": False}):
            statut, corps, _ = kodo_app.handle_request("POST", "/api/shopify/stock-align", {}, {}, charge)
            self.assertEqual(statut, 400, charge)
            self.assertFalse(corps["success"])
        self.assertEqual(self.ajustements_site(), [])
        self.assertEqual(self.etat["niveaux"][102], 1)

    def test_l_alignement_confirme_n_ajuste_que_les_ecarts(self):
        statut, corps, _ = kodo_app.handle_request(
            "POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True})
        self.assertEqual(statut, 200, corps)
        self.assertEqual(self.ajustements_site(), [(102, 2)])
        self.assertEqual(self.etat["niveaux"][102], 3)
        self.assertEqual(self.etat["niveaux"][101], 5, "une ligne déjà égale n'est pas touchée")
        self.assertEqual(corps["bilan"]["ajustees"], 1)
        self.assertEqual(len(corps["lignes"]), 1, "seule la ligne en écart est traitée")
        self.assertEqual(corps["lignes"][0]["ajustement"], 2)
        self.assertEqual(corps["lignes"][0]["quantiteSiteAvant"], 1)
        self.assertEqual([r[0] for r in self.marques()], [self.stock["L"]],
                         "ni les lignes absentes, ni les ambiguës, ni les identiques ne sont marquées")

    def test_les_sku_ambigus_ne_sont_jamais_ajustes(self):
        kodo_app.handle_request("POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True})
        self.assertNotIn(201, [i for i, _ in self.ajustements_site()])
        self.assertNotIn(202, [i for i, _ in self.ajustements_site()])

    def test_rejoue_l_alignement_ne_change_plus_rien(self):
        kodo_app.handle_request("POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True})
        statut, corps, _ = kodo_app.handle_request(
            "POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True})
        self.assertEqual(statut, 200)
        self.assertEqual(corps["bilan"]["ajustees"], 0)
        self.assertEqual(self.ajustements_site(), [(102, 2)])

    def test_stock_ids_restreint_l_alignement(self):
        autre = self.rows("SELECT s.id FROM Stocks s JOIN Produits p ON p.id = s.id_produit "
                          "WHERE p.nom = 'Écharpe'")[0][0]
        statut, corps, _ = kodo_app.handle_request(
            "POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True, "stockIds": [autre]})
        self.assertEqual(statut, 200)
        self.assertEqual(self.ajustements_site(), [], "la ligne en écart n'était pas dans la liste")

    def test_stock_ids_invalides_sont_refuses(self):
        for ids in ("1,2", [1, "2"], [True], 5):
            statut, _, _ = kodo_app.handle_request(
                "POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True, "stockIds": ids})
            self.assertEqual(statut, 400, ids)

    def test_catalogue_illisible_rien_n_est_compare_ni_modifie(self):
        self.etat["catalogue_en_panne"] = True
        statut, corps, _ = kodo_app.handle_request("GET", "/api/shopify/stock-comparison", {}, {}, {})
        self.assertEqual(statut, 502)
        self.assertIn("rien n'a été comparé", corps["error"])
        statut, _, _ = kodo_app.handle_request("POST", "/api/shopify/stock-align", {}, {}, {"confirmer": True})
        self.assertEqual(statut, 502)
        self.assertEqual(self.ajustements_site(), [])

    def test_sans_boutique_configuree_la_route_repond_400(self):
        self.ecrire("DELETE FROM Parametres WHERE cle IN ('shopify_store_url', 'shopify_access_token')")
        statut, _, _ = kodo_app.handle_request("GET", "/api/shopify/stock-comparison", {}, {}, {})
        self.assertEqual(statut, 400)


# =============================================================================================
# 5. Un article que Kōdo ne retrouve pas par son SKU : la comparaison dit à quoi il ressemble
# =============================================================================================

class TestSuggestion(BaseAlignement):
    """
    Kōdo cherche `<code>-<taille>`. Si la boutique a numéroté ses variantes autrement (un EAN par
    taille, un SKU libre), les ventes ne partent pas et la ligne reste « introuvable ». La
    comparaison le DIT, avec la variante probable — sans jamais s'en servir pour agir.
    """

    def setUp(self):
        super().setUp()
        self.etat["variantes"] = {}          # aucun SKU attendu par Kōdo n'existe en ligne
        self.etat["niveaux"] = {501: 4, 502: 2}
        self.etat["produits"] = [self.produit_en_ligne(1, "Pull noir col demi-montant", 9001, 501)]

    @staticmethod
    def produit_en_ligne(ident, titre, premiere_variante, premier_item, skus=("PULL-NOIR-M", "PULL-NOIR-L")):
        return {"id": ident, "title": titre, "variants": [
            {"id": premiere_variante, "sku": skus[0], "barcode": "", "option1": "M", "option2": None,
             "inventory_item_id": premier_item},
            {"id": premiere_variante + 1, "sku": skus[1], "barcode": "", "option1": "L", "option2": None,
             "inventory_item_id": premier_item + 1}]}

    def lignes(self):
        return {l["taille"]: l for l in ShopifySync().comparer_stock()["lignes"]}

    def test_meme_nom_et_meme_taille(self):
        lignes = self.lignes()
        self.assertEqual(lignes["M"]["etat"], "absent")
        suggestion = lignes["M"]["suggestion"]
        self.assertEqual(suggestion["skuEnLigne"], "PULL-NOIR-M")
        self.assertEqual(suggestion["via"], "même nom et même taille")
        self.assertEqual(lignes["L"]["suggestion"]["skuEnLigne"], "PULL-NOIR-L")

    def test_le_nom_est_compare_sans_accent_ni_casse(self):
        self.etat["produits"] = [self.produit_en_ligne(1, "PULL  NOIR col demi-montant", 9001, 501)]
        self.assertEqual(self.lignes()["M"]["suggestion"]["skuEnLigne"], "PULL-NOIR-M")

    def test_variante_deja_reliee_par_un_import_meme_si_le_nom_a_change(self):
        self.etat["produits"] = [self.produit_en_ligne(1, "Pull col demi-montant (nouvelle collection)", 9001, 501)]
        self.ecrire("INSERT INTO Shopify_Variantes (variant_id, id_produit, date_maj) VALUES (9001, ?, 'x')",
                    (self.pid,))
        self.ecrire("INSERT INTO Shopify_Variantes (variant_id, id_produit, date_maj) VALUES (9002, ?, 'x')",
                    (self.pid,))
        suggestion = self.lignes()["L"]["suggestion"]
        self.assertEqual(suggestion["skuEnLigne"], "PULL-NOIR-L")
        self.assertIn("import", suggestion["via"])

    def test_deux_produits_en_ligne_de_meme_nom_aucune_suggestion(self):
        self.etat["produits"].append(self.produit_en_ligne(
            2, "Pull noir col demi-montant", 9101, 601, skus=("AUTRE-M", "AUTRE-L")))
        self.etat["niveaux"].update({601: 1, 602: 1})
        lignes = self.lignes()
        self.assertIsNone(lignes["M"]["suggestion"], "une réponse non unique n'est jamais donnée")

    def test_la_suggestion_n_agit_jamais(self):
        self.saisir(M=8, L=3)
        ShopifySync().aligner_lignes_modifiees()
        self.assertEqual(self.ajustements_site(), [])
        self.assertEqual(self.etat["niveaux"], {501: 4, 502: 2})
        resultat = ShopifySync().aligner_stock()
        self.assertEqual(resultat["bilan"]["ajustees"], 0)
        self.assertEqual(self.ajustements_site(), [])

    def test_le_rapport_en_texte_montre_la_variante_probable(self):
        texte = rapport_comparaison_texte(ShopifySync().comparer_stock())
        self.assertIn("SKU INTROUVABLE EN LIGNE", texte)
        self.assertIn("→ en ligne : « Pull noir col demi-montant », taille M, SKU « PULL-NOIR-M »", texte)

    def test_article_sans_declinaison_suggere_par_son_nom(self):
        self.creer_produit("ECH-1", "Écharpe laine", [("Taille Unique", 4)])
        self.etat["produits"].append({"id": 3, "title": "Echarpe laine", "variants": [
            {"id": 7001, "sku": "", "barcode": "3700000000011", "option1": "Default Title", "option2": None,
             "inventory_item_id": 701}]})
        self.etat["niveaux"][701] = 4
        ligne = next(l for l in ShopifySync().comparer_stock()["lignes"] if l["produit"] == "Écharpe laine")
        self.assertEqual(ligne["suggestion"]["codeBarresEnLigne"], "3700000000011")


if __name__ == "__main__":
    unittest.main()
