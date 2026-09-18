# -*- coding: utf-8 -*-
"""
Façade de compatibilité database_manager pour Kōdo POS Core.
Assure la rétrocompatibilité complète vers kodo_core.domain.* et kodo_core.db.*.
"""

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
import datetime
import os
import sys
import shutil
from contextlib import contextmanager





def resource_path(relative_path):
    """Obtient le chemin absolu vers une ressource intégrée via PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def data_path(relative_path):
    """Obtient le chemin absolu pour les données persistantes."""
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


# Base de données persistante
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
    return hashlib.sha256((str(pin_plain) + salt).encode('utf-8')).hexdigest()


class SafeConnection:
    """Wrapper ultra-sécurisé pour garantir la fermeture des connexions."""
    def __init__(self, db_name, **kwargs):
        self.db_name = db_name
        self._closed = False
        self._conn = sqlite3.connect(db_name, **kwargs)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass

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

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def fetchall(self):
        return self._conn.fetchall()

    def fetchone(self):
        return self._conn.fetchone()

    def __del__(self):
        self.close()


def get_connection(db_path=None):
    target_db = db_path or DB_NAME
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target_db)), exist_ok=True)
    except Exception:
        pass
    return SafeConnection(target_db, detect_types=sqlite3.PARSE_DECLTYPES)


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


def initialiser_db(conn=None, *args, **kwargs):
    """Initialise le schéma de la base de données de manière tolérante aux arguments."""
    try:
        from core.migrations import MigrationManager
        db_target = getattr(conn, 'db_name', DB_NAME) if conn is not None else DB_NAME
        # Quand un `conn` explicite est fourni (ex. bases ":memory:" ou temporaires des
        # tests), il faut appliquer les migrations SUR CETTE connexion : sans le passer
        # ici, run_migrations ouvrait sa propre connexion ":memory:" séparée et jetable,
        # et les migrations (déclencheurs d'immutabilité, colonnes z_id/caisse_id, etc.)
        # n'atteignaient jamais la connexion réellement utilisée par l'appelant.
        raw_conn = getattr(conn, "_conn", conn) if conn is not None else None
        MigrationManager.run_migrations(db_target, conn=raw_conn)
    except Exception as me:
        print(f"⚠️ Avertissement Migration: {me}")

    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        _initialiser_db_raw(conn)
    finally:
        if should_close:
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

    # Table Parametres
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Parametres (
            cle TEXT PRIMARY KEY,
            valeur TEXT
        )
    ''')

    # Filet de sécurité : ces 3 tables sont censées être créées par MigrationManager
    # (versions 1.0.0 / 1.4.0), mais toute base dont la migration correspondante échoue
    # silencieusement (cf. le except Exception large de initialiser_db ci-dessus) ne doit
    # jamais se retrouver sans elles. IF NOT EXISTS les rend sans danger à répéter ici.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Audit_Trail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            entity_id TEXT,
            user_name TEXT,
            action TEXT NOT NULL,
            details TEXT,
            previous_hash TEXT,
            current_hash TEXT NOT NULL,
            signature TEXT
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_trail_timestamp ON Audit_Trail(timestamp)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Cartes_Cadeaux (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            solde_initial DECIMAL NOT NULL,
            solde_actuel DECIMAL NOT NULL,
            date_creation DATETIME DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT NULL,
            client_id INTEGER DEFAULT NULL,
            client_nom TEXT DEFAULT NULL,
            status TEXT DEFAULT 'active',
            emis_par TEXT DEFAULT NULL
        )
    ''')
    cursor.execute("PRAGMA table_info(Cartes_Cadeaux)")
    cols_cartes_cadeaux = [row[1] for row in cursor.fetchall()]
    for col, ddl in (
        ("notes", "ALTER TABLE Cartes_Cadeaux ADD COLUMN notes TEXT DEFAULT NULL"),
        ("client_id", "ALTER TABLE Cartes_Cadeaux ADD COLUMN client_id INTEGER DEFAULT NULL"),
        ("client_nom", "ALTER TABLE Cartes_Cadeaux ADD COLUMN client_nom TEXT DEFAULT NULL"),
        ("status", "ALTER TABLE Cartes_Cadeaux ADD COLUMN status TEXT DEFAULT 'active'"),
        ("emis_par", "ALTER TABLE Cartes_Cadeaux ADD COLUMN emis_par TEXT DEFAULT NULL"),
    ):
        if col not in cols_cartes_cadeaux:
            try: cursor.execute(ddl)
            except Exception: pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Shopify_Sync (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            shopify_id TEXT NOT NULL,
            last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'synced',
            details TEXT
        )
    ''')

    # Table Marques
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Marques (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT UNIQUE NOT NULL
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
            prix_solde_tvac DECIMAL DEFAULT NULL,
            type_vente TEXT DEFAULT 'unite',
            unite_mesure TEXT DEFAULT 'pce',
            marque TEXT DEFAULT NULL,
            attributs_json TEXT DEFAULT NULL,
            sync_status INTEGER DEFAULT 0,
            seuil_alerte INTEGER DEFAULT 5
        )
    ''')

    # Migrations de colonnes manquantes sur Produits
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
    if 'seuil_alerte' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN seuil_alerte INTEGER DEFAULT 5")
        except: pass

    # Insertion automatique dans Categories & Marques
    cursor.execute("INSERT OR IGNORE INTO Categories (nom) SELECT DISTINCT categorie FROM Produits WHERE categorie IS NOT NULL AND categorie != ''")
    cursor.execute("INSERT OR IGNORE INTO Marques (nom) SELECT DISTINCT marque FROM Produits WHERE marque IS NOT NULL AND marque != ''")

    # Table ShopInfo
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

    # Table Depenses_Caisse
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
            sync_status INTEGER DEFAULT 1,
            offline_uuid TEXT,
            created_at_utc TEXT,
            synced_shopify INTEGER DEFAULT 0,
            shopify_order_id TEXT,
            z_id INTEGER DEFAULT NULL,
            FOREIGN KEY (id_client) REFERENCES Clients(id)
        )
    ''')

    # Migrations de colonnes manquantes sur Tickets
    cursor.execute("PRAGMA table_info(Tickets)")
    cols_tickets = [row[1] for row in cursor.fetchall()]
    if 'z_id' not in cols_tickets:
        try: cursor.execute("ALTER TABLE Tickets ADD COLUMN z_id INTEGER DEFAULT NULL")
        except: pass

    # Table Ventes_Details
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Ventes_Details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_ticket INTEGER,
            id_stock INTEGER,
            quantite INTEGER,
            prix_unitaire_tvac DECIMAL,
            refund_of_vd_id INTEGER DEFAULT NULL,
            FOREIGN KEY (id_ticket) REFERENCES Tickets(id) ON DELETE CASCADE,
            FOREIGN KEY (id_stock) REFERENCES Stocks(id)
        )
    ''')

    # Migrations de colonnes manquantes sur Ventes_Details
    cursor.execute("PRAGMA table_info(Ventes_Details)")
    cols_ventes_details = [row[1] for row in cursor.fetchall()]
    if 'refund_of_vd_id' not in cols_ventes_details:
        try: cursor.execute("ALTER TABLE Ventes_Details ADD COLUMN refund_of_vd_id INTEGER DEFAULT NULL")
        except: pass

    # Table Ledger_Caisse
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Ledger_Caisse (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            vendeur TEXT,
            type_mouvement TEXT NOT NULL,
            montant DECIMAL NOT NULL,
            methode_paiement TEXT,
            reference TEXT,
            signature TEXT,
            hash_precedent TEXT,
            caisse_id TEXT DEFAULT 'POS-01',
            z_id INTEGER DEFAULT NULL
        )
    ''')

    # Migrations de colonnes manquantes sur Ledger_Caisse
    cursor.execute("PRAGMA table_info(Ledger_Caisse)")
    cols_ledger = [row[1] for row in cursor.fetchall()]
    if 'caisse_id' not in cols_ledger:
        try: cursor.execute("ALTER TABLE Ledger_Caisse ADD COLUMN caisse_id TEXT DEFAULT 'POS-01'")
        except: pass
    if 'z_id' not in cols_ledger:
        try: cursor.execute("ALTER TABLE Ledger_Caisse ADD COLUMN z_id INTEGER DEFAULT NULL")
        except: pass

    # Table Rapports_Z
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Rapports_Z (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            donnees_json TEXT NOT NULL,
            signature TEXT,
            hash_precedent TEXT
        )
    ''')

    # Table Clotures_Caisse
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

    # Tables Live Shopping
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Live_Sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titre TEXT NOT NULL,
            statut TEXT DEFAULT 'en_cours',
            date_debut DATETIME DEFAULT CURRENT_TIMESTAMP,
            date_fin DATETIME,
            produit_vedette_id INTEGER,
            notes TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Live_Buyers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            nom TEXT,
            prenom TEXT,
            telephone TEXT,
            email TEXT,
            pseudo_social TEXT,
            mode_reception TEXT DEFAULT 'retrait_magasin',
            adresse_rue TEXT,
            code_postal TEXT,
            ville TEXT,
            pays TEXT,
            taille_haut TEXT,
            taille_bas TEXT,
            pointure TEXT,
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Live_Claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            client_id INTEGER,
            product_id INTEGER NOT NULL,
            stock_id INTEGER,
            article_nom TEXT NOT NULL,
            taille TEXT,
            prix_unitaire_tvac REAL NOT NULL DEFAULT 0.0,
            quantite INTEGER NOT NULL DEFAULT 1,
            statut_attribution TEXT DEFAULT 'file_attente',
            rang_file INTEGER DEFAULT 1,
            statut_paiement TEXT DEFAULT 'non_paye',
            statut_commande TEXT DEFAULT 'en_attente',
            ticket_pos_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Table Paniers_En_Attente
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

    # Paramètres par défaut
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('pin_admin', ?)", (hash_pin('0000'),))
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_name', 'Kōdo POS')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_tva', '0.21')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_seuil_alerte', '5')")

    conn.commit()


def generer_numero_ticket(cursor):
    """Génère un numéro de ticket séquentiel TCK-AAAA-XXXX."""
    annee = datetime.datetime.now().year
    cursor.execute('''
        SELECT COALESCE(MAX(CAST(SUBSTR(numero_ticket, 10) AS INTEGER)), 0) 
        FROM Tickets 
        WHERE strftime('%Y', date_heure) = ?
    ''', (str(annee),))
    seq = cursor.fetchone()[0]
    return f"TCK-{annee}-{seq + 1:04d}"


def calculer_hash_transaction(previous_hash, timestamp, montant_total, caisse_id="POS-01", details_articles=""):
    """Calcule le hash SHA-256 d'une transaction."""
    import hashlib
    prev_str = str(previous_hash or "GENESIS_BLOCK_KODO_POS")
    ts_str = str(timestamp or "")
    montant_str = f"{Decimal(str(montant_total)):.2f}"
    caisse_str = str(caisse_id or "POS-01")
    details_str = str(details_articles or "")
    data = f"{prev_str}|{ts_str}|{montant_str}|{caisse_str}|{details_str}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id="POS-01", details_articles=""):
    """Génère la signature cryptographique d'un ticket."""
    cursor.execute("SELECT COALESCE(current_hash, signature) FROM Tickets WHERE current_hash IS NOT NULL OR signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    previous_hash = row[0] if (row and row[0]) else "GENESIS_BLOCK_KODO_POS"
    current_hash = calculer_hash_transaction(previous_hash, date_heure, total_tvac, caisse_id, details_articles)
    return current_hash, previous_hash


def signer_ledger(cursor, type_mouvement, montant, methode, reference, date_heure):
    """Génère une signature cryptographique pour le Ledger_Caisse."""
    import hashlib
    cursor.execute("SELECT signature FROM Ledger_Caisse WHERE signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    hash_precedent = row[0] if row else "GENESIS_LEDGER_KODO_POS"
    montant_str = f"{Decimal(str(montant)):.2f}"
    data = f"{hash_precedent}|{type_mouvement}|{montant_str}|{methode}|{reference}|{date_heure}"
    signature = hashlib.sha256(data.encode('utf-8')).hexdigest()
    return signature, hash_precedent


def signer_rapport_z(cursor, date_z, donnees_json):
    """Génère une signature cryptographique pour un Rapport Z."""
    import hashlib
    cursor.execute("SELECT signature FROM Rapports_Z WHERE signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    hash_precedent = row[0] if row else "GENESIS_Z_KODO_POS"
    data = f"{hash_precedent}|{date_z}|{donnees_json}"
    signature = hashlib.sha256(data.encode('utf-8')).hexdigest()
    return signature, hash_precedent


def enregistrer_vente(cursor, numero_ticket, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, panier, vendeur_nom, date_heure, paiements, caisse_id="POS-01", sync_status=1, offline_uuid=None, created_at_utc=None):
    """Enregistre une vente (NF525)."""
    import uuid
    from datetime import timezone

    if not offline_uuid:
        offline_uuid = str(uuid.uuid4())
    if not created_at_utc:
        created_at_utc = datetime.datetime.now(timezone.utc).isoformat()

    details_list = []
    if panier:
        for it in panier:
            code = it.get("code_barre") or it.get("nom") or str(it.get("stock_id", ""))
            px = it.get("prix_vente_tvac", 0)
            details_list.append(f"{code}:{px}")
    details_articles = ";".join(details_list)

    current_hash, previous_hash = signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id=caisse_id, details_articles=details_articles)

    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, caisse_id, details_articles, signature, hash_precedent, previous_hash, current_hash, sync_status, offline_uuid, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, caisse_id, details_articles, current_hash, previous_hash, previous_hash, current_hash, sync_status, offline_uuid, created_at_utc))

    ticket_id = cursor.lastrowid

    # NOTE: la disponibilité du stock n'est PAS vérifiée ici. `enregistrer_vente` est
    # aussi le primitif de rejeu utilisé par OfflineSyncEngine pour valider a posteriori
    # des ventes déjà physiquement conclues sur des caisses déconnectées (stratégie
    # Last-Write-Wins) : une vente qui a réellement eu lieu en boutique ne peut pas être
    # rejetée après coup sous prétexte que 2 caisses ont vendu le dernier article en même
    # temps hors-ligne — le stock doit pouvoir passer sous 0 et être flagué pour audit
    # (cf. OfflineSyncEngine). Le contrôle de disponibilité en temps réel se fait un niveau
    # au-dessus, dans process_sale_transaction, avant tout enregistrement.
    for it in panier:
        s_id = it.get("stock_id")
        px = it.get("prix_vente_tvac", 0)
        qty = it.get("quantite", 1)

        cursor.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, ?, ?)
        """, (ticket_id, s_id, qty, px))
        if s_id:
            cursor.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - ? WHERE id = ?", (qty, s_id))

    if id_client:
        cursor.execute("""
            UPDATE Clients 
            SET total_depense = total_depense + ?, points_fidelite = points_fidelite + ? 
            WHERE id = ?
        """, (total_tvac, int(total_tvac), id_client))

    for methode, montant_paiement in paiements:
        montant_reel = Decimal(str(montant_paiement))
        # Comparaison insensible à la casse/accents (cf. total_especes plus bas) : un
        # matching exact-string ("Espèces" seul) laisserait passer "espèces"/"ESPECES"/
        # "cash" sans déduire le rendu de monnaie, gonflant le montant encaissé loggé
        # dans Ledger_Caisse et créant un faux écart de caisse à la clôture Z.
        if str(methode or "").strip().lower() in ("espèces", "especes", "cash") and rendu_monnaie > 0:
            montant_reel -= Decimal(str(rendu_monnaie))

        sig_ledger, hash_ledger = signer_ledger(cursor, 'VENTE', montant_reel, methode, numero_ticket, date_heure)
        cursor.execute("""
            INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent, caisse_id)
            VALUES (?, 'VENTE', ?, ?, ?, ?, ?, ?, ?)
        """, (vendeur_nom, float(montant_reel), methode, numero_ticket, date_heure, sig_ledger, hash_ledger, caisse_id))

    return ticket_id


def enregistrer_remboursement(cursor, ticket_origine, vd_id, stock_id, prix, mode, vendeur_nom, date_heure, quantite=1, caisse_id="POS-01"):
    """Enregistre un remboursement (NF525).

    Le prix, le taux de TVA et le stock à recréditer sont TOUJOURS relus depuis la
    ligne de vente d'origine (`Ventes_Details`/`Produits`) plutôt que fournis par
    l'appelant : un `prix` ou `stock_id` arbitraire transmis par le client ne sont
    jamais utilisés pour le calcul financier réel. `ticket_origine` est vérifié
    contre le ticket réellement propriétaire de `vd_id`, et la quantité déjà
    remboursée pour cette ligne (via `refund_of_vd_id`) est déduite pour empêcher
    tout remboursement en double ou au-delà de la quantité effectivement vendue.
    """
    cursor.execute("""
        SELECT vd.quantite, vd.prix_unitaire_tvac, vd.id_stock, t.numero_ticket, p.taux_tva
        FROM Ventes_Details vd
        JOIN Tickets t ON vd.id_ticket = t.id
        LEFT JOIN Stocks s ON vd.id_stock = s.id
        LEFT JOIN Produits p ON s.id_produit = p.id
        WHERE vd.id = ?
    """, (vd_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"Ligne de vente introuvable (vd_id={vd_id}) : remboursement refusé.")

    qte_vendue, prix_unitaire_origine, id_stock_origine, numero_ticket_origine, taux_tva_row = row

    if ticket_origine and str(ticket_origine) != str(numero_ticket_origine):
        raise ValueError(
            f"Incohérence ticket/ligne : vd_id={vd_id} appartient au ticket {numero_ticket_origine}, "
            f"pas à {ticket_origine}. Remboursement refusé."
        )

    if qte_vendue is None or qte_vendue <= 0:
        raise ValueError(f"La ligne vd_id={vd_id} n'est pas une ligne de vente valide (déjà un remboursement ?).")

    taux_tva = Decimal(str(taux_tva_row)) if taux_tva_row is not None else Decimal('0.21')
    prix_unitaire = Decimal(str(prix_unitaire_origine))

    cursor.execute("SELECT COALESCE(SUM(-quantite), 0) FROM Ventes_Details WHERE refund_of_vd_id = ?", (vd_id,))
    deja_rembourse = cursor.fetchone()[0] or 0

    quantite = int(quantite) if quantite else 1
    if quantite <= 0:
        raise ValueError("La quantité remboursée doit être strictement positive.")

    restant_remboursable = int(qte_vendue) - int(deja_rembourse)
    if quantite > restant_remboursable:
        raise ValueError(
            f"Remboursement refusé : {restant_remboursable} unité(s) restent remboursables sur "
            f"vd_id={vd_id} (vendu={qte_vendue}, déjà remboursé={deja_rembourse}, demandé={quantite})."
        )

    total_tvac = -(prix_unitaire * Decimal(str(quantite))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_htva = (total_tvac / (Decimal('1.00') + taux_tva)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_tva = total_tvac - total_htva

    import time
    timestamp_suffix = str(int(time.time()))[-5:]
    new_tk = f"REF-{numero_ticket_origine}-{timestamp_suffix}"

    signature, hash_prec = signer_ticket(cursor, new_tk, total_tvac, date_heure, caisse_id=caisse_id)

    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, caisse_id, signature, hash_precedent)
        VALUES (?, ?, ?, ?, ?, 0.00, ?, ?, ?, ?)
    """, (new_tk, date_heure, total_tvac, total_htva, total_tva, f"REMB ({mode})", caisse_id, signature, hash_prec))

    ticket_id = cursor.lastrowid

    if id_stock_origine:
        cursor.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle + ? WHERE id = ?", (quantite, id_stock_origine))
        cursor.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac, refund_of_vd_id)
            VALUES (?, ?, ?, ?, ?)
        """, (ticket_id, id_stock_origine, -quantite, prix_unitaire, vd_id))

    sig_ledger, hash_ledger = signer_ledger(cursor, 'REMBOURSEMENT', total_tvac, mode, new_tk, date_heure)
    cursor.execute("""
        INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent, caisse_id)
        VALUES (?, 'REMBOURSEMENT', ?, ?, ?, ?, ?, ?, ?)
    """, (vendeur_nom, float(total_tvac), mode, new_tk, date_heure, sig_ledger, hash_ledger, caisse_id))

    return new_tk, total_tvac


def rechercher_ticket_pour_remboursement(numero_ticket, conn=None):
    """Recherche un ticket de vente par numéro et retourne ses lignes avec la quantité
    encore remboursable par ligne (vendue - déjà remboursée via refund_of_vd_id).

    Source d'autorité pour l'écran Retours : sans ce lookup, l'UI n'avait aucun moyen de
    vérifier qu'un numéro de ticket / un article / un prix tapés à la main correspondaient
    à une vente réellement enregistrée.
    """
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, numero_ticket, date_heure, total_tvac, methode_paiement, vendeur_nom, id_client
            FROM Tickets WHERE numero_ticket = ?
        """, (numero_ticket,))
        t = c.fetchone()
        if not t or t[3] is None or float(t[3]) <= 0:
            # Introuvable, ou c'est un ticket de remboursement (total négatif) : on ne
            # rembourse pas un remboursement.
            return None

        ticket_id, num, date_heure, total_tvac, methode, vendeur, id_client = t

        c.execute("""
            SELECT vd.id, vd.id_stock, vd.quantite, vd.prix_unitaire_tvac,
                   COALESCE(p.nom, 'Article'), s.taille
            FROM Ventes_Details vd
            LEFT JOIN Stocks s ON vd.id_stock = s.id
            LEFT JOIN Produits p ON s.id_produit = p.id
            WHERE vd.id_ticket = ? AND vd.quantite > 0 AND vd.refund_of_vd_id IS NULL
            ORDER BY vd.id ASC
        """, (ticket_id,))
        lignes = []
        for vd_id, stock_id, qte_vendue, prix_unitaire, nom, taille in c.fetchall():
            c.execute("SELECT COALESCE(SUM(-quantite), 0) FROM Ventes_Details WHERE refund_of_vd_id = ?", (vd_id,))
            deja_rembourse = c.fetchone()[0] or 0
            lignes.append({
                "vd_id": vd_id,
                "stock_id": stock_id,
                "name": nom,
                "size": taille or "",
                "unitPrice": float(prix_unitaire),
                "quantitySold": int(qte_vendue),
                "quantityRefunded": int(deja_rembourse),
                "quantityRemaining": int(qte_vendue) - int(deja_rembourse),
            })

        return {
            "ticketId": ticket_id,
            "numero_ticket": num,
            "date_heure": date_heure,
            "total_tvac": float(total_tvac),
            "methode_paiement": methode,
            "vendeur_nom": vendeur or "",
            "lines": lignes,
        }
    finally:
        if should_close:
            conn.close()


