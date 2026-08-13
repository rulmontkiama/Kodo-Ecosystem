import sqlite3
from database_manager import get_connection, hash_pin

def reset_database():
    print("[RESET] Démarrage de la remise à zéro de la base de données...")
    conn = get_connection()
    c = conn.cursor()
    
    try:
        # 1. Vider toutes les tables de données utilisateur
        tables = [
            "Ventes_Details",
            "Tickets",
            "Stocks",
            "Produits",
            "Clients",
            "Depenses_Caisse",
            "Ledger_Caisse",
            "Sessions_Caisse",
            "Rapports_Z",
            "Vendeurs",
            "Parametres"
        ]
        
        for table in tables:
            c.execute(f"DELETE FROM {table}")
            print(f"   - Table '{table}' vidée.")
            
        # 2. Réinitialiser les compteurs d'auto-incrémentation
        try:
            c.execute("DELETE FROM sqlite_sequence")
            print("   - Auto-incréments SQLite réinitialisés.")
        except Exception as e:
            print(f"   (Info auto-incréments : {e})")
            
        # 3. Réinsérer les paramètres d'usine par défaut
        c.execute("INSERT INTO Parametres (cle, valeur) VALUES ('pin_admin', ?)", (hash_pin('0000'),))
        c.execute("INSERT INTO Parametres (cle, valeur) VALUES ('shop_name', 'L''ADRESSE B')")
        c.execute("INSERT INTO Parametres (cle, valeur) VALUES ('shop_subtitle', 'Boutique de Mode')")
        c.execute("INSERT INTO Parametres (cle, valeur) VALUES ('shop_address', 'Chemin Rue 53, 4960 Malmedy')")
        c.execute("INSERT INTO Parametres (cle, valeur) VALUES ('shop_vat', 'BE 1035.331.577')")
        c.execute("INSERT INTO Parametres (cle, valeur) VALUES ('default_tva', '0.21')")
        print("   - Paramètres par défaut insérés ('pin_admin': '0000' haché, 'shop_name': 'L''ADRESSE B').")
        
        # 4. Réinsérer le Vendeur Administrateur d'usine
        c.execute("INSERT INTO Vendeurs (nom, pin, role_admin) VALUES ('Administrateur', ?, 1)", (hash_pin('0000'),))
        print("   - Vendeur 'Administrateur' par défaut réinséré (PIN: 0000 haché).")
        
        conn.commit()
        print("[OK] Remise à zéro de la base de données effectuée avec succès ! Prêt pour le déploiement.")
        
    except Exception as e:
        conn.rollback()
        print(f"[ERREUR] Erreur lors de la remise à zéro de la base de données : {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    reset_database()
