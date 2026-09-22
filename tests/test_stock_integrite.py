# -*- coding: utf-8 -*-
"""Intégrité du stock : conservation, concurrence, déclinaisons, seuils, drapeau d'audit.

Chaque test ci-dessous a d'abord échoué sur le code en place ; les commentaires nomment
le défaut évité. Base temporaire à chaque test : aucune donnée réelle touchée.
"""
import datetime
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.db.connection import get_connection
from kodo_core.domain.catalog.inventory_manager import InventoryManager, StockHistoriqueError
from kodo_core.domain.sales.cart_engine import process_sale_transaction


class _BaseStock(unittest.TestCase):
    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    # -- helpers ---------------------------------------------------------------------------------
    def produit(self, nom):
        return next(p for p in InventoryManager.get_all_products() if p["name"] == nom)

    def lignes_stock(self, nom):
        """(taille, quantité) de toutes les lignes de Stocks d'un article, ordre stable."""
        with get_connection() as conn:
            return [
                (r[0], r[1])
                for r in conn.cursor().execute(
                    "SELECT s.taille, s.quantite_actuelle FROM Stocks s "
                    "JOIN Produits p ON p.id = s.id_produit WHERE p.nom = ? ORDER BY s.id",
                    (nom,),
                ).fetchall()
            ]

    def total_stock(self, nom):
        return sum(q for _, q in self.lignes_stock(nom))

    def vendre(self, nom, taille=None, qte=1):
        prod = self.produit(nom)
        item = {"product_id": prod["product_id"], "quantite": qte}
        if taille is not None:
            item["taille"] = taille
        conn = get_connection()
        try:
            return process_sale_transaction(
                cart_items=[item],
                total_tvac=float(prod["price"]) * qte,
                payments=[("CB", float(prod["price"]) * qte)],
                conn=conn,
            )
        finally:
            conn.close()


