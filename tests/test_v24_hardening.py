# -*- coding: utf-8 -*-
"""
Tests de durcissement v2.4 :
1. Pied de ticket multi-liens, QR multiples (grille / pile) et message libre (/api/settings/social)
2. Contrôle d'accès par rôle (RBAC non-Gérant sur stats, clôture Z, utilisateurs, prix d'achat)
3. Préservation de la cause racine `(Caused by ...)` dans ShopifySyncEngine._resumer_exception
"""

import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

import database_manager
from database_manager import initialiser_db
from kodo_core.api.app import kodo_app
from kodo_core.api.session_manager import create_session_token
from kodo_core.hardware.printer import generate_social_multi_image, generer_ticket
from kodo_core.sync.shopify import ShopifySync


class TestV24Hardening(unittest.TestCase):
    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tf.name
        self.tf.close()
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.db_path
        os.environ["KODO_DB_PATH"] = self.db_path
        initialiser_db()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                try:
                    os.remove(self.db_path + suffix)
                except Exception:
                    pass

    def test_social_multi_links_roundtrip_and_image(self):
        links = [
            {
                "id": "ig-1",
                "kind": "instagram",
                "title": "INSTAGRAM",
                "url": "https://instagram.com/kodo",
                "subtitle": "@kodo",
                "display": "qr",
                "enabled": True,
            },
            {
                "id": "gg-2",
                "kind": "google",
                "title": "AVIS GOOGLE",
                "url": "https://g.page/r/kodo",
                "subtitle": "5 etoiles",
                "display": "qr",
                "enabled": True,
            },
            {
                "id": "web-3",
                "kind": "website",
                "title": "E-SHOP",
                "url": "https://kodo-pos.be",
                "subtitle": "www.kodo-pos.be",
                "display": "text",
                "enabled": True,
            },
        ]

        img = generate_social_multi_image(links, header="RETROUVEZ-NOUS", width=512, qr_size="large", qr_layout="grid")
        self.assertEqual(img.width, 512)
        self.assertGreater(img.height, 100)

        status, post_res, _ = kodo_app.handle_request(
            "POST",
            "/api/settings/social",
            {},
            {},
            {
                "mode": "qr",
                "header": "RETROUVEZ-NOUS",
                "qr_size": "large",
                "qr_layout": "grid",
                "footer_message": "Merci !\nEchange 14j",
                "links": links,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(post_res.get("success"))
        self.assertEqual(post_res.get("qr_layout"), "grid")
        self.assertEqual(len(post_res.get("links") or []), 3)

        status_get, get_res, _ = kodo_app.handle_request("GET", "/api/settings/social", {}, {}, {})
        self.assertEqual(status_get, 200)
        self.assertTrue(get_res.get("has_social"))
        self.assertEqual(get_res.get("qr_layout"), "grid")
        self.assertEqual(get_res.get("footer_message"), "Merci !\nEchange 14j")
        self.assertEqual(len(get_res.get("links") or []), 3)

        ticket_txt = generer_ticket(
            numero="TCK-TEST-1",
            panier=[{"nom": "Robe Lin", "quantite": 1, "prix_vente_tvac": Decimal("50.00"), "taux_tva": Decimal("21")}],
            total_tvac=Decimal("50.00"),
            remise=Decimal("0.00"),
            paiements=[("CB", Decimal("50.00"))],
            rendu_monnaie=Decimal("0.00"),
            footer_message="Merci pour votre achat !\nA tres bientot",
        )
        self.assertIn("Merci pour votre achat !", ticket_txt)
        self.assertIn("A tres bientot", ticket_txt)

    def test_rbac_blocks_non_admin_financials_and_protects_cost_price(self):
        admin_tok = create_session_token("1", "Camille", "Gérant")
        cashier_tok = create_session_token("2", "Lucas", "Caissier")
        admin_headers = {"Authorization": f"Bearer {admin_tok}"}
        cashier_headers = {"Authorization": f"Bearer {cashier_tok}"}

        # 1. Créer un article avec prix d'achat en tant que Gérant
        st, created, _ = kodo_app.handle_request(
            "POST",
            "/api/products",
            {},
            admin_headers,
            {
                "name": "Chemise Test",
                "category": "HAUTS",
                "price": 80.0,
                "costPrice": 32.5,
                "stock": 5,
                "barcode": "5400000000123",
            },
        )
        self.assertEqual(st, 200)
        pid = created["productId"]

        # 2. Le caissier ne voit pas prix_achat / costPrice sur GET /api/products
        st_list, prods_cashier, _ = kodo_app.handle_request("GET", "/api/products", {}, cashier_headers, {})
        self.assertEqual(st_list, 200)
        item_c = next(p for p in prods_cashier if str(p["id"]) == str(pid))
        self.assertNotIn("costPrice", item_c)
        self.assertNotIn("prix_achat_htva", item_c)

        # 3. Une mise à jour par le caissier ne doit pas écraser le prix d'achat existant
        st_upd, _, _ = kodo_app.handle_request(
            "PUT",
            "/api/products",
            {},
            cashier_headers,
            {
                "id": pid,
                "name": "Chemise Test Mod",
                "category": "HAUTS",
                "price": 85.0,
                "costPrice": 0.0,
                "stock": 5,
                "barcode": "5400000000123",
            },
        )
        self.assertEqual(st_upd, 200)

        _, prods_admin, _ = kodo_app.handle_request("GET", "/api/products", {}, admin_headers, {})
        item_a = next(p for p in prods_admin if str(p["id"]) == str(pid))
        self.assertAlmostEqual(item_a["costPrice"], 32.5, places=2)

        # 4. Stats, Clôture Z et Gestion utilisateurs retournent 403 pour le caissier
        for method, route in (
            ("GET", "/api/stats/dashboard"),
            ("GET", "/api/cloture-z/summary"),
            ("POST", "/api/cloture-z"),
            ("POST", "/api/users"),
        ):
            s_code, _, _ = kodo_app.handle_request(method, route, {}, cashier_headers, {})
            self.assertEqual(s_code, 403, f"{method} {route} devrait refuser Caissier")

    def test_shopify_exception_preserves_caused_by(self):
        long_err = Exception(
            "HTTPSConnectionPool(host='biqyjz-3e.myshopify.com', port=443): "
            "Max retries exceeded with url: /admin/api/2026-01/orders.json?status=any&updated_at_min=2026-11-20T11%3A12%3A11%2B01%3A00 "
            "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')))"
        )
        summary = ShopifySync._resumer_exception(long_err)
        self.assertIn("Caused by", summary)
        self.assertIn("SSLCertVerificationError", summary)
