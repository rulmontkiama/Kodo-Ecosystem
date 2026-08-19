import unittest
import os
import sys
import tempfile
import sqlite3
import json
from unittest.mock import MagicMock, patch

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import ShopConfig, ShopProfile
import database_manager
import kodo_core
from kodo_core.sync.shopify import ShopifySync, ShopifySyncThread
from kodo_core.sync.firebase import FirebaseSync, FirebaseSyncThread
from kodo_core.sync.offline_engine import OfflineSyncEngine
from kodo_core.services.license import (
    get_machine_fingerprint,
    generate_local_signature,
    save_local_license,
    load_local_license,
    check_license,
    activate_license_key,
    get_license_info,
)
from kodo_core.services.updater import (
    parse_version,
    check_for_updates_sync,
    apply_remote_update_sync,
    AppUpdateEngine,
)


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

        cursor.execute("PRAGMA table_info(Produits)")
        cols_produits = [row[1] for row in cursor.fetchall()]
        self.assertIn("type_vente", cols_produits)
        self.assertIn("unite_mesure", cols_produits)
        self.assertIn("marque", cols_produits)

        cursor.execute("SELECT nom_magasin, type_commerce FROM ShopInfo")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "L'Adresse B")

        conn.close()

    def test_license_management_and_hwid(self):
        """Vérifie la génération HWID, la signature cryptographique et l'activation."""
        hwid = get_machine_fingerprint()
        self.assertEqual(len(hwid), 16)
        self.assertTrue(hwid.isalnum())

        sig = generate_local_signature(hwid, "active", "2056-08-10", "2026-08-14")
        self.assertIsInstance(sig, str)

        save_local_license("active", "2056-08-10", "2026-08-14", "KODO-PRO-TEST-2026")
        cache = load_local_license()
        self.assertIsNotNone(cache)
        self.assertEqual(cache.get("fingerprint"), hwid)
        self.assertEqual(cache.get("status"), "active")

        info = get_license_info()
        self.assertEqual(info["fingerprint"], hwid)
        self.assertIn("enabled_features", info)

        success, msg = activate_license_key("KODO-TEST-KEY-123456")
        self.assertTrue(success)

    def test_shopify_sync_engine(self):
        """Vérifie les fonctions de synchronisation REST/GraphQL Shopify."""
        sync = ShopifySync(store_url="https://test-shop.myshopify.com", access_token="shpat_12345")
        self.assertEqual(sync.store_url, "https://test-shop.myshopify.com")

        # Mock REST API response for location
        mock_response = json.dumps({"locations": [{"id": 98765, "active": True}]}).encode("utf-8")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_cm = MagicMock()
            mock_cm.__enter__.return_value.read.return_value = mock_response
            mock_urlopen.return_value = mock_cm

            loc_id = sync.get_location_id()
            self.assertEqual(loc_id, 98765)

    def test_firebase_sync_engine(self):
        """Vérifie la migration de colonnes et l'initialisation de FirebaseSync."""
        fb_sync = FirebaseSync()
        fb_sync.migrate_sync_columns()

        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(Tickets)")
        cols = [r[1] for r in cursor.fetchall()]
        self.assertIn("synced", cols)
        conn.close()

    def test_offline_engine_lww_and_audit(self):
        """Vérifie la file d'attente hors-ligne, le marquage LWW et conflit de stock."""
        OfflineSyncEngine.init_db_schema()

        conn = sqlite3.connect(self.temp_db_path)
        c = conn.cursor()
        c.execute("INSERT INTO Categories (nom) VALUES ('TestCat')")
        cat_id = c.lastrowid
        c.execute("INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac) VALUES ('SKU-OFF', 'Article Off', 'TestCat', 10.0)")
        p_id = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'Unique', 0)", (p_id,))
        s_id = c.lastrowid

        c.execute("""
            INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, sync_status)
            VALUES ('TCK-OFF-001', '2026-08-14 12:00:00', 10.0, 0)
        """)
        t_id = c.lastrowid

        c.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, 1, 10.0)
        """, (t_id, s_id))
        conn.commit()
        conn.close()

        count = OfflineSyncEngine.process_pending_tickets()
        self.assertEqual(count, 1)

        conn = sqlite3.connect(self.temp_db_path)
        c = conn.cursor()
        c.execute("SELECT quantite_actuelle, requires_stock_audit FROM Stocks WHERE id=?", (s_id,))
        row = c.fetchone()
        self.assertEqual(row[0], -1)
        self.assertEqual(row[1], 1)
        conn.close()

    def test_updater_semver_and_overlay(self):
        """Vérifie parse_version SemVer et la logique d'update."""
        self.assertEqual(parse_version("1.0.19"), (1, 0, 19))
        self.assertEqual(parse_version("v2.1.0"), (2, 1, 0))
        self.assertTrue(parse_version("1.0.19") > parse_version("1.0.18"))

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = json.dumps({"latestVersion": "1.0.19"}).encode("utf-8")
            mock_cm = MagicMock()
            mock_cm.__enter__.return_value.read.return_value = mock_response
            mock_urlopen.return_value = mock_cm

            res = check_for_updates_sync("1.0.18")
            self.assertTrue(res.get("has_update"))


if __name__ == "__main__":
    unittest.main()