class TestConservation(_BaseStock):
    """Le compteur doit être reconstructible à partir des mouvements, à chaque étape."""

    def test_vente_remboursement_partiel_solde_puis_inventaire(self):
        InventoryManager.save_product({"name": "Robe", "price": 50, "sizes": "S:5|M:5"})
        self.assertEqual(self.lignes_stock("Robe"), [("S", 5), ("M", 5)])

        self.vendre("Robe", taille="M", qte=3)
        self.assertEqual(self.lignes_stock("Robe"), [("S", 5), ("M", 2)], "vente de 3 M")

        conn = get_connection()
        cur = conn.cursor()
        vd_id, id_stock, id_ticket = cur.execute(
            "SELECT id, id_stock, id_ticket FROM Ventes_Details ORDER BY id LIMIT 1"
        ).fetchone()
        numero = cur.execute("SELECT numero_ticket FROM Tickets WHERE id=?", (id_ticket,)).fetchone()[0]

        cur.execute("BEGIN IMMEDIATE")
        database_manager.enregistrer_remboursement(
            cur, numero, vd_id, id_stock, 50.0, "CB", "Test", self._maintenant(), quantite=1
        )
        conn.commit()
        self.assertEqual(self.lignes_stock("Robe"), [("S", 5), ("M", 3)], "remboursement partiel de 1")

        # HORS PÉRIMÈTRE, contourné ici : le numéro du ticket de remboursement est
        # « REF-<ticket>-<secondes> » (database_manager.py:1103) alors que
        # Tickets.numero_ticket est UNIQUE — deux remboursements de la même ligne dans
        # la même seconde se heurtent. Cette attente rend le test déterministe ; le
        # défaut lui-même est signalé au rapport.
        time.sleep(1.1)

        cur.execute("BEGIN IMMEDIATE")
        database_manager.enregistrer_remboursement(
            cur, numero, vd_id, id_stock, 50.0, "CB", "Test", self._maintenant(), quantite=2
        )
        conn.commit()
        self.assertEqual(self.lignes_stock("Robe"), [("S", 5), ("M", 5)], "remboursement du solde")

        # Au-delà du vendu, plus rien n'est remboursable : sinon le stock se reconstituerait
        # à partir de rien et ne serait plus reconstructible depuis les mouvements.
        with self.assertRaises(ValueError):
            cur.execute("BEGIN IMMEDIATE")
            database_manager.enregistrer_remboursement(
                cur, numero, vd_id, id_stock, 50.0, "CB", "Test", self._maintenant(), quantite=1
            )
        conn.rollback()

        # Le compteur se reconstruit exactement : initial - somme des lignes de vente.
        solde_lignes = cur.execute(
            "SELECT COALESCE(SUM(quantite), 0) FROM Ventes_Details WHERE id_stock = ?", (id_stock,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(solde_lignes, 0, "3 vendus - 3 remboursés")
        self.assertEqual(dict(self.lignes_stock("Robe"))["M"], 5 - solde_lignes)

        # Inventaire : recomptage physique à 4 par l'écran Stocks.
        prod = self.produit("Robe")
        InventoryManager.save_product({"id": prod["product_id"], "name": "Robe", "price": 50, "sizes": "S:5|M:4"})
        self.assertEqual(self.lignes_stock("Robe"), [("S", 5), ("M", 4)])

    @staticmethod
    def _maintenant():
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TestConcurrence(_BaseStock):
    """Deux décréments simultanés sur la même ligne ne doivent jamais produire un total faux."""

    def test_deux_caisses_simultanees_sur_la_meme_ligne(self):
        InventoryManager.save_product({"name": "Casquette", "price": 20, "stock": 10})
        stock_id = self.produit("Casquette")["stocks"][0]["stock_id"]

        depart = threading.Barrier(2)
        incidents = []

        def encaisser():
            conn = get_connection()
            try:
                depart.wait(timeout=10)
                for _ in range(5):
                    process_sale_transaction(
                        cart_items=[{"stock_id": stock_id, "quantite": 1}],
                        total_tvac=20.0,
                        payments=[("CB", 20.0)],
                        conn=conn,
                    )
            except Exception as exc:  # noqa: BLE001 - on veut le motif exact dans le rapport
                incidents.append(f"{type(exc).__name__}: {exc}")
            finally:
                conn.close()

        fils = [threading.Thread(target=encaisser) for _ in range(2)]
        for f in fils:
            f.start()
        for f in fils:
            f.join(timeout=60)

        self.assertEqual(incidents, [], "aucun encaissement ne doit échouer (BEGIN IMMEDIATE + busy_timeout)")
        with get_connection() as conn:
            restant = conn.cursor().execute(
                "SELECT quantite_actuelle FROM Stocks WHERE id=?", (stock_id,)
            ).fetchone()[0]
            vendu = conn.cursor().execute(
                "SELECT COALESCE(SUM(quantite), 0) FROM Ventes_Details WHERE id_stock=?", (stock_id,)
            ).fetchone()[0]
        # Sans BEGIN IMMEDIATE, les deux fils lisaient le même stock et le total dérivait.
        self.assertEqual(vendu, 10)
        self.assertEqual(restant, 10 - vendu)


class TestDeclinaisons(_BaseStock):
    """Une vente d'un M ne doit jamais toucher une autre ligne, et deux libellés
    équivalents ne doivent jamais donner deux lignes concurrentes."""

    def test_libelles_equivalents_fusionnes_sans_perte(self):
        # « M », « m » et «  M  » désignent la même déclinaison pour le moteur de vente
        # (cart_engine._norm_taille). save_product les traitait comme trois tailles
        # distinctes puis n'en gardait que deux : 3 unités disparaissaient sans trace.
        InventoryManager.save_product({"name": "Jean", "price": 40, "sizes": "M:3|m:4| M :5"})
        lignes = self.lignes_stock("Jean")
        self.assertEqual(len(lignes), 1, f"une seule ligne pour une seule taille réelle : {lignes}")
        self.assertEqual(self.total_stock("Jean"), 12, "aucune unité déclarée ne disparaît")

    def test_vente_dune_taille_ne_debite_pas_une_autre(self):
        InventoryManager.save_product({"name": "Pull", "price": 30, "sizes": "S:5|M:5|L:5"})
        self.vendre("Pull", taille="m", qte=2)  # casse libre côté écran
        self.assertEqual(self.lignes_stock("Pull"), [("S", 5), ("M", 3), ("L", 5)])

    def test_edition_dune_taille_ne_change_pas_son_identifiant_de_stock(self):
        # Le Stocks.id est l'ancre des lignes de vente (Ventes_Details.id_stock) : le
        # recréer à chaque changement de casse orphelinait l'historique fiscal.
        InventoryManager.save_product({"name": "Pull", "price": 30, "sizes": "S:5|M:5"})
        avant = {s["size"]: s["stock_id"] for s in self.produit("Pull")["stocks"]}
        pid = self.produit("Pull")["product_id"]
        InventoryManager.save_product({"id": pid, "name": "Pull", "price": 30, "sizes": "s:6|m:7"})
        apres = {s["size"].strip().casefold(): s["stock_id"] for s in self.produit("Pull")["stocks"]}
        self.assertEqual(apres["s"], avant["S"])
        self.assertEqual(apres["m"], avant["M"])

    def test_article_importe_en_taille_unique_nest_pas_duplique(self):
        # L'import Shopify écrit « Unique », l'écran Stocks « Taille Unique ». save_product
        # ne reconnaissait que le second : enregistrer un article Shopify créait une
        # SECONDE ligne et doublait le stock total de l'article.
        InventoryManager.save_product({"name": "Bougie", "price": 15, "stock": 8})
        pid = self.produit("Bougie")["product_id"]
        with get_connection() as conn:
            conn.cursor().execute("UPDATE Stocks SET taille='Unique' WHERE id_produit=?", (pid,))
            conn.commit()

        InventoryManager.save_product({"id": pid, "name": "Bougie", "price": 15, "stock": 8})
        self.assertEqual(len(self.lignes_stock("Bougie")), 1, self.lignes_stock("Bougie"))
        self.assertEqual(self.total_stock("Bougie"), 8, "le stock ne doit pas doubler")


class TestHistoriqueFiscal(_BaseStock):
    """Une ligne de vente passée ne doit jamais disparaître, ni son ancre de stock."""

    def test_retirer_une_taille_deja_vendue_conserve_la_ligne_de_vente(self):
        InventoryManager.save_product({"name": "Robe", "price": 50, "sizes": "S:5|M:5"})
        self.vendre("Robe", taille="M", qte=2)
        pid = self.produit("Robe")["product_id"]

        # Avant : sqlite3.IntegrityError « FOREIGN KEY constraint failed » remontée
        # jusqu'à un HTTP 500 — la commerçante ne pouvait plus enregistrer son article.
        InventoryManager.save_product({"id": pid, "name": "Robe", "price": 50, "sizes": "S:5"})

        lignes = dict(self.lignes_stock("Robe"))
        self.assertEqual(lignes["S"], 5)
        self.assertEqual(lignes.get("M"), 0, "la taille vendue est conservée à 0, pas supprimée")
        with get_connection() as conn:
            self.assertEqual(
                conn.cursor().execute("SELECT COUNT(*) FROM Ventes_Details").fetchone()[0], 1,
                "la ligne de vente survit",
            )

    def test_retirer_une_taille_jamais_vendue_la_supprime_bien(self):
        InventoryManager.save_product({"name": "Robe", "price": 50, "sizes": "S:5|M:5"})
        pid = self.produit("Robe")["product_id"]
        InventoryManager.save_product({"id": pid, "name": "Robe", "price": 50, "sizes": "S:5"})
        self.assertEqual(self.lignes_stock("Robe"), [("S", 5)])

    def test_supprimer_un_produit_vendu_est_refuse_avec_un_message_clair(self):
        InventoryManager.save_product({"name": "Robe", "price": 50, "sizes": "S:5|M:5"})
        self.vendre("Robe", taille="M", qte=1)
        pid = self.produit("Robe")["product_id"]

        with self.assertRaises(StockHistoriqueError) as ctx:
            InventoryManager.delete_product(pid)
        message = str(ctx.exception)
        self.assertIn("Robe", message)
        self.assertNotIn("FOREIGN KEY", message, "le message SQL brut ne doit pas remonter à l'écran")

        with get_connection() as conn:
            self.assertEqual(conn.cursor().execute("SELECT COUNT(*) FROM Ventes_Details").fetchone()[0], 1)
            self.assertEqual(conn.cursor().execute("SELECT COUNT(*) FROM Produits WHERE id=?", (pid,)).fetchone()[0], 1)

    def test_supprimer_un_produit_jamais_vendu_fonctionne(self):
        InventoryManager.save_product({"name": "Echarpe", "price": 10, "stock": 4})
        pid = self.produit("Echarpe")["product_id"]
        self.assertTrue(InventoryManager.delete_product(pid))
        with get_connection() as conn:
            self.assertEqual(conn.cursor().execute("SELECT COUNT(*) FROM Produits WHERE id=?", (pid,)).fetchone()[0], 0)
            self.assertEqual(
                conn.cursor().execute("SELECT COUNT(*) FROM Stocks WHERE id_produit=?", (pid,)).fetchone()[0], 0
            )


class TestSeuilsAlerte(_BaseStock):
    """Un seuil NULL suit le seuil du produit puis le seuil global — jamais un 0 implicite."""

    def test_seuil_null_suit_le_seuil_du_produit_et_non_le_seuil_global(self):
        InventoryManager.save_product({"name": "Echarpe", "price": 10, "stock": 3, "alert_threshold": 20})
        pid = self.produit("Echarpe")["product_id"]
        with get_connection() as conn:
            # Cas réel : ligne créée par l'import Shopify, qui écrit seuil_alerte = NULL.
            conn.cursor().execute("UPDATE Stocks SET seuil_alerte=NULL WHERE id_produit=?", (pid,))
            conn.commit()

        prod = self.produit("Echarpe")
        alerte = next(a for a in InventoryManager.get_low_stock_alerts() if a["product_name"] == "Echarpe")
        # L'écran affichait 5 (seuil global) là où le moteur d'alertes déclenchait à 20 :
        # deux seuils différents pour la même ligne, donc une alerte incompréhensible.
        self.assertEqual(prod["stocks"][0]["alert_threshold"], alerte["alert_threshold"])
        self.assertEqual(prod["stocks"][0]["alert_threshold"], 20)

    def test_un_seuil_null_ne_se_comporte_pas_comme_un_zero(self):
        InventoryManager.set_default_alert_threshold(5)
        InventoryManager.save_product({"name": "Bonnet", "price": 8, "stock": 2})
        with get_connection() as conn:
            conn.cursor().execute(
                "UPDATE Stocks SET seuil_alerte=NULL WHERE id_produit=?", (self.produit("Bonnet")["product_id"],)
            )
            conn.cursor().execute("UPDATE Produits SET seuil_alerte=NULL WHERE nom='Bonnet'")
            conn.commit()
        noms = [a["product_name"] for a in InventoryManager.get_low_stock_alerts()]
        # Un NULL traité comme 0 aurait masqué cette vraie alerte (2 <= 5).
        self.assertIn("Bonnet", noms)

    def test_une_quantite_negative_est_refusee(self):
        # Le déclencheur prevent_negative_stock n'est PAS posé par database_manager.initialiser_db
        # (chemin réel des postes) : sans ce garde-fou applicatif, « M:-3 » s'écrivait tel quel
        # et faussait le stock total du catalogue.
        with self.assertRaises(ValueError):
            InventoryManager.save_product({"name": "Gant", "price": 12, "sizes": "S:2|M:-3"})
        self.assertEqual(
            [p for p in InventoryManager.get_all_products() if p["name"] == "Gant"], [],
            "l'enregistrement entier est annulé",
        )

    def test_un_seuil_zero_explicite_nalerte_quen_rupture(self):
        InventoryManager.save_product({"name": "Sac", "price": 60, "stock": 1, "alert_threshold": 0})
        self.assertNotIn("Sac", [a["product_name"] for a in InventoryManager.get_low_stock_alerts()])
        self.vendre("Sac", qte=1)
        alerte = next(a for a in InventoryManager.get_low_stock_alerts() if a["product_name"] == "Sac")
        self.assertEqual(alerte["status"], "RUPTURE")


class TestDrapeauAudit(_BaseStock):
    """Un drapeau qu'on ne peut ni voir ni éteindre est un drapeau inutile."""

    def _lever_le_drapeau(self, nom):
        with get_connection() as conn:
            conn.cursor().execute(
                "UPDATE Stocks SET requires_stock_audit=1 WHERE id_produit=(SELECT id FROM Produits WHERE nom=?)",
                (nom,),
            )
            conn.cursor().execute("UPDATE Produits SET requires_stock_audit=1 WHERE nom=?", (nom,))
            conn.commit()

    def test_le_drapeau_est_visible_depuis_le_catalogue_et_les_alertes(self):
        InventoryManager.save_product({"name": "Echarpe", "price": 10, "stock": 0})
        self._lever_le_drapeau("Echarpe")
        prod = self.produit("Echarpe")
        self.assertTrue(prod["requires_stock_audit"])
        self.assertTrue(prod["stocks"][0]["requires_stock_audit"])
        alerte = next(a for a in InventoryManager.get_low_stock_alerts() if a["product_name"] == "Echarpe")
        self.assertTrue(alerte["requires_stock_audit"])

    def test_le_recomptage_eteint_le_drapeau(self):
        InventoryManager.save_product({"name": "Echarpe", "price": 10, "stock": 3})
        self._lever_le_drapeau("Echarpe")
        pid = self.produit("Echarpe")["product_id"]
        # Le recomptage physique saisi dans l'écran Stocks EST l'audit demandé.
        InventoryManager.save_product({"id": pid, "name": "Echarpe", "price": 10, "stock": 3})
        prod = self.produit("Echarpe")
        self.assertFalse(prod["requires_stock_audit"])
        self.assertFalse(prod["stocks"][0]["requires_stock_audit"])

    def test_une_taille_non_recomptee_maintient_le_drapeau_du_produit(self):
        InventoryManager.save_product({"name": "Robe", "price": 50, "sizes": "S:5|M:5"})
        self.vendre("Robe", taille="M", qte=1)  # M devient une ancre fiscale, non supprimable
        pid = self.produit("Robe")["product_id"]
        with get_connection() as conn:
            conn.cursor().execute(
                "UPDATE Stocks SET requires_stock_audit=1 WHERE id_produit=? AND taille='M'", (pid,)
            )
            conn.cursor().execute("UPDATE Produits SET requires_stock_audit=1 WHERE id=?", (pid,))
            conn.commit()

        # La charge ne porte que S : M n'est pas recompté, son drapeau reste levé,
        # donc celui du produit aussi — sinon un audit réel serait classé sans avoir eu lieu.
        InventoryManager.save_product({"id": pid, "name": "Robe", "price": 50, "sizes": "S:6"})
        prod = self.produit("Robe")
        self.assertTrue(prod["requires_stock_audit"])
        par_taille = {s["size"]: s["requires_stock_audit"] for s in prod["stocks"]}
        self.assertFalse(par_taille["S"])
        self.assertTrue(par_taille["M"])


if __name__ == "__main__":
    unittest.main()
