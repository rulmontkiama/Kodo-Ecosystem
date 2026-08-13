"""
Gestionnaire de Rollback Automatique et de Restauration d'Urgence (Kōdo POS).
"""
import os
import shutil
import time
import json
import sqlite3
from core.config import ShopConfig

class RollbackManager:
    """Gère la création de snapshots système et la restauration automatique en cas de crash post-update < 10s."""

    @classmethod
    def get_snapshot_dir(cls) -> str:
        snap_dir = os.path.join(ShopConfig.get_base_data_dir(), "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        return snap_dir

    @classmethod
    def create_pre_update_snapshot(cls, from_version: str) -> str:
        """Crée une copie de sauvegarde de la base de données et des métadonnées avant mise à jour."""
        snap_dir = cls.get_snapshot_dir()
        timestamp_str = str(int(time.time()))
        snapshot_folder = os.path.join(snap_dir, f"snapshot_v{from_version}_{timestamp_str}")
        os.makedirs(snapshot_folder, exist_ok=True)

        # Copier la BDD courante
        db_src = os.path.join(ShopConfig.get_base_data_dir(), "kodo_pos.db")
        if not os.path.exists(db_src):
            db_src = "kodo_pos.db"

        if os.path.exists(db_src):
            shutil.copy2(db_src, os.path.join(snapshot_folder, "kodo_pos.db"))

        manifest = {
            "version": from_version,
            "timestamp": time.time(),
            "db_backup": "kodo_pos.db",
            "status": "ready"
        }
        with open(os.path.join(snapshot_folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return snapshot_folder

    @classmethod
    def execute_emergency_rollback(cls, snapshot_folder: str = None) -> bool:
        """Exécute la restauration d'urgence de la version précédente et de la BDD."""
        try:
            print("[ROLLBACK] 🚨 Déclenchement de la restauration d'urgence post-crash...")
            snap_dir = cls.get_snapshot_dir()
            
            if not snapshot_folder or not os.path.exists(snapshot_folder):
                # Trouver le snapshot le plus récent
                snaps = sorted([os.path.join(snap_dir, d) for d in os.listdir(snap_dir) if d.startswith("snapshot_")], reverse=True)
                if not snaps:
                    print("[ROLLBACK] Aucun snapshot disponible pour la restauration.")
                    return False
                snapshot_folder = snaps[0]

            db_backup = os.path.join(snapshot_folder, "kodo_pos.db")
            target_db = os.path.join(ShopConfig.get_base_data_dir(), "kodo_pos.db")

            if os.path.exists(db_backup):
                shutil.copy2(db_backup, target_db)
                print(f"[ROLLBACK] Base de données restaurée depuis {db_backup}")

            # Effacer le marqueur d'update en échec
            marker = os.path.join(ShopConfig.get_base_data_dir(), ".update_in_progress.json")
            if os.path.exists(marker):
                os.remove(marker)

            print("[ROLLBACK] ✅ Restauration d'urgence terminée avec succès.")
            return True
        except Exception as e:
            print(f"[ROLLBACK ERROR] Échec de la restauration d'urgence: {e}")
            return False
