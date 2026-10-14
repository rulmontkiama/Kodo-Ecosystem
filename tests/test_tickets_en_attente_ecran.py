# -*- coding: utf-8 -*-
"""
Kōdo POS — la liste des tickets en attente a la forme que l'écran lit, et la taille survit.

Revue du 2026-10-14 : GET /api/held-tickets renvoyait `panier` / `total_tvac` alors que l'écran lit `items`,
`ticketNumber`, `totalTTC` : `ticket.items.map` plantait à l'ouverture de la liste après un redémarrage ou une
restauration de sauvegarde. La taille, envoyée sous `selectedSize`, était lue sous `size` et perdue.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db  # noqa: E402
from kodo_core.api.routes.pos_routes import handle_pos_request  # noqa: E402


class TestTicketsEnAttenteEcran(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "attente.db")
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

    def _mettre_en_attente(self):
        corps = {
            "items": [
                {"product": {"id": "12", "name": "Robe", "price": 49.9, "barcode": "123"}, "quantity": 2,
                 "selectedSize": "M"},
                {"product": {"id": "7", "name": "Écharpe", "price": 15.0}, "quantity": 1},
            ],
            "totalTTC": 114.8, "discountPercent": 0, "cashierName": "Léa",
            "client": {"id": "3", "name": "Mme Dupont"},
        }
        code, rep = handle_pos_request("POST", "/api/held-tickets", {}, corps)
        self.assertEqual(code, 200, rep)
        return rep["ticketId"]

    def test_la_liste_a_la_forme_de_l_ecran(self):
        tid = self._mettre_en_attente()
        code, liste = handle_pos_request("GET", "/api/held-tickets", {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(len(liste), 1)
        t = liste[0]
        self.assertEqual(t["id"], str(tid))
        self.assertEqual(t["ticketNumber"], f"ATT-{tid}")
        self.assertAlmostEqual(t["totalTTC"], 114.8)
        self.assertEqual(t["cashierName"], "Léa")
        self.assertEqual(t["client"]["name"], "Mme Dupont")
        self.assertEqual(sorted((i["product"]["name"], i["quantity"]) for i in t["items"]),
                         [("Robe", 2), ("Écharpe", 1)])
        # clés brutes conservées pour les autres lecteurs
        self.assertIn("panier", t)

    def test_la_taille_et_l_identifiant_produit_survivent(self):
        self._mettre_en_attente()
        _, liste = handle_pos_request("GET", "/api/held-tickets", {}, {})
        robe = next(i for i in liste[0]["items"] if i["product"]["name"] == "Robe")
        self.assertEqual(robe["selectedSize"], "M")
        self.assertEqual(robe["product"]["id"], "12")
        self.assertEqual(robe["product"]["price"], 49.9)

    def test_suppression_par_identifiant_texte(self):
        tid = self._mettre_en_attente()
        code, _ = handle_pos_request("DELETE", "/api/held-tickets", {"id": [str(tid)]}, {})
        self.assertEqual(code, 200)
        _, liste = handle_pos_request("GET", "/api/held-tickets", {}, {})
        self.assertEqual(liste, [])


if __name__ == "__main__":
    unittest.main()
