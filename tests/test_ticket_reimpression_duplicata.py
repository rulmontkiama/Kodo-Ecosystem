# -*- coding: utf-8 -*-
"""
Kōdo POS — une réimpression porte la date d'ORIGINE et la mention DUPLICATA, et un rendu de monnaie se lit juste.

Revue du 2026-10-14 (`printer.py`) : la réimpression imprimait la date du JOUR (la copie passait pour un
original d'aujourd'hui) sans aucune mention de duplicata ; et « PAID BY ESPECES 15.00 / CHANGE RETURNED 5.00 »
laissait croire que 15 € avaient été remis ET 5 € rendus, alors que le grand livre est net du rendu (20 € remis).
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db, get_connection, enregistrer_vente  # noqa: E402
from kodo_core.hardware import printer  # noqa: E402

PANIER = [{"nom": "Robe", "taille": "M", "quantite": 1, "prix_vente_tvac": Decimal("15.00"),
           "taux_tva": Decimal("0.21"), "stock_id": None}]


class TestTicketReimpression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "impr.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()
        conn = get_connection()
        try:
            c = conn.cursor()
            enregistrer_vente(c, "TCK-2026-0001", Decimal("15.00"), Decimal("12.40"), Decimal("2.60"),
                              Decimal("0.00"), "Espèces", None, Decimal("5.00"), PANIER, "Léa",
                              "2026-03-02 14:35:00", [("Espèces", Decimal("20.00"))])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        database_manager.DB_NAME = self._origine
        os.environ.pop("KODO_DB_PATH", None)
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass

    def _contenu(self, **kw):
        capture = {}

        def faux(contenu, *a, **k):
            capture["texte"] = contenu
            return True
        with mock.patch.object(printer, "imprimer_ticket", side_effect=faux):
            self.assertTrue(printer.imprimer_ticket_caisse("TCK-2026-0001", **kw))
        return capture["texte"]

    def test_le_ticket_porte_sa_date_d_origine(self):
        texte = self._contenu()
        self.assertIn("02/03/2026 14:35", texte)

    def test_une_reimpression_porte_la_mention_duplicata(self):
        self.assertIn("DUPLICATA", self._contenu(duplicata=True))
        self.assertNotIn("DUPLICATA", self._contenu())

    def test_le_montant_remis_est_affiche_avec_le_rendu(self):
        texte = self._contenu()
        ligne = next(l for l in texte.splitlines() if "PAID BY ESPECES" in l)
        self.assertIn("20.00", ligne, "20 € remis (15 dus + 5 rendus), pas 15")
        self.assertIn("5.00", next(l for l in texte.splitlines() if "CHANGE RETURNED" in l))

    def test_le_travail_d_impression_transporte_le_duplicata(self):
        from kodo_core.hardware.print_worker import PrintJob, PrintWorker
        w = PrintWorker.__new__(PrintWorker)
        import threading, queue
        w._lock = threading.Lock()
        w._jobs, w._recent_job_ids, w._queue = {}, [], queue.Queue()
        self.assertEqual(w.enqueue_ticket_print("TCK-1", duplicata=True).payload, {"duplicata": True})
        self.assertEqual(w.enqueue_ticket_print("TCK-1").payload, {})


if __name__ == "__main__":
    unittest.main()


class TestReprisesDisjoncteur(unittest.TestCase):
    """Un ticket jamais tenté (disjoncteur ouvert) est remis en file, puis abandonné visiblement."""

    def _worker(self):
        import queue
        import threading
        from kodo_core.hardware.print_worker import PrintWorker
        w = PrintWorker.__new__(PrintWorker)
        w._lock = threading.Lock()
        w._jobs, w._recent_job_ids, w._queue = {}, [], queue.Queue()
        w.circuit_breaker = mock.Mock(recovery_timeout=15.0, last_failure_reason="papier")
        return w

    def test_remis_en_file_puis_abandonne_apres_le_plafond(self):
        from kodo_core.hardware.print_worker import PrintJob
        w = self._worker()
        job = PrintJob("TCK-1")
        with mock.patch("kodo_core.hardware.print_worker.threading.Timer") as timer:
            for _ in range(w._REPRISES_MAX):
                self.assertTrue(w._replanifier(job))
                self.assertEqual(job.status, PrintJob.STATUS_PENDING)
            self.assertEqual(timer.call_count, w._REPRISES_MAX)
            self.assertFalse(w._replanifier(job), "au-delà du plafond, l'échec devient visible")

    def test_un_travail_deja_tente_n_est_jamais_rejoue(self):
        from kodo_core.hardware.print_worker import PrintJob
        w = self._worker()
        job = PrintJob("TCK-1")
        job.attempts = 1
        self.assertFalse(w._replanifier(job), "risque de double impression")
