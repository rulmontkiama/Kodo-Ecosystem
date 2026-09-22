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

# AUCUNE migration automatique à l'import de ce module.
#
# Un bloc précédent copiait ici le fichier *.db le plus récemment modifié du répertoire
# par-dessus la base de production, au simple import, sans sauvegarde et sans journal.
# Mesuré : en présence d'une vraie base de 42 articles et d'un fichier quelconque plus
# récent, c'est le fichier quelconque qui était adopté comme base du commerce.
# La reprise d'une base héritée est une opération métier explicite, elle vit dans
# migrer_base_heritee() et n'est jamais déclenchée par un import.

# Noms de bases héritées reconnus. Liste EXPLICITE : jamais de glob sauvage, jamais de
# sélection par date de modification — un fichier .db présent dans le répertoire n'est
# pas une présomption de base du commerce.
BASES_HERITEES_CONNUES = (
    "legacy_pos.db",
    "pilot_store.db",
    "v1_kodo.db",
)
# Une base portant un autre nom se reprend en le déclarant dans l'environnement de la
# machine concernée (KODO_LEGACY_DB_NAME), jamais en inscrivant quoi que ce soit ici.
# Aucune empreinte de nom n'est stockée : le condensé SHA-256 d'un nom de fichier ne
# l'anonymise pas. Un nom de fichier n'est pas un secret — l'espace des candidats
# plausibles est minuscule et le nom se retrouve au premier essai. Le code livré aux
# clients ne doit porter aucune trace, même dérivée, du nom d'une boutique cliente.

def migrer_base_heritee(db_path: str = None, dry_run: bool = True) -> dict:
    """
    Reprend une base héritée vers kodo_pos.db, une seule fois, de façon vérifiée.
    Opération explicite : aucun appel depuis un import de module. Par défaut en
    simulation (dry_run=True) — l'appelant doit demander l'écriture sciemment.

    Garanties : la cible n'est jamais écrasée si elle existe ; la source doit porter
    le schéma Kōdo ; la copie passe par l'API sqlite3.backup() (jamais shutil, jamais
    de -wal/-shm copiés à la main) ; le résultat est relu avant d'être retenu ; toute
    anomalie est remontée à l'appelant, jamais avalée.
    """
    cible = db_path or DB_NAME
    rapport = {"migre": False, "source": None, "raison": "", "produits": 0}

    if os.path.exists(cible):
        rapport["raison"] = "kodo_pos.db existe déjà : aucune reprise n'est tentée."
        return rapport

    base_dir = os.path.dirname(cible)
    env_legacy = os.environ.get("KODO_LEGACY_DB_NAME")
    allowed_names = set(BASES_HERITEES_CONNUES)
    if env_legacy:
        allowed_names.add(env_legacy)

    sources = []
    if os.path.exists(base_dir):
        for fname in os.listdir(base_dir):
            if fname == os.path.basename(cible):
                continue
            if fname in allowed_names:
                sources.append(os.path.join(base_dir, fname))

    if not sources:
        rapport["raison"] = "Aucune base héritée connue dans le répertoire."
        return rapport

    if len(sources) > 1:
        rapport["raison"] = (
            f"Plusieurs bases héritées trouvées ({', '.join(os.path.basename(s) for s in sources)}) : "
            "reprise refusée, choix manuel requis."
        )
        return rapport

    source = sources[0]
    rapport["source"] = source

    # La source doit être une base Kōdo intègre, pas un fichier .db quelconque.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        row = src.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            rapport["raison"] = f"{os.path.basename(source)} : intégrité SQLite non confirmée, reprise refusée."
            return rapport

        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        requises = {"Produits", "Stocks", "Tickets", "Ledger_Caisse"}
        manquantes = requises - tables
        if manquantes:
            rapport["raison"] = (
                f"{os.path.basename(source)} n'est pas une base Kōdo "
                f"(tables absentes : {', '.join(sorted(manquantes))}), reprise refusée."
            )
            return rapport

        rapport["produits"] = int(src.execute("SELECT COUNT(*) FROM Produits").fetchone()[0] or 0)

        if dry_run:
            rapport["raison"] = (
                f"Simulation : {os.path.basename(source)} ({rapport['produits']} produits) "
                "serait repris. Relancer avec dry_run=False pour écrire."
            )
            return rapport

        # Copie transactionnelle par l'API native : ni shutil, ni -wal, ni -shm.
        tmp = cible + ".migration_tmp"
        if os.path.exists(tmp):
            os.remove(tmp)

        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Relecture de la copie avant de la retenir : une copie non vérifiée n'est pas une copie.
    verif = sqlite3.connect(tmp)
    try:
        row = verif.execute("PRAGMA integrity_check").fetchone()
        n = int(verif.execute("SELECT COUNT(*) FROM Produits").fetchone()[0] or 0)
    finally:
        verif.close()

    if not row or row[0] != "ok" or n != rapport["produits"]:
        os.remove(tmp)
        rapport["raison"] = (
            f"Copie non conforme (intégrité={row[0] if row else '?'}, "
            f"produits {n} au lieu de {rapport['produits']}) : reprise annulée."
        )
        return rapport

    os.rename(tmp, cible)
    rapport["migre"] = True
    rapport["raison"] = (
        f"Base héritée {os.path.basename(source)} reprise : {n} produits. "
        f"Le fichier d'origine est conservé intact."
    )
    print(f"🗄️ [KODO POS] {rapport['raison']}")
    return rapport

