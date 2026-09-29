# -*- coding: utf-8 -*-
"""
Golden Contracts — Mission Roi de Belgique (Kōdo POS)
Sanctuarisation anti-régression des invariants fiscaux, matériels et d'audit.
Règle d'or : « 1 bug = 1 test qui échoue avant et passe après ».
"""

import os
import csv
import sqlite3
import pytest
from decimal import Decimal
from unittest.mock import patch

import database_manager
from kodo_core.config import ShopConfig
from export_manager import export_comptable_belge
from kodo_core.hardware.printer import imprimer_ticket_test
from kodo_core.db.audit_trail import verifier_chainage
from kodo_core.domain.live.live_manager import LiveManager


@pytest.fixture
def clean_db(tmp_path):
    """Initialise une base de test isolée."""
    db_file = str(tmp_path / "kodo_test_golden.db")
    orig_db = database_manager.DB_NAME
    orig_env = os.environ.get("KODO_DB_PATH")
    
    database_manager.DB_NAME = db_file
    os.environ["KODO_DB_PATH"] = db_file
    
    database_manager.initialiser_db()
    conn = database_manager.get_connection()
    LiveManager.init_tables(conn=conn)
    conn.close()
    
    yield db_file
    
    database_manager.DB_NAME = orig_db
    if orig_env:
        os.environ["KODO_DB_PATH"] = orig_env
    else:
        os.environ.pop("KODO_DB_PATH", None)


def test_golden_export_comptable_quantity(clean_db):
    """
    GOLDEN CONTRACT 1 : L'export comptable officiel belge (CSV) doit obligatoirement
    multiplier le prix unitaire par la quantité d'articles vendus.
    Scénario : Achat de 4 robes à 50.00 € TVAC = 200.00 € TVAC.
    """
    conn = database_manager.get_connection()
    c = conn.cursor()
    
    # 1. Créer produit et stock
    c.execute("INSERT INTO Produits (nom, code_barre, prix_vente_tvac, taux_tva) VALUES ('Robe Soie', 'ROBE-001', 50.00, 0.21)")
    p_id = c.lastrowid
    c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 10)", (p_id,))
    s_id = c.lastrowid
    
    # 2. Créer un ticket avec 4 articles à 50 € = 200 €
    c.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement)
        VALUES ('TK-GOLDEN-01', '2026-09-28 12:00:00', 200.00, 165.29, 34.71, 'Bancontact')
    """)
    t_id = c.lastrowid
    
    c.execute("""
        INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
        VALUES (?, ?, 4, 50.00)
    """, (t_id, s_id))
    conn.commit()
    conn.close()

    # 3. Exécuter l'export comptable belge
    csv_path = export_comptable_belge()
    assert os.path.exists(csv_path), "Le fichier d'export comptable doit exister."

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = list(csv.DictReader(f, delimiter=";"))
    
    # Trouver la ligne du ticket
    target = next((r for r in reader if r.get("Numéro Ticket") == "TK-GOLDEN-01"), None)
    assert target is not None, "Le ticket TK-GOLDEN-01 doit être présent dans l'export."
    
    montant_tvac_str = target.get("Montant TVAC (€)", "").replace(",", ".")
    montant_tvac = Decimal(montant_tvac_str)
    
    # Invariant contractuel : 4 * 50.00 = 200.00 (et NON 50.00 !)
    assert montant_tvac == Decimal("200.00"), f"Incohérence fiscale : attendu 200.00€ TVAC, obtenu {montant_tvac}€ TVAC (quantité omise ?)"


def test_golden_imprimer_ticket_test_detects_hardware_failure():
    """
    GOLDEN CONTRACT 2 : L'outil de test imprimante (imprimer_ticket_test)
    doit renvoyer success=False si l'imprimante matérielle ne répond pas.
    """
    with patch("kodo_core.hardware.printer.ESCPOSThermalPrinter.send_raw", return_value=False):
        result = imprimer_ticket_test(host="192.168.1.999", port=9100)
        assert result.get("success") is False, (
            "Défaillance Circuit Breaker : imprimer_ticket_test a renvoyé success=True "
            "alors que l'envoi matériel a échoué (imprimante débranchée/injoignable)."
        )


def test_golden_audit_genesis_tamper_detection(clean_db):
    """
    GOLDEN CONTRACT 3 : L'audit trail NF525 (verifier_chainage) doit obligatoirement
    vérifier que le premier enregistrement de la chaîne est ancré sur le bloc de genèse
    GENESIS_BLOCK_KODO_POS. Si un attaquant tronque le début de la base, la rupture
    doit être signalée (renvoie False).
    """
    conn = database_manager.get_connection()
    c = conn.cursor()
    # Supprimer les triggers pour simuler une manipulation frauduleuse directe en base
    c.execute("DROP TRIGGER IF EXISTS prevent_ticket_tamper_update")
    c.execute("DROP TRIGGER IF EXISTS prevent_ticket_tamper_delete")
    
    # Insérer un ticket avec un hash précédent arbitraire (simulation de tickets antérieurs effacés)
    c.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, signature, current_hash, hash_precedent, previous_hash)
        VALUES ('TK-TAMPERED-01', '2026-09-28 10:00:00', 10.00, 'sig1', 'hash1', 'FAKE_PREV_HASH_AFTER_TRUNCATE', 'FAKE_PREV_HASH_AFTER_TRUNCATE')
    """)
    conn.commit()

    # verifier_chainage doit détecter que la chaîne ne démarre pas sur le GENESIS_BLOCK_KODO_POS
    intègre = verifier_chainage("Tickets", conn=conn)
    conn.close()
    
    assert intègre is False, (
        "Faille de genèse NF525 : verifier_chainage a validé une chaîne de tickets "
        "dont le bloc initial ne référence pas GENESIS_BLOCK_KODO_POS."
    )


