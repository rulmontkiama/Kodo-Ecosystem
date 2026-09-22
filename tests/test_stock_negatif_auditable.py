# -*- coding: utf-8 -*-
"""
Kōdo POS — Un stock négatif doit rester enregistrable ET signalable, sur TOUS les chemins
de création de base.

Dans Kōdo POS, un stock négatif n'est pas une erreur à interdire : c'est un état métier
légitime. Quand deux caisses hors-ligne vendent le dernier article, la vente physiquement
conclue n'est jamais rejetée (Last-Write-Wins), le compteur passe sous zéro, et
`OfflineSyncEngine.process_pending_tickets` le détecte pour lever `requires_stock_audit`.

Or `MigrationManager.initialiser_db` — l'initialiseur emprunté à l'IMPORT D'UN PACK DE
MIGRATION, c'est-à-dire quand une boutique change d'ordinateur — posait un déclencheur
`prevent_negative_stock` qui refusait toute UPDATE sur une ligne négative. Il bloquait donc
l'UPDATE qui pose le drapeau : le conflit devenait non seulement irréparable, mais MUET.
La base des postes (`database_manager.initialiser_db`) n'a jamais eu ce déclencheur, d'où
une divergence invisible entre une boutique d'origine et la même boutique après migration.

Ces tests interdisent sa réapparition, par l'un ou l'autre chemin.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _declencheurs(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        conn.close()


class TestStockNegatifAuditable(unittest.TestCase):

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        import database_manager
        self.dm = database_manager
        self._db_origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def test_le_poste_de_caisse_n_interdit_pas_le_stock_negatif(self):
        self.dm.initialiser_db()
        self.assertNotIn(
            "prevent_negative_stock", _declencheurs(self.path),
            "Le déclencheur rendrait muet le signalement de conflit de stock hors-ligne.")

    def test_une_base_issue_d_un_pack_de_migration_non_plus(self):
        """Le chemin emprunté quand une boutique change d'ordinateur."""
        from kodo_core.db.migrations import MigrationManager
        MigrationManager.run_migrations(self.path)
        MigrationManager.initialiser_db(db_path=self.path)
        self.assertNotIn(
            "prevent_negative_stock", _declencheurs(self.path),
            "Une boutique migrée hériterait d'une base où son audit de stock est cassé.")

    def test_le_drapeau_d_audit_reste_posable_sur_une_ligne_negative(self):
        """Le cœur du défaut : c'est l'UPDATE qui SIGNALE le conflit qui était bloqué."""
        from kodo_core.db.migrations import MigrationManager
        MigrationManager.run_migrations(self.path)
        MigrationManager.initialiser_db(db_path=self.path)

        conn = sqlite3.connect(self.path)
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) "
                      "VALUES ('Article hors-ligne', 10.0, 0.21)")
            pid = c.lastrowid
            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) "
                      "VALUES (?, 'Unique', -1)", (pid,))
            sid = c.lastrowid

            # C'est exactement ce que fait OfflineSyncEngine.process_pending_tickets.
            try:
                c.execute("UPDATE Stocks SET requires_stock_audit = 1 WHERE id = ?", (sid,))
            except sqlite3.IntegrityError as e:
                self.fail(f"Le conflit de stock est devenu impossible à signaler : {e}")

            c.execute("SELECT quantite_actuelle, requires_stock_audit FROM Stocks WHERE id = ?", (sid,))
            quantite, drapeau = c.fetchone()
            self.assertEqual(quantite, -1, "L'état de conflit doit rester visible tel quel.")
            self.assertEqual(drapeau, 1, "Le conflit doit être signalé pour recomptage.")
            conn.commit()
        finally:
            conn.close()

    def test_une_saisie_negative_reste_refusee_au_bon_niveau(self):
        """La protection existe, mais dans l'écran Stocks — là où c'est une vraie faute de saisie."""
        self.dm.initialiser_db()
        from kodo_core.domain.catalog.inventory_manager import InventoryManager
        with self.assertRaises(ValueError):
            InventoryManager.save_product({"name": "Bonnet", "price": 10.0, "sizes": "M:-3"})


if __name__ == "__main__":
    unittest.main()


