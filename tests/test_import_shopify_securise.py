# -*- coding: utf-8 -*-
"""
Kōdo POS — L'import du catalogue Shopify ne fait plus « remonter » le stock, et tout écrasement laisse une trace.

Cas client (2.0.6) : « les ventes ne se décomptent pas, la quantité initiale reste ». La base montrait
des ventes bien décomptées en caisse, mais la synchro vers Shopify était bloquée depuis plusieurs
jours (file `sync_queue` en attente, « échec de communication (http) » sans autre détail). Or
l'import du catalogue recopie le stock de Shopify par-dessus celui de la caisse en supposant que les
ventes y sont déjà comptées : lancé pendant la panne, il aurait fait réapparaître en stock tous les
articles vendus depuis. Rien dans la base ne permettait ensuite de dire quand ni par quoi une
quantité avait changé.

Ce fichier démontre, sans boutique Shopify réelle (réponses bouchonnées, base temporaire) :

1. l'import est REFUSÉ, avant tout appel réseau, tant que des ventes n'ont pas été envoyées à Shopify,
   et cette notion d'« envoyée » est EXACTEMENT celle de la boucle de synchronisation ;
2. une copie de sécurité de la base précède l'écrasement, et son échec annule l'import ;
3. une vente ou une poussée survenue PENDANT le téléchargement du catalogue annule l'import ;
4. chaque quantité réécrite (import, fiche produit) est consignée avant/après dans `Stock_Journal`,
   sans jamais bloquer l'écriture qu'elle décrit ;
5. une synchro qui échoue dit POURQUOI (code HTTP, message de Shopify, erreur GraphQL), sans jamais
   exposer le jeton, et la route d'import ne présente plus un refus du serveur comme un succès.
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.config import ShopConfig
from kodo_core.db import stock_journal
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.sync import shopify as shopify_sync
from kodo_core.sync.shopify import ImportShopifyBloque, ShopifySync, ShopifySyncThread
from tests.test_shopify_sku_taille import FausseBoutique
from tests.test_shopify_sync import REPONSE_LOCATIONS, BaseTemporaire, MoteurBouchonne


class BaseImport(BaseTemporaire):
    """Une robe en S/M dont la CAISSE a déjà décompté des ventes que Shopify ne connaît pas encore."""

    def setUp(self):
        super().setUp()
        # Les copies de sécurité vont dans un dossier jetable, même hors pytest.
        self.dossier_copies = tempfile.mkdtemp(prefix="kodo_copies_")
        patch = mock.patch.object(ShopConfig, "get_snapshots_dir", return_value=self.dossier_copies)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.dossier_copies, True)

        conn = database_manager.get_connection()
        try:
            shopify_sync._assurer_tables_sync(conn.cursor())
            conn.commit()
        finally:
            conn.close()
        self.regler_shopify("boutique.myshopify.com", "shpat_jeton_secret")
        # État de la caisse APRÈS ventes : S 2, M 5. Shopify, lui, dit encore S 4, M 6.
        self.pid = self.creer_produit("BI038", "Robe", [("S", 2), ("M", 5)])
        self.stock = {taille: ident for ident, taille in self.rows(
            "SELECT id, taille FROM Stocks WHERE id_produit = ?", (self.pid,))}

    # --- outils -------------------------------------------------------------------------------------

    def fixer_seuil(self, valeur="0"):
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                    (shopify_sync.PARAM_SEUIL_TICKETS, str(valeur)))

    def quantites(self):
        return dict(self.rows("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ? ORDER BY id",
                              (self.pid,)))

    def journal(self):
        try:
            return self.rows("SELECT origine, produit, taille, avant, apres FROM Stock_Journal ORDER BY id")
        except sqlite3.OperationalError:
            return []

    def copies(self):
        return sorted(n for n in os.listdir(self.dossier_copies) if n.startswith("kodo_pos_pre_import_"))

    def catalogue(self, s=4, m=6):
        return [{"id": 5, "title": "Robe", "product_type": "Femme", "variants": [
            {"id": 51, "sku": "BI038-S", "price": "50.00", "inventory_quantity": s, "option1": "S"},
            {"id": 52, "sku": "BI038-M", "price": "50.00", "inventory_quantity": m, "option1": "M"}]}]

    def moteur(self, produits=None, pendant_telechargement=None, **kw):
        """ShopifySync dont `make_request` sert le catalogue ; `pendant_telechargement` simule la vie de la boutique."""
        produits = self.catalogue() if produits is None else produits
        eng = ShopifySync(store_url="boutique.myshopify.com", access_token="shpat_jeton_secret")
        eng.requetes = []

        def faux(endpoint, method="GET", data=None, max_retries=3, rejouable=True):
            eng.requetes.append(endpoint)
            if pendant_telechargement:
                pendant_telechargement()
            return {"products": [p for p in produits if p["id"] > 0]}

        eng.make_request = faux
        return eng

    def marquer_envoye(self, ticket_id, statut=shopify_sync.STATUT_POUSSE):
        """Comme la boucle de synchro après un envoi réussi : journal ligne à ligne + ticket synchronisé."""
        for vd_id, in self.rows("SELECT id FROM Ventes_Details WHERE id_ticket = ?", (ticket_id,)):
            self.ecrire("INSERT INTO Shopify_Sync_Lignes (id_vente_detail, id_ticket, code_barre, "
                        "inventory_item_id, quantite_poussee, statut, date_heure) "
                        "VALUES (?, ?, 'BI038-S', 1, -1, ?, '2026-01-01 10:00:00')", (vd_id, ticket_id, statut))
        self.ecrire("UPDATE Tickets SET synced_shopify = 1 WHERE id = ?", (ticket_id,))


# =============================================================================================
# 1. Refus tant que des ventes n'ont pas été envoyées à Shopify
# =============================================================================================

class TestRefusTantQueDesVentesNeSontPasEnvoyees(BaseImport):

    def setUp(self):
        super().setUp()
        self.fixer_seuil("0")
        self.ticket = self.creer_ticket("TCK-2026-0102", [(self.stock["S"], 2), (self.stock["M"], 1)])

    def test_l_import_est_refuse_et_rien_ne_change(self):
        eng = self.moteur()
        with self.assertRaises(ImportShopifyBloque) as ctx:
            eng.import_catalog()
        self.assertEqual(ctx.exception.raison, "ventes_non_envoyees")
        self.assertEqual(ctx.exception.bilan["lignes"], 2)
        self.assertEqual(ctx.exception.bilan["tickets"], 1)
        self.assertEqual(self.quantites(), {"S": 2, "M": 5}, "les ventes décomptées en caisse ont été effacées")
        self.assertEqual(eng.requetes, [], "le refus doit précéder tout appel réseau")
        self.assertEqual(self.copies(), [], "aucune copie inutile quand rien n'est modifié")
        self.assertEqual(self.journal(), [])

    def test_le_message_nomme_les_tickets_et_la_marche_a_suivre(self):
        message = ""
        try:
            self.moteur().import_catalog()
        except ImportShopifyBloque as bloque:
            message = str(bloque)
        self.assertIn("TCK-2026-0102", message)
        self.assertIn("2 ligne(s)", message)
        self.assertIn("Rafraîchir la synchronisation", message, "le bouton existe dans Paramètres")
        self.assertNotIn("shpat_", message, "le jeton ne doit jamais apparaître dans un message d'écran")

    def test_le_dernier_message_de_synchro_est_joint_au_refus(self):
        """La commerçante voit POURQUOI les ventes ne partent pas, au moment précis où elle en a besoin."""
        shopify_sync.enregistrer_etat_sync(False, "échec de communication (http — 401 : [API] Invalid API key)")
        with self.assertRaises(ImportShopifyBloque) as ctx:
            self.moteur().import_catalog()
        self.assertIn("401 : [API] Invalid API key", str(ctx.exception))

    def test_plusieurs_tickets_sont_resumes_par_leur_premier_et_dernier_numero(self):
        self.creer_ticket("TCK-2026-0103", [(self.stock["S"], 1)])
        with self.assertRaises(ImportShopifyBloque) as ctx:
            self.moteur().import_catalog()
        self.assertEqual(ctx.exception.bilan["tickets"], 2)
        self.assertIn("du TCK-2026-0102 au TCK-2026-0103", str(ctx.exception))

    def test_une_fois_les_ventes_envoyees_l_import_passe_et_recopie_shopify(self):
        self.marquer_envoye(self.ticket)
        eng = self.moteur()
        self.assertEqual(eng.import_catalog(), 2)
        self.assertEqual(self.quantites(), {"S": 4, "M": 6}, "après envoi, Shopify FAIT autorité (comportement voulu)")
        self.assertEqual(eng.dernier_import["modifiees"], 2)

    def test_une_ligne_deja_tentee_ne_bloque_pas(self):
        """INDETERMINE / ABSENT_SHOPIFY : la boucle ne les repousse jamais, elles ne peuvent pas bloquer l'import."""
        self.marquer_envoye(self.ticket, statut=shopify_sync.STATUT_INDETERMINE)
        self.ecrire("UPDATE Tickets SET synced_shopify = 0")   # le ticket reste « à vérifier à la main »
        self.assertEqual(self.moteur().import_catalog(), 2)

    def test_les_tickets_d_avant_le_seuil_ne_bloquent_pas(self):
        """Ventes d'avant l'alignement : Shopify les compte déjà, la boucle ne les pousse pas."""
        self.fixer_seuil(self.ticket)
        self.assertEqual(self.moteur().import_catalog(), 2)

    def test_premiere_activation_sans_seuil_ni_alignement_ne_bloque_pas(self):
        """Une commerçante qui vend depuis des mois puis branche Shopify doit pouvoir importer."""
        self.ecrire("DELETE FROM Parametres WHERE cle = ?", (shopify_sync.PARAM_SEUIL_TICKETS,))
        self.assertEqual(self.moteur().import_catalog(), 2)

    def test_alignement_sans_seuil_les_ventes_posterieures_bloquent(self):
        """Seuil pas encore fixé : on applique celui que la première passe fixerait (l'alignement)."""
        self.ecrire("DELETE FROM Parametres WHERE cle = ?", (shopify_sync.PARAM_SEUIL_TICKETS,))
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                    (shopify_sync.PARAM_ALIGNEMENT_TICKETS, str(self.ticket - 1)))
        with self.assertRaises(ImportShopifyBloque):
            self.moteur().import_catalog()

    def test_poussee_eteinte_shopify_fait_autorite(self):
        """Poussée éteinte : ces ventes ne partiront jamais, le refus bloquerait l'import pour toujours."""
        self.regler_shopify("boutique.myshopify.com", "shpat_jeton_secret", auto_sync="0")
        self.assertEqual(self.moteur().import_catalog(), 2)
        self.assertEqual(self.quantites(), {"S": 4, "M": 6})

    def test_forcer_importe_malgre_tout_et_le_dit(self):
        eng = self.moteur()
        self.assertEqual(eng.import_catalog(forcer=True), 2)
        self.assertEqual(self.quantites(), {"S": 4, "M": 6})
        self.assertEqual(eng.dernier_import["ecrasees"], 2, "les ventes effacées doivent être annoncées")
        self.assertEqual(len(self.copies()), 1, "même forcé, le point de retour existe")


