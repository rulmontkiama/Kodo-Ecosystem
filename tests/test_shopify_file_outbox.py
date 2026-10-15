# -*- coding: utf-8 -*-
"""
Outbox des mouvements de stock vers Shopify (`sync_queue`) :
écriture dans la transaction de la vente, recul exponentiel par ligne, ordre FIFO,
mutation idempotente sans repli REST après une coupure, migration additive d'une base 2.0.9.

Aucun appel externe : moteur bouchonné, base SQLite jetable.
"""
import datetime
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import database_manager  # noqa: E402
from test_shopify_sync import (  # noqa: E402
    BaseTemporaire, MoteurBouchonne, REPONSE_LOCATIONS, reponse_graphql,
)
from kodo_core.db import shopify_file  # noqa: E402
from kodo_core.db.migrations import MigrationManager  # noqa: E402
from kodo_core.domain.sales.cart_engine import process_sale_transaction  # noqa: E402

PASSE = "2000-01-01 00:00:00"


def activer_poussee(testcase):
    testcase.ecrire("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                    (shopify_file.PARAM_SEUIL_TICKETS, "0"))


class TestRecul(unittest.TestCase):
    def test_delai_exponentiel_plafonne_a_15_minutes(self):
        self.assertEqual([shopify_file.delai_backoff(n) for n in range(1, 8)],
                         [30, 60, 120, 240, 480, 900, 900])
        self.assertEqual(shopify_file.delai_backoff(0), 30)
        self.assertEqual(shopify_file.delai_backoff(10_000), 900)

    def test_prochaine_tentative_et_echeance(self):
        t0 = datetime.datetime(2026, 10, 15, 12, 0, 0)
        p = shopify_file.prochaine_tentative(3, t0)
        self.assertEqual(p, "2026-10-15 12:02:00")
        self.assertFalse(shopify_file.est_due(p, t0))
        self.assertTrue(shopify_file.est_due(p, t0 + datetime.timedelta(seconds=120)))
        self.assertTrue(shopify_file.est_due(None))


class TestInscriptionDansLaTransactionDeLaVente(BaseTemporaire):

    def setUp(self):
        super().setUp()
        pid = self.creer_produit("CB-1", "Article", [("Unique", 9)])
        self.stock_id = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]

    def vendre(self, qte=2):
        return process_sale_transaction(
            cart_items=[{"stock_id": self.stock_id, "quantite": qte}],
            payments=[("CB", 10.0 * qte)], total_tvac=10.0 * qte)

    def test_la_ligne_naît_avec_la_vente(self):
        activer_poussee(self)
        res = self.vendre(2)
        self.assertEqual(self.rows("SELECT id_ticket, delta, statut, tentatives, prochaine_tentative "
                                   "FROM sync_queue"),
                         [(res["ticket_id"], -2, "PENDING", 0, None)])

    def test_sans_activation_rien_ne_s_accumule(self):
        self.vendre(1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sync_queue"), [(0,)])

    def test_un_rollback_emporte_la_ligne_avec_la_vente(self):
        activer_poussee(self)
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute("BEGIN IMMEDIATE")
            database_manager.enregistrer_vente(
                cursor=c, numero_ticket="T-ROLL", total_tvac=10.0, total_htva=8.26, total_tva=1.74,
                remise=0.0, methode_paiement="CB", id_client=None, rendu_monnaie=0.0,
                panier=[{"stock_id": self.stock_id, "prix_vente_tvac": 10.0, "quantite": 1}],
                vendeur_nom="T", date_heure="2026-10-15 10:00:00", paiements=[("CB", 10.0)])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0], 1)
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sync_queue"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Tickets WHERE numero_ticket = 'T-ROLL'"), [(0,)])

    def test_une_commande_web_deja_synchronisee_n_est_pas_renvoyee(self):
        activer_poussee(self)
        tid = self.creer_ticket("WEB-1", [(self.stock_id, 1)])
        self.ecrire("UPDATE Tickets SET synced_shopify = 1 WHERE id = ?", (tid,))
        conn = database_manager.get_connection()
        try:
            self.assertEqual(shopify_file.inscrire_ticket(conn.cursor(), tid), 0)
        finally:
            conn.close()

    def test_une_panne_de_la_file_ne_bloque_pas_la_vente(self):
        activer_poussee(self)
        self.ecrire("CREATE TRIGGER refuse_file BEFORE INSERT ON sync_queue "
                    "BEGIN SELECT RAISE(ABORT, 'file indisponible'); END")
        res = self.vendre(1)
        self.assertTrue(res["success"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sync_queue"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Tickets WHERE id = ?", (res["ticket_id"],)), [(1,)])


