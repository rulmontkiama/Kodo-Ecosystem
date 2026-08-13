import sqlite3
import os
import shutil
import datetime
from core.config import ShopConfig

class MigrationError(Exception):
    """Exception levée en cas d'échec critique lors d'une migration de schéma."""
    pass

class MigrationManager:
    """Gestionnaire robuste et sécurisé de migrations de base de données SQLite avec rollback automatique."""
    
    # Registre des migrations incrémentales (Version: Liste d'instructions SQL ou fonctions)
    MIGRATIONS = [
        {
            "version": "1.0.0",
            "description": "Structure initiale Kōdo POS Core",
            "sql": [
                """CREATE TABLE IF NOT EXISTS Categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom TEXT UNIQUE NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS Produits (
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
                )""",
                """CREATE TABLE IF NOT EXISTS Stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_produit INTEGER,
                    taille TEXT,
                    quantite_actuelle INTEGER,
                    seuil_alerte INTEGER,
                    FOREIGN KEY (id_produit) REFERENCES Produits(id) ON DELETE CASCADE
                )""",
                """CREATE TABLE IF NOT EXISTS Clients (
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
                )""",
                """CREATE TABLE IF NOT EXISTS Vendeurs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom TEXT NOT NULL,
                    pin TEXT UNIQUE NOT NULL,
                    role_admin INTEGER DEFAULT 0
                )""",
                """CREATE TABLE IF NOT EXISTS Sessions_Caisse (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_ouverture TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fond_caisse_matin DECIMAL DEFAULT '0.00',
                    date_cloture TIMESTAMP,
                    montant_compté_soir DECIMAL,
                    montant_theorique_soir DECIMAL,
                    ecart_caisse DECIMAL DEFAULT '0.00'
                )""",
                """CREATE TABLE IF NOT EXISTS Depenses_Caisse (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    libelle TEXT,
                    montant DECIMAL,
                    moyen_paiement TEXT DEFAULT 'Espèces'
                )""",
                """CREATE TABLE IF NOT EXISTS Tickets (
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
                    signature TEXT,
                    hash_precedent TEXT,
                    FOREIGN KEY (id_client) REFERENCES Clients(id)
                )"""
            ]
        },
        {
            "version": "1.1.0",
            "description": "Multi-commerce, attributs dynamiques et ShopInfo",
            "sql": [
                "ALTER TABLE Produits ADD COLUMN type_vente TEXT DEFAULT 'unite'",
                "ALTER TABLE Produits ADD COLUMN unite_mesure TEXT DEFAULT 'pce'",
                "ALTER TABLE Produits ADD COLUMN marque TEXT DEFAULT NULL",
                "ALTER TABLE Produits ADD COLUMN attributs_json TEXT DEFAULT NULL",
                """CREATE TABLE IF NOT EXISTS ShopInfo (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom_magasin TEXT NOT NULL,
                    adresse TEXT,
                    telephone TEXT,
                    email TEXT,
                    siret_tva TEXT,
                    type_commerce TEXT DEFAULT 'pret_a_porter',
                    devise TEXT DEFAULT '€',
                    logo_path TEXT
                )""",
                """INSERT INTO ShopInfo (nom_magasin, adresse, siret_tva, type_commerce, devise)
                   SELECT "L'Adresse B", "Boutique Pilote", "BE 0123.456.789", "pret_a_porter", "€"
                   WHERE NOT EXISTS (SELECT 1 FROM ShopInfo)"""
            ]
        }
    ]

    @classmethod
    def get_applied_versions(cls, conn: sqlite3.Connection) -> list:
        """Retourne la liste des versions déjà appliquées."""
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cursor.execute("SELECT version FROM schema_version ORDER BY version ASC")
        return [row[0] for row in cursor.fetchall()]

    @classmethod
    def create_pre_migration_snapshot(cls, db_path: str) -> str:
        """Crée une sauvegarde physique complète avant migration."""
        if not os.path.exists(db_path):
            return ""
        
        snapshots_dir = ShopConfig.get_snapshots_dir()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_filename = f"kodo_pos_pre_migration_{timestamp}.db"
        snapshot_path = os.path.join(snapshots_dir, snapshot_filename)
        
        shutil.copy2(db_path, snapshot_path)
        return snapshot_path

    @classmethod
    def restore_snapshot(cls, db_path: str, snapshot_path: str):
        """Restaure physiquement la base de données à partir du snapshot."""
        if snapshot_path and os.path.exists(snapshot_path):
            shutil.copy2(snapshot_path, db_path)

    @classmethod
    def run_migrations(cls, db_path: str):
        """Exécute de façon atomique et sécurisée toutes les migrations manquantes."""
        conn = sqlite3.connect(db_path)
        applied = cls.get_applied_versions(conn)
        conn.close()

        pending = [m for m in cls.MIGRATIONS if m["version"] not in applied]
        if not pending:
            return

        # 1. Prise de snapshot de sécurité pré-migration
        snapshot_path = cls.create_pre_migration_snapshot(db_path)

        # 2. Exécution des migrations dans une transaction isolée
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            for migration in pending:
                version = migration["version"]
                for statement in migration["sql"]:
                    try:
                        cursor.execute(statement)
                    except sqlite3.OperationalError as oe:
                        # Tolérance aux colonnes déjà existantes lors d'une ré-exécution partiel
                        if "duplicate column name" not in str(oe).lower():
                            raise oe

                cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

            conn.commit()

            # 3. Healthcheck de la base post-migration
            cursor.execute("PRAGMA quick_check")
            check_res = cursor.fetchone()
            if not check_res or check_res[0].lower() != "ok":
                raise MigrationError(f"Contrôle d'intégrité échoué post-migration: {check_res}")

            conn.close()

        except Exception as e:
            if 'conn' in locals() and conn:
                try: conn.rollback(); conn.close()
                except Exception: pass
            
            # Restauration physique immédiate du snapshot
            if snapshot_path:
                cls.restore_snapshot(db_path, snapshot_path)
            
            raise MigrationError(f"Échec critique lors de la migration. Restauration du snapshot effectuée. Erreur : {e}")