class TestLaNotionDEnvoyeEstCelleDeLaBoucle(BaseImport):
    """`ventes_non_envoyees` doit voir exactement ce que `sync_tickets_to_shopify` poussera encore."""

    def setUp(self):
        super().setUp()
        self.fixer_seuil("0")
        self.boutique = FausseBoutique([{"sku": "BI038-S", "barcode": "BI038", "item": 41},
                                        {"sku": "BI038-M", "barcode": "", "item": 42}])
        self.creer_ticket("T-1", [(self.stock["S"], 1), (self.stock["M"], 1)])

    def non_envoyees(self):
        conn = database_manager.get_connection()
        try:
            return shopify_sync.ventes_non_envoyees(conn.cursor())["lignes"]
        finally:
            conn.close()

    def test_apres_une_passe_reussie_plus_rien_n_est_en_attente(self):
        self.assertEqual(self.non_envoyees(), 2)
        self.assertEqual(self.boutique.moteur().sync_tickets_to_shopify(), 1)
        self.assertEqual(self.non_envoyees(), 0)
        self.assertEqual(self.moteur().import_catalog(), 2, "et l'import est alors permis")

    def test_une_passe_en_echec_laisse_les_lignes_en_attente_et_l_import_refuse(self):
        en_panne = MoteurBouchonne({"locations.json": REPONSE_LOCATIONS, "graphql.json": lambda e, d: None,
                                    "products.json": lambda e, d: None},
                                   store_url="boutique.myshopify.com", access_token="jeton")
        self.assertEqual(en_panne.sync_tickets_to_shopify(), 0)
        self.assertEqual(self.non_envoyees(), 2, "rien n'est parti : le stock en ligne est resté plus haut")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sync_queue WHERE statut = 'PENDING'"), [(2,)])
        with self.assertRaises(ImportShopifyBloque):
            self.moteur().import_catalog()
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})

    def test_une_variante_absente_de_shopify_ne_bloque_pas(self):
        """Tracée ABSENT_SHOPIFY puis plus retentée : la boucle ne la poussera jamais, l'import non plus."""
        self.boutique.variantes = [{"sku": "BI038-S", "barcode": "BI038", "item": 41}]   # M inconnue en ligne
        self.boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(self.non_envoyees(), 0, "une variante absente de Shopify est tracée ABSENT, plus retentée")

    def test_seuil_effectif_egal_au_seuil_que_fixerait_la_premiere_passe(self):
        """Une seule règle, deux usages : lecture seule ici, enregistrement dans `fixer_seuil_tickets`."""
        scenarios = []
        # 1. ni alignement ni historique de poussée
        scenarios.append(lambda: None)
        # 2. un alignement enregistré
        scenarios.append(lambda: self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, '1')",
                                             (shopify_sync.PARAM_ALIGNEMENT_TICKETS,)))
        # 3. une poussée déjà faite (installation antérieure) : seuil 0
        scenarios.append(lambda: self.ecrire(
            "INSERT INTO Shopify_Sync_Lignes (id_vente_detail, id_ticket, statut, date_heure) "
            "VALUES (999, 1, 'POUSSE', '2026-01-01 10:00:00')"))
        for preparer in scenarios:
            self.ecrire("DELETE FROM Parametres WHERE cle = ?", (shopify_sync.PARAM_SEUIL_TICKETS,))
            preparer()
            conn = database_manager.get_connection()
            try:
                avant = shopify_sync._seuil_tickets_effectif(conn.cursor())
            finally:
                conn.close()
            self.assertEqual(shopify_sync.fixer_seuil_tickets(), avant)
            self.assertEqual(self.rows("SELECT valeur FROM Parametres WHERE cle = ?",
                                       (shopify_sync.PARAM_SEUIL_TICKETS,)), [(str(avant),)])


