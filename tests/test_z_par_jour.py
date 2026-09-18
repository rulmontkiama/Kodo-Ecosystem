# -*- coding: utf-8 -*-
"""Clôture Z par jour, classification QR_Code et régularisation du rendu de monnaie.

Reproduit le cas d'un client dont la base contenait des tickets non clôturés sur 11 jours, des
paiements « QR_Code » d'une ancienne version (comptés à tort en carte) et des rendus de monnaie
déduits deux fois dans le journal de caisse. Base temporaire : aucune donnée réelle touchée.
"""
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import (
    classer_moyen_paiement, enregistrer_cloture_caisse, enregistrer_vente,
    generer_bilan_z_journalier, initialiser_db, lister_jours_non_clotures,
)
from kodo_core.domain.accounting.z_report import ZReportEngine

PANIER = [{"nom": "Robe", "taille": "M", "prix_vente_tvac": Decimal("10.00"), "taux_tva": Decimal("0.21"), "stock_id": None}]


def vente(c, num, jour, total, paiements, rendu="0.00", mode=None):
    total = Decimal(total)
    ht = (total / Decimal("1.21")).quantize(Decimal("0.01"))
    enregistrer_vente(c, num, total, ht, total - ht, Decimal("0.00"), mode or paiements[0][0], None,
                      Decimal(rendu), PANIER, "V", f"{jour} 10:00:00",
                      [(m, Decimal(a)) for m, a in paiements])


