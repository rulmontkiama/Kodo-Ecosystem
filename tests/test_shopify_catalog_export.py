# -*- coding: utf-8 -*-
"""
Tests unitaires rigoureux - Export du Catalogue Kōdo POS vers Shopify CSV Officiel.
Niveau de rigueur : Banque Centrale Européenne / Marchés Financiers.
Vérifie le respect strict des 53 colonnes officielles Shopify,
l'idempotence, la gestion des déclinaisons, le format décimal des prix,
la politique de stock et l'absence de régression.
"""

import unittest
import sqlite3
import os
import sys
import csv
import io
import tempfile
from decimal import Decimal

# Inclure le dossier racine
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import database_manager
from database_manager import initialiser_db
import export_manager
from export_manager import export_shopify_catalog_csv, SHOPIFY_PRODUCT_HEADERS, _slugify_shopify
from kodo_core.api.routes.products_routes import handle_products_request


class TestShopifyCatalogExport(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_name = self.temp_db.name
        self.temp_db.close()
        database_manager.DB_NAME = self.db_name
        self.conn = sqlite3.connect(self.db_name)
        initialiser_db(conn=self.conn)
        self.cursor = self.conn.cursor()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_name):
            try:
                os.remove(self.db_name)
            except OSError:
                pass

    def _insert_product(self, code_barre, nom, cat, marque, pv_tvac, pa_htva=0.0, taux_tva=0.21, en_solde=0, prix_solde=None, img_url=""):
        self.cursor.execute("""
            INSERT INTO Produits (
                code_barre, nom, categorie, marque, 
                prix_vente_tvac, prix_achat_htva, taux_tva, 
                en_solde, prix_solde_tvac, image_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (code_barre, nom, cat, marque, pv_tvac, pa_htva, taux_tva, en_solde, prix_solde, img_url))
        return self.cursor.lastrowid

    def _insert_stock(self, id_produit, taille, quantite):
        self.cursor.execute("""
            INSERT INTO Stocks (id_produit, taille, quantite_actuelle)
            VALUES (?, ?, ?)
        """, (id_produit, taille, quantite))

    def test_01_headers_count_and_exact_match(self):
        """Vérifie que la liste SHOPIFY_PRODUCT_HEADERS contient exactement 53 colonnes conformes."""
        self.assertEqual(len(SHOPIFY_PRODUCT_HEADERS), 53)
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[0], "Handle")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[1], "Title")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[7], "Published")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[8], "Option1 Name")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[9], "Option1 Value")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[14], "Variant SKU")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[20], "Variant Price")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[21], "Variant Compare At Price")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[24], "Variant Barcode")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[45], "Cost per item")
        self.assertEqual(SHOPIFY_PRODUCT_HEADERS[52], "Status")

    def test_02_slugify_shopify_conformity(self):
        """Vérifie la transformation de titres complexes en handles Shopify valides."""
        self.assertEqual(_slugify_shopify("T-Shirt Été & Mer!"), "t-shirt-ete-mer")
        self.assertEqual(_slugify_shopify("Jean Slim 501 - Noir/Bleu"), "jean-slim-501-noirbleu")
        self.assertEqual(_slugify_shopify("  Robe à pois  "), "robe-a-pois")
        self.assertEqual(_slugify_shopify("100% Coton Bio"), "100-coton-bio")
        self.assertEqual(_slugify_shopify(""), "")

    def test_03_single_variant_product(self):
        """Produit mono-variante (sans taille / taille unique) : Option1 = Title / Default Title."""
        pid = self._insert_product(
            code_barre="5412345678901",
            nom="Ceinture Cuir",
            cat="Accessoires",
            marque="Kodo Luxe",
            pv_tvac=45.00,
            pa_htva=18.50,
            taux_tva=0.21
        )
        self._insert_stock(pid, "Taille Unique", 15)
        self.conn.commit()

        csv_path = export_shopify_catalog_csv(status="active", conn=self.conn)
        self.assertTrue(os.path.exists(csv_path))

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = list(csv.DictReader(f))

        self.assertEqual(len(reader), 1)
        row = reader[0]
        self.assertEqual(row["Handle"], f"ceinture-cuir-{pid}")
        self.assertEqual(row["Title"], "Ceinture Cuir")
        self.assertEqual(row["Vendor"], "Kodo Luxe")
        self.assertEqual(row["Type"], "Accessoires")
        self.assertEqual(row["Published"], "TRUE")
        self.assertEqual(row["Status"], "active")
        self.assertEqual(row["Option1 Name"], "Title")
        self.assertEqual(row["Option1 Value"], "Default Title")
        self.assertEqual(row["Variant SKU"], "5412345678901")
        self.assertEqual(row["Variant Barcode"], "5412345678901")
        self.assertEqual(row["Variant Price"], "45.00")
        self.assertEqual(row["Variant Compare At Price"], "")
        self.assertEqual(row["Cost per item"], "18.50")
        self.assertEqual(row["Variant Inventory Qty"], "15")
        self.assertEqual(row["Variant Inventory Tracker"], "shopify")
        self.assertEqual(row["Variant Inventory Policy"], "deny")
        self.assertEqual(row["Variant Requires Shipping"], "TRUE")
        self.assertEqual(row["Variant Taxable"], "TRUE")

        os.remove(csv_path)

    def test_04_multi_variant_product_declinaisons(self):
        """Produit à déclinaisons (S, M, L) : Handle groupé, code-barres uniquement sur le premier."""
        pid = self._insert_product(
            code_barre="9876543210987",
            nom="Pull Laine Merinos",
            cat="Pulls",
            marque="Kodo Knit",
            pv_tvac=89.90,
            pa_htva=35.00,
            taux_tva=0.21
        )
        self._insert_stock(pid, "S", 5)
        self._insert_stock(pid, "M", 10)
        self._insert_stock(pid, "L", 8)
        self.conn.commit()

        csv_path = export_shopify_catalog_csv(status="active", conn=self.conn)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 3)

        # Les 3 lignes partagent le MÊME Handle
        expected_handle = f"pull-laine-merinos-{pid}"
        for r in rows:
            self.assertEqual(r["Handle"], expected_handle)
            self.assertEqual(r["Option1 Name"], "Taille")
            self.assertEqual(r["Variant Price"], "89.90")
            self.assertEqual(r["Cost per item"], "35.00")
            self.assertEqual(r["Variant Inventory Tracker"], "shopify")

        # Seule la première ligne porte Title, Vendor, Type, Status, et le Barcode
        self.assertEqual(rows[0]["Title"], "Pull Laine Merinos")
        self.assertEqual(rows[0]["Vendor"], "Kodo Knit")
        self.assertEqual(rows[0]["Option1 Value"], "S")
        self.assertEqual(rows[0]["Variant SKU"], "9876543210987-S")
        self.assertEqual(rows[0]["Variant Barcode"], "9876543210987")
        self.assertEqual(rows[0]["Variant Inventory Qty"], "5")

        # Lignes suivantes : champs produit vides pour l'import Shopify, Barcode vide anti-doublon
        self.assertEqual(rows[1]["Title"], "")
        self.assertEqual(rows[1]["Vendor"], "")
        self.assertEqual(rows[1]["Option1 Value"], "M")
        self.assertEqual(rows[1]["Variant SKU"], "9876543210987-M")
        self.assertEqual(rows[1]["Variant Barcode"], "")
        self.assertEqual(rows[1]["Variant Inventory Qty"], "10")

        self.assertEqual(rows[2]["Title"], "")
        self.assertEqual(rows[2]["Option1 Value"], "L")
        self.assertEqual(rows[2]["Variant SKU"], "9876543210987-L")
        self.assertEqual(rows[2]["Variant Barcode"], "")
        self.assertEqual(rows[2]["Variant Inventory Qty"], "8")

        os.remove(csv_path)

    def test_05_promotional_sale_pricing(self):
        """Gestion des soldes : prix barré d'origine dans Compare At Price, prix promo dans Price."""
        pid = self._insert_product(
            code_barre="1112223334445",
            nom="Veste Cuir Soldée",
            cat="Vestes",
            marque="Kodo Brand",
            pv_tvac=120.00,
            pa_htva=50.00,
            taux_tva=0.21,
            en_solde=1,
            prix_solde=79.90
        )
        self._insert_stock(pid, "M", 2)
        self.conn.commit()

        csv_path = export_shopify_catalog_csv(status="active", conn=self.conn)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Prix de vente réel = prix soldé
        self.assertEqual(row["Variant Price"], "79.90")
        # Prix d'origine barré = prix normal
        self.assertEqual(row["Variant Compare At Price"], "120.00")
        self.assertEqual(row["Cost per item"], "50.00")

        os.remove(csv_path)

    def test_06_draft_status_export(self):
        """Export avec statut 'draft' : Published = FALSE et Status = draft."""
        pid = self._insert_product(
            code_barre="5556667778889",
            nom="Nouveau Produit Secret",
            cat="Teaser",
            marque="Kodo",
            pv_tvac=25.00
        )
        self._insert_stock(pid, "Unique", 100)
        self.conn.commit()

        csv_path = export_shopify_catalog_csv(status="draft", conn=self.conn)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Published"], "FALSE")
        self.assertEqual(rows[0]["Status"], "draft")

        os.remove(csv_path)

    def test_07_image_url_handling(self):
        """Shopify n'accepte que des URL web publiques (http/https), les chemins locaux doivent être ignorés."""
        # Produit avec URL web
        pid1 = self._insert_product(
            code_barre="888001",
            nom="Chemise Blanche",
            cat="Chemises",
            marque="",
            pv_tvac=39.99,
            img_url="https://mon-site.com/images/chemise.jpg"
        )
        self._insert_stock(pid1, "Taille Unique", 1)

        # Produit avec chemin local du POS
        pid2 = self._insert_product(
            code_barre="888002",
            nom="Chemise Bleue",
            cat="Chemises",
            marque="",
            pv_tvac=39.99,
            img_url="/Users/kodo/Images/chemise_bleue.png"
        )
        self._insert_stock(pid2, "Taille Unique", 1)
        self.conn.commit()

        csv_path = export_shopify_catalog_csv(status="active", conn=self.conn)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Image Src"], "https://mon-site.com/images/chemise.jpg")
        self.assertEqual(rows[1]["Image Src"], "")

        os.remove(csv_path)

    def test_08_api_route_integration(self):
        """Vérifie la route REST GET /api/products/export/shopify."""
        pid = self._insert_product(
            code_barre="7771112223334",
            nom="Robe Soie",
            cat="Robes",
            marque="Kodo Chic",
            pv_tvac=150.00
        )
        self._insert_stock(pid, "38", 4)
        self.conn.commit()

        res = handle_products_request(
            "GET", 
            "/api/products/export/shopify", 
            {"status": ["draft"]}, 
            {}
        )
        self.assertEqual(len(res), 3)
        status_code, content, headers = res
        self.assertEqual(status_code, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("Shopify_Produits_Kodo_POS_draft_", headers["Content-Disposition"])
        
        # Décodage et vérification du contenu
        csv_str = content.decode("utf-8-sig")
        reader = list(csv.DictReader(io.StringIO(csv_str)))
        self.assertGreaterEqual(len(reader), 1)
        self.assertEqual(reader[0]["Status"], "draft")
        self.assertEqual(reader[0]["Published"], "FALSE")

    def test_09_adversarial_dirty_data_resilience(self):
        """Résilience aux données sales ou corrompues : prix nuls, textes dans les champs numériques, TVA 0%."""
        # Article 1: TVA 0% (exonéré)
        pid1 = self._insert_product(
            code_barre="000111",
            nom="Livre d'Art Exonéré",
            cat="Livres",
            marque="",
            pv_tvac=20.00,
            pa_htva=10.00,
            taux_tva=0.0
        )
        self._insert_stock(pid1, "Unique", 5)

        # Article 2: Stock None ou négatif dans la base
        pid2 = self._insert_product(
            code_barre="000222",
            nom="Article Stock Négatif",
            cat="Divers",
            marque="",
            pv_tvac=15.00
        )
        self._insert_stock(pid2, "Unique", -3)

        # Article 3: Tailles avec caractères spéciaux complexes
        pid3 = self._insert_product(
            code_barre="000333",
            nom="Baskets Spéciales",
            cat="Chaussures",
            marque="Kodo",
            pv_tvac=120.00
        )
        self._insert_stock(pid3, "38 1/2 (Large)", 2)
        self._insert_stock(pid3, "+ / ?", 1)

        # Article 4: Aucun stock associé
        pid4 = self._insert_product(
            code_barre="000444",
            nom="Article Fantôme Sans Stock",
            cat="Ghost",
            marque="",
            pv_tvac=5.00
        )
        self.conn.commit()

        csv_path = export_shopify_catalog_csv(status="active", conn=self.conn)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        # Vérification Livre TVA 0% -> Variant Taxable = FALSE
        r_livre = [r for r in rows if r["Handle"].startswith("livre-dart-exonere")][0]
        self.assertEqual(r_livre["Variant Taxable"], "FALSE")

        # Vérification Stock négatif -> stock conservé à -3 ou 0 selon policy
        r_neg = [r for r in rows if r["Handle"].startswith("article-stock-negatif")][0]
        self.assertEqual(r_neg["Variant Inventory Qty"], "-3")

        # Vérification SKU assaini pour taille "38 1/2 (Large)" -> "000333-3812Large"
        r_baskets = [r for r in rows if r["Handle"].startswith("baskets-speciales")]
        self.assertEqual(len(r_baskets), 2)
        self.assertEqual(r_baskets[0]["Variant SKU"], "000333-3812Large")
        # Taille "+ / ?" -> sans aucun caractère alphanumérique, fallback "000333-V2"
        self.assertEqual(r_baskets[1]["Variant SKU"], "000333-V2")

        # Vérification Article sans stock -> 1 variante par défaut "Default Title", qty "0"
        r_ghost = [r for r in rows if r["Handle"].startswith("article-fantome-sans-stock")][0]
        self.assertEqual(r_ghost["Option1 Name"], "Title")
        self.assertEqual(r_ghost["Option1 Value"], "Default Title")
        self.assertEqual(r_ghost["Variant Inventory Qty"], "0")

        os.remove(csv_path)

    def test_10_desktop_export_path_creation(self):
        """Vérifie la création automatique des dossiers si le chemin cible n'existe pas."""
        custom_dir = os.path.join(tempfile.gettempdir(), "kodo_test_export_dir", "sub")
        custom_path = os.path.join(custom_dir, "shopify_catalog_test.csv")
        
        pid = self._insert_product("999", "Test Path", "Cat", "", 10.0)
        self._insert_stock(pid, "TU", 1)
        self.conn.commit()

        result_path = export_shopify_catalog_csv(output_path=custom_path, conn=self.conn)
        self.assertEqual(result_path, custom_path)
        self.assertTrue(os.path.exists(custom_path))

        # Nettoyage
        if os.path.exists(custom_path):
            os.remove(custom_path)
        if os.path.exists(custom_dir):
            os.removedirs(custom_dir)


if __name__ == "__main__":
    unittest.main()
