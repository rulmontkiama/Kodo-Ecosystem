import unittest
import os
import sys
import json
import tempfile
import shutil

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.telemetry import DataSanitizer, TelemetryEngine
from core.config import ShopConfig

class TestTelemetry(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        ShopConfig.get_base_data_dir = lambda: self.temp_dir
        ShopConfig.get_logs_dir = lambda: os.path.join(self.temp_dir, "logs")
        os.makedirs(ShopConfig.get_logs_dir(), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_data_sanitizer_masking(self):
        """Vérifie le caviardage strict des emails, montants, pins et téléphones."""
        raw_log = (
            "Erreur lors de la vente pour le client jean.dupont@email.com (Tel: +32470123456).\n"
            "Montant de la transaction: 149.99 € avec le PIN secret: '123456'."
        )
        
        sanitized = DataSanitizer.sanitize_text(raw_log)
        
        # Vérifications de sécurité
        self.assertNotIn("jean.dupont@email.com", sanitized)
        self.assertNotIn("149.99 €", sanitized)
        self.assertNotIn("123456", sanitized)
        
        self.assertIn("[MASKED_EMAIL]", sanitized)
        self.assertIn("[MASKED_AMOUNT]", sanitized)
        self.assertIn("[MASKED_SECRET]", sanitized)

    def test_diagnostic_bundle_generation(self):
        """Vérifie la structure et la création du Diagnostic Bundle JSON."""
        stacktrace = "Traceback (most recent call last):\n  File 'main.py', line 42\nException: Erreur de test avec client client@domain.com pour 50.00 €"
        
        filepath = TelemetryEngine.generate_diagnostic_bundle(
            error_type="TestError",
            raw_stacktrace=stacktrace,
            extra_context={"test_key": "val"}
        )

        self.assertTrue(os.path.exists(filepath))
        
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["error_type"], "TestError")
        self.assertIn("os_platform", data["system_info"])
        self.assertIn("free_disk_gb", data["system_info"])
        self.assertNotIn("client@domain.com", data["sanitized_stacktrace"])
        self.assertIn("[MASKED_EMAIL]", data["sanitized_stacktrace"])

if __name__ == "__main__":
    unittest.main()
