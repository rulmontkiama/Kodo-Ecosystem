# -*- coding: utf-8 -*-
"""
Kōdo POS — le retour intégral d'un ticket remisé rend EXACTEMENT ce qui a été payé.

Revue du 2026-10-14 : les montants rendus ligne par ligne sont arrondis chacun au centime ; leur somme pouvait
être inférieure de 1 à 2 centimes au total payé (ex. 3 lignes à 10,01 € remisées, 28,00 € payés).
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


class TestRetourIntegralRemise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        database_manager.DB_NAME = os.path.join(self.tmp, "kodo_pos.db")
        initialiser_db()
        self.conn = database_manager.get_connection()
        self.c = self.conn.cursor()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ticket(self, prix_lignes, total_paye):
        self.c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('P', 10, 0.21)")
        pid = self.c.lastrowid
        self.c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'U', 9)", (pid,))
        sid = self.c.lastrowid
        total = Decimal(str(total_paye))
        self.c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, caisse_id, "
                       "methode_paiement) VALUES ('T-9', '2026-10-10 10:00:00', ?, ?, ?, 'POS-01', 'CB')",
                       (str(total), str((total / Decimal('1.21')).quantize(Decimal('0.01'))), "0"))
        tid = self.c.lastrowid
        vds = []
        for prix in prix_lignes:
            self.c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) "
                           "VALUES (?, ?, 1, ?)", (tid, sid, str(prix)))
            vds.append(self.c.lastrowid)
        return vds

    def _rendu_total(self, vds):
        total = Decimal("0.00")
        for vd in vds:
            _, montant = enregistrer_remboursement(self.c, "T-9", vd, None, 0, "CB", "V", "2026-10-11 09:00:00")
            total += -Decimal(str(montant))
        return total

    def test_trois_lignes_remisees_rendent_le_total_paye(self):
        # 3 × 10,01 = 30,03 brut, 28,00 payés : ratio 0,93240…, chaque ligne arrondie à 9,33 (= 27,99).
        vds = self._ticket(["10.01", "10.01", "10.01"], "28.00")
        self.assertEqual(self._rendu_total(vds), Decimal("28.00"))

    def test_cas_rapporte_28_sur_150(self):
        vds = self._ticket(["50.00", "50.00", "50.00"], "28.00")
        self.assertEqual(self._rendu_total(vds), Decimal("28.00"))

    def test_retour_partiel_ne_depasse_pas_le_paye_et_laisse_le_reste(self):
        vds = self._ticket(["10.01", "10.01", "10.01"], "28.00")
        deux = self._rendu_total(vds[:2])
        self.assertLessEqual(deux, Decimal("18.67"))
        troisieme = self._rendu_total(vds[2:])
        self.assertEqual(deux + troisieme, Decimal("28.00"))


if __name__ == "__main__":
    unittest.main()
