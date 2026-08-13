import os
import sys
import shutil
import hashlib
import zipfile
import datetime
import sqlite3
from core.config import ShopConfig
from core.migrations import MigrationManager, MigrationError
from core.crash_watcher import CrashWatcher

class UpdateError(Exception):
    """Exception levée en cas d'erreur durant le processus de mise à jour."""
    pass

class AppUpdateEngine:
    """Moteur de mise à jour transactionnel avec permutation atomique par symlink et rollback automatique."""

    @classmethod
    def get_app_dir(cls) -> str:
        """Répertoire racine des releases applicatives."""
        app_dir = os.path.join(ShopConfig.get_base_data_dir(), "app")
        os.makedirs(os.path.join(app_dir, "releases"), exist_ok=True)
        return app_dir

    @classmethod
    def get_releases_dir(cls) -> str:
        return os.path.join(cls.get_app_dir(), "releases")

    @classmethod
    def get_current_symlink(cls) -> str:
        return os.path.join(cls.get_app_dir(), "current")

    @classmethod
    def calculate_sha256(cls, filepath: str) -> str:
        """Calcule le hash SHA256 d'un fichier."""
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()

    @classmethod
    def switch_symlink_atomic(cls, target_release_dir: str):
        """Permute le lien symbolique 'current' de façon atomique (compatible macOS/Unix)."""
        current_symlink = cls.get_current_symlink()
        tmp_symlink = current_symlink + "_tmp"

        if os.path.exists(tmp_symlink) or os.path.islink(tmp_symlink):
            try: os.remove(tmp_symlink)
            except Exception: pass

        os.symlink(target_release_dir, tmp_symlink)
        os.replace(tmp_symlink, current_symlink)

    @classmethod
    def log_failure(cls, from_version: str, to_version: str, error_msg: str):
        """Enregistre un rapport d'échec chiffré/détaillé dans logs/update_failures.log."""
        logs_dir = ShopConfig.get_logs_dir()
        log_file = os.path.join(logs_dir, "update_failures.log")
        timestamp = datetime.datetime.now().isoformat()
        
        entry = (
            f"==================================================\n"
            f"TIMESTAMP   : {timestamp}\n"
            f"FROM VERSION: {from_version}\n"
            f"TO VERSION  : {to_version}\n"
            f"ERROR       : {error_msg}\n"
            f"==================================================\n\n"
        )
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)

    @classmethod
    def trigger_rollback(cls, from_version: str, to_version: str, snapshot_path: str, reason: str):
        """Exécute la procédure de rollback automatique N-1."""
        cls.log_failure(from_version, to_version, reason)

        db_path = ShopConfig.get_db_path()
        if snapshot_path and os.path.exists(snapshot_path):
            try:
                shutil.copy2(snapshot_path, db_path)
            except Exception as e:
                cls.log_failure(from_version, to_version, f"Échec critique de restauration BDD: {e}")

        previous_release = os.path.join(cls.get_releases_dir(), f"v{from_version}")
        if os.path.exists(previous_release):
            cls.switch_symlink_atomic(previous_release)

        CrashWatcher.mark_update_successful()
        raise UpdateError(f"Mise à jour annulée. Rollback v{from_version} effectué. Raison : {reason}")

    @classmethod
    def cleanup_old_releases(cls, keep_last: int = 3):
        """Conserve uniquement les N dernières releases."""
        releases_dir = cls.get_releases_dir()
        releases = sorted([r for r in os.listdir(releases_dir) if r.startswith("v")])
        if len(releases) > keep_last:
            to_delete = releases[:-keep_last]
            for r in to_delete:
                path = os.path.join(releases_dir, r)
                if not os.path.islink(cls.get_current_symlink()) or os.readlink(cls.get_current_symlink()) != path:
                    shutil.rmtree(path, ignore_errors=True)

    @classmethod
    def execute_update_pipeline(cls, from_version: str, to_version: str, zip_path: str, expected_sha256: str = None) -> bool:
        """Exécute le pipeline complet de mise à jour en 5 étapes avec rollback garanti."""
        db_path = ShopConfig.get_db_path()
        target_release_dir = os.path.join(cls.get_releases_dir(), f"v{to_version}")
        snapshot_path = ""

        try:
            snapshot_path = MigrationManager.create_pre_migration_snapshot(db_path)

            if expected_sha256:
                actual_sha = cls.calculate_sha256(zip_path)
                if actual_sha.lower() != expected_sha256.lower():
                    raise UpdateError(f"Checksum SHA256 invalide ! Attendu: {expected_sha256}, Obtentu: {actual_sha}")

            os.makedirs(target_release_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(target_release_dir)

            MigrationManager.run_migrations(db_path)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA quick_check")
            res = cursor.fetchone()
            conn.close()
            if not res or res[0].lower() != "ok":
                raise UpdateError("Health Check BDD échoué post-migration.")

            CrashWatcher.mark_update_started(from_version, to_version, snapshot_path)

            cls.switch_symlink_atomic(target_release_dir)
            cls.cleanup_old_releases(keep_last=3)

            return True

        except Exception as e:
            cls.trigger_rollback(from_version, to_version, snapshot_path, str(e))
            return False

    @classmethod
    def check_and_recover_from_crash(cls):
        """Déclenche le rollback si l'application s'est arrêtée brutalement durant les 30s post-update."""
        pending = CrashWatcher.get_pending_update_info()
        if pending:
            cls.trigger_rollback(
                from_version=pending["from_version"],
                to_version=pending["to_version"],
                snapshot_path=pending.get("snapshot_path", ""),
                reason="Crash applicatif détecté durant la fenêtre de stabilité de 30 secondes."
            )
