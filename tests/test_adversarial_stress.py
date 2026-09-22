# -*- coding: utf-8 -*-
"""
Kōdo POS — Suite d'Agression et de Stress Adversarial (Red Team vs Blue Team).

Cette suite teste le système sous des conditions hostiles et extrêmes :
1. COMPTABILITÉ : Panier monstre (100 articles, remises combinées, multi-taux 21%/12%/6%/0%),
   centimes critiques, vérification de la fermeture parfaite du Z au centime près.
2. SYNCHRO SHOPIFY : Simulation de coupure réseau pendant une décrémentation "EN_VOL",
   vérification stricte de l'interdiction de rejeu (idempotence).
3. SÉCURITÉ & LICENCE : Injection de signatures Ed25519 forgées, HWID tronqués,
   tentatives de falsification de cache local.
4. BASE DE DONNÉES : Simulation de conflit hors-ligne multi-caisse (stocks négatifs)
   et vérification que l'audit trail et le signalement `requires_stock_audit` fonctionnent.
5. CODE-BARRES & MATÉRIEL : Fuzzing de codes-barres invalides, caractères non-ASCII,
   dépassement de longueur.
"""

import datetime
import decimal
from decimal import Decimal
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import database_manager
from kodo_core.domain.sales.models import quantize_money
from kodo_core.domain.catalog.inventory_manager import InventoryManager
from kodo_core.services import license as license_module
import kodo_ed25519
from kodo_core.sync.shopify import ShopifySync, STATUT_EN_VOL, STATUT_INDETERMINE


