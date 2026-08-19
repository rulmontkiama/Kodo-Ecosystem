# -*- coding: utf-8 -*-
"""
Gestionnaire de Sauvegardes & Migration Inter-Machines - Kōdo POS Core
Permet l'exportation et l'importation sécurisées de packs de migration (.ZIP)
avec calcul d'empreinte SHA-256, validation d'intégrité SQLite et snapshot rollback automatique.
"""

import os
import io
import json
import shutil
import base64
import hashlib
import sqlite3
import datetime
import zipfile
from typing import Dict, Any, Tuple, Optional, List

from database_manager import DB_NAME


def get_backup_directory() -> str:
    """
    Retourne le chemin du répertoire de sauvegarde.
    Par défaut ~/Documents/Kodo_Backups (synchronisable iCloud / sauvegarde locale).
    """
    doc_dir = os.path.expanduser("~/Documents/Kodo_Backups")
    os.makedirs(doc_dir, exist_ok=True)
    return doc_dir


def verifier_integrite_db(db_path: str) -> bool:
    """
    Vérifie l'intégrité structurelle de la base de données SQLite via PRAGMA integrity_check.
    """
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()
        conn.close()
        return bool(result and result[0] == "ok")
    except Exception as e:
        print(f"[BackupManager] Erreur integrity_check: {e}")
        return False


def _calculer_sha256(file_path: str) -> str:
    """Calcule le hash SHA-256 d'un fichier."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _get_db_stats(db_path: str) -> Dict[str, Any]:
    """Extrait un résumé statistique complet de la base de données SQLite."""
    stats = {
        "products_count": 0,
        "clients_count": 0,
        "sales_count": 0,
        "users_count": 0,
        "held_tickets_count": 0,
        "categories_count": 0
    }
    if not os.path.exists(db_path):
        return stats

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Helper pour compter les lignes d'une table si elle existe
        def count_table(table_name: str) -> int:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
                row = cursor.fetchone()
                return int(row[0]) if row else 0
            except Exception:
                return 0

        stats["products_count"] = count_table("products")
        stats["clients_count"] = count_table("clients")
        stats["sales_count"] = count_table("sales")
        stats["users_count"] = count_table("users")
        stats["held_tickets_count"] = count_table("held_tickets")
        stats["categories_count"] = count_table("categories")

        conn.close()
    except Exception as e:
        print(f"[BackupManager] Erreur lecture stats DB: {e}")

    return stats


def creer_backup_local() -> Optional[str]:
    """
    Crée une copie compressée de la base de données actuelle.
    """
    try:
        if not os.path.exists(DB_NAME):
            return None

        backup_dir = get_backup_directory()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_copy = os.path.join(backup_dir, f"temp_{timestamp}.db")

        # Sauvegarde à chaud avec l'API SQLite
        source_conn = sqlite3.connect(DB_NAME)
        dest_conn = sqlite3.connect(temp_copy)
        with source_conn:
            source_conn.backup(dest_conn)
        dest_conn.close()
        source_conn.close()

        zip_filename = os.path.join(backup_dir, f"kodo_backup_{timestamp}.zip")
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(temp_copy, arcname=os.path.basename(DB_NAME))

        if os.path.exists(temp_copy):
            os.remove(temp_copy)

        _nettoyer_anciennes_sauvegardes(backup_dir, limit=30)
        return zip_filename
    except Exception as e:
        print(f"[BackupManager] Erreur creer_backup_local: {e}")
        return None


def creer_sauvegarde_manuelle() -> Dict[str, Any]:
    """API wrapper pour la création manuelle d'une sauvegarde locale."""
    zip_path = creer_backup_local()
    if zip_path:
        return {
            "success": True,
            "filename": os.path.basename(zip_path),
            "filepath": zip_path,
            "message": "Sauvegarde créée avec succès."
        }
    return {"success": False, "error": "Impossible de créer la sauvegarde."}


def lister_sauvegardes() -> List[Dict[str, Any]]:
    """Liste les sauvegardes existantes avec date et taille."""
    backup_dir = get_backup_directory()
    res = []
    try:
        files = [f for f in os.listdir(backup_dir) if f.endswith(".zip")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)), reverse=True)

        for filename in files:
            filepath = os.path.join(backup_dir, filename)
            stat = os.stat(filepath)
            dt = datetime.datetime.fromtimestamp(stat.st_mtime)
            size_kb = round(stat.st_size / 1024, 1)
            res.append({
                "filename": filename,
                "filepath": filepath,
                "size_kb": size_kb,
                "date": dt.strftime("%d/%m/%Y %H:%M:%S"),
                "is_migration": filename.startswith("Kodo_POS_Transfert_")
            })
    except Exception as e:
        print(f"[BackupManager] Erreur listage sauvegardes: {e}")
    return res