# =============================================================================================
# 2. Copie de sécurité avant d'écraser
# =============================================================================================

class TestCopieDeSecurite(BaseImport):

    def test_la_copie_contient_l_etat_d_avant_l_import(self):
        eng = self.moteur()
        eng.import_catalog()
        self.assertEqual(self.quantites(), {"S": 4, "M": 6})
        copies = self.copies()
        self.assertEqual(len(copies), 1)
        self.assertEqual(eng.dernier_import["sauvegarde"], os.path.join(self.dossier_copies, copies[0]))
        copie = sqlite3.connect(os.path.join(self.dossier_copies, copies[0]))
        try:
            avant = dict(copie.execute("SELECT taille, quantite_actuelle FROM Stocks").fetchall())
        finally:
            copie.close()
        self.assertEqual(avant, {"S": 2, "M": 5}, "la copie doit permettre de revenir en arrière")

    def test_seules_les_dix_copies_les_plus_recentes_sont_gardees(self):
        for i in range(12):
            open(os.path.join(self.dossier_copies, f"kodo_pos_pre_import_2026010{i % 10}_00000{i}_000000.db"), "w").close()
        autre = os.path.join(self.dossier_copies, "kodo_pos_pre_migration_20260101_000000_000000.db")
        open(autre, "w").close()
        eng = self.moteur()
        eng.import_catalog()
        copies = self.copies()
        self.assertEqual(len(copies), ShopifySync.SAUVEGARDES_IMPORT_CONSERVEES)
        self.assertTrue(os.path.exists(autre), "les copies de migration ne sont jamais touchées")
        self.assertIn(os.path.basename(eng.dernier_import["sauvegarde"]), copies,
                      "la copie qui vient d'être faite fait partie de celles qui restent")

    def test_si_la_copie_echoue_l_import_est_annule_sans_rien_modifier(self):
        with mock.patch("kodo_core.db.sanctuary_shield.copier_base_sqlite", side_effect=OSError("disque plein")):
            with self.assertRaises(RuntimeError) as ctx:
                self.moteur().import_catalog()
        self.assertIn("Import annulé", str(ctx.exception))
        self.assertIn("disque plein", str(ctx.exception))
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Shopify_Variantes"), [(0,)])

    def test_aucun_import_n_ecrit_de_copie_quand_shopify_ne_repond_pas(self):
        eng = ShopifySync(store_url="boutique.myshopify.com", access_token="jeton")
        eng.make_request = lambda *a, **k: None
        self.assertEqual(eng.import_catalog(), 0)
        self.assertTrue(eng.dernier_import["echec"])
        self.assertEqual(self.copies(), [])


