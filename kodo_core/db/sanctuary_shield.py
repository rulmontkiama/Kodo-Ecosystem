# -*- coding: utf-8 -*-
"""
Système de Sanctuarisation Automatisé (Stock & Données Magasin) - Kōdo POS Core.

Garantit :
1. RÈGLE CARDINALE N°0 : Aucune perte de données de stock, catalogue ou magasin.
2. Empreinte d'intégrité cryptographique (Checksums, somme de stock, hash canonique SHA-256).
3. Sauvegarde atomique hermétique préalable à toute opération technique ou migration.
4. Validation différentielle stricte : Delta Stock == 0.
5. Registre du code sanctuarisé (fichiers et fonctions du domaine stock/magasin intouchables).
"""

import os
import sqlite3
import hashlib
import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Tuple, Optional, Set, List

# Tables critiques sanctuarisées
SANCTUARY_TABLES = frozenset({
    # Catalogue & Inventaire
    "Produits",
    "Stocks",
    "Mouvements_Stock",
    "Categories",
    # Identité & Configuration Magasin
    "Parametres",
    # Piste d'audit, Ventes & Comptabilité
    "Tickets",
    "Ventes_Details",
    "Clients",
    "Ledger_Caisse",
    "Rapports_Z",
    "Clotures_Caisse"
})

# Registre du code sanctuarisé (fichiers fondamentaux pour les données stock & boutique)
CODE_SANCTUARY_REGISTRY = frozenset({
    "kodo_core/services/stock_service.py",
    "kodo_core/services/fiscal_service.py",
    "kodo_core/domain/catalog/models.py",
    "kodo_core/domain/sales/cart_engine.py",
    "kodo_core/domain/sales/models.py",
    "kodo_core/db/connection.py",
    "kodo_core/db/migrations.py",
    "database_manager.py",
    "kodo_core/config.py"
})


class SanctuaryIntegrityError(Exception):
    """Levée quand une violation de l'intégrité du stock ou des données du magasin est détectée."""
    pass


