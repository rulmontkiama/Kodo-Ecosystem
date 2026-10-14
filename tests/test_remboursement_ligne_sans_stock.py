# -*- coding: utf-8 -*-
"""
Kōdo POS — une ligne de vente SANS article en stock (`id_stock` NULL) ne se rembourse pas à l'infini.

Défaut trouvé par la revue du 2026-10-14 (deux relecteurs indépendamment) : `enregistrer_remboursement`
n'écrivait la ligne négative (`refund_of_vd_id`) QUE si `id_stock_origine` était renseigné. Une commande
Shopify dont l'article est « INTROUVABLE » crée une ligne `id_stock NULL` : le garde-fou
anti-double-remboursement ne perdait pas seulement sa trace, il n'en avait jamais — la même ligne pouvait
être remboursée autant de fois qu'on cliquait, et chaque clic rendait de l'argent.
"""
import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestRemboursementLigneSansStock(unittest.TestCase):

    def setUp(self):
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _ligne_sans_stock(self, cursor, quantite=1, prix=50.0):
        """Ticket Shopify dont l'article n'a pas été retrouvé : `id_stock` reste NULL."""
        cursor.execute(
            "INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, caisse_id, "
            "methode_paiement) VALUES ('SHOP-1001', '2026-10-10 10:00:00', ?, ?, ?, 'POS-01', 'Shopify')",
            (prix * quantite, round(prix * quantite / 1.21, 2), round(prix * quantite - prix * quantite / 1.21, 2)))
        id_ticket = cursor.lastrowid
        cursor.execute(
            "INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) VALUES (?, NULL, ?, ?)",
            (id_ticket, quantite, prix))
        return id_ticket, cursor.lastrowid

    def _rembourser(self, cursor, vd_id, quantite=1):
        return self.dm.enregistrer_remboursement(
            cursor, "SHOP-1001", vd_id, None, 0, "Espèces", "Admin", "2026-10-11 09:00:00", quantite=quantite)

    def _connexion(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_la_meme_ligne_ne_se_rembourse_pas_deux_fois(self):
        conn = self._connexion()
        try:
            c = conn.cursor()
            _, vd = self._ligne_sans_stock(c, quantite=1)
            nouveau, montant = self._rembourser(c, vd)
            self.assertTrue(nouveau.startswith("REF-SHOP-1001"))
            self.assertEqual(str(montant), "-50.00")
            with self.assertRaises(ValueError) as ctx:
                self._rembourser(c, vd)
            self.assertIn("0 unité(s) restent remboursables", str(ctx.exception))
            # une seule écriture de remboursement dans le journal de caisse
            c.execute("SELECT COUNT(*) FROM Ledger_Caisse WHERE type_mouvement = 'REMBOURSEMENT'")
            self.assertEqual(c.fetchone()[0], 1)
        finally:
            conn.close()

    def test_le_remboursement_laisse_une_trace_negative_liee_a_la_ligne(self):
        conn = self._connexion()
        try:
            c = conn.cursor()
            _, vd = self._ligne_sans_stock(c, quantite=1)
            self._rembourser(c, vd)
            c.execute("SELECT quantite, id_stock FROM Ventes_Details WHERE refund_of_vd_id = ?", (vd,))
            lignes = c.fetchall()
            self.assertEqual([(l[0], l[1]) for l in lignes], [(-1, None)])
        finally:
            conn.close()

    def test_quantite_partielle_le_total_rembourse_ne_depasse_jamais_le_vendu(self):
        conn = self._connexion()
        try:
            c = conn.cursor()
            _, vd = self._ligne_sans_stock(c, quantite=2)
            self._rembourser(c, vd, quantite=1)
            self._rembourser(c, vd, quantite=1)
            with self.assertRaises(ValueError):
                self._rembourser(c, vd, quantite=1)
            c.execute("SELECT COALESCE(SUM(-quantite), 0) FROM Ventes_Details WHERE refund_of_vd_id = ?", (vd,))
            self.assertEqual(c.fetchone()[0], 2)
        finally:
            conn.close()

    def test_la_quantite_remboursable_affichee_decroit(self):
        conn = self._connexion()
        try:
            c = conn.cursor()
            _, vd = self._ligne_sans_stock(c, quantite=2)
            conn.commit()
            avant = self.dm.rechercher_ticket_pour_remboursement("SHOP-1001", conn=conn)
            self._rembourser(c, vd, quantite=1)
            conn.commit()
            apres = self.dm.rechercher_ticket_pour_remboursement("SHOP-1001", conn=conn)
        finally:
            conn.close()
        restant = lambda r: [l for l in (r.get("lignes") or r.get("articles") or [])]
        self.assertTrue(avant, "le ticket doit être retrouvé")
        self.assertNotEqual(str(avant), str(apres), "l'écran Retours doit refléter le remboursement déjà fait")


if __name__ == "__main__":
    unittest.main()
