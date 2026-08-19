# -*- coding: utf-8 -*-
"""
Façade de compatibilité database_manager pour Kōdo POS Core.
Assure la rétrocompatibilité complète vers kodo_core.domain.* et kodo_core.db.*.
"""

import sqlite3
from decimal import Decimal
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
        self._conn = sqlite3.connect(db_name, **kwargs)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
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
        MigrationManager.run_migrations(db_target)
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
            sync_status INTEGER DEFAULT 0
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
            FOREIGN KEY (id_client) REFERENCES Clients(id)
        )
    ''')

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
            hash_precedent TEXT
        )
    ''')

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
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_name', 'L''ADRESSE B')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_tva', '0.21')")

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
        if methode == "Espèces" and rendu_monnaie > 0:
            montant_reel -= Decimal(str(rendu_monnaie))

        sig_ledger, hash_ledger = signer_ledger(cursor, 'VENTE', montant_reel, methode, numero_ticket, date_heure)
        cursor.execute("""
            INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent)
            VALUES (?, 'VENTE', ?, ?, ?, ?, ?, ?)
        """, (vendeur_nom, float(montant_reel), methode, numero_ticket, date_heure, sig_ledger, hash_ledger))

    return ticket_id


def enregistrer_remboursement(cursor, ticket_origine, vd_id, stock_id, prix, mode, vendeur_nom, date_heure):
    """Enregistre un remboursement (NF525)."""
    cursor.execute("""
        SELECT p.taux_tva 
        FROM Ventes_Details vd
        JOIN Stocks s ON vd.id_stock = s.id
        JOIN Produits p ON s.id_produit = p.id
        WHERE vd.id = ?
    """, (vd_id,))
    row = cursor.fetchone()
    taux_tva = Decimal(str(row[0])) if row else Decimal('0.21')

    total_tvac = -Decimal(str(prix))
    total_htva = (total_tvac / (Decimal('1.00') + taux_tva)).quantize(Decimal('0.01'))
    total_tva = total_tvac - total_htva

    import time
    timestamp_suffix = str(int(time.time()))[-5:]
    new_tk = f"REF-{ticket_origine}-{timestamp_suffix}"

    signature, hash_prec = signer_ticket(cursor, new_tk, total_tvac, date_heure)

    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, signature, hash_precedent)
        VALUES (?, ?, ?, ?, ?, 0.00, ?, ?, ?)
    """, (new_tk, date_heure, total_tvac, total_htva, total_tva, f"REMB ({mode})", signature, hash_prec))

    ticket_id = cursor.lastrowid

    if stock_id:
        cursor.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle + 1 WHERE id = ?", (stock_id,))
        cursor.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, -1, ?)
        """, (ticket_id, stock_id, prix))

    sig_ledger, hash_ledger = signer_ledger(cursor, 'REMBOURSEMENT', total_tvac, mode, new_tk, date_heure)
    cursor.execute("""
        INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent)
        VALUES (?, 'REMBOURSEMENT', ?, ?, ?, ?, ?, ?)
    """, (vendeur_nom, float(total_tvac), mode, new_tk, date_heure, sig_ledger, hash_ledger))

    return new_tk


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
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        c = conn.cursor()

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

        if last_z_date:
            c.execute("SELECT methode_paiement, SUM(montant) FROM Ledger_Caisse WHERE date_heure > ? GROUP BY methode_paiement", (last_z_date,))
        else:
            c.execute("SELECT methode_paiement, SUM(montant) FROM Ledger_Caisse GROUP BY methode_paiement")

        ledger_rows = c.fetchall()
        tot_esp = Decimal("0.00")
        tot_carte = Decimal("0.00")

        for m, mt in ledger_rows:
            mt_dec = Decimal(str(mt or "0.00"))
            if m and str(m).lower() in ["espèces", "especes", "cash"]:
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
        if should_close:
            conn.close()

def enregistrer_cloture_caisse(caisse_id="POS-01", fond_caisse_reel=Decimal("0.00"), vendeur="Admin", conn=None):
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
            "total_tvac": float(bilan["total_tvac"]),
            "ecart": float(ecart)
        }
    finally:
        if should_close:
            conn.close()