class SanctuaryShield:
    """
    Gestionnaire de sanctuarisation du stock et des données magasin.
    """

    @staticmethod
    def compute_sanctuary_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Calcule l'empreinte d'intégrité complète des données sensibles du magasin :
        - Nombre total d'articles dans le catalogue
        - Somme exacte des pièces actuellement en stock
        - Nombre de déclinaisons de stock
        - Nombre total de tickets historiques
        - Somme des mouvements du grand livre
        - Hash canonique SHA-256 des tables de stock et catalogue
        """
        cursor = conn.cursor()

        # 1. Vérification de l'existence des tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = set(row[0] for row in cursor.fetchall())

        fingerprint: Dict[str, Any] = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "tables_found": list(existing_tables.intersection(SANCTUARY_TABLES)),
            "products_count": 0,
            "stock_rows_count": 0,
            "total_stock_units": 0,
            "tickets_count": 0,
            "ledger_movements_count": 0,
            "canonical_hash": ""
        }

        hasher = hashlib.sha256()

        # Catalogue : Produits
        if "Produits" in existing_tables:
            cursor.execute("SELECT COUNT(*) FROM Produits")
            fingerprint["products_count"] = cursor.fetchone()[0] or 0

            cursor.execute("SELECT id, nom, code_barre, prix_vente_tvac, taux_tva FROM Produits ORDER BY id")
            for row in cursor.fetchall():
                row_str = f"P:{row[0]}:{row[1]}:{row[2]}:{row[3]}:{row[4]}|"
                hasher.update(row_str.encode("utf-8"))

        # Stock : Stocks
        if "Stocks" in existing_tables:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(quantite_actuelle), 0) FROM Stocks")
            stock_row = cursor.fetchone()
            fingerprint["stock_rows_count"] = stock_row[0] or 0
            fingerprint["total_stock_units"] = int(stock_row[1] or 0)

            cursor.execute("SELECT id, id_produit, taille, quantite_actuelle FROM Stocks ORDER BY id")
            for row in cursor.fetchall():
                row_str = f"S:{row[0]}:{row[1]}:{row[2]}:{row[3]}|"
                hasher.update(row_str.encode("utf-8"))

        # Ventes : Tickets
        if "Tickets" in existing_tables:
            cursor.execute("SELECT COUNT(*) FROM Tickets")
            fingerprint["tickets_count"] = cursor.fetchone()[0] or 0

        # Grand Livre : Ledger_Caisse
        if "Ledger_Caisse" in existing_tables:
            cursor.execute("SELECT COUNT(*) FROM Ledger_Caisse")
            fingerprint["ledger_movements_count"] = cursor.fetchone()[0] or 0

        fingerprint["canonical_hash"] = hasher.hexdigest()
        return fingerprint

    @staticmethod
    def create_atomic_sanctuary_backup(
        source_db_path: str,
        backup_dir: Optional[str] = None
    ) -> str:
        """
        Crée une sauvegarde atomique hermétique de la base de données active
        dans le répertoire de sanctuarisation dédié (~/Documents/Kodo_POS/backups/sanctuary_pre_v2/).
        Utilise l'API native sqlite3.Connection.backup() pour garantir zéro corruption.
        """
        if not os.path.exists(source_db_path):
            raise FileNotFoundError(f"Base de données source introuvable : {source_db_path}")

        if backup_dir is None:
            user_home = os.path.expanduser("~")
            backup_dir = os.path.join(user_home, "Documents", "Kodo_POS", "backups", "sanctuary_pre_v2")

        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        db_basename = os.path.basename(source_db_path)
        backup_filename = f"sanctuary_{timestamp}_{db_basename}"
        target_path = os.path.join(backup_dir, backup_filename)

        source_conn = sqlite3.connect(source_db_path, timeout=10.0)
        target_conn = sqlite3.connect(target_path, timeout=10.0)
        try:
            source_conn.backup(target_conn)
            print(f"🛡️ [SANCTUARY SHIELD] Sauvegarde atomique créée : {target_path}")
        finally:
            target_conn.close()
            source_conn.close()

        # Vérification de la copie : une sauvegarde non relue n'est pas une sauvegarde.
        meta_path = f"{target_path}.meta.json"
        verify_conn = sqlite3.connect(target_path)
        try:
            row = verify_conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                os.remove(target_path)
                raise SanctuaryIntegrityError(
                    f"Sauvegarde Sanctuaire corrompue, copie supprimée : {target_path}"
                )
            fp = SanctuaryShield.compute_sanctuary_fingerprint(verify_conn)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(fp, f, indent=2, ensure_ascii=False)
        finally:
            verify_conn.close()

        # Rétention : 10 sauvegardes maximum. Sans purge, une copie intégrale de la base à
        # chaque démarrage sature le disque du poste boutique en quelques semaines.
        copies = sorted(
            (os.path.join(backup_dir, n) for n in os.listdir(backup_dir) if n.startswith("sanctuary_") and not n.endswith(".meta.json")),
            key=os.path.getmtime,
            reverse=True,
        )
        for obsolete in copies[10:]:
            for chemin in (obsolete, f"{obsolete}.meta.json"):
                try:
                    os.remove(chemin)
                except OSError:
                    pass
        return target_path

    @staticmethod
    def verify_invariant(
        fingerprint_before: Dict[str, Any],
        fingerprint_after: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Vérifie l'invariance stricte du stock et du catalogue entre deux étapes.
        Delta Stock == 0 et Delta Produits == 0.
        """
        delta_stock = fingerprint_after["total_stock_units"] - fingerprint_before["total_stock_units"]
        delta_products = fingerprint_after["products_count"] - fingerprint_before["products_count"]
        delta_stock_rows = fingerprint_after["stock_rows_count"] - fingerprint_before["stock_rows_count"]

        if delta_stock != 0:
            msg = (f"Rupture d'intégrité de stock : variation de {delta_stock} pièces "
                   f"(avant: {fingerprint_before['total_stock_units']}, "
                   f"après: {fingerprint_after['total_stock_units']}).")
            return False, msg

        if delta_products != 0:
            msg = (f"Rupture de catalogue : variation de {delta_products} produits "
                   f"(avant: {fingerprint_before['products_count']}, "
                   f"après: {fingerprint_after['products_count']}).")
            return False, msg

        if delta_stock_rows != 0:
            msg = (f"Rupture de déclinaisons de stock : variation de {delta_stock_rows} lignes "
                   f"(avant: {fingerprint_before['stock_rows_count']}, "
                   f"après: {fingerprint_after['stock_rows_count']}).")
            return False, msg

        return True, "Invariance sanctuarisée : Stock et Catalogue 100% conformes (Delta = 0)."

    @staticmethod
    def is_file_sanctuary_protected(file_path: str) -> bool:
        """
        Vérifie si un fichier de code est protégé par le registre de sanctuarisation.
        Tout fichier sanctuarisé est inviolable par les scripts de purge de code mort.
        """
        norm_path = os.path.normpath(file_path).replace("\\", "/")
        for protected in CODE_SANCTUARY_REGISTRY:
            if norm_path.endswith(protected) or protected in norm_path:
                return True
        return False


