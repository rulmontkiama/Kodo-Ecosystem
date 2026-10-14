# -*- coding: utf-8 -*-
"""
Kōdo POS — enregistrer une fiche produit SANS quantités ne détruit pas le stock par taille, et un refus métier
n'est plus une erreur 500 muette.

Revue du 2026-10-14 (`inventory_manager.py:905-925`, `products_routes.py:419-428`) : une charge sans `sizes` ni
`stock` sur un article existant était lue comme « une taille unique à 0 » : les tailles S/M/L étaient retirées ou
mises à zéro, et la mise à zéro était propagée à la boutique en ligne. Le refus d'un code-barres déjà pris
remontait en HTTP 500 sans message : l'écran rechargeait le catalogue et la modification disparaissait.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db, get_connection  # noqa: E402
from kodo_core.api.routes.products_routes import handle_products_request  # noqa: E402


class TestFicheProduitSansQuantites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "fiche.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()

    def tearDown(self):
        database_manager.DB_NAME = self._origine
        os.environ.pop("KODO_DB_PATH", None)
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass

    def _post(self, corps):
        return handle_products_request("POST", "/api/products", {}, corps)

    def _stocks(self, pid):
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ? ORDER BY taille", (pid,))
            return c.fetchall()
        finally:
            conn.close()

    def _creer_robe(self):
        code, rep = self._post({"name": "Robe", "price": 50, "sizes": "S:4 | M:6 | L:2", "barcode": "1111"})
        self.assertEqual(code, 200, rep)
        return int(rep["productId"])

    def test_changement_de_prix_sans_quantites_garde_les_tailles(self):
        pid = self._creer_robe()
        code, rep = self._post({"id": pid, "name": "Robe", "price": 55})
        self.assertEqual(code, 200, rep)
        self.assertEqual([tuple(r) for r in self._stocks(pid)], [("L", 2), ("M", 6), ("S", 4)])

    def test_quantites_fournies_sont_toujours_appliquees(self):
        pid = self._creer_robe()
        self._post({"id": pid, "name": "Robe", "price": 50, "sizes": "S:1 | M:1"})
        self.assertEqual([tuple(r) for r in self._stocks(pid)], [("M", 1), ("S", 1)])

    def test_code_barres_deja_pris_est_un_409_avec_message(self):
        self._creer_robe()
        code, rep = self._post({"name": "Jupe", "price": 30, "barcode": "1111", "sizes": "U:1"})
        self.assertEqual(code, 409)
        self.assertFalse(rep["success"])
        self.assertIn("déjà utilisé", rep["error"])

    def test_quantite_negative_est_un_400_avec_message(self):
        code, rep = self._post({"name": "Jupe", "price": 30, "sizes": "S:-3"})
        self.assertEqual(code, 400)
        self.assertIn("négative", rep["error"])


if __name__ == "__main__":
    unittest.main()


class TestTauxTvaZero(unittest.TestCase):
    """Un taux de 0 % (livres, exonérés) est un vrai taux : il ne devient pas 21 %."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "tva.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()

    def tearDown(self):
        database_manager.DB_NAME = self._origine
        os.environ.pop("KODO_DB_PATH", None)

    def _taux(self, pid):
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT taux_tva FROM Produits WHERE id = ?", (pid,))
            return float(c.fetchone()[0])
        finally:
            conn.close()

    def test_taux_zero_conserve(self):
        code, rep = handle_products_request("POST", "/api/products", {},
                                            {"name": "Livre", "price": 20, "vat_rate": 0, "sizes": "U:3"})
        self.assertEqual(code, 200, rep)
        self.assertEqual(self._taux(int(rep["productId"])), 0.0)

    def test_taux_absent_reste_21(self):
        code, rep = handle_products_request("POST", "/api/products", {}, {"name": "Robe", "price": 20, "sizes": "U:3"})
        self.assertEqual(self._taux(int(rep["productId"])), 0.21)

    def test_taux_6_conserve_a_la_reedition(self):
        code, rep = handle_products_request("POST", "/api/products", {},
                                            {"name": "Plante", "price": 20, "vat_rate": 0.06, "sizes": "U:3"})
        pid = int(rep["productId"])
        handle_products_request("POST", "/api/products", {}, {"id": pid, "name": "Plante", "price": 22,
                                                              "vat_rate": 0.06, "sizes": "U:3"})
        self.assertEqual(self._taux(pid), 0.06)
