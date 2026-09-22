#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Répétition & Vérification Sécurisée des Migrations sur Copie de Base Réelle (P3).

Usage :
    python3 scripts/verify_migration_on_copy.py /chemin/vers/copie_kodo_pos.db

RÈGLES STRICTES :
- Ne JAMAIS exécuter ce script sur la base de production (~/Documents/Kodo_POS/db/kodo_pos.db).
- Travaille exclusivement sur une copie fournie.
- Valide l'intégrité avant et après, mesure les tables et s'assure qu'aucune donnée n'est perdue.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from kodo_core.db.migrations import MigrationManager


def inspecter_base(conn: sqlite3.Connection) -> dict:
    """Mesure l'état d'une base SQLite : versions, tables, comptages et déclencheurs."""
    c = conn.cursor()

    # Intégrité SQLite
    c.execute("PRAGMA integrity_check")
    integrity = c.fetchone()[0]

    # Versions de schéma
    try:
        c.execute("SELECT version FROM schema_version ORDER BY version ASC")
        versions = [r[0] for r in c.fetchall()]
    except Exception:
        versions = []

    # Liste des tables
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in c.fetchall()]

    # Comptages des tables principales
    comptages = {}
    for t in sorted(tables):
        try:
            c.execute(f"SELECT COUNT(*) FROM \"{t}\"")
            comptages[t] = c.fetchone()[0]
        except Exception as e:
            comptages[t] = f"erreur: {e}"

    # Déclencheurs
    c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    declencheurs = [r[0] for r in c.fetchall()]

    return {
        "integrity": integrity,
        "versions": versions,
        "tables": tables,
        "comptages": comptages,
        "declencheurs": declencheurs,
    }


def main():
    parser = argparse.ArgumentParser(description="Vérification des migrations sur copie de base Kōdo POS")
    parser.add_argument("db_copy", help="Chemin vers le fichier de copie de la base (.db)")
    args = parser.parse_args()

    copy_path = os.path.abspath(args.db_copy)

    # Garde-fou absolu : interdiction formelle de viser la base de production
    if "Documents/Kodo_POS/db" in copy_path or copy_path.endswith("/db/kodo_pos.db"):
        sys.exit(
            "🛑 INTERDICTION : Ce chemin ressemble à la base de production active !\n"
            "Copiez d'abord le fichier dans /tmp/copie_kodo.db et ciblez la copie."
        )

    if not os.path.exists(copy_path):
        sys.exit(f"❌ Fichier introuvable : {copy_path}")

    print("\n" + "=" * 70)
    print("🔍 AUDIT & RÉPÉTITION DES MIGRATIONS SUR COPIE DE BASE")
    print("=" * 70)
    print(f"Fichier analysé : {copy_path}")
    print(f"Taille fichier  : {os.path.getsize(copy_path):,} octets")

    # Étape 1 : Travail sur un clone temporaire de la copie pour sécurité maximale
    fd, work_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy2(copy_path, work_path)

    try:
        conn = sqlite3.connect(work_path)
        avant = inspecter_base(conn)
        conn.close()

        print("\n--- 1. ÉTAT INITIAL (AVANT MIGRATION) ---")
        print(f"Intégrité SQLite        : {avant['integrity']}")
        print(f"Versions appliquées     : {', '.join(avant['versions']) if avant['versions'] else 'Aucune'}")
        print(f"Déclencheur prevent_neg : {'PRÉSENT' if 'prevent_negative_stock' in avant['declencheurs'] else 'ABSENT'}")
        print(f"Table Remboursements    : {'PRÉSENTE' if 'Shopify_Remboursements' in avant['tables'] else 'ABSENTE'}")
        print(f"Table Variantes         : {'PRÉSENTE' if 'Shopify_Variantes' in avant['tables'] else 'ABSENTE'}")
        print(f"Nombre de tables        : {len(avant['tables'])}")

        # Étape 2 : Exécution des migrations
        print("\n--- 2. APPLICATION DES MIGRATIONS ---")
        MigrationManager.run_migrations(db_path=work_path)

        # Étape 3 : Inspection après migration
        conn = sqlite3.connect(work_path)
        apres = inspecter_base(conn)

        # Test d'écriture sur Shopify_Remboursements
        test_refund_ok = False
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO Shopify_Remboursements (cle, order_id, refund_id, tickets, montant, date_traitement) "
                "VALUES ('test:refund:999', 'order_999', 'ref_999', 'T-TEST', 10.0, '2026-09-22 12:00:00')"
            )
            conn.commit()
            c.execute("DELETE FROM Shopify_Remboursements WHERE cle='test:refund:999'")
            conn.commit()
            test_refund_ok = True
        except Exception as e:
            test_refund_err = str(e)

        conn.close()

        print("\n--- 3. ÉTAT FINAL (APRÈS MIGRATION) ---")
        print(f"Intégrité SQLite        : {apres['integrity']}")
        print(f"Versions appliquées     : {', '.join(apres['versions'])}")
        print(f"Déclencheur prevent_neg : {'PRÉSENT (ANOMALIE)' if 'prevent_negative_stock' in apres['declencheurs'] else 'RETIRÉ AVEC SUCCÈS'}")
        print(f"Table Remboursements    : {'PRÉSENTE' if 'Shopify_Remboursements' in apres['tables'] else 'MANQUANTE'}")
        print(f"Test écriture remb.     : {'SUCCÈS' if test_refund_ok else f'ÉCHEC ({test_refund_err})'}")

        # Étape 4 : Vérification de la non-régression des données
        print("\n--- 4. CONTRÔLE DE NON-RÉGRESSION DES DONNÉES ---")
        perte_donnees = False
        for t, count_avant in avant["comptages"].items():
            if t == "schema_version":
                continue
            count_apres = apres["comptages"].get(t, 0)
            if count_apres != count_avant:
                print(f"⚠️ ÉCART DÉTECTÉ sur '{t}' : {count_avant} -> {count_apres}")
                perte_donnees = True
            else:
                if isinstance(count_avant, int) and count_avant > 0:
                    print(f"✅ Table '{t}' : {count_avant} lignes conservées à l'identique")

        print("\n" + "=" * 70)
        if apres["integrity"] == "ok" and not perte_donnees and test_refund_ok and "prevent_negative_stock" not in apres["declencheurs"]:
            print("🎉 RÉSULTAT : MIGRATIONS 2.0.6 & 2.0.7 100% VALIDÉES SANS AUCUNE PERTE !")
            print("   La base est saine, enrichie et prête pour le déploiement.")
        else:
            print("❌ RÉSULTAT : Anomalie détectée lors de la simulation.")
        print("=" * 70 + "\n")

    finally:
        for sfx in ("", "-wal", "-shm"):
            if os.path.exists(work_path + sfx):
                os.remove(work_path + sfx)


if __name__ == "__main__":
    main()
