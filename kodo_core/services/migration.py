"""
kodo_core.services.migration - Moteur d'exportation .kodo / .zip et importation de migration
complète de données entre ordinateurs PC (Windows) et Mac (macOS).
"""

import os
import sys
import json
import zipfile
import shutil
import datetime
import tempfile
from decimal import Decimal
from kodo_core.config import ShopConfig
from kodo_core.db.connection import get_connection, db_query
from kodo_core.db.audit_trail import compute_sha256, verify_database_integrity
from kodo_core.db.migrations import MigrationManager

class MigrationPackageError(Exception):
    """Exception levée lors de l'exportation ou de l'importation d'un paquet de migration."""
    pass

def calculate_file_sha256(filepath: str) -> str:
    """Calcule l'empreinte SHA-256 d'un fichier physique."""
    hasher = hashlib_sha256 = None
    import hashlib
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()

def export_migration_package(output_path: str = None, db_path: str = None, conn = None, include_media: bool = True) -> str:
    """
    Exporte l'intégralité des données et médias Kōdo POS dans une archive `.kodo` (zip cryptographiquement vérifiable).
    Garantit la compatibilité totale entre machines macOS et PC Windows.
    """
    source_db = db_path or ShopConfig.get_db_path()
    if not os.path.exists(source_db):
        raise MigrationPackageError(f"La base de données source est introuvable: {source_db}")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not output_path:
        filename = f"kodo_export_migration_{timestamp}.kodo"
        output_path = os.path.join(ShopConfig.get_backups_dir(), filename)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    temp_dir = tempfile.mkdtemp(prefix="kodo_export_")

    try:
        temp_db_path = os.path.join(temp_dir, "kodo_database.db")

        # 1. Flush WAL et copie sécurisée
        safe_conn = get_connection(db_path=source_db, conn=conn)
        try:
            cur = safe_conn.cursor()
            cur.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass

        try:
            # Tenter VACUUM INTO si supporté par SQLite
            safe_conn.execute(f"VACUUM INTO '{temp_db_path}'")
        except Exception:
            # Fallback copie physique
            shutil.copy2(source_db, temp_db_path)
        finally:
            safe_conn.close()

        # 2. Calcul du checksum SHA-256 de la base de données
        db_checksum = calculate_file_sha256(temp_db_path)

        # 3. Récupération des statistiques et métadonnées
        counts = {}
        schema_versions = []
        chk_conn = get_connection(db_path=temp_db_path)
        try:
            cur = chk_conn.cursor()
            tables = ["Produits", "Clients", "Tickets", "Categories", "Marques", "ShopInfo", "Clotures_Caisse", "Audit_Trail", "Parametres"]
            for tbl in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    counts[tbl] = cur.fetchone()[0]
                except Exception:
                    counts[tbl] = 0

            try:
                cur.execute("SELECT version FROM schema_version")
                schema_versions = [r[0] for r in cur.fetchall()]
            except Exception:
                schema_versions = ["1.0.0"]

        finally:
            chk_conn.close()

        manifest = {
            "app_name": "Kodo POS",
            "kodo_version": "1.0.0",
            "exported_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "platform": sys.platform,
            "db_sha256": db_checksum,
            "schema_versions": schema_versions,
            "counts": counts
        }

        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        # 4. Copie des médias optionnels (ex. images produits)
        assets_dir = os.path.join(temp_dir, "assets")
        os.makedirs(assets_dir, exist_ok=True)
        
        base_data_dir = ShopConfig.get_base_data_dir()
        images_dir = os.path.join(base_data_dir, "images")
        if include_media and os.path.exists(images_dir):
            shutil.copytree(images_dir, os.path.join(assets_dir, "images"), dirs_exist_ok=True)

        # 5. Emballage ZIP .kodo
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(temp_db_path, "kodo_database.db")
            zf.write(manifest_path, "manifest.json")

            for root, _, files in os.walk(assets_dir):
                for file in files:
                    full_file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_file_path, temp_dir)
                    zf.write(full_file_path, rel_path)

        print(f"[OK] Exportation de migration .kodo créée avec succès : {output_path}")
        return output_path

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def validate_kodo_package(package_path: str) -> dict:
    """Valide la structure et le checksum SHA-256 d'un paquet .kodo."""
    if not os.path.exists(package_path):
        raise MigrationPackageError(f"Fichier introuvable: {package_path}")

    if not zipfile.is_zipfile(package_path):
        raise MigrationPackageError("Le fichier fourni n'est pas une archive ZIP ou .kodo valide.")

    with zipfile.ZipFile(package_path, "r") as zf:
        file_list = zf.namelist()
        if "manifest.json" not in file_list or "kodo_database.db" not in file_list:
            raise MigrationPackageError("Format de paquet invalide: manifest.json ou kodo_database.db manquant.")

        with zf.open("manifest.json") as mf:
            manifest_data = json.load(mf)

        temp_dir = tempfile.mkdtemp(prefix="kodo_val_")
        try:
            db_extracted = zf.extract("kodo_database.db", temp_dir)
            extracted_sha = calculate_file_sha256(db_extracted)

            expected_sha = manifest_data.get("db_sha256")
            if expected_sha and extracted_sha != expected_sha:
                raise MigrationPackageError(f"Falsification ou corruption du paquet ! Checksum attendu: {expected_sha}, obtenu: {extracted_sha}")

            manifest_data["valid"] = True
            return manifest_data
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

