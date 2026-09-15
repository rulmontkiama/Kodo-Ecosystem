# -*- coding: utf-8 -*-
"""
File de synchronisation Offline-First - Kōdo POS Core.
Persiste les événements métier (vente, stock, client) à synchroniser plus tard,
avec retries à seuil et résolution de conflit par deltas relatifs pour le stock.
Aucune dépendance réseau directe : la synchronisation réelle est injectée via
un callback (sync_handler_callback), mockable dans les tests.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from kodo_core.db.connection import db_transaction


class EventType:
    """Types d'événements métier synchronisables."""

    VENTE_CREEE = "VENTE_CREEE"
    STOCK_AJUSTE = "STOCK_AJUSTE"
    CLIENT_MODIFIE = "CLIENT_MODIFIE"

    ALL = {VENTE_CREEE, STOCK_AJUSTE, CLIENT_MODIFIE}


class QueueStatus:
    """États possibles d'un événement dans la file de synchronisation."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


DEFAULT_MAX_RETRIES = 3


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée la table sync_queue si elle n'existe pas."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_message TEXT
        )
        """
    )
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OfflineQueueManager:
    """Gère la persistance et le rejeu de la file de synchronisation offline-first."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        ensure_schema(self.conn)
        # Garantit un accès aux colonnes par nom, y compris hors des blocs db_transaction.
        self.conn.row_factory = sqlite3.Row

    def enqueue_event(
        self,
        event_type: str,
        payload: Dict,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> int:
        """Enregistre un événement à synchroniser, de manière atomique.

        Pour STOCK_AJUSTE, le payload doit porter un delta relatif ("delta": +/-N)
        et non une quantité absolue : cela permet, à la synchronisation, de fusionner
        des ajustements concurrents par addition plutôt que par écrasement.
        """
        if event_type not in EventType.ALL:
            raise ValueError(f"event_type inconnu : {event_type}")

        if event_type == EventType.STOCK_AJUSTE and "delta" not in payload:
            raise ValueError(
                "STOCK_AJUSTE requiert un delta relatif ('delta') et non une quantité absolue."
            )

        now = _now_iso()
        with db_transaction(conn=self.conn) as cursor:
            cursor.execute(
                "INSERT INTO sync_queue "
                "(event_type, payload_json, status, retry_count, max_retries, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (event_type, json.dumps(payload), QueueStatus.PENDING, max_retries, now, now),
            )
            event_id = cursor.lastrowid

        return event_id

    def _update_event(
        self,
        event_id: int,
        status: str,
        retry_count: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with db_transaction(conn=self.conn) as cursor:
            if retry_count is None:
                cursor.execute(
                    "UPDATE sync_queue SET status = ?, updated_at = ?, error_message = ? WHERE id = ?",
                    (status, _now_iso(), error_message, event_id),
                )
            else:
                cursor.execute(
                    "UPDATE sync_queue SET status = ?, retry_count = ?, updated_at = ?, "
                    "error_message = ? WHERE id = ?",
                    (status, retry_count, _now_iso(), error_message, event_id),
                )

    def get_event(self, event_id: int) -> Optional[sqlite3.Row]:
        """Retourne un événement de la file par son id, ou None."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM sync_queue WHERE id = ?", (event_id,))
        return cursor.fetchone()

    def list_events(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        """Retourne les événements de la file, éventuellement filtrés par statut, en ordre FIFO."""
        cursor = self.conn.cursor()
        if status is None:
            cursor.execute("SELECT * FROM sync_queue ORDER BY id ASC")
        else:
            cursor.execute("SELECT * FROM sync_queue WHERE status = ? ORDER BY id ASC", (status,))
        return cursor.fetchall()

    def process_pending(self, sync_handler_callback: Callable[[str, Dict], None]) -> Dict[str, int]:
        """Dépile les événements PENDING en ordre FIFO et tente leur synchronisation.

        sync_handler_callback(event_type, payload) doit lever une exception en cas
        d'échec (ex : panne réseau simulée). Chaque événement est traité et son statut
        mis à jour de façon atomique et indépendante des autres événements de la file :
        - succès : passage à SUCCESS.
        - échec avec retry_count encore sous max_retries : retry_count incrémenté,
          l'événement repasse en PENDING pour être retenté lors d'un futur appel.
        - échec ayant atteint max_retries : passage définitif à FAILED avec error_message.
        """
        pending_rows = self.list_events(status=QueueStatus.PENDING)

        summary = {"success": 0, "retried": 0, "failed": 0}

        for row in pending_rows:
            event_id = row["id"]
            event_type = row["event_type"]
            payload = json.loads(row["payload_json"])
            retry_count = row["retry_count"]
            max_retries = row["max_retries"]

            self._update_event(event_id, QueueStatus.IN_PROGRESS)

            try:
                sync_handler_callback(event_type, payload)
            except Exception as exc:
                retry_count += 1
                if retry_count >= max_retries:
                    self._update_event(
                        event_id, QueueStatus.FAILED, retry_count=retry_count, error_message=str(exc)
                    )
                    summary["failed"] += 1
                else:
                    self._update_event(
                        event_id, QueueStatus.PENDING, retry_count=retry_count, error_message=str(exc)
                    )
                    summary["retried"] += 1
            else:
                self._update_event(event_id, QueueStatus.SUCCESS)
                summary["success"] += 1

        return summary