def copier_base_sqlite(source_path: str, target_path: str) -> str:
    """
    Copie une base SQLite vivante vers target_path, sans perte.
    `shutil.copy2` est proscrit pour cet usage : en mode WAL, les transactions validées
    résident dans le fichier -wal tant qu'aucun point de contrôle n'a eu lieu. Copier le
    seul fichier principal produit une base amputée — et, sur une base jeune, une base
    sans schéma.
    Mesuré : 200 tickets committés, copie shutil.copy2 illisible (« no such table »),
    copie sqlite3.backup() complète.
    L'API native sqlite3.Connection.backup() prend un verrou de lecture cohérent et intègre
    le contenu du -wal. La copie est systématiquement relue avant d'être retenue : une
    sauvegarde non vérifiée n'est pas une sauvegarde.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Base source introuvable : {source_path}")

    os.makedirs(os.path.dirname(os.path.abspath(target_path)) or ".", exist_ok=True)
    tmp_path = f"{target_path}.tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    source_conn = sqlite3.connect(source_path, timeout=10.0)
    target_conn = sqlite3.connect(tmp_path, timeout=10.0)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()

    verif = sqlite3.connect(tmp_path)
    try:
        row = verif.execute("PRAGMA integrity_check").fetchone()
    finally:
        verif.close()

    if not row or row[0] != "ok":
        os.remove(tmp_path)
        raise SanctuaryIntegrityError(
            f"Copie de {os.path.basename(source_path)} corrompue, copie supprimée."
        )

    os.replace(tmp_path, target_path)
    return target_path


def restaurer_base_sqlite(snapshot_path: str, target_path: str) -> str:
    """
    Restaure target_path depuis snapshot_path, en éliminant les journaux périmés.
    Écraser le fichier principal en laissant en place le -wal de la base vivante rend
    celle-ci illisible : le journal désapparié est rejoué sur un fichier qui ne lui
    correspond plus. La restauration passe donc par une reconstruction complète, après
    retrait explicite des fichiers -wal et -shm.
    """
    if not os.path.exists(snapshot_path):
        raise FileNotFoundError(f"Sauvegarde introuvable : {snapshot_path}")

    verif = sqlite3.connect(snapshot_path)
    try:
        row = verif.execute("PRAGMA integrity_check").fetchone()
    finally:
        verif.close()

    if not row or row[0] != "ok":
        raise SanctuaryIntegrityError(
            f"Sauvegarde {os.path.basename(snapshot_path)} corrompue : restauration refusée, "
            "la base en place est conservée."
        )

    for ext in ("-wal", "-shm"):
        stale = target_path + ext
        if os.path.exists(stale):
            os.remove(stale)

    if os.path.exists(target_path):
        os.remove(target_path)

    return copier_base_sqlite(snapshot_path, target_path)
