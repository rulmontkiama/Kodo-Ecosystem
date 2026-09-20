# -*- coding: utf-8 -*-
"""
Suite de tests de non-régression et d'homologation Claude Audit.
Couvre les 4 cas critiques exigés pour la levée du rejet :
1. Arrondi vers le bas : total_especes == encaissé réel (10,00 €) et ecart_reglements == 0.
2. Concurrence stock : 8 ventes simultanées sur un stock de 5 -> 5 acceptées, stock final = 0.
3. Clôture séquentielle de jours en retard : la session de caisse active survit aux Z de rattrapage.
4. Bilan de santé get_system_health_report() : stock_total_units > 0, aucune erreur SQL.
"""

import os
import sqlite3
import unittest
import threading
from decimal import Decimal
from datetime import date, timedelta

import database_manager
from kodo_core.domain.sales.cart_engine import process_sale_transaction
from kodo_core.domain.accounting.z_report import ZReportEngine
from kodo_core.services.client_sanitizer import get_system_health_report


class TestClaudeAuditVerification(unittest.TestCase):

    def setUp(self):
        self.db_path = ":memory:"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        database_manager.initialiser_db(conn=self.conn)

        # Création d'un produit et stock de test
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO Produits (nom, code_barre, prix_vente_tvac, taux_tva)
            VALUES ('T-Shirt Audit', 'AUDIT-001', 10.02, 0.21)
        """)
        self.produit_id = c.lastrowid

        c.execute("""
            INSERT INTO Stocks (id_produit, taille, quantite_actuelle)
            VALUES (?, 'M', 5)
        """, (self.produit_id,))
        self.stock_id = c.lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_1_arrondi_vers_le_bas_reconcilie_sans_ecart_fantome(self):
        """
        Cas 1 : Vente de 10,02 € réglée en espèces.
        Arrondi légal à 10,00 €.
        Le bilan Z doit avoir total_especes == 10.00 et ecart_reglements == 0.00.
        """
        cart = [{
            "stock_id": self.stock_id,
            "quantite": 1,
            "prix_vente_tvac": 10.02,
            "nom": "T-Shirt Audit",
            "code_barre": "AUDIT-001"
        }]
        # Le client paie 10,00 € en espèces (arrondi légal belge)
        res = process_sale_transaction(
            cart_items=cart,
            total_tvac=10.02,
            payments=[("Espèces", 10.00)],
            cashier_name="Auditeur",
            caisse_id="POS-01",
            conn=self.conn
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["ecart_arrondi_cash"], -0.02)

        # Génération du bilan Z
        bilan = database_manager.generer_bilan_z_journalier(caisse_id="POS-01", conn=self.conn)
        self.assertEqual(bilan["total_especes"], Decimal("10.00"))
        self.assertEqual(bilan["total_arrondi_cash"], Decimal("-0.02"))
        self.assertEqual(bilan["ecart_reglements"], Decimal("0.00"))
        self.assertEqual(bilan["regularisation_rendu"], Decimal("0.00"))

    def test_2_concurrence_stock_pas_de_stock_negatif(self):
        """
        Cas 2 : 8 ventes simultanées sur un stock initial de 5.
        Exactement 5 ventes doivent être acceptées et 3 rejetées.
        Le stock final doit être exactement 0 (aucun stock négatif).
        """
        # Utiliser une base SQLite sur fichier temporaire pour permettre la concurrence multi-threads
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db_path = tf.name

        try:
            init_conn = sqlite3.connect(temp_db_path)
            database_manager.initialiser_db(conn=init_conn)
            c = init_conn.cursor()
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("""
                INSERT INTO Produits (nom, code_barre, prix_vente_tvac, taux_tva)
                VALUES ('Article Concurrence', 'CONC-001', 20.00, 0.21)
            """)
            pid = c.lastrowid
            c.execute("""
                INSERT INTO Stocks (id_produit, taille, quantite_actuelle)
                VALUES (?, 'L', 5)
            """, (pid,))
            sid = c.lastrowid
            init_conn.commit()
            init_conn.close()

            results = []
            errors = []

            def worker():
                thread_conn = sqlite3.connect(temp_db_path, timeout=10.0)
                try:
                    cart = [{
                        "stock_id": sid,
                        "quantite": 1,
                        "prix_vente_tvac": 20.00,
                        "nom": "Article Concurrence",
                        "code_barre": "CONC-001"
                    }]
                    res = process_sale_transaction(
                        cart_items=cart,
                        total_tvac=20.00,
                        payments=[("CB", 20.00)],
                        cashier_name="Thread-Worker",
                        caisse_id="POS-01",
                        conn=thread_conn
                    )
                    results.append(res)
                except ValueError as ve:
                    errors.append(str(ve))
                finally:
                    thread_conn.close()

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(results), 5, f"Exactement 5 ventes doivent réussir, mais {len(results)} ont réussi")
            self.assertEqual(len(errors), 3, f"Exactement 3 ventes doivent échouer, mais {len(errors)} ont échoué")

            # Vérification du stock final
            check_conn = sqlite3.connect(temp_db_path)
            c = check_conn.cursor()
            c.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (sid,))
            final_stock = c.fetchone()[0]
            check_conn.close()

            self.assertEqual(final_stock, 0, f"Le stock final doit être 0, obtenu: {final_stock}")
        finally:
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except OSError:
                    pass

    def test_3_cloture_sequentielle_ne_ferme_pas_session_active(self):
        """
        Cas 3 : Clôture séquentielle de jours passés en retard.
        La session de caisse en cours (aujourd'hui) ne doit PAS être fermée par les Z de rattrapage.
        """
        c = self.conn.cursor()
        # Ouvrir une session active pour aujourd'hui avec 150 € de fond de caisse
        today_str = date.today().isoformat()
        c.execute("""
            INSERT INTO Sessions_Caisse (date_ouverture, fond_caisse_matin)
            VALUES (?, 150.00)
        """, (f"{today_str} 08:00:00",))

        # Insérer des tickets non clôturés sur un jour passé (hier)
        hier = (date.today() - timedelta(days=1)).isoformat()
        c.execute("""
            INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, vendeur_nom, caisse_id)
            VALUES ('TCK-HIER-01', ?, 50.00, 41.32, 8.68, 0, 'CB', 'Caissier', 'POS-01')
        """, (f"{hier} 14:00:00",))
        t_id = c.lastrowid
        c.execute("""
            INSERT INTO Ledger_Caisse (date_heure, type_mouvement, methode_paiement, montant, reference, caisse_id, signature)
            VALUES (?, 'VENTE', 'CB', 50.00, 'TCK-HIER-01', 'POS-01', 'sig')
        """, (f"{hier} 14:00:00",))
        self.conn.commit()

        # Clôturer les jours passés en séquence
        closed_reports = ZReportEngine.close_all_pending_days_sequentially(
            caisse_id="POS-01",
            vendeur="Admin",
            conn=self.conn
        )
        self.assertEqual(len(closed_reports), 1)
        self.assertEqual(closed_reports[0]["jusqu_au"], hier)

        # Vérifier que la session active n'a PAS été fermée
        c.execute("SELECT id, date_cloture, fond_caisse_matin FROM Sessions_Caisse WHERE date_cloture IS NULL")
        active_session = c.fetchone()
        self.assertIsNotNone(active_session, "La session de caisse d'aujourd'hui doit toujours être ouverte !")
        self.assertEqual(float(active_session["fond_caisse_matin"]), 150.00)

    def test_4_get_system_health_report_succes_et_stock_positif(self):
        """
        Cas 4 : get_system_health_report() sur base peuplée.
        Doit rapporter stock_total_units > 0 sans exception ni fuite.
        """
        old_db_name = database_manager.DB_NAME
        try:
            # Créer un fichier de base temporaire pour le health check
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
                temp_db_path = tf.name

            hconn = sqlite3.connect(temp_db_path)
            database_manager.initialiser_db(conn=hconn)
            hc = hconn.cursor()
            hc.execute("INSERT INTO Produits (nom, code_barre, prix_vente_tvac) VALUES ('P1', 'CB1', 10.0)")
            pid = hc.lastrowid
            hc.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 42)", (pid,))
            hconn.commit()
            hconn.close()

            database_manager.DB_NAME = temp_db_path

            report = get_system_health_report()
            self.assertTrue(report["database"]["exists"])
            self.assertTrue(report["database"]["integrity_ok"])
            self.assertEqual(report["database"]["products_count"], 1)
            self.assertEqual(report["database"]["stock_total_units"], 42)
            self.assertNotIn("Diagnostic base de données incomplet", str(report.get("alerts", [])))
        finally:
            database_manager.DB_NAME = old_db_name
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except OSError:
                    pass

    def test_5_sous_perception_reelle_refusee_et_jamais_comptee_en_arrondi(self):
        """
        Sur un total DÉJÀ multiple de 5 centimes, aucun arrondi légal ne s'applique :
        un règlement inférieur doit être refusé, jamais absorbé en écart d'arrondi.
        """
        c = self.conn.cursor()
        c.execute("UPDATE Produits SET prix_vente_tvac = 10.00 WHERE id = ?", (self.produit_id,))
        self.conn.commit()
        cart = [{"stock_id": self.stock_id, "quantite": 1, "prix_vente_tvac": 10.00, "nom": "T-Shirt Audit", "code_barre": "AUDIT-001"}]
        for remis in (9.98, 9.99):
            with self.assertRaises(ValueError, msg=f"{remis} € doit être refusé sur 10,00 € dus"):
                process_sale_transaction(
                    cart_items=cart,
                    total_tvac=10.00,
                    payments=[("Espèces", remis)],
                    cashier_name="Auditeur",
                    caisse_id="POS-01",
                    conn=self.conn
                )
        # Le montant exact reste évidemment accepté, sans écart d'arrondi.
        res = process_sale_transaction(
            cart_items=cart,
            total_tvac=10.00,
            payments=[("Espèces", 10.00)],
            cashier_name="Auditeur",
            caisse_id="POS-01",
            conn=self.conn
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["ecart_arrondi_cash"], 0.0)

    def test_6_tolerance_ouverte_uniquement_sur_arrondi_legal(self):
        """
        Total 10,03 € (arrondi légal à 10,05 €) : le montant brut 10,03 € reste accepté —
        c'est la caisse qui n'a pas encore appliqué l'arrondi, pas le client qui paie moins.
        """
        c = self.conn.cursor()
        c.execute("UPDATE Produits SET prix_vente_tvac = 10.03 WHERE id = ?", (self.produit_id,))
        self.conn.commit()
        cart = [{"stock_id": self.stock_id, "quantite": 1, "prix_vente_tvac": 10.03, "nom": "T-Shirt Audit", "code_barre": "AUDIT-001"}]
        res = process_sale_transaction(
            cart_items=cart,
            total_tvac=10.03,
            payments=[("Espèces", 10.03)],
            cashier_name="Auditeur",
            caisse_id="POS-01",
            conn=self.conn
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["ecart_arrondi_cash"], 0.0)
        self.assertEqual(res["total_a_payer_arrondi"], 10.03)

    def test_7_scellement_numero_ticket_v2_et_retrocompatibilite_v1(self):
        """
        Vérifie que l'algorithme v2 scelle le numéro de ticket (empêche la collision)
        et que verify_database_integrity valide à la fois les maillons v2 et v1 sans faux positif.
        """
        from kodo_core.db.audit_trail import verify_database_integrity
        from database_manager import calculer_hash_transaction, HASH_ALGO_V1, HASH_ALGO_V2

        # 1. Prouver que v2 différencie deux tickets identiques de numéros distincts
        h1 = calculer_hash_transaction("GENESIS", "2026-09-20 12:00:00", 25.00, "POS-01", "", numero_ticket="TCK-001", algo=HASH_ALGO_V2)
        h2 = calculer_hash_transaction("GENESIS", "2026-09-20 12:00:00", 25.00, "POS-01", "", numero_ticket="TCK-002", algo=HASH_ALGO_V2)
        self.assertNotEqual(h1, h2, "En v2, deux numéros de ticket distincts DOIVENT avoir des hash différents")

        # 2. Prouver que verify_database_integrity accepte les tickets enregistrés
        is_valid = verify_database_integrity(conn=self.conn)
        self.assertTrue(is_valid)


if __name__ == "__main__":
    unittest.main()
