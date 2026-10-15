# -*- coding: utf-8 -*-
"""
Tests d'atomicité transactionnelle absolue (NF525) de la vente et du scellement fiscal.

Valide que le scellement fiscal (seal_sale) et l'enregistrement de la vente s'exécutent
sous la MÊME transaction atomique (BEGIN IMMEDIATE -> commit unique).
Tout échec ou interruption avant le commit annule l'intégralité des opérations :
aucun ticket orphelin, aucun stock décrémenté, aucun mouvement de caisse fantôme.
"""

import os
import sys
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database_manager
from kodo_core.domain.sales.cart_engine import process_sale_transaction
from kodo_core.services.fiscal_service import audit_ledger_integrity


class TestFiscalSaleAtomicity(unittest.TestCase):

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()

        # Initialisation du schéma fiscal anti-fraude
        from kodo_core.services.fiscal_service import ensure_schema
        conn_init = database_manager.get_connection()
        try:
            ensure_schema(conn_init)
        finally:
            conn_init.close()

        # Création d'un produit avec stock initial de 10
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva, code_barre) VALUES ('Robe Lin', 100.00, 0.21, 'ROBE-001')"
            )
            self.prod_id = c.lastrowid
            c.execute(
                "INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)",
                (self.prod_id,),
            )
            self.stock_id = c.lastrowid
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def test_nominal_sale_and_seal_are_atomic_and_linked(self):
        """Vérifie qu'une vente normale enregistre le ticket ET scelle le fiscal_ledger sous le même identifiant."""
        cart_items = [
            {
                "stock_id": self.stock_id,
                "quantite": 1,
                "prix_vente_tvac": 100.00,
            }
        ]
        payments = [("CB", 100.00)]

        res = process_sale_transaction(
            cart_items=cart_items,
            payments=payments,
            total_tvac=100.00,
            cashier_name="Alice",
        )

        self.assertTrue(res["success"])
        ticket_id = res["ticket_id"]

        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            # 1. Le ticket existe
            c.execute("SELECT id, total_tvac, total_htva, total_tva FROM Tickets WHERE id = ?", (ticket_id,))
            tck = c.fetchone()
            self.assertIsNotNone(tck)

            # 2. Le scellement fiscal existe et pointe sur ce ticket
            c.execute("SELECT id, sequence_number, sale_id, total_ttc FROM fiscal_ledger WHERE sale_id = ?", (ticket_id,))
            fisc = c.fetchone()
            self.assertIsNotNone(fisc)
            self.assertEqual(fisc[2], ticket_id)
            self.assertEqual(Decimal(str(fisc[3])), Decimal("100.00"))

            # 3. La chaîne est intègre
            self.assertTrue(audit_ledger_integrity(conn))

            # 4. Le stock a bien été décrémenté
            c.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (self.stock_id,))
            self.assertEqual(c.fetchone()[0], 9)
        finally:
            conn.close()

    def test_crash_during_seal_sale_rolls_back_everything_leaving_zero_orphans(self):
        """Si le scellement fiscal échoue (crash, panne ou exception), toute la vente est annulée (conn.rollback()).

        Aucun ticket orphelin dans Tickets, aucune ligne dans Ventes_Details, aucun mouvement
        dans Ledger_Caisse, et le stock reste strictement intact.
        """
        cart_items = [
            {
                "stock_id": self.stock_id,
                "quantite": 2,
                "prix_vente_tvac": 100.00,
            }
        ]
        payments = [("CB", 200.00)]

        with patch("kodo_core.services.fiscal_service.seal_sale", side_effect=RuntimeError("SIMULATED DISK ERROR DURING SEAL")):
            with self.assertRaises(RuntimeError):
                process_sale_transaction(
                    cart_items=cart_items,
                    payments=payments,
                    total_tvac=200.00,
                    cashier_name="Bob",
                )

        # Vérification d'intégrité absolue après l'échec
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM Tickets")
            self.assertEqual(c.fetchone()[0], 0, "Aucun ticket ne doit subsister après échec du scellement")

            c.execute("SELECT COUNT(*) FROM Ventes_Details")
            self.assertEqual(c.fetchone()[0], 0, "Aucune ligne de détail ne doit subsister")

            c.execute("SELECT COUNT(*) FROM Ledger_Caisse")
            self.assertEqual(c.fetchone()[0], 0, "Aucun mouvement de caisse ne doit subsister")

            c.execute("SELECT COUNT(*) FROM fiscal_ledger")
            self.assertEqual(c.fetchone()[0], 0, "Aucun enregistrement fiscal partiel ne doit subsister")

            c.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (self.stock_id,))
            self.assertEqual(c.fetchone()[0], 10, "Le stock doit être strictement intact (10 unités)")
        finally:
            conn.close()

    def test_simulated_process_kill_before_commit_leaves_zero_persisted_records(self):
        """Simulation d'une coupure brutale (kill -9 / panne de courant) entre l'écriture et le commit."""
        conn1 = database_manager.get_connection()
        c1 = conn1.cursor()
        c1.execute("BEGIN IMMEDIATE")

        # Écriture partielle simulant un travail en cours
        ticket_id = database_manager.enregistrer_vente(
            cursor=c1,
            numero_ticket="TCK-SIMUL-KILL",
            total_tvac=100.00,
            total_htva=82.64,
            total_tva=17.36,
            remise=0.0,
            methode_paiement="CB",
            id_client=None,
            rendu_monnaie=0.0,
            panier=[{"stock_id": self.stock_id, "prix_vente_tvac": 100.00, "quantite": 1}],
            vendeur_nom="CrashTester",
            date_heure="2026-10-15 15:00:00",
            paiements=[("CB", 100.00)],
        )
        self.assertIsNotNone(ticket_id)

        # FERMETURE BRUTALE SANS COMMIT (simulation d'interruption brutale du process)
        conn1.close()

        # Réouverture d'une nouvelle connexion fraîche (redémarrage de l'application)
        conn2 = database_manager.get_connection()
        try:
            c2 = conn2.cursor()
            c2.execute("SELECT COUNT(*) FROM Tickets WHERE numero_ticket = 'TCK-SIMUL-KILL'")
            self.assertEqual(c2.fetchone()[0], 0, "La transaction non commitée a été annulée par SQLite WAL")

            c2.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (self.stock_id,))
            self.assertEqual(c2.fetchone()[0], 10, "Le stock est intact")
        finally:
            conn2.close()

    def test_persist_receipt_requires_active_transaction(self):
        """persist_receipt doit refuser d'exécuter si aucune transaction n'est ouverte."""
        from kodo_core.domain.sales.models import FinancialReceipt
        from kodo_core.domain.sales.persistence import SalePersistenceContext, persist_receipt

        conn = database_manager.get_connection()
        try:
            cursor = conn.cursor()
            receipt = FinancialReceipt(
                total_tvac=Decimal("50.00"),
                total_htva=Decimal("41.32"),
                total_tva=Decimal("8.68"),
                remise=Decimal("0.00"),
                rendu=Decimal("0.00"),
                ecart_arrondi_cash=Decimal("0.00"),
                payments=[("CB", Decimal("50.00"))],
            )
            ctx = SalePersistenceContext(
                numero_ticket="TCK-NO-TXN",
                now_local="2026-10-15 15:00:00",
                now_utc_iso="2026-10-15T13:00:00Z",
                lines=[],
                main_method="CB",
            )
            with self.assertRaises(RuntimeError) as cm:
                persist_receipt(cursor, receipt, ctx)
            self.assertIn("BEGIN IMMEDIATE", str(cm.exception))
        finally:
            conn.close()

    def test_persist_receipt_nominal_within_active_transaction(self):
        """persist_receipt écrit ticket et scellement dans la transaction de l'appelant sans la fermer."""
        from kodo_core.domain.sales.models import FinancialReceipt
        from kodo_core.domain.sales.persistence import SalePersistenceContext, persist_receipt

        conn = database_manager.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            receipt = FinancialReceipt(
                total_tvac=Decimal("100.00"),
                total_htva=Decimal("82.64"),
                total_tva=Decimal("17.36"),
                remise=Decimal("0.00"),
                rendu=Decimal("0.00"),
                ecart_arrondi_cash=Decimal("0.00"),
                payments=[("CB", Decimal("100.00"))],
            )
            ctx = SalePersistenceContext(
                numero_ticket="TCK-PERSIST-PORT",
                now_local="2026-10-15 15:00:00",
                now_utc_iso="2026-10-15T13:00:00Z",
                lines=[{"stock_id": self.stock_id, "prix_vente_tvac": 100.00, "quantite": 1}],
                main_method="CB",
            )

            ticket_id = persist_receipt(cursor, receipt, ctx)
            self.assertIsNotNone(ticket_id)
            # La transaction doit être toujours active !
            self.assertTrue(conn.in_transaction)

            conn.commit()

            # Vérification post-commit
            c = conn.cursor()
            c.execute("SELECT total_tvac FROM Tickets WHERE id = ?", (ticket_id,))
            self.assertEqual(c.fetchone()[0], 100.00)

            c.execute("SELECT total_ttc FROM fiscal_ledger WHERE sale_id = ?", (ticket_id,))
            self.assertEqual(Decimal(str(c.fetchone()[0])), Decimal("100.00"))

            self.assertTrue(audit_ledger_integrity(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
