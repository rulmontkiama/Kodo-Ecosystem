import os
import time
import json
import threading
from core.config import ShopConfig

class CrashWatcher:
    """Surveille la santé applicative durant la fenêtre critique de 30 secondes post-update."""
    
    @classmethod
    def get_marker_path(cls) -> str:
        return os.path.join(ShopConfig.get_base_data_dir(), ".update_in_progress.json")

    @classmethod
    def mark_update_started(cls, from_version: str, to_version: str, snapshot_path: str):
        """Dépose un marqueur indiquant qu'une mise à jour a eu lieu et nécessite 30s de stabilité."""
        data = {
            "from_version": from_version,
            "to_version": to_version,
            "snapshot_path": snapshot_path,
            "timestamp": time.time(),
            "status": "pending"
        }
        with open(cls.get_marker_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def get_pending_update_info(cls) -> dict:
        """Retourne les informations si un update non validé/planté a eu lieu."""
        marker = cls.get_marker_path()
        if os.path.exists(marker):
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("status") == "pending":
                        # Générer un bundle de diagnostic anonymisé pour l'Agent SRE
                        try:
                            from core.telemetry import TelemetryEngine
                            TelemetryEngine.generate_diagnostic_bundle(
                                error_type="CrashWatcher_PendingUpdateFailure",
                                raw_stacktrace=f"Crash post-update détecté. Version source: {data.get('from_version')}, Cible: {data.get('to_version')}",
                                extra_context=data
                            )
                        except Exception:
                            pass
                        return data
            except Exception:
                pass
        return None

    @classmethod
    def check_post_update_health(cls) -> bool:
        """
        Vérifie au démarrage si une mise à jour récente a échoué (< 10s healthcheck).
        Déclenche la restauration d'urgence via RollbackManager.
        """
        update_info = cls.get_pending_update_info()
        if update_info:
            print("[CRASH WATCHER] ⚠️ Échec du healthcheck post-mise à jour détecté !")
            from core.rollback_manager import RollbackManager
            snap_path = update_info.get("snapshot_path")
            return RollbackManager.execute_emergency_rollback(snap_path)
        return False

    @classmethod
    def mark_update_successful(cls):
        """Efface le marqueur après validation des 30 secondes de stabilité."""
        marker = cls.get_marker_path()
        if os.path.exists(marker):
            try:
                os.remove(marker)
            except Exception:
                pass

    @classmethod
    def start_stability_timer(cls, duration_seconds: int = 30):
        """Lance un thread d'arrière-plan pour valider la stabilité après N secondes."""
        def _wait_and_validate():
            time.sleep(duration_seconds)
            cls.mark_update_successful()

        t = threading.Thread(target=_wait_and_validate, daemon=True)
        t.start()

    @classmethod
    def get_session_basket_path(cls) -> str:
        return os.path.join(ShopConfig.get_base_data_dir(), "panier_session.json")

    @classmethod
    def get_unfinalized_basket(cls) -> dict:
        """Détecte s'il existe un panier de session non finalisé suite à un crash ou arrêt brutal."""
        path = cls.get_session_basket_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data and isinstance(data, dict) and data.get("panier"):
                    return data
            except Exception:
                pass
        return None

    @classmethod
    def clear_unfinalized_basket(cls):
        """Purge le marqueur de session de panier non finalisé."""
        path = cls.get_session_basket_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
