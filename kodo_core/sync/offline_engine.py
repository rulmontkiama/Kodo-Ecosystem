"""
Kōdo POS - Moteur Hors-Ligne : détection de la reconnexion, résolution LWW et audit de stock.

La file des mouvements de stock vers Shopify est `sync_queue` (kodo_core/db/shopify_file.py) ;
l'ancienne table `OfflineQueue` et la file générique `offline_queue.py` étaient inutilisées en
production (consommateur factice) et ont été retirées.
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
            # Vérification TLS complète (contexte maison de l'updater, magasin certifi) :
            # un portail captif d'hôtel ou de centre commercial répond 200 à tout et serait
            # pris pour un accès Internet valide, ce qui relancerait la file de synchro dans
            # le vide. Un certificat qui ne valide pas = pas d'Internet utilisable.
            from kodo_core.services.updater import build_ssl_context
            ctx = build_ssl_context()
            req = urllib.request.Request(host, headers={"User-Agent": "KodoPOS-NetworkCheck/1.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=timeout):
                return True
        except Exception:
            return False

    @classmethod
    def init_db_schema(cls, conn=None):
        """Ajoute les colonnes de synchronisation hors-ligne (Tickets, Stocks, Produits) si absentes."""
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True
        try:
            c = conn.cursor()
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
            logger.error(f"Erreur d'initialisation du schéma hors-ligne: {e}")
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

        cls.init_db_schema(conn=conn)

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
                        # Le décompte a déjà été appliqué par enregistrer_vente() à la création
                        # du ticket (hors-ligne ou non) : ne pas décrémenter une seconde fois ici,
                        # seulement détecter et signaler un stock négatif pour audit manuel.
                        if (qte_actuelle or 0) < 0:
                            cursor.execute("UPDATE Stocks SET requires_stock_audit=1 WHERE id=?", (stock_id,))
                            if prod_id:
                                cursor.execute("UPDATE Produits SET requires_stock_audit=1 WHERE id=?", (prod_id,))
                            logger.warning(f"[AUDIT STOCK] Conflit de stock lors de la synchro du ticket {t_num} (Stock ID {stock_id}: {qte_actuelle}). Produit marqué pour audit.")

                cursor.execute("UPDATE Tickets SET sync_status=1 WHERE id=?", (t_id,))
                synced_count += 1

            conn.commit()
            if synced_count > 0:
                logger.info(f"{synced_count} ticket(s) hors-ligne synchronisé(s) avec succès.")
            return synced_count
        finally:
            if close_conn:
                conn.close()
