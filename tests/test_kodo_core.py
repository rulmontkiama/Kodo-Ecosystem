import unittest
import os
import sys
import tempfile
import sqlite3

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import ShopConfig, ShopProfile
import database_manager

class TestKodoCore(unittest.TestCase):

    def setUp(self):
        # Utiliser une BDD SQLite temporaire pour les tests
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        database_manager.DB_NAME = self.temp_db_path
        database_manager.initialiser_db()

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_shop_config_defaults(self):
        """Vérifie la configuration multi-commerce par défaut."""
        self.assertEqual(ShopConfig.PROFIL_METIER, ShopProfile.PRET_A_PORTER)
        self.assertIn("Kodo_POS", ShopConfig.get_base_data_dir())

    def test_database_initialization_and_schema(self):
        """Vérifie la présence des tables et colonnes multi-commerces."""
        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        
        # Vérification des colonnes de Produits
        cursor.execute("PRAGMA table_info(Produits)")
        cols_produits = [row[1] for row in cursor.fetchall()]
        self.assertIn("type_vente", cols_produits)
        self.assertIn("unite_mesure", cols_produits)
        self.assertIn("marque", cols_produits)
        self.assertIn("attributs_json", cols_produits)
        
        # Vérification de la table ShopInfo
        cursor.execute("SELECT nom_magasin, type_commerce FROM ShopInfo")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "L'Adresse B")
        self.assertEqual(row[1], "pret_a_porter")
        
        conn.close()

if __name__ == "__main__":
    unittest.main()
