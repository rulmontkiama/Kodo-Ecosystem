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
        # kodo_core.db.connection résout son propre chemin via ShopConfig.get_db_path(),
        # indépendamment de database_manager.DB_NAME : sans cet override, les modules
        # domain/* écriraient dans la vraie base persistante au lieu du fichier temporaire.
        os.environ["KODO_DB_PATH"] = self.temp_db_path
        database_manager.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
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

    def test_product_without_custom_threshold_follows_global_default(self):
        """Un article sans seuil personnalisé doit suivre le seuil global, y compris après modification de celui-ci."""
        res = InventoryManager.save_product({
            "name": "T-Shirt Basique",
            "category": "Hauts",
            "price": 19.90,
            "sizes": "M:3"
        })
        prod_id = int(res["product_id"])

        prod = InventoryManager.get_product_by_id(prod_id)
        self.assertFalse(prod["has_custom_alert_threshold"])
        self.assertEqual(prod["alertStock"], 5)  # seuil global par défaut

        InventoryManager.set_default_alert_threshold(2)
        prod_after = InventoryManager.get_product_by_id(prod_id)
        self.assertEqual(prod_after["alertStock"], 2)

    def test_product_with_custom_threshold_ignores_global_default(self):
        """Un seuil personnalisé sur un article ne doit jamais être écrasé par le seuil global."""
        InventoryManager.set_default_alert_threshold(5)
        res = InventoryManager.save_product({
            "name": "Veste Cuir",
            "category": "Manteaux",
            "price": 199.0,
            "sizes": "L:1",
            "alertStock": 1
        })
        prod_id = int(res["product_id"])

        prod = InventoryManager.get_product_by_id(prod_id)
        self.assertTrue(prod["has_custom_alert_threshold"])
        self.assertEqual(prod["alertStock"], 1)

        InventoryManager.set_default_alert_threshold(10)
        prod_after = InventoryManager.get_product_by_id(prod_id)
        self.assertEqual(prod_after["alertStock"], 1)

    def test_low_stock_alerts_use_effective_threshold(self):
        """get_low_stock_alerts doit détecter les ruptures en tenant compte du seuil global."""
        InventoryManager.set_default_alert_threshold(3)
        InventoryManager.save_product({
            "name": "Casquette",
            "category": "Accessoires",
            "price": 15.0,
            "sizes": "Unique:2"
        })

        alerts = InventoryManager.get_low_stock_alerts()
        self.assertTrue(any(a["product_name"] == "Casquette" for a in alerts))

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

        # Settings Social POST (avec /api)
        status, res_post, _ = kodo_app.handle_request(
            "POST", "/api/settings/social", {}, {},
            {"mode": "qr", "title": "Instagram", "url": "https://instagram.com/test", "qr_size": "large"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(res_post.get("success"))

        # Settings Social GET (sans /api)
        status, res_get, _ = kodo_app.handle_request("GET", "/settings/social", {}, {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(res_get.get("has_social"))
        self.assertEqual(res_get.get("title"), "Instagram")

        # Settings Social POST (sans /api)
        status, res_post2, _ = kodo_app.handle_request(
            "POST", "/settings/social", {}, {},
            {"mode": "none"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(res_post2.get("success"))
        self.assertEqual(res_post2.get("mode"), "none")

        # Settings Social DELETE (avec /api)
        status, res_del, _ = kodo_app.handle_request("DELETE", "/api/settings/social", {}, {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(res_del.get("success"))

    def test_api_default_alert_threshold_and_bulk_update(self):
        """Vérifie la persistance du seuil global via /api/settings et la modification en masse via /api/products/bulk-alert."""
        # 1. Vérifier GET /api/settings retourne defaultAlertThreshold
        status, settings, _ = kodo_app.handle_request("GET", "/api/settings", {}, {}, {})
        self.assertEqual(status, 200)
        self.assertIn("defaultAlertThreshold", settings)
        self.assertEqual(settings["defaultAlertThreshold"], 5)

        # 2. Mettre à jour le seuil global via POST /api/settings
        status, res_set, _ = kodo_app.handle_request("POST", "/api/settings", {}, {}, {"defaultAlertThreshold": 8})
        self.assertEqual(status, 200)
        self.assertTrue(res_set.get("success"))

        # Vérifier que le GET renvoie bien 8
        status, settings2, _ = kodo_app.handle_request("GET", "/api/settings", {}, {}, {})
        self.assertEqual(settings2["defaultAlertThreshold"], 8)

        # 3. Créer 2 produits
        p1 = InventoryManager.save_product({"name": "Produit A", "category": "Test", "price": 10.0, "stock": 5})
        p2 = InventoryManager.save_product({"name": "Produit B", "category": "Test", "price": 20.0, "stock": 10})
        pid1 = p1["product_id"]
        pid2 = p2["product_id"]

        # Les 2 produits doivent hériter du seuil global 8
        prod_a = InventoryManager.get_product_by_id(int(pid1))
        self.assertEqual(prod_a["alertStock"], 8)
        self.assertFalse(prod_a["has_custom_alert_threshold"])

        # 4. Appliquer un seuil personnalisé en masse via /api/products/bulk-alert
        status, res_bulk, _ = kodo_app.handle_request(
            "POST", "/api/products/bulk-alert", {}, {},
            {"product_ids": [pid1, pid2], "alertStock": 3}
        )
        self.assertEqual(status, 200)
        self.assertEqual(res_bulk.get("updated"), 2)

        # Les 2 produits ont maintenant un seuil custom de 3
        prod_a2 = InventoryManager.get_product_by_id(int(pid1))
        self.assertEqual(prod_a2["alertStock"], 3)
        self.assertTrue(prod_a2["has_custom_alert_threshold"])

        # 5. Réinitialiser au seuil global via /api/products/bulk-alert avec None/vide
        status, res_reset, _ = kodo_app.handle_request(
            "POST", "/api/products/bulk-alert", {}, {},
            {"product_ids": [pid1], "alertStock": None}
        )
        self.assertEqual(status, 200)
        prod_a3 = InventoryManager.get_product_by_id(int(pid1))
        self.assertEqual(prod_a3["alertStock"], 8)  # De retour au seuil global
        self.assertFalse(prod_a3["has_custom_alert_threshold"])


if __name__ == "__main__":
    unittest.main()
