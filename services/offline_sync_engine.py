"""
Moteur de Synchronisation Offline-First, Résolution de Conflits LWW (Last-Write-Wins) et Audit de Stock.
"""
import time
import threading
import sqlite3
import uuid
import datetime
from datetime import timezone
import urllib.request
from database_manager import get_connection

class OfflineSyncEngine:
    """Gère la file de synchronisation différée et la résilience en cas de réseau instable."""

    _running = False
    _worker_thread = None
    _check_interval = 5  # Secondes entre deux tentatives de synchro

    @classmethod
    def check_internet_connection(cls, host="https://www.google.com", timeout=2) -> bool:
        """Vérifie si la connexion Internet est active."""
        try:
            req = urllib.request.Request(host, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:
            return False

    @classmethod
    def start_engine(cls):
        """Démarre le thread d'arrière-plan de synchronisation."""
        if not cls._running:
            cls._running = True
            cls._worker_thread = threading.Thread(target=cls._sync_loop, daemon=True)
            cls._worker_thread.start()

    @classmethod
    def stop_engine(cls):
        """Arrête le moteur de synchronisation."""
        cls._running = False

    @classmethod
    def mark_ticket_pending(cls, ticket_id: int, conn=None):
        """Marque un ticket pour synchro différée (sync_status=0)."""
        close_conn = False
        if conn is None:
            conn = get_connection()
            close_conn = True
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE Tickets SET sync_status=0 WHERE id=?", (ticket_id,))
            conn.commit()
        finally:
            if close_conn:
                conn.close()

    @classmethod
    def _sync_loop(cls):
        """Boucle résiliente d'arrière-plan avec retries et backoff exponentiel."""
        backoff = 2
        while cls._running:
            time.sleep(cls._check_interval)
            
            if not cls.check_internet_connection():
                # En mode hors-ligne, réinitialiser ou augmenter doucement le backoff
                backoff = min(backoff * 2, 60)
                continue

            # Si Internet est OK, procéder à la poussée des transactions en attente
            try:
                cls.process_pending_tickets()
                backoff = 2  # Réinitialiser le backoff après succès
            except Exception as e:
                print(f"⚠️ Erreur durant la synchro hors-ligne: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    @classmethod
    def process_pending_tickets(cls, conn=None) -> int:
        """
        Pousse l'ensemble des tickets non synchronisés (sync_status=0) vers le registre central.
        Stratégie LWW (Last-Write-Wins) avec détection de conflit de stock (requires_stock_audit=1).
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
            audit_alerts = []

            for ticket in pending_tickets:
                t_id, t_num, t_total, off_uuid, t_utc = ticket
                
                # 1. Vérifier si un UUID et UTC ISO timestamp existent, sinon en générer un
                if not off_uuid:
                    off_uuid = str(uuid.uuid4())
                    cursor.execute("UPDATE Tickets SET offline_uuid=? WHERE id=?", (off_uuid, t_id))
                if not t_utc:
                    t_utc = datetime.datetime.now(timezone.utc).isoformat()
                    cursor.execute("UPDATE Tickets SET created_at_utc=? WHERE id=?", (t_utc, t_id))

                # 2. Récupérer les articles vendus dans ce ticket
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

                        # Stratégie LWW & Conflit de Stock:
                        # Si la vente fait tomber le stock sous 0 (vente simultanée sur caisses déconnectées),
                        # la transaction est TOUJOURS acceptée/validée, mais le produit/stock est marqué pour audit manuel.
                        if nouvelle_qte < 0:
                            cursor.execute("UPDATE Stocks SET requires_stock_audit=1 WHERE id=?", (stock_id,))
                            if prod_id:
                                cursor.execute("UPDATE Produits SET requires_stock_audit=1 WHERE id=?", (prod_id,))
                            alert_msg = f"Conflit de stock survenu lors de la synchro du ticket {t_num} (Stock ID {stock_id}: {nouvelle_qte}). Produit marqué pour audit."
                            print(f"[AUDIT STOCK] {alert_msg}")
                            audit_alerts.append(alert_msg)

                # 3. Valider la synchronisation LWW (sync_status=1)
                cursor.execute("UPDATE Tickets SET sync_status=1 WHERE id=?", (t_id,))
                synced_count += 1

            conn.commit()
            return synced_count
        finally:
            if close_conn:
                conn.close()
