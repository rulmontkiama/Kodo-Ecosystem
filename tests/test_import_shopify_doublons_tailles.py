# -*- coding: utf-8 -*-
"""Un produit Shopify décliné en tailles = UN produit local à plusieurs lignes de stock.

Défaut constaté chez un client : après import, l'article d'origine (S:4 | M:6) coexistait avec
une fiche « même nom » par taille, ce qui comptait le stock et sa valeur plusieurs fois.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.test_import_shopify import TestImportShopify as _Base

ROBE = {"id": 5, "title": "Robe", "product_type": "Femme", "variants": [
    {"id": 51, "sku": "BI038-S", "price": "50.00", "inventory_quantity": 4, "option1": "S"},
    {"id": 52, "sku": "BI038-M", "price": "50.00", "inventory_quantity": 6, "option1": "M"},
    {"id": 53, "sku": "BI038-L", "price": "50.00", "inventory_quantity": 2, "option1": "L"}]}


class TestImportSansDoublons(_Base):
    test_reimport_conserve_id_tva_marque_seuil_et_ligne_de_stock = None
    test_reimport_repete_ne_change_aucun_id = None
    test_pagination_importe_plus_de_250_produits = None
    test_page_suivante_en_echec_ne_modifie_rien = None
    test_premiere_page_en_echec_retourne_zero = None
    test_variante_unique_reutilise_la_ligne_taille_unique_locale = None
    test_apres_import_la_vente_de_lecran_fonctionne = None

    def tailles(self):
        return self.rows("SELECT p.id, p.nom, p.code_barre, s.taille, s.quantite_actuelle "
                         "FROM Stocks s JOIN Produits p ON p.id = s.id_produit ORDER BY s.id")

    def test_import_a_vide_cree_un_seul_produit(self):
        self.moteur([ROBE]).import_catalog()
        lignes = self.tailles()
        self.assertEqual({l[0] for l in lignes}, {1})
        self.assertEqual([(l[3], l[4]) for l in lignes], [("S", 4), ("M", 6), ("L", 2)])
        self.assertEqual(lignes[0][2], "BI038")

    def test_article_local_existant_n_est_pas_double(self):
        InventoryManager = __import__("kodo_core.domain.catalog.inventory_manager",
                                      fromlist=["InventoryManager"]).InventoryManager
        InventoryManager.save_product({"name": "Robe", "barcode": "BI038", "price": 50,
                                       "sizes": "S:1 | M:1 | L:1"})
        self.moteur([ROBE]).import_catalog()
        lignes = self.tailles()
        self.assertEqual(len({l[0] for l in lignes}), 1)
        self.assertEqual([(l[3], l[4]) for l in lignes], [("S", 4), ("M", 6), ("L", 2)])

    def test_doublons_existants_sont_refondus_et_ventes_conservees(self):
        conn = __import__("database_manager").get_connection()
        c = conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac) VALUES ('BI038', 'Robe', 50)")
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (1, 'S', 4)")
        for i, t in ((2, "S"), (3, "M")):
            c.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac) VALUES (?, 'Robe', 50)", (f"BI038-{t}",))
            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, ?, 9)", (i, t))
            c.execute("INSERT INTO Shopify_Variantes (variant_id, id_produit) VALUES (?, ?)", (50 + i - 1, i)) \
                if False else None
        c.execute("CREATE TABLE IF NOT EXISTS Shopify_Variantes (variant_id INTEGER PRIMARY KEY, id_produit INTEGER NOT NULL, date_maj TEXT)")
        c.execute("INSERT INTO Shopify_Variantes (variant_id, id_produit) VALUES (51, 2)")
        c.execute("INSERT INTO Shopify_Variantes (variant_id, id_produit) VALUES (52, 3)")
        c.execute("INSERT INTO Tickets (numero_ticket, total_tvac) VALUES ('T1', 50)")
        c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) VALUES (1, 2, 1, 50)")
        conn.commit()
        conn.close()

        self.moteur([ROBE]).import_catalog()

        self.assertEqual(self.rows("SELECT id FROM Produits"), [(1,)])
        self.assertEqual([(l[3], l[4]) for l in self.tailles() if l[4]], [("S", 4), ("M", 6), ("L", 2)])
        # La vente scellée garde sa ligne, rattachée au produit conservé et ramenée à 0.
        self.assertEqual(self.rows("SELECT id_stock FROM Ventes_Details"), [(2,)])
        self.assertEqual(self.rows("SELECT id_produit, quantite_actuelle FROM Stocks WHERE id=2"), [(1, 0)])
        self.assertEqual(self.rows("SELECT DISTINCT id_produit FROM Shopify_Variantes"), [(1,)])


if __name__ == "__main__":
    unittest.main()
