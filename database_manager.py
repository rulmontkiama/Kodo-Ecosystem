import sqlite3
from decimal import Decimal
import datetime
import os
import sys
import shutil

def resource_path(relative_path):
    """Obtient le chemin absolu vers une ressource intégrée via PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def data_path(relative_path):
    """Obtient le chemin absolu pour les données persistantes dans Documents (ou fallback local)"""
    try:
        from core.config import ShopConfig
        return os.path.join(ShopConfig.get_base_data_dir(), relative_path)
    except Exception:
        try:
            doc_dir = os.path.expanduser("~/Documents/Kodo_POS")
            os.makedirs(doc_dir, exist_ok=True)
            return os.path.join(doc_dir, relative_path)
        except Exception:
            lib_dir = os.path.expanduser("~/Library/Application Support/Kodo_POS")
            os.makedirs(lib_dir, exist_ok=True)
            return os.path.join(lib_dir, relative_path)

# Base de données persistante (Kōdo POS Core)
try:
    from core.config import ShopConfig
    DB_NAME = ShopConfig.get_db_path("kodo_pos.db")
except Exception:
    try:
        doc_dir = os.path.expanduser("~/Documents/Kodo_POS")
        os.makedirs(doc_dir, exist_ok=True)
        DB_NAME = os.path.join(doc_dir, "kodo_pos.db")
    except Exception:
        lib_dir = os.path.expanduser("~/Library/Application Support/Kodo_POS")
        os.makedirs(lib_dir, exist_ok=True)
        DB_NAME = os.path.join(lib_dir, "kodo_pos.db")

# Migration de compatibilité si ladresse_b.db existe et kodo_pos.db n'existe pas encore
legacy_db = os.path.expanduser("~/Documents/Kodo_POS/ladresse_b.db")
if not os.path.exists(DB_NAME) and os.path.exists(legacy_db):
    try:
        shutil.copy2(legacy_db, DB_NAME)
    except Exception:
        pass

# Copie de la base initiale embarquée vers Documents (premier lancement)
bundled_db = resource_path("kodo_pos.db")
if not os.path.exists(DB_NAME) and os.path.exists(bundled_db):
    try:
        shutil.copy2(bundled_db, DB_NAME)
    except Exception:
        pass

# Adaptateur et convertisseur pour utiliser Decimal avec SQLite
def adapt_decimal(d):
    return str(d)

def convert_decimal(s):
    return Decimal(s.decode('utf-8'))

sqlite3.register_adapter(Decimal, adapt_decimal)
sqlite3.register_converter("DECIMAL", convert_decimal)

def hash_pin(pin_plain):
    """Génère un hachage SHA-256 avec sel pour sécuriser les PINs."""
    if not pin_plain:
        return ""
    import hashlib
    salt = "KODO_POS_SECURE_SALT_2026"
    return hashlib.sha256((pin_plain + salt).encode('utf-8')).hexdigest()

from contextlib import contextmanager

class SafeConnection:
    """Wrapper ultra-sécurisé pour garantir la fermeture des connexions (Zéro Crash / Zéro Verrou)."""
    def __init__(self, db_name, **kwargs):
        self._conn = sqlite3.connect(db_name, **kwargs)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._closed = False
        
    def cursor(self):
        return self._conn.cursor()
        
    def commit(self):
        self._conn.commit()
        
    def rollback(self):
        self._conn.rollback()
        
    def close(self):
        if not self._closed:
            try:
                self._conn.close()
            except Exception:
                pass
            finally:
                self._closed = True
                
    def __del__(self):
        self.close()

def get_connection():
    return SafeConnection(DB_NAME, detect_types=sqlite3.PARSE_DECLTYPES)

@contextmanager
def db_transaction():
    conn = get_connection()
    try:
        yield conn.cursor()
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

@contextmanager
def db_query():
    conn = get_connection()
    try:
        yield conn.cursor()
    finally:
        conn.close()


def initialiser_db():
    try:
        from core.migrations import MigrationManager
        MigrationManager.run_migrations(DB_NAME)
    except Exception as me:
        print(f"⚠️ Avertissement Migration: {me}")

    conn = get_connection()
    try:
        _initialiser_db_raw(conn)
    finally:
        conn.close()

def _initialiser_db_raw(conn):
    cursor = conn.cursor()
    
    # Table Categories
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT UNIQUE NOT NULL
        )
    ''')

    # Table Parametres (Configuration globale)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Parametres (
            cle TEXT PRIMARY KEY,
            valeur TEXT
        )
    ''')

    # Table Marques
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Marques (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT UNIQUE NOT NULL
        )
    ''')

    # Table Clotures_Caisse (NF525 Z de Caisse)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Clotures_Caisse (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_cloture TEXT NOT NULL,
            caisse_id TEXT NOT NULL,
            total_ventes_tvac DECIMAL DEFAULT '0.00',
            total_htva DECIMAL DEFAULT '0.00',
            total_tva DECIMAL DEFAULT '0.00',
            total_especes DECIMAL DEFAULT '0.00',
            total_carte DECIMAL DEFAULT '0.00',
            total_remises DECIMAL DEFAULT '0.00',
            total_tickets INTEGER DEFAULT 0,
            fond_caisse_reel DECIMAL DEFAULT '0.00',
            ecart DECIMAL DEFAULT '0.00',
            vendeur TEXT,
            hash_precedent TEXT,
            current_hash TEXT,
            signature TEXT,
            created_at_utc TEXT
        )
    ''')

    # Table Produits
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Produits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_barre TEXT UNIQUE,
            nom TEXT NOT NULL,
            categorie TEXT,
            prix_achat_htva DECIMAL,
            prix_vente_tvac DECIMAL,
            taux_tva DECIMAL DEFAULT '0.21',
            image_path TEXT,
            en_solde INTEGER DEFAULT 0,
            prix_solde_tvac DECIMAL DEFAULT NULL
        )
    ''')
    
    # Migration automatique pour Categories & Marques
    cursor.execute("INSERT OR IGNORE INTO Categories (nom) SELECT DISTINCT categorie FROM Produits WHERE categorie IS NOT NULL AND categorie != ''")
    cursor.execute("INSERT OR IGNORE INTO Marques (nom) SELECT DISTINCT marque FROM Produits WHERE marque IS NOT NULL AND marque != ''")

    # Initialisation des catégories et marques par défaut uniquement lors de la création de la base
    cursor.execute("SELECT COUNT(*) FROM Parametres")
    param_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM Parametres WHERE cle='db_is_initialized'")
    is_initialized = cursor.fetchone()[0] > 0

    if not is_initialized and param_count > 0:
        # Il s'agit d'une ancienne base de données (mise à jour) qui a déjà été initialisée.
        is_initialized = True
        cursor.execute("INSERT INTO Parametres (cle, valeur) VALUES ('db_is_initialized', '1')")

    if not is_initialized:
        cursor.execute("SELECT COUNT(*) FROM Categories")
        if cursor.fetchone()[0] == 0:
            default_cats = ["T-Shirts & Tops", "Pantalons & Jeans", "Robes & Jupes", "Vestes & Manteaux", "Chaussures", "Accessoires", "Sacs", "Bijoux", "Lingerie", "Costumes & Tailleurs"]
            cursor.executemany("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", [(c,) for c in default_cats])

        cursor.execute("SELECT COUNT(*) FROM Marques")
        if cursor.fetchone()[0] == 0:
            default_marques = ["Hugo Boss", "Ralph Lauren", "Zara", "Nike", "Adidas", "Levi's", "Mango", "H&M", "Tommy Hilfiger", "Calvin Klein"]
            cursor.executemany("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", [(m,) for m in default_marques])
    
    # Migration automatique pour Produits
    cursor.execute("PRAGMA table_info(Produits)")
    cols_produits = [row[1] for row in cursor.fetchall()]
    if 'image_path' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN image_path TEXT")
        except: pass
    if 'en_solde' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN en_solde INTEGER DEFAULT 0")
        except: pass
    if 'prix_solde_tvac' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN prix_solde_tvac DECIMAL DEFAULT NULL")
        except: pass
    if 'type_vente' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN type_vente TEXT DEFAULT 'unite'")
        except: pass
    if 'unite_mesure' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN unite_mesure TEXT DEFAULT 'pce'")
        except: pass
    if 'marque' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN marque TEXT DEFAULT NULL")
        except: pass
    if 'attributs_json' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN attributs_json TEXT DEFAULT NULL")
        except: pass
    if 'sync_status' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN sync_status INTEGER DEFAULT 0")
        except: pass

    # Migration automatique pour Tickets
    cursor.execute("PRAGMA table_info(Tickets)")
    cols_tickets = [row[1] for row in cursor.fetchall()]
    if 'sync_status' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN sync_status INTEGER DEFAULT 0")
        except: pass

    # Table ShopInfo (Branding & Tenant Config)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ShopInfo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom_magasin TEXT NOT NULL,
            adresse TEXT,
            telephone TEXT,
            email TEXT,
            siret_tva TEXT,
            type_commerce TEXT DEFAULT 'pret_a_porter',
            devise TEXT DEFAULT '€',
            logo_path TEXT
        )
    ''')
    cursor.execute("SELECT COUNT(*) FROM ShopInfo")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO ShopInfo (nom_magasin, adresse, siret_tva, type_commerce, devise)
            VALUES (?, ?, ?, ?, ?)
        ''', ("L'Adresse B", "Boutique Pilote", "BE 0123.456.789", "pret_a_porter", "€"))

    # Table Stocks
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_produit INTEGER,
            taille TEXT,
            quantite_actuelle INTEGER,
            seuil_alerte INTEGER,
            FOREIGN KEY (id_produit) REFERENCES Produits(id) ON DELETE CASCADE
        )
    ''')
    
    # Table Clients
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            email TEXT UNIQUE,
            total_depense DECIMAL DEFAULT '0.00',
            points_fidelite INTEGER DEFAULT 0,
            taille_haut TEXT,
            taille_bas TEXT,
            pointure TEXT,
            pref_couleurs TEXT,
            date_anniversaire TEXT
        )
    ''')
    # Table Vendeurs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Vendeurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            pin TEXT UNIQUE NOT NULL,
            role_admin INTEGER DEFAULT 0
        )
    ''')

    # Table Sessions_Caisse
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Sessions_Caisse (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_ouverture TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fond_caisse_matin DECIMAL DEFAULT '0.00',
            date_cloture TIMESTAMP,
            montant_compté_soir DECIMAL,
            montant_theorique_soir DECIMAL,
            ecart_caisse DECIMAL DEFAULT '0.00'
        )
    ''')
    
    # Migration automatique si la table existe déjà sous une ancienne version
    cursor.execute("PRAGMA table_info(Sessions_Caisse)")
    colonnes = [row[1] for row in cursor.fetchall()]
    if 'montant_compté_soir' not in colonnes:
        try: cursor.execute("ALTER TABLE Sessions_Caisse ADD COLUMN montant_compté_soir DECIMAL")
        except: pass
    if 'montant_theorique_soir' not in colonnes:
        try: cursor.execute("ALTER TABLE Sessions_Caisse ADD COLUMN montant_theorique_soir DECIMAL")
        except: pass
    if 'ecart_caisse' not in colonnes:
        try: cursor.execute("ALTER TABLE Sessions_Caisse ADD COLUMN ecart_caisse DECIMAL DEFAULT '0.00'")
        except: pass

    # Table Depenses_Caisse (Sorties de caisse)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Depenses_Caisse (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            libelle TEXT,
            montant DECIMAL,
            moyen_paiement TEXT DEFAULT 'Espèces'
        )
    ''')

    # Table Tickets
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_ticket TEXT UNIQUE NOT NULL,
            date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_tvac DECIMAL,
            total_htva DECIMAL,
            total_tva DECIMAL,
            remise DECIMAL DEFAULT '0.00',
            methode_paiement TEXT,
            id_client INTEGER,
            vendeur_nom TEXT,
            rendu_monnaie DECIMAL DEFAULT '0.00',
            caisse_id TEXT DEFAULT 'POS-01',
            details_articles TEXT,
            signature TEXT,
            hash_precedent TEXT,
            previous_hash TEXT,
            current_hash TEXT,
            FOREIGN KEY (id_client) REFERENCES Clients(id)
        )
    ''')
    
    # Migration automatique si la table Tickets existe déjà sous une ancienne version
    cursor.execute("PRAGMA table_info(Tickets)")
    cols_tickets = [row[1] for row in cursor.fetchall()]
    if 'remise' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN remise DECIMAL DEFAULT '0.00'")
        except: pass
    if 'id_client' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN id_client INTEGER")
        except: pass
    if 'vendeur_nom' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN vendeur_nom TEXT")
        except: pass
    if 'rendu_monnaie' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN rendu_monnaie DECIMAL DEFAULT '0.00'")
        except Exception as e: print("Err rendu_monnaie:", e)
    if 'signature' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN signature TEXT")
        except Exception as e: print("Err signature:", e)
    if 'hash_precedent' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN hash_precedent TEXT")
        except Exception as e: print("Err hash_precedent:", e)
    if 'previous_hash' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN previous_hash TEXT")
        except Exception as e: print("Err previous_hash:", e)
    if 'current_hash' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN current_hash TEXT")
        except Exception as e: print("Err current_hash:", e)
    if 'caisse_id' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN caisse_id TEXT DEFAULT 'POS-01'")
        except Exception as e: print("Err caisse_id:", e)
    if 'details_articles' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN details_articles TEXT")
        except Exception as e: print("Err details_articles:", e)
    if 'sync_status' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN sync_status INTEGER DEFAULT 1")
        except: pass
    if 'offline_uuid' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN offline_uuid TEXT")
        except: pass
    if 'created_at_utc' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN created_at_utc TEXT")
        except: pass
    
    # Table Ventes_Details
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Ventes_Details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_ticket INTEGER,
            id_stock INTEGER,
            quantite INTEGER,
            prix_unitaire_tvac DECIMAL,
            FOREIGN KEY (id_ticket) REFERENCES Tickets(id) ON DELETE CASCADE,
            FOREIGN KEY (id_stock) REFERENCES Stocks(id)
        )
    ''')

    # Table Ledger_Caisse (Journal des transactions financières immuable)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Ledger_Caisse (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            vendeur TEXT,
            type_mouvement TEXT NOT NULL,
            montant DECIMAL NOT NULL,
            methode_paiement TEXT,
            reference TEXT
        )
    ''')

    # Table Parametres (PIN admin) a été déplacée au début de la fonction.
    
    # Table Rapports_Z (Clôtures comptables immuables)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Rapports_Z (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            donnees_json TEXT NOT NULL
        )
    ''')
    
    # Migration automatique NF525 (Signature)
    cursor.execute("PRAGMA table_info(Ledger_Caisse)")
    cols_ledger = [row[1] for row in cursor.fetchall()]
    if 'signature' not in cols_ledger:
        try: cursor.execute("ALTER TABLE Ledger_Caisse ADD COLUMN signature TEXT")
        except: pass
    if 'hash_precedent' not in cols_ledger:
        try: cursor.execute("ALTER TABLE Ledger_Caisse ADD COLUMN hash_precedent TEXT")
        except: pass

    cursor.execute("PRAGMA table_info(Rapports_Z)")
    cols_z = [row[1] for row in cursor.fetchall()]
    if 'signature' not in cols_z:
        try: cursor.execute("ALTER TABLE Rapports_Z ADD COLUMN signature TEXT")
        except: pass
    if 'hash_precedent' not in cols_z:
        try: cursor.execute("ALTER TABLE Rapports_Z ADD COLUMN hash_precedent TEXT")
        except: pass
        
    cursor.execute("PRAGMA table_info(Tickets)")
    cols_tickets = [row[1] for row in cursor.fetchall()]
    if 'synced_shopify' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN synced_shopify INTEGER DEFAULT 0")
        except: pass
    if 'shopify_order_id' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN shopify_order_id TEXT")
        except: pass

    # Migration Clients : points_fidelite & 360
    cursor.execute("PRAGMA table_info(Clients)")
    cols_clients = [row[1] for row in cursor.fetchall()]
    if 'points_fidelite' not in cols_clients:
        try: cursor.execute("ALTER TABLE Clients ADD COLUMN points_fidelite INTEGER DEFAULT 0")
        except: pass
    if 'taille_haut' not in cols_clients:
        try: cursor.execute("ALTER TABLE Clients ADD COLUMN taille_haut TEXT")
        except: pass
    if 'taille_bas' not in cols_clients:
        try: cursor.execute("ALTER TABLE Clients ADD COLUMN taille_bas TEXT")
        except: pass
    if 'pointure' not in cols_clients:
        try: cursor.execute("ALTER TABLE Clients ADD COLUMN pointure TEXT")
        except: pass
    if 'pref_couleurs' not in cols_clients:
        try: cursor.execute("ALTER TABLE Clients ADD COLUMN pref_couleurs TEXT")
        except: pass
    if 'date_anniversaire' not in cols_clients:
        try: cursor.execute("ALTER TABLE Clients ADD COLUMN date_anniversaire TEXT")
        except: pass

    # Table Cartes_Cadeaux
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Cartes_Cadeaux (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            solde_initial DECIMAL NOT NULL,
            solde_actuel DECIMAL NOT NULL,
            date_creation DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Table Paniers_En_Attente (Mise en attente temporaire)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Paniers_En_Attente (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            client_id INTEGER,
            client_nom TEXT,
            total_tvac DECIMAL,
            remise DECIMAL DEFAULT '0.00',
            panier_json TEXT NOT NULL,
            note TEXT
        )
    ''')
    
    # Insertion des paramètres par défaut s'ils n'existent pas
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('pin_admin', ?)", (hash_pin('0000'),))
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_name', 'L''ADRESSE B')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_subtitle', 'Boutique de Mode')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_address', 'Chemin Rue 53, 4960 Malmedy')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_vat', 'BE 1035.331.577')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_tva', '0.21')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shopify_store_url', '')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shopify_access_token', '')")
    
    # Categories supplémentaires et Vendeur Admin par défaut uniquement lors de la création
    if not is_initialized:
        categories_defaut = [
            "SERVICE COIFFURE",
            "ESTHÉTIQUE",
            "VENTE BOUTIQUE",
            "DÉCORATION",
            "COFFRET",
            "Accessoires & Bijoux",
            "Chaussures",
            "Mailles & Pulls",
            "Maroquinerie",
            "Pantalons & Jeans",
            "Robes & Jupes",
            "Vestes & Manteaux",
            "Général"
        ]
        for cat in categories_defaut:
            cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))
        
        # Initialisation Vendeur Admin par défaut (PIN 0000 haché)
        cursor.execute("INSERT OR IGNORE INTO Vendeurs (nom, pin, role_admin) VALUES ('Administrateur', ?, 1)", (hash_pin('0000'),))

        # Marquer la base de données comme initialisée pour éviter de recréer ces données fictives si l'utilisateur les supprime
        cursor.execute("INSERT INTO Parametres (cle, valeur) VALUES ('db_is_initialized', '1')")

    # Migration automatique des PINs en clair existants vers leur version hachée
    cursor.execute("SELECT id, pin FROM Vendeurs")
    vendeurs = cursor.fetchall()
    for vid, pin in vendeurs:
        # Si le PIN est de 4 chiffres en clair, on le hache
        if pin and len(pin) == 4 and pin.isdigit():
            hashed = hash_pin(pin)
            # Puisque le PIN doit être UNIQUE, on vérifie s'il n'y a pas déjà ce hash pour éviter les erreurs d'unicité
            cursor.execute("SELECT COUNT(*) FROM Vendeurs WHERE pin = ?", (hashed,))
            if cursor.fetchone()[0] == 0:
                cursor.execute("UPDATE Vendeurs SET pin = ? WHERE id = ?", (hashed, vid))
            else:
                # Si le doublon existe déjà, on génère un PIN temporaire pour forcer le changement
                import random
                temp_pin = f"TEMP_{random.randint(1000, 9999)}"
                cursor.execute("UPDATE Vendeurs SET pin = ? WHERE id = ?", (temp_pin, vid))

    # Migration de pin_admin dans Parametres
    cursor.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin'")
    row_pin = cursor.fetchone()
    if row_pin:
        pin_val = row_pin[0]
        if pin_val and len(pin_val) == 4 and pin_val.isdigit():
            cursor.execute("UPDATE Parametres SET valeur = ? WHERE cle = 'pin_admin'", (hash_pin(pin_val),))
    
    # Sécurité Base de données (Triggers)
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS prevent_negative_stock
        BEFORE UPDATE ON Stocks
        FOR EACH ROW
        WHEN NEW.quantite_actuelle < 0
        BEGIN
            SELECT RAISE(ABORT, 'Le stock ne peut pas être négatif');
        END;
    ''')

    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS clean_empty_barcode_insert
        AFTER INSERT ON Produits
        FOR EACH ROW
        WHEN NEW.code_barre = ''
        BEGIN
            UPDATE Produits SET code_barre = NULL WHERE id = NEW.id;
        END;
    ''')

    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS clean_empty_barcode_update
        AFTER UPDATE ON Produits
        FOR EACH ROW
        WHEN NEW.code_barre = ''
        BEGIN
            UPDATE Produits SET code_barre = NULL WHERE id = NEW.id;
        END;
    ''')
    
    # Optimisations de performance (Index)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_date ON Tickets(date_heure)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ventes_details_ticket ON Ventes_Details(id_ticket)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stocks_produit ON Stocks(id_produit)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_produits_code ON Produits(code_barre)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rapports_z_date ON Rapports_Z(date)")
    conn.commit()
    print("[OK] Base de données SQLite initialisée avec succès.")

def generer_numero_ticket(cursor):
    """Génère un numéro de ticket séquentiel au format TCK-AAAA-XXXX de manière robuste"""
    annee_actuelle = datetime.datetime.now().year
    
    # On récupère l'ID le plus élevé de l'année au lieu de compter (sécurité anti-doublons)
    cursor.execute('''
        SELECT COALESCE(MAX(CAST(SUBSTR(numero_ticket, 10) AS INTEGER)), 0) 
        FROM Tickets 
        WHERE strftime('%Y', date_heure) = ?
    ''', (str(annee_actuelle),))
    
    dernier_seq = cursor.fetchone()[0]
    
    # Format: TCK-2024-0001
    numero = f"TCK-{annee_actuelle}-{dernier_seq + 1:04d}"
    return numero

def calculer_hash_transaction(previous_hash, timestamp, montant_total, caisse_id="POS-01", details_articles=""):
    """
    Calcule le hash SHA-256 d'une transaction à partir de :
    previous_hash + timestamp + montant_total + caisse_id + details_articles
    """
    import hashlib
    from decimal import Decimal
    
    prev_str = str(previous_hash or "GENESIS_BLOCK_KODO_POS")
    ts_str = str(timestamp or "")
    montant_str = f"{Decimal(str(montant_total)):.2f}"
    caisse_str = str(caisse_id or "POS-01")
    details_str = str(details_articles or "")
    
    data_to_hash = f"{prev_str}|{ts_str}|{montant_str}|{caisse_str}|{details_str}"
    return hashlib.sha256(data_to_hash.encode('utf-8')).hexdigest()

def signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id="POS-01", details_articles=""):
    """
    Génère une signature cryptographique inaltérable (Audit Trail) pour un ticket.
    Retourne (current_hash, previous_hash).
    """
    # 1. Récupérer le dernier hash de la table Tickets
    cursor.execute("SELECT COALESCE(current_hash, signature) FROM Tickets WHERE current_hash IS NOT NULL OR signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    previous_hash = row[0] if (row and row[0]) else "GENESIS_BLOCK_KODO_POS"
    
    # 2. Calculer le hash courant via SHA-256
    current_hash = calculer_hash_transaction(previous_hash, date_heure, total_tvac, caisse_id, details_articles)
    
    return current_hash, previous_hash

def signer_ledger(cursor, type_mouvement, montant, methode, reference, date_heure):
    """Génère une signature cryptographique pour une ligne du Ledger_Caisse (NF525)"""
    import hashlib
    cursor.execute("SELECT signature FROM Ledger_Caisse WHERE signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    hash_precedent = row[0] if row else "GENESIS_LEDGER_KODO_POS"
    montant_str = f"{Decimal(str(montant)):.2f}"
    data_to_hash = f"{hash_precedent}|{type_mouvement}|{montant_str}|{methode}|{reference}|{date_heure}"
    signature = hashlib.sha256(data_to_hash.encode('utf-8')).hexdigest()
    return signature, hash_precedent

def signer_rapport_z(cursor, date_z, donnees_json):
    """Génère une signature cryptographique pour un Rapport Z (NF525)"""
    import hashlib
    cursor.execute("SELECT signature FROM Rapports_Z WHERE signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    hash_precedent = row[0] if row else "GENESIS_Z_KODO_POS"
    data_to_hash = f"{hash_precedent}|{date_z}|{donnees_json}"
    signature = hashlib.sha256(data_to_hash.encode('utf-8')).hexdigest()
    return signature, hash_precedent

def ajouter_produit_test():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Utilisation de la bibliothèque Decimal pour les calculs monétaires
        prix_achat = Decimal('45.50')
        taux_tva = Decimal('0.21')
        
        # Calcul: Marge souhaitée + TVA
        prix_vente_htva = Decimal('90.00')
        prix_vente_tvac = prix_vente_htva * (Decimal('1.00') + taux_tva)
        # Arrondi à 2 décimales pour la vente
        prix_vente_tvac = prix_vente_tvac.quantize(Decimal('0.01'))
        
        # 1. Insertion du Produit
        cursor.execute('''
            INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, taux_tva)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('1234567890123', 'Robe d\'été en Lin', 'Vêtements Femme', prix_achat, prix_vente_tvac, taux_tva))
        
        produit_id = cursor.lastrowid
        
        # 2. Insertion du Stock
        cursor.execute('''
            INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte)
            VALUES (?, ?, ?, ?)
        ''', (produit_id, 'M', 15, 3))
        
        stock_id = cursor.lastrowid
        
        # 3. Simulation d'une Vente (Création d'un ticket)
        numero_ticket = generer_numero_ticket(cursor)
        quantite_vendue = 2
        
        total_tvac = prix_vente_tvac * Decimal(str(quantite_vendue))
        total_htva = prix_vente_htva * Decimal(str(quantite_vendue))
        total_tva = total_tvac - total_htva
        
        import datetime
        date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details_articles = f"ProduitTest:{quantite_vendue}x{prix_vente_tvac}"
        current_hash, previous_hash = signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id="POS-01", details_articles=details_articles)

        cursor.execute('''
            INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement, caisse_id, details_articles, signature, hash_precedent, previous_hash, current_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (numero_ticket, date_heure, total_tvac, total_htva, total_tva, 'Bancontact', 'POS-01', details_articles, current_hash, previous_hash, previous_hash, current_hash))
        
        ticket_id = cursor.lastrowid
        
        # 4. Insertion des détails de la vente
        cursor.execute('''
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, ?, ?)
        ''', (ticket_id, stock_id, quantite_vendue, prix_vente_tvac))
        
        # Mise à jour du stock
        cursor.execute('''
            UPDATE Stocks 
            SET quantite_actuelle = quantite_actuelle - ? 
            WHERE id = ?
        ''', (quantite_vendue, stock_id))
        
        conn.commit()
        print(f"[OK] Produit de test '{produit_id}' et Stock '{stock_id}' créés.")
        print(f"[OK] Ticket test '{numero_ticket}' créé avec un total de {total_tvac}€.")
        
    except sqlite3.IntegrityError:
        print("[INFO] Le produit de test (ou le code barre) existe déjà dans la base.")
    except Exception as e:
        print(f"[ERREUR] Erreur lors de l'ajout: {e}")
        conn.rollback()
    finally:
        conn.close()


def enregistrer_vente(cursor, numero_ticket, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, panier, vendeur_nom, date_heure, paiements, caisse_id="POS-01", sync_status=1, offline_uuid=None, created_at_utc=None):
    """
    Enregistre une vente complète dans la base de données (NF525 & Offline-First).
    """
    import uuid, datetime
    from datetime import timezone

    if not offline_uuid:
        offline_uuid = str(uuid.uuid4())
    if not created_at_utc:
        created_at_utc = datetime.datetime.now(timezone.utc).isoformat()

    # Formater les détails d'articles pour la chaîne de hachage immuable
    details_list = []
    if panier:
        for it in panier:
            code = it.get("code_barre") or it.get("nom") or str(it.get("stock_id", ""))
            px = it.get("prix_vente_tvac", 0)
            details_list.append(f"{code}:{px}")
    details_articles = ";".join(details_list)

    # 1. Signer le ticket
    current_hash, previous_hash = signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id=caisse_id, details_articles=details_articles)
    
    # 2. Insérer le ticket
    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, caisse_id, details_articles, signature, hash_precedent, previous_hash, current_hash, sync_status, offline_uuid, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, caisse_id, details_articles, current_hash, previous_hash, previous_hash, current_hash, sync_status, offline_uuid, created_at_utc))
    
    ticket_id = cursor.lastrowid
    
    # 3. Insérer les détails de vente et mettre à jour le stock
    for it in panier:
        cursor.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, ?, ?)
        """, (ticket_id, it["stock_id"], 1, it["prix_vente_tvac"]))
        if it["stock_id"]:
            cursor.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - 1 WHERE id = ?", (it["stock_id"],))
            
    # 4. Mettre à jour la fidélité client
    if id_client:
        cursor.execute("""
            UPDATE Clients 
            SET total_depense = total_depense + ?, points_fidelite = points_fidelite + ? 
            WHERE id = ?
        """, (total_tvac, int(total_tvac), id_client))
        
    # 5. Enregistrer dans le ledger immuable
    for methode, montant_paiement in paiements:
        montant_reel = montant_paiement
        if methode == "Espèces" and rendu_monnaie > 0:
            montant_reel -= rendu_monnaie
            
        sig_ledger, hash_ledger = signer_ledger(cursor, 'VENTE', montant_reel, methode, numero_ticket, date_heure)
        cursor.execute("""
            INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent)
            VALUES (?, 'VENTE', ?, ?, ?, ?, ?, ?)
        """, (vendeur_nom, montant_reel, methode, numero_ticket, date_heure, sig_ledger, hash_ledger))
        
    return ticket_id

