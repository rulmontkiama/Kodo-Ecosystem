# -*- coding: utf-8 -*-
"""
Tests unitaires pour le Bouclier de Sanctuarisation (SanctuaryShield) - Kōdo POS Core.
Vérifie la détection d'intégrité, le calcul de l'invariance Delta Stock == 0
et la protection des fichiers de code sanctuarisés.
"""

import os
import sqlite3
import tempfile
import unittest
from kodo_core.db.sanctuary_shield import (
    SanctuaryShield,
    SanctuaryIntegrityError,
    SANCTUARY_TABLES,
    CODE_SANCTUARY_REGISTRY
)


class TestSanctuaryShield(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_sanctuary.db")
        self.conn = sqlite3.connect(self.db_path)
        self._init_mock_db()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _init_mock_db(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE Produits (
                id INTEGER PRIMARY KEY,
                nom TEXT,
                code_barre TEXT,
                prix_vente_tvac REAL,
                taux_tva REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE Stocks (
                id INTEGER PRIMARY KEY,
                id_produit INTEGER,
                taille TEXT,
                quantite_actuelle INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE Tickets (
                id INTEGER PRIMARY KEY,
                numero_ticket TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE Ledger_Caisse (
                id INTEGER PRIMARY KEY,
                montant REAL
            )
        """)

        # Insertion d'articles et de stocks de test
        cursor.execute("INSERT INTO Produits VALUES (1, 'Robe Fleurie', '111222', 89.95, 0.21)")
        cursor.execute("INSERT INTO Produits VALUES (2, 'Veste Cuir', '333444', 199.00, 0.21)")

        cursor.execute("INSERT INTO Stocks VALUES (1, 1, 'M', 5)")
        cursor.execute("INSERT INTO Stocks VALUES (2, 1, 'L', 3)")
        cursor.execute("INSERT INTO Stocks VALUES (3, 2, 'Unique', 2)")

        cursor.execute("INSERT INTO Tickets VALUES (1, 'TCK-2026-0001')")
        cursor.execute("INSERT INTO Ledger_Caisse VALUES (1, 89.95)")
        self.conn.commit()

    def test_fingerprint_computation(self):
        """Vérifie que l'empreinte compte fidèlement les produits et la somme des stocks."""
        fp = SanctuaryShield.compute_sanctuary_fingerprint(self.conn)
        self.assertEqual(fp["products_count"], 2)
        self.assertEqual(fp["stock_rows_count"], 3)
        self.assertEqual(fp["total_stock_units"], 10)  # 5 + 3 + 2 = 10
        self.assertEqual(fp["tickets_count"], 1)
        self.assertEqual(fp["ledger_movements_count"], 1)
        self.assertTrue(len(fp["canonical_hash"]) == 64)

    def test_invariant_success_delta_zero(self):
        """Vérifie que l'invariant Delta == 0 est validé quand le stock ne bouge pas."""
        fp1 = SanctuaryShield.compute_sanctuary_fingerprint(self.conn)
        fp2 = SanctuaryShield.compute_sanctuary_fingerprint(self.conn)
        valid, msg = SanctuaryShield.verify_invariant(fp1, fp2)
        self.assertTrue(valid)
        self.assertIn("Delta = 0", msg)

    def test_invariant_failure_on_stock_loss(self):
        """Vérifie qu'une perte ou modification inattendue du stock est immédiatement interceptée."""
        fp1 = SanctuaryShield.compute_sanctuary_fingerprint(self.conn)

        # Simulation d'une altération accidentelle du stock
        cursor = self.conn.cursor()
        cursor.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - 1 WHERE id = 1")
        self.conn.commit()

        fp2 = SanctuaryShield.compute_sanctuary_fingerprint(self.conn)
        valid, msg = SanctuaryShield.verify_invariant(fp1, fp2)
        self.assertFalse(valid)
        self.assertIn("Rupture d'intégrité de stock", msg)

    def test_code_sanctuary_registry(self):
        """Vérifie que les fichiers vitaux pour le stock et le magasin sont reconnus comme sanctuarisés."""
        self.assertTrue(SanctuaryShield.is_file_sanctuary_protected("kodo_core/services/stock_service.py"))
        self.assertTrue(SanctuaryShield.is_file_sanctuary_protected("/Volumes/Extreme SSD/KIAMA/Kōdo POS/database_manager.py"))
        self.assertTrue(SanctuaryShield.is_file_sanctuary_protected("kodo_core/domain/sales/cart_engine.py"))
        self.assertFalse(SanctuaryShield.is_file_sanctuary_protected("old_test_temp_script.py"))

    def test_atomic_backup_creation(self):
        """Vérifie la création d'une sauvegarde atomique hermétique avec son fichier meta.json."""
        backup_dir = os.path.join(self.temp_dir, "backups")
        backup_path = SanctuaryShield.create_atomic_sanctuary_backup(self.db_path, backup_dir=backup_dir)

        self.assertTrue(os.path.exists(backup_path))
        self.assertTrue(os.path.exists(f"{backup_path}.meta.json"))

        # Vérification de l'intégrité de la sauvegarde
        conn_b = sqlite3.connect(backup_path)
        try:
            fp_backup = SanctuaryShield.compute_sanctuary_fingerprint(conn_b)
            self.assertEqual(fp_backup["total_stock_units"], 10)
        finally:
            conn_b.close()


if __name__ == "__main__":
    unittest.main()