def enregistrer_mouvement_caisse(cursor, type_mouvement, montant, motif, vendeur_nom, date_heure, caisse_id="POS-01"):
    """Enregistre un apport ou un prélèvement d'espèces (NF525), scellé et chaîné
    comme toute autre écriture de Ledger_Caisse.

    Sans persistance backend, ces mouvements ne vivaient qu'en mémoire côté frontend
    (React state) et disparaissaient au moindre rechargement de page, désynchronisant
    durablement le théorique caisse affiché lors du comptage / de la clôture Z.
    """
    type_mouvement = str(type_mouvement or "").upper()
    if type_mouvement not in ("APPORT", "PRELEVEMENT"):
        raise ValueError(f"Type de mouvement de caisse invalide : {type_mouvement!r}")

    montant_dec = Decimal(str(montant)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if montant_dec <= Decimal('0.00'):
        raise ValueError("Le montant d'un mouvement de caisse doit être strictement positif.")

    signature, hash_prec = signer_ledger(cursor, type_mouvement, montant_dec, "Espèces", motif or "", date_heure)
    cursor.execute("""
        INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent, caisse_id)
        VALUES (?, ?, ?, 'Espèces', ?, ?, ?, ?, ?)
    """, (vendeur_nom, type_mouvement, float(montant_dec), motif or "", date_heure, signature, hash_prec, caisse_id))

    return cursor.lastrowid


def lister_mouvements_caisse(caisse_id="POS-01", conn=None):
    """Liste les apports/prélèvements de la période en cours (non encore clôturés)."""
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, type_mouvement, montant, reference, vendeur, date_heure
            FROM Ledger_Caisse
            WHERE caisse_id = ? AND z_id IS NULL AND type_mouvement IN ('APPORT', 'PRELEVEMENT')
            ORDER BY id ASC
        """, (caisse_id,))
        rows = c.fetchall()
        return [
            {
                "id": r[0],
                "type": "apport" if r[1] == "APPORT" else "prelevement",
                "amount": float(r[2]),
                "reason": r[3] or "",
                "userName": r[4] or "",
                "date_heure": r[5],
            }
            for r in rows
        ]
    finally:
        if should_close:
            conn.close()


def _generer_code_carte_cadeau(cursor, prefix="AVOIR"):
    """Génère un code de carte cadeau/avoir garanti unique (retry sur collision)."""
    import random
    for _ in range(20):
        code = f"{prefix}-{random.randint(100000, 999999)}"
        cursor.execute("SELECT 1 FROM Cartes_Cadeaux WHERE code = ?", (code,))
        if cursor.fetchone() is None:
            return code
    raise ValueError("Impossible de générer un code de carte cadeau unique.")


def emettre_carte_cadeau(cursor, montant, code=None, client_id=None, client_nom=None, notes=None, emis_par=None, prefix="AVOIR"):
    """Émet une carte cadeau / bon d'avoir réel, persisté et traçable (NF525).

    Sans cette persistance, un avoir « émis » n'existait qu'en mémoire côté navigateur
    (React state / localStorage) : invisible depuis une autre caisse, jamais audité, et
    surtout jamais vérifiable au moment de la dépense (cf. `utiliser_carte_cadeau`).
    """
    montant_dec = Decimal(str(montant)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if montant_dec <= Decimal('0.00'):
        raise ValueError("Le montant d'une carte cadeau/avoir doit être strictement positif.")

    final_code = (code or "").strip().upper() or _generer_code_carte_cadeau(cursor, prefix)
    cursor.execute("SELECT 1 FROM Cartes_Cadeaux WHERE code = ?", (final_code,))
    if cursor.fetchone() is not None:
        raise ValueError(f"Le code de carte cadeau {final_code!r} existe déjà.")

    cursor.execute("""
        INSERT INTO Cartes_Cadeaux (code, solde_initial, solde_actuel, notes, client_id, client_nom, status, emis_par)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
    """, (final_code, float(montant_dec), float(montant_dec), notes, client_id, client_nom, emis_par))

    try:
        from kodo_core.db.audit_trail import record_audit_event
        record_audit_event(cursor, "CARTE_CADEAU_EMISE", "Cartes_Cadeaux", final_code, emis_par or "", "EMISSION",
                            f"Montant initial {montant_dec} EUR" + (f" - {notes}" if notes else ""))
    except Exception:
        pass

    return {
        "code": final_code,
        "initialAmount": float(montant_dec),
        "remainingAmount": float(montant_dec),
    }


def lister_cartes_cadeaux(conn=None):
    """Liste toutes les cartes cadeaux/avoirs émis, soldes actuels inclus."""
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, code, solde_initial, solde_actuel, date_creation, notes, client_id, client_nom, status
            FROM Cartes_Cadeaux ORDER BY id DESC
        """)
        return [
            {
                "id": str(r[0]),
                "code": r[1],
                "initialAmount": float(r[2]),
                "remainingAmount": float(r[3]),
                "createdAt": r[4],
                "notes": r[5],
                "clientId": r[6],
                "clientName": r[7],
                "status": r[8] or "active",
            }
            for r in c.fetchall()
        ]
    finally:
        if should_close:
            conn.close()


