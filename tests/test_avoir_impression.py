# -*- coding: utf-8 -*-
"""Impression du bon d'avoir : rendu, refus des bons sans valeur, routes, impression automatique."""

import os
import sys
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kodo_core.hardware import avoir_printer as ap


class TestRendu(unittest.TestCase):

    AVOIR = {"code": "AVOIR-K7M2QX9TBR", "initial": Decimal("60.50"), "restant": Decimal("60.50"),
             "date": "2026-10-03 09:41:00", "client": "Camille", "emis_par": "Julie", "status": "active",
             "ticket_origine": "TCK-1", "ticket_remboursement": "REF-TCK-1-123"}
    BOUTIQUE = {"nom": "Maison Kodo", "sous_titre": "", "adresse": "", "tva": "",
                "conditions": ap.CONDITIONS_PAR_DEFAUT}

    def test_formater_montant(self):
        self.assertEqual(ap.formater_montant(60.5), "60,50 €")
        self.assertEqual(ap.formater_montant(Decimal("1234567.895")), "1 234 567,90 €")
        self.assertEqual(ap.formater_montant(0), "0,00 €")

    def test_image_80mm_et_payload_escpos(self):
        img = ap.generer_image_avoir(self.AVOIR, self.BOUTIQUE)
        self.assertEqual(img.width, 512)
        self.assertGreater(img.height, 600)
        payload = ap.construire_payload(img)
        self.assertTrue(payload.startswith(b"\x1b@"))      # ESC @ : réinitialisation
        self.assertIn(b"\x1dv0", payload)                  # raster GS v 0
        self.assertTrue(payload.endswith(b"\x1dVB\x00"))   # découpe

    def test_les_variantes_se_dessinent_sans_erreur(self):
        ap.generer_image_avoir(dict(self.AVOIR, restant=Decimal("5.00")), self.BOUTIQUE, duplicata=True)
        ap.generer_image_avoir(dict(self.AVOIR, client="", ticket_origine="", ticket_remboursement=""),
                               dict(self.BOUTIQUE, nom=""))
        ap.generer_image_avoir(dict(self.AVOIR, initial=Decimal("99999.99"), restant=Decimal("99999.99")),
                               dict(self.BOUTIQUE, nom="UNE ENSEIGNE AU NOM VRAIMENT TRÈS TRÈS LONG POUR VOIR"))

    def test_une_carte_cadeau_ne_porte_pas_le_titre_bon_d_avoir(self):
        with mock.patch.object(ap, "_centre", wraps=ap._centre) as centre:
            ap.generer_image_avoir(dict(self.AVOIR, code="KODO-ABCDE12345"), self.BOUTIQUE)
            ap.generer_image_avoir(self.AVOIR, self.BOUTIQUE)
        textes = [c.args[2] for c in centre.call_args_list]
        self.assertIn("CARTE CADEAU", textes)
        self.assertIn("BON D'AVOIR", textes)

    def test_le_code_barres_encode_le_code(self):
        self.assertIsNotNone(ap._code_barres("AVOIR-K7M2QX9TBR", 440, 90))


class _Base(unittest.TestCase):

    def setUp(self):
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()
        conn = sqlite3.connect(self.path)
        c = conn.cursor()
        self.code = dm.emettre_carte_cadeau(c, 40, emis_par="Julie", ticket_origine="TCK-9",
                                            ticket_remboursement="REF-TCK-9-1")["code"]
        conn.commit()
        conn.close()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _sql(self, sql, *params):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, params)
        conn.commit()
        conn.close()


class TestChargement(_Base):

    def test_un_bon_actif_se_charge(self):
        a = ap.charger_avoir(self.code)
        self.assertEqual((a["initial"], a["restant"], a["ticket_origine"]), (Decimal("40.0"), Decimal("40.0"), "TCK-9"))

    def test_un_bon_annule_ne_s_imprime_pas(self):
        self._sql("UPDATE Cartes_Cadeaux SET status='annulee', solde_actuel=0 WHERE code=?", self.code)
        with self.assertRaisesRegex(ValueError, "annulé"):
            ap.charger_avoir(self.code)

    def test_un_bon_epuise_ne_s_imprime_pas(self):
        self._sql("UPDATE Cartes_Cadeaux SET solde_actuel=0 WHERE code=?", self.code)
        with self.assertRaisesRegex(ValueError, "épuisé"):
            ap.charger_avoir(self.code)

    def test_un_bon_inconnu_est_refuse(self):
        with self.assertRaisesRegex(ValueError, "introuvable"):
            ap.charger_avoir("AVOIR-NOPE")

    def test_imprimer_avoir_envoie_le_payload_a_l_imprimante(self):
        envoyes = []
        with mock.patch("kodo_core.hardware.printer.ESCPOSThermalPrinter.send_raw",
                        side_effect=lambda self_, raw: envoyes.append(raw) or True, autospec=True):
            self.assertTrue(ap.imprimer_avoir(self.code))
        self.assertEqual(len(envoyes), 1)
        self.assertTrue(envoyes[0].endswith(b"\x1dVB\x00"))

    def test_imprimer_un_bon_annule_n_envoie_rien(self):
        self._sql("UPDATE Cartes_Cadeaux SET status='annulee', solde_actuel=0 WHERE code=?", self.code)
        with mock.patch("kodo_core.hardware.printer.ESCPOSThermalPrinter.send_raw") as envoi:
            with self.assertRaises(ValueError):
                ap.imprimer_avoir(self.code)
            envoi.assert_not_called()


