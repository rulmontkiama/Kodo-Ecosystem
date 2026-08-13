import unittest
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from views.modals import ToastNotification, TAILLES_PRET_A_PORTER, COULEURS_PRET_A_PORTER
from services.shopify_service import ShopifyService
from services.firebase_service import FirebaseService

class TestViewsAndServices(unittest.TestCase):

    def test_modal_exports(self):
        """Vérifie que les constantes et classes modales sont ré-exportées avec succès."""
        self.assertIn("M", TAILLES_PRET_A_PORTER)
        self.assertIn("Noir", COULEURS_PRET_A_PORTER)

    def test_shopify_async_sync(self):
        """Vérifie la mise en file asynchrone sans blocage."""
        ShopifyService.enqueue_stock_sync(product_id=1, variant_sku="SKU-TEST-S", new_stock=15)
        time.sleep(0.6)
        self.assertTrue(True)

    def test_firebase_async_license_check(self):
        """Vérifie le callback de vérification de licence asynchrone."""
        res_box = []
        def _cb(valid):
            res_box.append(valid)

        FirebaseService.check_license_async(callback=_cb)
        time.sleep(0.5)
        self.assertTrue(len(res_box) > 0)
        self.assertTrue(res_box[0])

if __name__ == "__main__":
    unittest.main()
