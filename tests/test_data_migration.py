# -*- coding: utf-8 -*-
"""
Tests unitaires pour le système d'exportation, prévisualisation et restauration de données (Pack ZIP de Migration).
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
import base64

import backup_manager
import database_manager


class TestDataMigration(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="kodo_test_migration_")
        self.test_db = os.path.join(self.test_dir, "test_kodo.db")

        # Initialiser une base de test avec des données
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE products (
                id TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                stock INTEGER,
                category TEXT
            );
        """)
        cursor.execute("""
            CREATE TABLE clients (
                id TEXT PRIMARY KEY,
                name TEXT,
                email TEXT,
                phone TEXT
            );
        """)
        cursor.execute("""
            CREATE TABLE sales (
                id TEXT PRIMARY KEY,
                receipt_number TEXT,
                date TEXT,
                total_ttc REAL
            );
        """)
        cursor.execute("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                name TEXT,
                role TEXT,
                pin_hash TEXT
            );
        """)

        # Insérer des données de test
        cursor.execute("INSERT INTO products VALUES ('p1', 'Shampoing Bio', 18.50, 25, 'Soins');")
        cursor.execute("INSERT INTO products VALUES ('p2', 'Sérum Éclat', 45.00, 10, 'Visage');")
        cursor.execute("INSERT INTO clients VALUES ('c1', 'Sophie Martin', 'sophie@test.fr', '0601020304');")
        cursor.execute("INSERT INTO sales VALUES ('s1', 'TK-1001', '2026-08-17', 63.50);")
        cursor.execute("INSERT INTO users VALUES ('u1', 'Admin Kiama', 'admin', 'dummyhash');")

        conn.commit()
        conn.close()

        # Configurer backup_manager pour pointer sur notre base de test
        self.original_db_name = backup_manager.DB_NAME
        backup_manager.DB_NAME = self.test_db

    def tearDown(self):
        backup_manager.DB_NAME = self.original_db_name
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_preview_restore_flow(self):
        # 1. Export du pack de transfert
        success, filename, zip_bytes, manifest = backup_manager.creer_pack_migration_machine()
        self.assertTrue(success)
        self.assertTrue(filename.startswith("Kodo_POS_Transfert_"))
        self.assertTrue(len(zip_bytes) > 0)
        self.assertEqual(manifest["stats"]["products_count"], 2)
        self.assertEqual(manifest["stats"]["clients_count"], 1)
        self.assertEqual(manifest["stats"]["sales_count"], 1)
        self.assertEqual(manifest["stats"]["users_count"], 1)

        # 2. Prévisualisation de l'archive ZIP
        preview = backup_manager.previsualiser_pack_migration(zip_bytes)
        self.assertTrue(preview["valid"])
        self.assertTrue(preview.get("sha_match", True))
        self.assertEqual(preview["stats"]["products_count"], 2)
        self.assertEqual(preview["stats"]["clients_count"], 1)

        # 3. Simulation d'une nouvelle machine avec une base vide
        new_machine_db = os.path.join(self.test_dir, "new_machine_kodo.db")
        conn = sqlite3.connect(new_machine_db)
        conn.execute("CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT);")
        conn.commit()
        conn.close()

        backup_manager.DB_NAME = new_machine_db

        # 4. Restauration du pack de transfert sur la nouvelle machine
        restore_result = backup_manager.restaurer_pack_migration(zip_bytes)
        self.assertTrue(restore_result["success"])
        self.assertEqual(restore_result["stats"]["products_count"], 2)

        # 5. Vérifier que les données existent bien dans la nouvelle base
        check_conn = sqlite3.connect(new_machine_db)
        cursor = check_conn.cursor()
        cursor.execute("SELECT name, price FROM products WHERE id='p1';")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'Shampoing Bio')
        self.assertEqual(row[1], 18.50)

        cursor.execute("SELECT name FROM clients WHERE id='c1';")
        client_row = cursor.fetchone()
        self.assertIsNotNone(client_row)
        self.assertEqual(client_row[0], 'Sophie Martin')
        check_conn.close()


if __name__ == '__main__':
    unittest.main()