class TestLesBasesQuiLePORTENTDEJA(unittest.TestCase):
    """
    Le retrait ne profitait qu'aux bases NEUVES.

    Retirer le `CREATE TRIGGER` de `initialiser_db` ne touche pas une base qui porte déjà le
    déclencheur : `initialiser_db` crée, elle ne supprime jamais. Or ce sont précisément les
    bases des boutiques en service — créées par une version antérieure, ou restaurées depuis
    une sauvegarde — qui le portent. Le correctif ne réparait donc aucune des installations
    où le défaut existe vraiment. Seule une migration versionnée les atteint.
    """

    DECLENCHEUR = """
        CREATE TRIGGER IF NOT EXISTS prevent_negative_stock
        BEFORE UPDATE ON Stocks
        FOR EACH ROW
        WHEN NEW.quantite_actuelle < 0
        BEGIN
            SELECT RAISE(ABORT, 'Le stock ne peut pas être négatif');
        END;
    """

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        import database_manager
        self.dm = database_manager
        self._db_origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _base_de_commercante_en_service(self):
        """Une base telle qu'une version antérieure l'a laissée : avec le déclencheur."""
        from kodo_core.db.migrations import MigrationManager
        MigrationManager.run_migrations(self.path)
        MigrationManager.initialiser_db(db_path=self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DELETE FROM schema_version WHERE version = '2.0.7'")
            conn.executescript(self.DECLENCHEUR)
            conn.commit()
        finally:
            conn.close()
        self.assertIn("prevent_negative_stock", _declencheurs(self.path),
                      "le montage du test n'a pas posé le déclencheur")

    def test_une_mise_a_jour_retire_le_declencheur_des_bases_existantes(self):
        from kodo_core.db.migrations import MigrationManager

        self._base_de_commercante_en_service()
        MigrationManager.run_migrations(self.path)

        self.assertNotIn(
            "prevent_negative_stock", _declencheurs(self.path),
            "Une boutique déjà en service garde un audit de stock muet après mise à jour.")

    def test_apres_mise_a_jour_le_drapeau_d_audit_se_pose_vraiment(self):
        """Le défaut réel, pas seulement la présence du déclencheur."""
        from kodo_core.db.migrations import MigrationManager

        self._base_de_commercante_en_service()
        MigrationManager.run_migrations(self.path)

        conn = sqlite3.connect(self.path)
        try:
            conn.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva, en_solde) "
                         "VALUES ('Robe', 19.99, 0.21, 0)")
            pid = conn.execute("SELECT id FROM Produits WHERE nom = 'Robe'").fetchone()[0]
            conn.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) "
                         "VALUES (?, 'Unique', 0)", (pid,))
            sid = conn.execute("SELECT id FROM Stocks WHERE id_produit = ?", (pid,)).fetchone()[0]
            # Deux caisses hors ligne ont vendu la dernière pièce : le compteur passe sous zéro.
            conn.execute("UPDATE Stocks SET quantite_actuelle = -1 WHERE id = ?", (sid,))
            # Puis le moteur hors-ligne SIGNALE le conflit. C'est cet UPDATE qui était refusé.
            conn.execute("UPDATE Stocks SET requires_stock_audit = 1 WHERE id = ?", (sid,))
            conn.commit()
            drapeau = conn.execute(
                "SELECT requires_stock_audit FROM Stocks WHERE id = ?", (sid,)).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(drapeau, 1,
                         "Le conflit de stock hors-ligne reste invisible à la commerçante.")


class TestLeDrapeauNonPoseNeSeTaitPlus(unittest.TestCase):
    """
    Ne pas faire échouer la passe est bon ; se taire ne l'est pas.

    `_marquer_audit` avalait toute exception « colonne absente sur une base ancienne ». Le
    drapeau étant le SEUL signalement d'un écart de stock, l'avaler en silence rendait
    l'écart indétectable — y compris quand la cause était tout autre.
    """

    def test_un_echec_de_pose_du_drapeau_est_journalise(self):
        import logging
        from kodo_core.sync.shopify import ShopifySync

        class CurseurQuiRefuse:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("no such column: requires_stock_audit")

        with self.assertLogs("kodo_core.sync.shopify", level=logging.ERROR) as journal:
            ShopifySync._marquer_audit(CurseurQuiRefuse(), 42, None)

        self.assertTrue(
            any("42" in ligne and "audit" in ligne.lower() for ligne in journal.output),
            f"L'échec de pose du drapeau ne laisse aucune trace exploitable : {journal.output}")
