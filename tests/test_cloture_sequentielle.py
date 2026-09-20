# -*- coding: utf-8 -*-
"""
Tests unitaires pour la clôture séquentielle par lot (NF525) et l'arrondi belge dans les Z.
Vérifie qu'une accumulation de journées passées non clôturées est scellée jour par jour
sans jamais fusionner les ventes dans la journée en cours.
"""

import os
import sys
import tempfile
import unittest
import sqlite3
import datetime
from decimal import Decimal

# Racine du projet
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import initialiser_db, get_connection, enregistrer_cloture_caisse, lister_jours_non_clotures
from kodo_core.domain.accounting.z_report import ZReportEngine


class TestClotureSequentielle(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_cloture_seq.db")
        database_manager.DB_NAME = self.db_path
        initialiser_db()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def _creer_ticket(self, conn, date_str, numero, total_tvac=50.0, total_htva=41.32, total_tva=8.68, ecart_arrondi=0.02):
        c = conn.cursor()
        c.execute("""
            INSERT INTO Tickets (
                date_heure, caisse_id, vendeur_nom, id_client, total_tvac, total_htva, total_tva,
                remise, numero_ticket, methode_paiement, ecart_arrondi_cash, z_id
            ) VALUES (?, 'POS-01', 'Admin', NULL, ?, ?, ?, 0.0, ?, 'Espèces', ?, NULL)
        """, (f"{date_str} 14:30:00", total_tvac, total_htva, total_tva, numero, ecart_arrondi))
        tid = c.lastrowid
        c.execute("""
            INSERT INTO Ledger_Caisse (
                date_heure, caisse_id, type_mouvement, methode_paiement, montant, reference, z_id
            ) VALUES (?, 'POS-01', 'VENTE', 'Espèces', ?, ?, NULL)
        """, (f"{date_str} 14:30:00", total_tvac, numero))
        conn.commit()
        return tid

    def test_cloture_sequentielle_preserve_les_dates_et_stats(self):
        """Vérifie que 3 jours distincts créent 3 Z distincts scellés avec leurs dates respectives."""
        conn = get_connection()
        try:
            # 3 jours distincts passés
            self._creer_ticket(conn, "2026-09-10", "TK-0910-01", 100.0, 82.64, 17.36, 0.0)
            self._creer_ticket(conn, "2026-09-10", "TK-0910-02", 50.0, 41.32, 8.68, 0.0)

            self._creer_ticket(conn, "2026-09-11", "TK-0911-01", 80.0, 66.12, 13.88, 0.02)

            self._creer_ticket(conn, "2026-09-12", "TK-0912-01", 120.0, 99.17, 20.83, -0.01)

            # Vérifier lister_jours_non_clotures
            jours = lister_jours_non_clotures(caisse_id="POS-01", conn=conn)
            self.assertEqual(len(jours), 3)
            self.assertEqual(jours[0]["jour"], "2026-09-10")
            self.assertEqual(jours[0]["nb_tickets"], 2)
            self.assertEqual(jours[1]["jour"], "2026-09-11")
            self.assertEqual(jours[1]["nb_tickets"], 1)
            self.assertEqual(jours[2]["jour"], "2026-09-12")
            self.assertEqual(jours[2]["nb_tickets"], 1)

            # Exécuter la clôture séquentielle
            reports = ZReportEngine.close_all_pending_days_sequentially(caisse_id="POS-01", vendeur="Admin", conn=conn)
            self.assertEqual(len(reports), 3)

            # Vérifier que les 3 Z ont été scellés dans Clotures_Caisse
            c = conn.cursor()
            c.execute("SELECT id, total_ventes_tvac, total_tickets, periode_jusqu_au, total_arrondi_cash FROM Clotures_Caisse ORDER BY id ASC")
            clotures = c.fetchall()
            self.assertEqual(len(clotures), 3)

            # Z #1 (2026-09-10) : 2 tickets, 150€
            self.assertEqual(clotures[0][1], 150.0)
            self.assertEqual(clotures[0][2], 2)
            self.assertEqual(clotures[0][3], "2026-09-10")

            # Z #2 (2026-09-11) : 1 ticket, 80€, arrondi 0.02
            self.assertEqual(clotures[1][1], 80.0)
            self.assertEqual(clotures[1][2], 1)
            self.assertEqual(clotures[1][3], "2026-09-11")
            self.assertAlmostEqual(float(clotures[1][4]), 0.02, places=2)

            # Z #3 (2026-09-12) : 1 ticket, 120€, arrondi -0.01
            self.assertEqual(clotures[2][1], 120.0)
            self.assertEqual(clotures[2][2], 1)
            self.assertEqual(clotures[2][3], "2026-09-12")
            self.assertAlmostEqual(float(clotures[2][4]), -0.01, places=2)

            # Tous les tickets doivent désormais être associés à leur Z respectif
            c.execute("SELECT COUNT(*) FROM Tickets WHERE z_id IS NULL")
            self.assertEqual(c.fetchone()[0], 0)

            # Plus aucun jour non clôturé
            restants = lister_jours_non_clotures(caisse_id="POS-01", conn=conn)
            self.assertEqual(len(restants), 0)

        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