# =============================================================================================
# 3. Une vente ou une poussée PENDANT le téléchargement rend le catalogue périmé
# =============================================================================================

class TestActivitePendantLeTelechargement(BaseImport):

    def test_une_vente_pendant_le_telechargement_annule_l_import(self):
        eng = self.moteur(pendant_telechargement=lambda: self.creer_ticket("T-PENDANT", [(self.stock["S"], 1)]))
        with self.assertRaises(ImportShopifyBloque) as ctx:
            eng.import_catalog()
        self.assertEqual(ctx.exception.raison, "activite_pendant_import")
        self.assertIn("Relancez l'import", str(ctx.exception))
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Shopify_Variantes"), [(0,)], "rien n'a été écrit")

    def test_une_poussee_pendant_le_telechargement_annule_l_import(self):
        """Shopify a été décrémenté entre-temps : le catalogue reçu est plus haut que la réalité."""
        self.fixer_seuil("0")
        ticket = self.creer_ticket("T-ANCIEN", [(self.stock["S"], 1)])
        self.ecrire("UPDATE Tickets SET synced_shopify = 1 WHERE id = ?", (ticket,))   # déjà envoyé : n'empêche pas
        eng = self.moteur(pendant_telechargement=lambda: self.marquer_envoye(ticket))
        with self.assertRaises(ImportShopifyBloque) as ctx:
            eng.import_catalog()
        self.assertEqual(ctx.exception.raison, "activite_pendant_import")
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})

    def test_meme_force_une_vente_pendant_le_telechargement_annule(self):
        eng = self.moteur(pendant_telechargement=lambda: self.creer_ticket("T-PENDANT", [(self.stock["M"], 1)]))
        with self.assertRaises(ImportShopifyBloque):
            eng.import_catalog(forcer=True)
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})

    def test_sans_activite_l_import_passe(self):
        self.assertEqual(self.moteur().import_catalog(), 2)


# =============================================================================================
# 4. Journal des réécritures de stock
# =============================================================================================

