import os
import sys

class ShopProfile:
    PRET_A_PORTER = "pret_a_porter"
    EPICERIE = "epicerie"
    GENERAL = "general"
    SALON = "salon"

class ShopConfig:
    """Configuration centralisée et dynamique de l'établissement Kōdo POS."""
    
    # Profil métier actif (Pilote: prêt-à-porter)
    PROFIL_METIER = ShopProfile.PRET_A_PORTER
    
    # Branding et Informations par défaut (Pilote L'Adresse B)
    NOM_MAGASIN_DEFAULT = "L'Adresse B"
    DEVISE_DEFAULT = "€"
    TAUX_TVA_DEFAULT = 0.21
    
    @staticmethod
    def get_base_data_dir() -> str:
        """Retourne le chemin racine du dossier de données utilisateur avec fallback local en cas de restriction de permissions."""
        try:
            doc_dir = os.path.expanduser("~/Documents/Kodo_POS")
            os.makedirs(doc_dir, exist_ok=True)
            test_file = os.path.join(doc_dir, ".perm_check")
            with open(test_file, "a") as f:
                pass
            return doc_dir
        except Exception:
            try:
                fallback_dir = os.path.expanduser("~/Library/Application Support/Kodo_POS")
                os.makedirs(fallback_dir, exist_ok=True)
                return fallback_dir
            except Exception:
                try:
                    bulletproof_dir = os.path.expanduser("~/.kodo_pos_data")
                    os.makedirs(bulletproof_dir, exist_ok=True)
                    return bulletproof_dir
                except Exception:
                    fallback_dir = os.path.join(os.path.abspath("."), "data")
                    os.makedirs(fallback_dir, exist_ok=True)
                    return fallback_dir
    @classmethod
    def get_db_dir(cls) -> str:
        """Chemin dédié à la base de données de production."""
        path = os.path.join(cls.get_base_data_dir(), "db")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_snapshots_dir(cls) -> str:
        """Chemin dédié aux sauvegardes automatiques pré-migration."""
        path = os.path.join(cls.get_base_data_dir(), "snapshots")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_logs_dir(cls) -> str:
        """Chemin dédié aux journaux d'erreurs et d'audit."""
        path = os.path.join(cls.get_base_data_dir(), "logs")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_exports_dir(cls) -> str:
        """Chemin dédié aux fichiers d'exportation (CSV/Excel/PDF)."""
        path = os.path.join(cls.get_base_data_dir(), "exports")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_tickets_dir(cls) -> str:
        """Chemin dédié aux archives de tickets et factures."""
        path = os.path.join(cls.get_base_data_dir(), "tickets")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def get_db_path(cls, db_name: str = "kodo_pos.db") -> str:
        """Retourne le chemin vers la base de données SQLite locale isolée."""
        return os.path.join(cls.get_db_dir(), db_name)

    @classmethod
    def get_firebase_credentials_path(cls) -> str:
        """Charge le chemin du fichier de credentials Firebase en toute sécurité."""
        env_path = os.environ.get("KODO_FIREBASE_CREDENTIALS")
        if env_path and os.path.exists(env_path):
            return env_path
        
        # Fallback local hors-git ou bundle
        base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
        local_secret = os.path.join(base_path, "kodo-pos-firebase-adminsdk-fbsvc-c56ff45f8c.json")
        if os.path.exists(local_secret):
            return local_secret
        return ""
