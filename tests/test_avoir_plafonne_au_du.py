# -*- coding: utf-8 -*-
"""
Kōdo POS — un avoir n'est jamais débité au-delà du reste dû.

Revue du 2026-10-14 (`cart_engine`) : un règlement « Avoir » de 60 € sur un dû de 50 € débitait 60 € de la
carte, enregistrait 60 € d'avoir au journal et ne rendait rien : 10 € de dette envers la cliente disparaissaient
sans écriture. Le débit est désormais plafonné au dû.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db, get_connection  # noqa: E402
from kodo_core.domain.sales.cart_engine import process_sale_transaction  # noqa: E402


class TestAvoirPlafonneAuDu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "avoir.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Robe', 50.00, 0.21)")
            self.pid = c.lastrowid
            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 20)", (self.pid,))
            database_manager.emettre_carte_cadeau(c, Decimal("80.00"), code="AVOIR-TEST-1")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        database_manager.DB_NAME = self._origine
        os.environ.pop("KODO_DB_PATH", None)
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass

    def _vendre(self, paiements):
        return process_sale_transaction(
            cart_items=[{"product_id": self.pid, "nom": "Robe", "quantite": 1, "prix_vente_tvac": 50.0,
                         "taux_tva": 0.21, "taille": "M"}],
            total_tvac=50.0, payments=paiements, client_id=None, cashier_name="Admin",
            caisse_id="POS-01", discount_percent=0, change_given=0, gift_card_code="AVOIR-TEST-1")

    def _solde(self):
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT solde_actuel FROM Cartes_Cadeaux WHERE code = 'AVOIR-TEST-1'")
            return Decimal(str(c.fetchone()[0]))
        finally:
            conn.close()

    def test_avoir_superieur_au_du_ne_debite_que_le_du(self):
        self._vendre([("Avoir", 60.0)])
        self.assertEqual(self._solde(), Decimal("30.00"), "50 € dus : la carte de 80 € ne perd que 50 €")

    def test_avoir_plus_especes_le_surplus_en_especes_est_rendu_pas_l_avoir(self):
        self._vendre([("Avoir", 20.0), ("Espèces", 40.0)])
        self.assertEqual(self._solde(), Decimal("60.00"))

    def test_avoir_exact_inchange(self):
        self._vendre([("Avoir", 50.0)])
        self.assertEqual(self._solde(), Decimal("30.00"))


if __name__ == "__main__":
    unittest.main()
