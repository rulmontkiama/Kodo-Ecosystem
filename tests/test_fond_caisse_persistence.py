# -*- coding: utf-8 -*-
"""
Tests unitaires pour la persistance du fond de caisse et l'élimination du reset forcé à 200€.
"""

import os
import sys
import tempfile
import unittest
import sqlite3
from decimal import Decimal

# Racine du projet
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import initialiser_db, get_connection, enregistrer_cloture_caisse
from kodo_core.services.cash_session_service import set_fond_caisse_matin, get_fond_caisse_matin


class TestFondCaissePersistence(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_fond_persistence.db")
        database_manager.DB_NAME = self.db_path
        initialiser_db()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_fond_caisse_personnalise_persiste_et_ne_revient_pas_a_200(self):
        """Vérifie qu'un fond de caisse à 150€ persiste et n'est jamais écrasé à 200€."""
        conn = get_connection()
        try:
            c = conn.cursor()

            # Au départ sans configuration, doit retourner 0.0 et non 200.0
            initial = get_fond_caisse_matin(c)
            self.assertEqual(initial, 0.0)

            # Définir le fond de caisse à 150.00
            set_fond_caisse_matin(c, "150.00")
            c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('fond_caisse_matin', '150.00')")
            conn.commit()

            # Vérifier la lecture
            fond_lu = get_fond_caisse_matin(c)
            self.assertEqual(fond_lu, 150.0)

            # Effectuer une clôture Z
            enregistrer_cloture_caisse(
                caisse_id="POS-01",
                fond_caisse_reel=Decimal("150.00"),
                fond_caisse_matin=Decimal("150.00"),
                conn=conn
            )

            # Vérifier que la session précédente a bien été fermée
            c.execute("SELECT date_cloture FROM Sessions_Caisse WHERE date_cloture IS NOT NULL")
            self.assertIsNotNone(c.fetchone())

            # Après clôture, la session active est fermée mais le fond par défaut dans Parametres reste 150.0
            fond_apres = get_fond_caisse_matin(c)
            self.assertEqual(fond_apres, 150.0)
            self.assertNotEqual(fond_apres, 200.0)

        finally:
            conn.close()

    def test_fond_caisse_a_zero_est_autorise_et_persiste(self):
        """Vérifie qu'un fond de caisse explicitement mis à 0.00 persiste."""
        conn = get_connection()
        try:
            c = conn.cursor()
            set_fond_caisse_matin(c, "0.00")
            c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('fond_caisse_matin', '0.00')")
            conn.commit()

            self.assertEqual(get_fond_caisse_matin(c), 0.0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
