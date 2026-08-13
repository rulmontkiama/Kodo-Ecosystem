#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script utilitaire Kōdo POS : Mise à jour des informations boutique (Adresse, TVA, Nom)
Permet de modifier les paramètres du ticket de caisse sans toucher aux produits, stocks ou ventes.
"""

import sqlite3
import os
import sys

def get_db_path():
    user_db = os.path.expanduser("~/Documents/Kodo_POS/ladresse_b.db")
    if os.path.exists(user_db):
        return user_db
    local_db = os.path.abspath("ladresse_b.db")
    return local_db

def update_shop_info(shop_name=None, shop_subtitle=None, shop_address=None, shop_vat=None):
    db_path = get_db_path()
    print(f"📌 Base de données ciblée : {db_path}")
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    params_to_update = {
        "shop_name": shop_name,
        "shop_subtitle": shop_subtitle,
        "shop_address": shop_address,
        "shop_vat": shop_vat
    }
    
    updated_count = 0
    for key, value in params_to_update.items():
        if value is not None:
            c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (key, str(value)))
            print(f"  ✅ {key} -> '{value}'")
            updated_count += 1
            
    conn.commit()
    conn.close()
    print(f"🎉 Modifié avec succès {updated_count} paramètre(s). Aucune donnée de vente ou de stock n'a été altérée.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser(description="Mise à jour des coordonnées boutique Kōdo POS")
        parser.add_argument("--name", help="Nom de l'établissement")
        parser.add_argument("--subtitle", help="Sous-titre (ex: Boutique de Mode)")
        parser.add_argument("--address", help="Adresse complète")
        parser.add_argument("--vat", help="N° TVA / Entreprise")
        args = parser.parse_args()
        
        update_shop_info(args.name, args.subtitle, args.address, args.vat)
    else:
        print("=== Mise à jour des informations Boutique (Kōdo POS) ===")
        name = input("Nom de l'établissement (laisser vide pour ne pas changer) : ").strip() or None
        subtitle = input("Sous-titre (laisser vide pour ne pas changer) : ").strip() or None
        address = input("Adresse complète (laisser vide pour ne pas changer) : ").strip() or None
        vat = input("N° TVA / Entreprise (laisser vide pour ne pas changer) : ").strip() or None
        
        update_shop_info(name, subtitle, address, vat)