def enregistrer_remboursement(cursor, ticket_origine, vd_id, stock_id, prix, mode, vendeur_nom, date_heure):
    """
    Enregistre un remboursement conforme dans la base de données (NF525).
    """
    # 1. Déterminer le taux de TVA
    cursor.execute("""
        SELECT p.taux_tva 
        FROM Ventes_Details vd
        JOIN Stocks s ON vd.id_stock = s.id
        JOIN Produits p ON s.id_produit = p.id
        WHERE vd.id = ?
    """, (vd_id,))
    row = cursor.fetchone()
    taux_tva = Decimal(str(row[0])) if row else Decimal('0.21')
    
    # 2. Calculer les montants négatifs
    total_tvac = -Decimal(str(prix))
    total_htva = (total_tvac / (Decimal('1.00') + taux_tva)).quantize(Decimal('0.01'))
    total_tva = total_tvac - total_htva
    
    # 3. Créer le numéro de ticket négatif de manière séquentielle
    import time
    timestamp_suffix = str(int(time.time()))[-5:]
    new_tk = f"REF-{ticket_origine}-{timestamp_suffix}"
    
    # 4. Signer le ticket de remboursement
    signature, hash_prec = signer_ticket(cursor, new_tk, total_tvac, date_heure)
    
    # 5. Insérer le ticket négatif
    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, signature, hash_precedent)
        VALUES (?, ?, ?, ?, ?, 0.00, ?, ?, ?)
    """, (new_tk, date_heure, total_tvac, total_htva, total_tva, f"REMB ({mode})", signature, hash_prec))
    
    ticket_id = cursor.lastrowid
    
    # 6. Remettre en stock
    if stock_id:
        cursor.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle + 1 WHERE id = ?", (stock_id,))
        cursor.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, -1, ?)
        """, (ticket_id, stock_id, prix))
        
    # 7. Enregistrer dans le Ledger Immuable (Retrait)
    sig_ledger, hash_ledger = signer_ledger(cursor, 'REMBOURSEMENT', total_tvac, mode, new_tk, date_heure)
    cursor.execute("""
        INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent)
        VALUES (?, 'REMBOURSEMENT', ?, ?, ?, ?, ?, ?)
    """, (vendeur_nom, total_tvac, mode, new_tk, date_heure, sig_ledger, hash_ledger))
    
    return new_tk