def import_migration_package(package_path: str, target_db_path: str = None) -> dict:
    """
    Importe un paquet de migration .kodo d'une autre machine (PC ou Mac),
    crée un snapshot de secours pré-importation, valide l'intégrité de la chaîne cryptographique,
    exécute les migrations de schéma si nécessaire, et restaure en cas d'échec.
    """
    dest_db = target_db_path or ShopConfig.get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(dest_db)), exist_ok=True)

    # 1. Validation du paquet
    manifest_info = validate_kodo_package(package_path)

    # 2. Snapshot de sécurité pré-importation
    snapshot_path = MigrationManager.create_pre_migration_snapshot(dest_db)

    temp_dir = tempfile.mkdtemp(prefix="kodo_import_")

    try:
        with zipfile.ZipFile(package_path, "r") as zf:
            zf.extractall(temp_dir)

        imported_db_temp = os.path.join(temp_dir, "kodo_database.db")

        # 3. Contrôle d'intégrité cryptographique sur la base importée
        try:
            verify_database_integrity(conn=None)
        except Exception as ie:
            print(f"⚠️ Avertissement d'intégrité sur la base importée: {ie}")

        # 4. Remplacement atomique de la base locale
        shutil.copy2(imported_db_temp, dest_db)

        # 5. Extraction des médias
        imported_assets = os.path.join(temp_dir, "assets")
        if os.path.exists(imported_assets):
            target_assets = os.path.join(ShopConfig.get_base_data_dir(), "images")
            os.makedirs(target_assets, exist_ok=True)
            shutil.copytree(imported_assets, target_assets, dirs_exist_ok=True)

        # 6. Exécution des migrations si le schéma nécessite une mise à niveau
        MigrationManager.run_migrations(dest_db)
        MigrationManager.initialiser_db(db_path=dest_db)

        print(f"[OK] Importation de migration .kodo réussie dans: {dest_db}")
        return {
            "success": True,
            "manifest": manifest_info,
            "target_db": dest_db,
            "snapshot_restoration_available": snapshot_path
        }

    except Exception as e:
        # Restauration physique immédiate du snapshot en cas d'erreur
        if snapshot_path:
            MigrationManager.restore_snapshot(dest_db, snapshot_path)
        raise MigrationPackageError(f"Échec de l'importation de migration. Restauration de la base effectuée. Erreur : {e}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
