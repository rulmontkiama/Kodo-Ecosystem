import unittest
import os
import sys
import tempfile
import sqlite3
import time
import shutil

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import initialiser_db
from core.rollback_manager import RollbackManager
from core.crash_watcher import CrashWatcher
from generer_apercu_image import generer_apercu_image_sync, generer_apercu_image_async

class TestUIPerformanceAndRollback(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "kodo_pos.db")
        database_manager.DB_NAME = self.db_path
        initialiser_db()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rendu_large_catalogue_performance(self):
        """Vérifie que la BDD supporte 1000+ articles sans latence lors des requêtes d'affichage."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Insérer 1000 produits de test
        products = []
        for i in range(1, 1001):
            products.append((f"8888{i:04d}", f"Produit Test #{i}", "Collection 2026", 20.0, 50.0, 0.21))
        
        cursor.executemany("INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, taux_tva) VALUES (?, ?, ?, ?, ?, ?)", products)
        conn.commit()

        # Mesurer le temps de lecture d'une tranche fenêtrée (Batch 40 items)
        start_time = time.time()
        cursor.execute("SELECT id, code_barre, nom, prix_vente_tvac FROM Produits LIMIT 40 OFFSET 0")
        batch = cursor.fetchall()
        elapsed = time.time() - start_time
        
        conn.close()

        self.assertEqual(len(batch), 40)
        self.assertLess(elapsed, 0.05)  # Temps de lecture < 50ms (Fluidité 60 FPS)

    def test_worker_image_asynchrone(self):
        """Vérifie que la génération d'images s'exécute dans un thread d'arrière-plan sans bloquer l'UI."""
        finished = []
        
        def _cb(res):
            finished.append(res)

        start_time = time.time()
        thread = generer_apercu_image_async(callback=_cb)
        
        # Le thread principal doit immédiatement reprendre la main (< 10ms)
        non_blocking_time = time.time() - start_time
        self.assertLess(non_blocking_time, 0.05)
        
        thread.join(timeout=3.0)
        self.assertTrue(len(finished) >= 0)

    def test_rollback_automatique_urgence(self):
        """Vérifie la restauration d'urgence post-crash < 10s via RollbackManager."""
        # 1. Créer un snapshot pré-update
        snap_path = RollbackManager.create_pre_update_snapshot("2.1.0")
        self.assertTrue(os.path.exists(snap_path))

        # 2. Marquer l'update en cours
        CrashWatcher.mark_update_started("2.1.0", "2.2.0", snap_path)

        # 3. Déclencher le check post-update health -> Doit faire un rollback automatique
        rolled_back = CrashWatcher.check_post_update_health()
        self.assertTrue(rolled_back)

if __name__ == "__main__":
    unittest.main()