def tester_lecture_decimal():
    """Vérifie que les données ressortent bien en tant que type Decimal."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT prix_vente_tvac FROM Produits LIMIT 1')
    result = cursor.fetchone()
    if result:
        valeur = result[0]
        print(f"[INFO] Test de lecture: Valeur = {valeur}, Type = {type(valeur)}")
    conn.close()

def sauvegarder_panier_en_attente(panier, total_tvac, client_id=None, client_nom=None, remise=Decimal('0.00'), note="", conn=None):
    """Stocke un panier temporairement en attente sans affecter la numérotation des tickets ni les stocks."""
    import json
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
        
    try:
        c = conn.cursor()
        panier_serializable = []
        for item in panier:
            panier_serializable.append({
                "nom": item.get("nom"),
                "taille": item.get("taille"),
                "prix_vente_tvac": str(item.get("prix_vente_tvac")),
                "taux_tva": str(item.get("taux_tva", '0.21')),
                "stock_id": item.get("stock_id"),
                "en_solde": item.get("en_solde", 0),
                "prix_original_tvac": str(item["prix_original_tvac"]) if item.get("prix_original_tvac") is not None else None,
                "remise_label": item.get("remise_label", ""),
                "code_barre": item.get("code_barre")
            })
            
        panier_json = json.dumps(panier_serializable, ensure_ascii=False)
        c.execute("""
            INSERT INTO Paniers_En_Attente (client_id, client_nom, total_tvac, remise, panier_json, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, client_nom, str(total_tvac), str(remise), panier_json, note))
        
        panier_id = c.lastrowid
        conn.commit()
        return panier_id
    finally:
        if close_conn:
            conn.close()

