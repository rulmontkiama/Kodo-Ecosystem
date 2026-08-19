# -*- coding: utf-8 -*-
"""
Routes API Sauvegardes & Migration Inter-Machines - Kōdo POS Core
Gère l'exportation du pack ZIP de transfert, la prévisualisation et la restauration.
"""

from typing import Dict, Any, Tuple, Optional
import backup_manager


def handle_backup_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any]) -> Optional[Any]:
    """
    Gestionnaire de requêtes pour les sauvegardes et le transfert de machine.
    """

    # 1. Exporter le pack de transfert complet (.ZIP pour changement de machine)
    if method == "GET" and path == "/api/backup/export-migration":
        success, filename, zip_bytes, manifest = backup_manager.creer_pack_migration_machine()
        if not success or not zip_bytes:
            return 500, {"error": "Échec de création du pack de transfert.", "details": manifest}

        headers = {
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Manifest-Stats": str(manifest.get("stats", {}))
        }
        return 200, zip_bytes, headers

    # 2. Prévisualiser un pack de transfert ZIP avant restauration
    elif method == "POST" and path == "/api/backup/preview-migration":
        zip_payload = data.get("zip_data") or data.get("file") or data.get("base64")
        if not zip_payload:
            return 400, {"error": "Aucune donnée de fichier ZIP reçue."}

        res = backup_manager.previsualiser_pack_migration(zip_payload)
        return (200, res) if res.get("valid") else (400, res)

    # 3. Restaurer un pack de transfert ZIP sur cette machine
    elif method == "POST" and path == "/api/backup/import-migration":
        zip_payload = data.get("zip_data") or data.get("file") or data.get("base64")
        if not zip_payload:
            return 400, {"error": "Aucune donnée de fichier ZIP reçue pour la restauration."}

        res = backup_manager.restaurer_pack_migration(zip_payload)
        return (200, res) if res.get("success") else (400, res)

    # 4. Déclencher une sauvegarde manuelle locale classique
    elif method == "POST" and path == "/api/backup/create":
        res = backup_manager.creer_sauvegarde_manuelle()
        return 200, res

    # 5. Lister les sauvegardes disponibles
    elif method == "GET" and path == "/api/backup/list":
        backups = backup_manager.lister_sauvegardes()
        return 200, backups

    # 6. Restaurer une sauvegarde existante du dossier local
    elif method == "POST" and path == "/api/backup/restore":
        filename = data.get("filename") or data.get("backup_file")
        if not filename:
            return 400, {"error": "Nom du fichier de sauvegarde requis"}

        backup_dir = backup_manager.get_backup_directory()
        import os
        filepath = os.path.join(backup_dir, filename) if not os.path.isabs(filename) else filename
        res = backup_manager.restaurer_pack_migration(filepath)
        return 200, res

    return None