def test_golden_live_shopping_preserves_reduced_vat(clean_db):
    """
    GOLDEN CONTRACT 4 : L'encaissement d'une réservation Live Shopping (checkout_claim)
    doit respecter le taux de TVA propre au produit (ex: 6% pour les livres/aliments en Belgique)
    et non forcer aveuglément 21%.
    """
    conn = database_manager.get_connection()
    c = conn.cursor()
    
    # 1. Produit à taux réduit 6%
    c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Livre Art Belge', 20.00, 0.06)")
    p_id = c.lastrowid
    c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'Unique', 5)", (p_id,))
    s_id = c.lastrowid
    
    # 2. Session live et acheteur
    c.execute("INSERT INTO Live_Sessions (titre, statut) VALUES ('Live Spécial Livres', 'active')")
    session_id = c.lastrowid
    c.execute("INSERT INTO Live_Buyers (nom, prenom, telephone) VALUES ('Vandamme', 'Jean', '0470123456')")
    buyer_id = c.lastrowid
    
    # 3. Claim attribué
    c.execute("""
        INSERT INTO Live_Claims (session_id, buyer_id, product_id, stock_id, article_nom, taille, prix_unitaire_tvac, quantite, statut_attribution, statut_paiement)
        VALUES (?, ?, ?, ?, 'Livre Art Belge', 'Unique', 20.00, 1, 'attribué', 'non_paye')
    """, (session_id, buyer_id, p_id, s_id))
    claim_id = c.lastrowid
    conn.commit()
    conn.close()
    
    # 4. Encaisser la claim via LiveManager
    res = LiveManager.checkout_claim(claim_id, {"paymentMethod": "Bancontact", "cashierName": "Admin"})
    assert res.get("success") is True, f"Erreur encaissement live: {res.get('error')}"
    
    # 5. Vérifier la TVA enregistrée en base
    ticket_id = res["ticket_id"]
    conn = database_manager.get_connection()
    c = conn.cursor()
    c.execute("SELECT total_tvac, total_htva, total_tva FROM Tickets WHERE id=?", (ticket_id,))
    tvac, htva, tva = c.fetchone()
    conn.close()
    
    # À 6% : Total 20.00 => HTVA = 20 / 1.06 = 18.87, TVA = 1.13
    # Si le bug forçait 21% : HTVA = 20 / 1.21 = 16.53, TVA = 3.47
    tva_d = Decimal(str(tva))
    assert tva_d == Decimal("1.13"), (
        f"Erreur TVA Live Shopping : la TVA enregistrée est de {tva_d} € au lieu de 1.13 € (6%). "
        "Le taux de TVA du produit a été écrasé par un 21% codé en dur !"
    )


def test_golden_build_script_verifies_tickets_table():
    """
    GOLDEN CONTRACT 5 : Le script de build usine build_final_pro.sh doit obligatoirement
    vérifier l'absence de données dans la table 'tickets' (et non seulement 'ventes').
    """
    build_script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "build_final_pro.sh")
    with open(build_script_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Doit contenir 'tickets' dans la liste user_tables
    assert "'tickets'" in content or '"tickets"' in content, (
        "Faille de packaging usine : 'tickets' est absent de user_tables dans build_final_pro.sh. "
        "Des tickets de vente de test pourraient être livrés dans le DMG final !"
    )


def test_golden_backup_preserves_hmac_audit_key(clean_db, tmp_path):
    """
    GOLDEN CONTRACT 6 : Le gestionnaire de sauvegarde (backup_manager.py)
    doit impérativement inclure la clé secrète HMAC d'audit (audit_hmac.key)
    dans les archives créées, et la restaurer fidèlement sur la machine cible.
    """
    import zipfile
    from backup_manager import creer_pack_migration_machine, restaurer_pack_migration
    
    # 1. Configurer un dossier de clé secret temporaire
    fake_key_dir = str(tmp_path / ".kodo_signing")
    os.makedirs(fake_key_dir, exist_ok=True)
    fake_key_file = os.path.join(fake_key_dir, "audit_hmac.key")
    fake_secret = b"ROYAL_BELGIUM_HMAC_SECRET_2026_TEST"
    with open(fake_key_file, "wb") as f:
        f.write(fake_secret)
        
    with patch("backup_manager.DB_NAME", clean_db), \
         patch("backup_manager._get_audit_key_path", return_value=fake_key_file):
        # 2. Créer un pack de migration
        ok, zip_name, zip_bytes, manifest = creer_pack_migration_machine()
        assert ok is True, f"Échec création pack migration: {manifest}"
        
        # 3. Vérifier que audit_hmac.key est dans le zip
        import io
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            assert "audit_hmac.key" in zf.namelist(), (
                "Régression NF525 : audit_hmac.key n'est pas présente dans l'archive de sauvegarde !"
            )
            assert zf.read("audit_hmac.key") == fake_secret

