"""
kodo_core.db.migrations - Moteur de migration automatique de schémas, création de tables
et gestion des versions de base de données pour Kōdo POS.
"""

import sqlite3
import os
import shutil
import datetime
from decimal import Decimal
from kodo_core.config import ShopConfig
from kodo_core.db.connection import get_connection, hash_pin

class MigrationError(Exception):
    """Exception levée en cas d'erreur critique de migration de schéma."""
    pass

class MigrationManager:
    """
    Gestionnaire centralisé de migrations de schémas SQLite et d'initialisation usine.
    Garantit la création propre de toutes les tables, triggers, index et vues de compatibilité.
    """

    MIGRATIONS = [
        {
            "version": "1.0.0",
            "description": "Structure initiale complète de la base Kōdo POS Core (Tables, Indexes, Triggers)",
            "sql": [
                """CREATE TABLE IF NOT EXISTS Categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom TEXT UNIQUE NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS Marques (
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
                    prix_solde_tvac DECIMAL DEFAULT NULL,
                    type_vente TEXT DEFAULT 'unite',
                    unite_mesure TEXT DEFAULT 'pce',
                    marque TEXT DEFAULT NULL,
                    attributs_json TEXT DEFAULT NULL,
                    sync_status INTEGER DEFAULT 0,
                    seuil_alerte INTEGER DEFAULT 5
                )""",
                """CREATE TABLE IF NOT EXISTS Stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_produit INTEGER,
                    taille TEXT,
                    quantite_actuelle INTEGER,
                    seuil_alerte INTEGER,
                    requires_stock_audit INTEGER DEFAULT 0,
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
                )""",
                """CREATE TABLE IF NOT EXISTS Ventes_Details (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_ticket INTEGER,
                    id_stock INTEGER,
                    quantite INTEGER,
                    prix_unitaire_tvac DECIMAL,
                    FOREIGN KEY (id_ticket) REFERENCES Tickets(id) ON DELETE CASCADE,
                    FOREIGN KEY (id_stock) REFERENCES Stocks(id)
                )""",
                """CREATE TABLE IF NOT EXISTS Ledger_Caisse (
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
                )""",
                """CREATE TABLE IF NOT EXISTS Rapports_Z (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    donnees_json TEXT NOT NULL,
                    signature TEXT,
                    hash_precedent TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS Clotures_Caisse (
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
                )""",
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
                """CREATE TABLE IF NOT EXISTS Parametres (
                    cle TEXT PRIMARY KEY,
                    valeur TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS Cartes_Cadeaux (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    solde_initial DECIMAL NOT NULL,
                    solde_actuel DECIMAL NOT NULL,
                    date_creation DATETIME DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS Paniers_En_Attente (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    client_id INTEGER,
                    client_nom TEXT,
                    total_tvac DECIMAL,
                    remise DECIMAL DEFAULT '0.00',
                    panier_json TEXT NOT NULL,
                    note TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS Audit_Trail (
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
                )""",
                """CREATE TABLE IF NOT EXISTS Shopify_Sync (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    shopify_id TEXT NOT NULL,
                    last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'synced',
                    details TEXT
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
                """INSERT INTO ShopInfo (nom_magasin, adresse, siret_tva, type_commerce, devise)
                   SELECT "Mon Commerce", "Boutique Pilote", "BE 0123.456.789", "pret_a_porter", "€"
                   WHERE NOT EXISTS (SELECT 1 FROM ShopInfo)"""
            ]
        },
        {
            "version": "1.2.0",
            "description": "Immutabilité des ventes (piste d'audit) et anti-double-comptage Z multi-caisse",
            "sql": [
                # z_id marque un ticket/mouvement comme déjà compté dans une clôture Z :
                # sans ce marqueur, generer_bilan_z_journalier ne peut distinguer, pour une
                # caisse donnée, ce qui a déjà été clôturé de ce qui ne l'a pas été, et un Z
                # multi-caisse recompte les ventes des autres caisses (comparaison de date
                # seule, sans filtre caisse_id).
                "ALTER TABLE Tickets ADD COLUMN z_id INTEGER DEFAULT NULL",
                "ALTER TABLE Ledger_Caisse ADD COLUMN caisse_id TEXT DEFAULT 'POS-01'",
                "ALTER TABLE Ledger_Caisse ADD COLUMN z_id INTEGER DEFAULT NULL",

                # Un ticket scellé (piste d'audit / NF525) ne peut plus être modifié ou
                # supprimé par un simple UPDATE/DELETE : seul le marquage z_id (clôture Z)
                # est autorisé après coup, toute autre colonne financière ou d'identité
                # reste figée dès l'insertion. Sans ce déclencheur, PRAGMA foreign_keys
                # absent + aucune contrainte applicative ne protégeait Tickets d'une
                # falsification directe en base.
                """CREATE TRIGGER IF NOT EXISTS prevent_ticket_tamper_update
                   BEFORE UPDATE ON Tickets
                   FOR EACH ROW
                   WHEN NOT (
                       NEW.numero_ticket IS OLD.numero_ticket
                       AND NEW.date_heure IS OLD.date_heure
                       AND NEW.total_tvac IS OLD.total_tvac
                       AND NEW.total_htva IS OLD.total_htva
                       AND NEW.total_tva IS OLD.total_tva
                       AND NEW.remise IS OLD.remise
                       AND NEW.methode_paiement IS OLD.methode_paiement
                       AND NEW.id_client IS OLD.id_client
                       AND NEW.vendeur_nom IS OLD.vendeur_nom
                       AND NEW.rendu_monnaie IS OLD.rendu_monnaie
                       AND NEW.caisse_id IS OLD.caisse_id
                       AND NEW.signature IS OLD.signature
                       AND NEW.hash_precedent IS OLD.hash_precedent
                       AND NEW.previous_hash IS OLD.previous_hash
                       AND NEW.current_hash IS OLD.current_hash
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'Modification interdite : ticket scelle (piste audit). Utiliser une contre-passation.');
                   END""",
                """CREATE TRIGGER IF NOT EXISTS prevent_ticket_tamper_delete
                   BEFORE DELETE ON Tickets
                   FOR EACH ROW
                   BEGIN
                       SELECT RAISE(ABORT, 'Suppression interdite : ticket scelle (piste audit). Utiliser une contre-passation.');
                   END""",

                """CREATE TRIGGER IF NOT EXISTS prevent_ventes_details_tamper_update
                   BEFORE UPDATE ON Ventes_Details
                   FOR EACH ROW
                   BEGIN
                       SELECT RAISE(ABORT, 'Modification interdite : ligne de vente scellee (piste audit).');
                   END""",
                """CREATE TRIGGER IF NOT EXISTS prevent_ventes_details_tamper_delete
                   BEFORE DELETE ON Ventes_Details
                   FOR EACH ROW
                   BEGIN
                       SELECT RAISE(ABORT, 'Suppression interdite : ligne de vente scellee (piste audit).');
                   END""",

                """CREATE TRIGGER IF NOT EXISTS prevent_ledger_caisse_tamper_update
                   BEFORE UPDATE ON Ledger_Caisse
                   FOR EACH ROW
                   WHEN NOT (
                       NEW.date_heure IS OLD.date_heure
                       AND NEW.vendeur IS OLD.vendeur
                       AND NEW.type_mouvement IS OLD.type_mouvement
                       AND NEW.montant IS OLD.montant
                       AND NEW.methode_paiement IS OLD.methode_paiement
                       AND NEW.reference IS OLD.reference
                       AND NEW.signature IS OLD.signature
                       AND NEW.hash_precedent IS OLD.hash_precedent
                       AND NEW.caisse_id IS OLD.caisse_id
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'Modification interdite : mouvement de caisse scelle (piste audit).');
                   END""",
                """CREATE TRIGGER IF NOT EXISTS prevent_ledger_caisse_tamper_delete
                   BEFORE DELETE ON Ledger_Caisse
                   FOR EACH ROW
                   BEGIN
                       SELECT RAISE(ABORT, 'Suppression interdite : mouvement de caisse scelle (piste audit).');
                   END"""
            ]
        },
        {
            "version": "1.3.0",
            "description": "Traçabilité des remboursements (anti-survente/anti-doublon) et durcissement anti-fraude",
            "sql": [
                # Relie une ligne de remboursement (quantite négative) à la ligne de vente
                # d'origine qu'elle rembourse. Sans ce lien, rien n'empêchait de rembourser
                # indéfiniment la même ligne (même vd_id) : chaque appel recréditait le stock
                # et le ledger sans jamais vérifier combien avait déjà été remboursé.
                "ALTER TABLE Ventes_Details ADD COLUMN refund_of_vd_id INTEGER DEFAULT NULL"
            ]
        },
        {
            "version": "1.4.0",
            "description": "Auto-réparation de dérive de schéma : Audit_Trail, Cartes_Cadeaux et Shopify_Sync "
                            "avaient été ajoutées au SQL de la migration 1.0.0 après coup, donc toute base déjà "
                            "marquée '1.0.0' comme appliquée (avant cet ajout) ne les a jamais reçues. Comme le "
                            "suivi de version se fait par identifiant de migration (et non par instruction), ces "
                            "tables ne pouvaient plus jamais être créées rétroactivement. Cette migration est "
                            "volontairement idempotente (IF NOT EXISTS) pour combler l'écart sur toute base réelle "
                            "existante, sans toucher aux données déjà présentes.",
            "sql": [
                """CREATE TABLE IF NOT EXISTS Audit_Trail (
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
                )""",
                "CREATE INDEX IF NOT EXISTS idx_audit_trail_timestamp ON Audit_Trail(timestamp)",
                """CREATE TABLE IF NOT EXISTS Cartes_Cadeaux (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    solde_initial DECIMAL NOT NULL,
                    solde_actuel DECIMAL NOT NULL,
                    date_creation DATETIME DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS Shopify_Sync (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    shopify_id TEXT NOT NULL,
                    last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'synced',
                    details TEXT
                )"""
            ]
        },
        {
            "version": "1.5.0",
            "description": "Cartes_Cadeaux/Avoirs : traçabilité (bénéficiaire, motif, statut, émetteur) et "
                            "durcissement anti-fraude de la redemption. Le paiement par 'Avoir' en caisse "
                            "n'était vérifié ni créé côté serveur : n'importe quel code (même inventé) "
                            "validait une vente en 2 clics sans aucune contrepartie réelle, et une carte "
                            "valide pouvait être réutilisée indéfiniment sans jamais voir son solde décrémenté.",
            "sql": [
                "ALTER TABLE Cartes_Cadeaux ADD COLUMN notes TEXT DEFAULT NULL",
                "ALTER TABLE Cartes_Cadeaux ADD COLUMN client_id INTEGER DEFAULT NULL",
                "ALTER TABLE Cartes_Cadeaux ADD COLUMN client_nom TEXT DEFAULT NULL",
                "ALTER TABLE Cartes_Cadeaux ADD COLUMN status TEXT DEFAULT 'active'",
                "ALTER TABLE Cartes_Cadeaux ADD COLUMN emis_par TEXT DEFAULT NULL"
            ]
        }
    ]

    @classmethod
    def get_applied_versions(cls, conn: sqlite3.Connection) -> list:
        """Retourne la liste des versions de schémas déjà appliquées."""
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
    def create_pre_migration_snapshot(cls, db_path: str = None) -> str:
        """Crée une sauvegarde physique complète avant l'application de migrations."""
        path = db_path or ShopConfig.get_db_path()
        if not os.path.exists(path):
            return ""

        try:
            snapshots_dir = ShopConfig.get_snapshots_dir()
            os.makedirs(snapshots_dir, exist_ok=True)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot_filename = f"kodo_pos_pre_migration_{timestamp}.db"
            snapshot_path = os.path.join(snapshots_dir, snapshot_filename)

            shutil.copy2(path, snapshot_path)
            return snapshot_path
        except Exception as e:
            print(f"⚠️ Avertissement lors de la création du snapshot pre-migration: {e}")
            return ""

    @classmethod
    def restore_snapshot(cls, db_path: str, snapshot_path: str):
        """Restaure physiquement la base de données depuis un snapshot."""
        if snapshot_path and os.path.exists(snapshot_path):
            try:
                shutil.copy2(snapshot_path, db_path)
            except Exception as e:
                print(f"⚠️ Échec de la restauration du snapshot {snapshot_path}: {e}")

    @classmethod
    def run_migrations(cls, db_path: str = None, conn=None):
        """Exécute de façon atomique et sécurisée toutes les migrations manquantes."""
        safe_conn = get_connection(db_path=db_path, conn=conn)
        target_path = db_path or getattr(safe_conn, "db_path", ShopConfig.get_db_path())
        snapshot_path = ""
        
        try:
            applied = cls.get_applied_versions(safe_conn._conn)
            pending = [m for m in cls.MIGRATIONS if m["version"] not in applied]

            if not pending:
                return

            # Création du snapshot de sécurité s'il y a des migrations à appliquer
            snapshot_path = cls.create_pre_migration_snapshot(target_path)

            cursor = safe_conn.cursor()
            for migration in pending:
                version = migration["version"]
                for statement in migration["sql"]:
                    try:
                        cursor.execute(statement)
                    except sqlite3.OperationalError as oe:
                        if "duplicate column name" not in str(oe).lower():
                            raise oe

                cursor.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))

            safe_conn.commit()

            # Contrôle d'intégrité
            cursor.execute("PRAGMA quick_check")
            check_res = cursor.fetchone()
            if check_res and check_res[0].lower() != "ok":
                raise MigrationError(f"Intégrité SQLite compromise post-migration: {check_res}")

        except Exception as e:
            safe_conn.rollback()
            if snapshot_path:
                cls.restore_snapshot(target_path, snapshot_path)
            raise MigrationError(f"Échec de migration: {e}")
        finally:
            safe_conn.close()


def initialiser_db(db_path: str = None, conn=None):
    """
    Réinitialise et garantit la structure complète de la base usine Kōdo POS.
    Crée toutes les tables, index, déclencheurs, vues d'alias et données initiales.
    Accept d'être appelé avec un db_path ou une connexion existante.
    """
    safe_conn = get_connection(db_path=db_path, conn=conn)
    try:
        cursor = safe_conn.cursor()

        # Prise en compte explicite de la table de version
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for m in MigrationManager.MIGRATIONS:
            cursor.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (m["version"],))

        # ---------------------------------------------------------------------
        # 1. TABLE: Categories
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT UNIQUE NOT NULL
            )
        ''')

        # ---------------------------------------------------------------------
        # 2. TABLE: Marques
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Marques (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT UNIQUE NOT NULL
            )
        ''')

        # ---------------------------------------------------------------------
        # 3. TABLE: Produits (avec toutes les colonnes intégrées)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 4. TABLE: Stocks
        # ---------------------------------------------------------------------
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
            except Exception: pass


        # ---------------------------------------------------------------------
        # 5. TABLE: Clients
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 6. TABLE: Vendeurs
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Vendeurs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT NOT NULL,
                pin TEXT UNIQUE NOT NULL,
                role_admin INTEGER DEFAULT 0
            )
        ''')

        # ---------------------------------------------------------------------
        # 7. TABLE: Sessions_Caisse
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 8. TABLE: Depenses_Caisse
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Depenses_Caisse (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                libelle TEXT,
                montant DECIMAL,
                moyen_paiement TEXT DEFAULT 'Espèces'
            )
        ''')

        # ---------------------------------------------------------------------
        # 9. TABLE: Tickets (Ventes)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 10. TABLE: Ventes_Details (Lignes de Vente)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 11. TABLE: Ledger_Caisse (Journal financier NF525)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 12. TABLE: Rapports_Z / Clotures_Caisse (Clôtures comptables Z NF525)
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Rapports_Z (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                donnees_json TEXT NOT NULL,
                signature TEXT,
                hash_precedent TEXT
            )
        ''')

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

        # ---------------------------------------------------------------------
        # 13. TABLE: ShopInfo / Shop_Config
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 14. TABLE: Parametres
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Parametres (
                cle TEXT PRIMARY KEY,
                valeur TEXT
            )
        ''')

        # ---------------------------------------------------------------------
        # 15. TABLE: Cartes_Cadeaux
        # ---------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Cartes_Cadeaux (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                solde_initial DECIMAL NOT NULL,
                solde_actuel DECIMAL NOT NULL,
                date_creation DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ---------------------------------------------------------------------
        # 16. TABLE: Paniers_En_Attente (Tickets en attente)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 17. TABLE: Audit_Trail (Chaîne d'audit inaltérable SHA-256)
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 18. TABLE: Shopify_Sync
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # 19. MIGRATION ET DEFAUTS POUR TABLES EXISTANTES ET COLONNES
        # ---------------------------------------------------------------------
        cursor.execute("PRAGMA table_info(Produits)")
        cols_produits = [row[1] for row in cursor.fetchall()]
        for col_def in [
            ('image_path', 'TEXT'),
            ('en_solde', "INTEGER DEFAULT 0"),
            ('prix_solde_tvac', "DECIMAL DEFAULT NULL"),
            ('type_vente', "TEXT DEFAULT 'unite'"),
            ('unite_mesure', "TEXT DEFAULT 'pce'"),
            ('marque', "TEXT DEFAULT NULL"),
            ('attributs_json', "TEXT DEFAULT NULL"),
            ('sync_status', "INTEGER DEFAULT 0"),
            ('seuil_alerte', "INTEGER DEFAULT 5")
        ]:
            if col_def[0] not in cols_produits:
                try: cursor.execute(f"ALTER TABLE Produits ADD COLUMN {col_def[0]} {col_def[1]}")
                except Exception: pass

        try:
            cursor.execute("INSERT OR IGNORE INTO Categories (nom) SELECT DISTINCT categorie FROM Produits WHERE categorie IS NOT NULL AND categorie != ''")
            cursor.execute("INSERT OR IGNORE INTO Marques (nom) SELECT DISTINCT marque FROM Produits WHERE marque IS NOT NULL AND marque != ''")
        except Exception:
            pass

        cursor.execute("PRAGMA table_info(Tickets)")
        cols_tickets = [row[1] for row in cursor.fetchall()]
        for col_def in [
            ('remise', "DECIMAL DEFAULT '0.00'"),
            ('id_client', "INTEGER"),
            ('vendeur_nom', "TEXT"),
            ('rendu_monnaie', "DECIMAL DEFAULT '0.00'"),
            ('signature', "TEXT"),
            ('hash_precedent', "TEXT"),
            ('previous_hash', "TEXT"),
            ('current_hash', "TEXT"),
            ('caisse_id', "TEXT DEFAULT 'POS-01'"),
            ('details_articles', "TEXT"),
            ('sync_status', "INTEGER DEFAULT 1"),
            ('offline_uuid', "TEXT"),
            ('created_at_utc', "TEXT"),
            ('synced_shopify', "INTEGER DEFAULT 0"),
            ('shopify_order_id', "TEXT")
        ]:
            if col_def[0] not in cols_tickets:
                try: cursor.execute(f"ALTER TABLE Tickets ADD COLUMN {col_def[0]} {col_def[1]}")
                except Exception: pass

        # z_id marque le ticket comme déjà compté dans une clôture Z, pour empêcher tout
        # double comptage entre caisses ou entre exécutions successives d'un Z (voir
        # generer_bilan_z_journalier / enregistrer_cloture_caisse).
        if 'z_id' not in cols_tickets:
            try: cursor.execute("ALTER TABLE Tickets ADD COLUMN z_id INTEGER DEFAULT NULL")
            except Exception: pass

        cursor.execute("PRAGMA table_info(Ledger_Caisse)")
        cols_ledger_caisse = [row[1] for row in cursor.fetchall()]
        for col_def in [
            # caisse_id identifie la caisse d'origine du mouvement : sans cette colonne,
            # Ledger_Caisse ne peut pas être filtré par caisse, ce qui rend tout Z
            # multi-caisse comptable sur des mouvements d'autres caisses.
            ('caisse_id', "TEXT DEFAULT 'POS-01'"),
            # z_id marque le mouvement comme déjà compté dans une clôture Z (même logique
            # que Tickets.z_id).
            ('z_id', "INTEGER DEFAULT NULL"),
        ]:
            if col_def[0] not in cols_ledger_caisse:
                try: cursor.execute(f"ALTER TABLE Ledger_Caisse ADD COLUMN {col_def[0]} {col_def[1]}")
                except Exception: pass

        # ---------------------------------------------------------------------
        # 20. SEEDING DONNÉES INITIALES USINE
        # ---------------------------------------------------------------------
        cursor.execute("SELECT COUNT(*) FROM ShopInfo")
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO ShopInfo (nom_magasin, adresse, siret_tva, type_commerce, devise)
                VALUES (?, ?, ?, ?, ?)
            ''', (ShopConfig.NOM_MAGASIN_DEFAULT, "Boutique Pilote", "BE 0123.456.789", ShopConfig.PROFIL_METIER, ShopConfig.DEVISE_DEFAULT))

        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('pin_admin', ?)", (hash_pin('0000'),))
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_name', ?)", (ShopConfig.NOM_MAGASIN_DEFAULT,))
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_subtitle', 'Boutique')")
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_address', '')")
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_vat', '')")
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('default_tva', '0.21')")
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shopify_store_url', '')")
        cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shopify_access_token', '')")

        cursor.execute("SELECT COUNT(*) FROM Parametres WHERE cle='db_is_initialized'")
        is_initialized = cursor.fetchone()[0] > 0

        if not is_initialized:
            cursor.execute("SELECT COUNT(*) FROM Categories")
            if cursor.fetchone()[0] == 0:
                default_cats = [
                    "T-Shirts & Tops", "Pantalons & Jeans", "Robes & Jupes", "Vestes & Manteaux",
                    "Chaussures", "Accessoires", "Sacs", "Bijoux", "Lingerie", "Costumes & Tailleurs",
                    "SERVICE COIFFURE", "ESTHÉTIQUE", "VENTE BOUTIQUE", "DÉCORATION", "COFFRET", "Général"
                ]
                cursor.executemany("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", [(c,) for c in default_cats])

            cursor.execute("SELECT COUNT(*) FROM Marques")
            if cursor.fetchone()[0] == 0:
                default_marques = ["Hugo Boss", "Ralph Lauren", "Zara", "Nike", "Adidas", "Levi's", "Mango", "H&M", "Tommy Hilfiger", "Calvin Klein"]
                cursor.executemany("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", [(m,) for m in default_marques])

            cursor.execute("SELECT COUNT(*) FROM Vendeurs")
            if cursor.fetchone()[0] == 0:
                cursor.execute("INSERT OR IGNORE INTO Vendeurs (nom, pin, role_admin) VALUES ('Administrateur', ?, 1)", (hash_pin('0000'),))

            cursor.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('db_is_initialized', '1')")

        cursor.execute("SELECT id, pin FROM Vendeurs")
        vendeurs = cursor.fetchall()
        for vid, pin in vendeurs:
            if pin and len(pin) == 4 and pin.isdigit():
                hashed = hash_pin(pin)
                cursor.execute("SELECT COUNT(*) FROM Vendeurs WHERE pin = ?", (hashed,))
                if cursor.fetchone()[0] == 0:
                    cursor.execute("UPDATE Vendeurs SET pin = ? WHERE id = ?", (hashed, vid))
                else:
                    import random
                    temp_pin = f"TEMP_{random.randint(1000, 9999)}"
                    cursor.execute("UPDATE Vendeurs SET pin = ? WHERE id = ?", (temp_pin, vid))

        # ---------------------------------------------------------------------
        # 21. DÉCLENCHEURS (TRIGGERS) ET INDEXES DE SÉCURITÉ
        # ---------------------------------------------------------------------
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

        # Seul le marquage z_id (clôture Z) est autorisé après coup ; toute autre colonne
        # financière ou d'identité du ticket est figée dès l'insertion.
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS prevent_ticket_tamper_update
            BEFORE UPDATE ON Tickets
            FOR EACH ROW
            WHEN NOT (
                NEW.numero_ticket IS OLD.numero_ticket
                AND NEW.date_heure IS OLD.date_heure
                AND NEW.total_tvac IS OLD.total_tvac
                AND NEW.total_htva IS OLD.total_htva
                AND NEW.total_tva IS OLD.total_tva
                AND NEW.remise IS OLD.remise
                AND NEW.methode_paiement IS OLD.methode_paiement
                AND NEW.id_client IS OLD.id_client
                AND NEW.vendeur_nom IS OLD.vendeur_nom
                AND NEW.rendu_monnaie IS OLD.rendu_monnaie
                AND NEW.caisse_id IS OLD.caisse_id
                AND NEW.signature IS OLD.signature
                AND NEW.hash_precedent IS OLD.hash_precedent
                AND NEW.previous_hash IS OLD.previous_hash
                AND NEW.current_hash IS OLD.current_hash
            )
            BEGIN
                SELECT RAISE(ABORT, 'Modification interdite : ticket scellé (piste d''audit). Utiliser une contre-passation.');
            END;
        ''')

        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS prevent_ticket_tamper_delete
            BEFORE DELETE ON Tickets
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Suppression interdite : ticket scellé (piste d''audit). Utiliser une contre-passation.');
            END;
        ''')

        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS prevent_ledger_caisse_tamper_update
            BEFORE UPDATE ON Ledger_Caisse
            FOR EACH ROW
            WHEN NOT (
                NEW.date_heure IS OLD.date_heure
                AND NEW.vendeur IS OLD.vendeur
                AND NEW.type_mouvement IS OLD.type_mouvement
                AND NEW.montant IS OLD.montant
                AND NEW.methode_paiement IS OLD.methode_paiement
                AND NEW.reference IS OLD.reference
                AND NEW.signature IS OLD.signature
                AND NEW.hash_precedent IS OLD.hash_precedent
                AND NEW.caisse_id IS OLD.caisse_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Modification interdite : mouvement de caisse scellé (piste d''audit).');
            END;
        ''')

        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS prevent_ledger_caisse_tamper_delete
            BEFORE DELETE ON Ledger_Caisse
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Suppression interdite : mouvement de caisse scellé (piste d''audit).');
            END;
        ''')

        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS prevent_ventes_details_tamper_update
            BEFORE UPDATE ON Ventes_Details
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Modification interdite : ligne de vente scellée (piste d''audit).');
            END;
        ''')

        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS prevent_ventes_details_tamper_delete
            BEFORE DELETE ON Ventes_Details
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Suppression interdite : ligne de vente scellée (piste d''audit).');
            END;
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_date ON Tickets(date_heure)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ventes_details_ticket ON Ventes_Details(id_ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stocks_produit ON Stocks(id_produit)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_produits_code ON Produits(code_barre)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rapports_z_date ON Rapports_Z(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_trail_timestamp ON Audit_Trail(timestamp)")

        # ---------------------------------------------------------------------
        # 22. VUES DE COMPATIBILITÉ POUR NOMENCLATURES ALTERNATIVES / FRANÇAISES
        # ---------------------------------------------------------------------
        views_mapping = [
            ("produits", "SELECT * FROM Produits"),
            ("categories", "SELECT * FROM Categories"),
            ("marques", "SELECT * FROM Marques"),
            ("clients", "SELECT * FROM Clients"),
            ("ventes", "SELECT * FROM Tickets"),
            ("ligne_ventes", "SELECT * FROM Ventes_Details"),
            ("tickets_en_attente", "SELECT * FROM Paniers_En_Attente"),
            ("clotures_z", "SELECT * FROM Clotures_Caisse"),
            ("audit_trail", "SELECT * FROM Audit_Trail"),
            ("shop_config", "SELECT * FROM ShopInfo"),
            ("shopify_sync", "SELECT * FROM Shopify_Sync")
        ]
        for view_name, select_sql in views_mapping:
            try:
                cursor.execute(f"CREATE VIEW IF NOT EXISTS {view_name} AS {select_sql}")
            except Exception:
                pass

        safe_conn.commit()
        print("[OK] Base de données SQLite initialisée usine avec succès.")

    except Exception as e:
        safe_conn.rollback()
        print(f"⚠️ Erreur lors de l'initialisation usine de la base de données: {e}")
        raise e
    finally:
        safe_conn.close()

# Association statique sur la classe MigrationManager pour rétrocompatibilité totale
MigrationManager.initialiser_db = staticmethod(initialiser_db)
