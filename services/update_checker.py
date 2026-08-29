import webbrowser
import threading

from kodo_core.services.updater import (
    CURRENT_VERSION,
    UPDATE_ENDPOINTS as VERSION_URLS,
    BROWSER_USER_AGENT,
    DEFAULT_HEADERS as HEADERS,
    parse_version,
    get_installed_version,
    check_for_updates_sync,
    get_target_dist_dir,
    apply_remote_update_sync,
)


def open_download_page(url: str = None):
    """Ouvre la page de téléchargement ou GitHub release dans le navigateur par défaut."""
    target_url = url or "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.44.zip"
    try:
        webbrowser.open(target_url)
    except Exception as e:
        print(f"[UPDATE] Erreur ouverture URL de téléchargement : {e}")


def check_for_updates_async(callback=None):
    """Exécute la vérification de mise à jour dans un thread asynchrone non-bloquant."""
    def _worker():
        try:
            res = check_for_updates_sync()
            if callback:
                callback(res)
        except Exception as e:
            if callback:
                callback({"has_update": False, "error": str(e)})

    threading.Thread(target=_worker, daemon=True).start()


__all__ = [
    "CURRENT_VERSION",
    "VERSION_URLS",
    "BROWSER_USER_AGENT",
    "HEADERS",
    "parse_version",
    "get_installed_version",
    "check_for_updates_sync",
    "check_for_updates_async",
    "open_download_page",
    "get_target_dist_dir",
    "apply_remote_update_sync",
]
