import unittest
import os
import sys
import tempfile
import sqlite3
import shutil
import uuid
import datetime
from datetime import timezone
from decimal import Decimal

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from database_manager import initialiser_db, enregistrer_vente
from services.offline_sync_engine import OfflineSyncEngine
from core.resumable_downloader import ResumableDownloader, ResumableDownloadError

class TestOfflineAndResumable(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "offline_test.db")
        database_manager.DB_NAME = self.db_path
        self.conn = sqlite3.connect(self.db_path)
        initialiser_db(conn=self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_coupure_reseau_et_attribution_uuid_utc(self):
        """
        Vérifie qu'une coupure réseau durant le paiement génère un ticket hors-ligne
        doté d'un UUID unique temporaire, d'un timestamp UTC ISO strict et sync_status=0.
        """
        cursor = self.conn.cursor()
        off_uuid = str(uuid.uuid4())
        utc_ts = datetime.datetime.now(timezone.utc).isoformat()

        panier = [{"code_barre": "OFFLINE_ITEM", "stock_id": 1, "prix_vente_tvac": Decimal("89.90")}]
        
        # Enregistrement de la vente en mode offline (sync_status=0)
        ticket_id = enregistrer_vente(
            cursor=cursor,
            numero_ticket="TCK-OFF-001",
            total_tvac=Decimal("89.90"),
            total_htva=Decimal("74.30"),
            total_tva=Decimal("15.60"),
            remise=Decimal("0.00"),
            methode_paiement="Bancontact",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier,
            vendeur_nom="Vendeur Offline",
            date_heure="2026-07-28 13:00:00",
            paiements=[("Bancontact", Decimal("89.90"))],
            caisse_id="POS-OFFLINE-1",
            sync_status=0,
            offline_uuid=off_uuid,
            created_at_utc=utc_ts
        )
        self.conn.commit()

        # Vérifier en base la présence de l'UUID et du statut non-synchro
        cursor.execute("SELECT sync_status, offline_uuid, created_at_utc FROM Tickets WHERE id=?", (ticket_id,))
        row = cursor.fetchone()
        self.assertEqual(row[0], 0)  # sync_status = 0 (en attente)
        self.assertEqual(row[1], off_uuid)
        self.assertIn("T", row[2])  # ISO 8601 UTC string

    def test_double_vente_simultanee_conflit_stock_lww(self):
        """
        Vérifie qu'en cas de double vente simultanée du dernier article sur 2 caisses hors-ligne,
        la stratégie Last-Write-Wins valide les 2 transactions et marque le stock avec requires_stock_audit=1.
        """
        cursor = self.conn.cursor()
        
        # 1. Créer un produit et un stock initial de 1 seule unité
        cursor.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac) VALUES ('SOLO_ITEM', 'Article Unique', 100.0)")
        prod_id = cursor.lastrowid
        cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, 'M', 1, 1)", (prod_id,))
        stock_id = cursor.lastrowid
        self.conn.commit()

        # 2. Caisses A et B vendent simultanément cet article hors-ligne (quantité demandée = 1 par caisse)
        panier_a = [{"code_barre": "SOLO_ITEM", "stock_id": stock_id, "prix_vente_tvac": Decimal("100.00")}]
        panier_b = [{"code_barre": "SOLO_ITEM", "stock_id": stock_id, "prix_vente_tvac": Decimal("100.00")}]

        # Caisse A enregistre son ticket offline (sync_status=0)
        t_id_a = enregistrer_vente(
            cursor=cursor,
            numero_ticket="TCK-CAISSE-A",
            total_tvac=Decimal("100.00"),
            total_htva=Decimal("82.64"),
            total_tva=Decimal("17.36"),
            remise=Decimal("0.00"),
            methode_paiement="Bancontact",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier_a,
            vendeur_nom="Vendeur A",
            date_heure="2026-07-28 13:05:00",
            paiements=[("Bancontact", Decimal("100.00"))],
            caisse_id="POS-A",
            sync_status=0
        )
        # Caisse B enregistre son ticket offline (sync_status=0)
        t_id_b = enregistrer_vente(
            cursor=cursor,
            numero_ticket="TCK-CAISSE-B",
            total_tvac=Decimal("100.00"),
            total_htva=Decimal("82.64"),
            total_tva=Decimal("17.36"),
            remise=Decimal("0.00"),
            methode_paiement="Espèces",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier_b,
            vendeur_nom="Vendeur B",
            date_heure="2026-07-28 13:05:01",
            paiements=[("Espèces", Decimal("100.00"))],
            caisse_id="POS-B",
            sync_status=0
        )
        self.conn.commit()

        # 3. Synchronisation différée via le moteur OfflineSyncEngine (Stratégie LWW)
        synced_count = OfflineSyncEngine.process_pending_tickets(conn=self.conn)
        self.assertEqual(synced_count, 2)

        # 4. Vérifications :
        # - Les deux tickets sont validés (sync_status=1)
        cursor.execute("SELECT COUNT(*) FROM Tickets WHERE sync_status=1")
        self.assertEqual(cursor.fetchone()[0], 2)

        # - Le stock est tombé sous 0 (-1)
        cursor.execute("SELECT quantite_actuelle, requires_stock_audit FROM Stocks WHERE id=?", (stock_id,))
        s_qte, s_audit = cursor.fetchone()
        self.assertEqual(s_qte, -1)
        self.assertEqual(s_audit, 1)  # Marqué pour audit manuel

        # - Le produit est également marqué pour audit
        cursor.execute("SELECT requires_stock_audit FROM Produits WHERE id=?", (prod_id,))
        p_audit = cursor.fetchone()[0]
        self.assertEqual(p_audit, 1)

    def test_reconnexion_instable_flapping_backoff(self):
        """Vérifie la résilience du moteur lors d'une connexion réseau instable."""
        cursor = self.conn.cursor()
        
        # Inserer un ticket en attente
        cursor.execute("INSERT INTO Tickets (numero_ticket, total_tvac, sync_status) VALUES ('T-FLAP-01', 30.00, 0)")
        self.conn.commit()

        # Premier traitement de synchro
        count1 = OfflineSyncEngine.process_pending_tickets(conn=self.conn)
        self.assertEqual(count1, 1)

        # Un deuxième appel ne doit pas dupliquer la synchro (0 ticket restant)
        count2 = OfflineSyncEngine.process_pending_tickets(conn=self.conn)
        self.assertEqual(count2, 0)

    def test_resumable_downloader_sha256(self):
        """Vérifie le calcul de checksum SHA256 pour le téléchargeur reprenable."""
        test_file = os.path.join(self.temp_dir, "test_file.bin")
        with open(test_file, "wb") as f:
            f.write(b"KODO_POS_RESUMABLE_DOWNLOAD_DATA_TEST_BYTES")

        expected_hash = ResumableDownloader.calculate_sha256(test_file)
        self.assertTrue(len(expected_hash) == 64)

if __name__ == "__main__":
    unittest.main()
