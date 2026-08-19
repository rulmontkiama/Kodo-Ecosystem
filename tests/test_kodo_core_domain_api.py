# -*- coding: utf-8 -*-
"""
Tests unitaires pour la couche Domaine et API REST kodo_core.
"""

import unittest
import os
import sys
import tempfile
import sqlite3

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.domain.sales.cart_engine import (
    CartEngine, CartItem, process_sale_transaction, park_cart, get_parked_carts, restore_parked_cart
)
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.domain.customers.crm import CRMManager
from kodo_core.domain.accounting.z_report import ZReportEngine
from kodo_core.api.app import kodo_app


class TestKodoCoreDomainAndAPI(unittest.TestCase):

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        database_manager.DB_NAME = self.temp_db_path
        database_manager.initialiser_db()

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_cart_engine_calculations(self):
        """Vérifie l'exactitude des calculs financiers du panier."""
        engine = CartEngine()
        item1 = CartItem(name="Robe Lin", unit_price_tvac=100.0, quantity=2, vat_rate=0.21)
        engine.add_item(item1)
        
        totals = engine.calculate_totals()
        self.assertEqual(totals["subtotal_tvac"], 200.0)
        self.assertAlmostEqual(totals["total_htva"], 165.29, places=2)
        self.assertAlmostEqual(totals["total_tva"], 34.71, places=2)

    def test_inventory_manager_crud(self):
        """Vérifie la création, modification et gestion de stock d'un produit."""
        res = InventoryManager.save_product({
            "name": "Jean Slim",
            "category": "Pantalons",
            "price": 89.90,
            "sizes": "38:5|40:10"
        })
        self.assertTrue(res["success"])
        prod_id = int(res["product_id"])

        prod = InventoryManager.get_product_by_id(prod_id)
        self.assertIsNotNone(prod)
        self.assertEqual(prod["name"], "Jean Slim")
        self.assertEqual(prod["stock"], 15)

    def test_crm_manager_customer(self):
        """Vérifie la création et gestion client et des points de fidélité."""
        res = CRMManager.save_customer({
            "name": "Sophie Martin",
            "email": "sophie@example.com",
            "points": 50
        })
        self.assertTrue(res["success"])
        cid = int(res["client_id"])

        client = CRMManager.get_customer_by_id(cid)
        self.assertEqual(client["name"], "Sophie Martin")
        self.assertEqual(client["points"], 50)

        # Échange de points
        redeem = CRMManager.redeem_points_for_discount(cid, 20)
        self.assertTrue(redeem["success"])
        self.assertEqual(redeem["remaining_points"], 30)

    def test_z_report_generation(self):
        """Vérifie la clôture Z de caisse et la ventilation TVA."""
        summary = ZReportEngine.get_daily_z_summary()
        self.assertIn("total_tvac", summary)
        self.assertIn("vat_breakdown", summary)

    def test_rest_api_routes(self):
        """Vérifie le routage et le traitement des requêtes REST API."""
        # Status GET
        status, data, _ = kodo_app.handle_request("GET", "/api/status", {}, {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "online")

        # Products GET
        status, prods, _ = kodo_app.handle_request("GET", "/api/products", {}, {}, {})
        self.assertEqual(status, 200)
        self.assertIsInstance(prods, list)


if __name__ == "__main__":
    unittest.main()
