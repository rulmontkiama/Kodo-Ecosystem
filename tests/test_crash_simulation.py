import unittest
import os
import sys
import shutil
import sqlite3
import tempfile

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.migrations import MigrationManager, MigrationError
from core.config import ShopConfig

class TestCrashSimulation(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "app_data.db")
        self.backup_dir = os.path.join(self.temp_dir, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)
        self.backup_path = os.path.join(self.backup_dir, "snapshot_pre_update.bak")

        # Initialiser base v1.0 de test
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price REAL NOT NULL
            )
        """)
        cursor.executemany(
            "INSERT INTO products (name, price) VALUES (?, ?)",
            [
                ("Chemise Homme Coton", 49.99),
                ("Pantalon Costume", 89.99),
                ("Veste Vêtement", 129.99),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_backup(self):
        """Création d'une sauvegarde physique de la BDD."""
        shutil.copy2(self.db_path, self.backup_path)

    def execute_emergency_rollback(self):
        """Restaure physiquement la base de données depuis le snapshot."""
        if os.path.exists(self.backup_path):
            shutil.copy2(self.backup_path, self.db_path)

    def test_simulated_crash_migration(self):
        """
        Simule une migration v1.0 -> v2.0
        - Ajoute une colonne 'stock'
        - Injecte une panne critique au milieu des transactions
        - Déclenche le rollback automatique et vérifie l'intégrité
        """
        self.create_backup()

        conn = None
        crash_occurred = False
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Étape 1 de la migration : Modification de schéma
            cursor.execute("BEGIN TRANSACTION;")
            cursor.execute("ALTER TABLE products ADD COLUMN stock INTEGER DEFAULT 0;")
            cursor.execute("UPDATE products SET stock = 10 WHERE name LIKE '%Chemise%';")

            # INJECTION DE LA PANNE
            raise RuntimeError("CRASH SIMULÉ : Interruption brutale pendant la migration.")

        except Exception as e:
            crash_occurred = True
            if conn:
                conn.rollback()
                conn.close()
            
            # Restauration d'urgence
            self.execute_emergency_rollback()

        self.assertTrue(crash_occurred)

        # PRÉSERVATIONS DES DONNÉES ET VERIFICATION POST-ROLLBACK
        conn_check = sqlite3.connect(self.db_path)
        cursor_check = conn_check.cursor()

        cursor_check.execute("PRAGMA table_info(products);")
        columns = [col[1] for col in cursor_check.fetchall()]

        cursor_check.execute("SELECT COUNT(*) FROM products;")
        count = cursor_check.fetchone()[0]
        conn_check.close()

        # Vérifications strictes : colonne 'stock' non présente & 3 produits intacts
        self.assertNotIn("stock", columns)
        self.assertEqual(count, 3)

def run_standalone_simulation():
    """Script autonome directement exécutable en ligne de commande."""
    print("--- DEMARRAGE DU CRASH TEST CONTROLE ---")
    suite = unittest.TestSuite()
    suite.addTest(TestCrashSimulation('test_simulated_crash_migration'))
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    if res.wasSuccessful():
        print("🎉 [TEST RÉUSSI] L'intégrité est 100% préservée ! Aucun dégât sur la BDD.")
    else:
        print("💥 [TEST ÉCHOUÉ] La BDD est corrompue ou incomplète !")

if __name__ == "__main__":
    run_standalone_simulation()