class TestZParJour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        database_manager.DB_NAME = os.path.join(self.tmp, "kodo_pos.db")
        initialiser_db()
        self.conn = database_manager.get_connection()
        self.c = self.conn.cursor()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_classification_des_moyens_de_paiement(self):
        for m in ("QR", "QR_Code", "qr code", "Virement"):
            self.assertEqual(classer_moyen_paiement(m), "qr", m)
        for m in ("Espèces", "ESPECES", "cash"):
            self.assertEqual(classer_moyen_paiement(m), "especes", m)
        for m in ("CB", "Carte", "Bancontact", "", None):
            self.assertEqual(classer_moyen_paiement(m), "carte", m)
        self.assertEqual(classer_moyen_paiement("Avoir"), "avoir")

    def test_qr_code_ancien_nest_plus_compte_en_carte(self):
        vente(self.c, "T1", "2026-07-19", "100.00", [("QR_Code", "100.00")])
        vente(self.c, "T2", "2026-07-19", "50.00", [("CB", "50.00")])
        self.conn.commit()
        b = generer_bilan_z_journalier(conn=self.conn)
        self.assertEqual(b["total_qr"], Decimal("100.00"))
        self.assertEqual(b["total_carte"], Decimal("50.00"))
        self.assertEqual(b["ecart_reglements"], Decimal("0.00"))

    def test_rendu_deduit_deux_fois_est_regularise_sur_tickets_100_pour_100_especes(self):
        # Ancienne version : paiement enregistré = total (34.99), rendu 15.01 déduit ENCORE => 19.98
        vente(self.c, "T1", "2026-07-28", "34.99", [("Espèces", "34.99")], rendu="15.01")
        # Ticket mixte : non deviné, doit ressortir dans ecart_reglements
        vente(self.c, "T2", "2026-07-28", "40.00", [("Espèces", "10.00"), ("CB", "20.00")])
        self.conn.commit()
        b = generer_bilan_z_journalier(conn=self.conn)
        self.assertEqual(b["regularisation_rendu"], Decimal("15.01"))
        self.assertEqual(b["total_especes"], Decimal("34.99") + Decimal("10.00"))
        self.assertEqual(b["ecart_reglements"], Decimal("10.00"))

    def test_rendu_correct_ne_declenche_aucune_regularisation(self):
        # Version corrigée : paiement = espèces remises (50.00), rendu 15.01 => encaissé 34.99
        vente(self.c, "T1", "2026-07-28", "34.99", [("Espèces", "50.00")], rendu="15.01")
        self.conn.commit()
        b = generer_bilan_z_journalier(conn=self.conn)
        self.assertEqual(b["total_especes"], Decimal("34.99"))
        self.assertEqual(b["regularisation_rendu"], Decimal("0.00"))
        self.assertEqual(b["ecart_reglements"], Decimal("0.00"))

    def _jeu_multi_jours(self):
        vente(self.c, "A1", "2026-07-19", "10.00", [("Espèces", "10.00")])
        vente(self.c, "A2", "2026-07-19", "20.00", [("CB", "20.00")])
        vente(self.c, "B1", "2026-07-21", "30.00", [("QR_Code", "30.00")])
        vente(self.c, "C1", "2026-09-18", "40.00", [("Espèces", "40.00")])
        self.conn.commit()

    def test_jours_non_clotures_du_plus_ancien_au_plus_recent(self):
        self._jeu_multi_jours()
        jours = lister_jours_non_clotures(conn=self.conn)
        self.assertEqual([j["jour"] for j in jours], ["2026-07-19", "2026-07-21", "2026-09-18"])
        self.assertEqual([j["nb_tickets"] for j in jours], [2, 1, 1])

    def test_bilan_jusquau_inclut_tout_ce_qui_est_anterieur(self):
        self._jeu_multi_jours()
        b = generer_bilan_z_journalier(conn=self.conn, jusqu_au="2026-07-21")
        self.assertEqual(b["nb_tickets"], 3)
        self.assertEqual(b["total_tvac"], Decimal("60.00"))
        b = generer_bilan_z_journalier(conn=self.conn, jusqu_au="2026-07-19")
        self.assertEqual(b["nb_tickets"], 2)

    def test_cloture_jour_par_jour_dans_lordre_puis_aujourdhui_propre(self):
        self._jeu_multi_jours()
        r1 = enregistrer_cloture_caisse(fond_caisse_reel=None, jusqu_au="2026-07-19", conn=self.conn)
        self.assertEqual(r1["nb_tickets"], 2)
        self.assertEqual(r1["ecart"], 0.0)
        r2 = enregistrer_cloture_caisse(fond_caisse_reel=None, jusqu_au="2026-07-21", conn=self.conn)
        self.assertEqual(r2["nb_tickets"], 1)
        # La part QR est conservée dans l'enregistrement du Z (et non fondue dans espèces/carte)
        qr, carte = self.conn.execute(
            "SELECT total_qr, total_carte FROM Clotures_Caisse WHERE periode_jusqu_au = '2026-07-21'").fetchone()
        self.assertEqual((float(qr), float(carte)), (30.0, 0.0))
        # Aujourd'hui : uniquement le ticket du jour, comptage physique = fond 100 + 40 espèces
        r3 = enregistrer_cloture_caisse(fond_caisse_reel=Decimal("140.00"), fond_caisse_matin=Decimal("100.00"),
                                        jusqu_au="2026-09-18", conn=self.conn)
        self.assertEqual(r3["nb_tickets"], 1)
        self.assertEqual(r3["total_tvac"], 40.0)
        self.assertEqual(r3["ecart"], 0.0)
        self.assertEqual(lister_jours_non_clotures(conn=self.conn), [])
        # Chaîne de hachage continue sur les 3 Z, et le jour couvert est enregistré
        rows = self.conn.execute(
            "SELECT hash_precedent, current_hash, periode_jusqu_au FROM Clotures_Caisse ORDER BY id").fetchall()
        self.assertEqual(rows[1][0], rows[0][1])
        self.assertEqual(rows[2][0], rows[1][1])
        self.assertEqual([r[2] for r in rows], ["2026-07-19", "2026-07-21", "2026-09-18"])

    def test_cloture_sans_jusquau_garde_le_comportement_historique(self):
        self._jeu_multi_jours()
        r = enregistrer_cloture_caisse(fond_caisse_reel=Decimal("50.00"), conn=self.conn)
        self.assertEqual(r["nb_tickets"], 4)

    def test_resume_api_expose_jours_en_attente_et_periode(self):
        self._jeu_multi_jours()
        s = ZReportEngine.get_daily_z_summary(conn=self.conn, jusqu_au="2026-07-19")
        self.assertEqual(s["nb_tickets"], 2)
        self.assertEqual(len(s["jours_en_attente"]), 3)
        self.assertEqual(s["jusqu_au"], "2026-07-19")
        self.assertIsInstance(s["ecart_reglements"], float)

    def test_reproduction_cas_reel_client(self):
        """Base du client : QR_Code compté en carte, espèces double-déduites."""
        vente(self.c, "R1", "2026-07-28", "34.99", [("Espèces", "34.99")], rendu="15.01")
        vente(self.c, "R2", "2026-07-28", "27.00", [("Espèces", "27.00")], rendu="25.00")
        vente(self.c, "R3", "2026-07-29", "2927.86", [("QR_Code", "2927.86")])
        vente(self.c, "R4", "2026-09-18", "99.99", [("CB", "99.99")])
        self.conn.commit()
        b = generer_bilan_z_journalier(conn=self.conn)
        self.assertEqual(b["total_carte"], Decimal("99.99"))
        self.assertEqual(b["total_qr"], Decimal("2927.86"))
        self.assertEqual(b["total_especes"], Decimal("61.99"))
        self.assertEqual(b["ecart_reglements"], Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
