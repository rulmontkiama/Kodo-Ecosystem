# -*- coding: utf-8 -*-
"""
Kōdo POS — Un remboursement « en avoir » et son bon d'avoir forment UNE seule transaction.

Historique : le remboursement était validé (ticket REF + journal de caisse signé en « Avoir »),
puis le front appelait un SECOND endpoint pour créer le bon. Si ce second appel échouait
(réseau, onglet fermé, erreur serveur), la cliente était remboursée « en avoir » sans bon,
et la ligne n'était plus remboursable (garde-fou anti-doublon).
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestAvoirRetourAtomique(unittest.TestCase):

    def setUp(self):
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()
        self.conn = sqlite3.connect(self.path)
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Robe', 60.50, 0.21)")
        pid = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (pid,))
        self.stock_id = c.lastrowid
        self.num = "TCK-AVOIR-001"
        dm.enregistrer_vente(
            cursor=c, numero_ticket=self.num, total_tvac=60.50, total_htva=50.00, total_tva=10.50,
            remise=0.0, methode_paiement="Espèces", id_client=None, rendu_monnaie=0.0,
            panier=[{"stock_id": self.stock_id, "prix_vente_tvac": 60.50, "quantite": 1}],
            vendeur_nom="Vendeuse", date_heure="2026-10-03 10:00:00",
            paiements=[("Espèces", 60.50)])
        c.execute("SELECT id FROM Ventes_Details WHERE id_ticket = (SELECT id FROM Tickets WHERE numero_ticket = ?)",
                  (self.num,))
        self.vd_id = c.fetchone()[0]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _retour(self, mode, prix=1.0):
        from kodo_core.domain.sales.cart_engine import process_return_transaction
        return process_return_transaction(
            original_ticket_number=self.num, sales_detail_id=self.vd_id, stock_id=self.stock_id,
            refund_price=prix, refund_mode=mode, cashier_name="Vendeuse", quantity=1, conn=self.conn)

    def _compter(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_retour_avoir_cree_le_bon_au_montant_relu_en_base(self):
        # `prix` annoncé par le client (1,00) est ignoré : le bon vaut le prix réellement vendu.
        res = self._retour("Avoir", prix=1.0)
        self.assertTrue(res["success"])
        carte = res["gift_card"]
        self.assertEqual(carte["initialAmount"], 60.50)
        ligne = self.conn.execute(
            "SELECT solde_actuel, status, ticket_origine, ticket_remboursement FROM Cartes_Cadeaux WHERE code = ?",
            (carte["code"],)).fetchone()
        self.assertEqual(ligne, (60.50, "active", self.num, res["refund_ticket_number"]))

    def test_echec_de_l_emission_annule_tout_le_remboursement(self):
        with mock.patch.object(self.dm, "emettre_carte_cadeau", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                self._retour("Avoir")
        self.assertEqual(self._compter("SELECT COUNT(*) FROM Tickets WHERE numero_ticket LIKE 'REF-%'"), 0)
        self.assertEqual(self._compter("SELECT COUNT(*) FROM Ledger_Caisse WHERE type_mouvement = 'REMBOURSEMENT'"), 0)
        self.assertEqual(self._compter("SELECT quantite_actuelle FROM Stocks WHERE id = ?", self.stock_id), 9)  # 10 - 1 vendu, rien recrédité
        self.assertEqual(self._compter("SELECT COUNT(*) FROM Cartes_Cadeaux"), 0)
        # La ligne reste remboursable : la vendeuse peut recommencer.
        self.assertTrue(self._retour("Avoir")["success"])

    def test_retour_en_especes_ne_cree_aucun_bon(self):
        res = self._retour("Espèces")
        self.assertTrue(res["success"])
        self.assertNotIn("gift_card", res)
        self.assertEqual(self._compter("SELECT COUNT(*) FROM Cartes_Cadeaux"), 0)

    def test_le_bon_est_liste_avec_son_ticket_d_origine(self):
        res = self._retour("Avoir")
        cartes = self.dm.lister_cartes_cadeaux(conn=self.conn)
        self.assertEqual(cartes[0]["originTicket"], self.num)
        self.assertEqual(cartes[0]["refundTicket"], res["refund_ticket_number"])


if __name__ == "__main__":
    unittest.main()


class TestDurcissementBons(unittest.TestCase):
    """Codes non devinables, audit non avalé, annulation tracée."""

    def setUp(self):
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()
        self.conn = sqlite3.connect(self.path)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def test_code_genere_non_devinable(self):
        import re
        codes = {self.dm.emettre_carte_cadeau(self.conn.cursor(), 10)["code"] for _ in range(50)}
        self.assertEqual(len(codes), 50)
        for code in codes:
            self.assertRegex(code, r"^AVOIR-[A-HJKMNP-Z2-9]{10}$")

    def test_echec_de_l_audit_annule_l_emission(self):
        with mock.patch("kodo_core.db.audit_trail.record_audit_event", side_effect=RuntimeError("audit KO")):
            with self.assertRaises(RuntimeError):
                self.dm.emettre_carte_cadeau(self.conn.cursor(), 10)

    def test_annulation_trace_solde_et_auteur(self):
        c = self.conn.cursor()
        code = self.dm.emettre_carte_cadeau(c, 25)["code"]
        self.dm.annuler_carte_cadeau(c, code, user_name="Julie")
        self.conn.commit()
        details = c.execute(
            "SELECT user_name, details FROM Audit_Trail WHERE event_type = 'CARTE_CADEAU_ANNULEE'").fetchone()
        self.assertEqual(details[0], "Julie")
        self.assertIn("25.00", details[1])
        self.assertEqual(c.execute("SELECT status, solde_actuel FROM Cartes_Cadeaux WHERE code = ?",
                                   (code,)).fetchone(), ("annulee", 0))


class TestRemboursementAvecRemiseGlobale(unittest.TestCase):
    """Une remise globale de ticket (ex. -10 %) n'est pas répartie dans Ventes_Details, qui garde
    le prix de ligne AVANT remise. Rembourser ce prix rendait plus que ce que la cliente a payé."""

    def setUp(self):
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()
        self.conn = sqlite3.connect(self.path)
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('A', 100, 0.21)")
        pa = c.lastrowid
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('B', 50, 0.21)")
        pb = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (pa,))
        self.sa = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (pb,))
        self.sb = c.lastrowid
        self.num = "TCK-REMISE-001"
        # 150 € bruts, remise globale 10 % -> 135 € payés
        dm.enregistrer_vente(
            cursor=c, numero_ticket=self.num, total_tvac=135.00, total_htva=111.57, total_tva=23.43,
            remise=15.00, methode_paiement="CB", id_client=None, rendu_monnaie=0.0,
            panier=[{"stock_id": self.sa, "prix_vente_tvac": 100, "quantite": 1},
                    {"stock_id": self.sb, "prix_vente_tvac": 50, "quantite": 1}],
            vendeur_nom="V", date_heure="2026-10-03 10:00:00", paiements=[("CB", 135.00)])
        c.execute("SELECT id, id_stock FROM Ventes_Details WHERE id_ticket = "
                  "(SELECT id FROM Tickets WHERE numero_ticket = ?) ORDER BY id", (self.num,))
        self.vd = [r[0] for r in c.fetchall()]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _rembourser(self, i, stock):
        c = self.conn.cursor()
        return self.dm.enregistrer_remboursement(
            cursor=c, ticket_origine=self.num, vd_id=self.vd[i], stock_id=stock, prix=1,
            mode="CB", vendeur_nom="V", date_heure="2026-10-03 11:00:00", quantite=1)

    def test_le_remboursement_suit_le_prix_reellement_paye(self):
        from decimal import Decimal
        _ref, montant = self._rembourser(0, self.sa)
        self.assertEqual(montant, Decimal("-90.00"))  # 100 x 0,90, pas 100

    def test_rembourser_tout_le_ticket_rend_exactement_le_total_paye(self):
        from decimal import Decimal
        _r1, m1 = self._rembourser(0, self.sa)
        _r2, m2 = self._rembourser(1, self.sb)
        self.assertEqual(m1 + m2, Decimal("-135.00"))

    def test_le_lookup_affiche_le_prix_net_de_remise(self):
        res = self.dm.rechercher_ticket_pour_remboursement(self.num, conn=self.conn)
        self.assertEqual([l["unitPrice"] for l in res["lines"]], [90.0, 45.0])
