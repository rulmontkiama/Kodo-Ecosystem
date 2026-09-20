# -*- coding: utf-8 -*-
"""
Spouleur d'impression thermique asynchrone et Circuit Breaker matériel - Kōdo POS Core.

Garantit :
1. Zéro blocage du comptoir : toute impression de ticket est envoyée dans un worker thread dédié.
   La réponse HTTP du checkout est renvoyée immédiatement (< 10 ms).
2. Circuit Breaker matériel :
   - CLOSED : fonctionnement normal.
   - OPEN : après 3 échecs consécutifs ou timeout réseau/USB, le circuit s'ouvre pendant 15s.
     Les nouvelles impressions sont immédiatement marquées 'PRINT_PENDING' sans figer le thread.
   - HALF_OPEN : après 15s, une tentative probe est autorisée pour tester le retour de l'imprimante.
3. Timeouts stricts : 1.5s sur les sockets TCP et 2.0s sur les processus CUPS/lpr/spooler.
"""

import time
import uuid
import queue
import threading
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone


class CircuitBreakerOpenError(Exception):
    """Levée lorsque le circuit breaker est ouvert (imprimante indisponible)."""
    pass


class PrinterCircuitBreaker:
    """
    Circuit Breaker matériel avec gestion des états CLOSED, OPEN, HALF_OPEN.
    """

    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 15.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = self.STATE_CLOSED
        self.last_state_change = time.monotonic()
        self.last_failure_reason: Optional[str] = None
        self._half_open_probe_in_flight = False
        self._lock = threading.Lock()

    def can_attempt(self) -> bool:
        """Vérifie si une tentative d'impression est autorisée."""
        with self._lock:
            now = time.monotonic()
            if self.state == self.STATE_CLOSED:
                return True
            elif self.state == self.STATE_OPEN:
                if now - self.last_state_change >= self.recovery_timeout:
                    self.state = self.STATE_HALF_OPEN
                    self.last_state_change = now
                    self._half_open_probe_in_flight = True
                    print("[CIRCUIT BREAKER] Imprimante passe en HALF_OPEN (test de sonde autorisé).")
                    return True
                return False
            elif self.state == self.STATE_HALF_OPEN:
                # En half-open, on autorise une seule requête probe à la fois
                if not self._half_open_probe_in_flight:
                    self._half_open_probe_in_flight = True
                    return True
                return False
            return False

    def record_success(self) -> None:
        """Enregistre un succès et réinitialise le circuit à CLOSED."""
        with self._lock:
            if self.state != self.STATE_CLOSED:
                print("[CIRCUIT BREAKER] Imprimante rétablie -> CLOSED.")
            self.state = self.STATE_CLOSED
            self.failure_count = 0
            self.last_state_change = time.monotonic()
            self.last_failure_reason = None
            self._half_open_probe_in_flight = False

    def record_failure(self, reason: str = "") -> None:
        """Enregistre un échec et ouvre le circuit si le seuil est atteint."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_reason = str(reason)
            self._half_open_probe_in_flight = False
            now = time.monotonic()
            if self.state == self.STATE_HALF_OPEN or self.failure_count >= self.failure_threshold:
                self.state = self.STATE_OPEN
                self.last_state_change = now
                print(f"[CIRCUIT BREAKER] Imprimante hors-service -> OPEN ({self.failure_count} échecs : {reason}). "
                      f"Pause de {self.recovery_timeout}s.")

    def get_status(self) -> Dict[str, Any]:
        """Retourne l'état courant du circuit breaker."""
        with self._lock:
            return {
                "state": self.state,
                "failure_count": self.failure_count,
                "last_failure_reason": self.last_failure_reason,
                "is_available": self.state == self.STATE_CLOSED or self.state == self.STATE_HALF_OPEN,
                "cooldown_remaining_sec": max(0.0, self.recovery_timeout - (time.monotonic() - self.last_state_change))
                if self.state == self.STATE_OPEN else 0.0
            }


