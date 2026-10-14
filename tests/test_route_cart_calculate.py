# -*- coding: utf-8 -*-
"""
POST /api/cart/calculate importait `compute_cart_totals`, fonction renommée `calculate_cart` en 2.0.5 : la
route répondait 400 « cannot import name » sur tout appel. Trouvé par la porte « importations » de
preflight.py après son durcissement (le `try/except` de la route cachait l'import cassé).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kodo_core.api.routes.pos_routes import handle_pos_request  # noqa: E402


class TestRouteCartCalculate(unittest.TestCase):

    def appeler(self, data):
        return handle_pos_request("POST", "/api/cart/calculate", {}, data)

    def test_panier_simple_avec_ventilation_tva(self):
        code, rep = self.appeler({"items": [
            {"unit_price_ttc": "121.00", "quantity": 1, "vat_rate": "0.21"},
            {"unit_price_ttc": "10.60", "quantity": 2, "vat_rate": "0.06"},
        ]})
        self.assertEqual(code, 200, rep)
        self.assertEqual(rep["total_ttc"], "142.20")
        self.assertEqual(rep["total_discount"], "0.00")
        self.assertEqual(rep["subtotal_ttc"], "142.20")
        taux = {l["rate"]: l for l in rep["vat_breakdown"]}
        self.assertEqual(set(taux), {"0.06", "0.21"})
        self.assertEqual(taux["0.21"]["total_ttc"], "121.00")
        self.assertEqual(taux["0.21"]["base_ht"], "100.00")
        self.assertEqual(taux["0.21"]["vat_amount"], "21.00")

    def test_remise_globale_en_pourcentage(self):
        code, rep = self.appeler({
            "items": [{"unit_price_ttc": "100.00", "quantity": 1, "vat_rate": "0.21"}],
            "global_discount": {"type": "PERCENT", "value": "10"},
        })
        self.assertEqual(code, 200, rep)
        self.assertEqual(rep["total_ttc"], "90.00")
        self.assertEqual(rep["total_discount"], "10.00")
        self.assertEqual(rep["subtotal_ttc"], "100.00")

    def test_panier_invalide_est_une_erreur_400_pas_une_exception(self):
        code, rep = self.appeler({"items": [{"quantity": 1}]})
        self.assertEqual(code, 400)
        self.assertIn("error", rep)


if __name__ == "__main__":
    unittest.main()