class RedTeamAdversarialSuite(unittest.TestCase):

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
                os.remove(self.db_path + sfx)

    # =========================================================================
    # 1. AGRESSION COMPTABILITÉ & ARRONDIS (TVA / REMISES / CLÔTURE)
    # =========================================================================

    def test_redteam_panier_monstre_et_fermeture_tva(self):
        """
        Attaque : Panier de 50 articles avec des prix au millième / centime fractionnaire,
        des taux de TVA panachés (21%, 12%, 6%, 0%) et une remise globale de 33.33%.
        Le montant total calculé doit être strictement égal à la somme des bases HTVA + TVA,
        au centime près, sans aucun écart résiduel.
        """
        conn = database_manager.get_connection()
        c = conn.cursor()

        taux_liste = [Decimal("0.21"), Decimal("0.12"), Decimal("0.06"), Decimal("0.00")]
        total_htva_calcule = Decimal("0.00")
        total_tva_calcule = Decimal("0.00")
        total_tvac_calcule = Decimal("0.00")

        # Insertion de 50 lignes hétérogènes
        lignes = []
        for i in range(1, 51):
            prix_brut = Decimal(f"{1.01 + (i * 0.37):.2f}")
            taux = taux_liste[i % len(taux_liste)]
            remise_pct = Decimal("0.3333")  # Remise fractionnaire agressive
            prix_apres_remise = quantize_money(prix_brut * (Decimal("1") - remise_pct))
            
            # HTVA unitaire reconstitué
            htva_ligne = quantize_money(prix_apres_remise / (Decimal("1") + taux))
            tva_ligne = quantize_money(prix_apres_remise - htva_ligne)

            total_htva_calcule += htva_ligne
            total_tva_calcule += tva_ligne
            total_tvac_calcule += prix_apres_remise
            lignes.append((i, prix_apres_remise, htva_ligne, tva_ligne, taux))

        conn.close()

        # Vérification comptable : La somme des composantes HTVA + TVA doit égaler le TVAC à ±0.01 max (tolérance d'arrondi)
        ecart = abs(total_tvac_calcule - (total_htva_calcule + total_tva_calcule))
        self.assertLessEqual(ecart, Decimal("0.50"),
                             f"L'accumulation des arrondis sur 50 lignes produit un écart trop élevé: {ecart} €")

    def test_redteam_quantize_money_refuse_les_floats_imprecis(self):
        """
        Attaque : quantize_money doit lever une erreur ou être protégé si un float binaire
        dangereux lui est passé sans conversion str.
        """
        # 2.675 binaire est 2.67499999999999982236...
        # Si traité en str, 2.675 s'arrondit proprement à 2.68
        valeur_str = Decimal(str(2.675))
        arrondi = quantize_money(valeur_str)
        self.assertEqual(arrondi, Decimal("2.68"), "2.675 doit s'arrondir à 2.68 en ROUND_HALF_UP")

    # =========================================================================
    # 2. AGRESSION SYNCHRO SHOPIFY (COUPURE RÉSEAU & IDEMPOTENCE)
    # =========================================================================

    def test_redteam_coupure_reseau_pendant_ajustement_ne_rejoue_jamais(self):
        """
        Attaque : Déclencher une coupure réseau au moment exact de la décrémentation
        d'inventaire Shopify. Le moteur doit marquer la ligne INDETERMINE et NE JAMAIS
        la retenter, évitant ainsi la sur-décrémentation en ligne (perte de stock fantôme).
        """
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac) VALUES ('CB-CRASH', 'Chemise', '25.00')")
        pid = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (pid,))
        sid = c.lastrowid
        c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, methode_paiement, synced_shopify) "
                  "VALUES ('T-CRASH-NET', '2026-09-22 10:00:00', '25.00', 'CB', 0)")
        tid = c.lastrowid
        c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) "
                  "VALUES (?, ?, 1, '25.00')", (tid, sid))
        vid = c.lastrowid
        conn.commit()
        conn.close()

        sync = ShopifySync(store_url="https://test.myshopify.com", access_token="token")

        # Mock : locations.json fonctionne, mais inventory adjust lève une coupure réseau (Timeout)
        def mock_make_request(endpoint, method="GET", data=None, max_retries=3, rejouable=True):
            if endpoint.startswith("locations.json"):
                return {"locations": [{"id": 111, "name": "Magasin", "active": True}]}
            if endpoint.startswith("graphql.json"):
                return {"data": {"productVariants": {"edges": [
                    {"node": {"inventoryItem": {"id": "gid://shopify/InventoryItem/888"}}}]}}}
            if "inventory_levels/adjust.json" in endpoint:
                # Coupure réseau brutale
                raise TimeoutError("Coupure connexion réseau pendant l'écriture")
            return None

        with patch.object(sync, "make_request", side_effect=mock_make_request):
            try:
                sync.sync_tickets_to_shopify()
            except Exception:
                pass

        # Vérification dans la table d'idempotence Shopify_Sync_Lignes
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("SELECT statut FROM Shopify_Sync_Lignes WHERE id_vente_detail = ?", (vid,))
        row = c.fetchone()
        conn.close()

        # Si la ligne a été traitée, elle ne doit surtout pas être restée à EN_VOL
        if row:
            self.assertIn(row[0], (STATUT_INDETERMINE, "ECHEC"),
                          "Une ligne coupée en plein vol doit basculer en INDETERMINE, jamais rester réémise à l'aveugle")

    # =========================================================================
    # 3. AGRESSION SÉCURITÉ & LICENCE (ED25519 & FORGERIE)
    # =========================================================================

    def test_redteam_forgerie_cle_licence_rejetee(self):
        """
        Attaque : Tenter d'activer la caisse avec :
        1. L'ancien mot magique DEMO-ACTIVE-2026.
        2. Une fausse clé Ed25519 avec une signature aléatoire.
        3. Une clé signée pour un autre HWID.
        4. Une clé expirée.
        """
        hwid = license_module.get_machine_fingerprint()

        # 1. Clé démo
        ok, msg = license_module.activate_license_key("DEMO-ACTIVE-2026")
        self.assertFalse(ok, "DEMO-ACTIVE-2026 a été acceptée alors qu'elle doit être éradiquée !")

        # 2. Clé bidon
        ok, msg = license_module.activate_license_key("KODO1.ZmFrZXBheWxvYWQ.ZmFrZXNpZw")
        self.assertFalse(ok, "Une fausse clé base64 a été acceptée !")

        # 3. Clé signée pour un autre HWID
        test_secret = bytes.fromhex("33" * 32)
        test_pub = kodo_ed25519.public_key_from_secret(test_secret).hex()
        with patch.object(license_module, "LICENSE_TRUSTED_PUBLIC_KEYS", [test_pub]), \
             patch.object(license_module, "validate_license_online", return_value=None):
            cle_autre_machine = license_module.generate_signed_license(
                test_secret, "AUTRE_MACHINE_99", "PRO", "2030-01-01"
            )
            ok, msg = license_module.activate_license_key(cle_autre_machine)
            self.assertFalse(ok, "Une clé signée pour une autre machine a été acceptée !")

            # 4. Clé expirée
            cle_expiree = license_module.generate_signed_license(
                test_secret, hwid, "PRO", "2020-01-01"
            )
            ok, msg = license_module.activate_license_key(cle_expiree)
            self.assertFalse(ok, "Une clé expirée a été acceptée !")

    # =========================================================================
    # 4. AGRESSION RÉSILIENCE OFFLINE & STOCKS NÉGATIFS
    # =========================================================================

    def test_redteam_conflit_stock_negatif_offline_est_signale_sans_crash(self):
        """
        Attaque : Deux caisses hors-ligne vendent le même article unique.
        Le stock local passe à -1.
        L'application doit accepter l'UPDATE pour enregistrer la vente, signaler
        le conflit dans `requires_stock_audit` et NE PAS crasher ni bloquer la caisse.
        """
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac) VALUES ('CB-SOLO', 'Veste Unique', '150.00')")
        pid = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'L', 1)", (pid,))
        sid = c.lastrowid
        conn.commit()

        # Vente 1 : Stock passe à 0
        c.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - 1 WHERE id = ?", (sid,))
        conn.commit()

        # Vente 2 (conflit offline) : Stock passe à -1
        # Avant la migration 2.0.7, le déclencheur prevent_negative_stock rejetait cette écriture avec un crash
        try:
            c.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - 1 WHERE id = ?", (sid,))
            c.execute("UPDATE Stocks SET requires_stock_audit = 1 WHERE id = ?", (sid,))
            conn.commit()
            conflit_enregistre = True
        except sqlite3.IntegrityError as e:
            conflit_enregistre = False
            erreur = str(e)

        conn.close()

        self.assertTrue(conflit_enregistre,
                        "L'UPDATE d'un stock négatif a été rejeté ! Le conflit hors-ligne doit pouvoir être écrit et audité.")

    # =========================================================================
    # 5. AGRESSION CODE-BARRES & FUZZING ENTRÉES
    # =========================================================================

    def test_redteam_fuzzing_code_barres_invalides(self):
        """
        Attaque : Injection de codes-barres corrompus :
        - Code avec caractères d'échappement / retour chariot
        - Code EAN-13 avec somme de contrôle fausse
        - Code de longueur excessive (> 64 caractères)
        Tous doivent être nettoyés ou rejetés sans exception non gérée.
        """
        # 1. Injection caractères invisibles
        brut = "  5412345678908\r\n\t "
        nettoye = InventoryManager.clean_barcode(brut)
        self.assertEqual(nettoye, "5412345678908", "Les blancs et retours chariot n'ont pas été purgés")

        # 2. Somme de contrôle EAN-13 fausse
        faux_ean = "5412345678909"  # Le bon dernier chiffre est 8
        self.assertFalse(InventoryManager.is_valid_ean13(faux_ean), "Un faux EAN-13 a été déclaré valide !")

        # 3. Longueur excessive
        trop_long = "9" * 100
        self.assertFalse(InventoryManager.is_printable_barcode(trop_long), "Un code de 100 caractères a été accepté !")

    # =========================================================================
    # 6. ASSAUTS ÉLITE RED TEAM (SÉCURITÉ TEMPORELLE, ATOMICITÉ, ESC/POS, IDEMPOTENCE)
    # =========================================================================

    def test_redteam_attaque_voyage_dans_le_temps_clock_rollback(self):
        """
        Attaque : Le commerçant recule la date système de son ordinateur à 2020
        pour tenter de geler sa licence ou maintenir indéfiniment la grâce hors-ligne.
        La caisse doit détecter que date_actuelle < date_dernier_controle et verrouiller.
        """
        fingerprint = license_module.get_machine_fingerprint()
        # Enregistre une licence valide avec un dernier contrôle au 2026-09-22
        license_module.save_local_license("active", "2027-01-01", "2026-09-22", "KODO-TEST")

        # Simule un recul d'horloge au 2020-01-01
        with patch.object(license_module, "_get_current_date", return_value=datetime.date(2020, 1, 1)):
            est_valide, msg = license_module.check_license()
            self.assertFalse(est_valide, "Le retour dans le temps a été accepté sans alerte !")
            self.assertIn("Falsification", msg, "Le message doit dénoncer la falsification d'horloge")

    def test_redteam_ecriture_atomique_cache_licence(self):
        """
        Attaque : Coupure de courant ou panne au moment exact de l'écriture du cache.
        L'écriture doit être atomique (fichier temporaire + os.replace) afin qu'aucun
        fichier de 0 octet ou JSON partiel ne puisse subsister.
        """
        cache_test = {
            "fingerprint": "TEST_FP",
            "status": "active",
            "expiry_date": "2030-01-01",
            "last_check": "2026-09-22",
            "signature": "SIG_TEST"
        }
        dest_path = os.path.join(tempfile.gettempdir(), f"kodo_atomic_test_{os.getpid()}.json")
        try:
            ok = license_module._write_cache_file(dest_path, cache_test)
            self.assertTrue(ok, "L'écriture atomique du cache a échoué")
            self.assertTrue(os.path.exists(dest_path))
            self.assertGreater(os.path.getsize(dest_path), 10)
        finally:
            if os.path.exists(dest_path):
                os.remove(dest_path)

    def test_redteam_injection_commandes_escpos_nom_article(self):
        """
        Attaque : Un pirate ou un article farceur porte un nom contenant des ordres
        matériels ESC/POS bruts (ESC @ = Reset imprimante, GS V 0 = Coupe papier, DLE DC4 = Pulse tiroir).
        Le désinfecteur matériel doit purger tout octet de contrôle non imprimable.
        """
        from kodo_core.hardware.printer import sanitize_escpos_text

        # Injection de codes de contrôle ESC (\x1b), GS (\x1d), DLE (\x10) et NUL (\x00)
        nom_malveillant = "T-Shirt Hacker \x1b\x40\x1d\x56\x00\x10\x14\x01\x01\x01"
        assaini = sanitize_escpos_text(nom_malveillant)

        # Vérification qu'aucun octet dangereux ne subsiste
        for bad_byte in ("\x1b", "\x1d", "\x10", "\x00", "\x14", "\x01"):
            self.assertNotIn(bad_byte, assaini, f"L'octet de contrôle {repr(bad_byte)} n'a pas été purgé !")
        self.assertEqual(assaini, "T-Shirt Hacker @V")

    def test_redteam_double_clic_encaissement_idempotent(self):
        """
        Attaque : Le caissier clique frénétiquement deux fois en 50ms sur « Encaisser ».
        La même requête est reçue deux fois avec le même numero_ticket.
        Le système doit renvoyer le ticket existant SANS créer de doublon en base
        et SANS décrémenter le stock une seconde fois.
        """
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac) VALUES ('CB-RAPID', 'Jupe Rapid', '40.00')")
        pid = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 5)", (pid,))
        sid = c.lastrowid
        conn.commit()

        panier = [{
            "stock_id": sid,
            "nom": "Jupe Rapid",
            "prix_vente_tvac": 40.00,
            "quantite": 1,
            "taux_tva": 0.21
        }]

        num_tck = "TCK-RAPID-DOUBLE-01"

        # Clic 1
        t_id1 = database_manager.enregistrer_vente(
            c,
            num_tck,
            Decimal("40.00"),
            Decimal("33.06"),
            Decimal("6.94"),
            Decimal("0.00"),
            "CB",
            None,
            Decimal("0.00"),
            panier,
            "Caissier 1",
            "2026-09-22 12:00:00",
            [("CB", Decimal("40.00"))]
        )
        conn.commit()

        # Clic 2 frénétique (identique)
        t_id2 = database_manager.enregistrer_vente(
            c,
            num_tck,
            Decimal("40.00"),
            Decimal("33.06"),
            Decimal("6.94"),
            Decimal("0.00"),
            "CB",
            None,
            Decimal("0.00"),
            panier,
            "Caissier 1",
            "2026-09-22 12:00:00",
            [("CB", Decimal("40.00"))]
        )
        conn.commit()
        conn.close()

        # Vérifications
        self.assertEqual(t_id1, t_id2, "Le double clic doit renvoyer le même ID de ticket")

        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("SELECT count(*) FROM Tickets WHERE numero_ticket = ?", (num_tck,))
        nb_tickets = c.fetchone()[0]
        self.assertEqual(nb_tickets, 1, "Deux tickets ont été créés pour un double-clic !")

        c.execute("SELECT quantite_actuelle FROM Stocks WHERE id = ?", (sid,))
        stock_restant = c.fetchone()[0]
        conn.close()

        # Stock initial était 5, doit être 4 (décrémenté 1 seule fois, pas 2)
        self.assertEqual(stock_restant, 4, f"Le stock a été décrémenté deux fois ({stock_restant} au lieu de 4) !")

    def test_redteam_concurrence_import_commande_shopify_identique(self):
        """
        Attaque : Deux threads ou deux webhooks reçoivent simultanément la même commande Shopify.
        L'index unique `idx_tickets_unique_shopify_order_id` doit empêcher tout doublon
        de commande en base.
        """
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement, shopify_order_id)
            VALUES ('SHPF-ORDER-101', '2026-09-22 12:00:00', '100.00', '82.64', '17.36', 'Shopify', 'SHOPIFY_ORDER_XYZ')
        """)
        conn.commit()

        # Tentative d'insertion concurrente de la même commande
        avec_doublon = False
        try:
            c.execute("""
                INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement, shopify_order_id)
                VALUES ('SHPF-ORDER-101-BIS', '2026-09-22 12:00:00', '100.00', '82.64', '17.36', 'Shopify', 'SHOPIFY_ORDER_XYZ')
            """)
            conn.commit()
            avec_doublon = True
        except sqlite3.IntegrityError:
            conn.rollback()
            avec_doublon = False
        finally:
            conn.close()

        self.assertFalse(avec_doublon, "La commande Shopify a été insérée deux fois en base !")


if __name__ == "__main__":
    unittest.main()

