# -*- coding: utf-8 -*-
"""
Kōdo POS — Une commande en ligne n'est PAS une vente de caisse (décision du 2026-10-15).

La comptabilité du web se tient dans Shopify, celle du magasin dans Kōdo. Jusqu'en 2.1.2, chaque
commande importée devenait un ticket « SHPF-… » signé, une écriture « VENTE / Shopify » au journal
de caisse, et tombait au Z dans la colonne « carte » : le total carte du Z ne correspondait plus au
terminal Bancontact, et le CA du magasin contenait des ventes qui n'ont jamais passé en caisse.

Désormais l'import ne fait que DÉCOMPTER LE STOCK (même garde-fou, même trace des surventes) et se
souvenir de la commande (`Shopify_Commandes`) pour ne pas la décompter deux fois. Un remboursement
en ligne ne fait que rendre le stock, jamais plus que ce qui a été décompté.

Le réglage `shopify_commandes_en_caisse` = 1 rétablit l'ancien comportement.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.sync import shopify as shopify_sync
from test_shopify_sync import BaseTemporaire, MoteurBouchonne


class BaseHorsCaisse(BaseTemporaire):
    SKU = "CB-ROBE"
    VARIANT_ID = 555

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.pid = self.creer_produit(self.SKU, "Robe", [("S", 4), ("M", 6), ("L", 2)])
        self.stocks = dict((t, i) for i, t in self.rows(
            "SELECT id, taille FROM Stocks WHERE id_produit = ?", (self.pid,)))

    def commande(self, quantite=2, **surcharges):
        base = {
            "id": 9001, "order_number": 1042,
            "total_price": "60.00", "total_tax": "10.41", "taxes_included": True,
            "updated_at": "2026-02-01T10:00:00+01:00",
            "line_items": [{
                "id": 4001, "sku": self.SKU, "title": "Robe", "quantity": quantite,
                "variant_id": self.VARIANT_ID, "variant_title": "M", "price": "30.00",
            }],
        }
        base.update(surcharges)
        return base

    def remboursement(self, quantite=1, restock_type="return", refund_id=7001):
        return {"id": refund_id, "created_at": "2026-02-02T09:00:00+01:00",
                "refund_line_items": [{
                    "id": 8001, "quantity": quantite, "restock_type": restock_type,
                    "line_item": {"id": 4001, "sku": self.SKU, "title": "Robe",
                                  "variant_id": self.VARIANT_ID, "variant_title": "M"},
                }]}

    def moteur(self, a_importer=None, apres=None):
        def routeur(endpoint, data):
            if "updated_at_min" in endpoint:
                return {"orders": list(apres or [])}
            return {"orders": list(a_importer or [])}

        return MoteurBouchonne({"orders.json": routeur},
                               store_url="boutique.myshopify.com", access_token="jeton")

    def stock_m(self):
        return self.rows("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (self.stocks["M"],))[0][0]

    def compter(self, table):
        return self.rows(f"SELECT COUNT(*) FROM {table}")[0][0]

    def regler_en_caisse(self, valeur="1"):
        self.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                    (shopify_sync.CLE_COMMANDES_EN_CAISSE, valeur))


class TestImportHorsCaisse(BaseHorsCaisse):

    def test_une_commande_decompte_le_stock_sans_aucune_ecriture_de_caisse(self):
        self.assertEqual(self.moteur([self.commande()]).sync_orders_from_shopify(), 1)
        self.assertEqual(self.stock_m(), 4, "6 - 2")
        for table in ("Tickets", "Ventes_Details", "Ledger_Caisse"):
            self.assertEqual(self.compter(table), 0, f"{table} ne doit rien recevoir d'une commande en ligne")

    def test_la_commande_est_memorisee_avec_ses_lignes(self):
        self.moteur([self.commande()]).sync_orders_from_shopify()
        self.assertEqual(self.rows("SELECT order_id, order_number, nb_lignes FROM Shopify_Commandes"),
                         [("9001", "1042", 1)])
        self.assertEqual(self.rows("SELECT stock_id, quantite, restituee FROM Shopify_Commandes_Lignes"),
                         [(self.stocks["M"], 2, 0)])

    def test_une_commande_n_est_jamais_decomptee_deux_fois(self):
        for _ in range(3):
            self.moteur([self.commande()]).sync_orders_from_shopify()
        self.assertEqual(self.stock_m(), 4)
        self.assertEqual(self.compter("Shopify_Commandes"), 1)

    def test_une_commande_deja_importee_comme_ticket_par_une_version_precedente_est_ignoree(self):
        """Le cas décisif de la mise à jour : les anciennes commandes ne doivent pas être décomptées de nouveau."""
        self.ecrire("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, methode_paiement, shopify_order_id, synced_shopify) "
                    "VALUES ('SHPF-1042', '2026-01-01 10:00:00', '60.00', 'Shopify', '9001', 1)")
        self.assertEqual(self.moteur([self.commande()]).sync_orders_from_shopify(), 0)
        self.assertEqual(self.stock_m(), 6, "le stock de la commande historique ne doit pas rebouger")
        self.assertEqual(self.compter("Shopify_Commandes"), 0)

    def test_la_commande_en_ligne_n_apparait_pas_au_z(self):
        self.moteur([self.commande()]).sync_orders_from_shopify()
        bilan = database_manager.generer_bilan_z_journalier("POS-01")
        self.assertEqual(bilan["nb_tickets"], 0)
        self.assertEqual(bilan["total_tvac"], Decimal("0.00"))
        self.assertEqual(bilan["total_carte"], Decimal("0.00"))
        self.assertEqual(database_manager.lister_jours_non_clotures("POS-01"), [])

    def test_une_survente_est_toujours_tracee_et_le_stock_ne_passe_pas_sous_zero(self):
        cmd = self.commande(quantite=5, line_items=[{"sku": self.SKU, "title": "Robe", "quantity": 5,
                                                     "variant_title": "L", "price": "30.00"}])
        self.moteur([cmd]).sync_orders_from_shopify()
        self.assertEqual(self.rows("SELECT quantite_actuelle, requires_stock_audit FROM Stocks WHERE id = ?",
                                   (self.stocks["L"],)), [(0, 1)])

    def test_une_commande_annulee_n_est_pas_importee(self):
        self.assertEqual(
            self.moteur([self.commande(cancelled_at="2026-01-02T10:00:00Z")]).sync_orders_from_shopify(), 0)
        self.assertEqual(self.compter("Shopify_Commandes"), 0)
        self.assertEqual(self.stock_m(), 6)

    def test_le_reglage_de_repli_retablit_les_tickets_de_caisse(self):
        self.regler_en_caisse("1")
        self.assertEqual(self.moteur([self.commande()]).sync_orders_from_shopify(), 1)
        self.assertEqual(self.compter("Tickets"), 1)
        self.assertEqual(self.compter("Ledger_Caisse"), 1)
        self.assertEqual(self.compter("Shopify_Commandes"), 0)
        self.assertEqual(self.stock_m(), 4)


class TestRemboursementHorsCaisse(BaseHorsCaisse):

    def importer_puis_rembourser(self, remboursements, commande=None, passes=1, **maj):
        commande = commande or self.commande()
        self.assertEqual(self.moteur(a_importer=[commande]).sync_orders_from_shopify(), 1)
        apres = dict(commande, refunds=remboursements, updated_at="2026-02-02T09:00:00+01:00", **maj)
        moteur = self.moteur(apres=[apres])
        return [moteur.sync_refunds_from_shopify() for _ in range(passes)]

    def test_un_remboursement_rend_le_stock_sans_ticket_ni_ecriture_de_caisse(self):
        self.assertEqual(self.importer_puis_rembourser([self.remboursement()]), [1])
        self.assertEqual(self.stock_m(), 5, "6 - 2 + 1")
        for table in ("Tickets", "Ventes_Details", "Ledger_Caisse"):
            self.assertEqual(self.compter(table), 0)
        self.assertEqual(self.rows("SELECT quantite, restituee FROM Shopify_Commandes_Lignes"), [(2, 1)])

    def test_le_meme_remboursement_n_est_jamais_applique_deux_fois(self):
        self.assertEqual(self.importer_puis_rembourser([self.remboursement()], passes=3), [1, 0, 0])
        self.assertEqual(self.stock_m(), 5)
        self.assertEqual(self.compter("Shopify_Remboursements"), 1)

    def test_no_restock_ne_remet_pas_l_article_en_rayon(self):
        self.importer_puis_rembourser([self.remboursement(restock_type="no_restock")])
        self.assertEqual(self.stock_m(), 4, "l'article n'est pas revenu au stock vendable")
        self.assertEqual(self.rows("SELECT restituee FROM Shopify_Commandes_Lignes"), [(1,)],
                         "l'unité est tout de même comptée comme traitée : pas de second passage")

    def test_on_ne_rend_jamais_plus_que_ce_qui_a_ete_decompte(self):
        self.importer_puis_rembourser([self.remboursement(quantite=5)])
        self.assertEqual(self.stock_m(), 6, "2 décomptées, 2 rendues, pas 5")
        self.assertEqual(self.rows("SELECT requires_stock_audit FROM Stocks WHERE id = ?", (self.stocks["M"],)),
                         [(1,)], "l'écart est signalé, pas masqué")

    def test_deux_remboursements_successifs_cumulent_sans_depasser(self):
        commande = self.commande()
        self.assertEqual(self.moteur(a_importer=[commande]).sync_orders_from_shopify(), 1)
        apres = dict(commande, refunds=[self.remboursement(1, refund_id=1), self.remboursement(5, refund_id=2)],
                     updated_at="2026-02-02T09:00:00+01:00")
        self.assertEqual(self.moteur(apres=[apres]).sync_refunds_from_shopify(), 2)
        self.assertEqual(self.stock_m(), 6, "6 - 2 + 1 + 1 (le second est borné au reste)")

    def test_une_annulation_sans_remboursement_n_invente_rien(self):
        self.importer_puis_rembourser([], cancelled_at="2026-02-02T09:00:00+01:00")
        self.assertEqual(self.stock_m(), 4, "pas de remise en stock d'office")
        self.assertEqual(self.rows("SELECT requires_stock_audit FROM Stocks WHERE id = ?", (self.stocks["M"],)),
                         [(1,)])
        for table in ("Tickets", "Ledger_Caisse"):
            self.assertEqual(self.compter(table), 0)

    def test_un_remboursement_sur_une_commande_historique_suit_toujours_le_chemin_ticket(self):
        """Une commande importée comme ticket AVANT la mise à jour garde son circuit complet."""
        self.regler_en_caisse("1")
        commande = self.commande()
        self.assertEqual(self.moteur(a_importer=[commande]).sync_orders_from_shopify(), 1)
        self.regler_en_caisse("0")        # bascule vers le nouveau mode : le ticket existe déjà
        apres = dict(commande, refunds=[self.remboursement()], updated_at="2026-02-02T09:00:00+01:00")
        self.assertEqual(self.moteur(apres=[apres]).sync_refunds_from_shopify(), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Tickets WHERE total_tvac < 0"), [(1,)])
        self.assertEqual(self.stock_m(), 5)


if __name__ == "__main__":
    unittest.main()
