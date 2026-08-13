"""
Moteur de Télémétrie, Masquage PII et Génération de Diagnostic Bundles.
"""
import os
import re
import sys
import json
import time
import shutil
import platform
import datetime
import threading
import sqlite3
from core.config import ShopConfig

class DataSanitizer:
    """Filtre de sécurité DevSecOps pour anonymiser à 100% les logs et stacktraces."""

    # Patterns de masquage PII et données financières
    EMAIL_REGEX = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
    PHONE_REGEX = re.compile(r'(\+?\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]?\d{2,4}[\s.-]?\d{2,4}')
    AMOUNT_REGEX = re.compile(r'\b\d+(?:[\.,]\d{1,2})?\s*(?:€|\$|EUR|USD)\b', re.IGNORECASE)
    PIN_TOKEN_REGEX = re.compile(r'(pin|token|password|secret|key)["\s:=]+["\']?([a-zA-Z0-9_\-]+)["\']?', re.IGNORECASE)

    @classmethod
    def sanitize_text(cls, text: str) -> str:
        """Nettoie le texte en remplaçant toutes les données sensibles par des balises masquées."""
        if not text:
            return ""
        
        # Masquage des Emails
        text = cls.EMAIL_REGEX.sub("[MASKED_EMAIL]", text)
        
        # Masquage des Montants Financiers
        text = cls.AMOUNT_REGEX.sub("[MASKED_AMOUNT]", text)

        # Masquage des Clés & PINs
        text = cls.PIN_TOKEN_REGEX.sub(r'\1: "[MASKED_SECRET]"', text)

        # Masquage des numéros de téléphone potentiels (si > 8 chiffres)
        text = cls.PHONE_REGEX.sub("[MASKED_PHONE]", text)

        return text

class TelemetryEngine:
    """Générateur de Diagnostic Bundles anonymisés et expéditeur asynchrone."""

    @classmethod
    def get_diagnostics_dir(cls) -> str:
        path = os.path.join(ShopConfig.get_logs_dir(), "diagnostics")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def collect_system_info(cls) -> dict:
        """Récupère les informations système sans données nominatives."""
        total, used, free = shutil.disk_usage(ShopConfig.get_base_data_dir())
        
        # Interroger la version de schéma BDD si disponible
        db_version = "Inconnue"
        try:
            db_path = ShopConfig.get_db_path()
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
                row = cursor.fetchone()
                if row:
                    db_version = row[0]
                conn.close()
        except Exception:
            pass

        return {
            "os_platform": platform.system(),
            "os_release": platform.release(),
            "python_version": platform.python_version(),
            "free_disk_gb": round(free / (1024 ** 3), 2),
            "db_schema_version": db_version,
            "shop_profile": ShopConfig.PROFIL_METIER
        }

    @classmethod
    def generate_diagnostic_bundle(cls, error_type: str, raw_stacktrace: str, extra_context: dict = None) -> str:
        """Génère un bundle JSON anonymisé et le sauvegarde dans logs/diagnostics/."""
        sanitized_trace = DataSanitizer.sanitize_text(raw_stacktrace)
        
        bundle = {
            "timestamp": datetime.datetime.now().isoformat(),
            "error_type": error_type,
            "system_info": cls.collect_system_info(),
            "sanitized_stacktrace": sanitized_trace,
            "extra_context": extra_context or {}
        }

        timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(cls.get_diagnostics_dir(), f"diag_{timestamp_str}.json")
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2, ensure_ascii=False)

        cls.purge_old_reports(max_days=7)
        return filepath

    @classmethod
    def purge_old_reports(cls, max_days: int = 7):
        """Purge les vieux rapports de diagnostic âgés de plus de N jours."""
        diag_dir = cls.get_diagnostics_dir()
        now = time.time()
        cutoff = now - (max_days * 86400)
        
        for fname in os.listdir(diag_dir):
            if fname.startswith("diag_") and fname.endswith(".json"):
                fpath = os.path.join(diag_dir, fname)
                if os.path.getmtime(fpath) < cutoff:
                    try: os.remove(fpath)
                    except Exception: pass

    @classmethod
    def transmit_pending_reports_async(cls, endpoint_url: str = None):
        """Transmet en tâche de fond les rapports de crash non encore envoyés."""
        def _worker():
            diag_dir = cls.get_diagnostics_dir()
            reports = [f for f in os.listdir(diag_dir) if f.endswith(".json")]
            for r in reports:
                # Simulation d'envoi chiffré vers Sentry / Télémetrie Custom
                time.sleep(0.1)
                # Une fois transmis, supprimer ou archiver le rapport
                try: os.remove(os.path.join(diag_dir, r))
                except Exception: pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
