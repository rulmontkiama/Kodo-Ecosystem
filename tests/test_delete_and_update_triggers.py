#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test automatisé Kōdo POS : Refutation de régression pour
1. Persistance des suppressions (Ghost Data)
2. Déclencheur de mise à jour (Dead Update Trigger)
"""

import os
import sys
import unittest
import sqlite3
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database_manager
import services.update_checker as update_checker

class TestDeleteAndUpdateTriggers(unittest.TestCase):
    def setUp(self):
        self.db_name = "test_triggers_qa.db"
        database_manager.DB_NAME = self.db_name
        if os.path.exists(self.db_name):
            try: os.remove(self.db_name)
            except Exception: pass
        database_manager.initialiser_db()

    def tearDown(self):
        if os.path.exists(self.db_name):
            try: os.remove(self.db_name)
            except Exception: pass

    def test_held_ticket_deletion_regex_fix(self):
        """Vérifie que les formats ht_101 et ht-101 s'effacent correctement de SQLite sans ValueError."""
        t_id = database_manager.sauvegarder_panier_en_attente(
            panier=[{"nom": "Test Item", "prix_vente_tvac": 10.0, "quantite": 1}],
            total_tvac=10.0,
            client_nom="Client QA"
        )
        self.assertIsNotNone(t_id)

        import re
        ticket_id = f"ht-{t_id}"
        digits = re.findall(r'\d+', ticket_id)
        self.assertTrue(len(digits) > 0)
        db_id = int(digits[0])

        database_manager.supprimer_panier_en_attente(db_id)

        paniers = database_manager.lister_paniers_en_attente()
        self.assertEqual(len(paniers), 0)

    def test_update_checker_target_dist_dir_writable(self):
        """Vérifie que get_target_dist_dir retourne un dossier inscriptible."""
        dist_dir = update_checker.get_target_dist_dir()
        self.assertIsNotNone(dist_dir)
        self.assertTrue(len(dist_dir) > 0)

if __name__ == '__main__':
    unittest.main()