class TestRoutes(_Base):

    def _appel(self, method, path, query=None, data=None):
        from kodo_core.api.routes.pos_routes import handle_pos_request
        return handle_pos_request(method, path, query or {}, data or {})

    def test_apercu_renvoie_un_png(self):
        status, body = self._appel("GET", "/api/gift-cards/preview", query={"code": [self.code]})
        self.assertEqual(status, 200)
        self.assertTrue(body["image"].startswith("data:image/png;base64,"))

    def test_apercu_d_un_bon_annule_est_refuse(self):
        self._sql("UPDATE Cartes_Cadeaux SET status='annulee', solde_actuel=0 WHERE code=?", self.code)
        status, body = self._appel("GET", "/api/gift-cards/preview", query={"code": [self.code]})
        self.assertEqual(status, 400)

    def test_impression_enfile_un_travail_duplicata(self):
        worker = mock.MagicMock()
        worker.enqueue_voucher_print.return_value.job_id = "job-1"
        with mock.patch("kodo_core.hardware.print_worker.get_print_worker", return_value=worker):
            status, body = self._appel("POST", "/api/gift-cards/print", data={"code": self.code, "duplicata": True})
        self.assertEqual((status, body["status"]), (200, "ENQUEUED"))
        worker.enqueue_voucher_print.assert_called_once_with(self.code, duplicata=True)

    def test_impression_d_un_bon_inconnu_est_refusee_sans_enfiler(self):
        worker = mock.MagicMock()
        with mock.patch("kodo_core.hardware.print_worker.get_print_worker", return_value=worker):
            status, _ = self._appel("POST", "/api/gift-cards/print", data={"code": "AVOIR-NOPE"})
        self.assertEqual(status, 400)
        worker.enqueue_voucher_print.assert_not_called()


class TestImpressionAutoApresRetour(_Base):

    def _vente(self):
        conn = sqlite3.connect(self.path)
        c = conn.cursor()
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Robe', 60.50, 0.21)")
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (c.lastrowid,))
        sid = c.lastrowid
        self.dm.enregistrer_vente(
            cursor=c, numero_ticket="TCK-AUTO-1", total_tvac=60.50, total_htva=50.0, total_tva=10.5, remise=0.0,
            methode_paiement="CB", id_client=None, rendu_monnaie=0.0,
            panier=[{"stock_id": sid, "prix_vente_tvac": 60.50, "quantite": 1}],
            vendeur_nom="V", date_heure="2026-10-03 10:00:00", paiements=[("CB", 60.50)])
        c.execute("SELECT id FROM Ventes_Details WHERE id_ticket=(SELECT id FROM Tickets WHERE numero_ticket='TCK-AUTO-1')")
        vd = c.fetchone()[0]
        conn.commit()
        conn.close()
        return sid, vd

    def _retour(self, mode, **extra):
        from kodo_core.api.routes.pos_routes import handle_pos_request
        sid, vd = self._vente()
        worker = mock.MagicMock()
        worker.enqueue_voucher_print.return_value.job_id = "job-9"
        with mock.patch("kodo_core.hardware.print_worker.get_print_worker", return_value=worker):
            status, body = handle_pos_request("POST", "/api/sales/return", {}, dict(
                ticket_number="TCK-AUTO-1", sales_detail_id=vd, stock_id=sid, quantity=1, mode=mode, **extra))
        return status, body, worker

    def test_un_retour_en_avoir_imprime_le_bon(self):
        status, body, worker = self._retour("Avoir")
        self.assertEqual(status, 200)
        worker.enqueue_voucher_print.assert_called_once_with(body["gift_card"]["code"])
        self.assertEqual(body["print_status"], "ENQUEUED")

    def test_un_retour_en_especes_n_imprime_aucun_bon(self):
        status, body, worker = self._retour("Espèces")
        self.assertEqual(status, 200)
        worker.enqueue_voucher_print.assert_not_called()
        self.assertNotIn("print_status", body)

    def test_un_souci_d_impression_ne_remet_pas_le_remboursement_en_cause(self):
        from kodo_core.api.routes.pos_routes import handle_pos_request
        sid, vd = self._vente()
        with mock.patch("kodo_core.hardware.print_worker.get_print_worker", side_effect=RuntimeError("pas d'imprimante")):
            status, body = handle_pos_request("POST", "/api/sales/return", {}, dict(
                ticket_number="TCK-AUTO-1", sales_detail_id=vd, stock_id=sid, quantity=1, mode="Avoir"))
        self.assertEqual((status, body["print_status"]), (200, "ERROR"))
        self.assertTrue(body["gift_card"]["code"])


if __name__ == "__main__":
    unittest.main()
