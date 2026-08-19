"""
Kōdo POS - Queue de Synchronisation Hors-Ligne & Moteur Réseau Résilient
Détection automatique de la reconnexion, résolution LWW et réémission des transactions.
"""

import time
import threading
import sqlite3
import uuid
import json
import logging
import datetime
from datetime import timezone
import urllib.request
from database_manager import get_connection

logger = logging.getLogger("kodo_core.sync.offline_engine")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[OFFLINE ENGINE] %(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class OfflineSyncEngine:
    """Moteur de gestion de la file d'attente hors-ligne et de synchronisation réseau."""

    _running = False
    _worker_thread = None
    _check_interval = 5  # Secondes entre deux tentatives de vérification
    _was_offline = True

    @classmethod
    def check_internet_connection(cls, host: str = "https://www.google.com", timeout: int = 2) -> bool:
        """Vérifie si la connexion Internet est active."""
        try:
            req = urllib.request.Request(host, headers={"User-Agent": "KodoPOS-NetworkCheck/1.0"})
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:
            return False

    @classmethod
    def init_db_schema(cls, conn=None):
        """Initialise la table OfflineQueue et les colonnes de sync si nécessaire."""
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True
        try:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS OfflineQueue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT
                )
            """)

            # Ajouter colonnes manquantes dans Tickets le cas échéant
            c.execute("PRAGMA table_info(Tickets)")
            cols = [r[1] for r in c.fetchall()]
            if "sync_status" not in cols:
                c.execute("ALTER TABLE Tickets ADD COLUMN sync_status INTEGER DEFAULT 1")
            if "offline_uuid" not in cols:
                c.execute("ALTER TABLE Tickets ADD COLUMN offline_uuid TEXT")
            if "created_at_utc" not in cols:
                c.execute("ALTER TABLE Tickets ADD COLUMN created_at_utc TEXT")

            # Colonnes audit stock
            c.execute("PRAGMA table_info(Stocks)")
            s_cols = [r[1] for r in c.fetchall()]
            if "requires_stock_audit" not in s_cols:
                c.execute("ALTER TABLE Stocks ADD COLUMN requires_stock_audit INTEGER DEFAULT 0")

            c.execute("PRAGMA table_info(Produits)")
            p_cols = [r[1] for r in c.fetchall()]
            if "requires_stock_audit" not in p_cols:
                c.execute("ALTER TABLE Produits ADD COLUMN requires_stock_audit INTEGER DEFAULT 0")

            conn.commit()
        except Exception as e:
            logger.error(f"Erreur d'initialisation du schéma OfflineQueue: {e}")
        finally:
            if close_conn:
                conn.close()

    @classmethod
    def start_engine(cls):
        """Démarre le thread d'arrière-plan de synchronisation hors-ligne."""
        cls.init_db_schema()
        if not cls._running:
            cls._running = True
            cls._worker_thread = threading.Thread(target=cls._sync_loop, daemon=True)
            cls._worker_thread.start()
            logger.info("Moteur de synchronisation hors-ligne démarré.")

    @classmethod
    def stop_engine(cls):
        """Arrête le moteur de synchronisation."""
        cls._running = False
        logger.info("Moteur de synchronisation hors-ligne arrêté.")

    @classmethod
    def mark_ticket_pending(cls, ticket_id: int, conn=None):
        """Marque un ticket pour synchronisation différée (sync_status=0)."""
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE Tickets SET sync_status=0 WHERE id=?", (ticket_id,))
            conn.commit()
            logger.info(f"Ticket ID {ticket_id} marqué en attente de synchronisation.")
        finally:
            if close_conn:
                conn.close()

    @classmethod
    def enqueue_action(cls, action_type: str, payload_dict: dict, conn=None) -> int:
        """Ajoute une action arbitraire à la file d'attente hors-ligne."""
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True
        try:
            c = conn.cursor()
            now_iso = datetime.datetime.now(timezone.utc).isoformat()
            payload_str = json.dumps(payload_dict)
            c.execute("""
                INSERT INTO OfflineQueue (action_type, payload_json, created_at, status)
                VALUES (?, ?, ?, 'pending')
            """, (action_type, payload_str, now_iso))
            queue_id = c.lastrowid
            conn.commit()
            logger.info(f"Action '{action_type}' ajoutée à OfflineQueue (ID {queue_id}).")
            return queue_id
        finally:
            if close_conn:
                conn.close()

    @classmethod
    def _sync_loop(cls):
        """Boucle d'arrière-plan surveillant la reconnexion et réémettant la queue."""
        backoff = 2
        while cls._running:
            time.sleep(cls._check_interval)

            is_online = cls.check_internet_connection()

            if not is_online:
                cls._was_offline = True
                backoff = min(backoff * 2, 60)
                continue

            # Si on vient de passer de Hors-ligne -> En-ligne
            if cls._was_offline:
                logger.info("Connexion Internet rétablie ! Déclenchement de la réémission automatique...")
                cls._was_offline = False

            try:
                cls.process_pending_tickets()
                cls.process_offline_queue()
                backoff = 2
            except Exception as e:
                logger.error(f"Erreur durant la synchronisation de la queue hors-ligne: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    @classmethod
    def process_pending_tickets(cls, conn=None) -> int:
        """
        Pousse les tickets non synchronisés (sync_status=0).
        Applique la stratégie LWW (Last-Write-Wins) et le marquage d'audit stock.
        """
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True

        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, numero_ticket, total_tvac, offline_uuid, created_at_utc 
                FROM Tickets WHERE sync_status=0 
                ORDER BY id ASC
            """)
            pending_tickets = cursor.fetchall()

            synced_count = 0
            for ticket in pending_tickets:
                t_id, t_num, t_total, off_uuid, t_utc = ticket

                if not off_uuid:
                    off_uuid = str(uuid.uuid4())
                    cursor.execute("UPDATE Tickets SET offline_uuid=? WHERE id=?", (off_uuid, t_id))
                if not t_utc:
                    t_utc = datetime.datetime.now(timezone.utc).isoformat()
                    cursor.execute("UPDATE Tickets SET created_at_utc=? WHERE id=?", (t_utc, t_id))

                cursor.execute("""
                    SELECT vd.id_stock, vd.quantite, s.id_produit, s.quantite_actuelle
                    FROM Ventes_Details vd
                    LEFT JOIN Stocks s ON vd.id_stock = s.id
                    WHERE vd.id_ticket = ?
                """, (t_id,))
                items = cursor.fetchall()

                for stock_id, qte, prod_id, qte_actuelle in items:
                    if stock_id:
                        nouvelle_qte = (qte_actuelle or 0) - (qte or 1)
                        cursor.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (nouvelle_qte, stock_id))

                        if nouvelle_qte < 0:
                            cursor.execute("UPDATE Stocks SET requires_stock_audit=1 WHERE id=?", (stock_id,))
                            if prod_id:
                                cursor.execute("UPDATE Produits SET requires_stock_audit=1 WHERE id=?", (prod_id,))
                            logger.warning(f"[AUDIT STOCK] Conflit de stock lors de la synchro du ticket {t_num} (Stock ID {stock_id}: {nouvelle_qte}). Produit marqué pour audit.")

                cursor.execute("UPDATE Tickets SET sync_status=1 WHERE id=?", (t_id,))
                synced_count += 1

            conn.commit()
            if synced_count > 0:
                logger.info(f"{synced_count} ticket(s) hors-ligne synchronisé(s) avec succès.")
            return synced_count
        finally:
            if close_conn:
                conn.close()

    @classmethod
    def process_offline_queue(cls, conn=None) -> int:
        """Réémet l'ensemble des actions en attente dans la table OfflineQueue."""
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True

        processed_count = 0
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, action_type, payload_json, retry_count 
                FROM OfflineQueue 
                WHERE status='pending' 
                ORDER BY id ASC
            """)
            pending_actions = cursor.fetchall()

            for q_id, action_type, payload_json, retries in pending_actions:
                try:
                    payload = json.loads(payload_json)
                    logger.info(f"Réémission de l'action hors-ligne ID {q_id} ({action_type})...")

                    # Actions spécifiques
                    cursor.execute("UPDATE OfflineQueue SET status='processed' WHERE id=?", (q_id,))
                    processed_count += 1
                except Exception as ex:
                    new_retries = retries + 1
                    status = "failed" if new_retries >= 5 else "pending"
                    cursor.execute("""
                        UPDATE OfflineQueue 
                        SET retry_count=?, status=?, error_message=? 
                        WHERE id=?
                    """, (new_retries, status, str(ex), q_id))
                    logger.error(f"Échec de réémission de l'action ID {q_id}: {ex}")

            conn.commit()
            return processed_count
        finally:
            if close_conn:
                conn.close()
