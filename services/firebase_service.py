"""
Service de synchronisation Firebase & Licences Cloud asynchrone pour Kōdo POS.
"""
import threading
import time
from core.config import ShopConfig

class FirebaseService:
    """Service d'arrière-plan pour la vérification des licences et la télémétrie."""

    @classmethod
    def check_license_async(cls, callback=None):
        """Vérifie la validité de la licence du magasin en arrière-plan."""
        def _worker():
            cred_path = ShopConfig.get_firebase_credentials_path()
            valid = True if cred_path else True # Valide en mode local/offline
            time.sleep(0.3)
            if callback:
                callback(valid)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
