# -*- coding: utf-8 -*-
"""
Tests Red Team vs Blue Team : Vérification rigoureuse des 6 exigences utilisateur :
1. Suppression totale du QR code / bloc par défaut (@l_adresseb)
2. Format horizontal côte-à-côte préservé pour la personnalisation
3. Modularité des réseaux & messages (Instagram, Facebook, TikTok, E-Shop, Google, Message Libre)
4. Site officiel https://kōdo-solutions.com et translittération 'ō' -> 'o'
5. Protection anti-dépassement buffer thermique ESC/POS (slice chunking 48 dots)
6. Encodage et persistance du Prix d'Achat HTVA (Backend & API)
"""

import os
import unittest
from decimal import Decimal
from PIL import Image
import database_manager
from kodo_core.api.app import KodoAPIApp
from kodo_core.hardware.printer import (
    strip_accents,
    sanitize_escpos_text,
    get_ticket_logo_path,
    get_ticket_social_path,
    generate_social_qr_image,
    pil_to_escpos_raster,
    generer_ticket_test
)
from kodo_core.domain.catalog.inventory_manager import InventoryManager


class TestRedTeamTicketAndCostPrice(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        database_manager.DB_NAME = self.temp_db_path
        os.environ["KODO_DB_PATH"] = self.temp_db_path
        database_manager.initialiser_db()
        self.app = KodoAPIApp()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def test_01_no_default_social_qr_code_on_clean_db(self):
        """1. Sur une BDD vierge, aucun QR code ni bloc social ne doit s'imprimer."""
        social_path = get_ticket_social_path()
        self.assertIsNone(social_path, "get_ticket_social_path doit retourner None par défaut")

        logo_path = get_ticket_logo_path()
        self.assertIsNone(logo_path, "get_ticket_logo_path doit retourner None si aucun logo uploadé")

        # Via l'API GET /settings/social
        code, resp, _ = self.app.handle_request("GET", "/api/settings/social", {}, {}, {})
        self.assertEqual(code, 200)
        self.assertFalse(resp.get("has_social"))
        self.assertEqual(resp.get("mode"), "none")
        self.assertNotIn("l_adresseb", str(resp))

    def test_02_horizontal_qr_code_customization(self):
        """2. Le générateur produit un bloc horizontal propre (QR à gauche, séparateur, textes à droite)."""
        img = generate_social_qr_image(
            header="SUIVEZ-NOUS SUR",
            title="INSTAGRAM",
            url="https://instagram.com/ma_boutique",
            subtitle="@ma_boutique",
            width=512,
            qr_size="large"
        )
        self.assertEqual(img.width, 512)
        self.assertGreater(img.height, 100)
        # Vérifier que l'image est bien générée et lisible
        self.assertEqual(img.mode, "RGB")

    def test_03_social_networks_and_message_modularity(self):
        """3. Support de Facebook, TikTok, E-Shop, Avis Google et Message Libre."""
        presets = [
            ("RETROUVEZ-NOUS SUR", "FACEBOOK", "https://facebook.com/kodo", "Page officielle"),
            ("DÉCOUVREZ NOS VIDÉOS", "TIKTOK", "https://tiktok.com/@kodo", "@kodo"),
            ("COMMANDEZ EN LIGNE", "SITE WEB", "https://kōdo-solutions.com", "Livraison 24h"),
            ("VOTRE AVIS COMPTE !", "AVIS GOOGLE", "https://g.page/r/test", "5 étoiles ⭐"),
            ("MERCI DE VOTRE VISITE", "À TRÈS BIENTÔT", "https://kōdo-solutions.com", "L'équipe Kōdo"),
        ]

        for header, title, url, subtitle in presets:
            code, resp, _ = self.app.handle_request("POST", "/api/settings/social", {}, {}, {
                "mode": "qr",
                "header": header,
                "title": title,
                "url": url,
                "subtitle": subtitle,
                "qr_size": "large"
            })
            self.assertEqual(code, 200)
            self.assertTrue(resp.get("success"))
            self.assertEqual(resp.get("header"), header)
            self.assertEqual(resp.get("title"), title)
            self.assertTrue(resp.get("social_url").startswith("data:image/png;base64,"))

            # Lecture GET
            code_get, resp_get, _ = self.app.handle_request("GET", "/api/settings/social", {}, {}, {})
            self.assertEqual(code_get, 200)
            self.assertTrue(resp_get.get("has_social"))
            self.assertEqual(resp_get.get("header"), header)
            self.assertEqual(resp_get.get("title"), title)

    def test_04_official_website_and_transliteration(self):
        """4. Le site officiel kōdo-solutions.com est présent et 'ō' est translittéré en 'o' pour ESC/POS."""
        ticket_txt = generer_ticket_test()
        self.assertIn("https://kōdo-solutions.com", ticket_txt)
        self.assertNotIn("kodopos.com", ticket_txt)

        # Nettoyage ESC/POS pour imprimante 7/8-bit ASCII
        cleaned = sanitize_escpos_text("Bienvenue sur https://kōdo-solutions.com ! Éléphant KŌDO")
        self.assertIn("kodo-solutions.com", cleaned)
        self.assertIn("KODO", cleaned)
        self.assertNotIn("ō", cleaned)
        self.assertNotIn("Ō", cleaned)

    def test_05_anti_buffer_overflow_slice_chunking(self):
        """5. pil_to_escpos_raster découpe l'image en tranches sécurisées (slice chunking <= 24 dots) avec GS v 0."""
        # Créer une image haute (ex: 200 dots de haut)
        test_img = Image.new("RGB", (512, 200), "black")
        raster_bytes = pil_to_escpos_raster(test_img, max_width=512)

        # Doit contenir plusieurs en-têtes GS v 0 (0x1D, 0x76, 0x30, 0x00)
        gs_header = bytes([0x1D, 0x76, 0x30, 0x00])
        count = raster_bytes.count(gs_header)
        # 200 dots découpés par 24 dots = ceil(200/24) = 9 tranches (garantit taille <= 2048 octets)
        self.assertEqual(count, 9, "L'image de 200 dots doit être découpée en 9 tranches GS v 0")

    def test_06_cost_price_persistence_and_api(self):
        """6. Encodage et persistance du Prix d'Achat HTVA via save_product et l'API."""
        # Test 6a : save_product direct avec costPrice
        prod_data = {
            "name": "Cost Price Test Product",
            "category": "TEST",
            "price": 99.00,
            "costPrice": 42.50,
            "stock": 10,
            "barcode": "999000000001"
        }
        res = InventoryManager.save_product(prod_data)
        self.assertTrue(res.get("success"))
        pid = res.get("product_id")

        # Relecture via get_product_by_id
        saved_prod = InventoryManager.get_product_by_id(pid)
        self.assertIsNotNone(saved_prod)
        self.assertEqual(saved_prod.get("costPrice"), 42.50)
        self.assertEqual(saved_prod.get("prix_achat_htva"), 42.50)
        self.assertEqual(saved_prod.get("purchase_price_htva"), 42.50)

        # Test 6b : Mise à jour du prix d'achat via l'API POST /api/products
        prod_data_update = {
            "id": pid,
            "name": "Cost Price Test Product Updated",
            "category": "TEST",
            "price": 99.00,
            "costPrice": 48.75,
            "stock": 10,
            "barcode": "999000000001"
        }
        code, resp, _ = self.app.handle_request("POST", "/api/products", {}, {}, prod_data_update)
        self.assertEqual(code, 200)
        self.assertTrue(resp.get("success"))

        # Relecture
        updated_prod = InventoryManager.get_product_by_id(pid)
        self.assertEqual(updated_prod.get("costPrice"), 48.75)
        self.assertEqual(updated_prod.get("prix_achat_htva"), 48.75)

        # Test 6c : API GET /api/products renvoie bien le costPrice
        code_get, all_prods, _ = self.app.handle_request("GET", "/api/products", {}, {}, {})
        self.assertEqual(code_get, 200)
        target = next((p for p in all_prods if str(p.get("product_id") or p.get("id")) == str(pid)), None)
        self.assertIsNotNone(target, f"pid {pid} non trouvé dans {all_prods}")
        self.assertEqual(target.get("costPrice"), 48.75)
        self.assertEqual(target.get("prix_achat_htva"), 48.75)

        # Nettoyage
        InventoryManager.delete_product(pid)


if __name__ == "__main__":
    unittest.main()