class TestMigrationDepuis209(BaseTemporaire):
    """Base figée dans l'état 2.0.9 : la colonne s'ajoute, aucune ligne n'est touchée."""

    SQL_209 = """CREATE TABLE sync_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_vente_detail INTEGER NOT NULL UNIQUE,
        id_ticket INTEGER NOT NULL,
        code_barre TEXT,
        delta INTEGER NOT NULL,
        statut TEXT NOT NULL DEFAULT 'PENDING',
        tentatives INTEGER NOT NULL DEFAULT 0,
        derniere_erreur TEXT,
        date_creation TEXT NOT NULL,
        date_maj TEXT NOT NULL
    )"""

    def figer_en_209(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP TABLE sync_queue")
            conn.execute(self.SQL_209)
            conn.execute("INSERT INTO sync_queue (id_vente_detail, id_ticket, code_barre, delta, statut, "
                         "tentatives, derniere_erreur, date_creation, date_maj) "
                         "VALUES (7, 3, 'CB-7', -2, 'PENDING', 4, 'refus', '2026-10-01 10:00:00', "
                         "'2026-10-01 10:05:00')")
            conn.execute("DELETE FROM schema_version WHERE version = '2.1.2'")
            conn.commit()
        finally:
            conn.close()

    def colonnes(self):
        return [r[1] for r in self.rows("PRAGMA table_info(sync_queue)")]

    def test_la_migration_ajoute_la_colonne_sans_toucher_aux_lignes(self):
        self.figer_en_209()
        self.assertNotIn("prochaine_tentative", self.colonnes())

        MigrationManager.run_migrations(db_path=self.path)

        self.assertIn("prochaine_tentative", self.colonnes())
        self.assertEqual(
            self.rows("SELECT id_vente_detail, id_ticket, code_barre, delta, statut, tentatives, "
                      "derniere_erreur, prochaine_tentative FROM sync_queue"),
            [(7, 3, "CB-7", -2, "PENDING", 4, "refus", None)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM schema_version WHERE version = '2.1.2'"), [(1,)])

    def test_la_migration_est_rejouable(self):
        self.figer_en_209()
        MigrationManager.run_migrations(db_path=self.path)
        self.ecrire("DELETE FROM schema_version WHERE version = '2.1.2'")
        MigrationManager.run_migrations(db_path=self.path)  # colonne déjà là : sans erreur
        self.assertEqual(self.colonnes().count("prochaine_tentative"), 1)

    def test_une_ligne_sans_delai_est_tentee_tout_de_suite(self):
        self.figer_en_209()
        MigrationManager.run_migrations(db_path=self.path)
        self.assertEqual(self.rows("SELECT prochaine_tentative FROM sync_queue"), [(None,)])
        self.assertTrue(shopify_file.est_due(None))


class _Base(BaseTemporaire):
    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        pid = self.creer_produit("CB-1", "Article", [("Unique", 20)])
        self.stock_id = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]
        pid2 = self.creer_produit("CB-2", "Autre", [("Unique", 20)])
        self.stock_id2 = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid2,))[0][0]

    def moteur(self, adjust, graphql=None):
        return MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": graphql or (lambda e, d: reponse_graphql(1001)),
            "inventory_levels/adjust.json": adjust,
        }, store_url="boutique.myshopify.com", access_token="jeton")


