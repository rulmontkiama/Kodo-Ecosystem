# -*- coding: utf-8 -*-
"""
Kōdo POS — Les NOUVEAUX Z sont scellés en version 2 (phase 2 du déploiement, 2.1.3).

Les Z historiques restent en v1 et sont toujours jugés avec leur formule d'origine ; un Z neuf scelle tous
ses montants, de sorte qu'une rubrique retouchée après coup (bons vendus, QR, avoirs, arrondi, TVA…) est vue.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestEcritureV2(unittest.TestCase):

    def setUp(self):
        import database_manager as dm
        from kodo_core.db import audit_trail
        self.dm, self.at = dm, audit_trail
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

    def cloturer(self, **kw):
        return self.dm.enregistrer_cloture_caisse(conn=self.conn, **kw)

    def test_un_z_neuf_est_scelle_en_v2_et_se_verifie(self):
        self.dm.vendre_carte_cadeau(cursor=self.conn.cursor(), montant=50, mode_paiement="CB", emis_par="Julie")
        self.conn.commit()
        self.cloturer()
        self.assertEqual(self.conn.execute("SELECT hash_version, total_bons_vendus FROM Clotures_Caisse").fetchall(),
                         [(2, 50.0)])
        self.assertTrue(self.at.verifier_clotures(self.conn))

    def test_une_rubrique_retouchee_apres_scellement_est_detectee(self):
        self.dm.vendre_carte_cadeau(cursor=self.conn.cursor(), montant=50, mode_paiement="CB", emis_par="Julie")
        self.conn.commit()
        self.cloturer()
        for colonne in ("total_bons_vendus", "total_qr", "total_avoir", "total_arrondi_cash", "total_tva"):
            with self.subTest(colonne=colonne):
                original = self.conn.execute(f"SELECT {colonne} FROM Clotures_Caisse").fetchone()[0]
                self.conn.execute(f"UPDATE Clotures_Caisse SET {colonne} = ?", ((original or 0) + 3,))
                self.conn.commit()
                self.assertFalse(self.at.verifier_clotures(self.conn))
                self.conn.execute(f"UPDATE Clotures_Caisse SET {colonne} = ?", (original,))
                self.conn.commit()
                self.assertTrue(self.at.verifier_clotures(self.conn), "remis à l'état d'origine, il est de nouveau valide")

    def test_un_z_historique_v1_puis_un_z_v2_forment_une_chaine_valide(self):
        self.cloturer()
        self.conn.execute("UPDATE Clotures_Caisse SET hash_version = NULL, current_hash = ?, signature = ?",
                          ("x",) * 2)
        # Réécrit le Z comme l'aurait fait une version <= 2.1.2 (formule v1).
        ligne = self.conn.execute("SELECT date_cloture, caisse_id, total_ventes_tvac, total_especes, total_carte, "
                                  "hash_precedent FROM Clotures_Caisse").fetchone()
        h1 = self.at.calculer_hash_cloture(ligne[5], ligne[0], ligne[1], ligne[2], ligne[3], ligne[4])
        self.conn.execute("UPDATE Clotures_Caisse SET current_hash = ?, signature = ?", (h1, h1))
        self.conn.commit()
        self.assertTrue(self.at.verifier_clotures(self.conn))
        self.dm.vendre_carte_cadeau(cursor=self.conn.cursor(), montant=20, mode_paiement="Espèces", emis_par="Julie")
        self.conn.commit()
        self.cloturer()
        self.assertEqual([r[0] for r in self.conn.execute("SELECT hash_version FROM Clotures_Caisse ORDER BY id")],
                         [None, 2])
        self.assertTrue(self.at.verifier_clotures(self.conn))

    def test_le_hash_ecrit_est_celui_que_relit_le_verificateur_meme_avec_des_centimes(self):
        """Valeurs à décimales : le float écrit puis relu doit redonner exactement le même hash."""
        cur = self.conn.cursor()
        self.dm.vendre_carte_cadeau(cursor=cur, montant="33.33", mode_paiement="QR", emis_par="Julie")
        self.dm.vendre_carte_cadeau(cursor=cur, montant="0.07", mode_paiement="Espèces", emis_par="Julie")
        self.conn.commit()
        self.cloturer(fond_caisse_reel=12.34, fond_caisse_matin=10)
        self.assertTrue(self.at.verifier_clotures(self.conn))


if __name__ == "__main__":
    unittest.main()
