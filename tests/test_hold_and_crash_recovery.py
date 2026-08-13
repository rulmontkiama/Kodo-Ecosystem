import unittest
import sqlite3
import os
import sys
import json
import tempfile
from decimal import Decimal

# Inclure le dossier racine dans sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import database_manager
from database_manager import (
    initialiser_db,
    sauvegarder_panier_en_attente,
    lister_paniers_en_attente,
    recuperer_panier_en_attente,
    supprimer_panier_en_attente
)
from core.crash_watcher import CrashWatcher
from core.config import ShopConfig

class TestHoldAndCrashRecovery(unittest.TestCase):
    def setUp(self):
        self.db_name = ":memory:"
        database_manager.DB_NAME = self.db_name
        self.conn = sqlite3.connect(self.db_name)
        initialiser_db(conn=self.conn)
        
        # Configuration d'un dossier temporaire pour la session panier
        self.temp_dir = tempfile.mkdtemp()
        self.original_get_base_dir = ShopConfig.get_base_data_dir
        ShopConfig.get_base_data_dir = lambda: self.temp_dir

    def tearDown(self):
        self.conn.close()
        ShopConfig.get_base_data_dir = self.original_get_base_dir

    def test_mise_en_attente_et_restauration(self):
        """Vérifie qu'un panier peut être mis en attente, listé, restauré puis supprimé."""
        panier_sample = [
            {
                "nom": "Robe en Soie",
                "taille": "S",
                "prix_vente_tvac": Decimal("120.00"),
                "taux_tva": Decimal("0.21"),
                "stock_id": 101,
                "en_solde": 0,
                "prix_original_tvac": Decimal("120.00"),
                "code_barre": "111222333"
            }
        ]
        
        # 1. Sauvegarder en attente
        pid = sauvegarder_panier_en_attente(
            panier=panier_sample,
            total_tvac=Decimal("120.00"),
            client_id=5,
            client_nom="Marie Curie",
            remise=Decimal("0.00"),
            note="Test attente",
            conn=self.conn
        )
        self.assertIsNotNone(pid)
        
        # 2. Lister les paniers en attente
        paniers = lister_paniers_en_attente(conn=self.conn)
        self.assertEqual(len(paniers), 1)
        self.assertEqual(paniers[0]["id"], pid)
        self.assertEqual(paniers[0]["client_nom"], "Marie Curie")
        self.assertEqual(Decimal(str(paniers[0]["total_tvac"])), Decimal("120.00"))
        
        # 3. Récupérer / Restaurer le panier
        restaure = recuperer_panier_en_attente(pid, conn=self.conn)
        self.assertIsNotNone(restaure)
        self.assertEqual(len(restaure["panier_raw"]), 1)
        self.assertEqual(restaure["panier_raw"][0]["nom"], "Robe en Soie")
        
        # 4. Vérifier que la table est à nouveau vide après restauration
        paniers_apres = lister_paniers_en_attente(conn=self.conn)
        self.assertEqual(len(paniers_apres), 0)

    def test_suppression_panier_en_attente(self):
        """Vérifie la suppression définitive d'un panier en attente."""
        panier_sample = [{"nom": "Accessoire", "taille": "U", "prix_vente_tvac": "15.00", "stock_id": 2}]
        pid = sauvegarder_panier_en_attente(panier_sample, Decimal("15.00"), conn=self.conn)
        
        # Suppression
        res = supprimer_panier_en_attente(pid, conn=self.conn)
        self.assertTrue(res)
        self.assertEqual(len(lister_paniers_en_attente(conn=self.conn)), 0)

    def test_reprise_crash_watcher(self):
        """Simule un crash brutal avec une session panier non finalisée."""
        session_file = os.path.join(self.temp_dir, "panier_session.json")
        
        # Simuler l'état écrit par l'application avant interruption/crash
        mock_session_data = {
            "panier": [
                {
                    "nom": "Veste en Cuir",
                    "taille": "M",
                    "prix_vente_tvac": "250.00",
                    "taux_tva": "0.21",
                    "stock_id": 42
                }
            ],
            "remise": "0.00",
            "id_client": 12,
            "nom_client": "Sophie L.",
            "total_tvac": "250.00"
        }
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(mock_session_data, f)
            
        # Détection par CrashWatcher
        detected = CrashWatcher.get_unfinalized_basket()
        self.assertIsNotNone(detected)
        self.assertEqual(detected["nom_client"], "Sophie L.")
        self.assertEqual(len(detected["panier"]), 1)
        
        # Effacement du marqueur de crash
        CrashWatcher.clear_unfinalized_basket()
        self.assertIsNone(CrashWatcher.get_unfinalized_basket())

if __name__ == '__main__':
    unittest.main()