class TestReculEtFifo(_Base):

    def test_un_echec_pose_un_delai_qui_double_a_chaque_essai(self):
        self.creer_ticket("T-1", [(self.stock_id, 1)])
        moteur = self.moteur(lambda e, d: None)  # Shopify refuse
        moteur.sync_tickets_to_shopify()
        n, p1 = self.rows("SELECT tentatives, prochaine_tentative FROM sync_queue")[0]
        self.assertEqual(n, 1)
        ecart1 = (datetime.datetime.strptime(p1, "%Y-%m-%d %H:%M:%S") - datetime.datetime.now()).total_seconds()
        self.assertTrue(20 <= ecart1 <= 35, ecart1)

        self.ecrire("UPDATE sync_queue SET prochaine_tentative = ?", (PASSE,))  # délai écoulé
        self.moteur(lambda e, d: None).sync_tickets_to_shopify()
        n, p2 = self.rows("SELECT tentatives, prochaine_tentative FROM sync_queue")[0]
        self.assertEqual(n, 2)
        ecart2 = (datetime.datetime.strptime(p2, "%Y-%m-%d %H:%M:%S") - datetime.datetime.now()).total_seconds()
        self.assertTrue(50 <= ecart2 <= 65, ecart2)

    def test_une_ligne_en_recul_n_est_pas_rejouee_et_ne_retarde_pas_les_autres(self):
        t1 = self.creer_ticket("T-1", [(self.stock_id, 1)])
        self.creer_ticket("T-2", [(self.stock_id2, 1)])
        # T-1 vient d'échouer : en recul.
        self.moteur(lambda e, d: None).sync_tickets_to_shopify()
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets ORDER BY id"), [(0,), (0,)])

        # T-1 reste en recul ; T-2 (sa ligne est aussi en recul ici) : on libère T-2 seulement.
        self.ecrire("UPDATE sync_queue SET prochaine_tentative = ? WHERE id_ticket != ?", (PASSE, t1))
        envoyes = []
        moteur = self.moteur(lambda e, d: (envoyes.append(d["available_adjustment"]), {"ok": 1})[1])
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(envoyes, [-1], "seule la ligne échue part")
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets ORDER BY id"), [(0,), (1,)])

    def test_les_lignes_echues_partent_dans_l_ordre_des_tickets_puis_des_lignes(self):
        self.creer_ticket("T-1", [(self.stock_id, 1), (self.stock_id2, 2)])
        self.creer_ticket("T-2", [(self.stock_id, 3)])
        envoyes = []
        moteur = self.moteur(lambda e, d: (envoyes.append(d["available_adjustment"]), {"ok": 1})[1])
        self.assertEqual(moteur.sync_tickets_to_shopify(), 2)
        self.assertEqual(envoyes, [-1, -2, -3])

    def test_un_succes_efface_le_delai(self):
        self.creer_ticket("T-1", [(self.stock_id, 1)])
        self.moteur(lambda e, d: None).sync_tickets_to_shopify()
        self.ecrire("UPDATE sync_queue SET prochaine_tentative = ?", (PASSE,))
        self.moteur(lambda e, d: {"ok": 1}).sync_tickets_to_shopify()
        self.assertEqual(self.rows("SELECT statut, prochaine_tentative FROM sync_queue"), [("DONE", None)])


