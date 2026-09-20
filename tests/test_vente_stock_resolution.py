# -*- coding: utf-8 -*-
"""Vente : la ligne de stock est retrouvée par (produit, taille), jamais en confondant id produit et id stock.

Reproduit le bug d'un client dont les ids produit (184, 187...) ne coïncidaient plus avec les ids de stock
(734, 737...) : « Article introuvable en base (stock_id=184) ». L'écran envoie `{product, quantity,
selectedSize}` SANS stock_id ; ces tests utilisent exactement ce format, via l'API HTTP interne.
Base temporaire : aucune donnée réelle touchée.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.domain.sales.cart_engine import process_sale_transaction


class TestVenteStockResolution(unittest.TestCase):
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

    # -- helpers ---------------------------------------------------------------------------------
    def api(self, method, path, data=None):
        status, body, _ = kodo_app.handle_request(method, path, {}, {}, data or {})
        return status, body

    def produit(self, name):
        _, prods = self.api("GET", "/api/products")
        return next(p for p in prods if p["name"] == name)

    def vendre(self, product, size=None, qty=1, total=None, mode="CB"):
        """Vente telle que App.tsx handleCompleteSale l'envoie : pas de stock_id."""
        total = product["price"] * qty if total is None else total
        return self.api("POST", "/api/sales", {
            "items": [{"product": product, "quantity": qty, "size": size, "selectedSize": size}],
            "totalTTC": total, "paymentMethod": mode, "cashierName": "Test",
            "changeGiven": 0, "printReceipt": False,
        })

    def stock(self, nom, taille=None):
        conn = database_manager.get_connection()
        try:
            q = ("SELECT s.quantite_actuelle FROM Stocks s JOIN Produits p ON p.id = s.id_produit WHERE p.nom = ?")
            params = [nom]
            if taille is not None:
                q += " AND s.taille = ?"
                params.append(taille)
            return conn.cursor().execute(q, params).fetchone()[0]
        finally:
            conn.close()

    def lignes_vendues(self):
        conn = database_manager.get_connection()
        try:
            return [tuple(r) for r in conn.cursor().execute("""
                SELECT p.nom, s.taille, vd.quantite, vd.prix_unitaire_tvac
                FROM Ventes_Details vd JOIN Stocks s ON s.id = vd.id_stock JOIN Produits p ON p.id = s.id_produit
                ORDER BY vd.id""").fetchall()]
        finally:
            conn.close()

    def creer_catalogue_decale(self):
        """Robe (2 tailles) créée en premier : les ids produit et stock divergent ensuite."""
        self.api("POST", "/api/products", {"name": "Robe", "price": 50, "sizes": "S:5|M:5"})
        self.api("POST", "/api/products", {"name": "Spray Interieur", "price": 12, "stock": 10})
        self.api("POST", "/api/products", {"name": "Collection Prive", "price": 25, "stock": 32})

    # -- tests -----------------------------------------------------------------------------------
    def test_article_a_taille_unique_dont_id_produit_differe_de_id_stock(self):
        self.creer_catalogue_decale()
        spray = self.produit("Spray Interieur")
        self.assertNotEqual(int(spray["id"]), spray["stocks"][0]["stock_id"], "le scénario doit faire diverger les ids")

        status, res = self.vendre(spray)

        self.assertEqual(status, 200, res)
        self.assertEqual(self.lignes_vendues(), [("Spray Interieur", "Taille Unique", 1, 12.0)])
        self.assertEqual(self.stock("Spray Interieur"), 9)
        self.assertEqual(self.stock("Robe", "S"), 5)
        self.assertEqual(self.stock("Robe", "M"), 5)

    def test_le_stock_de_la_bonne_taille_est_debite(self):
        self.creer_catalogue_decale()
        robe = self.produit("Robe")

        status, res = self.vendre(robe, size="M", qty=2)

        self.assertEqual(status, 200, res)
        self.assertEqual(self.stock("Robe", "M"), 3)
        self.assertEqual(self.stock("Robe", "S"), 5)
        self.assertEqual(self.lignes_vendues(), [("Robe", "M", 2, 50.0)])

    def test_id_produit_egal_a_un_id_stock_dun_autre_article_ne_detourne_pas_la_vente(self):
        """Avant : vendre le produit n°2 débitait le STOCK n°2 (= Robe M) et facturait 50 € au lieu de 12 €."""
        self.creer_catalogue_decale()
        spray = self.produit("Spray Interieur")
        self.assertEqual(int(spray["id"]), 2)  # id produit 2 ...
        conn = database_manager.get_connection()
        try:
            proprio_stock_2 = conn.cursor().execute(
                "SELECT p.nom FROM Stocks s JOIN Produits p ON p.id = s.id_produit WHERE s.id = 2").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(proprio_stock_2, "Robe")  # ... = id de stock de « Robe M »

        status, res = self.vendre(spray)

        self.assertEqual(status, 200, res)
        self.assertEqual(res["ticket"]["total_tvac"], 12.0)
        self.assertEqual(self.stock("Robe", "M"), 5)
        self.assertEqual(self.stock("Spray Interieur"), 9)

    def test_taille_obligatoire_quand_plusieurs_tailles(self):
        self.creer_catalogue_decale()
        status, res = self.vendre(self.produit("Robe"), size=None)

        self.assertEqual(status, 400)
        self.assertIn("Précisez la taille", res["error"])
        self.assertIn("S, M", res["error"])
        self.assertEqual(self.stock("Robe", "S"), 5)

    def test_taille_inconnue_refusee(self):
        self.creer_catalogue_decale()
        status, res = self.vendre(self.produit("Robe"), size="XXL")

        self.assertEqual(status, 400)
        self.assertIn("Taille « XXL » introuvable", res["error"])

    def test_taille_unique_acceptee_avec_ou_sans_libelle(self):
        self.creer_catalogue_decale()
        spray = self.produit("Spray Interieur")
        for size in (None, "", "Taille Unique", "Unique", "__NO_SIZE__"):
            status, res = self.vendre(spray, size=size)
            self.assertEqual(status, 200, (size, res))
        self.assertEqual(self.stock("Spray Interieur"), 5)

    def test_deux_lignes_du_meme_article_sont_cumulees_pour_le_controle_de_stock(self):
        self.creer_catalogue_decale()
        conn = database_manager.get_connection()
        try:
            conn.cursor().execute("UPDATE Stocks SET quantite_actuelle = 3 WHERE id_produit = "
                                  "(SELECT id FROM Produits WHERE nom = 'Spray Interieur')")
            conn.commit()
        finally:
            conn.close()
        spray = self.produit("Spray Interieur")
        status, res = self.api("POST", "/api/sales", {
            "items": [{"product": spray, "quantity": 2}, {"product": spray, "quantity": 2, "size": "Taille Unique"}],
            "totalTTC": 48, "paymentMethod": "CB", "cashierName": "Test", "printReceipt": False,
        })
        self.assertEqual(status, 400)
        self.assertIn("Stock insuffisant", res["error"])
        self.assertEqual(self.stock("Spray Interieur"), 3)

    def test_produit_inexistant_ou_prestation_locale_refuse_proprement(self):
        for fake_id in ("9999", "pres-1"):
            status, res = self.api("POST", "/api/sales", {
                "items": [{"product": {"id": fake_id, "name": "X", "price": 5}, "quantity": 1}],
                "totalTTC": 5, "paymentMethod": "CB", "cashierName": "Test", "printReceipt": False,
            })
            self.assertEqual(status, 400, fake_id)
            self.assertIn("Article introuvable en base (produit=", res["error"])

    def test_stock_id_explicite_doit_appartenir_au_produit_indique(self):
        self.creer_catalogue_decale()
        robe, spray = self.produit("Robe"), self.produit("Spray Interieur")
        stock_de_la_robe = robe["stocks"][0]["stock_id"]
        conn = database_manager.get_connection()
        try:
            with self.assertRaises(ValueError) as ctx:
                process_sale_transaction(
                    cart_items=[{"product_id": spray["product_id"], "stock_id": stock_de_la_robe, "quantite": 1}],
                    total_tvac=12.0, payments=[("CB", 12.0)], conn=conn)
            self.assertIn("n'appartient pas", str(ctx.exception))
        finally:
            conn.close()

    def test_appelant_historique_avec_stock_id_seul_reste_supporte(self):
        self.creer_catalogue_decale()
        spray = self.produit("Spray Interieur")
        conn = database_manager.get_connection()
        try:
            res = process_sale_transaction(
                cart_items=[{"stock_id": spray["stocks"][0]["stock_id"], "quantite": 1}],
                total_tvac=12.0, payments=[("CB", 12.0)], conn=conn)
        finally:
            conn.close()
        self.assertTrue(res["success"])
        self.assertEqual(self.stock("Spray Interieur"), 9)


if __name__ == "__main__":
    unittest.main()