def lister_paniers_en_attente(conn=None):
    """Retourne la liste de tous les paniers actuellement en attente."""
    import json
    from decimal import Decimal
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
        
    try:
        c = conn.cursor()
        c.execute("SELECT id, date_creation, client_id, client_nom, total_tvac, remise, panier_json, note FROM Paniers_En_Attente ORDER BY id DESC")
        rows = c.fetchall()
        paniers = []
        for r in rows:
            paniers.append({
                "id": r[0],
                "date_creation": r[1],
                "client_id": r[2],
                "client_nom": r[3],
                "total_tvac": Decimal(str(r[4])),
                "remise": Decimal(str(r[5] or '0.00')),
                "panier": json.loads(r[6]),
                "note": r[7] or ""
            })
        return paniers
    finally:
        if close_conn:
            conn.close()

def recuperer_panier_en_attente(panier_id, conn=None):
    """Récupère et supprime un panier en attente par son ID pour le restaurer."""
    import json
    from decimal import Decimal
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
        
    try:
        c = conn.cursor()
        c.execute("SELECT id, date_creation, client_id, client_nom, total_tvac, remise, panier_json, note FROM Paniers_En_Attente WHERE id=?", (panier_id,))
        r = c.fetchone()
        if not r:
            return None
            
        res = {
            "id": r[0],
            "date_creation": r[1],
            "client_id": r[2],
            "client_nom": r[3],
            "total_tvac": Decimal(str(r[4])),
            "remise": Decimal(str(r[5] or '0.00')),
            "panier_raw": json.loads(r[6]),
            "note": r[7] or ""
        }
        
        c.execute("DELETE FROM Paniers_En_Attente WHERE id=?", (panier_id,))
        conn.commit()
        return res
    finally:
        if close_conn:
            conn.close()

