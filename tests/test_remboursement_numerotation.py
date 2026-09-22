# -*- coding: utf-8 -*-
"""
Kōdo POS — Le numéro d'un ticket de remboursement ne doit jamais entrer en collision.

Historique : `enregistrer_remboursement` composait le numéro comme
`REF-<ticket d'origine>-<horodatage tronqué à 5 chiffres>`. Deux remboursements de lignes
DIFFÉRENTES d'un même ticket tombant dans la même seconde produisaient donc le MÊME numéro,
et `Tickets.numero_ticket` étant UNIQUE, le second remboursement échouait sur
`sqlite3.IntegrityError` — alors que l'argent venait d'être rendu à la cliente.
Le chemin vente était protégé par un retry borné (`process_sale_transaction`) ;
le chemin remboursement avait été oublié. Deux audits indépendants ont relevé le défaut.

L'horodatage tronqué reboucle en plus toutes les 100 000 s (~27 h 46), donc la collision
n'est pas seulement instantanée.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestNumerotationRemboursement(unittest.TestCase):

    def setUp(self):
        # Base jetable à chaque test (patron de tests/test_stock_integrite.py) :
        # aucune donnée réelle n'est touchée. HOME est déjà redirigé par conftest.py.
        import database_manager as dm
        self.dm = dm
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._db_origine = dm.DB_NAME
        dm.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        dm.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        self.dm.DB_NAME = self._db_origine
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _vente_de_deux_lignes(self, cursor):
        """Un ticket, deux articles distincts : le cas exact où la vendeuse rembourse les deux."""
        cursor.execute(
            "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Article A', 60.50, 0.21)")
        pa = cursor.lastrowid
        cursor.execute(
            "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Article B', 24.20, 0.21)")
        pb = cursor.lastrowid
        cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (pa,))
        sa = cursor.lastrowid
        cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'L', 10)", (pb,))
        sb = cursor.lastrowid

        num = "TCK-COLLISION-001"
        self.dm.enregistrer_vente(
            cursor=cursor,
            numero_ticket=num,
            total_tvac=84.70, total_htva=70.00, total_tva=14.70, remise=0.0,
            methode_paiement="Espèces", id_client=None, rendu_monnaie=0.0,
            panier=[
                {"stock_id": sa, "prix_vente_tvac": 60.50, "quantite": 1},
                {"stock_id": sb, "prix_vente_tvac": 24.20, "quantite": 1},
            ],
            vendeur_nom="Vendeuse",
            date_heure="2026-09-21 14:32:10",
            paiements=[("Espèces", 84.70)],
        )
        cursor.execute(
            "SELECT id FROM Ventes_Details WHERE id_ticket = "
            "(SELECT id FROM Tickets WHERE numero_ticket = ?) ORDER BY id", (num,))
        vd_ids = [r[0] for r in cursor.fetchall()]
        return num, vd_ids, (sa, sb)

    def test_deux_remboursements_dans_la_meme_seconde_aboutissent(self):
        """Le cœur du défaut : aucune attente artificielle entre les deux remboursements."""
        conn = sqlite3.connect(self.path)
        try:
            cursor = conn.cursor()
            num, vd_ids, (sa, sb) = self._vente_de_deux_lignes(cursor)
            horodatage = "2026-09-21 14:32:11"  # une seule et même seconde pour les deux

            ref_a, montant_a = self.dm.enregistrer_remboursement(
                cursor=cursor, ticket_origine=num, vd_id=vd_ids[0], stock_id=sa,
                prix=Decimal("60.50"), mode="Espèces", vendeur_nom="Vendeuse",
                date_heure=horodatage, quantite=1)

            try:
                ref_b, montant_b = self.dm.enregistrer_remboursement(
                    cursor=cursor, ticket_origine=num, vd_id=vd_ids[1], stock_id=sb,
                    prix=Decimal("24.20"), mode="Espèces", vendeur_nom="Vendeuse",
                    date_heure=horodatage, quantite=1)
            except sqlite3.IntegrityError as e:
                self.fail(
                    "Le second remboursement a échoué sur une collision de numéro alors que "
                    f"l'argent a déjà été rendu à la cliente : {e}")

            self.assertNotEqual(ref_a, ref_b, "Les deux remboursements portent le même numéro.")
            self.assertEqual(montant_a, Decimal("-60.50"))
            self.assertEqual(montant_b, Decimal("-24.20"))

            cursor.execute("SELECT COUNT(*) FROM Tickets WHERE numero_ticket LIKE 'REF-%'")
            self.assertEqual(cursor.fetchone()[0], 2, "Les deux tickets de remboursement doivent exister.")
            conn.commit()
        finally:
            conn.close()

    def test_les_numeros_de_remboursement_restent_uniques_en_rafale(self):
        """Dix remboursements d'affilée : tous distincts, aucun perdu."""
        conn = sqlite3.connect(self.path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Chaussette', 12.10, 0.21)")
            pid = cursor.lastrowid
            cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'U', 50)", (pid,))
            sid = cursor.lastrowid

            num = "TCK-RAFALE-001"
            self.dm.enregistrer_vente(
                cursor=cursor, numero_ticket=num,
                total_tvac=121.00, total_htva=100.00, total_tva=21.00, remise=0.0,
                methode_paiement="CB", id_client=None, rendu_monnaie=0.0,
                panier=[{"stock_id": sid, "prix_vente_tvac": 12.10, "quantite": 10}],
                vendeur_nom="Vendeuse", date_heure="2026-09-21 15:00:00",
                paiements=[("CB", 121.00)])
            cursor.execute(
                "SELECT id FROM Ventes_Details WHERE id_ticket = "
                "(SELECT id FROM Tickets WHERE numero_ticket = ?)", (num,))
            vd_id = cursor.fetchone()[0]

            refs = []
            for _ in range(10):
                ref, _montant = self.dm.enregistrer_remboursement(
                    cursor=cursor, ticket_origine=num, vd_id=vd_id, stock_id=sid,
                    prix=Decimal("12.10"), mode="CB", vendeur_nom="Vendeuse",
                    date_heure="2026-09-21 15:00:01", quantite=1)
                refs.append(ref)

            self.assertEqual(len(set(refs)), 10, f"Numéros dupliqués dans la rafale : {refs}")
            cursor.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (sid,))
            self.assertEqual(cursor.fetchone()[0], 50, "Les 10 unités vendues doivent être recréditées.")
            conn.commit()
        finally:
            conn.close()

    def test_le_onzieme_remboursement_reste_refuse(self):
        """La correction ne doit pas ouvrir la porte au sur-remboursement."""
        conn = sqlite3.connect(self.path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Écharpe', 30.25, 0.21)")
            pid = cursor.lastrowid
            cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'U', 5)", (pid,))
            sid = cursor.lastrowid

            num = "TCK-LIMITE-001"
            self.dm.enregistrer_vente(
                cursor=cursor, numero_ticket=num,
                total_tvac=60.50, total_htva=50.00, total_tva=10.50, remise=0.0,
                methode_paiement="CB", id_client=None, rendu_monnaie=0.0,
                panier=[{"stock_id": sid, "prix_vente_tvac": 30.25, "quantite": 2}],
                vendeur_nom="Vendeuse", date_heure="2026-09-21 16:00:00",
                paiements=[("CB", 60.50)])
            cursor.execute(
                "SELECT id FROM Ventes_Details WHERE id_ticket = "
                "(SELECT id FROM Tickets WHERE numero_ticket = ?)", (num,))
            vd_id = cursor.fetchone()[0]

            for _ in range(2):
                self.dm.enregistrer_remboursement(
                    cursor=cursor, ticket_origine=num, vd_id=vd_id, stock_id=sid,
                    prix=Decimal("30.25"), mode="CB", vendeur_nom="Vendeuse",
                    date_heure="2026-09-21 16:00:01", quantite=1)

            with self.assertRaises(ValueError):
                self.dm.enregistrer_remboursement(
                    cursor=cursor, ticket_origine=num, vd_id=vd_id, stock_id=sid,
                    prix=Decimal("30.25"), mode="CB", vendeur_nom="Vendeuse",
                    date_heure="2026-09-21 16:00:01", quantite=1)
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
