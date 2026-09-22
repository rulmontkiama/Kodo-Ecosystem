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

# ---------------------------------------------------------------------------
# Hygiène du code-barres (Produits.code_barre)
# ---------------------------------------------------------------------------
# `code_barre` est déclaré TEXT UNIQUE partout où la table est créée. En SQLite,
# UNIQUE tolère autant de NULL qu'on veut, mais une seule chaîne vide : sans
# nettoyage, la DEUXIÈME fiche créée sans code-barres est refusée par
# « UNIQUE constraint failed », message incompréhensible sur un formulaire où la
# commerçante n'a justement rien saisi.
#
# Les blancs comptent autant que la chaîne vide : espace, tabulation, saut de
# ligne, et surtout le retour chariot que certaines douchettes ajoutent en fin
# de trame. Pour UNIQUE, '3245678901234' et '3245678901234\r' sont deux valeurs
# différentes : l'article existe en base mais reste introuvable au scan, et le
# même code peut être enregistré deux fois.
#
# Pourquoi AFTER et non BEFORE : BEFORE serait le moment naturel pour corriger
# la valeur avant le contrôle d'unicité, mais SQLite ne sait pas réécrire NEW
# (« SET NEW.code_barre = ... » est une erreur de syntaxe, contrairement à
# MySQL). AFTER est donc le seul moment possible — et il suffit : le trigger
# neutralise la ligne dans la même instruction, aucune ligne vide ne subsiste
# entre deux insertions, et les créations successives sans code-barres passent
# toutes (vérifié en insertions séquentielles, executemany et INSERT
# multi-lignes). Le seul cas que le trigger ne peut pas rattraper est une ligne
# vide écrite AVANT son existence : d'où la reprise de données ci-dessous.
_BARCODE_BLANKS = "' ' || char(9) || char(10) || char(13)"

# Forme normalisée de NEW.code_barre : rognée de ses blancs, et NULL si plus rien
# ne reste. NULL est la seule valeur que UNIQUE accepte en plusieurs exemplaires.
_BARCODE_NORMALISED = f"NULLIF(TRIM(NEW.code_barre, {_BARCODE_BLANKS}), '')"

# Source unique des deux déclencheurs : la liste versionnée MIGRATIONS les pose
# sur les bases existantes, et initialiser_db les réutilise telles quelles pour
# les bases neuves. Les deux chemins ne peuvent donc pas diverger.
# La condition WHEN compare la valeur à sa forme normalisée avec l'opérateur
# `IS NOT` (comparaison sûre vis-à-vis de NULL) : elle est donc fausse dès que la
# valeur est déjà propre. Le déclencheur s'arrête de lui-même après une passe,
# y compris si `PRAGMA recursive_triggers` est activé (vérifié).
#
# Le DROP préalable n'est pas un détail : « CREATE TRIGGER IF NOT EXISTS » ne
# remplace pas un déclencheur existant, il le CONSERVE. Les bases déjà passées
# par initialiser_db portent l'ancienne version (« WHEN NEW.code_barre = '' »),
# qui ignore espaces et retour chariot ; sans DROP elles la garderaient pour
# toujours et la correction ne les atteindrait jamais (vérifié). Un déclencheur
# ne contient aucune donnée : le détruire puis le recréer est sans perte, et
# rend l'ensemble rejouable à volonté.
BARCODE_HYGIENE_TRIGGERS_SQL = [
    "DROP TRIGGER IF EXISTS clean_empty_barcode_insert",
    f"""CREATE TRIGGER IF NOT EXISTS clean_empty_barcode_insert
        AFTER INSERT ON Produits
        FOR EACH ROW
        WHEN NEW.code_barre IS NOT NULL
         AND NEW.code_barre IS NOT {_BARCODE_NORMALISED}
        BEGIN
            UPDATE Produits SET code_barre = {_BARCODE_NORMALISED} WHERE id = NEW.id;
        END;""",
    "DROP TRIGGER IF EXISTS clean_empty_barcode_update",
    f"""CREATE TRIGGER IF NOT EXISTS clean_empty_barcode_update
        AFTER UPDATE ON Produits
        FOR EACH ROW
        WHEN NEW.code_barre IS NOT NULL
         AND NEW.code_barre IS NOT {_BARCODE_NORMALISED}
        BEGIN
            UPDATE Produits SET code_barre = {_BARCODE_NORMALISED} WHERE id = NEW.id;
        END;""",
]

