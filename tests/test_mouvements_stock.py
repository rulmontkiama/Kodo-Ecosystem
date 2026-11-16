# -*- coding: utf-8 -*-
"""
Kōdo POS — mouvements de stock justifiés (motifs) et historique.

Garanties vérifiées :
- une entrée / sortie / un recomptage modifie `Stocks` ET écrit la trace (motif, avant → après, qui) ;
- un stock ne passe jamais sous zéro, un motif incohérent ou sans précision obligatoire est refusé ;
- l'historique réunit les mouvements saisis et les ventes / retours, sans copier les ventes ;
- une base créée avant les mouvements (journal sans colonnes `motif`…) est mise à niveau sans perte.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db, get_connection  # noqa: E402
from kodo_core.api.routes.products_routes import handle_products_request  # noqa: E402
from kodo_core.db import stock_journal  # noqa: E402
from kodo_core.services import mouvements_stock as ms  # noqa: E402


class TestMouvementsStock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "mvt.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()
        code, rep = handle_products_request(
            "POST", "/api/products", {}, {"name": "Robe", "price": 50, "sizes": "S:4 | M:6", "barcode": "2222"})
        self.assertEqual(code, 200, rep)
        self.pid = int(rep["productId"])

    def tearDown(self):
        database_manager.DB_NAME = self._origine
        os.environ.pop("KODO_DB_PATH", None)
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass

    def _qte(self, taille):
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT quantite_actuelle FROM Stocks WHERE id_produit=? AND taille=?", (self.pid, taille))
            return c.fetchone()[0]
        finally:
            conn.close()

    def _saisis(self, **filtres):
        """Mouvements saisis avec un motif (la création de la fiche écrit aussi ses propres traces)."""
        h = ms.lister_mouvements(dict(filtres))["mouvements"]
        return [m for m in h if m["motif"] in ms.MOTIFS]

    def _mvt(self, **kw):
        base = {"product_id": self.pid, "taille": "M", "utilisateur": "Sophie"}
        base.update(kw)
        return ms.enregistrer_mouvement(base)

    def test_entree_augmente_le_stock_et_laisse_une_trace(self):
        r = self._mvt(sens="entree", motif="reception_fournisseur", quantite=5, reference="BL-12")
        self.assertEqual((r["avant"], r["apres"], r["delta"]), (6, 11, 5))
        self.assertEqual(self._qte("M"), 11)
        h = self._saisis()
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["motif"], "reception_fournisseur")
        self.assertEqual(h[0]["utilisateur"], "Sophie")
        self.assertEqual(h[0]["reference"], "BL-12")
        self.assertEqual(h[0]["delta"], 5)

    def test_sortie_diminue_et_ne_passe_jamais_sous_zero(self):
        self._mvt(sens="sortie", motif="casse", quantite=2)
        self.assertEqual(self._qte("M"), 4)
        with self.assertRaises(ms.MouvementError) as ctx:
            self._mvt(sens="sortie", motif="vol", quantite=5)
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(self._qte("M"), 4)

    def test_recomptage_ecrit_lecart_et_solde_laudit(self):
        conn = get_connection()
        conn.execute("UPDATE Stocks SET requires_stock_audit=1 WHERE id_produit=?", (self.pid,))
        conn.execute("UPDATE Produits SET requires_stock_audit=1 WHERE id=?", (self.pid,))
        conn.commit()
        conn.close()
        r = self._mvt(sens="recomptage", motif="inventaire", quantite=3)
        self.assertEqual((r["avant"], r["apres"], r["delta"]), (6, 3, -3))
        conn = get_connection()
        flag = conn.execute("SELECT requires_stock_audit FROM Stocks WHERE id_produit=? AND taille='M'", (self.pid,)).fetchone()[0]
        conn.close()
        self.assertEqual(flag, 0)

    def test_recomptage_sans_ecart_est_trace(self):
        self._mvt(sens="recomptage", motif="inventaire", quantite=6)
        h = self._saisis()
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["delta"], 0)

    def test_refus_metier(self):
        cas = [
            dict(sens="entree", motif="casse", quantite=1),             # motif d'une autre nature
            dict(sens="entree", motif="inconnu", quantite=1),
            dict(sens="entree", motif="autre_entree", quantite=1),      # note obligatoire
            dict(sens="entree", motif="reception_fournisseur", quantite=0),
            dict(sens="entree", motif="reception_fournisseur", quantite=1.5),
            dict(sens="sortie", motif="casse", quantite="abc"),
            dict(sens="avance", motif="casse", quantite=1),
        ]
        for c in cas:
            with self.subTest(c=c):
                with self.assertRaises(ms.MouvementError):
                    self._mvt(**c)
        self.assertEqual(self._qte("M"), 6)

    def test_taille_obligatoire_si_plusieurs_tailles(self):
        with self.assertRaises(ms.MouvementError):
            ms.enregistrer_mouvement({"product_id": self.pid, "sens": "entree",
                                      "motif": "reception_fournisseur", "quantite": 1})
        with self.assertRaises(ms.MouvementError) as ctx:
            self._mvt(taille="XXL", sens="entree", motif="reception_fournisseur", quantite=1)
        self.assertEqual(ctx.exception.code, 404)

    def test_les_ventes_apparaissent_sans_etre_copiees(self):
        conn = get_connection()
        c = conn.cursor()
        sid = c.execute("SELECT id FROM Stocks WHERE id_produit=? AND taille='S'", (self.pid,)).fetchone()[0]
        c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, vendeur_nom) "
                  "VALUES ('T-1', '2026-11-10 10:00:00', 50, 'Léa')")
        tid = c.lastrowid
        c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) VALUES (?, ?, 2, 50)", (tid, sid))
        conn.commit()
        conn.close()
        ventes = ms.lister_mouvements({"source": "vente"})["mouvements"]
        self.assertEqual(len(ventes), 1)
        self.assertEqual((ventes[0]["delta"], ventes[0]["motif"], ventes[0]["reference"]), (-2, "vente", "T-1"))
        # Aucune ligne de vente copiée dans le journal.
        conn = get_connection()
        copies = conn.execute("SELECT COUNT(*) FROM Stock_Journal WHERE origine IN ('vente', 'retour_vente')").fetchone()[0]
        conn.close()
        self.assertEqual(copies, 0)
        self.assertEqual(ms.lister_mouvements({})["totaux"]["sorties"], 2)

    def test_filtres_motif_sens_et_dates(self):
        self._mvt(sens="entree", motif="reception_fournisseur", quantite=4)
        self._mvt(sens="sortie", motif="vol", quantite=1)
        self.assertEqual(ms.lister_mouvements({"motif": "vol"})["total"], 1)
        self.assertEqual(ms.lister_mouvements({"motif": "reception_fournisseur"})["totaux"]["entrees"], 4)
        self.assertEqual(ms.lister_mouvements({"sens": "sortie"})["totaux"]["sorties"], 1)
        self.assertEqual(ms.lister_mouvements({"du": "2999-01-01"})["total"], 0)
        with self.assertRaises(ms.MouvementError):
            ms.lister_mouvements({"du": "pas-une-date"})

    def test_route_api(self):
        code, rep = handle_products_request("POST", "/api/stock/mouvements", {}, {
            "product_id": self.pid, "taille": "S", "sens": "sortie", "motif": "don_marketing", "quantite": 1})
        self.assertEqual(code, 200, rep)
        code, rep = handle_products_request("POST", "/api/stock/mouvements", {}, {
            "product_id": self.pid, "taille": "S", "sens": "sortie", "motif": "don_marketing", "quantite": 99})
        self.assertEqual(code, 409)
        code, rep = handle_products_request("GET", "/api/stock/mouvements", {"motif": ["don_marketing"]}, {})
        self.assertEqual((code, rep["total"]), (200, 1))
        code, rep = handle_products_request("GET", "/api/stock/motifs", {}, {})
        self.assertTrue(any(m["code"] == "casse" and m["sens"] == "sortie" for m in rep))

    def test_base_ancienne_sans_colonnes_de_mouvement(self):
        conn = get_connection()
        conn.execute("DROP TABLE IF EXISTS Stock_Journal")
        conn.execute("""CREATE TABLE Stock_Journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date_heure TEXT NOT NULL, stock_id INTEGER NOT NULL,
            produit_id INTEGER, produit TEXT, taille TEXT, avant INTEGER, apres INTEGER,
            origine TEXT NOT NULL, detail TEXT)""")
        conn.execute("INSERT INTO Stock_Journal (date_heure, stock_id, produit_id, produit, taille, avant, apres, origine, detail) "
                     "VALUES ('2026-09-01 09:00:00', 1, ?, 'Robe', 'M', 3, 8, 'fiche_produit', 'ancienne trace')", (self.pid,))
        conn.commit()
        conn.close()
        # Lecture d'une ancienne trace avant toute mise à niveau.
        anciens = ms.lister_mouvements({})["mouvements"]
        self.assertEqual(anciens[0]["delta"], 5)
        self.assertEqual(anciens[0]["motif_libelle"], "Modification depuis la fiche produit")
        # Un mouvement ajoute les colonnes et garde l'ancienne ligne.
        self._mvt(sens="entree", motif="article_retrouve", quantite=1)
        h = ms.lister_mouvements({"motif": "article_retrouve"})["mouvements"]
        self.assertEqual(len(h), 1)
        self.assertEqual(ms.lister_mouvements({"motif": "fiche_produit"})["total"], 1)  # l'ancienne trace est conservée


if __name__ == "__main__":
    unittest.main()