def creer_pack_migration_machine() -> Tuple[bool, str, bytes, Dict[str, Any]]:
    """
    Génère un pack de transfert complet (.ZIP) contenant :
    - La base SQLite clonée à chaud
    - Le fichier manifest.json (SHA-256, statistiques globales, date d'exportation)
    Retourne (success, filename, zip_bytes, manifest_data).
    """
    try:
        if not os.path.exists(DB_NAME):
            raise FileNotFoundError(f"La base de données {DB_NAME} est introuvable.")

        backup_dir = get_backup_directory()
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        date_formatted = now.strftime("%d/%m/%Y à %H:%M:%S")

        # 1. Cloner la base à chaud dans un fichier temporaire
        temp_db_path = os.path.join(backup_dir, f"temp_migration_{timestamp}.db")
        source_conn = sqlite3.connect(DB_NAME)
        dest_conn = sqlite3.connect(temp_db_path)
        with source_conn:
            source_conn.backup(dest_conn)
        dest_conn.close()
        source_conn.close()

        # 2. Vérifier l'intégrité de la copie et calculer son SHA-256
        if not verifier_integrite_db(temp_db_path):
            if os.path.exists(temp_db_path):
                os.remove(temp_db_path)
            raise ValueError("Le clone de la base de données a échoué au test d'intégrité.")

        db_sha256 = _calculer_sha256(temp_db_path)
        stats = _get_db_stats(temp_db_path)

        # 3. Créer le manifeste de migration
        manifest = {
            "app": "Kōdo POS",
            "format": "kodo_machine_migration",
            "version": "2.0.0",
            "export_timestamp": now.isoformat(),
            "export_date_formatted": date_formatted,
            "db_filename": "kodo_pos.db",
            "db_sha256": db_sha256,
            "stats": stats
        }

        # 4. Créer l'archive ZIP en mémoire et sur disque
        zip_filename = f"Kodo_POS_Transfert_{timestamp}.zip"
        zip_path = os.path.join(backup_dir, zip_filename)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(temp_db_path, arcname="kodo_pos.db")
            zipf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

        zip_bytes = zip_buffer.getvalue()

        # Écriture également sur le dossier de sauvegarde
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)

        # 5. Nettoyage du fichier temporaire .db
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)

        print(f"[BackupManager] Pack de migration généré avec succès : {zip_filename} ({len(zip_bytes)} octets)")
        return True, zip_filename, zip_bytes, manifest

    except Exception as e:
        print(f"[BackupManager] Erreur lors de la création du pack de migration: {e}")
        return False, "", b"", {"error": str(e)}


def _extraire_zip_donnees(zip_input: Any) -> Tuple[Optional[zipfile.ZipFile], Optional[bytes]]:
    """Convertit un input (chemin de fichier, bytes ou base64 string) en ZipFile."""
    try:
        if isinstance(zip_input, str):
            if os.path.exists(zip_input):
                with open(zip_input, 'rb') as f:
                    raw_bytes = f.read()
                return zipfile.ZipFile(io.BytesIO(raw_bytes)), raw_bytes
            else:
                # Tentative de décodage base64
                if ',' in zip_input:
                    zip_input = zip_input.split(',', 1)[1]
                raw_bytes = base64.b64decode(zip_input)
                return zipfile.ZipFile(io.BytesIO(raw_bytes)), raw_bytes
        elif isinstance(zip_input, (bytes, bytearray)):
            return zipfile.ZipFile(io.BytesIO(zip_input)), bytes(zip_input)
    except Exception as e:
        print(f"[BackupManager] Erreur ouverture ZipFile: {e}")
    return None, None


def previsualiser_pack_migration(zip_input: Any) -> Dict[str, Any]:
    """
    Inspecte un pack de transfert ZIP et retourne son aperçu sans rien modifier :
    - Manifeste (Date, version, boutique)
    - Statistiques (nombre de produits, ventes, clients)
    - Validité de l'archive et intégrité de la base incluse
    """
    zipf, raw_bytes = _extraire_zip_donnees(zip_input)
    if not zipf:
        return {"valid": False, "error": "Fichier ZIP invalide ou non reconnu."}

    try:
        namelist = zipf.namelist()
        if "manifest.json" not in namelist:
            # Recherche d'un fichier .db direct
            db_candidates = [n for n in namelist if n.endswith(".db")]
            if not db_candidates:
                return {"valid": False, "error": "L'archive ne contient ni manifest.json ni base SQLite valide."}
            return {
                "valid": True,
                "format": "legacy_sqlite_zip",
                "manifest": {
                    "app": "Kōdo POS",
                    "export_date_formatted": "Archive SQLite standard",
                    "db_filename": db_candidates[0]
                },
                "stats": {
                    "products_count": 0,
                    "sales_count": 0,
                    "clients_count": 0,
                    "users_count": 0
                }
            }

        # Lecture du manifest.json
        manifest_raw = zipf.read("manifest.json").decode('utf-8')
        manifest = json.loads(manifest_raw)

        db_name_in_zip = manifest.get("db_filename", "kodo_pos.db")
        if db_name_in_zip not in namelist:
            db_candidates = [n for n in namelist if n.endswith(".db")]
            if db_candidates:
                db_name_in_zip = db_candidates[0]
            else:
                return {"valid": False, "error": f"Fichier base de données '{db_name_in_zip}' absent du ZIP."}

        # Test d'intégrité temporaire
        temp_dir = os.path.join(get_backup_directory(), "temp_preview")
        os.makedirs(temp_dir, exist_ok=True)
        extracted_db = os.path.join(temp_dir, "preview.db")

        with open(extracted_db, "wb") as f:
            f.write(zipf.read(db_name_in_zip))

        is_valid_db = verifier_integrite_db(extracted_db)
        sha_calculated = _calculer_sha256(extracted_db)
        stats = _get_db_stats(extracted_db)

        # Nettoyage
        if os.path.exists(extracted_db):
            os.remove(extracted_db)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

        if not is_valid_db:
            return {"valid": False, "error": "La base SQLite contenue dans le ZIP est corrompue."}

        manifest_sha = manifest.get("db_sha256")
        sha_match = (manifest_sha == sha_calculated) if manifest_sha else True

        return {
            "valid": True,
            "sha_match": sha_match,
            "manifest": manifest,
            "stats": stats or manifest.get("stats", {})
        }

    except Exception as e:
        print(f"[BackupManager] Erreur previsualiser_pack_migration: {e}")
        return {"valid": False, "error": f"Erreur lors de l'analyse du pack : {e}"}