class TestJournalDeStock(BaseImport):

    def test_l_import_consigne_chaque_quantite_modifiee_et_seulement_celles_la(self):
        self.moteur(self.catalogue(s=4, m=5)).import_catalog()     # M identique : aucune trace
        self.assertEqual(self.journal(), [("import_shopify", "Robe", "S", 2, 4)])

    def test_une_taille_creee_par_l_import_a_un_avant_vide(self):
        produits = self.catalogue()
        produits[0]["variants"].append({"id": 53, "sku": "BI038-L", "price": "50.00",
                                        "inventory_quantity": 3, "option1": "L"})
        eng = self.moteur(produits)
        eng.import_catalog()
        self.assertIn(("import_shopify", "Robe", "L", None, 3), self.journal())
        self.assertEqual(eng.dernier_import["creees"], 1)

    def test_deux_imports_identiques_ne_journalisent_rien_la_deuxieme_fois(self):
        self.moteur().import_catalog()
        nb = len(self.journal())
        self.moteur().import_catalog()
        self.assertEqual(len(self.journal()), nb)

    def veste(self, sizes, prix=60, ident=None):
        """Crée (ident=None) ou réenregistre depuis l'écran Stocks la veste « VST1 » ; retourne son id."""
        donnees = {"name": "Veste", "barcode": "VST1", "price": prix, "sizes": sizes}
        if ident is not None:
            donnees["id"] = ident
        return InventoryManager.save_product(donnees)["product_id"]

    def test_la_fiche_produit_consigne_la_quantite_saisie(self):
        ident = self.veste("M:3|L:2|XL:1")
        self.assertEqual(sorted(self.rows("SELECT taille, avant, apres, origine FROM Stock_Journal WHERE produit = 'Veste'")),
                         [("L", None, 2, "fiche_produit"), ("M", None, 3, "fiche_produit"), ("XL", None, 1, "fiche_produit")])
        self.veste("M:3|L:1|XL:1", ident=ident)
        self.assertEqual(self.rows("SELECT taille, avant, apres, detail FROM Stock_Journal "
                                   "WHERE produit = 'Veste' AND avant IS NOT NULL"),
                         [("L", 2, 1, "quantité enregistrée depuis la fiche produit")])

    def test_reenregistrer_une_fiche_sans_changement_ne_journalise_rien(self):
        ident = self.veste("M:3|L:2")
        nb = self.rows("SELECT COUNT(*) FROM Stock_Journal")
        self.veste("M:3|L:2", prix=65, ident=ident)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Stock_Journal"), nb)

    def test_retirer_une_taille_est_consigne_supprimee_ou_gardee_a_zero(self):
        ident = self.veste("M:3|L:2|XL:4")
        stock_xl = self.rows("SELECT id FROM Stocks WHERE taille = 'XL'")[0][0]
        self.creer_ticket("T-VESTE", [(stock_xl, 1)])               # XL a déjà été vendue
        self.veste("M:3", ident=ident)
        retraits = dict((t, (a, b, d)) for t, a, b, d in self.rows(
            "SELECT taille, avant, apres, detail FROM Stock_Journal WHERE produit = 'Veste' AND avant IS NOT NULL"))
        self.assertEqual(retraits["XL"][:2], (4, 0), "taille déjà vendue : gardée à 0 pour l'historique")
        self.assertEqual(retraits["L"][:2], (2, None), "taille jamais vendue : ligne supprimée")

    def test_une_saisie_refusee_ne_laisse_aucune_trace(self):
        """Le journal est dans la transaction : un refus annule la trace comme l'écriture."""
        ident = self.veste("M:3|L:2")
        nb = self.rows("SELECT COUNT(*) FROM Stock_Journal")
        with self.assertRaises(ValueError):
            self.veste("M:9|L:-1", ident=ident)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Stock_Journal"), nb)
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks WHERE taille = 'M' "
                                   "AND id_produit = (SELECT id FROM Produits WHERE nom = 'Veste')"), [(3,)])

    def test_une_panne_du_journal_ne_bloque_jamais_l_ecriture_du_stock(self):
        with mock.patch.object(stock_journal, "assurer_table", side_effect=sqlite3.OperationalError("disk I/O error")):
            InventoryManager.save_product({"name": "Veste", "barcode": "VST1", "price": 60, "sizes": "M:3"})
            self.assertEqual(self.moteur().import_catalog(), 2)
        self.assertEqual(self.quantites(), {"S": 4, "M": 6})
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks WHERE taille = 'M' "
                                   "AND id_produit = (SELECT id FROM Produits WHERE nom = 'Veste')"), [(3,)])

    def test_le_journal_n_est_jamais_pris_pour_une_reference_de_ligne_de_stock(self):
        """Les routines de fusion cherchent toute table portant `id_stock` : le journal ne doit pas y répondre."""
        self.moteur().import_catalog()
        self.assertTrue(self.journal(), "précondition : le journal référence bien ces lignes de stock")
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            for ident in self.stock.values():
                self.assertFalse(ShopifySync._ligne_de_stock_referencee(c, ident))
            colonnes = [r[1] for r in c.execute("PRAGMA table_info(Stock_Journal)").fetchall()]
        finally:
            conn.close()
        self.assertNotIn("id_stock", colonnes)
        self.assertNotIn("id_produit", colonnes)

    def test_la_table_n_est_declaree_qu_a_la_premiere_trace(self):
        """Un import écrit des centaines de traces : pas de `CREATE TABLE` à chacune."""
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            with mock.patch.object(stock_journal, "assurer_table", wraps=stock_journal.assurer_table) as espion:
                for apres in (2, 3, 4):
                    self.assertTrue(stock_journal.noter(c, 1, apres - 1, apres, "test"))
            espion.assert_called_once()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM Stock_Journal").fetchone()[0], 3)
            conn.rollback()
        finally:
            conn.close()

    def test_une_autre_erreur_que_table_absente_n_est_pas_prise_pour_elle(self):
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            with mock.patch.object(stock_journal, "_inserer", side_effect=sqlite3.OperationalError("database is locked")), \
                    mock.patch.object(stock_journal, "assurer_table") as espion:
                self.assertFalse(stock_journal.noter(c, 1, 1, 2, "test"))
            espion.assert_not_called()
        finally:
            conn.close()

    def test_noter_n_ecrit_rien_quand_la_quantite_est_identique(self):
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            self.assertFalse(stock_journal.noter(c, 1, 5, 5, "test"))
            self.assertTrue(stock_journal.noter(c, 1, 5, 6, "test"))
            self.assertTrue(stock_journal.noter(c, 1, None, 6, "test"))
            conn.rollback()
        finally:
            conn.close()


# =============================================================================================
# 5. Une synchro qui échoue dit pourquoi
# =============================================================================================

