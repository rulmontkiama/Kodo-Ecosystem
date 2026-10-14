# -*- coding: utf-8 -*-
"""
Kōdo POS — Retours : registre fiscal scellé et numérotation des tickets.

1. Le journal fiscal inaltérable ne recevait que les ventes de caisse : un retour (ticket négatif)
   n'y figurait pas, la somme scellée dépassait le chiffre d'affaires réel du montant des retours.
2. `generer_numero_ticket` lisait « 026 » dans « REF-TCK-2026-… » : un retour fait avant le 26e ticket
   de l'année faisait sauter la numérotation (…0002 puis 0027), un trou impossible à combler après coup.
"""

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _BaseDB(unittest.TestCase):

    def setUp(self):
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()
        self.conn = sqlite3.connect(self.path)
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Robe', 60.50, 0.21)")
        pid = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 50)", (pid,))
        self.stock_id = c.lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _q(self, sql, *params):
        return self.conn.execute(sql, params).fetchall()

    def _vente(self, paiements=None):
        from kodo_core.domain.sales.cart_engine import process_sale_transaction
        return process_sale_transaction(
            cart_items=[{"stock_id": self.stock_id, "quantite": 1}], total_tvac=60.50,
            payments=paiements or [("Espèces", 60.50)], cashier_name="Julie")

    def _retour(self, numero, mode="Espèces"):
        from kodo_core.domain.sales.cart_engine import process_return_transaction
        vd_id = self._q("SELECT vd.id FROM Ventes_Details vd JOIN Tickets t ON t.id = vd.id_ticket "
                        "WHERE t.numero_ticket = ? AND vd.quantite > 0", numero)[0][0]
        return process_return_transaction(
            original_ticket_number=numero, sales_detail_id=vd_id, stock_id=self.stock_id,
            refund_price=0.0, refund_mode=mode, cashier_name="Julie", quantity=1)


class TestRegistreFiscalDesRetours(_BaseDB):

    def test_un_retour_est_scelle_avec_ses_montants_negatifs(self):
        vente = self._vente()
        retour = self._retour(vente["numero_ticket"])
        lignes = self._q("SELECT sale_id, total_ttc, total_ht, total_tva FROM fiscal_ledger ORDER BY id")
        self.assertEqual(len(lignes), 2)
        id_retour = self._q("SELECT id FROM Tickets WHERE numero_ticket = ?", retour["refund_ticket_number"])[0][0]
        self.assertEqual(lignes[1][0], id_retour)
        self.assertEqual([Decimal(str(v)) for v in lignes[1][1:]],
                         [Decimal("-60.50"), Decimal("-50.00"), Decimal("-10.50")])

    def test_la_somme_scellee_rejoint_le_chiffre_d_affaires_net(self):
        a = self._vente()
        self._vente()
        self._retour(a["numero_ticket"], "Avoir")
        scelle = sum(Decimal(str(r[0])) for r in self._q("SELECT total_ttc FROM fiscal_ledger"))
        net = sum(Decimal(str(r[0])) for r in self._q("SELECT total_tvac FROM Tickets"))
        self.assertEqual(scelle, net)
        self.assertEqual(net, Decimal("60.50"))

    def test_la_chaine_scellee_reste_verifiable(self):
        a = self._vente()
        self._retour(a["numero_ticket"])
        from kodo_core.services.fiscal_service import audit_ledger_integrity
        self.assertTrue(audit_ledger_integrity(self.conn))

    def test_sceller_deux_fois_ne_duplique_rien(self):
        a = self._vente()
        retour = self._retour(a["numero_ticket"])
        from kodo_core.services.fiscal_service import sceller_ticket
        self.assertIsNone(sceller_ticket(self.conn, retour["refund_ticket_number"]))
        self.assertEqual(self._q("SELECT COUNT(*) FROM fiscal_ledger"), [(2,)])

    def test_un_incident_de_scellement_ne_defait_pas_le_remboursement(self):
        a = self._vente()
        with mock.patch("kodo_core.services.fiscal_service.seal_sale", side_effect=RuntimeError("disque plein")):
            retour = self._retour(a["numero_ticket"])
        self.assertTrue(retour["success"])
        self.assertEqual(self._q("SELECT COUNT(*) FROM Tickets WHERE numero_ticket LIKE 'REF-%'"), [(1,)])
        en_attente = self._q("SELECT total_ttc FROM fiscal_unsealed_sales")
        self.assertEqual([Decimal(str(r[0])) for r in en_attente], [Decimal("-60.50")])
        # Au redémarrage, le retour en attente est rescellé.
        from kodo_core.services.fiscal_service import reseal_pending_sales
        self.assertEqual(len(reseal_pending_sales(self.conn)), 1)
        self.assertEqual(self._q("SELECT COUNT(*) FROM fiscal_ledger"), [(2,)])
        self.assertEqual(self._q("SELECT COUNT(*) FROM fiscal_unsealed_sales"), [(0,)])


class TestNumerotation(_BaseDB):

    def _numero_suivant(self):
        c = self.conn.cursor()
        numero = self.dm.generer_numero_ticket(c)
        self.conn.rollback()
        return numero

    def test_un_retour_en_debut_d_annee_ne_fait_pas_sauter_la_numerotation(self):
        annee = datetime.datetime.now().year
        a = self._vente()
        b = self._vente()
        self.assertEqual(a["numero_ticket"], f"TCK-{annee}-0001")
        self.assertEqual(b["numero_ticket"], f"TCK-{annee}-0002")
        self._retour(a["numero_ticket"])
        c = self._vente()
        self.assertEqual(c["numero_ticket"], f"TCK-{annee}-0003")

    def test_plusieurs_retours_puis_ventes_restent_consecutifs(self):
        annee = datetime.datetime.now().year
        numeros = [self._vente()["numero_ticket"] for _ in range(3)]
        for numero in numeros[:2]:
            self._retour(numero)
        suivant = self._vente()["numero_ticket"]
        self.assertEqual(suivant, f"TCK-{annee}-0004")

    def test_les_commandes_web_et_autres_prefixes_ne_comptent_pas(self):
        annee = datetime.datetime.now().year
        c = self.conn.cursor()
        for numero in (f"SHPF-{annee}9999", "REF-TCK-2099-0001-12036", "TKT-0012345678", f"TCK-{annee - 1}-0500"):
            c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac) VALUES (?, ?, 1)",
                      (numero, f"{annee}-01-02 10:00:00"))
        self.conn.commit()
        self.assertEqual(self._numero_suivant(), f"TCK-{annee}-0001")

    def test_la_sequence_continue_apres_un_numero_eleve(self):
        annee = datetime.datetime.now().year
        c = self.conn.cursor()
        c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac) VALUES (?, ?, 1)",
                  (f"TCK-{annee}-0041", f"{annee}-01-02 10:00:00"))
        self.conn.commit()
        self.assertEqual(self._numero_suivant(), f"TCK-{annee}-0042")


if __name__ == "__main__":
    unittest.main()