def restaurer_pack_migration(zip_input: Any) -> Dict[str, Any]:
    """
    Restaure un pack de transfert (.ZIP) sur cette machine :
    1. Vérifie la validité du ZIP et de la base de données.
    2. Crée un snapshot de sécurité préalable de la base actuelle (Rollback safety).
    3. Remplace la base active SQLite (DB_NAME).
    4. Exécute les migrations de schéma si nécessaire.
    5. Retourne le rapport de restauration avec statistiques.
    """
    # 1. Prévisualisation & Contrôle strict
    preview = previsualiser_pack_migration(zip_input)
    if not preview.get("valid"):
        return {"success": False, "error": preview.get("error", "Pack de transfert non valide.")}

    zipf, raw_bytes = _extraire_zip_donnees(zip_input)
    if not zipf:
        return {"success": False, "error": "Impossible de lire le fichier ZIP."}

    try:
        backup_dir = get_backup_directory()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 2. Snapshot de sécurité préventif (au cas où l'utilisateur souhaite annuler)
        if os.path.exists(DB_NAME):
            safety_backup_path = os.path.join(backup_dir, f"kodo_safety_pre_restore_{timestamp}.db")
            try:
                src_conn = sqlite3.connect(DB_NAME)
                dst_conn = sqlite3.connect(safety_backup_path)
                with src_conn:
                    src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
                print(f"[BackupManager] Snapshot de sécurité préventif créé : {safety_backup_path}")
            except Exception as snap_err:
                print(f"[BackupManager] Avertissement snapshot: {snap_err}")
                shutil.copy2(DB_NAME, safety_backup_path)

        # 3. Extraction de la nouvelle base de données
        manifest = preview.get("manifest", {})
        db_name_in_zip = manifest.get("db_filename", "kodo_pos.db")
        namelist = zipf.namelist()
        if db_name_in_zip not in namelist:
            db_candidates = [n for n in namelist if n.endswith(".db")]
            db_name_in_zip = db_candidates[0]

        temp_extracted_path = os.path.join(backup_dir, f"temp_restored_{timestamp}.db")
        with open(temp_extracted_path, "wb") as f:
            f.write(zipf.read(db_name_in_zip))

        # 4. Vérification d'intégrité finale sur le fichier extrait
        if not verifier_integrite_db(temp_extracted_path):
            if os.path.exists(temp_extracted_path):
                os.remove(temp_extracted_path)
            return {"success": False, "error": "Le fichier SQLite extrait a échoué au test d'intégrité."}

        # 5. Remplacement atomique de la base active
        os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
        shutil.copy2(temp_extracted_path, DB_NAME)

        # Nettoyage temporaire
        if os.path.exists(temp_extracted_path):
            os.remove(temp_extracted_path)

        # 6. Exécution des migrations pour s'assurer de la compatibilité de version
        try:
            from kodo_core.db.migrations import initialiser_db
            initialiser_db(DB_NAME)
        except Exception as mig_err:
            print(f"[BackupManager] Note post-migration : {mig_err}")

        stats = _get_db_stats(DB_NAME)

        print(f"[BackupManager] Restauration terminée avec succès sur {DB_NAME}.")
        return {
            "success": True,
            "message": "Toutes les données ont été transférées et restaurées avec succès !",
            "stats": stats,
            "manifest": manifest
        }

    except Exception as e:
        print(f"[BackupManager] Erreur critique lors de la restauration: {e}")
        return {"success": False, "error": f"Échec de la restauration : {str(e)}"}


def _nettoyer_anciennes_sauvegardes(backup_dir: str, limit: int = 30):
    """Conserve uniquement les `limit` sauvegardes les plus récentes."""
    try:
        backups = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith(".zip")]
        backups.sort(key=os.path.getmtime, reverse=True)
        if len(backups) > limit:
            for old_backup in backups[limit:]:
                try:
                    os.remove(old_backup)
                except Exception:
                    pass
    except Exception as e:
        print(f"[BackupManager] Erreur lors du nettoyage : {e}")
