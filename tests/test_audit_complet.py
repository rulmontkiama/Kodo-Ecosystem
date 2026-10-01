"""Non-régression : `audit_complet` doit accepter toutes les tables scellées.

La liste blanche de `verifier_chainage` avait oublié `Rapports_Z` et `Audit_Trail`
(l'audit levait ValueError), et l'ancrage de genèse de la piste d'audit était faux
(fausse alerte « corruption » dès le premier événement).
"""
import os
import tempfile
import unittest

import database_manager
from kodo_core.db import audit_trail


class TestAuditComplet(unittest.TestCase):
    def setUp(self):
        self._rep = tempfile.TemporaryDirectory()
        self._ancien = database_manager.DB_NAME
        database_manager.DB_NAME = os.path.join(self._rep.name, "audit.db")
        database_manager.initialiser_db()
        self.conn = database_manager.get_connection()

    def tearDown(self):
        self.conn.close()
        database_manager.DB_NAME = self._ancien
        self._rep.cleanup()

    def test_base_vide(self):
        self.assertTrue(audit_trail.audit_complet(self.conn)["conforme"])

    def test_piste_d_audit_non_vide_pas_de_fausse_alerte(self):
        audit_trail.record_audit_event(self.conn, "T", "Produits", 1, "u", "CREATE", "a")
        audit_trail.record_audit_event(self.conn, "T", "Produits", 2, "u", "CREATE", "b")
        self.conn.commit()
        rapport = audit_trail.audit_complet(self.conn)
        self.assertTrue(rapport["audit_ok"])
        self.assertTrue(rapport["conforme"])

    def test_table_inconnue_toujours_refusee(self):
        with self.assertRaises(ValueError):
            audit_trail.verifier_chainage("sqlite_master", conn=self.conn)


if __name__ == "__main__":
    unittest.main()
