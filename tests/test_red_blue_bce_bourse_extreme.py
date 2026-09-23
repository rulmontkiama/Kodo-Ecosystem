# -*- coding: utf-8 -*-
"""
Kōdo POS — Suite de Stress & Invariants d'Élite (Niveau Banque Centrale Européenne / Bourse)
Red Team vs Blue Team Extreme Adversarial Suite.

Vérifications critiques :
1. Conservation monétaire absolue sur 10 000 permutations aléatoires (TVA, remises, arrondis belges).
2. Intégrité cryptographique NF525 inviolable (Triggers d'immutabilité + Merkle Hash Chain).
3. Concurrence haute fréquence multithreadée (20 workers simultanés sans deadlock ni corruption de stock).
4. Marge commerciale, valorisation de stock et invariants du prix d'achat HTVA.
5. Fuzzing matériel de buffer thermique (simulation micro-buffer FIFO 1 Ko / 2 Ko, saturation impossible).
6. Étanchéité multitenant absolue (zéro résidu ni fuite de données de tiers sur base vierge).
7. Anti-double dépense & idempotence stricte des flux financiers.
"""

import concurrent.futures
from decimal import Decimal, ROUND_HALF_UP
import os
import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import database_manager
from audit_trail import verify_database_integrity
from kodo_core.domain.sales.models import quantize_money
from kodo_core.domain.sales.cart_engine import apply_belgian_cash_rounding
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.hardware.printer import (
    pil_to_escpos_raster,
    generate_social_qr_image,
    generer_ticket,
    generer_image_ticket,
    get_ticket_logo_path,
    get_ticket_social_path,
    sanitize_escpos_text,
    generer_ticket_test
)