class TestCauseDesEchecs(BaseTemporaire):

    JETON = "shpat_jeton_tres_secret_123"

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", self.JETON)

    def appeler(self, erreur, endpoint="locations.json"):
        """Un appel réel de `make_request` dont le transport lève `erreur`."""
        moteur = ShopifySync()
        original, dormir = urllib.request.urlopen, shopify_sync.time.sleep

        def coupe(req, *a, **k):
            raise erreur(req.full_url) if callable(erreur) else erreur

        urllib.request.urlopen = coupe
        shopify_sync.time.sleep = lambda _s: None
        try:
            resultat = moteur.make_request(endpoint)
        finally:
            urllib.request.urlopen = original
            shopify_sync.time.sleep = dormir
        return moteur, resultat

    @staticmethod
    def http(code, corps, raison="Erreur"):
        return lambda url: urllib.error.HTTPError(url, code, raison, {}, io.BytesIO(corps.encode("utf-8")))

    def test_jeton_invalide_401(self):
        moteur, resultat = self.appeler(self.http(
            401, '{"errors":"[API] Invalid API key or access token (unrecognized login or wrong password)"}'))
        self.assertIsNone(resultat)
        self.assertEqual(moteur.dernier_echec, "http")
        self.assertEqual(moteur.dernier_echec_detail,
                         "401 : [API] Invalid API key or access token (unrecognized login or wrong password)")

    def test_portee_manquante_403(self):
        moteur, _ = self.appeler(self.http(403, '{"errors":"[API] This action requires merchant approval for write_inventory scope."}'))
        self.assertEqual(moteur.dernier_echec_detail,
                         "403 : [API] This action requires merchant approval for write_inventory scope.")

    def test_boutique_introuvable_404_et_corps_vide(self):
        moteur, _ = self.appeler(self.http(404, ""))
        self.assertEqual(moteur.dernier_echec_detail, "404")

    def test_page_html_de_maintenance_5xx(self):
        moteur, _ = self.appeler(self.http(503, "<html><head><title>Service  Unavailable</title></head><body>…</body></html>"))
        self.assertEqual(moteur.dernier_echec_detail, "503 : Service Unavailable")

    def test_erreurs_sous_forme_de_liste_ou_de_dictionnaire(self):
        moteur, _ = self.appeler(self.http(422, '{"errors":{"base":["inventory item not stocked at location"]}}'))
        self.assertIn("422 : base ['inventory item not stocked at location']", moteur.dernier_echec_detail)
        moteur, _ = self.appeler(self.http(400, '{"errors":["a","b"]}'))
        self.assertEqual(moteur.dernier_echec_detail, "400 : a; b")

    def test_plafond_de_debit_429_apres_les_essais(self):
        moteur, _ = self.appeler(lambda url: urllib.error.HTTPError(url, 429, "Too Many", {"Retry-After": "0"}, None))
        self.assertEqual(moteur.dernier_echec, "http")
        self.assertTrue(moteur.dernier_echec_detail.startswith("429 :"), moteur.dernier_echec_detail)

    def test_coupure_reseau(self):
        moteur, _ = self.appeler(urllib.error.URLError("connexion interrompue"))
        self.assertEqual(moteur.dernier_echec, "reseau")
        self.assertIn("URLError", moteur.dernier_echec_detail)
        self.assertIn("connexion interrompue", moteur.dernier_echec_detail)

    def test_le_jeton_n_est_jamais_conserve_meme_si_le_serveur_le_renvoie(self):
        moteur, _ = self.appeler(self.http(401, '{"errors":"token %s rejected"}' % self.JETON))
        self.assertNotIn(self.JETON, moteur.dernier_echec_detail)
        self.assertIn("***", moteur.dernier_echec_detail)

    def test_un_succes_efface_la_cause(self):
        moteur, _ = self.appeler(self.http(500, '{"errors":"boom"}'))
        self.assertTrue(moteur.dernier_echec_detail)

        class Reponse:
            def read(self_inner):
                return b'{"locations": []}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, *a, **k: Reponse()
        try:
            moteur.make_request("locations.json")
        finally:
            urllib.request.urlopen = original
        self.assertIsNone(moteur.dernier_echec)
        self.assertEqual(moteur.dernier_echec_detail, "")

    def test_configuration_manquante(self):
        moteur = ShopifySync(store_url="", access_token="")
        self.ecrire("DELETE FROM Parametres WHERE cle LIKE 'shopify_%'")
        moteur.load_config()
        self.assertIsNone(moteur.make_request("locations.json"))
        self.assertEqual(moteur.dernier_echec, "config")
        self.assertIn("manquant", moteur.dernier_echec_detail)

    # --- jusqu'à l'écran et à la file d'attente --------------------------------------------------

    def test_le_message_de_la_passe_porte_la_cause(self):
        """Ce que `GET /api/shopify/status` rend à l'écran Paramètres."""
        fil = ShopifySyncThread()
        original = urllib.request.urlopen

        def refuse(req, *a, **k):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"errors":"[API] Invalid API key or access token"}'))

        urllib.request.urlopen = refuse
        try:
            self.assertFalse(fil.executer_une_passe())
        finally:
            urllib.request.urlopen = original
        etat = shopify_sync.lire_etat_sync()
        self.assertFalse(etat["succes"])
        self.assertIn("échec de communication (http — 401 : [API] Invalid API key or access token)", etat["message"])
        self.assertIn("boutique.myshopify.com", etat["message"])

    def test_la_file_d_attente_garde_la_cause_d_une_recherche_impossible(self):
        pid = self.creer_produit("BI038", "Robe", [("S", 2)])
        self.creer_ticket("T-1", [(self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0], 1)])

        class EnPanne(ShopifySync):
            def make_request(self, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
                if endpoint.startswith("locations"):          # le dépôt est connu : seule la recherche échoue
                    self._effacer_echec()
                    return REPONSE_LOCATIONS
                self._noter_echec("http", "401 : [API] Invalid API key or access token")
                return None

        EnPanne(store_url="boutique.myshopify.com", access_token="x").sync_tickets_to_shopify()
        self.assertEqual(self.rows("SELECT statut, derniere_erreur FROM sync_queue"),
                         [("PENDING", "recherche Shopify impossible — 401 : [API] Invalid API key or access token")])

    def test_une_erreur_graphql_en_statut_200_est_conservee_avec_celle_du_repli_rest(self):
        pid = self.creer_produit("BI038", "Robe", [("S", 2)])
        self.creer_ticket("T-1", [(self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0], 1)])

        class Refusee(ShopifySync):
            def make_request(self, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
                if endpoint.startswith("locations"):
                    self._effacer_echec()
                    return REPONSE_LOCATIONS
                if endpoint.startswith("graphql"):
                    self._effacer_echec()
                    return {"errors": [{"message": "Access denied for productVariants field.",
                                        "extensions": {"code": "ACCESS_DENIED"}}]}
                self._noter_echec("http", "403 : [API] This action requires read_products scope")
                return None

        Refusee(store_url="boutique.myshopify.com", access_token="x").sync_tickets_to_shopify()
        erreur = self.rows("SELECT derniere_erreur FROM sync_queue")[0][0]
        self.assertIn("GraphQL : ACCESS_DENIED Access denied for productVariants field.", erreur)
        self.assertIn("REST : 403 : [API] This action requires read_products scope", erreur)

    def test_un_ajustement_refuse_garde_la_cause(self):
        pid = self.creer_produit("BI038", "Robe", [("Unique", 2)])
        self.creer_ticket("T-1", [(self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0], 1)])

        def adjust(endpoint, data):
            moteur.dernier_echec = "http"
            return None

        boutique = FausseBoutique([{"sku": "BI038", "barcode": "BI038", "item": 41}])
        moteur = boutique.moteur()
        original = moteur.make_request

        def make_request(endpoint, method="GET", data=None, max_retries=3, rejouable=True):
            if endpoint.startswith("inventory_levels"):
                moteur._noter_echec("http", "422 : inventory item not stocked at location")
                return None
            return original(endpoint, method, data, max_retries, rejouable)

        moteur.make_request = make_request
        moteur.sync_tickets_to_shopify()
        self.assertEqual(self.rows("SELECT derniere_erreur FROM sync_queue"),
                         [("Shopify a refusé l'ajustement (429/5xx/erreur) — 422 : inventory item not stocked at location",)])

    def test_aucune_cause_obsolete_n_est_reutilisee_d_un_appel_a_l_autre(self):
        """La cause d'un échec ancien ne doit pas se coller à un échec sans cause (ex. dépôt absent)."""
        moteur, _ = self.appeler(self.http(500, '{"errors":"ancien incident"}'))
        self.assertTrue(moteur.dernier_echec_detail)
        self.assertFalse(moteur.adjust_shopify_stock(1, "", -1), "sans dépôt, aucun appel n'est même tenté")
        self.assertEqual(moteur.dernier_echec_detail, "")
        self.assertEqual(moteur._suffixe_echec(), "")


