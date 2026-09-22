#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Conversion des anciens codes-barres provisoires 'SHPF-<id>' en vrais codes EAN-13.

Usage :
    python3 scripts/convert_shpf_barcodes.py [--db-path /chemin/vers/kodo_pos.db] [--dry-run]

Ce script :
1. Recherche tous les articles dont le code-barres commence par 'SHPF-'.
2. Sécurise la liaison de variante dans la table `Shopify_Variantes` (pour que les synchronisations
   futures continuent d'identifier le produit sans doublon).
3. Génère un vrai code-barres interne EAN-13 scannable à la douchette et imprimable sur étiquettes.
4. Met à jour le catalogue local.
"""

import argparse
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from kodo_core.domain.catalog.inventory_manager import InventoryManager
from database_manager import DB_NAME


def main():
    parser = argparse.ArgumentParser(description="Conversion des codes SHPF en EAN-13 scannables")
    parser.add_argument("--db-path", type=str, help="Chemin vers la base de données SQLite")
    parser.add_argument("--dry-run", action="store_true", help="Simule la conversion sans modifier la base")
    args = parser.parse_args()

    db_path = args.db_path or DB_NAME
    if not os.path.exists(db_path):
        sys.exit(f"❌ Base de données introuvable : {db_path}")

    print("\n" + "=" * 64)
    print("🏷️ CONVERSION DES CODES SHPF- EN CODES-BARRES EAN-13")
    print("=" * 64)
    print(f"Base de données : {db_path}")
    print(f"Mode simulation : {'OUI (Dry-Run)' if args.dry_run else 'NON (Modifications appliquées)'}")
    print("-" * 64)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, code_barre, nom FROM Produits WHERE code_barre LIKE 'SHPF-%'")
        a_convertir = cursor.fetchall()

        if not a_convertir:
            print("✅ Aucun code-barres provisoire 'SHPF-' trouvé. Le catalogue est déjà propre !")
            print("=" * 64 + "\n")
            return

        print(f"Trouvé : {len(a_convertir)} produit(s) avec un code 'SHPF-' :\n")
        for pid, code, nom in a_convertir:
            print(f"  • [ID {pid}] « {nom} » : {code}")

        if args.dry_run:
            print("\n🔍 Mode simulation activé : aucune modification n'a été enregistrée.")
            print("=" * 64 + "\n")
            return

        res = InventoryManager.convertir_codes_shpf(conn)
        conn.commit()

        print("\n" + "-" * 64)
        print(f"🎉 {res['convertis']} produit(s) converti(s) avec succès en vrais codes EAN-13 :")
        for item in res["details"]:
            var_info = f" (Variante Shopify n° {item['variant_id']})" if item['variant_id'] else ""
            print(f"  ✅ [ID {item['id']}] « {item['nom']} » : {item['ancien_code']} ➔ {item['nouveau_code']}{var_info}")

        print("=" * 64 + "\n")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
