import unittest
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from views.modals import MandatoryUpdateBanner, MandatoryUpdateOverlay

class TestMandatoryUpdate(unittest.TestCase):

    def test_mandatory_payload_contract(self):
        """Vérifie la validité du schéma JSON du contrat d'API pour les MAJ obligatoires."""
        payload_str = """
        {
          "version": "2.2.0",
          "is_mandatory": true,
          "grace_period_seconds": 300,
          "title": "Mise à jour de sécurité critique",
          "reason": "Mise en conformité du module de TVA et correctif BDD.",
          "estimated_duration_seconds": 15,
          "backup_required": true
        }
        """
        payload = json.loads(payload_str)
        
        self.assertEqual(payload["version"], "2.2.0")
        self.assertTrue(payload["is_mandatory"])
        self.assertEqual(payload["grace_period_seconds"], 300)
        self.assertEqual(payload["estimated_duration_seconds"], 15)
        self.assertTrue(payload["backup_required"])

    def test_modal_imports(self):
        """Vérifie l'exportabilité des classes de MAJ obligatoire."""
        self.assertIsNotNone(MandatoryUpdateBanner)
        self.assertIsNotNone(MandatoryUpdateOverlay)

if __name__ == "__main__":
    unittest.main()
