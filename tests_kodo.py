import sys
import os
import datetime
from decimal import Decimal

import json

# Override DB_NAME to run tests on a separate test database
import database_manager
database_manager.DB_NAME = "test_ladresse_b.db"

# Now import the modules
from database_manager import (
    get_connection, initialiser_db, generer_numero_ticket,
    enregistrer_vente, enregistrer_remboursement
)
import export_manager
import ticket_printer
import audit_trail

def run_tests():
    print("=== DÉBUT DES TESTS AUTOMATISÉS ===")
    
    # 1. Initialisation de la base de test
    initialiser_db()
    conn = get_connection()
    c = conn.cursor()
    
    # Nettoyer la base de test
    c.execute("DELETE FROM Ventes_Details")
    c.execute("DELETE FROM Tickets")
    c.execute("DELETE FROM Ledger_Caisse")
    c.execute("DELETE FROM Stocks")
    c.execute("DELETE FROM Produits")
    c.execute("DELETE FROM Categories")
    conn.commit()
    print("✅ Base de données nettoyée pour les tests.")
    
    # 2. Test de création de catégorie et produit
    c.execute("INSERT INTO Categories (nom) VALUES (?)", ("Vêtements",))
    c.execute("""
        INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, taux_tva)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("1234567890123", "T-Shirt Test", "Vêtements", 10.0, 24.20, 0.21))
    pid = c.lastrowid
    
    c.execute("""
        INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte)
        VALUES (?, ?, ?, ?)
    """, (pid, "M", 10, 2))
    sid = c.lastrowid
    conn.commit()
    print("✅ Catégorie, Produit et Stock initialisés.")
    
    # 3. Simulation du panier et enregistrement d'une vente
    panier = [{
        "stock_id": sid,
        "code_barre": "1234567890123",
        "nom": "T-Shirt Test",
        "prix_vente_tvac": Decimal("24.20"),
        "taux_tva": Decimal("0.21")
    }]
    
    num_ticket = generer_numero_ticket(c)
    net = Decimal("24.20")
    htva = Decimal("20.00")
    tva = Decimal("4.20")
    remise = Decimal("0.00")
    
    enregistrer_vente(
        c, num_ticket, net, htva, tva, remise, "Bancontact", 
        None, Decimal("0.00"), panier, "Vendeur 1", 
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        [("Bancontact", Decimal("24.20"))]
    )
    conn.commit()
    print("✅ Vente enregistrée.")
    
    # Vérification du stock déduit
    c.execute("SELECT quantite_actuelle FROM Stocks WHERE id=?", (sid,))
    stock_restant = c.fetchone()[0]
    assert stock_restant == 9, f"Le stock devrait être 9, obtenu: {stock_restant}"
    print("✅ Stock mis à jour correctement (-1).")
    
    # 4. Impression virtuelle (génération de ticket)
    contenu = ticket_printer.generer_ticket(
        numero=num_ticket, panier=panier, total_tvac=net, remise=remise,
        paiements=[("Bancontact", Decimal("24.20"))], rendu_monnaie=Decimal("0.00"),
        shop_name="L'ADRESSE B", vendeur_nom="Vendeur 1"
    )
    assert "T-Shirt Test" in contenu
    assert "carte" in contenu.lower() or "bancontact" in contenu.lower()
    print("✅ Ticket généré et formaté avec succès.")
    
    # 5. Remboursement d'un article
    # On récupère le detail id
    c.execute("SELECT id FROM Ventes_Details WHERE id_ticket = (SELECT id FROM Tickets WHERE numero_ticket=?)", (num_ticket,))
    vd_id = c.fetchone()[0]
    
    date_heure_remb = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_tk = enregistrer_remboursement(c, num_ticket, vd_id, sid, Decimal("24.20"), "Bancontact", "Vendeur 1", date_heure_remb)
    conn.commit()
    print(f"✅ Remboursement enregistré : {new_tk}")
    
    # Vérification du stock recrédité
    c.execute("SELECT quantite_actuelle FROM Stocks WHERE id=?", (sid,))
    stock_restant = c.fetchone()[0]
    assert stock_restant == 10, f"Le stock devrait être 10 après remboursement, obtenu: {stock_restant}"
    print("✅ Stock recrédité correctement (+1).")
    
    # 6. Vérification du Z Virtuel
    today_str = datetime.date.today().isoformat()
    z_virtuel = export_manager.calculer_rapport_z_virtuel(today_str, c)
    assert z_virtuel is not None, "Le Z virtuel ne devrait pas être None"
    # 24.20 vente - 24.20 retour = 0.00
    assert abs(z_virtuel["financier"]["ca_ttc"]) < 0.001, f"Le CA total devrait être 0.0 après remboursement complet, obtenu: {z_virtuel['financier']['ca_ttc']}"
    print("✅ Calcul du Z Virtuel correct.")
    # 6b. Test de la synchronisation Shopify (Mocks)
    print("\n--- Test de Synchronisation Shopify ---")
    from shopify_sync import ShopifySyncThread
    
    # Configuration Shopify fictive en base
    c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_store_url', 'test-boutique.myshopify.com')")
    c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_access_token', 'shpat_testtoken123')")
    conn.commit()
    
    sync_thread = ShopifySyncThread()
    
    # Mocking self._make_request of ShopifySyncThread
    calls = []
    def mock_make_request(endpoint, method="GET", data=None):
        calls.append((endpoint, method, data))
        if "locations.json" in endpoint:
            return {"locations": [{"id": 112233, "name": "Boutique Test", "active": True}]}
        elif "graphql.json" in endpoint:
            return {
                "data": {
                    "productVariants": {
                        "edges": [
                            {
                                "node": {
                                    "inventoryItem": {
                                        "id": "gid://shopify/InventoryItem/998877"
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        elif "inventory_items.json" in endpoint:
            return {"inventory_items": [{"id": 998877, "sku": "1234567890123"}]}
        elif "inventory_levels/adjust.json" in endpoint:
            return {"inventory_level": {}}
        elif "orders.json" in endpoint:
            return {"orders": [
                {
                    "id": 99999,
                    "order_number": 1001,
                    "total_price": "48.40",
                    "total_tax": "8.40",
                    "line_items": [
                        {
                            "sku": "1234567890123",
                            "title": "T-Shirt Test",
                            "quantity": 2
                        }
                    ]
                }
            ]}
        return None
        
    sync_thread._make_request = mock_make_request
    
    # A. Test Push : Vente locale vers Shopify
    c.execute("UPDATE Tickets SET synced_shopify = 0")
    conn.commit()
    
    sync_thread._sync_tickets_to_shopify()
    
    # Vérifier que les tickets ont été marqués comme synchronisés
    c.execute("SELECT COUNT(*) FROM Tickets WHERE synced_shopify = 0")
    unsynced_count = c.fetchone()[0]
    assert unsynced_count == 0, f"Tous les tickets devraient être synchronisés, restant: {unsynced_count}"
    print("✅ Push (POS -> Shopify) : Ventes synchronisées et stock ajusté sur Shopify.")
    
    # B. Test Pull : Commande Shopify vers POS
    # Stock actuel est de 10. La commande Shopify demande 2 articles.
    sync_thread._sync_orders_from_shopify()
    
    # Vérifier le stock déduit en local
    c.execute("SELECT quantite_actuelle FROM Stocks WHERE id=?", (sid,))
    stock_restant = c.fetchone()[0]
    assert stock_restant == 8, f"Le stock local devrait être de 8 après déduction de 2 articles, obtenu: {stock_restant}"
    print("✅ Pull (Shopify -> POS) : Commande importée et stock local déduit (-2).")
    
    # Vérifier que le ticket de la commande a été créé et signé
    c.execute("SELECT total_tvac, methode_paiement, shopify_order_id FROM Tickets WHERE shopify_order_id = '99999'")
    order_row = c.fetchone()
    assert order_row is not None, "Le ticket Shopify n'a pas été créé"
    assert float(order_row[0]) == 48.40, f"Le total du ticket devrait être 48.40, obtenu: {order_row[0]}"
    print("✅ Pull (Shopify -> POS) : Ticket de caisse conforme créé pour la commande.")

    # 7. Audit Trail NF525
    print("\n--- Validation Cryptographique (Audit Trail) ---")
    
    def hash_ticket(r):
        from database_manager import calculer_hash_transaction
        return calculer_hash_transaction(r['hash_precedent'], r['date_heure'], r['total_tvac'], r.get('caisse_id', 'POS-01'), r.get('details_articles', ''))
        
    def hash_ledger(r):
        dt = r['date_heure']
        dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
        montant_str = f"{Decimal(str(r['montant'])):.2f}"
        data = f"{r['hash_precedent']}|{r['type_mouvement']}|{montant_str}|{r['methode_paiement']}|{r['reference']}|{dt_str}"
        import hashlib
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
        
    def hash_z(r):
        data = f"{r['hash_precedent']}|{r['date']}|{r['donnees_json']}"
        import hashlib
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    t = audit_trail.verifier_chainage("Tickets", compute_hash_func=hash_ticket)
    l = audit_trail.verifier_chainage("Ledger_Caisse", compute_hash_func=hash_ledger)
    z = audit_trail.verifier_chainage("Rapports_Z", compute_hash_func=hash_z)
    
    assert t and l and z, "La validation cryptographique de l'audit trail a échoué"
    print("✅ Audit Trail cryptographique validé avec succès.")
    
    # 8. Test du hachage de PIN et de la migration automatique
    print("\n--- Test du Hachage de PIN et Migration ---")
    from database_manager import hash_pin
    # Vérification que hash_pin donne un résultat attendu
    h0 = hash_pin("0000")
    assert len(h0) == 64, f"Le hash devrait faire 64 caractères, obtenu: {len(h0)}"
    assert h0 == hash_pin("0000"), "Le hachage doit être déterministe"
    assert h0 != hash_pin("1234"), "Deux PINs différents doivent donner des hashs différents"
    
    # Test de la migration automatique des PINs en clair
    c.execute("INSERT INTO Vendeurs (nom, pin, role_admin) VALUES (?, ?, ?)", ("Vendeur Test Mig", "1234", 0))
    conn.commit()
    
    # Appeler initialiser_db() à nouveau pour déclencher la migration
    initialiser_db()
    
    # Vérifier que le PIN de "Vendeur Test Mig" a été migré en hash
    c.execute("SELECT pin FROM Vendeurs WHERE nom = ?", ("Vendeur Test Mig",))
    migrated_pin = c.fetchone()[0]
    assert migrated_pin == hash_pin("1234"), f"Le PIN aurait dû être migré vers le hash de '1234', obtenu: {migrated_pin}"
    # 9. Test du système de licence (license_manager)
    print("\n--- Test du Système de Licence (license_manager) ---")
    import license_manager
    fingerprint = license_manager.get_machine_fingerprint()
    assert len(fingerprint) == 16, f"L'empreinte devrait faire 16 caractères, obtenu: {len(fingerprint)}"
    
    # Tester l'écriture et le chargement d'une licence locale
    expiry_date = "2029-12-31"
    last_check = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    license_manager.save_local_license("active", expiry_date, last_check)
    
    cache = license_manager.load_local_license()
    assert cache is not None, "Le chargement de la licence locale a échoué"
    assert cache["status"] == "active", "Le statut de la licence chargée est incorrect"
    assert cache["expiry_date"] == expiry_date, "La date d'expiration chargée est incorrecte"
    
    # Tester la détection de falsification
    # On modifie le fichier de cache manuellement sans recalculer la signature
    from database_manager import data_path
    cache_path = data_path("license_cache.json")
    with open(cache_path, "r") as f:
        tampered_data = json.load(f)
    tampered_data["status"] = "active_free" # Modification frauduleuse
    with open(cache_path, "w") as f:
        json.dump(tampered_data, f)
        
    tampered_cache = license_manager.load_local_license()
    assert tampered_cache is None, "La détection de falsification du cache local a échoué (devrait renvoyer None)"
    
    # Tester la vérification globale de la licence (mode hors-ligne)
    # On ré-enregistre une licence valide
    license_manager.save_local_license("active", expiry_date, last_check)
    is_valid, msg = license_manager.check_license(key_path="non_existent_key.json") # force le mode hors-ligne
    assert is_valid, f"La validation de licence valide en mode hors-ligne a échoué : {msg}"
    
    # Tester une licence expirée
    license_manager.save_local_license("active", "2020-01-01", last_check)
    is_valid, msg = license_manager.check_license(key_path="non_existent_key.json")
    assert not is_valid, "La détection d'une licence expirée a échoué"
    assert "expiré" in msg, f"Le message devrait mentionner l'expiration, obtenu : {msg}"
    
    # Tester une licence suspendue
    license_manager.save_local_license("suspended", expiry_date, last_check)
    is_valid, msg = license_manager.check_license(key_path="non_existent_key.json")
    assert not is_valid, "La détection d'une licence suspendue a échoué"
    
    # Nettoyage du fichier cache de test
    if os.path.exists(cache_path):
        os.remove(cache_path)
    print("✅ Système de licence testé avec succès.")
    
    conn.close()
    
    # Nettoyage du fichier db de test
    for ext in ["", "-shm", "-wal"]:
        f = "test_ladresse_b.db" + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
        
    print("=== TOUS LES TESTS SONT PASSÉS AVEC SUCCÈS ===")

if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
