import unittest
import os
import sys
import tempfile
import sqlite3
import shutil
import zipfile

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import ShopConfig
from core.updater import AppUpdateEngine, UpdateError
from core.crash_watcher import CrashWatcher
from core.migrations import MigrationManager

class TestUpdaterRollback(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        
        # Rediriger les sous-dossiers vers temp_dir
        ShopConfig.get_base_data_dir = lambda: self.temp_dir
        ShopConfig.get_db_dir = lambda: os.path.join(self.temp_dir, "db")
        ShopConfig.get_snapshots_dir = lambda: os.path.join(self.temp_dir, "snapshots")
        ShopConfig.get_logs_dir = lambda: os.path.join(self.temp_dir, "logs")
        
        os.makedirs(ShopConfig.get_db_dir(), exist_ok=True)
        os.makedirs(ShopConfig.get_snapshots_dir(), exist_ok=True)
        os.makedirs(ShopConfig.get_logs_dir(), exist_ok=True)

        self.db_path = ShopConfig.get_db_path()
        MigrationManager.run_migrations(self.db_path)

        # Créer v1.0.0 simulée
        self.releases_dir = AppUpdateEngine.get_releases_dir()
        self.v1_dir = os.path.join(self.releases_dir, "v1.0.0")
        os.makedirs(self.v1_dir, exist_ok=True)
        with open(os.path.join(self.v1_dir, "main.py"), "w") as f:
            f.write("print('v1.0.0')")
        
        # Configurer symlink 'current' vers v1.0.0
        AppUpdateEngine.switch_symlink_atomic(self.v1_dir)

        # Créer un fichier de zip v1.1.0 factice pour update
        self.zip_path = os.path.join(self.temp_dir, "v1.1.0.zip")
        with zipfile.ZipFile(self.zip_path, 'w') as z:
            z.writestr("main.py", "print('v1.1.0')")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_update_checksum_failure_rollback(self):
        """Vérifie que l'échec de checksum annule la mise à jour sans toucher au symlink."""
        bad_checksum = "0000000000000000000000000000000000000000000000000000000000000000"
        
        with self.assertRaises(UpdateError):
            AppUpdateEngine.execute_update_pipeline(
                from_version="1.0.0",
                to_version="1.1.0",
                zip_path=self.zip_path,
                expected_sha256=bad_checksum
            )

        # Le symlink doit toujours pointer vers v1.0.0
        active_target = os.readlink(AppUpdateEngine.get_current_symlink())
        self.assertEqual(active_target, self.v1_dir)

        # Rapport d'échec présent dans logs
        log_content = open(os.path.join(ShopConfig.get_logs_dir(), "update_failures.log")).read()
        self.assertIn("Checksum SHA256 invalide", log_content)

    def test_update_successful(self):
        """Vérifie l'exécution sans faute du pipeline avec bascule du symlink."""
        correct_checksum = AppUpdateEngine.calculate_sha256(self.zip_path)
        
        res = AppUpdateEngine.execute_update_pipeline(
            from_version="1.0.0",
            to_version="1.1.0",
            zip_path=self.zip_path,
            expected_sha256=correct_checksum
        )
        self.assertTrue(res)

        # Le symlink doit désormais pointer vers v1.1.0
        v1_1_dir = os.path.join(self.releases_dir, "v1.1.0")
        active_target = os.readlink(AppUpdateEngine.get_current_symlink())
        self.assertEqual(active_target, v1_1_dir)

    def test_crash_watcher_rollback(self):
        """Simule un crash dans les 30s post-update et vérifie le rollback automatique."""
        # 1. Enregistrer un update "pending"
        snapshot_file = os.path.join(ShopConfig.get_snapshots_dir(), "snap_v1.db")
        shutil.copy2(self.db_path, snapshot_file)
        
        CrashWatcher.mark_update_started("1.0.0", "1.1.0", snapshot_file)
        
        # 2. Déclencher la détection de crash applicatif
        with self.assertRaises(UpdateError):
            AppUpdateEngine.check_and_recover_from_crash()

        # 3. Le symlink doit être revenu sur v1.0.0
        active_target = os.readlink(AppUpdateEngine.get_current_symlink())
        self.assertEqual(active_target, self.v1_dir)

if __name__ == "__main__":
    unittest.main()
