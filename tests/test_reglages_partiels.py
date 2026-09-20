# -*- coding: utf-8 -*-
"""POST /api/settings est une mise à jour PARTIELLE : ne touche que les clés envoyées.

Avant, changer le fond de caisse (ou le seuil d'alerte) effaçait l'adresse, le n° BCE, le n° TVA et remettait
l'IP de l'imprimante à 192.168.1.150 ; un fond de caisse à 0 était ignoré sans erreur.
Base temporaire : aucune donnée réelle touchée.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app

IDENTITE = {"storeName": "Boutique X", "address": "Rue de la Loi 1, 1000 Bruxelles", "bceNumber": "0123.456.789",
            "tvaNumber": "BE0123456789", "printerIP": "192.168.0.77", "iban": "BE00 1111 2222 3333"}


class TestReglagesPartiels(unittest.TestCase):
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

    def api(self, method, path, data=None):
        status, body, _ = kodo_app.handle_request(method, path, {}, {}, data or {})
        return status, body

    def reglages(self):
        return self.api("GET", "/api/settings")[1]

    def identite_intacte(self, s):
        self.assertEqual(s["address"], IDENTITE["address"])
        self.assertEqual(s["bceNumber"], IDENTITE["bceNumber"])
        self.assertEqual(s["tvaNumber"], IDENTITE["tvaNumber"])
        self.assertEqual(s["printerIP"], IDENTITE["printerIP"])
        self.assertEqual(s["iban"], IDENTITE["iban"])
        self.assertEqual(s["storeName"], IDENTITE["storeName"])

    def test_changer_le_fond_de_caisse_ne_touche_pas_a_lidentite_de_la_boutique(self):
        self.api("POST", "/api/settings", dict(IDENTITE, fondCaisse=100))

        status, res = self.api("POST", "/api/settings", {"fondCaisse": 150})

        self.assertEqual(status, 200, res)
        s = self.reglages()
        self.assertEqual(s["fondCaisse"], 150.0)
        self.identite_intacte(s)

    def test_fond_de_caisse_a_zero_est_enregistre(self):
        self.api("POST", "/api/settings", dict(IDENTITE, fondCaisse=100))

        status, res = self.api("POST", "/api/settings", {"fondCaisse": 0})

        self.assertEqual(status, 200, res)
        s = self.reglages()
        self.assertEqual(s["fondCaisse"], 0.0)
        self.identite_intacte(s)

    def test_changer_le_seuil_dalerte_ne_touche_pas_a_lidentite_de_la_boutique(self):
        self.api("POST", "/api/settings", dict(IDENTITE))

        self.api("POST", "/api/settings", {"defaultAlertThreshold": 9})

        s = self.reglages()
        self.assertEqual(s["defaultAlertThreshold"], 9)
        self.identite_intacte(s)

    def test_une_valeur_vide_envoyee_explicitement_efface_bien_le_champ(self):
        self.api("POST", "/api/settings", dict(IDENTITE))

        self.api("POST", "/api/settings", {"address": ""})

        s = self.reglages()
        self.assertEqual(s["address"], "")
        self.assertEqual(s["bceNumber"], IDENTITE["bceNumber"])

    def test_fond_de_caisse_negatif_ou_invalide_toujours_refuse(self):
        self.assertEqual(self.api("POST", "/api/settings", {"fondCaisse": -5})[0], 400)
        self.assertEqual(self.api("POST", "/api/settings", {"fondCaisse": "abc"})[0], 400)


if __name__ == "__main__":
    unittest.main()
