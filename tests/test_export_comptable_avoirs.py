# -*- coding: utf-8 -*-
"""
Kōdo POS — Exports comptables : règlements par moyen, TVA par taux, avoirs et bons cadeaux.

Historique (export WinBooks) : les bons et avoirs étaient postés sur la banque ; un remboursement en
espèces aussi ; un ticket réglé en deux moyens passait en entier sur le premier ; la TVA était d'un
seul bloc codé « 21 » ; les montants des retours étaient négatifs ; un bon cadeau vendu n'apparaissait nulle
part. L'export « comptable belge » lisait le prix avant remise globale, donc un retour sur un ticket
remisé s'affichait au prix affiché et non au prix rendu.
"""

import csv
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _lire(chemin):
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _dec(texte):
    return Decimal(str(texte).replace(",", "."))


class TestExportsComptables(unittest.TestCase):

    def setUp(self):
        import database_manager as dm
        import export_manager
        self.dm = dm
        self.em = export_manager
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()
        self.dossier = tempfile.mkdtemp(prefix="exports_")
        self._patch = mock.patch.object(export_manager, "get_export_dir", return_value=self.dossier)
        self._patch.start()

        self.conn = sqlite3.connect(self.path)
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Robe', 60.50, 0.21)")
        self.s_robe = self._stock(c, c.lastrowid)
        c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Livre', 10.60, 0.06)")
        self.s_livre = self._stock(c, c.lastrowid)
        self.conn.commit()

        self.t1 = self._vente([(self.s_robe, 1)], [("Espèces", 60.50)])
        self.t2 = self._vente([(self.s_robe, 1), (self.s_livre, 1)], [("CB", 30.00), ("Espèces", 41.10)])
        self.r1 = self._retour(self.t1, self.s_robe, "Avoir")
        self.t3 = self._vente([(self.s_livre, 1), (self.s_robe, 1)], [("Avoir", 60.50), ("Espèces", 10.60)],
                              code=self.r1["gift_card"]["code"])
        self.r2 = self._retour(self.t2, self.s_livre, "Espèces")
        self.bon_cb = self._bon(50, "CB")
        self.bon_esp = self._bon(30, "Espèces")
        self.bon_offert = self._bon(20, "Offert")
        # Ticket remisé de 10 % : 2 robes = 108,90 € payés, dont 90,00 HTVA.
        self.t4 = self._vente([(self.s_robe, 2)], [("CB", 108.90)], remise=10)
        self.r4 = self._retour(self.t4, self.s_robe, "CB")

    def tearDown(self):
        self._patch.stop()
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    # -- jeu d'essai ---------------------------------------------------------------------------
    @staticmethod
    def _stock(c, produit_id):
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'U', 50)", (produit_id,))
        return c.lastrowid

    def _vente(self, lignes, paiements, code=None, remise=0.0):
        from kodo_core.domain.sales.cart_engine import process_sale_transaction
        return process_sale_transaction(
            cart_items=[{"stock_id": sid, "quantite": qte} for sid, qte in lignes], total_tvac=0.0,
            payments=paiements, cashier_name="Julie", gift_card_code=code, discount_percent=remise)["numero_ticket"]

    def _retour(self, numero, stock_id, mode):
        from kodo_core.domain.sales.cart_engine import process_return_transaction
        vd = self.conn.execute(
            "SELECT vd.id FROM Ventes_Details vd JOIN Tickets t ON t.id = vd.id_ticket "
            "WHERE t.numero_ticket = ? AND vd.id_stock = ? AND vd.quantite > 0", (numero, stock_id)).fetchone()[0]
        return process_return_transaction(
            original_ticket_number=numero, sales_detail_id=vd, stock_id=stock_id, refund_price=0.0,
            refund_mode=mode, cashier_name="Julie", quantity=1)

    def _bon(self, montant, mode):
        conn = self.dm.get_connection()
        try:
            c = conn.cursor()
            carte = self.dm.vendre_carte_cadeau(cursor=c, montant=montant, mode_paiement=mode, emis_par="Julie")
            conn.commit()
        finally:
            conn.close()
        return carte

    # -- lecture de l'export WinBooks ---------------------------------------------------------------
    def _winbooks(self):
        lignes = _lire(self.em.export_winbooks_csv())
        par_document = {}
        for ligne in lignes:
            par_document.setdefault(ligne["Document"], []).append(ligne)
        return lignes, par_document

    @staticmethod
    def _ecritures(document):
        """[(compte, sens, montant, code_tva)] d'un document."""
        return [(l["Compte"], l["Sens"], _dec(l["Montant"]), l["Code TVA"]) for l in document]

    def test_chaque_document_est_equilibre_et_aucun_montant_n_est_negatif(self):
        lignes, par_document = self._winbooks()
        self.assertEqual(len(par_document), 9)   # 4 ventes, 3 retours, 2 bons encaissés (le bon offert n'écrit rien)
        for numero, document in par_document.items():
            debit = sum(m for _c, s, m, _t in self._ecritures(document) if s == "D")
            credit = sum(m for _c, s, m, _t in self._ecritures(document) if s == "C")
            self.assertEqual(debit, credit, numero)
        self.assertTrue(all(_dec(l["Montant"]) > 0 for l in lignes))
        self.assertTrue(all(l["Montant"].endswith(tuple("0123456789")) and "," in l["Montant"] for l in lignes))

    def test_vente_en_especes(self):
        e = self._ecritures(self._winbooks()[1][self.t1])
        self.assertIn(("570000", "D", Decimal("60.50"), ""), e)
        self.assertIn(("700000", "C", Decimal("50.00"), "21"), e)
        self.assertIn(("451000", "C", Decimal("10.50"), ""), e)

    def test_paiement_mixte_ventile_par_moyen_et_par_taux(self):
        e = self._ecritures(self._winbooks()[1][self.t2])
        self.assertIn(("580000", "D", Decimal("30.00"), ""), e)    # CB -> banque
        self.assertIn(("570000", "D", Decimal("41.10"), ""), e)    # espèces -> caisse
        self.assertIn(("700000", "C", Decimal("50.00"), "21"), e)  # robe 21 %
        self.assertIn(("700000", "C", Decimal("10.00"), "06"), e)  # livre 6 %
        self.assertIn(("451000", "C", Decimal("10.50"), ""), e)
        self.assertIn(("451000", "C", Decimal("0.60"), ""), e)

    def test_retour_en_avoir_credite_la_dette_et_inverse_la_vente(self):
        e = self._ecritures(self._winbooks()[1][self.r1["refund_ticket_number"]])
        self.assertIn(("460000", "C", Decimal("60.50"), ""), e)    # dette envers la cliente
        self.assertIn(("700000", "D", Decimal("50.00"), "21"), e)
        self.assertIn(("451000", "D", Decimal("10.50"), ""), e)    # TVA à régulariser
        self.assertNotIn("580000", [c for c, *_ in e])             # jamais la banque

    def test_reglement_par_bon_debite_la_dette_et_non_la_banque(self):
        e = self._ecritures(self._winbooks()[1][self.t3])
        self.assertIn(("460000", "D", Decimal("60.50"), ""), e)
        self.assertIn(("570000", "D", Decimal("10.60"), ""), e)
        self.assertNotIn("580000", [c for c, *_ in e])

    def test_remboursement_en_especes_sort_de_la_caisse(self):
        e = self._ecritures(self._winbooks()[1][self.r2["refund_ticket_number"]])
        self.assertIn(("570000", "C", Decimal("10.60"), ""), e)
        self.assertIn(("700000", "D", Decimal("10.00"), "06"), e)
        self.assertIn(("451000", "D", Decimal("0.60"), ""), e)

    def test_bon_cadeau_vendu_entre_en_dette_sans_tva_et_le_bon_offert_n_ecrit_rien(self):
        _lignes, par_document = self._winbooks()
        cb = self._ecritures(par_document[self.bon_cb["code"]])
        self.assertCountEqual(cb, [("580000", "D", Decimal("50.00"), ""), ("460000", "C", Decimal("50.00"), "")])
        esp = self._ecritures(par_document[self.bon_esp["code"]])
        self.assertCountEqual(esp, [("570000", "D", Decimal("30.00"), ""), ("460000", "C", Decimal("30.00"), "")])
        self.assertNotIn(self.bon_offert["code"], par_document)

    def test_retour_sur_ticket_remise_est_ecrit_au_prix_rendu(self):
        e = self._ecritures(self._winbooks()[1][self.r4["refund_ticket_number"]])
        self.assertIn(("580000", "C", Decimal("54.45"), ""), e)    # 60,50 x 0,90, rendu en CB
        self.assertIn(("700000", "D", Decimal("45.00"), "21"), e)
        self.assertIn(("451000", "D", Decimal("9.45"), ""), e)

    def test_les_comptes_se_parametrent_et_un_numero_invalide_est_ignore(self):
        self.conn.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('compta_compte_bons', '499000')")
        self.conn.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('compta_compte_caisse', 'abc')")
        self.conn.commit()
        _lignes, par_document = self._winbooks()
        comptes = {c for c, *_ in self._ecritures(par_document[self.bon_cb["code"]])}
        self.assertEqual(comptes, {"580000", "499000"})
        comptes_esp = {c for c, *_ in self._ecritures(par_document[self.bon_esp["code"]])}
        self.assertEqual(comptes_esp, {"570000", "499000"})   # « abc » refusé : compte par défaut

    def test_le_filtre_mois_annee_s_applique_aux_bons(self):
        conn = self.dm.get_connection()
        try:
            ancien = self.dm.vendre_carte_cadeau(cursor=conn.cursor(), montant=15, mode_paiement="CB",
                                                 emis_par="Julie", date_heure="2020-01-15 10:00:00")
            conn.commit()
        finally:
            conn.close()
        lignes = _lire(self.em.export_winbooks_csv(mois=1, annee=2020))
        self.assertEqual({l["Document"] for l in lignes}, {ancien["code"]})
        self.assertEqual({l["Période"] for l in lignes}, {"202001"})

    # -- export « comptable belge » (une ligne par article) ---------------------------------------
    def test_export_belge_somme_des_lignes_egale_total_du_ticket(self):
        lignes = _lire(self.em.export_comptable_belge())
        for numero in (self.t1, self.t2, self.t3, self.t4, self.r1["refund_ticket_number"],
                       self.r2["refund_ticket_number"], self.r4["refund_ticket_number"]):
            somme = sum(_dec(l["Montant TVAC (€)"]) for l in lignes if l["Numéro Ticket"] == numero)
            total = Decimal(str(self.conn.execute(
                "SELECT total_tvac FROM Tickets WHERE numero_ticket = ?", (numero,)).fetchone()[0]))
            self.assertEqual(somme, total, numero)

    def test_export_belge_moyens_de_paiement_lus_dans_le_journal(self):
        lignes = _lire(self.em.export_comptable_belge())
        moyen = {l["Numéro Ticket"]: l["Moyen de Paiement"] for l in lignes}
        self.assertEqual(moyen[self.t2], "CB + Espèces")
        self.assertEqual(moyen[self.t3], "Avoir + Espèces")
        self.assertEqual(moyen[self.r1["refund_ticket_number"]], "REMB (Avoir)")
        self.assertEqual(moyen[self.r2["refund_ticket_number"]], "REMB (Espèces)")


if __name__ == "__main__":
    unittest.main()