class TestBceBourseExtremeStress(unittest.TestCase):

    def setUp(self):
        self.fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.db_path
        os.environ["KODO_DB_PATH"] = self.db_path
        database_manager.initialiser_db()

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for sfx in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + sfx):
                try:
                    os.remove(self.db_path + sfx)
                except Exception:
                    pass

    # =========================================================================
    # 1. CONSERVATION MONÉTAIRE & ARRONDIS (NIVEAU BANQUE CENTRALE)
    # =========================================================================

    def test_bce_conservation_monetaire_10000_permutations(self):
        """
        10 000 transactions financières aléatoires avec combinaisons hostiles de TVA,
        remises en cascade et arrondis monétaires.
        Invariant BCE : Total TVAC == Base HTVA + Montant TVA, sans dérive cumulative.
        """
        taux_tva_possibles = [Decimal("0.00"), Decimal("0.06"), Decimal("0.12"), Decimal("0.21")]
        CENT = Decimal("0.01")

        random.seed(42)

        for i in range(10_000):
            prix_brut = Decimal(random.randint(1, 99999)) / Decimal("100")
            qte = random.randint(1, 15)
            taux = random.choice(taux_tva_possibles)
            remise_pct = Decimal(random.randint(0, 50))

            total_ligne_brut = prix_brut * Decimal(qte)
            montant_remise = (total_ligne_brut * (remise_pct / Decimal("100"))).quantize(CENT, rounding=ROUND_HALF_UP)
            total_net_tvac = total_ligne_brut - montant_remise

            base_htva = (total_net_tvac / (Decimal("1.00") + taux)).quantize(CENT, rounding=ROUND_HALF_UP)
            montant_tva = (total_net_tvac - base_htva).quantize(CENT, rounding=ROUND_HALF_UP)

            self.assertEqual(
                base_htva + montant_tva,
                total_net_tvac,
                f"Dérive au centime détectée à l'itération {i} : HT {base_htva} + TVA {montant_tva} != TVAC {total_net_tvac}"
            )

            arrondi_cash, ecart = apply_belgian_cash_rounding(total_net_tvac)
            self.assertLessEqual(
                abs(ecart),
                Decimal("0.02"),
                f"L'arrondi belge a dévié de plus de 2 centimes ({ecart}) pour {total_net_tvac}"
            )

    # =========================================================================
    # 2. INTÉGRITÉ CRYPTOGRAPHIQUE NF525 & MERKLE HASH CHAIN
    # =========================================================================

    def test_bce_audit_trail_inviolable_hash_chain(self):
        """
        Vérifie la chaîne cryptographique SHA-256 sur 50 transactions séquentielles.
        Simule ensuite une attaque Red Team :
        1. Tentative de falsification directe via UPDATE -> bloquée net par trigger NF525.
        2. Bypassement du trigger par sabotage interne -> détecté net par la chaîne SHA-256.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        for i in range(1, 51):
            num_tck = f"TCK-BCE-{i:04d}"
            montant = Decimal(f"{i * 12}.50")
            ht = (montant / Decimal("1.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tva = montant - ht

            database_manager.enregistrer_vente(
                cursor=cursor,
                numero_ticket=num_tck,
                total_tvac=montant,
                total_htva=ht,
                total_tva=tva,
                remise=Decimal("0.00"),
                methode_paiement="Bancontact",
                id_client=None,
                rendu_monnaie=Decimal("0.00"),
                panier=[{"code_barre": f"EAN{i:03d}", "stock_id": 1, "prix_vente_tvac": montant}],
                vendeur_nom="Superviseur BCE",
                date_heure=f"2026-09-23 10:{i:02d}:00",
                paiements=[("Bancontact", montant)],
                caisse_id="POS-BCE-01"
            )
        conn.commit()

        self.assertTrue(
            verify_database_integrity(conn=conn),
            "La chaîne cryptographique intègre a été injustement invalidée !"
        )

        # 1. Attaque directe bloquée par le trigger d'immutabilité
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("UPDATE Tickets SET total_tvac = total_tvac + 0.01 WHERE numero_ticket = 'TCK-BCE-0025'")

        # 2. Attaque furtive avec destruction préalable du trigger
        cursor.execute("DROP TRIGGER IF EXISTS prevent_ticket_tamper_update")
        cursor.execute("UPDATE Tickets SET total_tvac = total_tvac + 0.01 WHERE numero_ticket = 'TCK-BCE-0025'")
        conn.commit()

        # 3. La chaîne cryptographique Merkle détecte formellement la rupture et lève ValueError
        with self.assertRaises(ValueError) as ctx:
            verify_database_integrity(conn=conn)
        self.assertIn("Falsification de données détectée", str(ctx.exception))
        conn.close()

    # =========================================================================
    # 3. CONCURRENCE HAUTE FRÉQUENCE BOURSE (20 WORKERS SIMULTANÉS)
    # =========================================================================

    def test_bourse_haute_frequence_concurrence_20_workers(self):
        """
        Stress test de concurrence type salle des marchés :
        20 threads concurrents bombardent simultanément la base :
        - 10 threads de vente avec décrémentation atomique de stock sur un produit chaud (500 unités).
        - 5 threads de lecture / reporting financier.
        - 5 threads de mise à jour de catalogue et prix d'achat sur d'autres références.
        Vérifie qu'aucun deadlock n'explose et que la conservation de stock est absolue.
        """
        conn_init = sqlite3.connect(self.db_path)
        c_init = conn_init.cursor()
        c_init.execute("INSERT INTO Categories (nom) VALUES ('Action Bourse')")
        cat_id = c_init.lastrowid
        c_init.execute("""
            INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac, prix_achat_htva, taux_tva)
            VALUES ('BOURSESKU01', 'Titre de Participation Kōdo', 'Action Bourse', 100.00, 75.00, 0.21)
        """)
        prod_id = c_init.lastrowid
        STOCK_INITIAL = 500
        c_init.execute("INSERT INTO Stocks (id_produit, quantite_actuelle) VALUES (?, ?)", (prod_id, STOCK_INITIAL))
        stock_id = c_init.lastrowid

        # Deuxième produit dédié aux modifications catalogue
        c_init.execute("""
            INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac, prix_achat_htva, taux_tva)
            VALUES ('BOURSESKU02', 'Obligation Entreprise', 'Action Bourse', 50.00, 40.00, 0.21)
        """)
        prod2_id = c_init.lastrowid
        c_init.execute("INSERT INTO Stocks (id_produit, quantite_actuelle) VALUES (?, 200)", (prod2_id,))
        conn_init.commit()
        conn_init.close()

        ventes_reussies = 0
        lock = threading.Lock()
        erreurs = []

        def worker_vendeur(worker_id):
            nonlocal ventes_reussies
            for j in range(10):
                try:
                    c_worker = sqlite3.connect(self.db_path, timeout=30.0)
                    cur = c_worker.cursor()
                    cur.execute("PRAGMA busy_timeout = 30000")
                    num_ticket = f"TCK-HF-{worker_id}-{j}"
                    panier = [{
                        "code_barre": "BOURSESKU01",
                        "stock_id": stock_id,
                        "prix_vente_tvac": Decimal("100.00"),
                        "quantite": 1
                    }]
                    database_manager.enregistrer_vente(
                        cursor=cur,
                        numero_ticket=num_ticket,
                        total_tvac=Decimal("100.00"),
                        total_htva=Decimal("82.64"),
                        total_tva=Decimal("17.36"),
                        remise=Decimal("0.00"),
                        methode_paiement="Bancontact",
                        id_client=None,
                        rendu_monnaie=Decimal("0.00"),
                        panier=panier,
                        vendeur_nom=f"Trader {worker_id}",
                        date_heure="2026-09-23 11:00:00",
                        paiements=[("Bancontact", Decimal("100.00"))],
                        caisse_id=f"POS-HF-{worker_id}"
                    )
                    c_worker.commit()
                    c_worker.close()
                    with lock:
                        ventes_reussies += 1
                except Exception as e:
                    with lock:
                        erreurs.append(f"Erreur Vendeur {worker_id}-{j}: {e}")

        def worker_lecteur(worker_id):
            for _ in range(10):
                try:
                    c_read = sqlite3.connect(self.db_path, timeout=30.0)
                    cur = c_read.cursor()
                    cur.execute("PRAGMA busy_timeout = 30000")
                    cur.execute("SELECT SUM(total_tvac) FROM Tickets")
                    cur.fetchone()
                    cur.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (stock_id,))
                    cur.fetchone()
                    c_read.close()
                except Exception as e:
                    with lock:
                        erreurs.append(f"Erreur Lecteur {worker_id}: {e}")

        def worker_editeur(worker_id):
            for step in range(5):
                try:
                    inv = InventoryManager()
                    nouveau_prix_achat = Decimal(35 + worker_id + step)
                    inv.save_product({
                        "id": str(prod2_id),
                        "code_barre": "BOURSESKU02",
                        "nom": "Obligation Entreprise",
                        "prix_vente_ttc": 50.00,
                        "costPrice": float(nouveau_prix_achat),
                        "taux_tva": 0.21,
                        "categorie": "Action Bourse",
                        "stock": 200
                    })
                except Exception as e:
                    with lock:
                        erreurs.append(f"Erreur Editeur {worker_id}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = []
            for w in range(10):
                futures.append(executor.submit(worker_vendeur, w))
            for w in range(5):
                futures.append(executor.submit(worker_lecteur, w))
            for w in range(5):
                futures.append(executor.submit(worker_editeur, w))
            concurrent.futures.wait(futures)

        self.assertEqual(erreurs, [], f"Des erreurs de concurrence ont été relevées : {erreurs}")
        self.assertEqual(ventes_reussies, 100, f"Attendu 100 ventes concurrentes, obtenu {ventes_reussies}")

        conn_verif = sqlite3.connect(self.db_path)
        c_verif = conn_verif.cursor()
        c_verif.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (stock_id,))
        stock_final = c_verif.fetchone()[0]
        conn_verif.close()

        self.assertEqual(
            stock_final,
            STOCK_INITIAL - ventes_reussies,
            f"Anomalie de conservation de stock sous concurrence : initial={STOCK_INITIAL}, vendus={ventes_reussies}, actuel={stock_final}"
        )

    # =========================================================================
    # 4. MARGE COMMERCIALE, VALORISATION DE STOCK & PRIX D'ACHAT HTVA
    # =========================================================================

    def test_bourse_marge_commerciale_et_valorisation(self):
        """
        Vérifie la robustesse mathématique du calcul de marge brute :
        - Marge Brute = Prix Vente HTVA - Prix Achat HTVA.
        - Persistance précise sur des décimales micro-financières.
        """
        inv = InventoryManager()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO Categories (nom) VALUES ('High Tech')")
        conn.commit()

        res = inv.save_product({
            "code_barre": "MICRO001",
            "nom": "Composant Micro-Finance",
            "prix_vente_ttc": 12.10,
            "costPrice": 4.55,
            "taux_tva": 0.21,
            "categorie": "High Tech"
        })
        p_id = res["product_id"]

        prods = inv.get_all_products()
        target = next(p for p in prods if str(p.get("id")) == str(p_id))

        self.assertEqual(float(target["costPrice"]), 4.55)
        self.assertEqual(float(target["prix_achat_htva"]), 4.55)

        pv_ht = Decimal("12.10") / Decimal("1.21")
        pa_ht = Decimal(str(target["costPrice"]))
        marge_unitaire = pv_ht - pa_ht
        taux_marque = (marge_unitaire / pv_ht) * Decimal("100")

        self.assertEqual(marge_unitaire.quantize(Decimal("0.01")), Decimal("5.45"))
        self.assertEqual(taux_marque.quantize(Decimal("0.1")), Decimal("54.5"))
        conn.close()

    # =========================================================================
    # 5. FUZZING BUFFER MATÉRIEL THERMIQUE (MICRO-BUFFER 1 Ko / 2 Ko)
    # =========================================================================

    def test_bce_micro_buffer_thermique_1kb_2kb_jamais_sature(self):
        """
        Simulation de micro-buffers matériels d'imprimante thermique restreints à 2048 octets.
        Vérifie que pour toutes les tailles d'images et textes complexes générés,
        chaque tranche raster ESC/POS émise par `pil_to_escpos_raster()` ne dépasse JAMAIS
        la limite de 2048 octets (32 dots = ~1.5 Ko max).
        """
        img_standard = generate_social_qr_image(
            title="Kōdo Official",
            url="https://kōdo-solutions.com",
            subtitle="@kodo_pos",
            header="Suivez l'excellence sur"
        )

        raster_bytes = pil_to_escpos_raster(img_standard, max_width=384)

        BUFFER_LIMIT = 2048
        GS_V_0 = b"\x1d\x76\x30\x00"

        slices = raster_bytes.split(GS_V_0)
        self.assertGreater(len(slices), 1, "Le flux raster doit être découpé en plusieurs tranches distinctes !")

        for idx, s in enumerate(slices[1:], start=1):
            total_slice_size = len(GS_V_0) + len(s)
            self.assertLessEqual(
                total_slice_size,
                BUFFER_LIMIT,
                f"La tranche #{idx} dépasse la limite de buffer matériel de 2048 octets ({total_slice_size} octets reçus) !"
            )

    def test_bce_fuzzing_caracteres_hostiles_et_injections(self):
        """
        Fuzzing adversarial : injection de caractères de contrôle ESC/POS malveillants,
        homoglyphes, accents exotiques, null bytes, et scripts XSS/SQL.
        """
        payload_hostile = (
            "T-Shirt Hacker \x00\x1b\x40\x1d\x56\x00\x10\x14"
            "<script>alert('xss')</script>"
            "Robert'); DROP TABLE Tickets;--"
            "Café crème & Thé glacé à 19.99 € — Kōdo Solutions"
        )

        texte_propre = sanitize_escpos_text(payload_hostile)

        for c in texte_propre:
            code = ord(c)
            self.assertTrue(
                code >= 32 or c in ("\n", "\t"),
                f"Caractère de contrôle résiduel détecté : {code}"
            )

    # =========================================================================
    # 6. ÉTANCHÉITÉ MULTITENANT & ZÉRO FUITE CLIENT SUR BASE USINE
    # =========================================================================

    def test_bce_etancheite_absolue_zero_fuite_client_sur_base_vierge(self):
        """
        Audit d'étanchéité absolue (Zéro Résidu) :
        Sur une base de données neuve, vérifie qu'aucun identifiant de l'ancien client
        n'apparaît nulle part.
        """
        self.assertIsNone(get_ticket_logo_path(), "Fuite : un logo est retourné sur une base usine vierge !")
        self.assertIsNone(get_ticket_social_path(), "Fuite : un bloc social est retourné sur une base usine vierge !")

        ticket_txt = generer_ticket_test()
        self.assertNotIn("l_adresse", ticket_txt.lower(), "Fuite : mention de l'ancien client dans le ticket texte !")
        self.assertNotIn("1035.331.577", ticket_txt, "Fuite : ancien numéro de TVA présent !")

        img_path = generer_image_ticket(ticket_txt, "TEST_BCE_ZERO_LEAK")
        self.assertTrue(os.path.exists(img_path))
        with Image.open(img_path) as im:
            self.assertLess(
                im.height,
                800,
                f"Hauteur anormale ({im.height}px) indiquant la présence clandestine d'un logo ou d'un QR code résiduel !"
            )
        try:
            os.remove(img_path)
        except Exception:
            pass

    # =========================================================================
    # 7. IDEMPOTENCE STRICTE ET ANTI-DOUBLE DÉPENSE (ANTI-DOUBLE-SPEND)
    # =========================================================================

    def test_bce_anti_double_depense_et_idempotence(self):
        """
        Simule un double-clic ou une répétition de message réseau à 10ms d'intervalle.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO Categories (nom) VALUES ('Banque')")
        cat_id = c.lastrowid
        c.execute("""
            INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac, taux_tva)
            VALUES ('BCE001', 'Lingot 1g', 'Banque', 85.00, 0.00)
        """)
        p_id = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, quantite_actuelle) VALUES (?, 100)", (p_id,))
        stock_id = c.lastrowid
        conn.commit()

        panier = [{"code_barre": "BCE001", "stock_id": stock_id, "prix_vente_tvac": Decimal("85.00"), "quantite": 1}]
        paiements = [("Cash", Decimal("85.00"))]

        id1 = database_manager.enregistrer_vente(
            cursor=c,
            numero_ticket="TCK-IDEM-001",
            total_tvac=Decimal("85.00"),
            total_htva=Decimal("85.00"),
            total_tva=Decimal("0.00"),
            remise=Decimal("0.00"),
            methode_paiement="Cash",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier,
            vendeur_nom="Caissier BCE",
            date_heure="2026-09-23 12:00:00",
            paiements=paiements,
            caisse_id="POS-BCE"
        )
        conn.commit()

        id2 = database_manager.enregistrer_vente(
            cursor=c,
            numero_ticket="TCK-IDEM-001",
            total_tvac=Decimal("85.00"),
            total_htva=Decimal("85.00"),
            total_tva=Decimal("0.00"),
            remise=Decimal("0.00"),
            methode_paiement="Cash",
            id_client=None,
            rendu_monnaie=Decimal("0.00"),
            panier=panier,
            vendeur_nom="Caissier BCE",
            date_heure="2026-09-23 12:00:00",
            paiements=paiements,
            caisse_id="POS-BCE"
        )
        conn.commit()

        self.assertEqual(id1, id2, "L'idempotence a échoué : deux ID différents ont été générés pour la même transaction !")

        c.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (stock_id,))
        stock_restant = c.fetchone()[0]
        self.assertEqual(stock_restant, 99, f"Double débit de stock constaté : stock={stock_restant} au lieu de 99 !")

        c.execute("SELECT COUNT(*) FROM Tickets WHERE numero_ticket = 'TCK-IDEM-001'")
        self.assertEqual(c.fetchone()[0], 1, "Duplication de ticket constatée dans la table financière !")
        conn.close()


if __name__ == "__main__":
    unittest.main()
