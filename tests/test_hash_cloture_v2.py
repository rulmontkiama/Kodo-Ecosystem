# -*- coding: utf-8 -*-
"""
Kōdo POS — Scellement des Z en version 2 (lecture : valable dès la 2.1.2).

La v1 ne scelle que total TVAC, espèces et carte. Les rubriques ajoutées ensuite (QR, avoirs, arrondi,
bons vendus), la TVA, les remises, le fond compté et l'écart restaient hors chaîne : on pouvait les retoucher
sans que la vérification le voie. La v2 les scelle ; les Z déjà scellés restent v1.

Ce fichier garantit surtout le DÉPLOIEMENT : un code qui lit v2 ne condamne jamais une version qu'il ignore
(retour arrière), et un Z v1 reste jugé avec sa formule d'origine.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VALEURS = {
    "total_ventes_tvac": 120.0, "total_htva": 99.17, "total_tva": 20.83, "total_remises": 5.0,
    "total_especes": 70.0, "total_carte": 40.0, "total_qr": 10.0, "total_avoir": 0.0,
    "total_arrondi_cash": -0.02, "total_bons_vendus": 25.0, "total_tickets": 4,
    "fond_caisse_reel": 150.0, "ecart": 0.0,
}


class TestHashZV2(unittest.TestCase):

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
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(Clotures_Caisse)")]
        if "hash_version" not in cols:
            self.conn.execute("ALTER TABLE Clotures_Caisse ADD COLUMN hash_version INTEGER")
        for col in ("periode_jusqu_au", "total_qr", "total_avoir", "total_arrondi_cash", "total_bons_vendus"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE Clotures_Caisse ADD COLUMN {col} DECIMAL")
        self.conn.commit()
        self.precedent = None

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def inserer(self, version, date="2026-10-15 20:00:00", jusqu_au="2026-10-15", **surcharges):
        v = dict(VALEURS, **surcharges)
        if version == 2:
            h = self.at.calculer_hash_cloture_v2(self.precedent, date, "POS-01", jusqu_au, v)
        else:
            h = self.at.calculer_hash_cloture(self.precedent, date, "POS-01", v["total_ventes_tvac"],
                                              v["total_especes"], v["total_carte"])
        self.conn.execute("""
            INSERT INTO Clotures_Caisse (date_cloture, caisse_id, total_ventes_tvac, total_htva, total_tva,
                total_especes, total_carte, total_remises, total_tickets, fond_caisse_reel, ecart, vendeur,
                hash_precedent, current_hash, signature, periode_jusqu_au, total_qr, total_avoir,
                total_arrondi_cash, total_bons_vendus, hash_version)
            VALUES (?, 'POS-01', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Admin', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, v["total_ventes_tvac"], v["total_htva"], v["total_tva"], v["total_especes"], v["total_carte"],
              v["total_remises"], v["total_tickets"], v["fond_caisse_reel"], v["ecart"], self.precedent, h, h,
              jusqu_au, v["total_qr"], v["total_avoir"], v["total_arrondi_cash"], v["total_bons_vendus"],
              None if version == 1 else version))
        self.conn.commit()
        self.precedent = h
        return h

    def verifier(self):
        return self.at.verifier_clotures(self.conn)

    # --- lecture -----------------------------------------------------------------------------

    def test_un_z_v1_est_toujours_juge_avec_sa_formule_d_origine(self):
        self.inserer(1)
        self.assertTrue(self.verifier())

    def test_un_z_v2_intact_est_valide(self):
        self.inserer(2)
        self.assertTrue(self.verifier())

    def test_une_chaine_mixte_v1_puis_v2_est_valide(self):
        self.inserer(1, date="2026-10-14 20:00:00", jusqu_au="2026-10-14")
        self.inserer(2, date="2026-10-15 20:00:00")
        self.assertTrue(self.verifier())

    def test_v2_detecte_la_retouche_d_une_rubrique_que_v1_ne_scellait_pas(self):
        for colonne in ("total_qr", "total_avoir", "total_arrondi_cash", "total_bons_vendus", "total_htva",
                        "total_tva", "total_remises", "fond_caisse_reel", "ecart", "total_tickets"):
            with self.subTest(colonne=colonne):
                self.conn.execute("DELETE FROM Clotures_Caisse")
                self.precedent = None
                self.inserer(2)
                self.assertTrue(self.verifier())
                self.conn.execute(f"UPDATE Clotures_Caisse SET {colonne} = COALESCE({colonne}, 0) + 7")
                self.conn.commit()
                self.assertFalse(self.verifier(), f"la retouche de {colonne} doit être détectée")

    def test_v2_scelle_aussi_la_periode_couverte(self):
        self.inserer(2)
        self.conn.execute("UPDATE Clotures_Caisse SET periode_jusqu_au = '2026-01-01'")
        self.conn.commit()
        self.assertFalse(self.verifier())

    def test_une_version_inconnue_n_est_jamais_condamnee(self):
        """Retour arrière : un Z scellé par une version PLUS RÉCENTE ne doit pas déclencher de fausse alerte."""
        h = self.inserer(2)
        self.conn.execute("UPDATE Clotures_Caisse SET hash_version = 3")
        self.conn.commit()
        self.assertTrue(self.verifier(), "version 3 inconnue : non jugée, pas accusée")
        # ... mais la rupture de chaînage, elle, reste détectée.
        self.precedent = "AUTRE"
        self.inserer(1, date="2026-10-16 20:00:00", jusqu_au="2026-10-16")
        self.assertFalse(self.verifier())

    def test_le_hash_v2_depend_de_chaque_champ(self):
        base = self.at.calculer_hash_cloture_v2("P", "2026-10-15 20:00:00", "POS-01", "2026-10-15", VALEURS)
        for champ in self.at.CHAMPS_MONTANTS_HASH_Z_V2 + ("total_tickets",):
            autre = dict(VALEURS, **{champ: (VALEURS.get(champ) or 0) + 1})
            self.assertNotEqual(base, self.at.calculer_hash_cloture_v2(
                "P", "2026-10-15 20:00:00", "POS-01", "2026-10-15", autre), champ)


if __name__ == "__main__":
    unittest.main()
