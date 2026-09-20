# -*- coding: utf-8 -*-
"""Import du catalogue Shopify : mise à jour sur place (ids et réglages conservés) et pagination.

Avant, `INSERT OR REPLACE INTO Produits` recréait chaque produit sous un nouvel id à chaque import : stocks
orphelins, TVA/marque/seuil remis à zéro, et écran de caisse pointant sur des ids disparus. Seuls les 250
premiers produits étaient importés. Aucun appel réseau (make_request simulé), base temporaire.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.sync.shopify import ShopifySync


def fake_catalogue(n, quantite=7):
    return [{"id": i, "title": f"Produit {i}", "product_type": "Maison",
             "variants": [{"id": 1000 + i, "sku": f"SKU-{i}", "price": "10.00",
                           "inventory_quantity": quantite, "option1": "Default Title"}]}
            for i in range(1, n + 1)]


class TestImportShopify(unittest.TestCase):
    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def moteur(self, produits):
        """ShopifySync dont make_request sert `produits` page par page selon since_id/limit."""
        eng = ShopifySync(store_url="demo.myshopify.com", access_token="x")
        eng.requetes = []

        def fake(endpoint, method="GET", data=None, max_retries=3):
            eng.requetes.append(endpoint)
            since = 0
            limit = 250
            if "?" in endpoint:
                for kv in endpoint.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    if k == "since_id":
                        since = int(v)
                    elif k == "limit":
                        limit = int(v)
            page = [p for p in produits if p["id"] > since][:limit]
            return {"products": page}

        eng.make_request = fake
        return eng

    def rows(self, sql, params=()):
        conn = database_manager.get_connection()
        try:
            return [tuple(r) for r in conn.cursor().execute(sql, params).fetchall()]
        finally:
            conn.close()

    def test_reimport_conserve_id_tva_marque_seuil_et_ligne_de_stock(self):
        InventoryManager.save_product({"name": "Spray Interieur", "barcode": "SKU-1", "price": 12, "vat_rate": 0.06,
                                       "brand": "MaisonX", "stock": 10, "alert_threshold": 3})
        avant = self.rows("SELECT id, taux_tva, marque, seuil_alerte FROM Produits")
        stock_avant = self.rows("SELECT id, taille FROM Stocks")

        eng = self.moteur(fake_catalogue(1, quantite=4))
        for _ in range(2):
            self.assertEqual(eng.import_catalog(), 1)

        self.assertEqual(self.rows("SELECT id, taux_tva, marque, seuil_alerte FROM Produits"), avant)
        self.assertEqual(self.rows("SELECT id, taille FROM Stocks"), stock_avant, "aucune ligne de stock ajoutée")
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks"), [(4,)], "le stock vient bien de Shopify")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Stocks s LEFT JOIN Produits p ON p.id = s.id_produit "
                                   "WHERE p.id IS NULL"), [(0,)], "pas de stock orphelin")

    def test_reimport_repete_ne_change_aucun_id(self):
        eng = self.moteur(fake_catalogue(5))
        eng.import_catalog()
        ids = self.rows("SELECT id, code_barre FROM Produits ORDER BY id")
        stocks = self.rows("SELECT id, id_produit FROM Stocks ORDER BY id")
        for _ in range(3):
            eng.import_catalog()
        self.assertEqual(self.rows("SELECT id, code_barre FROM Produits ORDER BY id"), ids)
        self.assertEqual(self.rows("SELECT id, id_produit FROM Stocks ORDER BY id"), stocks)

    def test_pagination_importe_plus_de_250_produits(self):
        eng = self.moteur(fake_catalogue(520))
        self.assertEqual(eng.import_catalog(), 520)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Produits"), [(520,)])
        self.assertEqual(len(eng.requetes), 3)
        self.assertIn("since_id=250", eng.requetes[1])
        self.assertIn("since_id=500", eng.requetes[2])

    def test_page_suivante_en_echec_ne_modifie_rien(self):
        produits = fake_catalogue(300)
        eng = self.moteur(produits)
        appels = {"n": 0}
        original = eng.make_request

        def capricieux(endpoint, *a, **k):
            appels["n"] += 1
            return original(endpoint, *a, **k) if appels["n"] == 1 else None

        eng.make_request = capricieux
        with self.assertRaises(RuntimeError):
            eng.import_catalog()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Produits"), [(0,)])

    def test_premiere_page_en_echec_retourne_zero(self):
        eng = ShopifySync(store_url="demo.myshopify.com", access_token="x")
        eng.make_request = lambda *a, **k: None
        self.assertEqual(eng.import_catalog(), 0)

    def test_variante_unique_reutilise_la_ligne_taille_unique_locale(self):
        """« Unique » (Shopify) et « Taille Unique » (POS) sont la même déclinaison : pas de doublon."""
        InventoryManager.save_product({"name": "Bougie", "barcode": "SKU-1", "price": 9, "stock": 2})
        self.assertEqual(self.rows("SELECT taille FROM Stocks"), [("Taille Unique",)])
        self.moteur(fake_catalogue(1, quantite=6)).import_catalog()
        self.assertEqual(self.rows("SELECT taille, quantite_actuelle FROM Stocks"), [("Taille Unique", 6)])

    def test_apres_import_la_vente_de_lecran_fonctionne(self):
        self.moteur(fake_catalogue(3)).import_catalog()
        _, prods = kodo_app.handle_request("GET", "/api/products", {}, {}, {})[:2]
        produit = next(p for p in prods if p["barcode"] == "SKU-2")
        status, res, _ = kodo_app.handle_request("POST", "/api/sales", {}, {}, {
            "items": [{"product": produit, "quantity": 1}], "totalTTC": 10.0, "paymentMethod": "CB",
            "cashierName": "Test", "printReceipt": False})
        self.assertEqual(status, 200, res)
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks s JOIN Produits p ON p.id = s.id_produit "
                                   "WHERE p.code_barre = 'SKU-2'"), [(6,)])


if __name__ == "__main__":
    unittest.main()
