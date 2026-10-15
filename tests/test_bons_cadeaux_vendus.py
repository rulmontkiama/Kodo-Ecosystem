# -*- coding: utf-8 -*-
"""
Kōdo POS — Un bon cadeau VENDU est un encaissement : journal de caisse signé, clôture Z, piste d'audit.

Historique : l'écran « Cartes cadeaux » émettait la carte sans aucune écriture. L'argent reçu n'entrait
ni dans le Z (écart de caisse ou de terminal du même montant), ni dans les exports comptables.
"""

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

    def _vendre(self, montant, mode, **kw):
        carte = self.dm.vendre_carte_cadeau(cursor=self.conn.cursor(), montant=montant, mode_paiement=mode,
                                            emis_par="Julie", **kw)
        self.conn.commit()
        return carte


class TestVenteDeBon(_BaseDB):

    def test_bon_paye_en_cb_ecrit_une_ligne_de_caisse_signee(self):
        carte = self._vendre(50, "CB")
        self.assertTrue(carte["encaisse"])
        self.assertEqual(
            self._q("SELECT type_mouvement, methode_paiement, montant, reference, vendeur FROM Ledger_Caisse"),
            [("VENTE_BON", "CB", 50.0, carte["code"], "Julie")])
        self.assertEqual(
            self._q("SELECT status, solde_actuel FROM Cartes_Cadeaux WHERE code = ?", carte["code"]),
            [("active", 50.0)])
        signature = self._q("SELECT signature FROM Ledger_Caisse")[0][0]
        self.assertEqual(len(signature), 64)

    def test_les_libelles_de_paiement_sont_normalises(self):
        attendus = {"espèces": "Espèces", "CASH": "Espèces", "Bancontact": "CB", "carte bancaire": "CB",
                    "CB": "CB", "qr_code": "QR"}
        for saisi, attendu in attendus.items():
            carte = self._vendre(10, saisi)
            ligne = self._q("SELECT methode_paiement FROM Ledger_Caisse WHERE reference = ?", carte["code"])
            self.assertEqual(ligne, [(attendu,)], saisi)

    def test_bon_offert_n_ecrit_aucun_mouvement_de_caisse(self):
        carte = self._vendre(20, "Offert")
        self.assertFalse(carte["encaisse"])
        self.assertEqual(self._q("SELECT COUNT(*) FROM Ledger_Caisse"), [(0,)])
        self.assertEqual(self._q("SELECT COUNT(*) FROM Audit_Trail WHERE event_type = 'CARTE_CADEAU_OFFERTE'"), [(1,)])
        self.assertEqual(self._q("SELECT solde_actuel FROM Cartes_Cadeaux WHERE code = ?", carte["code"]), [(20.0,)])

    def test_moyen_inconnu_avoir_ou_absent_refuse_sans_rien_ecrire(self):
        for mode in ("", None, "   ", "chèque cadeau", "Avoir", "Carte cadeau"):
            with self.assertRaises(ValueError, msg=repr(mode)):
                self._vendre(10, mode)
        self.assertEqual(self._q("SELECT COUNT(*) FROM Cartes_Cadeaux"), [(0,)])
        self.assertEqual(self._q("SELECT COUNT(*) FROM Ledger_Caisse"), [(0,)])

    def test_un_montant_nul_ou_negatif_est_refuse(self):
        for montant in (0, -5):
            with self.assertRaises(ValueError):
                self._vendre(montant, "CB")
        self.assertEqual(self._q("SELECT COUNT(*) FROM Ledger_Caisse"), [(0,)])

    def test_echec_de_l_ecriture_de_caisse_annule_aussi_l_emission(self):
        curseur = self.conn.cursor()
        with mock.patch.object(self.dm, "signer_ledger", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.dm.vendre_carte_cadeau(cursor=curseur, montant=40, mode_paiement="CB", emis_par="Julie")
        self.conn.rollback()
        self.assertEqual(self._q("SELECT COUNT(*) FROM Cartes_Cadeaux"), [(0,)])
        self.assertEqual(self._q("SELECT COUNT(*) FROM Ledger_Caisse"), [(0,)])


class TestClotureZ(_BaseDB):

    def test_le_z_compte_l_argent_recu_sans_le_compter_en_chiffre_d_affaires(self):
        self._vendre(50, "CB")
        self._vendre(30, "Espèces")
        self._vendre(10, "QR")
        self._vendre(20, "Offert")
        bilan = self.dm.generer_bilan_z_journalier(conn=self.conn)
        self.assertEqual(bilan["total_tvac"], Decimal("0.00"))
        self.assertEqual(bilan["total_carte"], Decimal("50.00"))
        self.assertEqual(bilan["total_especes"], Decimal("30.00"))
        self.assertEqual(bilan["total_qr"], Decimal("10.00"))
        self.assertEqual(bilan["total_bons_vendus"], Decimal("90.00"))
        self.assertEqual(bilan["ecart_reglements"], Decimal("0.00"))

    def test_le_tiroir_attendu_contient_le_bon_paye_en_especes(self):
        self._vendre(30, "Espèces")
        res = self.dm.enregistrer_cloture_caisse(fond_caisse_reel=Decimal("30.00"),
                                                 fond_caisse_matin=Decimal("0.00"), vendeur="Julie", conn=self.conn)
        self.assertEqual(res["ecart"], 0.0)
        self.assertEqual(res["total_bons_vendus"], 30.0)

    def test_la_cloture_marque_les_ecritures_et_ne_les_recompte_pas(self):
        self._vendre(30, "Espèces")
        self.dm.enregistrer_cloture_caisse(fond_caisse_reel=Decimal("30.00"), fond_caisse_matin=Decimal("0.00"),
                                           vendeur="Julie", conn=self.conn)
        self.assertEqual(
            self._q("SELECT COUNT(*) FROM Ledger_Caisse WHERE type_mouvement = 'VENTE_BON' AND z_id IS NULL"), [(0,)])
        self.assertEqual(self._q("SELECT total_bons_vendus FROM Clotures_Caisse"), [(30.0,)])
        suivant = self.dm.generer_bilan_z_journalier(conn=self.conn)
        self.assertEqual(suivant["total_bons_vendus"], Decimal("0.00"))
        self.assertEqual(suivant["total_especes"], Decimal("0.00"))

    def test_le_resume_du_z_expose_les_bons_vendus_en_nombre(self):
        self._vendre(25, "CB")
        from kodo_core.domain.accounting.z_report import ZReportEngine
        resume = ZReportEngine.get_daily_z_summary(conn=self.conn)
        self.assertEqual(resume["total_bons_vendus"], 25.0)
        self.assertIsInstance(resume["total_bons_vendus"], float)
        self.assertEqual(resume["ecart_reglements"], 0.0)

    def test_les_chaines_signees_restent_intactes(self):
        self._vendre(50, "CB")
        self._vendre(20, "Offert")
        self.dm.enregistrer_cloture_caisse(fond_caisse_reel=Decimal("0.00"), fond_caisse_matin=Decimal("0.00"),
                                           vendeur="Julie", conn=self.conn)
        from audit_trail import audit_complet
        resultat = audit_complet()
        self.assertTrue(resultat["ledger_ok"])
        self.assertTrue(resultat["audit_ok"])
        self.assertTrue(resultat["conforme"])


class TestRoute(_BaseDB):

    def _appel(self, data):
        from kodo_core.api.routes.pos_routes import handle_pos_request
        return handle_pos_request("POST", "/api/gift-cards", {}, data)

    def test_sans_moyen_de_paiement_la_route_refuse(self):
        statut, corps = self._appel({"amount": 50})
        self.assertEqual(statut, 400)
        self.assertIn("paiement", corps["error"].lower())
        self.assertEqual(self._q("SELECT COUNT(*) FROM Cartes_Cadeaux"), [(0,)])

    def test_moyen_inconnu_refuse_par_la_route(self):
        statut, corps = self._appel({"amount": 50, "paymentMethod": "chèque cadeau"})
        self.assertEqual(statut, 400)
        self.assertEqual(self._q("SELECT COUNT(*) FROM Cartes_Cadeaux"), [(0,)])

    def test_vente_en_cb_par_la_route(self):
        statut, corps = self._appel({"amount": 50, "paymentMethod": "CB", "userName": "Julie", "notes": "anniversaire"})
        self.assertEqual(statut, 200)
        self.assertTrue(corps["success"])
        self.assertEqual(corps["paymentMode"], "CB")
        self.assertTrue(corps["encaisse"])
        self.assertEqual(
            self._q("SELECT type_mouvement, methode_paiement, montant, reference FROM Ledger_Caisse"),
            [("VENTE_BON", "CB", 50.0, corps["code"])])

    def test_bon_offert_par_la_route(self):
        # Depuis 2.1.3 un bon offert est confirmé et motivé (voir test_bon_annule_et_offert.py).
        statut, corps = self._appel({"amount": 15, "paymentMethod": "Offert", "userName": "Julie",
                                     "confirmOffert": True, "reason": "Geste commercial"})
        self.assertEqual(statut, 200)
        self.assertFalse(corps["encaisse"])
        self.assertEqual(self._q("SELECT COUNT(*) FROM Ledger_Caisse"), [(0,)])


if __name__ == "__main__":
    unittest.main()
