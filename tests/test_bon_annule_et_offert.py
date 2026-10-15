# -*- coding: utf-8 -*-
"""
Kōdo POS — Annuler un bon : avec ou sans remboursement en caisse. Offrir un bon : confirmé et motivé.

Avant 2.1.3, annuler un bon vendu faisait disparaître la dette sans AUCUNE écriture : si la cliente était
remboursée (bon vendu par erreur), la caisse ne le savait pas et le tiroir manquait du même montant. Et un bon
« Offert » partait sans contrôle ni motif.

Règle : l'annulation SANS remboursement n'écrit rien en caisse (l'argent reste dans la boutique) ; l'annulation
AVEC remboursement écrit une ligne NÉGATIVE signée dans le moyen qui avait encaissé le bon, bornée au solde
restant, jamais plus que l'encaissement net.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Base(unittest.TestCase):

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

    def q(self, sql, *params):
        return self.conn.execute(sql, params).fetchall()

    def vendre(self, montant, mode, **kw):
        carte = self.dm.vendre_carte_cadeau(cursor=self.conn.cursor(), montant=montant, mode_paiement=mode,
                                            emis_par="Julie", **kw)
        self.conn.commit()
        return carte

    def annuler(self, code, **kw):
        bilan = self.dm.annuler_carte_cadeau(self.conn.cursor(), code, user_name="Julie", **kw)
        self.conn.commit()
        return bilan

    def bilan_z(self):
        return self.dm.generer_bilan_z_journalier("POS-01", conn=self.conn)


class TestAnnulationSansRemboursement(_Base):

    def test_comportement_historique_conserve_aucune_ecriture_de_caisse(self):
        carte = self.vendre(50, "Espèces")
        bilan = self.annuler(carte["code"])
        self.assertEqual(bilan["rembourse"], Decimal("0.00"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM Ledger_Caisse"), [(1,)], "seule la vente du bon")
        self.assertEqual(self.q("SELECT status, solde_actuel FROM Cartes_Cadeaux WHERE code = ?", carte["code"]),
                         [("annulee", 0.0)])

    def test_la_piste_d_audit_dit_sans_remboursement_et_le_motif(self):
        carte = self.vendre(50, "CB")
        self.annuler(carte["code"], motif="bon perdu")
        detail = self.q("SELECT details FROM Audit_Trail WHERE event_type = 'CARTE_CADEAU_ANNULEE'")[0][0]
        self.assertIn("sans remboursement", detail)
        self.assertIn("bon perdu", detail)

    def test_un_bon_deja_annule_ne_s_annule_pas_deux_fois(self):
        carte = self.vendre(50, "CB")
        self.annuler(carte["code"])
        with self.assertRaises(ValueError):
            self.annuler(carte["code"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM Audit_Trail WHERE event_type = 'CARTE_CADEAU_ANNULEE'"), [(1,)])


class TestAnnulationAvecRemboursement(_Base):

    def test_le_remboursement_ecrit_une_ligne_negative_signee_dans_le_moyen_d_origine(self):
        carte = self.vendre(50, "Espèces")
        bilan = self.annuler(carte["code"], rembourser=True)
        self.assertEqual(bilan["rembourse"], Decimal("50.00"))
        self.assertEqual(bilan["methode"], "Espèces")
        lignes = self.q("SELECT type_mouvement, methode_paiement, montant, reference, vendeur "
                        "FROM Ledger_Caisse ORDER BY id")
        self.assertEqual(lignes[-1], ("VENTE_BON", "Espèces", -50.0, carte["code"], "Julie"))
        self.assertEqual(len(self.q("SELECT signature FROM Ledger_Caisse ORDER BY id DESC LIMIT 1")[0][0]), 64)

    def test_le_z_se_neutralise_et_l_equation_des_reglements_tient(self):
        carte = self.vendre(50, "Espèces")
        avant = self.bilan_z()
        self.assertEqual(avant["total_especes"], Decimal("50.00"))
        self.annuler(carte["code"], rembourser=True)
        apres = self.bilan_z()
        self.assertEqual(apres["total_especes"], Decimal("0.00"), "le tiroir attendu retombe à zéro")
        self.assertEqual(apres["total_bons_vendus"], Decimal("0.00"))
        self.assertEqual(apres["ecart_reglements"], Decimal("0.00"))

    def test_le_remboursement_est_borne_au_solde_restant(self):
        carte = self.vendre(50, "CB")
        cur = self.conn.cursor()
        self.dm.utiliser_carte_cadeau(cur, carte["code"], Decimal("20.00"), "Julie")
        self.conn.commit()
        bilan = self.annuler(carte["code"], rembourser=True)
        self.assertEqual(bilan["rembourse"], Decimal("30.00"), "50 encaissés, 20 déjà consommés en caisse")
        self.assertEqual(self.q("SELECT montant FROM Ledger_Caisse ORDER BY id DESC LIMIT 1"), [(-30.0,)])

    def test_un_bon_offert_ne_peut_pas_etre_rembourse_en_caisse(self):
        carte = self.vendre(20, "Offert")
        with self.assertRaises(ValueError):
            self.annuler(carte["code"], rembourser=True)
        self.assertEqual(self.q("SELECT status FROM Cartes_Cadeaux WHERE code = ?", carte["code"]), [("active",)],
                         "le refus ne doit rien écrire")
        self.assertEqual(self.q("SELECT COUNT(*) FROM Ledger_Caisse"), [(0,)])

    def test_un_bon_deja_epuise_n_a_rien_a_rembourser(self):
        carte = self.vendre(10, "CB")
        cur = self.conn.cursor()
        self.dm.utiliser_carte_cadeau(cur, carte["code"], Decimal("10.00"), "Julie")
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.annuler(carte["code"], rembourser=True)

    def test_la_chaine_du_journal_reste_intacte(self):
        from kodo_core.db import audit_trail
        carte = self.vendre(50, "QR")
        self.annuler(carte["code"], rembourser=True)
        self.conn.commit()
        self.assertTrue(audit_trail.verifier_chainage("Ledger_Caisse", compute_hash_func=audit_trail.hash_ledger_row))
        self.assertTrue(audit_trail.verifier_chainage("Audit_Trail"))

    def test_la_liste_expose_l_encaissement_net_et_le_moyen(self):
        carte = self.vendre(50, "CB")
        offert = self.vendre(20, "Offert")
        liste = {c["code"]: c for c in self.dm.lister_cartes_cadeaux(conn=self.conn)}
        self.assertEqual(liste[carte["code"]]["cashCollected"], 50.0)
        self.assertEqual(liste[carte["code"]]["cashMethod"], "CB")
        self.assertEqual(liste[offert["code"]]["cashCollected"], 0.0)
        self.annuler(carte["code"], rembourser=True)
        liste = {c["code"]: c for c in self.dm.lister_cartes_cadeaux(conn=self.conn)}
        self.assertEqual(liste[carte["code"]]["cashCollected"], 0.0, "net : 50 - 50")


class TestRoutes(_Base):

    def appeler(self, methode, chemin, corps=None):
        from kodo_core.api.app import kodo_app
        statut, reponse, _ = kodo_app.handle_request(methode, chemin, {}, {}, corps or {})
        return statut, reponse

    def test_offert_exige_confirmation_et_motif(self):
        base = {"amount": 20, "paymentMethod": "Offert", "userName": "Julie"}
        statut, rep = self.appeler("POST", "/api/gift-cards", base)
        self.assertEqual((statut, rep.get("code")), (400, "OFFERT_CONFIRMATION_REQUISE"))
        statut, rep = self.appeler("POST", "/api/gift-cards", dict(base, confirmOffert=True))
        self.assertEqual((statut, rep.get("code")), (400, "OFFERT_MOTIF_REQUIS"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM Cartes_Cadeaux"), [(0,)], "aucun bon créé sur un refus")

    def test_offert_confirme_et_motive_passe_et_le_motif_est_audite(self):
        statut, rep = self.appeler("POST", "/api/gift-cards", {
            "amount": 20, "paymentMethod": "Offert", "userName": "Julie",
            "confirmOffert": True, "reason": "Geste suite à une erreur de taille"})
        self.assertEqual(statut, 200, rep)
        detail = self.q("SELECT details FROM Audit_Trail WHERE event_type = 'CARTE_CADEAU_OFFERTE'")[0][0]
        self.assertIn("erreur de taille", detail)

    def test_un_bon_paye_n_a_pas_besoin_de_confirmation_offert(self):
        statut, rep = self.appeler("POST", "/api/gift-cards", {"amount": 20, "paymentMethod": "CB", "userName": "Julie"})
        self.assertEqual(statut, 200, rep)

    def test_route_void_avec_et_sans_remboursement(self):
        _, rep = self.appeler("POST", "/api/gift-cards", {"amount": 30, "paymentMethod": "Espèces", "userName": "Julie"})
        code = rep["code"]
        statut, rep = self.appeler("POST", "/api/gift-cards/void", {"code": code, "refund": True, "userName": "Julie"})
        self.assertEqual(statut, 200, rep)
        self.assertEqual((rep["refunded"], rep["refundMethod"]), (30.0, "Espèces"))
        statut, rep = self.appeler("POST", "/api/gift-cards/void", {"code": code, "userName": "Julie"})
        self.assertEqual(statut, 400, "un bon déjà annulé est refusé")

    def test_route_void_sans_remboursement_par_defaut(self):
        _, rep = self.appeler("POST", "/api/gift-cards", {"amount": 30, "paymentMethod": "CB", "userName": "Julie"})
        statut, rep = self.appeler("POST", "/api/gift-cards/void", {"code": rep["code"], "userName": "Julie"})
        self.assertEqual(statut, 200, rep)
        self.assertEqual(rep["refunded"], 0.0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM Ledger_Caisse"), [(1,)])


if __name__ == "__main__":
    unittest.main()