def utiliser_carte_cadeau(cursor, code, montant_a_utiliser, user_name=None):
    """Débite une carte cadeau/avoir pour un règlement en caisse (NF525).

    Sans cette vérification serveur, sélectionner « Avoir » comme moyen de paiement
    validait la vente pour n'importe quel code (même inventé, même vide), sans aucune
    contrepartie réelle ; et une carte valide pouvait être réutilisée indéfiniment car
    rien ne décrémentait jamais son solde. Le solde est relu et débité dans la MÊME
    transaction que la vente : si la vente échoue derrière, tout est annulé ensemble.
    """
    code_norm = str(code or "").strip().upper()
    if not code_norm:
        raise ValueError("Code de carte cadeau/avoir manquant.")

    montant_dec = Decimal(str(montant_a_utiliser)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if montant_dec <= Decimal('0.00'):
        raise ValueError("Le montant à débiter sur la carte cadeau doit être strictement positif.")

    cursor.execute("SELECT id, solde_actuel, status FROM Cartes_Cadeaux WHERE code = ?", (code_norm,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"Carte cadeau/avoir introuvable : {code_norm}")

    carte_id, solde_actuel, status = row
    solde_dec = Decimal(str(solde_actuel))
    if (status or "active") != "active":
        raise ValueError(f"Carte cadeau/avoir {code_norm} invalidée (statut : {status}).")
    if solde_dec <= Decimal('0.00'):
        raise ValueError(f"Carte cadeau/avoir {code_norm} déjà épuisée (solde 0,00 €).")
    if montant_dec > solde_dec:
        raise ValueError(
            f"Solde insuffisant sur la carte {code_norm} : {solde_dec} € disponible pour {montant_dec} € demandé."
        )

    nouveau_solde = solde_dec - montant_dec
    cursor.execute("UPDATE Cartes_Cadeaux SET solde_actuel = ? WHERE id = ? AND solde_actuel = ?",
                   (float(nouveau_solde), carte_id, float(solde_actuel)))
    if cursor.rowcount != 1:
        # Le solde a changé entre la lecture et l'écriture (utilisation concurrente de la
        # même carte) : on refuse plutôt que de risquer un double-débit silencieux.
        raise ValueError(f"Conflit de mise à jour sur la carte {code_norm}, merci de réessayer.")

    try:
        from kodo_core.db.audit_trail import record_audit_event
        record_audit_event(cursor, "CARTE_CADEAU_UTILISEE", "Cartes_Cadeaux", code_norm, user_name or "", "DEBIT",
                            f"Débit {montant_dec} EUR - solde restant {nouveau_solde} EUR")
    except Exception:
        pass

    return montant_dec


def annuler_carte_cadeau(cursor, code):
    """Invalide une carte cadeau/avoir (mise à solde 0, statut 'annulee') sans jamais
    supprimer la ligne : une carte cadeau émise reste une pièce comptable, comme un
    ticket ou un mouvement de caisse."""
    code_norm = str(code or "").strip().upper()
    cursor.execute("SELECT id FROM Cartes_Cadeaux WHERE code = ?", (code_norm,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"Carte cadeau/avoir introuvable : {code_norm}")
    cursor.execute("UPDATE Cartes_Cadeaux SET solde_actuel = 0, status = 'annulee' WHERE id = ?", (row[0],))
    try:
        from kodo_core.db.audit_trail import record_audit_event
        record_audit_event(cursor, "CARTE_CADEAU_ANNULEE", "Cartes_Cadeaux", code_norm, "", "ANNULATION", "")
    except Exception:
        pass


def sauvegarder_panier_en_attente(panier, total_tvac, client_id=None, client_nom=None, remise=Decimal('0.00'), note="", conn=None):
    from kodo_core.domain.sales.cart_engine import park_cart
    return park_cart(panier, float(total_tvac), client_id, client_nom or "", float(remise), note, conn=conn)

def lister_paniers_en_attente(conn=None):
    from kodo_core.domain.sales.cart_engine import get_parked_carts
    return get_parked_carts(conn=conn)

def recuperer_panier_en_attente(panier_id, conn=None):
    from kodo_core.domain.sales.cart_engine import restore_parked_cart
    return restore_parked_cart(panier_id, conn=conn)

def supprimer_panier_en_attente(panier_id, conn=None):
    from kodo_core.domain.sales.cart_engine import delete_parked_cart
    return delete_parked_cart(panier_id, conn=conn)

def generer_bilan_z_journalier(caisse_id="POS-01", conn=None):
    """Agrège les ventes/mouvements non encore comptés dans un Z, pour UNE caisse donnée.

    Le filtrage se fait par (caisse_id, z_id IS NULL) plutôt que par une comparaison de
    date : une comparaison de date sur `Ledger_Caisse`/`Tickets` ignorait `caisse_id`,
    ce qui provoquait un double comptage dès qu'il y a plus d'une caisse (chaque Z
    recomptait les ventes des autres caisses). Le marquage z_id (posé par
    enregistrer_cloture_caisse dans la même transaction que l'INSERT Clotures_Caisse)
    garantit qu'une vente ou un mouvement n'est jamais compté deux fois, même en cas de
    clôtures rapprochées ou de plusieurs caisses.
    """
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        c = conn.cursor()

        c.execute("""
            SELECT id, total_tvac, total_htva, total_tva, remise
            FROM Tickets WHERE caisse_id = ? AND z_id IS NULL
        """, (caisse_id,))
        ticket_rows = c.fetchall()

        ticket_ids = [row[0] for row in ticket_rows]
        nb_tickets = len(ticket_ids)
        tot_tvac = sum((Decimal(str(row[1] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_htva = sum((Decimal(str(row[2] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_tva = sum((Decimal(str(row[3] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_remises = sum((Decimal(str(row[4] or "0.00")) for row in ticket_rows), Decimal("0.00"))

        # Seuls les mouvements de VENTE/REMBOURSEMENT alimentent le chiffre d'affaires
        # espèces/carte : sans ce filtre, tout autre type de mouvement présent dans
        # Ledger_Caisse (ex. apports/prélèvements de caisse) serait compté à tort comme
        # du chiffre d'affaires, gonflant ou faussant total_especes/total_carte.
        c.execute("""
            SELECT id, methode_paiement, montant
            FROM Ledger_Caisse WHERE caisse_id = ? AND z_id IS NULL AND type_mouvement IN ('VENTE', 'REMBOURSEMENT')
        """, (caisse_id,))
        ledger_rows = c.fetchall()

        ledger_ids = [row[0] for row in ledger_rows]
        tot_esp = Decimal("0.00")
        tot_carte = Decimal("0.00")
        tot_qr = Decimal("0.00")
        tot_avoir = Decimal("0.00")

        # Classification explicite par moyen de paiement : un split binaire espèces/carte
        # (tout ce qui n'est pas "espèces" tombait dans "carte") gonflait à tort le total
        # carte bancaire avec les paiements QR et les remboursements/débits en Avoir
        # (carte cadeau) — aucun de ces deux n'a jamais transité par le terminal CB.
        for _id, m, mt in ledger_rows:
            mt_dec = Decimal(str(mt or "0.00"))
            methode = str(m or "").strip().lower()
            if methode in ("espèces", "especes", "cash"):
                tot_esp += mt_dec
            elif methode == "qr":
                tot_qr += mt_dec
            elif methode in ("avoir", "carte cadeau", "carte_cadeau", "gift card", "giftcard"):
                tot_avoir += mt_dec
            else:
                tot_carte += mt_dec

        # Apports/prélèvements de la période en cours (non encore clôturés) : suivis
        # séparément du chiffre d'affaires pour ne pas fausser total_especes/total_carte,
        # mais nécessaires pour calculer le théorique caisse (fond + ventes + mouvements).
        c.execute("""
            SELECT id, type_mouvement, montant
            FROM Ledger_Caisse WHERE caisse_id = ? AND z_id IS NULL AND type_mouvement IN ('APPORT', 'PRELEVEMENT')
        """, (caisse_id,))
        mouvement_rows = c.fetchall()

        mouvement_ids = [row[0] for row in mouvement_rows]
        tot_apports = Decimal("0.00")
        tot_prelevements = Decimal("0.00")
        for _id, tm, mt in mouvement_rows:
            mt_dec = Decimal(str(mt or "0.00"))
            if tm == "APPORT":
                tot_apports += mt_dec
            elif tm == "PRELEVEMENT":
                tot_prelevements += mt_dec

        return {
            "caisse_id": caisse_id,
            "nb_tickets": nb_tickets,
            "total_tvac": tot_tvac,
            "total_htva": tot_htva,
            "total_tva": tot_tva,
            "total_remises": tot_remises,
            "total_especes": tot_esp,
            "total_carte": tot_carte,
            "total_qr": tot_qr,
            "total_avoir": tot_avoir,
            "total_apports": tot_apports,
            "total_prelevements": tot_prelevements,
            "ticket_ids": ticket_ids,
            "ledger_ids": ledger_ids + mouvement_ids,
        }
    finally:
        if should_close:
            conn.close()

def enregistrer_cloture_caisse(caisse_id="POS-01", fond_caisse_reel=Decimal("0.00"), fond_caisse_matin=Decimal("0.00"), vendeur="Admin", conn=None):
    from audit_trail import calculer_hash_cloture

    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        c = conn.cursor()
        bilan = generer_bilan_z_journalier(caisse_id, conn=conn)

        c.execute("SELECT current_hash FROM Clotures_Caisse WHERE caisse_id=? ORDER BY id DESC LIMIT 1", (caisse_id,))
        last_row = c.fetchone()
        hash_prec = last_row[0] if last_row and last_row[0] else "GENESIS_Z_00000000000000000000000000000000"

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_utc = datetime.datetime.utcnow().isoformat() + "Z"

        # L'écart caisse compare le comptage PHYSIQUE COMPLET du tiroir (billets + pièces,
        # fond initial inclus) au théorique attendu (fond initial + ventes espèces du jour
        # + apports - prélèvements). Comparer fond_caisse_reel directement à total_especes
        # (sans le fond initial) faisait apparaître un écart artificiellement gonflé du
        # montant exact du fond de caisse, à chaque clôture.
        fond_reel_dec = Decimal(str(fond_caisse_reel))
        fond_matin_dec = Decimal(str(fond_caisse_matin))
        theorique_especes = fond_matin_dec + bilan["total_especes"] + bilan["total_apports"] - bilan["total_prelevements"]
        ecart = (fond_reel_dec - theorique_especes).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

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
        z_id = c.lastrowid

        # Marquage atomique (même transaction que l'INSERT ci-dessus) des tickets et
        # mouvements de caisse inclus dans ce Z, pour empêcher qu'un Z ultérieur (sur
        # cette caisse ou une autre) ne les recompte.
        if bilan["ticket_ids"]:
            c.executemany(
                "UPDATE Tickets SET z_id = ? WHERE id = ?",
                [(z_id, tid) for tid in bilan["ticket_ids"]],
            )
        if bilan["ledger_ids"]:
            c.executemany(
                "UPDATE Ledger_Caisse SET z_id = ? WHERE id = ?",
                [(z_id, lid) for lid in bilan["ledger_ids"]],
            )

        conn.commit()
        print(f"[Z DE CAISSE] ✅ Clôture enregistrée avec succès. Hash: {curr_hash[:16]}...")
        return {
            "date": now_str,
            "current_hash": curr_hash,
            "total_tvac": float(bilan["total_tvac"]),
            "ecart": float(ecart)
        }
    finally:
        if should_close:
            conn.close()
