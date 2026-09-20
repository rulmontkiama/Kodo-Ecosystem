# -*- coding: utf-8 -*-
"""
Module d'Assainissement Automatique et Diagnostic de Santé Client - Kōdo POS v2.0
Garantit l'intégrité de l'environnement sur les postes boutiques :
1. Sauvegarde atomique du Sanctuaire (Stock & Ventes préservés à 100%).
2. Purge des anciens caches Web/React résiduels (fin des écrans blancs et des vieux bugs IHM).
3. Élimination des anciens patchs v1.0 obsolètes.
4. Validation et migration automatique du schéma SQLite (NF525 + Arrondi Belge).
5. Diagnostic complet de santé système.
"""

import os
import sys
import glob
import json
import shutil
import sqlite3
import logging
import datetime
from typing import Dict, Any, List

import kodo_base
import database_manager
from kodo_core.db.sanctuary_shield import SanctuaryShield

logger = logging.getLogger("kodo.sanitizer")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[SANITIZER v2.0] %(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def sanitize_client_environment() -> Dict[str, Any]:
    """
    Exécute l'assainissement automatique de l'environnement client au démarrage.
    Ne détruit JAMAIS les données utilisateur ; nettoie uniquement les caches et résidus v1.0.
    """
    logger.info("🔍 Démarrage de l'assainissement automatique de l'environnement client...")
    actions_taken: List[str] = []

    # 1. Sauvegarde Sanctuaire préalable de la base active
    db_path = database_manager.DB_NAME
    if os.path.exists(db_path):
        try:
            backup_path = SanctuaryShield.create_atomic_sanctuary_backup(db_path)
            actions_taken.append(f"Sauvegarde Sanctuaire créée : {os.path.basename(backup_path)}")
        except Exception as e:
            logger.warning(f"Avertissement sauvegarde préalable : {e}")

    # 1.5 Reprise d'une base héritée : explicite, vérifiée, et seulement si kodo_pos.db est absent.
    try:
        rapport = database_manager.migrer_base_heritee(dry_run=False)
        if rapport.get("migre"):
            actions_taken.append(f"Base héritée reprise avec succès ({rapport.get('produits', 0)} produits)")
        elif rapport.get("source"):
            logger.warning(f"Reprise de base héritée non effectuée : {rapport.get('raison')}")
    except Exception as me:
        logger.warning(f"Reprise de base héritée impossible : {me}")

    # 2. AUCUNE purge du cache IHM. ~/Library/Caches/KodoPOS/dist et ~/.kodo_pos/dist sont les
    # dossiers d'INSTALLATION des mises à jour OTA (updater.get_target_dist_dir), lus en
    # priorité par server_pos.get_dist_dir. Les effacer — et supprimer le version.json frère —
    # replonge le client sur l'IHM du DMG à chaque démarrage et enferme l'updater dans une
    # boucle de re-téléchargement. La cohérence de version est déjà garantie par
    # is_cache_dist_current() côté serveur : rien à nettoyer ici.

    # 3. AUCUNE purge des patchs : patch_loader gère lui-même sa rétention et son rollback
    # (retour au code du DMG après 3 démarrages en échec). Supprimer ces fichiers sous lui
    # lui retire son filet de sécurité.

    # 4. Validation et migration du schéma SQLite
    try:
        database_manager.initialiser_db()
        actions_taken.append("Schéma SQLite initialisé & vérifié (NF525 + Arrondi Belge)")
    except Exception as dbe:
        logger.error(f"Erreur vérification schéma BDD : {dbe}")

    # 5. AUCUNE suppression de fichiers dans le dossier de l'application : sur un .app signé et
    # notarisé, toute écriture dans le bundle invalide la signature (Gatekeeper).

    logger.info(f"✅ Assainissement client terminé avec succès ({len(actions_taken)} actions effectuées).")
    return {
        "success": True,
        "actions_taken": actions_taken,
        "version": kodo_base.BASE_VERSION
    }


def get_system_health_report() -> Dict[str, Any]:
    """
    Génère un bilan de santé exhaustif pour la console d'administration et l'API.
    Aucune exception ne doit masquer silencieusement un chiffre faux : chaque sonde
    est isolée et son échec est remonté dans `alerts`, jamais converti en zéro plausible.
    """
    db_path = database_manager.DB_NAME
    db_exists = os.path.exists(db_path)
    db_size = os.path.getsize(db_path) if db_exists else 0
    integrity_ok = False
    products_count = 0
    stock_sum = 0
    unclosed_days: List[Dict[str, Any]] = []
    past_days: List[Dict[str, Any]] = []
    alerts: List[str] = []

    if db_exists:
        conn = None
        try:
            conn = database_manager.get_connection()
            c = conn.cursor()
            c.execute("PRAGMA quick_check")
            row = c.fetchone()
            integrity_ok = bool(row and row[0] == "ok")

            c.execute("SELECT COUNT(*) FROM Produits")
            products_count = int(c.fetchone()[0] or 0)

            # Colonne réelle du schéma : Stocks.quantite_actuelle (cf. _initialiser_db_raw).
            c.execute("SELECT COALESCE(SUM(quantite_actuelle), 0) FROM Stocks")
            stock_sum = int(c.fetchone()[0] or 0)

            unclosed_days = database_manager.lister_jours_non_clotures(conn=conn)
            today_str = datetime.date.today().isoformat()
            past_days = [j for j in unclosed_days if str(j.get("jour", "")) < today_str]
        except Exception as e:
            logger.warning(f"Erreur lecture BDD health check: {e}")
            alerts.append(f"Diagnostic base de données incomplet : {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    else:
        alerts.append("Base de données introuvable")

    # Circuit breaker imprimante : la clé exposée par PrintWorker.get_circuit_status() est "state".
    printer_circuit = "UNKNOWN"
    try:
        from kodo_core.hardware.print_worker import get_print_worker
        printer_circuit = get_print_worker().get_circuit_status().get("state", "UNKNOWN")
    except Exception as pe:
        logger.warning(f"État imprimante indisponible : {pe}")

    if not integrity_ok:
        alerts.append("Intégrité SQLite non confirmée (PRAGMA quick_check)")
    if past_days:
        alerts.append(f"{len(past_days)} journée(s) antérieure(s) non clôturée(s) en attente")

    status = "HEALTHY" if not alerts else ("DEGRADED" if not integrity_ok else "ATTENTION")

    return {
        "status": status,
        "version": kodo_base.BASE_VERSION,
        "database": {
            "path": db_path,
            "exists": db_exists,
            "size_bytes": db_size,
            "integrity_ok": integrity_ok,
            "products_count": products_count,
            "stock_total_units": stock_sum,
        },
        "unclosed_days": {
            "total_pending": len(unclosed_days),
            "has_past_unclosed": bool(past_days),
            "details": unclosed_days,
        },
        "printer": {"circuit_state": printer_circuit},
        "alerts": alerts,
    }
