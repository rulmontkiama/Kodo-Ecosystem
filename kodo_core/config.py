"""
kodo_core.config - Configuration centralisée et multiplateforme Kōdo POS.
Gère les chemins de données (macOS / Windows / Linux), les variables d'environnement et les ports réseaux.
"""

import os
import sys

class ShopProfile:
    PRET_A_PORTER = "pret_a_porter"
    EPICERIE = "epicerie"
    GENERAL = "general"
    SALON = "salon"

class ShopConfig:
    """Configuration centralisée, dynamique et multiplateforme de l'application Kōdo POS."""
    
    # Profil métier actif par défaut
    PROFIL_METIER = ShopProfile.PRET_A_PORTER
    
    # Information & Branding par défaut
    NOM_MAGASIN_DEFAULT = "L'Adresse B"
    DEVISE_DEFAULT = "€"
    TAUX_TVA_DEFAULT = 0.21
    
    # Paramètres réseau & serveur
    DEFAULT_PORT = 8765
    DEFAULT_HOST = "0.0.0.0"

    @classmethod
    def get_env(cls, key: str, default: str = "") -> str:
        """Récupère une variable d'environnement avec fallback."""
        return os.environ.get(key, default)

    @classmethod
    def get_port(cls) -> int:
        """Retourne le port configuré pour le serveur POS REST API."""
        port_env = os.environ.get("KODO_PORT")
        if port_env and port_env.isdigit():
            return int(port_env)
        return cls.DEFAULT_PORT

    @classmethod
    def get_host(cls) -> str:
        """Retourne l'hôte configuré pour le serveur POS."""
        return os.environ.get("KODO_HOST", cls.DEFAULT_HOST)

    @classmethod
    def get_secret_key(cls) -> str:
        """Retourne la clé secrète de l'application."""
        return os.environ.get("KODO_SECRET_KEY", "KODO_POS_SECURE_SECRET_KEY_2026_NF525")

    @classmethod
    def get_salt(cls) -> str:
        """Retourne le sel cryptographique pour le hachage des PINs."""
        return os.environ.get("KODO_SALT", "KODO_POS_SECURE_SALT_2026")

    @staticmethod
    def get_base_data_dir() -> str:
        """
        Retourne le chemin racine du dossier de données utilisateur avec support multiplateforme
        (Windows %APPDATA%, macOS ~/Documents/Kodo_POS ou Application Support, Linux ~/.kodo_pos_data).
        """
        if sys.platform == "win32":
            app_data = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            doc_dir = os.path.join(app_data, "Kodo_POS")
        elif sys.platform == "darwin":
            doc_dir = os.path.expanduser("~/Documents/Kodo_POS")
        else:
            doc_dir = os.path.expanduser("~/.kodo_pos_data")

        # Vérification du droit d'écriture et création
        try:
            os.makedirs(doc_dir, exist_ok=True)
            test_file = os.path.join(doc_dir, ".perm_check")
            with open(test_file, "a") as f:
                pass
            if os.path.exists(test_file):
                try: os.remove(test_file)
                except Exception: pass
            return doc_dir
        except Exception:
            try:
                if sys.platform == "win32":
                    fallback_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Kodo_POS")
                elif sys.platform == "darwin":
                    fallback_dir = os.path.expanduser("~/Library/Application Support/Kodo_POS")
                else:
                    fallback_dir = os.path.expanduser("~/.kodo_pos_data")
                os.makedirs(fallback_dir, exist_ok=True)
                return fallback_dir
            except Exception:
                fallback_dir = os.path.join(os.path.abspath("."), "data", "Kodo_POS")
                os.makedirs(fallback_dir, exist_ok=True)
                return fallback_dir

    @classmethod
    def get_db_dir(cls) -> str:
        """Chemin dédié à la base de données de production SQLite."""
        path = os.path.join(cls.get_base_data_dir(), "db")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_snapshots_dir(cls) -> str:
        """Chemin dédié aux sauvegardes physiques automatiques pré-migration."""
        path = os.path.join(cls.get_base_data_dir(), "snapshots")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_backups_dir(cls) -> str:
        """Chemin dédié aux fichiers d'export et paquets de migration .kodo."""
        path = os.path.join(cls.get_base_data_dir(), "backups")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_logs_dir(cls) -> str:
        """Chemin dédié aux journaux d'erreurs, de télémétrie et d'audit."""
        path = os.path.join(cls.get_base_data_dir(), "logs")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_exports_dir(cls) -> str:
        """Chemin dédié aux rapports d'exportation (CSV/Excel/PDF)."""
        path = os.path.join(cls.get_base_data_dir(), "exports")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_tickets_dir(cls) -> str:
        """Chemin dédié aux archives de tickets et factures imprimables."""
        path = os.path.join(cls.get_base_data_dir(), "tickets")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_temp_dir(cls) -> str:
        """Chemin dédié aux fichiers temporaires d'import/export."""
        path = os.path.join(cls.get_base_data_dir(), "temp")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_db_path(cls, db_name: str = "kodo_pos.db") -> str:
        """Retourne le chemin complet vers la base de données SQLite locale."""
        override = os.environ.get("KODO_DB_PATH")
        if override:
            return override
        return os.path.join(cls.get_db_dir(), db_name)

    @classmethod
    def get_firebase_credentials_path(cls) -> str:
        """Charge le chemin du fichier d'identifiants Firebase Admin SDK."""
        env_path = os.environ.get("KODO_FIREBASE_CREDENTIALS")
        if env_path and os.path.exists(env_path):
            return env_path
        
        base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
        local_secret = os.path.join(base_path, "kodo-pos-firebase-adminsdk-fbsvc-c56ff45f8c.json")
        if os.path.exists(local_secret):
            return local_secret
        return ""
