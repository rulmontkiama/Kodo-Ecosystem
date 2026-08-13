import unittest
import sqlite3
import os
import sys
from decimal import Decimal

# Inclure le dossier racine dans sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import database_manager
from database_manager import initialiser_db, enregistrer_vente, calculer_hash_transaction
from audit_trail import verify_database_integrity

class TestAuditChain(unittest.TestCase):
    def setUp(self):
        self.db_name = ":memory:"
        database_manager.DB_NAME = self.db_name
        self.conn = sqlite3.connect(self.db_name)
        initialiser_db(conn=self.conn)

    def tearDown(self):
        self.conn.close()

    def test_chaine_valide(self):
        """Vérifie qu'une suite de transactions génère un chaînage cryptographique valide."""
        cursor = self.conn.cursor()
        
        # Inserer 3 ventes
        for i in range(1, 4):
            num_ticket = f"TCK-TEST-{i:03d}"
            total_tvac = Decimal(f"{i * 10}.00")
            total_htva = Decimal(f"{i * 8}.00")
            total_tva = Decimal(f"{i * 2}.00")
            panier = [{"code_barre": f"ART00{i}", "stock_id": i, "prix_vente_tvac": Decimal(f"{i * 10}.00")}]
            paiements = [("Bancontact", total_tvac)]
            
            enregistrer_vente(
                cursor=cursor,
                numero_ticket=num_ticket,
                total_tvac=total_tvac,
                total_htva=total_htva,
                total_tva=total_tva,
                remise=Decimal("0.00"),
                methode_paiement="Bancontact",
                id_client=None,
                rendu_monnaie=Decimal("0.00"),
                panier=panier,
                vendeur_nom="Vendeur Test",
                date_heure=f"2026-07-28 12:00:0{i}",
                paiements=paiements,
                caisse_id="POS-TEST-01"
            )
            
        self.conn.commit()
        
        # Vérification de l'intégrité
        res = verify_database_integrity(conn=self.conn)
        self.assertTrue(res)

    def test_detection_falsification_montant(self):
        """Vérifie que la falsification du montant d'une vente est détectée."""
        cursor = self.conn.cursor()
        panier = [{"code_barre": "ART01", "stock_id": 1, "prix_vente_tvac": Decimal("50.00")}]
        enregistrer_vente(
            cursor=cursor,
            numero_ticket="TCK-TEST-MODIF",
            total_tvac=Decimal("50.00"),
            total_htva=Decimal("40.00"),
            total_tva=Decimal("10.00"),
            remise=Decimal("0.00"),
            methode_paiement="Espèces",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier,
            vendeur_nom="Vendeur Test",
            date_heure="2026-07-28 12:10:00",
            paiements=[("Espèces", Decimal("50.00"))],
            caisse_id="POS-TEST-01"
        )
        self.conn.commit()

        # Falsifier directement le montant en base de données
        cursor.execute("UPDATE Tickets SET total_tvac = 5.00 WHERE numero_ticket = 'TCK-TEST-MODIF'")
        self.conn.commit()

        # verify_database_integrity doit lever une ValueError
        with self.assertRaises(ValueError) as ctx:
            verify_database_integrity(conn=self.conn)
        
        self.assertIn("Falsification de données détectée", str(ctx.exception))

    def test_detection_rupture_suppression_ticket(self):
        """Vérifie que la suppression d'un ticket au milieu de la chaîne est détectée."""
        cursor = self.conn.cursor()
        
        for i in range(1, 4):
            enregistrer_vente(
                cursor=cursor,
                numero_ticket=f"TCK-DEL-{i}",
                total_tvac=Decimal("20.00"),
                total_htva=Decimal("16.00"),
                total_tva=Decimal("4.00"),
                remise=Decimal("0.00"),
                methode_paiement="Bancontact",
                id_client=None,
                rendu_monnaie=Decimal("0.00"),
                panier=[{"code_barre": "ART", "stock_id": 1, "prix_vente_tvac": Decimal("20.00")}],
                vendeur_nom="Vendeur Test",
                date_heure=f"2026-07-28 12:20:0{i}",
                paiements=[("Bancontact", Decimal("20.00"))],
                caisse_id="POS-TEST-01"
            )
        self.conn.commit()

        # Supprimer le ticket intermédiaire TCK-DEL-2
        cursor.execute("DELETE FROM Tickets WHERE numero_ticket = 'TCK-DEL-2'")
        self.conn.commit()

        # verify_database_integrity doit lever une ValueError pour rupture de chaîne
        with self.assertRaises(ValueError) as ctx:
            verify_database_integrity(conn=self.conn)

        self.assertIn("Rupture de chaîne détectée", str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