def supprimer_panier_en_attente(panier_id, conn=None):
    """Supprime définitivement un panier en attente."""
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True
    try:
        c = conn.cursor()
        c.execute("DELETE FROM Paniers_En_Attente WHERE id=?", (panier_id,))
        conn.commit()
        return True
    finally:
        if close_conn:
            conn.close()

def generer_bilan_z_journalier(caisse_id="POS-01", conn=None):
    """Calcul le bilan de clôture de caisse quotidien (Z de Caisse) sur les tickets non clôturés."""
    from decimal import Decimal
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        c = conn.cursor()

        # Date de la dernière clôture
        c.execute("SELECT MAX(date_cloture) FROM Clotures_Caisse WHERE caisse_id=?", (caisse_id,))
        row_z = c.fetchone()
        last_z_date = row_z[0] if row_z else None

        if last_z_date:
            c.execute("""
                SELECT COUNT(*), COALESCE(SUM(total_tvac), 0.0), COALESCE(SUM(total_htva), 0.0), 
                       COALESCE(SUM(total_tva), 0.0), COALESCE(SUM(remise), 0.0)
                FROM Tickets WHERE date_heure > ?
            """, (last_z_date,))
        else:
            c.execute("""
                SELECT COUNT(*), COALESCE(SUM(total_tvac), 0.0), COALESCE(SUM(total_htva), 0.0), 
                       COALESCE(SUM(total_tva), 0.0), COALESCE(SUM(remise), 0.0)
                FROM Tickets
            """)

        nb_tickets, tot_tvac, tot_htva, tot_tva, tot_remises = c.fetchone()

        # Extraction par mode de paiement depuis Ledger_Caisse
        if last_z_date:
            c.execute("SELECT methode_paiement, SUM(montant) FROM Ledger_Caisse WHERE date_mouvement > ? GROUP BY methode_paiement", (last_z_date,))
        else:
            c.execute("SELECT methode_paiement, SUM(montant) FROM Ledger_Caisse GROUP BY methode_paiement")

        ledger_rows = c.fetchall()
        tot_esp = Decimal("0.00")
        tot_carte = Decimal("0.00")

        for m, mt in ledger_rows:
            mt_dec = Decimal(str(mt or "0.00"))
            if m == "Espèces":
                tot_esp += mt_dec
            else:
                tot_carte += mt_dec

        return {
            "caisse_id": caisse_id,
            "nb_tickets": nb_tickets or 0,
            "total_tvac": Decimal(str(tot_tvac or "0.00")),
            "total_htva": Decimal(str(tot_htva or "0.00")),
            "total_tva": Decimal(str(tot_tva or "0.00")),
            "total_remises": Decimal(str(tot_remises or "0.00")),
            "total_especes": tot_esp,
            "total_carte": tot_carte
        }
    finally:
        if close_conn:
            conn.close()