# Reprise des lignes écrites avant l'existence des déclencheurs : un trigger ne
# rétroagit pas. Volontairement limitée aux valeurs VIDES, qui ne peuvent que
# devenir NULL — et UNIQUE accepte les NULL en nombre, donc cette instruction ne
# peut pas échouer, quel que soit le contenu de la base cliente.
# Les codes NON vides mal formés (espace ou retour chariot résiduel) ne sont
# délibérément PAS rognés ici : deux lignes se rognant vers la même valeur
# entreraient en collision avec UNIQUE et feraient échouer la migration au
# démarrage, boutique à l'arrêt. Ils sont signalés dans le rapport d'audit et
# relèvent d'une reprise manuelle contrôlée.
BARCODE_HYGIENE_BACKFILL_SQL = [
    f"""UPDATE Produits
           SET code_barre = NULL
         WHERE code_barre IS NOT NULL
           AND TRIM(code_barre, {_BARCODE_BLANKS}) = ''""",
]


class MigrationError(Exception):
    """Exception levée en cas d'erreur critique de migration de schéma."""
    pass

def _executer_conversion_shpf(conn):
    """Exécute la conversion des codes SHPF- en EAN-13 via InventoryManager."""
    try:
        from kodo_core.domain.catalog.inventory_manager import InventoryManager
        InventoryManager.convertir_codes_shpf(conn)
    except Exception as e:
        import logging
        logging.getLogger("kodo.migrations").warning(f"Conversion SHPF non exécutée : {e}")


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
                    seuil_alerte INTEGER DEFAULT 5,
                    -- Drapeau de survente. database_manager l'ajoute par ALTER sur les bases
                    -- existantes ; sans lui ICI, une base née uniquement de ce fichier perdait
                    -- en silence toute trace des ventes passées sous le stock disponible.
                    requires_stock_audit INTEGER DEFAULT 0
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
                # Amorce neutre : le commerçant saisit son identité dans Paramètres > Boutique.
                # Ni nom de boutique pilote, ni numéro de TVA fictif — une TVA inventée écrite
                # en base ressort ensuite sur des documents fiscaux.
                """INSERT INTO ShopInfo (nom_magasin, adresse, siret_tva, type_commerce, devise)
                   SELECT "Mon Commerce", "", "", "pret_a_porter", "€"
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
        },
        {
            "version": "2.0.1",
            "description": "Purge des valeurs d'amorce fictives (TVA de démonstration, adresse pilote)",
            "sql": [
                # Réparation des bases ayant exécuté l'amorce 1.1.0 avant sa correction.
                # Ne touche que les valeurs strictement égales aux constantes de démonstration :
                # une identité réellement saisie par le commerçant n'est jamais modifiée.
                """UPDATE ShopInfo SET siret_tva = '' WHERE siret_tva IN ('BE 0123.456.789', 'BE0123.456.789', '0123.456.789')""",
                """UPDATE ShopInfo SET adresse = '' WHERE adresse = 'Boutique Pilote'""",
                """UPDATE Parametres SET valeur = '' WHERE cle IN ('shop_vat', 'shop_tva', 'shop_bce') AND valeur IN ('BE 0123.456.789', 'BE0123.456.789', '0123.456.789')""",
            ]
        },
        {
            "version": "2.0.2",
            "description": "Purge de l'IBAN de démonstration enregistré en base",
            "sql": [
                # Le repli « BE68 0000 0000 0000 » a ete retire du code, mais les bases
                # creees avant l'ont ENREGISTRE comme une vraie valeur : le message de
                # virement envoye aux clientes designait alors un compte inexistant.
                # Seule l'egalite stricte avec la constante de demonstration est purgee ;
                # un IBAN reellement saisi par le commercant n'est jamais touche.
                """UPDATE Parametres SET valeur = '' WHERE cle = 'shop_iban' AND REPLACE(valeur, ' ', '') = 'BE68000000000000'""",
            ]
        },
        {
            "version": "2.0.4",
            "description": "Hygiène du code-barres : déclencheurs de nettoyage et reprise des valeurs vides",
            "sql": [
                # Les deux déclencheurs n'étaient définis que dans initialiser_db, qui
                # n'est PAS le chemin de démarrage de l'application : celui-ci passe par
                # database_manager.initialiser_db, lequel appelle run_migrations puis sa
                # propre création de tables, et aucun des deux ne posait ces triggers.
                # Constaté sur les bases du dépôt : elles ont bien la contrainte UNIQUE
                # mais aucun déclencheur de nettoyage. Le filet existait dans le code et
                # pas chez les clientes ; cette entrée versionnée l'y installe enfin.
                *BARCODE_HYGIENE_TRIGGERS_SQL,
                # Puis on rattrape les lignes vides déjà présentes, qu'un déclencheur
                # posé après coup ne corrige pas de lui-même (vérifié).
                *BARCODE_HYGIENE_BACKFILL_SQL,
            ]
        },
        {
            "version": "2.0.5",
            "description": "Synchronisation Shopify : journal d'idempotence par ligne de vente et "
                           "table de réconciliation des variantes. Sans le journal, un ticket dont "
                           "UNE ligne échouait n'était jamais marqué comme synchronisé, et les lignes "
                           "déjà poussées repartaient à chaque passe : le stock Shopify était "
                           "décrémenté plusieurs fois pour une seule vente, et l'écart ne se "
                           "rattrapait jamais de lui-même.",
            "sql": [
                # La clé primaire EST la garantie d'idempotence : une ligne de vente ne peut
                # figurer qu'une fois dans le journal, donc ne peut être poussée qu'une fois,
                # même si le ticket entier n'a pas pu être clos. Statuts possibles :
                # EN_VOL (réservée avant l'appel réseau), POUSSE, ABSENT_SHOPIFY, SANS_OBJET,
                # INDETERMINE (coupure réseau : on ignore si l'ajustement a porté, donc on ne
                # rejoue JAMAIS — sous-décompter se corrige à l'inventaire, sur-décompter non).
                """CREATE TABLE IF NOT EXISTS Shopify_Sync_Lignes (
                    id_vente_detail INTEGER PRIMARY KEY,
                    id_ticket INTEGER NOT NULL,
                    code_barre TEXT,
                    inventory_item_id INTEGER,
                    quantite_poussee INTEGER NOT NULL DEFAULT 0,
                    statut TEXT NOT NULL,
                    date_heure TEXT NOT NULL
                )""",
                """CREATE INDEX IF NOT EXISTS idx_shopify_sync_lignes_ticket
                   ON Shopify_Sync_Lignes(id_ticket)""",
                # Correspondance variante Shopify → produit local. C'est la clé de
                # réconciliation stable : le SKU et le code-barres d'une variante peuvent
                # changer côté Shopify, son id de variante non. Sans elle, l'import
                # s'appuyait sur le code-barres et recréait un doublon à chaque renommage.
                """CREATE TABLE IF NOT EXISTS Shopify_Variantes (
                    variant_id INTEGER PRIMARY KEY,
                    id_produit INTEGER NOT NULL,
                    date_maj TEXT
                )""",
            ]
        },
        {
            "version": "2.0.6",
            "description": "Shopify : prise en compte des remboursements et annulations survenus "
                           "APRÈS l'import de la commande. Sans elle, une commande en ligne "
                           "remboursée restait comptée comme une vente pleine dans le rapport Z et "
                           "dans la TVA, et l'article remboursé ne revenait jamais en rayon.",
            "sql": [
                # Même patron d'idempotence que `Shopify_Sync_Lignes` : la clé primaire EST la
                # garantie. `cle` vaut 'refund:<id>' pour un remboursement Shopify et
                # 'annulation:<order_id>' pour une commande annulée — un identifiant de
                # remboursement et un identifiant de commande vivent dans deux espaces de
                # numérotation distincts et pourraient se télescoper sur un entier nu.
                """CREATE TABLE IF NOT EXISTS Shopify_Remboursements (
                    cle TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    refund_id TEXT,
                    tickets TEXT,
                    montant DECIMAL,
                    date_traitement TEXT NOT NULL
                )""",
                """CREATE INDEX IF NOT EXISTS idx_shopify_remboursements_order
                   ON Shopify_Remboursements(order_id)""",
            ]
        },
        {
            "version": "2.0.7",
            "description": "Retrait du déclencheur `prevent_negative_stock` des bases DÉJÀ en "
                           "service. Le retirer de `initialiser_db` ne suffisait pas : cette "
                           "fonction crée, elle ne supprime jamais. Les bases des boutiques en "
                           "activité — créées par une version antérieure ou restaurées depuis "
                           "une sauvegarde — le gardaient donc, et ce sont exactement celles-là "
                           "qui souffrent du défaut. Le déclencheur refusait toute UPDATE sur une "
                           "ligne de stock négative, y compris l'`UPDATE Stocks SET "
                           "requires_stock_audit = 1` par lequel `OfflineSyncEngine` SIGNALE le "
                           "conflit : le stock négatif issu de deux caisses hors-ligne devenait "
                            "non seulement irréparable, mais muet.",
            "sql": [
                "DROP TRIGGER IF EXISTS prevent_negative_stock",
            ]
        },
        {
            "version": "2.0.8",
            "description": "Conversion des anciens codes-barres provisoires 'SHPF-<id>' en vrais "
                           "codes-barres internes EAN-13 scannables à la douchette et imprimables. "
                           "Préservation préalable de la correspondance dans Shopify_Variantes.",
            "sql": [
                """CREATE TABLE IF NOT EXISTS Shopify_Variantes (
                    variant_id INTEGER PRIMARY KEY,
                    id_produit INTEGER NOT NULL,
                    date_maj TEXT
                )"""
            ],
            "python": lambda conn: _executer_conversion_shpf(conn),
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
        """Sauvegarde physique complète et vérifiée avant l'application de migrations."""
        path = db_path or ShopConfig.get_db_path()
        if not os.path.exists(path):
            return ""

        from kodo_core.db.sanctuary_shield import copier_base_sqlite
        snapshots_dir = ShopConfig.get_snapshots_dir()
        os.makedirs(snapshots_dir, exist_ok=True)
        # Résolution à la microseconde : deux migrations dans la même seconde ne
        # doivent pas produire le même nom, faute de quoi la seconde sauvegarde
        # écrase la première — exactement le cas d'une migration relancée après échec.
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_path = os.path.join(snapshots_dir, f"kodo_pos_pre_migration_{timestamp}.db")
        # Aucun `except` ici : une migration ne s'exécute pas sans sauvegarde vérifiée.
        # Échouer avant de toucher à la base est le comportement attendu.
        return copier_base_sqlite(path, snapshot_path)

    @classmethod
    def restore_snapshot(cls, db_path: str, snapshot_path: str):
        """Restaure la base depuis un snapshot vérifié, journaux périmés retirés."""
        if not snapshot_path or not os.path.exists(snapshot_path):
            return
        from kodo_core.db.sanctuary_shield import restaurer_base_sqlite
        try:
            restaurer_base_sqlite(snapshot_path, db_path)
            print(f"🛡️ Base restaurée depuis {os.path.basename(snapshot_path)}")
        except Exception as e:
            # La transaction a déjà été annulée par le rollback : la base en place est
            # cohérente. Mieux vaut la laisser telle quelle que l'écraser à l'aveugle.
            print(f"⚠️ Restauration impossible ({e}) — base laissée en l'état, "
                  f"sauvegarde conservée : {snapshot_path}")

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
                for statement in migration.get("sql", []):
                    try:
                        cursor.execute(statement)
                    except sqlite3.OperationalError as oe:
                        if "duplicate column name" not in str(oe).lower():
                            raise oe

                # Exécution d'une routine Python associée à la migration
                if "python" in migration and callable(migration["python"]):
                    migration["python"](safe_conn._conn)

                cursor.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))

            safe_conn.commit()

            # Contrôle d'intégrité
            cursor.execute("PRAGMA quick_check")
            check_res = cursor.fetchone()
            if check_res and check_res[0].lower() != "ok":
                raise MigrationError(f"Intégrité SQLite compromise post-migration: {check_res}")

        except Exception as e:
            safe_conn.rollback()
            # Le rollback suffit dans le cas normal. On ne restaure que si la base
            # est effectivement compromise : une restauration inutile est un risque net.
            try:
                chk = safe_conn.cursor().execute("PRAGMA quick_check").fetchone()
                base_saine = bool(chk) and chk[0].lower() == "ok"
            except Exception:
                base_saine = False
            if not base_saine and snapshot_path:
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
            ''', (ShopConfig.NOM_MAGASIN_DEFAULT, "", "", ShopConfig.PROFIL_METIER, ShopConfig.DEVISE_DEFAULT))

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
        # PAS de déclencheur `prevent_negative_stock` ici, et ce retrait est délibéré.
        # Un stock négatif est un ÉTAT MÉTIER LÉGITIME dans Kōdo POS : quand deux caisses
        # hors-ligne vendent le dernier article, la vente physiquement conclue n'est jamais
        # rejetée (Last-Write-Wins), le compteur passe sous zéro et `OfflineSyncEngine`
        # le détecte pour le signaler (`offline_engine.py:228-233`).
        # Le déclencheur interdisait toute UPDATE sur une ligne négative — y compris
        # l'`UPDATE Stocks SET requires_stock_audit = 1` qui pose justement le signalement.
        # Il rendait donc le conflit non seulement irréparable mais INVISIBLE.
        # Il ne s'installait que par ce chemin-ci, c'est-à-dire à l'IMPORT D'UN PACK DE
        # MIGRATION : une boutique changeant de Mac aurait hérité d'une base où son
        # mécanisme d'audit de stock était muet. Constaté en exécutant la suite :
        # `test_offline_engine_lww_and_audit` échoue dès que ce déclencheur est posé.
        # La protection contre une saisie négative est faite au bon niveau, dans
        # `InventoryManager.save_product`, qui refuse la valeur avec un message clair.

        # Hygiène du code-barres : mêmes déclencheurs que la migration 2.0.4, pris à
        # la même source pour que les bases neuves et les bases migrées ne puissent
        # pas diverger. Ils restent posés ici parce que initialiser_db marque toutes
        # les versions de MIGRATIONS comme appliquées (voir plus haut) : une base
        # passant par ce chemin ne rejouerait jamais 2.0.4 et se retrouverait sans
        # filet. La reprise des valeurs vides est jouée ensuite, pour une base
        # préexistante que initialiser_db viendrait compléter.
        for _barcode_sql in BARCODE_HYGIENE_TRIGGERS_SQL + BARCODE_HYGIENE_BACKFILL_SQL:
            cursor.execute(_barcode_sql)

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