class TestMutationIdempotente(_Base):

    def setUp(self):
        super().setUp()
        self.creer_ticket("T-1", [(self.stock_id, 2)])
        self.vd = self.rows("SELECT id FROM Ventes_Details")[0][0]

    def graphql(self, mutation, appels):
        def repondre(endpoint, data):
            q = (data or {}).get("query", "")
            if "inventoryAdjustQuantities" in q:
                appels.append(data)
                return mutation(data)
            return reponse_graphql(1001)
        return repondre

    def test_la_mutation_part_avec_une_cle_stable_et_sans_repli_rest(self):
        appels, rest = [], []
        moteur = self.moteur(lambda e, d: (rest.append(d), {"ok": 1})[1],
                             self.graphql(lambda d: {"data": {"inventoryAdjustQuantities": {"userErrors": []}}}, appels))
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(len(appels), 1)
        self.assertEqual(appels[0]["variables"]["key"], f"kodo-vd-{self.vd}-1001")
        self.assertIn("@idempotent(key: $key)", appels[0]["query"])
        change = appels[0]["variables"]["input"]["changes"][0]
        self.assertEqual(change["delta"], -2)
        self.assertEqual(rest, [], "GraphQL a appliqué : REST ne doit pas repartir")
        self.assertEqual(self.rows("SELECT statut FROM sync_queue"), [("DONE",)])

    def test_graphql_qui_n_execute_rien_retombe_sur_rest_une_seule_fois(self):
        appels, rest = [], []
        moteur = self.moteur(lambda e, d: (rest.append(d["available_adjustment"]), {"ok": 1})[1],
                             self.graphql(lambda d: {"errors": [{"message": "Access denied"}]}, appels))
        self.assertEqual(moteur.sync_tickets_to_shopify(), 1)
        self.assertEqual(rest, [-2])

    def test_une_coupure_apres_la_mutation_ne_retombe_jamais_sur_rest(self):
        """La mutation a pu être appliquée : la rejouer en REST (sans clé) décompterait deux fois."""
        rest = []

        class Coupure(MoteurBouchonne):
            def make_request(self, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
                if endpoint.startswith("graphql.json") and "inventoryAdjustQuantities" in (data or {}).get("query", ""):
                    self.appels.append((endpoint, data))
                    self.dernier_echec = "reseau"
                    return None
                return super().make_request(endpoint, method, data, max_retries, rejouable)

        moteur = Coupure({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": lambda e, d: reponse_graphql(1001),
            "inventory_levels/adjust.json": lambda e, d: (rest.append(d), {"ok": 1})[1],
        }, store_url="boutique.myshopify.com", access_token="jeton")
        moteur.sync_tickets_to_shopify()
        self.assertEqual(rest, [], "aucun repli REST après une coupure")
        self.assertEqual(self.rows("SELECT statut FROM sync_queue"), [("INDETERMINE",)])
        self.assertEqual(self.rows("SELECT statut FROM Shopify_Sync_Lignes"), [("INDETERMINE",)])

    def test_une_erreur_metier_graphql_laisse_la_ligne_pending_avec_recul(self):
        appels, rest = [], []
        moteur = self.moteur(lambda e, d: (rest.append(d), {"ok": 1})[1],
                             self.graphql(lambda d: {"data": {"inventoryAdjustQuantities": {
                                 "userErrors": [{"field": ["changes"], "message": "stock non suivi"}]}}}, appels))
        self.assertEqual(moteur.sync_tickets_to_shopify(), 0)
        self.assertEqual(rest, [])
        statut, essais, prochaine = self.rows("SELECT statut, tentatives, prochaine_tentative FROM sync_queue")[0]
        self.assertEqual((statut, essais), ("PENDING", 1))
        self.assertIsNotNone(prochaine)


class TestPlusDeFileMorteNiDeCollision(BaseTemporaire):

    def test_la_file_generique_et_la_table_offlinequeue_sont_retirees(self):
        with self.assertRaises(ImportError):
            import kodo_core.sync.offline_queue  # noqa: F401
        from kodo_core.sync.offline_engine import OfflineSyncEngine
        self.assertFalse(hasattr(OfflineSyncEngine, "enqueue_action"))
        self.assertFalse(hasattr(OfflineSyncEngine, "process_offline_queue"))
        OfflineSyncEngine.init_db_schema()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sqlite_master WHERE name = 'OfflineQueue'"), [(0,)])

    def test_sync_queue_n_a_qu_un_schema(self):
        cols = [r[1] for r in self.rows("PRAGMA table_info(sync_queue)")]
        self.assertIn("id_vente_detail", cols)
        self.assertIn("prochaine_tentative", cols)
        self.assertNotIn("event_type", cols)


if __name__ == "__main__":
    unittest.main()
