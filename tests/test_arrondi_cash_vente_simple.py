# -*- coding: utf-8 -*-
"""
Kōdo POS — l'arrondi légal belge à 5 centimes est ENREGISTRÉ pour une vente réglée en espèces seules.

Revue du 2026-10-14 : l'écran affiche le montant arrondi au 5 c (10,03 € → 10,05 €) mais n'envoie, pour un
règlement unique en espèces, que le total brut. Sans liste de règlements, le serveur retenait ce brut comme
« espèces remises » : le tiroir reçoit 10,05 € et le journal scellé n'en porte que 10,03 € (écart de 2 c par
vente, jamais tracé). L'arrondi doit être appliqué par le serveur au règlement par défaut.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db, get_connection  # noqa: E402
from kodo_core.api.routes.pos_routes import handle_pos_request  # noqa: E402


class TestArrondiCashVenteSimple(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "arrondi.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()
        conn = get_connection()
        try:
            c = conn.cursor()
            self.pids = {}
            for nom, prix in (("Haut", "10.03"), ("Bas", "10.02"), ("Rond", "10.05")):
                c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES (?, ?, 0.21)", (nom, prix))
                pid = c.lastrowid
                c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'U', 20)", (pid,))
                self.pids[nom] = (pid, float(prix))
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

    def _vendre_especes(self, nom):
        pid, prix = self.pids[nom]
        corps = {"items": [{"product": {"id": pid, "name": nom, "price": prix, "vat_rate": 0.21},
                            "quantity": 1, "selectedSize": "U", "size": "U"}],
                 "totalTTC": prix, "paymentMethod": "Espèces", "cashierName": "Admin", "printReceipt": False}
        code, rep = handle_pos_request("POST", "/api/sales", {}, corps)
        self.assertEqual(code, 200, rep)
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT total_tvac, COALESCE(ecart_arrondi_cash, 0) FROM Tickets WHERE numero_ticket = ?",
                      (rep["receiptNumber"] if "receiptNumber" in rep else rep["ticket"]["numero_ticket"],))
            total, ecart = c.fetchone()
            c.execute("SELECT montant FROM Ledger_Caisse WHERE type_mouvement = 'VENTE' ORDER BY id DESC LIMIT 1")
            ledger = c.fetchone()[0]
        finally:
            conn.close()
        return Decimal(str(total)), Decimal(str(ecart)), Decimal(str(ledger))

    def test_arrondi_vers_le_haut_est_enregistre_et_le_tiroir_concorde(self):
        total, ecart, ledger = self._vendre_especes("Haut")
        self.assertEqual(total, Decimal("10.03"), "le ticket scellé garde le TTC exact")
        self.assertEqual(ecart, Decimal("0.02"))
        self.assertEqual(ledger, Decimal("10.05"), "le journal doit porter ce que le tiroir a reçu")

    def test_arrondi_vers_le_bas_est_enregistre(self):
        total, ecart, ledger = self._vendre_especes("Bas")
        self.assertEqual(total, Decimal("10.02"))
        self.assertEqual(ecart, Decimal("-0.02"))
        self.assertEqual(ledger, Decimal("10.00"))

    def test_montant_deja_rond_inchange(self):
        total, ecart, ledger = self._vendre_especes("Rond")
        self.assertEqual((total, ecart, ledger), (Decimal("10.05"), Decimal("0.00"), Decimal("10.05")))


if __name__ == "__main__":
    unittest.main()