class TestBalayageRestUneFoisParPasse(BaseTemporaire):
    """GraphQL indisponible : le repli REST télécharge TOUT le catalogue. Un échec n'est pas relancé par ligne."""

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.stocks = []
        for code in ("CB-A", "CB-B", "CB-C"):
            pid = self.creer_produit(code, f"Article {code}", [("Unique", 5)])
            self.stocks.append((self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0], 1))
        self.creer_ticket("T-1", self.stocks)
        self.appels = {"products": 0, "graphql": 0, "adjust": []}

    def moteur(self, produits):
        appels = self.appels

        def graphql(endpoint, data):
            appels["graphql"] += 1
            return None                                  # GraphQL indisponible

        def liste(endpoint, data):
            appels["products"] += 1
            return produits(endpoint, data)

        return MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": graphql,
            "products.json": liste,
            "inventory_levels/adjust.json": lambda e, d: appels["adjust"].append(d["inventory_item_id"]) or {"ok": 1},
        }, store_url="boutique.myshopify.com", access_token="jeton")

    def test_un_balayage_en_echec_n_est_pas_relance_pour_chaque_ligne(self):
        moteur = self.moteur(lambda e, d: None)
        self.assertEqual(moteur.sync_tickets_to_shopify(), 0)
        self.assertEqual(self.appels["products"], 1, "un seul téléchargement du catalogue pour trois lignes")
        self.assertEqual(self.appels["graphql"], 3, "chaque ligne retente tout de même GraphQL")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sync_queue WHERE statut = 'PENDING'"), [(3,)])
        self.ecrire("UPDATE sync_queue SET prochaine_tentative = '2000-01-01 00:00:00'")  # délai de recul écoulé
        moteur.sync_tickets_to_shopify()
        self.assertEqual(self.appels["products"], 2, "la passe suivante, elle, retente le balayage")

    def test_un_balayage_interrompu_sert_les_variantes_deja_recues(self):
        """Page 1 reçue (250 produits dont CB-A), page 2 en échec : CB-A est exacte, CB-B reste en attente."""
        def produits(endpoint, data):
            if "since_id" in endpoint:
                return None
            page = [{"id": i, "variants": [{"sku": f"FILL-{i}", "inventory_item_id": 9000 + i}]} for i in range(2, 251)]
            return {"products": [{"id": 1, "variants": [{"sku": "CB-A", "inventory_item_id": 7001}]}] + page}

        self.moteur(produits).sync_tickets_to_shopify()
        self.assertEqual(self.appels["adjust"], [7001])
        self.assertEqual(self.appels["products"], 2, "deux pages demandées, une seule fois dans la passe")
        etat = dict(self.rows("SELECT code_barre, statut FROM sync_queue"))
        self.assertEqual(etat, {"CB-A": "DONE", "CB-B": "PENDING", "CB-C": "PENDING"})

    def test_la_cause_du_balayage_est_conservee_pour_les_lignes_suivantes(self):
        class Refusee(ShopifySync):
            nb_balayages = 0

            def make_request(self, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
                if endpoint.startswith("locations"):
                    self._effacer_echec()
                    return REPONSE_LOCATIONS
                if endpoint.startswith("graphql"):
                    self._effacer_echec()
                    return {"errors": [{"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}]}
                Refusee.nb_balayages += 1
                self._noter_echec("http", "403 : [API] This action requires read_products scope")
                return None

        Refusee(store_url="boutique.myshopify.com", access_token="x").sync_tickets_to_shopify()
        self.assertEqual(Refusee.nb_balayages, 1)
        erreurs = [r[0] for r in self.rows("SELECT derniere_erreur FROM sync_queue ORDER BY id")]
        self.assertEqual(len(erreurs), 3)
        for erreur in erreurs:
            self.assertIn("GraphQL : ACCESS_DENIED Access denied", erreur)
            self.assertIn("REST : 403 : [API] This action requires read_products scope", erreur)

    def test_un_balayage_reussi_sert_toutes_les_lignes_une_seule_fois(self):
        """Non-régression du comportement nominal : succès = un seul balayage, aucune ligne en attente."""
        produits = lambda e, d: {"products": [
            {"id": 1, "variants": [{"sku": "CB-A", "inventory_item_id": 71}, {"sku": "CB-B", "inventory_item_id": 72},
                                   {"sku": "CB-C", "inventory_item_id": 73}]}]}
        self.assertEqual(self.moteur(produits).sync_tickets_to_shopify(), 1)
        self.assertEqual(self.appels["products"], 1)
        self.assertEqual(sorted(self.appels["adjust"]), [71, 72, 73])


# =============================================================================================
# 6. La route d'import : ce que la commerçante lit à l'écran
# =============================================================================================

class TestRouteImport(BaseImport):

    def appeler(self, data=None, produits=None, panne=None):
        """POST /api/shopify/import, la boutique Shopify étant bouchonnée au niveau de `make_request`."""
        produits = self.catalogue() if produits is None else produits

        def faux(moteur, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
            if panne:
                moteur._noter_echec("http", panne)
                return None
            return {"products": produits}

        with mock.patch.object(ShopifySync, "make_request", faux):
            statut, corps, _ = kodo_app.handle_request("POST", "/api/shopify/import", {}, {}, data or {})
        json.dumps(corps)        # le serveur local renvoie du JSON : rien de non sérialisable dans la réponse
        return statut, corps

    def test_succes_annonce_les_quantites_corrigees_et_la_copie_de_securite(self):
        statut, corps = self.appeler()
        self.assertEqual(statut, 200, corps)
        self.assertTrue(corps["success"])
        self.assertEqual(corps["imported_count"], 2)
        self.assertEqual(corps["modified_count"], 2)
        self.assertIn("2 quantité(s) de stock ont été corrigées d'après Shopify", corps["message"])
        self.assertIn("copie de sécurité", corps["message"])
        self.assertTrue(corps["backup_file"].startswith("kodo_pos_pre_import_"))
        self.assertEqual(self.quantites(), {"S": 4, "M": 6})

    def test_un_import_sans_changement_ne_parle_pas_de_correction(self):
        statut, corps = self.appeler(produits=self.catalogue(s=2, m=5))
        self.assertEqual(statut, 200)
        self.assertEqual(corps["modified_count"], 0)
        self.assertNotIn("corrigées", corps["message"])

    def test_refus_409_porte_le_texte_sous_message_ET_sous_error(self):
        """L'écran n'affiche que `message` : sans lui, « Erreur lors de l'import » sans explication."""
        self.fixer_seuil("0")
        self.creer_ticket("TCK-2026-0102", [(self.stock["S"], 2)])
        statut, corps = self.appeler()
        self.assertEqual(statut, 409)
        self.assertFalse(corps["success"])
        self.assertTrue(corps["blocked"])
        self.assertEqual(corps["reason"], "ventes_non_envoyees")
        self.assertEqual(corps["message"], corps["error"])
        self.assertIn("TCK-2026-0102", corps["message"])
        self.assertEqual((corps["unsentLines"], corps["unsentTickets"]), (1, 1))
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})

    def test_force_dans_la_requete_passe_outre_le_refus_et_avertit(self):
        self.fixer_seuil("0")
        self.creer_ticket("TCK-2026-0102", [(self.stock["S"], 2)])
        statut, corps = self.appeler({"force": True})
        self.assertEqual(statut, 200, corps)
        self.assertIn("Attention : 1 ligne(s) de vente non envoyées", corps["message"])
        self.assertEqual(self.quantites(), {"S": 4, "M": 6})

    def test_force_doit_etre_le_booleen_true(self):
        """Un `"force": "false"` (chaîne) ou 1 ne doit jamais lever le garde-fou par accident."""
        self.fixer_seuil("0")
        self.creer_ticket("TCK-2026-0102", [(self.stock["S"], 2)])
        for valeur in ("false", "true", 1, "1"):
            statut, _ = self.appeler({"force": valeur})
            self.assertEqual(statut, 409, f"force={valeur!r} ne doit pas forcer")

    def test_refus_du_serveur_n_est_plus_annonce_comme_un_succes(self):
        """Avant : « Importation réussie : 0 variantes » alors que Shopify répondait 401."""
        statut, corps = self.appeler(panne="401 : [API] Invalid API key or access token")
        self.assertEqual(statut, 502)
        self.assertFalse(corps["success"])
        self.assertIn("401 : [API] Invalid API key or access token", corps["message"])
        self.assertEqual(corps["message"], corps["error"])
        self.assertIn("Rien n'a été modifié", corps["message"])
        self.assertEqual(self.quantites(), {"S": 2, "M": 5})

    def test_erreur_interne_porte_aussi_le_texte_sous_message(self):
        with mock.patch("kodo_core.db.sanctuary_shield.copier_base_sqlite", side_effect=OSError("disque plein")):
            statut, corps = self.appeler()
        self.assertEqual(statut, 500)
        self.assertIn("disque plein", corps["message"])
        self.assertEqual(corps["message"], corps["error"])

    def test_la_vente_de_l_ecran_fonctionne_apres_l_import(self):
        """Non-régression de bout en bout : le journal et la copie ne gênent pas la caisse."""
        self.appeler()
        _, produits, _ = kodo_app.handle_request("GET", "/api/products", {}, {}, {})
        robe = next(p for p in produits if p["name"] == "Robe")
        statut, res, _ = kodo_app.handle_request("POST", "/api/sales", {}, {}, {
            "items": [{"product": robe, "quantity": 1, "size": "M", "selectedSize": "M"}],
            "totalTTC": 50.0, "paymentMethod": "CB", "cashierName": "Test", "printReceipt": False})
        self.assertEqual(statut, 200, res)
        self.assertEqual(self.quantites(), {"S": 4, "M": 5})


if __name__ == "__main__":
    unittest.main()
