# -*- coding: utf-8 -*-
"""
Kōdo POS — la ventilation TVA du Z reste juste après le retour d'un ticket REMISÉ.

Défaut de la revue du 2026-10-14 (`z_report.py:90`, apparu en 2.1.0) : un ticket de remboursement porte des
lignes de quantité négative ; le brut de ce ticket est donc négatif et le ratio net/brut était forcé à 1 dès
`gross_total <= 0`. La ligne remboursée était ventilée à son prix AVANT remise : 100 € de brut au lieu des
90 € réellement rendus. Le Z affichait alors une ventilation TVA qui ne justifiait pas ses propres totaux
scellés (la vente remisée à +90, le retour à -100).
"""
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import enregistrer_remboursement, initialiser_db
from kodo_core.domain.accounting.z_report import ZReportEngine


class TestZVentilationApresRetourRemise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        database_manager.DB_NAME = os.path.join(self.tmp, "kodo_pos.db")
        initialiser_db()
        self.conn = database_manager.get_connection()
        self.c = self.conn.cursor()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _produit(self, nom, taux, prix):
        self.c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES (?, ?, ?)", (nom, prix, taux))
        pid = self.c.lastrowid
        self.c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'U', 5)", (pid,))
        return self.c.lastrowid

    def _ticket_remise(self, numero, lignes, total_paye):
        """`lignes` = [(id_stock, prix brut, taux)] ; `total_paye` = total APRÈS remise globale."""
        total = Decimal(str(total_paye))
        htva = (total / Decimal("1.21")).quantize(Decimal("0.01"))
        self.c.execute(
            "INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, caisse_id, "
            "methode_paiement) VALUES (?, '2026-10-10 10:00:00', ?, ?, ?, 'POS-01', 'Espèces')",
            (numero, str(total), str(htva), str(total - htva)))
        tid = self.c.lastrowid
        vds = []
        for id_stock, prix, _taux in lignes:
            self.c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) "
                           "VALUES (?, ?, 1, ?)", (tid, id_stock, str(prix)))
            vds.append(self.c.lastrowid)
        return vds

    def _somme_ventilation(self, bilan):
        return sum((Decimal(str(v["tvac"])) for v in bilan["vat_breakdown"].values()), Decimal("0.00"))

    def test_retour_integral_dun_ticket_remise_ventile_zero(self):
        s = self._produit("Robe", "0.21", "100.00")
        (vd,) = self._ticket_remise("T-1", [(s, "100.00", "0.21")], "90.00")
        enregistrer_remboursement(self.c, "T-1", vd, s, 0, "Espèces", "V", "2026-10-10 11:00:00")
        self.conn.commit()
        bilan = ZReportEngine.get_daily_z_summary("POS-01", conn=self.conn)
        self.assertEqual(Decimal(str(bilan["total_tvac"])), Decimal("0.00"))
        self.assertEqual(self._somme_ventilation(bilan), Decimal("0.00"),
                         "la ventilation TVA doit justifier le total scellé (0,00 après retour intégral)")

    def test_retour_partiel_deux_taux_ventile_comme_le_total(self):
        a = self._produit("Robe", "0.21", "10.01")
        b = self._produit("Livre", "0.06", "10.01")
        va, vb = self._ticket_remise("T-2", [(a, "10.01", "0.21"), (b, "10.01", "0.06")], "10.01")
        enregistrer_remboursement(self.c, "T-2", va, a, 0, "Espèces", "V", "2026-10-10 11:00:00")
        self.conn.commit()
        bilan = ZReportEngine.get_daily_z_summary("POS-01", conn=self.conn)
        self.assertEqual(self._somme_ventilation(bilan), Decimal(str(bilan["total_tvac"])).quantize(Decimal("0.01")))
        # la part à 21 % a été rendue : elle ne doit plus ventiler que la part vendue moins la part rendue
        self.assertLessEqual(Decimal(str(bilan["vat_breakdown"]["21%"]["tvac"])), Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
