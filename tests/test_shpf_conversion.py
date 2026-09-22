# -*- coding: utf-8 -*-
"""
Tests unitaires et d'intégration pour la conversion des codes SHPF en EAN-13 (P5.2).

Vérifie que :
1. Les anciens codes 'SHPF-<id>' sont convertis en vrais EAN-13 internes valides (13 chiffres, clé de contrôle exacte).
2. Les codes réguliers existants ne sont pas altérés.
3. La table `Shopify_Variantes` est peuplée pour préserver la correspondance Shopify sans doublon.
4. Le moteur `ShopifySync._retrouver_produit` continue d'identifier le produit après conversion.
5. La migration 2.0.8 exécute la conversion de façon transparente.
"""

import os
import sqlite3
import tempfile
import unittest

from kodo_core.db.migrations import MigrationManager
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.sync.shopify import ShopifySync


class TestConversionCodesSHPF(unittest.TestCase):

    def setUp(self):
        self.fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.conn = sqlite3.connect(self.db_path)
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE Produits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_barre TEXT UNIQUE,
                nom TEXT NOT NULL,
                categorie TEXT,
                prix_vente_tvac DECIMAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.close(self.fd)
        for sfx in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + sfx):
                os.remove(self.db_path + sfx)

    def test_convertir_codes_shpf_en_vrais_ean13(self):
        """Les codes SHPF sont convertis en EAN-13 valides et les autres codes restent intacts."""
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom) VALUES ('SHPF-11111', 'Robe Soie')")
        p1 = c.lastrowid
        c.execute("INSERT INTO Produits (code_barre, nom) VALUES ('SHPF-22222', 'Chemise Lin')")
        p2 = c.lastrowid
        c.execute("INSERT INTO Produits (code_barre, nom) VALUES ('5412345678908', 'Produit Déjà EAN13')")
        p3 = c.lastrowid
        self.conn.commit()

        res = InventoryManager.convertir_codes_shpf(self.conn)
        self.conn.commit()

        self.assertEqual(res["convertis"], 2)

        # Vérification du produit 1
        c.execute("SELECT code_barre FROM Produits WHERE id = ?", (p1,))
        code1 = c.fetchone()[0]
        self.assertTrue(InventoryManager.is_valid_ean13(code1), f"Le code généré {code1} n'est pas un EAN-13 valide")
        self.assertFalse(code1.startswith("SHPF-"))

        # Vérification du produit 2
        c.execute("SELECT code_barre FROM Produits WHERE id = ?", (p2,))
        code2 = c.fetchone()[0]
        self.assertTrue(InventoryManager.is_valid_ean13(code2), f"Le code généré {code2} n'est pas un EAN-13 valide")
        self.assertNotEqual(code1, code2, "Deux produits ne doivent jamais recevoir le même code")

        # Vérification du produit déjà EAN-13 : intact
        c.execute("SELECT code_barre FROM Produits WHERE id = ?", (p3,))
        self.assertEqual(c.fetchone()[0], "5412345678908")

        # Vérification de la table Shopify_Variantes
        c.execute("SELECT variant_id, id_produit FROM Shopify_Variantes ORDER BY variant_id ASC")
        variantes = c.fetchall()
        self.assertEqual(variantes, [(11111, p1), (22222, p2)])

    def test_retrouver_produit_shopify_apres_conversion(self):
        """Le moteur ShopifySync retrouve le produit même si son code-barres n'est plus SHPF-."""
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom) VALUES ('SHPF-77777', 'Pull Laine')")
        pid = c.lastrowid
        self.conn.commit()

        InventoryManager.convertir_codes_shpf(self.conn)
        self.conn.commit()

        sync = ShopifySync(store_url="https://test.myshopify.com", access_token="token")

        # 1. Recherche par variant_id (la méthode prioritaire)
        trouve_par_variant = sync._retrouver_produit(c, variant_id=77777, code=None, code_brut=None)
        self.assertEqual(trouve_par_variant, pid)

        # 2. Même si Shopify envoie une chaîne vide pour le code-barres
        trouve_vide = sync._retrouver_produit(c, variant_id=77777, code="", code_brut="")
        self.assertEqual(trouve_vide, pid)

    def test_mutation_ean13_invalide_est_detectee(self):
        """Test de mutation : altérer le dernier chiffre d'un EAN-13 le rend immédiatement invalide."""
        c = self.conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom) VALUES ('SHPF-999', 'Article')")
        pid = c.lastrowid
        self.conn.commit()

        InventoryManager.convertir_codes_shpf(self.conn)
        self.conn.commit()

        c.execute("SELECT code_barre FROM Produits WHERE id = ?", (pid,))
        bon_code = c.fetchone()[0]
        self.assertTrue(InventoryManager.is_valid_ean13(bon_code))

        # Sabotage de la clé de contrôle (dernier chiffre)
        mauvais_dernier = "0" if bon_code[-1] != "0" else "1"
        code_sabote = bon_code[:-1] + mauvais_dernier

        self.assertFalse(InventoryManager.is_valid_ean13(code_sabote),
                         "Un code dont la clé de contrôle est fausse ne doit JAMAIS être validé")

    def test_migration_2_0_8_applique_la_conversion(self):
        """La migration 2.0.8 exécute automatiquement la conversion des codes résiduels."""
        c = self.conn.cursor()
        c.execute("INSERT INTO schema_version (version) VALUES ('2.0.7')")
        c.execute("INSERT INTO Produits (code_barre, nom) VALUES ('SHPF-555', 'Chemise Vintage')")
        pid = c.lastrowid
        self.conn.commit()

        # Exécution de la migration 2.0.8
        MigrationManager.run_migrations(db_path=self.db_path)

        # Vérification
        c.execute("SELECT code_barre FROM Produits WHERE id = ?", (pid,))
        nouveau_code = c.fetchone()[0]
        self.assertTrue(InventoryManager.is_valid_ean13(nouveau_code))
        self.assertFalse(nouveau_code.startswith("SHPF-"))

        c.execute("SELECT variant_id, id_produit FROM Shopify_Variantes WHERE variant_id = 555")
        self.assertEqual(c.fetchone(), (555, pid))


if __name__ == "__main__":
    unittest.main()
