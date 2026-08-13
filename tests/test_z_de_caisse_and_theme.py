import unittest
import os
import sys
import tempfile
import sqlite3
from decimal import Decimal

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import initialiser_db, generer_bilan_z_journalier, enregistrer_cloture_caisse, enregistrer_vente
from audit_trail import calculer_hash_cloture

class TestZDeCaisseAndTheme(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "kodo_pos.db")
        database_manager.DB_NAME = self.db_path
        initialiser_db()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bilan_et_cloture_z_cryptographique(self):
        """Vérifie le calcul du bilan Z et la génération de la clôture scellée en SHA-256."""
        conn = database_manager.get_connection()
        c = conn.cursor()

        # Enregistrer 2 ventes de test
        panier = [{"nom": "Robe Test", "taille": "M", "prix_vente_tvac": Decimal("100.00"), "taux_tva": Decimal("0.21"), "stock_id": 1}]
        enregistrer_vente(c, "TKT-001", Decimal("100.00"), Decimal("82.64"), Decimal("17.36"), Decimal("0.00"), "Espèces", None, Decimal("0.00"), panier, "Vendeur1", "2026-07-28 10:00:00", [("Espèces", Decimal("100.00"))])
        enregistrer_vente(c, "TKT-002", Decimal("50.00"), Decimal("41.32"), Decimal("8.68"), Decimal("0.00"), "Carte", None, Decimal("0.00"), panier, "Vendeur1", "2026-07-28 11:00:00", [("Carte", Decimal("50.00"))])
        conn.commit()
        conn.close()

        # 1. Obtenir le bilan Z
        bilan = generer_bilan_z_journalier("POS-01")
        self.assertEqual(bilan["nb_tickets"], 2)
        self.assertEqual(bilan["total_tvac"], Decimal("150.00"))
        self.assertEqual(bilan["total_especes"], Decimal("100.00"))
        self.assertEqual(bilan["total_carte"], Decimal("50.00"))

        # 2. Enregistrer la clôture Z
        res = enregistrer_cloture_caisse(caisse_id="POS-01", fond_caisse_reel=Decimal("100.00"), vendeur="Admin")
        self.assertIsNotNone(res["current_hash"])
        self.assertEqual(res["ecart"], Decimal("0.00"))

        # 3. Vérifier l'insertion dans la BDD
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("SELECT id, total_ventes_tvac, hash_precedent, current_hash FROM Clotures_Caisse WHERE caisse_id='POS-01'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(Decimal(str(row[1])), Decimal("150.00"))
        self.assertEqual(row[3], res["current_hash"])

    def test_chaine_cryptographique_double_cloture(self):
        """Vérifie le chaînage SHA-256 entre 2 clôtures successives."""
        res1 = enregistrer_cloture_caisse("POS-01", Decimal("50.00"))
        res2 = enregistrer_cloture_caisse("POS-01", Decimal("50.00"))

        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("SELECT hash_precedent, current_hash FROM Clotures_Caisse ORDER BY id ASC")
        rows = c.fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        # Le hash précédent de la 2ème clôture doit être le current_hash de la 1ère
        self.assertEqual(rows[1][0], rows[0][1])

if __name__ == "__main__":
    unittest.main()