# Adaptateur et convertisseur pour utiliser Decimal avec SQLite
def adapt_decimal(d):
    return str(d)

def convert_decimal(s):
    return Decimal(s.decode('utf-8'))

sqlite3.register_adapter(Decimal, adapt_decimal)
sqlite3.register_converter("DECIMAL", convert_decimal)


def hash_pin_sha256(pin_plain, salt=None):
    """Ancien hachage SHA-256 avec sel statique (conservé pour rétrocompatibilité)."""
    if not pin_plain:
        return ""
    import hashlib
    s = salt or "KODO_POS_SECURE_SALT_2026"
    return hashlib.sha256((str(pin_plain) + s).encode('utf-8')).hexdigest()


def hash_pin(pin_plain, salt=None):
    """
    Génère un hachage PBKDF2-HMAC-SHA256 à 100 000 itérations (64 caractères hex déterministes).
    Conforme aux recommandations de sécurité OWASP / NIST pour les codes PIN.
    """
    if not pin_plain:
        return ""
    import hashlib
    s = salt or "KODO_POS_SECURE_SALT_2026"
    return hashlib.pbkdf2_hmac('sha256', (str(pin_plain) + s).encode('utf-8'), s.encode('utf-8'), 100_000).hex()


def verify_pin_hash(pin_plain, stored_hash, salt=None):
    """
    Vérifie un code PIN contre une empreinte stockée en BDD.
    Supporte PBKDF2 (format courant) et SHA-256 (format historique).
    Retourne (is_valid: bool, needs_rehash: bool).
    """
    if not pin_plain or not stored_hash:
        return False, False
    import hmac
    s = salt or "KODO_POS_SECURE_SALT_2026"
    # 1. Vérification PBKDF2 (courant)
    pbkdf2_h = hash_pin(pin_plain, s)
    if hmac.compare_digest(pbkdf2_h, stored_hash):
        return True, False
    # 2. Vérification SHA-256 (hérité)
    legacy_h = hash_pin_sha256(pin_plain, s)
    if hmac.compare_digest(legacy_h, stored_hash):
        return True, True
    return False, False


