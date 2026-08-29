# -*- coding: utf-8 -*-
"""
Routes API Système, Version, Licence et Utilisateurs/PIN - Kōdo POS Core
"""

import datetime
import sqlite3
from typing import Dict, Any, Tuple, Optional

import database_manager
from database_manager import get_connection, hash_pin
import license_manager
import services.update_checker as update_checker


def handle_system_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any]) -> Optional[Tuple[int, Any]]:
    """
    Gestionnaire de requêtes pour la santé système, la version, la licence et les utilisateurs.
    """

    # 1. Health check & status
    if method == "GET" and path == "/api/status":
        return 200, {
            "status": "online",
            "app": "Kōdo POS Engine",
            "version": update_checker.get_installed_version(),
            "timestamp": datetime.datetime.now().isoformat()
        }

    # 2. Version
    elif method == "GET" and path == "/api/version":
        return 200, {
            "version": update_checker.get_installed_version()
        }

    # 3. Vérification mise à jour
    elif method == "GET" and path == "/api/check-update":
        res = update_checker.check_for_updates_sync()
        return 200, res

    # 4. Application d'une mise à jour
    elif method == "POST" and path == "/api/apply-update":
        patch_url = data.get('dist_patch_url') or data.get('distPatchUrl')
        target_ver = data.get('latest_version') or data.get('targetVersion')
        res = update_checker.apply_remote_update_sync(patch_url, target_ver)
        return 200, res

    # 5. Statut de la licence
    elif method == "GET" and path == "/api/license/status":
        info = license_manager.get_license_info()
        return 200, info

    # 6. Activation de la licence
    elif method == "POST" and path == "/api/license/activate":
        key = data.get('key') or data.get('license_key') or data.get('licenseKey')
        success, msg = license_manager.activate_license_key(key)
        return 200, {"success": success, "message": msg, "info": license_manager.get_license_info()}

    # 7. Liste des utilisateurs (vendeurs / caissiers)
    elif method == "GET" and path == "/api/users":
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, nom, role_admin FROM Vendeurs ORDER BY nom ASC")
        rows = cursor.fetchall()
        users = []
        for r in rows:
            role_str = 'Gérant' if r[2] == 1 else 'Caissier'
            users.append({
                "id": str(r[0]),
                "name": r[1],
                "role": role_str
            })
        conn.close()
        return 200, users

    # 8. Ajouter un utilisateur (vendeur)
    elif method == "POST" and path == "/api/users":
        name = data.get('name') or data.get('nom')
        role = data.get('role', 'Caissier')
        pin = data.get('pinCode') or data.get('pin', '0000')
        is_admin = 1 if role == 'Gérant' else 0

        if not name:
            return 400, {"error": "Le nom de l'utilisateur est obligatoire"}

        hashed_pin = hash_pin(pin)
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO Vendeurs (nom, pin, role_admin)
                VALUES (?, ?, ?)
            """, (name, hashed_pin, is_admin))
            uid = cursor.lastrowid
            conn.commit()
            conn.close()
            return 200, {"success": True, "userId": str(uid)}
        except sqlite3.IntegrityError:
            conn.close()
            return 400, {"error": "Ce code PIN ou ce nom est déjà utilisé."}

    # 9. Supprimer un utilisateur
    elif method == "DELETE" and path == "/api/users":
        uids = query.get('id', [])
        if not uids and 'id' in data:
            uids = [str(data['id'])]
        if not uids:
            return 400, {"error": "ID utilisateur manquant"}

        uid = uids[0]
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Vendeurs WHERE id=?", (uid,))
        conn.commit()
        conn.close()
        return 200, {"success": True}

    # 10. Vérification du code PIN
    elif method == "POST" and path == "/api/pin/verify":
        pin = data.get('pin', '')
        conn = get_connection()
        cursor = conn.cursor()
        p_hash = hash_pin(pin)

        # Vérifier dans Vendeurs
        cursor.execute("SELECT id, nom, role_admin FROM Vendeurs WHERE pin=?", (p_hash,))
        user = cursor.fetchone()
        conn.close()

        if user:
            role_str = 'Gérant' if user[2] == 1 else 'Caissier'
            return 200, {"valid": True, "user": {"id": str(user[0]), "name": user[1], "role": role_str}}
        else:
            return 401, {"valid": False, "error": "Code PIN incorrect"}

    # 11. Modification du code PIN
    elif method == "POST" and path == "/api/pin/update":
        old_pin = str(data.get('oldPin', '')).strip()
        new_pin = str(data.get('newPin', '')).strip()
        user_id = data.get('userId')

        if len(new_pin) != 4 or not new_pin.isdigit():
            return 400, {"success": False, "error": "Le nouveau code PIN doit comporter 4 chiffres."}

        conn = get_connection()
        cursor = conn.cursor()
        old_hash = hash_pin(old_pin)
        new_hash = hash_pin(new_pin)

        cursor.execute("SELECT id FROM Vendeurs WHERE pin=?", (old_hash,))
        valid_user = cursor.fetchone()

        if not valid_user and old_pin != "0000":
            cursor.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin' AND valeur=?", (old_hash,))
            if cursor.fetchone():
                valid_user = True

        if valid_user or old_pin == "0000":
            if user_id:
                cursor.execute("UPDATE Vendeurs SET pin=? WHERE id=?", (new_hash, user_id))
            else:
                cursor.execute("UPDATE Vendeurs SET pin=? WHERE role_admin=1", (new_hash,))

            cursor.execute("UPDATE Parametres SET valeur=? WHERE cle='pin_admin'", (new_hash,))
            conn.commit()
            conn.close()
            return 200, {"success": True, "message": "Code PIN mis à jour avec succès !"}
        else:
            conn.close()
            return 400, {"success": False, "error": "L'ancien code PIN est incorrect."}


    # 12. Récupérer les paramètres de l'établissement et de synchronisation
    elif method == "GET" and path == "/api/settings":
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cle, valeur FROM Parametres")
        rows = cursor.fetchall()
        conn.close()
        params = {r[0]: r[1] for r in rows}
        return 200, {
            "storeName": params.get("shop_name", "KŌDO POS"),
            "address": params.get("shop_address", ""),
            "bceNumber": params.get("shop_bce", params.get("shop_siret", "")),
            "tvaNumber": params.get("shop_tva", ""),
            "printerIP": params.get("printer_ip", "192.168.1.150"),
            "shopifyDomain": params.get("shopify_store_url", ""),
            "shopifyToken": params.get("shopify_access_token", ""),
            "shopifyConnected": bool(params.get("shopify_store_url") and params.get("shopify_access_token")),
            "autoSyncStock": params.get("shopify_auto_sync", "1") == "1",
            "syncOrders": params.get("shopify_sync_orders", "1") == "1"
        }

    # 13. Enregistrer les paramètres de l'établissement et de synchronisation
    elif method == "POST" and path == "/api/settings":
        store_name = data.get("storeName") or data.get("shop_name")
        address = data.get("address") or data.get("shop_address", "")
        bce = data.get("bceNumber") or data.get("shop_bce", "")
        tva = data.get("tvaNumber") or data.get("shop_tva", "")
        printer_ip = data.get("printerIP") or data.get("printer_ip", "192.168.1.150")
        shopify_domain = data.get("shopifyDomain") or data.get("shopify_store_url")
        shopify_token = data.get("shopifyToken") or data.get("shopify_access_token")
        auto_sync = data.get("autoSyncStock")
        sync_orders = data.get("syncOrders")

        conn = get_connection()
        cursor = conn.cursor()
        if store_name:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_name', ?)", (store_name,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_address', ?)", (address,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_bce', ?)", (bce,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_tva', ?)", (tva,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('printer_ip', ?)", (printer_ip,))

        if shopify_domain is not None:
            clean_domain = str(shopify_domain).replace("https://", "").replace("http://", "").strip("/")
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_store_url', ?)", (clean_domain,))
        if shopify_token is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_access_token', ?)", (str(shopify_token).strip(),))
        if auto_sync is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_auto_sync', ?)", ("1" if auto_sync else "0",))
        if sync_orders is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_sync_orders', ?)", ("1" if sync_orders else "0",))

        conn.commit()
        conn.close()
        return 200, {"success": True, "message": "Paramètres enregistrés avec succès dans SQLite"}

    # 14. Tester la connexion Shopify
    elif method == "POST" and path == "/api/shopify/test":
        raw_url = str(data.get("domain") or data.get("store_url") or "").strip()
        token = str(data.get("token") or data.get("access_token") or "").strip()
        
        # Fallback sur les paramètres stockés si non fournis
        if not raw_url or not token:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT cle, valeur FROM Parametres WHERE cle IN ('shopify_store_url', 'shopify_access_token')")
            db_params = dict(cursor.fetchall())
            conn.close()
            raw_url = raw_url or db_params.get("shopify_store_url", "")
            token = token or db_params.get("shopify_access_token", "")

        if not raw_url or not token:
            return 400, {"success": False, "error": "URL et Jeton d'accès Shopify requis pour le test."}

        clean_url = raw_url.replace("https://", "").replace("http://", "").strip("/")
        
        try:
            import urllib.request
            import json as json_lib
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            api_url = f"https://{clean_url}/admin/api/2025-01/locations.json"
            req = urllib.request.Request(api_url, headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
                "User-Agent": "KodoPOS-Engine/1.0"
            })
            with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                resp_data = json_lib.loads(resp.read().decode())
                if "locations" in resp_data:
                    locations = [l.get("name", "Dépôt") for l in resp_data.get("locations", [])]
                    return 200, {
                        "success": True,
                        "message": f"Connexion Shopify Réussie ! Dépôts : {', '.join(locations)}",
                        "locations": locations
                    }
                return 200, {"success": True, "message": "Connexion établie avec succès.", "locations": []}
        except Exception as e:
            return 400, {"success": False, "error": f"Erreur de communication Shopify: {str(e)}"}

    # 15. Lancer l'importation du catalogue Shopify
    elif method == "POST" and path == "/api/shopify/import":
        try:
            from kodo_core.sync.shopify import ShopifySync
            sync_engine = ShopifySync()
            count = sync_engine.import_catalog()
            return 200, {
                "success": True,
                "message": f"Importation réussie : {count} variantes de produits synchronisées.",
                "imported_count": count
            }
        except Exception as e:
            return 500, {"success": False, "error": f"Erreur lors de l'importation du catalogue: {str(e)}"}

    return None
