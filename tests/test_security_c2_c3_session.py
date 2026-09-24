# -*- coding: utf-8 -*-
"""
Tests de validation pour les 3 points de sécurité renforcés :
1. Jeton de Session & Headers (kodo_core.api.session_manager & app.py)
2. Sécurité du PIN C2 (Rate Limiting anti-bruteforce, PBKDF2 et migration automatique)
3. Scellement C3 (HMAC-SHA256 avec clé secrète machine locale et rétrocompatibilité)
"""

import os
import sys
import time
import sqlite3
import unittest
from decimal import Decimal

import database_manager
from database_manager import (
    initialiser_db,
    hash_pin,
    hash_pin_sha256,
    verify_pin_hash,
    calculer_hash_transaction,
    enregistrer_vente,
    HASH_ALGO_V1,
    HASH_ALGO_V2,
    HASH_ALGO_V3,
    HASH_ALGO_COURANT,
)
from audit_trail import verify_database_integrity, verifier_chainage
from kodo_core.api.session_manager import (
    create_session_token,
    verify_session_token,
    extract_token,
    get_current_user,
)
from kodo_core.api.app import kodo_app
import kodo_core.api.routes.system_routes as system_routes


class TestSecurityC2C3Session(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tf.name
        self.tf.close()
        database_manager.DB_NAME = self.db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        initialiser_db(conn=self.conn)
        # Réinitialiser le rate limiter en mémoire
        system_routes._PIN_ATTEMPTS.clear()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # 1. Tests Jeton de Session & Headers
    # -------------------------------------------------------------------------

    def test_session_token_creation_and_verification(self):
        token = create_session_token(user_id="42", user_name="Alice", role="Gérant", ttl_seconds=3600)
        self.assertIsInstance(token, str)
        self.assertIn(".", token)

        user = verify_session_token(token)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], "42")
        self.assertEqual(user["name"], "Alice")
        self.assertEqual(user["role"], "Gérant")

    def test_session_token_expired(self):
        # Token créé avec TTL négatif (expiré immédiatement)
        expired_token = create_session_token(user_id="1", user_name="Bob", role="Caissier", ttl_seconds=-10)
        self.assertIsNone(verify_session_token(expired_token))

    def test_session_token_tampered(self):
        token = create_session_token(user_id="1", user_name="Bob", role="Caissier")
        parts = token.split(".")
        # Falsification du payload
        fake_token = f"{parts[0]}fake.{parts[1]}"
        self.assertIsNone(verify_session_token(fake_token))

    def test_extract_token_and_get_current_user(self):
        token = create_session_token(user_id="5", user_name="Charlie", role="Caissier")
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(extract_token(headers), token)

        user = get_current_user(headers)
        self.assertIsNotNone(user)
        self.assertEqual(user["name"], "Charlie")

        headers_x = {"X-Session-Token": token}
        self.assertEqual(extract_token(headers_x), token)

    def test_app_dispatcher_passes_headers(self):
        # Vérifie que app.handle_request passe bien headers aux routeurs
        token = create_session_token(user_id="1", user_name="Admin", role="Gérant")
        headers = {"Authorization": f"Bearer {token}", "X-Custom": "test"}
        status, content, resp_headers = kodo_app.handle_request(
            method="GET",
            path="/api/status",
            query={},
            headers=headers,
            data={}
        )
        self.assertEqual(status, 200)
        self.assertEqual(content.get("status"), "online")

    # -------------------------------------------------------------------------
    # 2. Tests Sécurité PIN (C2)
    # -------------------------------------------------------------------------

    def test_pin_pbkdf2_deterministic_and_64_chars(self):
        h0 = hash_pin("1234")
        self.assertEqual(len(h0), 64)
        self.assertEqual(h0, hash_pin("1234"))
        self.assertNotEqual(h0, hash_pin("5678"))

    def test_pin_verify_hash_pbkdf2_and_legacy_sha256(self):
        # 1. PBKDF2
        pbkdf2_hash = hash_pin("9876")
        is_val, needs_reh = verify_pin_hash("9876", pbkdf2_hash)
        self.assertTrue(is_val)
        self.assertFalse(needs_reh)

        # 2. Legacy SHA-256
        legacy_hash = hash_pin_sha256("9876")
        is_val, needs_reh = verify_pin_hash("9876", legacy_hash)
        self.assertTrue(is_val)
        self.assertTrue(needs_reh)  # Doit signaler le besoin de migration

        # 3. Faux PIN
        is_val, needs_reh = verify_pin_hash("0000", pbkdf2_hash)
        self.assertFalse(is_val)

    def test_pin_verify_machine_bound_and_legacy_rehash(self):
        legacy_salt_hash = hash_pin("4321", salt="KODO_POS_SECURE_SALT_2026")
        is_val, needs_reh = verify_pin_hash("4321", legacy_salt_hash)
        self.assertTrue(is_val)
        from database_manager import _get_security_salt
        if _get_security_salt() != "KODO_POS_SECURE_SALT_2026":
            self.assertTrue(needs_reh)

    def test_pin_verify_rate_limiting_anti_bruteforce(self):
        headers = {"remote-addr": "127.0.0.1"}

        # 4 tentatives erronées -> 401
        for _ in range(4):
            status, res = system_routes.handle_system_request(
                "POST", "/api/pin/verify", {}, {"pin": "9999"}, headers=headers
            )
            self.assertEqual(status, 401)
            self.assertFalse(res.get("valid"))

        # 5e tentative erronée -> 429 Too Many Requests (verrouillé pour 30s)
        status, res = system_routes.handle_system_request(
            "POST", "/api/pin/verify", {}, {"pin": "9999"}, headers=headers
        )
        self.assertEqual(status, 429)
        self.assertTrue(res.get("locked"))
        self.assertGreater(res.get("retry_after"), 0)

        # Une 6e tentative, même avec le bon PIN, doit être rejetée pendant le lockout
        status, res = system_routes.handle_system_request(
            "POST", "/api/pin/verify", {}, {"pin": "0000"}, headers=headers
        )
        self.assertEqual(status, 429)

    def test_pin_verify_success_emits_session_token_and_default_warning(self):
        headers = {"remote-addr": "127.0.0.2"}
        status, res = system_routes.handle_system_request(
            "POST", "/api/pin/verify", {}, {"pin": "0000"}, headers=headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(res.get("valid"))
        self.assertTrue(res.get("default_pin_warning"))  # 0000 = default PIN
        self.assertIn("token", res)
        self.assertIn("session_token", res)

        # Vérifier que le token émis est valide
        user = verify_session_token(res["token"])
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "Gérant")

    # -------------------------------------------------------------------------
    # 3. Tests Scellement Cryptographique (C3)
    # -------------------------------------------------------------------------

    def test_hmac_audit_trail_v3(self):
        cursor = self.conn.cursor()
        # Enregistrer une vente qui sera scellée en V3 (HMAC)
        panier = [{"code_barre": "TST", "stock_id": 1, "prix_vente_tvac": Decimal("25.00")}]
        enregistrer_vente(
            cursor=cursor,
            numero_ticket="TCK-V3-001",
            total_tvac=Decimal("25.00"),
            total_htva=Decimal("20.00"),
            total_tva=Decimal("5.00"),
            remise=Decimal("0.00"),
            methode_paiement="Bancontact",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier,
            vendeur_nom="Admin",
            date_heure="2026-09-20 12:00:00",
            paiements=[("Bancontact", Decimal("25.00"))],
            caisse_id="POS-01"
        )
        self.conn.commit()

        # Vérifier l'intégrité de la base
        self.assertTrue(verify_database_integrity(conn=self.conn))

    def test_hmac_audit_trail_tamper_detection(self):
        cursor = self.conn.cursor()
        panier = [{"code_barre": "TST", "stock_id": 1, "prix_vente_tvac": Decimal("30.00")}]
        enregistrer_vente(
            cursor=cursor,
            numero_ticket="TCK-V3-MODIF",
            total_tvac=Decimal("30.00"),
            total_htva=Decimal("24.00"),
            total_tva=Decimal("6.00"),
            remise=Decimal("0.00"),
            methode_paiement="Espèces",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier,
            vendeur_nom="Admin",
            date_heure="2026-09-20 12:05:00",
            paiements=[("Espèces", Decimal("30.00"))],
            caisse_id="POS-01"
        )
        self.conn.commit()

        # Désactiver temporairement le trigger pour simuler une modification directe en base
        cursor.execute("DROP TRIGGER IF EXISTS prevent_ticket_tamper_update")
        cursor.execute("UPDATE Tickets SET total_tvac = 10.00 WHERE numero_ticket = 'TCK-V3-MODIF'")
        self.conn.commit()

        # La vérification d'intégrité doit impérativement lever une ValueError
        with self.assertRaises(ValueError) as ctx:
            verify_database_integrity(conn=self.conn)
        self.assertIn("Falsification de données détectée", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