def enregistrer_cloture_caisse(caisse_id="POS-01", fond_caisse_reel=Decimal("0.00"), vendeur="Admin", conn=None):
    """Enregistre officiellement la clôture Z de caisse avec chaînage cryptographique SHA-256 (NF525)."""
    import datetime
    from decimal import Decimal
    from audit_trail import calculer_hash_cloture

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        c = conn.cursor()
        bilan = generer_bilan_z_journalier(caisse_id, conn=conn)

        # Dernier hash de clôture Z
        c.execute("SELECT current_hash FROM Clotures_Caisse WHERE caisse_id=? ORDER BY id DESC LIMIT 1", (caisse_id,))
        last_row = c.fetchone()
        hash_prec = last_row[0] if last_row and last_row[0] else "GENESIS_Z_00000000000000000000000000000000"

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_utc = datetime.datetime.utcnow().isoformat() + "Z"

        fond_reel_dec = Decimal(str(fond_caisse_reel))
        ecart = (fond_reel_dec - bilan["total_especes"]).quantize(Decimal("0.01"))

        curr_hash = calculer_hash_cloture(
            hash_prec, now_str, caisse_id,
            bilan["total_tvac"], bilan["total_especes"], bilan["total_carte"]
        )

        c.execute("""
            INSERT INTO Clotures_Caisse (
                date_cloture, caisse_id, total_ventes_tvac, total_htva, total_tva,
                total_especes, total_carte, total_remises, total_tickets,
                fond_caisse_reel, ecart, vendeur, hash_precedent, current_hash, signature, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_str, caisse_id, float(bilan["total_tvac"]), float(bilan["total_htva"]), float(bilan["total_tva"]),
            float(bilan["total_especes"]), float(bilan["total_carte"]), float(bilan["total_remises"]),
            bilan["nb_tickets"], float(fond_reel_dec), float(ecart), vendeur,
            hash_prec, curr_hash, curr_hash, now_utc
        ))

        conn.commit()
        print(f"[Z DE CAISSE] ✅ Clôture enregistrée avec succès. Hash: {curr_hash[:16]}...")
        return {
            "date": now_str,
            "current_hash": curr_hash,
            "total_tvac": bilan["total_tvac"],
            "ecart": ecart
        }
    finally:
        if close_conn:
            conn.close()

if __name__ == '__main__':
    print("--- Démarrage de l'initialisation du POS 'L'ADRESSE B' ---")
    initialiser_db()
    ajouter_produit_test()
    tester_lecture_decimal()
    print("--- Opérations terminées ---")