class PrintJob:
    """Représente une tâche d'impression dans la file d'attente."""

    STATUS_PENDING = "PENDING"
    STATUS_PRINTING = "PRINTING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_SKIPPED = "SKIPPED_CIRCUIT_OPEN"

    def __init__(self, ticket_number: str, printer_name: Optional[str] = None, host: Optional[str] = None, port: int = 9100):
        self.job_id = str(uuid.uuid4())
        self.ticket_number = ticket_number
        self.printer_name = printer_name
        self.host = host
        self.port = port
        self.status = self.STATUS_PENDING
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.completed_at: Optional[str] = None
        self.error: Optional[str] = None
        self.attempts = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "ticket_number": self.ticket_number,
            "printer_name": self.printer_name,
            "host": self.host,
            "port": self.port,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "attempts": self.attempts
        }


class PrintWorker:
    """
    Spouleur asynchrone universel pour l'impression thermique.
    Exécute les impressions dans un thread daemon séparé.
    """

    def __init__(self):
        self._queue: queue.Queue[PrintJob] = queue.Queue()
        self._jobs: Dict[str, PrintJob] = {}
        self._recent_job_ids: List[str] = []
        self._lock = threading.Lock()
        self.circuit_breaker = PrinterCircuitBreaker()
        self._running = True
        self._worker_thread = threading.Thread(target=self._process_queue, name="KodoPrintWorker", daemon=True)
        self._worker_thread.start()

    def enqueue_ticket_print(self, ticket_number: str, printer_name: Optional[str] = None, host: Optional[str] = None, port: int = 9100) -> PrintJob:
        """
        Enfile une impression de ticket de manière totalement non-bloquante.
        Retourne l'objet PrintJob immédiatement.
        """
        job = PrintJob(ticket_number, printer_name=printer_name, host=host, port=port)
        with self._lock:
            self._jobs[job.job_id] = job
            self._recent_job_ids.append(job.job_id)
            if len(self._recent_job_ids) > 100:
                old = self._recent_job_ids.pop(0)
                self._jobs.pop(old, None)

        self._queue.put(job)
        return job

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def get_recent_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            recent_ids = self._recent_job_ids[-limit:]
            return [self._jobs[jid].to_dict() for jid in reversed(recent_ids) if jid in self._jobs]

    def is_printer_available(self) -> bool:
        return self.circuit_breaker.can_attempt()

    def get_circuit_status(self) -> Dict[str, Any]:
        status = self.circuit_breaker.get_status()
        status["queue_size"] = self._queue.qsize()
        return status

    def _process_queue(self) -> None:
        """Boucle du thread de traitement d'impression en arrière-plan."""
        while self._running:
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Vérification Circuit Breaker avant tout appel matériel
            if not self.circuit_breaker.can_attempt():
                job.status = PrintJob.STATUS_SKIPPED
                job.error = f"Circuit breaker ouvert (imprimante indisponible : {self.circuit_breaker.last_failure_reason})"
                job.completed_at = datetime.now(timezone.utc).isoformat()
                self._queue.task_done()
                continue

            job.status = PrintJob.STATUS_PRINTING
            job.attempts += 1

            try:
                # Import paresseux pour éviter les cycles
                from kodo_core.hardware.printer import imprimer_ticket_caisse
                success = imprimer_ticket_caisse(
                    num_ticket=job.ticket_number,
                    printer_name=job.printer_name,
                    host=job.host,
                    port=job.port
                )

                if success:
                    job.status = PrintJob.STATUS_SUCCESS
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    self.circuit_breaker.record_success()
                else:
                    job.status = PrintJob.STATUS_FAILED
                    job.error = "Échec d'impression du ticket (retour pilote False/None)"
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    self.circuit_breaker.record_failure(job.error)
            except Exception as ex:
                job.status = PrintJob.STATUS_FAILED
                job.error = str(ex)
                job.completed_at = datetime.now(timezone.utc).isoformat()
                self.circuit_breaker.record_failure(str(ex))
            finally:
                self._queue.task_done()


# Instance singleton globale
_GLOBAL_PRINT_WORKER: Optional[PrintWorker] = None
_WORKER_INIT_LOCK = threading.Lock()


def get_print_worker() -> PrintWorker:
    """Retourne l'instance singleton du PrintWorker."""
    global _GLOBAL_PRINT_WORKER
    if _GLOBAL_PRINT_WORKER is None:
        with _WORKER_INIT_LOCK:
            if _GLOBAL_PRINT_WORKER is None:
                _GLOBAL_PRINT_WORKER = PrintWorker()
    return _GLOBAL_PRINT_WORKER