class SafeConnection:
    """Wrapper ultra-sécurisé pour garantir la fermeture des connexions et la résilience concurrente."""
    def __init__(self, db_name, **kwargs):
        self.db_name = db_name
        self._closed = False
        if "timeout" not in kwargs:
            kwargs["timeout"] = 5.0
        self._conn = sqlite3.connect(db_name, **kwargs)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            pass

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def __getattr__(self, name):
        return getattr(self._conn, name)

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
        conn.execute("BEGIN IMMEDIATE")
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
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'en_solde' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN en_solde INTEGER DEFAULT 0")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'prix_solde_tvac' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN prix_solde_tvac DECIMAL DEFAULT NULL")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'type_vente' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN type_vente TEXT DEFAULT 'unite'")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'unite_mesure' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN unite_mesure TEXT DEFAULT 'pce'")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'marque' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN marque TEXT DEFAULT NULL")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'attributs_json' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN attributs_json TEXT DEFAULT NULL")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'sync_status' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN sync_status INTEGER DEFAULT 0")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'seuil_alerte' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN seuil_alerte INTEGER DEFAULT 5")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'requires_stock_audit' not in cols_produits:
        try: cursor.execute("ALTER TABLE Produits ADD COLUMN requires_stock_audit INTEGER DEFAULT 0")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass

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
            requires_stock_audit INTEGER DEFAULT 0,
            FOREIGN KEY (id_produit) REFERENCES Produits(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute("PRAGMA table_info(Stocks)")
    cols_stocks = [row[1] for row in cursor.fetchall()]
    if 'requires_stock_audit' not in cols_stocks:
        try: cursor.execute("ALTER TABLE Stocks ADD COLUMN requires_stock_audit INTEGER DEFAULT 0")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass

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
    for col, col_type in [
        ('caisse_id', "TEXT DEFAULT 'POS-01'"),
        ('details_articles', "TEXT"),
        ('previous_hash', "TEXT"),
        ('current_hash', "TEXT"),
        ('sync_status', "INTEGER DEFAULT 1"),
        ('offline_uuid', "TEXT"),
        ('created_at_utc', "TEXT"),
        ('z_id', "INTEGER DEFAULT NULL"),
        ('ecart_arrondi_cash', "DECIMAL DEFAULT '0.00'"),
    ]:
        if col not in cols_tickets:
            try: cursor.execute(f"ALTER TABLE Tickets ADD COLUMN {col} {col_type}")
            except (sqlite3.OperationalError, sqlite3.DatabaseError): pass

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_unique_shopify_order_id
        ON Tickets(shopify_order_id)
        WHERE shopify_order_id IS NOT NULL AND shopify_order_id != ''
    """)

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
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass

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
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass
    if 'z_id' not in cols_ledger:
        try: cursor.execute("ALTER TABLE Ledger_Caisse ADD COLUMN z_id INTEGER DEFAULT NULL")
        except (sqlite3.OperationalError, sqlite3.DatabaseError): pass

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

    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('pin_admin', ?)", (hash_pin('0000'),))
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_name', 'Kōdo POS')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_tva', '0.21')")
    cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_seuil_alerte', '5')")

    # Migration des PINs en clair existants vers leur hachage sécurisé
    cursor.execute("SELECT id, pin FROM Vendeurs")
    for vid, pin in cursor.fetchall():
        if pin and len(pin) == 4 and pin.isdigit():
            hashed = hash_pin(pin)
            cursor.execute("SELECT COUNT(*) FROM Vendeurs WHERE pin = ?", (hashed,))
            if cursor.fetchone()[0] == 0:
                cursor.execute("UPDATE Vendeurs SET pin = ? WHERE id = ?", (hashed, vid))
            else:
                import random
                temp_pin = f"TEMP_{random.randint(1000, 9999)}"
                cursor.execute("UPDATE Vendeurs SET pin = ? WHERE id = ?", (temp_pin, vid))

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


# Versions de l'algorithme de chaînage fiscal.
# v1 (historique) : previous|timestamp|montant|caisse|details — sans numéro ni secret.
# v2 (transition) : "v2"|previous|timestamp|montant|caisse|numero_ticket|details — scelle le ticket.
# v3 (courant HMAC) : "v3"|previous|timestamp|montant|caisse|numero_ticket|details — scellé par HMAC machine.
# L'algorithme n'est JAMAIS réappliqué rétroactivement : les chaînes déjà constituées restent vérifiables.
HASH_ALGO_V1 = "v1"
HASH_ALGO_V2 = "v2"
HASH_ALGO_V3 = "v3"
HASH_ALGO_COURANT = HASH_ALGO_V3


def get_audit_machine_secret() -> bytes:
    """
    Retourne la clé secrète locale pour le scellement HMAC de la chaîne d'audit.
    Stockée dans ~/.kodo_signing/audit_hmac.key (chmod 0600) ou dérivée de façon déterministe.
    """
    try:
        secret_dir = os.path.expanduser("~/.kodo_signing")
        secret_path = os.path.join(secret_dir, "audit_hmac.key")
        if os.path.exists(secret_path):
            with open(secret_path, "rb") as f:
                key = f.read().strip()
                if len(key) >= 16:
                    return key
        os.makedirs(secret_dir, exist_ok=True)
        new_key = os.urandom(32).hex().encode('utf-8')
        with open(secret_path, "wb") as f:
            f.write(new_key)
        try:
            os.chmod(secret_path, 0o600)
        except Exception:
            pass
        return new_key
    except Exception:
        import hashlib
        return hashlib.sha256(b"KODO_POS_AUDIT_HMAC_SECRET_2026").digest()


def calculer_hash_transaction(previous_hash, timestamp, montant_total, caisse_id="POS-01", details_articles="", numero_ticket=None, algo=HASH_ALGO_COURANT, secret_key=None):
    """
    Calcule l'empreinte chaînée d'une transaction de vente (Audit Trail).
    - HASH_ALGO_V1 : SHA-256 historique (sans numéro de ticket, sans secret).
    - HASH_ALGO_V2 : SHA-256 scellant le numéro de ticket (sans secret).
    - HASH_ALGO_V3 : HMAC-SHA256 scellant le numéro de ticket avec la clé secrète machine locale.
    """
    import hashlib
    import hmac
    prev_str = str(previous_hash or "GENESIS_BLOCK_KODO_POS")
    ts_str = str(timestamp or "")
    montant_str = f"{Decimal(str(montant_total)):.2f}"
    caisse_str = str(caisse_id or "POS-01")
    details_str = str(details_articles or "")

    if algo == HASH_ALGO_V1:
        data = f"{prev_str}|{ts_str}|{montant_str}|{caisse_str}|{details_str}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    elif algo == HASH_ALGO_V2:
        num_str = str(numero_ticket or "")
        data = (f"{HASH_ALGO_V2}|{prev_str}|{ts_str}|{montant_str}|"
                f"{caisse_str}|{num_str}|{details_str}")
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    else:
        num_str = str(numero_ticket or "")
        data = (f"{HASH_ALGO_V3}|{prev_str}|{ts_str}|{montant_str}|"
                f"{caisse_str}|{num_str}|{details_str}")
        key = secret_key or get_audit_machine_secret()
        if isinstance(key, str):
            key = key.encode('utf-8')
        return hmac.new(key, data.encode('utf-8'), hashlib.sha256).hexdigest()


def signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id="POS-01", details_articles=""):
    """Génère l'empreinte chaînée d'un ticket avec son numéro scellé (algorithme courant)."""
    cursor.execute(
        "SELECT COALESCE(current_hash, signature) FROM Tickets "
        "WHERE current_hash IS NOT NULL OR signature IS NOT NULL ORDER BY id DESC LIMIT 1"
    )
    row = cursor.fetchone()
    previous_hash = row[0] if (row and row[0]) else "GENESIS_BLOCK_KODO_POS"
    current_hash = calculer_hash_transaction(
        previous_hash,
        date_heure,
        total_tvac,
        caisse_id,
        details_articles,
        numero_ticket=numero_ticket,
        algo=HASH_ALGO_COURANT,
    )
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


def enregistrer_vente(cursor, numero_ticket, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, panier, vendeur_nom, date_heure, paiements, caisse_id="POS-01", sync_status=1, offline_uuid=None, created_at_utc=None, ecart_arrondi_cash=0.0):
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

    if numero_ticket:
        cursor.execute("SELECT id FROM Tickets WHERE numero_ticket = ?", (numero_ticket,))
        existing_tck = cursor.fetchone()
        if existing_tck:
            print(f"[IDEMPOTENCE] Ticket {numero_ticket} déjà existant (id={existing_tck[0]}). Aucun doublon créé.")
            return existing_tck[0]

    current_hash, previous_hash = signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id=caisse_id, details_articles=details_articles)

    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, caisse_id, details_articles, signature, hash_precedent, previous_hash, current_hash, sync_status, offline_uuid, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, id_client, rendu_monnaie, caisse_id, details_articles, current_hash, previous_hash, previous_hash, current_hash, sync_status, offline_uuid, created_at_utc))

    ticket_id = cursor.lastrowid

    if ecart_arrondi_cash != 0.0:
        try:
            cursor.execute("UPDATE Tickets SET ecart_arrondi_cash = ? WHERE id = ?", (float(ecart_arrondi_cash), ticket_id))
        except Exception:
            pass

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

        # Si un stock_id est fourni mais n'existe pas en base (ex: données mock de test, article hors stock géré),
        # on évite de violer la contrainte FOREIGN KEY (id_stock) REFERENCES Stocks(id)
        if s_id is not None:
            cursor.execute("SELECT 1 FROM Stocks WHERE id = ?", (s_id,))
            if not cursor.fetchone():
                s_id = None

        cursor.execute("""
            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
            VALUES (?, ?, ?, ?)
        """, (ticket_id, s_id, qty, px))
        if s_id:
            cursor.execute(
                "UPDATE Stocks SET quantite_actuelle = quantite_actuelle - ? "
                "WHERE id = ? AND quantite_actuelle >= ?",
                (qty, s_id, qty),
            )
            if cursor.rowcount == 0:
                # Vente hors-ligne rejouée sur un stock déjà épuisé (Last-Write-Wins) :
                # on ne rejette pas une vente physiquement conclue, mais on interdit au
                # compteur de passer sous zéro et on trace l'incident pour audit.
                try:
                    cursor.execute(
                        "UPDATE Stocks SET quantite_actuelle = 0, requires_stock_audit = 1 WHERE id = ? AND quantite_actuelle < ?",
                        (s_id, qty),
                    )
                    cursor.execute(
                        "UPDATE Produits SET requires_stock_audit = 1 WHERE id = (SELECT id_produit FROM Stocks WHERE id = ?)",
                        (s_id,)
                    )
                except Exception:
                    cursor.execute(
                        "UPDATE Stocks SET quantite_actuelle = 0 WHERE id = ? AND quantite_actuelle < ?",
                        (s_id, qty),
                    )
                print(
                    f"[STOCK AUDIT] Survente détectée sur stock_id={s_id} "
                    f"(demandé={qty}) — stock plafonné à 0, ticket {numero_ticket}."
                )

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


def enregistrer_remboursement(cursor, ticket_origine, vd_id, stock_id, prix, mode, vendeur_nom, date_heure, quantite=1, caisse_id="POS-01", recrediter_stock=True):
    """Enregistre un remboursement (NF525).

    Le prix, le taux de TVA et le stock à recréditer sont TOUJOURS relus depuis la
    ligne de vente d'origine (`Ventes_Details`/`Produits`) plutôt que fournis par
    l'appelant : un `prix` ou `stock_id` arbitraire transmis par le client ne sont
    jamais utilisés pour le calcul financier réel. `ticket_origine` est vérifié
    contre le ticket réellement propriétaire de `vd_id`, et la quantité déjà
    remboursée pour cette ligne (via `refund_of_vd_id`) est déduite pour empêcher
    tout remboursement en double ou au-delà de la quantité effectivement vendue.

    `recrediter_stock=False` enregistre le remboursement financier SANS remettre
    l'article en rayon. Seule la synchronisation Shopify s'en sert, pour honorer le
    `restock_type: "no_restock"` d'un remboursement fait en ligne : la cliente est
    remboursée mais l'article ne revient pas au stock vendable (abîmé, ou conservé).
    Le recrédit reste le défaut, donc le comportement de la caisse est inchangé ; la
    ligne négative de `Ventes_Details` est écrite dans les DEUX cas, sans quoi le
    garde-fou anti-double-remboursement perdrait sa trace.
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
    base_tk = f"REF-{numero_ticket_origine}-{timestamp_suffix}"

    # Le suffixe n'est que l'horodatage tronqué à 5 chiffres : deux remboursements de
    # lignes DIFFÉRENTES d'un même ticket d'origine produisaient le même numéro s'ils
    # tombaient dans la même seconde (cas banal : la vendeuse rembourse deux articles
    # d'affilée, ou le serveur HTTP multi-threadé traite deux requêtes en parallèle), et
    # l'horodatage tronqué reboucle en plus toutes les 100 000 s (~27 h 46). L'INSERT
    # violait alors la contrainte UNIQUE et le remboursement ÉCHOUAIT — alors que l'argent
    # avait déjà été rendu à la cliente. Le chemin vente était, lui, protégé par un retry
    # borné (cf. `process_sale_transaction`) ; le chemin remboursement avait été oublié.
    # On est ici sous BEGIN IMMEDIATE (cf. `process_return_transaction`) : aucun autre
    # écrivain ne peut s'intercaler entre ce SELECT et l'INSERT, donc choisir un numéro
    # libre suffit — pas de boucle de retry, et le ticket n'est signé qu'une seule fois.
    # La contrainte UNIQUE reste le garde-fou ultime contre tout doublon.
    new_tk = base_tk
    discriminant = 1
    while True:
        cursor.execute("SELECT 1 FROM Tickets WHERE numero_ticket = ?", (new_tk,))
        if cursor.fetchone() is None:
            break
        discriminant += 1
        new_tk = f"{base_tk}-{discriminant}"

    signature, hash_prec = signer_ticket(cursor, new_tk, total_tvac, date_heure, caisse_id=caisse_id)

    cursor.execute("""
        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, remise, methode_paiement, caisse_id, signature, hash_precedent)
        VALUES (?, ?, ?, ?, ?, 0.00, ?, ?, ?, ?)
    """, (new_tk, date_heure, total_tvac, total_htva, total_tva, f"REMB ({mode})", caisse_id, signature, hash_prec))

    ticket_id = cursor.lastrowid

    if id_stock_origine:
        if recrediter_stock:
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

_MODES_ESPECES = ("espèces", "especes", "cash")
_MODES_QR = ("qr", "qr_code", "qr code", "qrcode", "virement")
_MODES_AVOIR = ("avoir", "carte cadeau", "carte_cadeau", "gift card", "giftcard")


def classer_moyen_paiement(methode):
    """Range un libellé de moyen de paiement dans 'especes' | 'qr' | 'avoir' | 'carte'.

    Les anciennes versions de la caisse enregistrent le QR sous "QR_Code" (et la carte sous
    "Bancontact") : sans cette normalisation partagée, tout ce qui n'était pas exactement
    "qr" tombait dans "carte" et gonflait à tort le total carte bancaire de la clôture Z.
    """
    m = str(methode or "").strip().lower()
    if m in _MODES_ESPECES:
        return "especes"
    if m in _MODES_QR:
        return "qr"
    if m in _MODES_AVOIR:
        return "avoir"
    return "carte"


def lister_jours_non_clotures(caisse_id="POS-01", conn=None):
    """Jours (AAAA-MM-JJ) ayant des tickets non encore clôturés, du plus ancien au plus récent."""
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    try:
        c = conn.cursor()
        c.execute("""
            SELECT substr(date_heure, 1, 10) AS jour, COUNT(*), COALESCE(SUM(total_tvac), 0)
            FROM Tickets WHERE caisse_id = ? AND z_id IS NULL
            GROUP BY jour ORDER BY jour ASC
        """, (caisse_id,))
        return [
            {"jour": r[0], "nb_tickets": r[1], "total_tvac": float(Decimal(str(r[2] or "0")).quantize(Decimal("0.01")))}
            for r in c.fetchall()
        ]
    finally:
        if should_close:
            conn.close()


def generer_bilan_z_journalier(caisse_id="POS-01", conn=None, jusqu_au=None):
    """Agrège les ventes/mouvements non encore comptés dans un Z, pour UNE caisse donnée.

    `jusqu_au` (AAAA-MM-JJ, optionnel) limite le bilan aux tickets/mouvements datés au plus de ce
    jour INCLUS. Comme tout ce qui est antérieur est toujours inclus, on ne peut jamais « sauter »
    un jour : la séquence des Z reste continue et chronologique (exigence NF525). Sans `jusqu_au`,
    tout ce qui n'est pas encore clôturé est compté (comportement historique).

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

        filtre_date = ""
        params_date = ()
        if jusqu_au:
            filtre_date = " AND substr(date_heure, 1, 10) <= ?"
            params_date = (str(jusqu_au),)

        c.execute("PRAGMA table_info(Tickets)")
        cols_tickets = [row[1] for row in c.fetchall()]
        has_arrondi = "ecart_arrondi_cash" in cols_tickets

        sql_tickets = f"""
            SELECT id, total_tvac, total_htva, total_tva, remise, numero_ticket{", COALESCE(ecart_arrondi_cash, 0.0)" if has_arrondi else ", 0.0"}
            FROM Tickets WHERE caisse_id = ? AND z_id IS NULL""" + filtre_date
        c.execute(sql_tickets, (caisse_id,) + params_date)
        ticket_rows = c.fetchall()

        ticket_ids = [row[0] for row in ticket_rows]
        nb_tickets = len(ticket_ids)
        tot_tvac = sum((Decimal(str(row[1] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_htva = sum((Decimal(str(row[2] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_tva = sum((Decimal(str(row[3] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_remises = sum((Decimal(str(row[4] or "0.00")) for row in ticket_rows), Decimal("0.00"))
        tot_arrondi_cash = sum((Decimal(str(row[6] or "0.00")) for row in ticket_rows), Decimal("0.00"))

        # Seuls les mouvements de VENTE/REMBOURSEMENT alimentent le chiffre d'affaires
        # espèces/carte : sans ce filtre, tout autre type de mouvement présent dans
        # Ledger_Caisse (ex. apports/prélèvements de caisse) serait compté à tort comme
        # du chiffre d'affaires, gonflant ou faussant total_especes/total_carte.
        c.execute("""
            SELECT id, methode_paiement, montant, reference, type_mouvement
            FROM Ledger_Caisse WHERE caisse_id = ? AND z_id IS NULL AND type_mouvement IN ('VENTE', 'REMBOURSEMENT')""" + filtre_date, (caisse_id,) + params_date)
        ledger_rows = c.fetchall()

        ledger_ids = [row[0] for row in ledger_rows]
        tot_esp = Decimal("0.00")
        tot_carte = Decimal("0.00")
        tot_qr = Decimal("0.00")
        tot_avoir = Decimal("0.00")

        # Classification explicite par moyen de paiement (cf. classer_moyen_paiement) : aucun
        # paiement QR ni avoir n'a jamais transité par le terminal CB.
        lignes_par_ticket = {}
        for _id, m, mt, ref, tm in ledger_rows:
            mt_dec = Decimal(str(mt or "0.00"))
            classe = classer_moyen_paiement(m)
            if classe == "especes":
                tot_esp += mt_dec
            elif classe == "qr":
                tot_qr += mt_dec
            elif classe == "avoir":
                tot_avoir += mt_dec
            else:
                tot_carte += mt_dec
            if tm == "VENTE":
                lignes_par_ticket.setdefault(ref, []).append((classe, mt_dec))

        # Régularisation du rendu de monnaie : d'anciennes versions déduisaient le rendu deux
        # fois dans le journal de caisse (montant encaissé = total - rendu au lieu du total).
        # Pour un ticket réglé UNIQUEMENT en espèces, l'encaissement réel attendu est le total
        # du ticket CORRIGÉ DE L'ARRONDI LÉGAL BELGE : sans ce terme, un arrondi vers le bas
        # (10,02 -> 10,00) était pris pour un rendu déduit deux fois, et les espèces du Z
        # étaient re-gonflées au montant brut — créant un écart de caisse purement fictif.
        regularisation_rendu = Decimal("0.00")
        for row in ticket_rows:
            total_tk = Decimal(str(row[1] or "0.00"))
            ecart_tk = Decimal(str(row[6] or "0.00"))
            encaisse_attendu = total_tk + ecart_tk
            lignes = lignes_par_ticket.get(row[5])
            if not lignes or encaisse_attendu <= Decimal("0.00"):
                continue
            if all(classe == "especes" for classe, _mt in lignes):
                manque = encaisse_attendu - sum((mt for _c, mt in lignes), Decimal("0.00"))
                if manque > Decimal("0.00"):
                    regularisation_rendu += manque
        tot_esp += regularisation_rendu

        # Le total encaissé légalement dû = CA brut + somme des écarts d'arrondi espèces.
        # Sans ce terme, l'arrondi légal apparaissait chaque jour comme une anomalie de
        # règlement, rendant impossible la détection d'un vrai écart de caisse.
        ecart_reglements = (
            (tot_tvac + tot_arrondi_cash) - (tot_esp + tot_carte + tot_qr + tot_avoir)
        ).quantize(Decimal("0.01"))

        # Apports/prélèvements de la période en cours (non encore clôturés) : suivis
        # séparément du chiffre d'affaires pour ne pas fausser total_especes/total_carte,
        # mais nécessaires pour calculer le théorique caisse (fond + ventes + mouvements).
        c.execute("""
            SELECT id, type_mouvement, montant
            FROM Ledger_Caisse WHERE caisse_id = ? AND z_id IS NULL AND type_mouvement IN ('APPORT', 'PRELEVEMENT')""" + filtre_date, (caisse_id,) + params_date)
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
            "total_arrondi_cash": tot_arrondi_cash,
            "regularisation_rendu": regularisation_rendu,
            "ecart_reglements": ecart_reglements,
            "jusqu_au": jusqu_au,
            "total_apports": tot_apports,
            "total_prelevements": tot_prelevements,
            "ticket_ids": ticket_ids,
            "ledger_ids": ledger_ids + mouvement_ids,
        }
    finally:
        if should_close:
            conn.close()

def _assurer_colonne_periode_z(cursor):
    """Ajoute à Clotures_Caisse les colonnes récentes si absentes : periode_jusqu_au (dernier jour
    couvert par le Z), total_qr, total_avoir et total_arrondi_cash. Ces colonnes ne font pas partie du hash de la chaîne NF525."""
    cursor.execute("PRAGMA table_info(Clotures_Caisse)")
    existantes = [row[1] for row in cursor.fetchall()]
    for col, ddl in (
        ("periode_jusqu_au", "TEXT"),
        ("total_qr", "DECIMAL DEFAULT '0.00'"),
        ("total_avoir", "DECIMAL DEFAULT '0.00'"),
        ("total_arrondi_cash", "DECIMAL DEFAULT '0.00'")
    ):
        if col not in existantes:
            cursor.execute(f"ALTER TABLE Clotures_Caisse ADD COLUMN {col} {ddl}")


def enregistrer_cloture_caisse(caisse_id="POS-01", fond_caisse_reel=Decimal("0.00"), fond_caisse_matin=Decimal("0.00"), vendeur="Admin", conn=None, jusqu_au=None):
    """Scelle un Z. `jusqu_au` (AAAA-MM-JJ) clôture au plus ce jour inclus (cf. generer_bilan_z_journalier).

    `fond_caisse_reel=None` = pas de comptage physique (rattrapage d'un ancien jour) : l'écart
    n'est alors pas applicable et vaut 0.
    """
    from audit_trail import calculer_hash_cloture

    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        c = conn.cursor()
        _assurer_colonne_periode_z(c)
        bilan = generer_bilan_z_journalier(caisse_id, conn=conn, jusqu_au=jusqu_au)

        c.execute("SELECT current_hash FROM Clotures_Caisse WHERE caisse_id=? ORDER BY id DESC LIMIT 1", (caisse_id,))
        last_row = c.fetchone()
        hash_prec = last_row[0] if last_row and last_row[0] else "GENESIS_Z_00000000000000000000000000000000"

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # L'écart caisse compare le comptage PHYSIQUE COMPLET du tiroir (billets + pièces,
        # fond initial inclus) au théorique attendu (fond initial + ventes espèces du jour
        # + apports - prélèvements). Comparer fond_caisse_reel directement à total_especes
        # (sans le fond initial) faisait apparaître un écart artificiellement gonflé du
        # montant exact du fond de caisse, à chaque clôture.
        fond_matin_dec = Decimal(str(fond_caisse_matin))
        theorique_especes = fond_matin_dec + bilan["total_especes"] + bilan["total_apports"] - bilan["total_prelevements"]
        if fond_caisse_reel is None:
            fond_reel_dec = theorique_especes.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            fond_reel_dec = Decimal(str(fond_caisse_reel))
        ecart = (fond_reel_dec - theorique_especes).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        curr_hash = calculer_hash_cloture(
            hash_prec, now_str, caisse_id,
            bilan["total_tvac"], bilan["total_especes"], bilan["total_carte"]
        )

        tot_arrondi = Decimal(str(bilan.get("total_arrondi_cash", "0.00")))
        c.execute("""
            INSERT INTO Clotures_Caisse (
                date_cloture, caisse_id, total_ventes_tvac, total_htva, total_tva,
                total_especes, total_carte, total_remises, total_tickets,
                fond_caisse_reel, ecart, vendeur, hash_precedent, current_hash, signature, created_at_utc,
                periode_jusqu_au, total_qr, total_avoir, total_arrondi_cash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_str, caisse_id, float(bilan["total_tvac"]), float(bilan["total_htva"]), float(bilan["total_tva"]),
            float(bilan["total_especes"]), float(bilan["total_carte"]), float(bilan["total_remises"]),
            bilan["nb_tickets"], float(fond_reel_dec), float(ecart), vendeur,
            hash_prec, curr_hash, curr_hash, now_utc, jusqu_au,
            float(bilan["total_qr"]), float(bilan["total_avoir"]), float(tot_arrondi)
        ))
        z_id = c.lastrowid

        # Clôture formelle de la session de caisse — UNIQUEMENT lorsque ce Z couvre la journée
        # en cours. Un Z de rattrapage sur un jour antérieur (clôture séquentielle jour par jour)
        # ne doit PAS fermer la session vivante : sinon le fond de caisse du matin disparaît pour
        # tous les jours suivants et le théorique espèces du Z suivant est faussé.
        if jusqu_au is None or str(jusqu_au) >= datetime.date.today().isoformat():
            c.execute("""
                UPDATE Sessions_Caisse 
                SET date_cloture = ?, montant_theorique_soir = ?, montant_compté_soir = ?, ecart_caisse = ?
                WHERE date_cloture IS NULL
            """, (now_str, float(theorique_especes), float(fond_reel_dec), float(ecart)))

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
            "nb_tickets": bilan["nb_tickets"],
            "jusqu_au": jusqu_au,
            "ecart": float(ecart),
            "total_arrondi_cash": float(tot_arrondi)
        }
    finally:
        if should_close:
            conn.close()
