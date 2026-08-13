import unittest
import os
import sys
import tempfile
import sqlite3
import shutil

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.migrations import MigrationManager, MigrationError
from core.config import ShopConfig

class TestMigrations(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_kodo.db")
        # Forcer les snapshots vers un sous-dossier temporaire
        ShopConfig.get_snapshots_dir = lambda: os.path.join(self.temp_dir, "snapshots")
        os.makedirs(ShopConfig.get_snapshots_dir(), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_run_migrations_success(self):
        """Vérifie l'application séquentielle des migrations v1.0.0 et v1.1.0."""
        MigrationManager.run_migrations(self.db_path)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Vérifier que les versions sont enregistrées dans schema_version
        cursor.execute("SELECT version FROM schema_version ORDER BY version ASC")
        versions = [row[0] for row in cursor.fetchall()]
        self.assertIn("1.0.0", versions)
        self.assertIn("1.1.0", versions)
        
        # Vérifier l'existence des tables métiers
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        self.assertIn("Produits", tables)
        self.assertIn("Tickets", tables)
        self.assertIn("ShopInfo", tables)
        
        conn.close()

    def test_pre_migration_snapshot_created(self):
        """Vérifie qu'un snapshot de sauvegarde est généré avant d'exécuter de nouvelles migrations."""
        # 1. Appliquer v1.0.0 uniquement
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES ('1.0.0')")
        conn.commit()
        conn.close()

        # 2. Exécuter la migration vers v1.1.0
        MigrationManager.run_migrations(self.db_path)

        # 3. Vérifier que le dossier snapshots contient au moins un snapshot .db
        snapshots = os.listdir(ShopConfig.get_snapshots_dir())
        self.assertTrue(len(snapshots) > 0)
        self.assertTrue(any(s.endswith(".db") for s in snapshots))

    def test_rollback_on_migration_failure(self):
        """Simule un échec de script de migration et vérifie la restauration du snapshot."""
        # Initialiser v1.0.0
        MigrationManager.run_migrations(self.db_path)
        
        # Ajouter une donnée test
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO Produits (nom, prix_vente_tvac) VALUES ('Produit Test Rollback', 50.0)")
        conn.commit()
        conn.close()

        # Injecter temporairement une migration corrompue
        corrupted_migration = {
            "version": "9.9.9",
            "description": "Migration Corrompue Test",
            "sql": [
                "CREATE TABLE TableValide (id INT)",
                "SYNTAX ERROR INVALID SQL STATEMENT THAT WILL FAIL"
            ]
        }
        MigrationManager.MIGRATIONS.append(corrupted_migration)

        try:
            with self.assertRaises(MigrationError):
                MigrationManager.run_migrations(self.db_path)
            
            # Vérifier que la base a été restaurée à son état d'origine (Produit Test toujours présent)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT nom FROM Produits WHERE nom='Produit Test Rollback'")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            
            # Vérifier que la version corrompue 9.9.9 n'a PAS été validée
            cursor.execute("SELECT version FROM schema_version WHERE version='9.9.9'")
            self.assertIsNone(cursor.fetchone())
            conn.close()

        finally:
            # Retirer la migration corrompue du registre
            MigrationManager.MIGRATIONS.pop()

if __name__ == "__main__":
    unittest.main()
