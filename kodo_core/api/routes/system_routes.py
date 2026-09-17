# -*- coding: utf-8 -*-
"""
Routes API Système, Version, Licence et Utilisateurs/PIN - Kōdo POS Core
"""

import os
import sys
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
        params = {r[0]: r[1] for r in rows}

        # Fond de caisse actuel de la session active (sans réécriture historique)
        try:
            cursor.execute("SELECT fond_caisse_matin FROM Sessions_Caisse WHERE date_cloture IS NULL ORDER BY id DESC LIMIT 1")
            row_fc = cursor.fetchone()
            fond_caisse = float(row_fc[0]) if (row_fc and row_fc[0] is not None) else float(params.get("fond_caisse_matin", 200.0))
        except Exception:
            fond_caisse = float(params.get("fond_caisse_matin", 200.0))
        conn.close()

        return 200, {
            "storeName": params.get("shop_name", "KŌDO POS"),
            "address": params.get("shop_address", ""),
            "bceNumber": params.get("shop_bce", params.get("shop_siret", "")),
            "tvaNumber": params.get("shop_tva", ""),
            "iban": params.get("shop_iban", "BE68 0000 0000 0000"),
            "fondCaisse": fond_caisse,
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
        iban = data.get("iban") or data.get("shop_iban")
        fond_caisse_val = data.get("fondCaisse") or data.get("fond_caisse")
        printer_ip = data.get("printerIP") or data.get("printer_ip", "192.168.1.150")
        shopify_domain = data.get("shopifyDomain") or data.get("shopify_store_url")
        shopify_token = data.get("shopifyToken") or data.get("shopify_access_token")
        auto_sync = data.get("autoSyncStock")
        sync_orders = data.get("syncOrders")

        conn = get_connection()
        cursor = conn.cursor()
        if fond_caisse_val is not None:
            try:
                from kodo_core.services.cash_session_service import set_fond_caisse_matin
                set_fond_caisse_matin(cursor, str(fond_caisse_val))
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('fond_caisse_matin', ?)", (str(fond_caisse_val),))
            except Exception:
                pass

        if store_name:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_name', ?)", (store_name,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_address', ?)", (address,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_bce', ?)", (bce,))
        cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_tva', ?)", (tva,))
        if iban is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_iban', ?)", (str(iban).strip(),))
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

    # 16. Personnalisation du Ticket : Upload du Logo Boutique
    elif method == "POST" and path == "/api/settings/logo":
        try:
            import base64
            from io import BytesIO
            from PIL import Image

            logo_b64 = data.get("logo") or data.get("logo_base64")
            if not logo_b64:
                return 400, {"success": False, "error": "Données d'image manquantes (logo_base64 requis)"}

            # Nettoyer l'éventuel header data:image/...;base64,
            if "," in logo_b64:
                logo_b64 = logo_b64.split(",", 1)[1]

            image_data = base64.b64decode(logo_b64)
            img = Image.open(BytesIO(image_data))

            # Gestion de la transparence (Alpha channel) : fond blanc net
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == 'RGBA':
                    bg.paste(img, mask=img.split()[-1])
                else:
                    bg.paste(img)
                img = bg

            # Optimisation pour imprimante thermique 80mm ESC/POS (max 384px de large)
            max_width = 384
            if img.width > max_width:
                ratio = max_width / float(img.width)
                new_height = int(float(img.height) * ratio)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

            # Sauvegarder dans tous les chemins potentiels pour assurer la persistance
            import os
            target_path = database_manager.data_path("logo_ticket.png")
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                img.save(target_path, format="PNG")
            except Exception as e:
                print(f"[WARN] Sauvegarde logo data_path: {e}")

            for alt_dir in [
                os.path.expanduser("~/Documents/Kodo_POS"),
                os.path.expanduser("~/Library/Application Support/Kodo_POS"),
                os.path.abspath(".")
            ]:
                try:
                    os.makedirs(alt_dir, exist_ok=True)
                    img.save(os.path.join(alt_dir, "logo_ticket.png"), format="PNG")
                except Exception:
                    pass

            # Préparer le base64 final pour stockage SQLite et retour frontend
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            stored_b64 = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")

            # Mettre à jour en base SQLite (persistance universelle)
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_logo_custom', '1')")
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_logo_b64', ?)", (stored_b64,))
            conn.commit()
            conn.close()

            return 200, {
                "success": True,
                "message": "Logo du ticket enregistré avec succès !",
                "width": img.width,
                "height": img.height,
                "logo_url": stored_b64
            }
        except Exception as e:
            return 500, {"success": False, "error": f"Erreur lors du traitement du logo : {str(e)}"}

    # 17. Obtenir le statut et l'URL du logo actuel
    elif method == "GET" and path == "/api/settings/logo":
        try:
            import os
            import base64

            # 1. Vérifier en base SQLite en premier
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT valeur FROM Parametres WHERE cle = 'receipt_logo_b64'")
                row = cursor.fetchone()
                conn.close()
                if row and row[0] and len(row[0]) > 50:
                    return 200, {"has_logo": True, "logo_url": row[0]}
            except Exception:
                pass

            # 2. Vérifier sur le disque
            candidate_paths = [
                database_manager.data_path("logo_ticket.png"),
                os.path.expanduser("~/Documents/Kodo_POS/logo_ticket.png"),
                os.path.expanduser("~/Library/Application Support/Kodo_POS/logo_ticket.png"),
                os.path.join(os.path.abspath("."), "logo_ticket.png")
            ]
            for target_path in candidate_paths:
                if os.path.exists(target_path) and os.path.getsize(target_path) > 100:
                    with open(target_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    return 200, {"has_logo": True, "logo_url": f"data:image/png;base64,{b64}"}

            return 200, {"has_logo": False}
        except Exception as e:
            return 500, {"has_logo": False, "error": str(e)}

    # 17b. Supprimer le logo personnalisé
    elif method == "DELETE" and path == "/api/settings/logo":
        try:
            import os
            # Supprimer de SQLite
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Parametres WHERE cle IN ('receipt_logo_custom', 'receipt_logo_b64')")
                conn.commit()
                conn.close()
            except Exception:
                pass

            # Supprimer des emplacements de données personnalisées
            candidate_paths = [
                database_manager.data_path("logo_ticket.png"),
                os.path.expanduser("~/Documents/Kodo_POS/logo_ticket.png"),
                os.path.expanduser("~/Library/Application Support/Kodo_POS/logo_ticket.png"),
                os.path.join(os.path.abspath("."), "logo_ticket.png")
            ]
            from kodo_core.hardware.printer import get_resource_path
            default_logo = get_resource_path("logo_ticket.png")
            for p in candidate_paths:
                try:
                    if default_logo and os.path.abspath(p) == os.path.abspath(default_logo):
                        continue
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

            return 200, {"success": True, "message": "Logo supprimé avec succès"}
        except Exception as e:
            return 500, {"success": False, "error": str(e)}

    # 18. Personnalisation du Ticket : Upload du bloc Réseaux Sociaux
    # 18. Personnalisation du Ticket : Upload ou Génération du bloc Réseaux Sociaux / QR Code
    elif method == "POST" and path == "/api/settings/social":
        try:
            import os
            import base64
            from io import BytesIO
            from PIL import Image
            from kodo_core.hardware.printer import generate_social_qr_image

            mode = data.get("mode")

            # Cas 1 : Désactivation explicite du bloc en bas de ticket
            if mode == "none":
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_mode', 'none')")
                cursor.execute("DELETE FROM Parametres WHERE cle IN ('receipt_social_custom', 'receipt_social_b64')")
                conn.commit()
                conn.close()

                target_p = database_manager.data_path("social_ticket.png")
                try:
                    if os.path.exists(target_p):
                        os.remove(target_p)
                except Exception:
                    pass

                return 200, {
                    "success": True,
                    "message": "Bloc en bas de ticket désactivé avec succès.",
                    "mode": "none",
                    "has_social": False
                }

            # Cas 2 : Générateur QR Code dynamique et personnalisable (Réseaux / Web / Avis / etc.)
            elif mode == "qr" or ("url" in data or "title" in data):
                title = (data.get("title") or "REJOIGNEZ-NOUS !").strip()
                url = (data.get("url") or data.get("qr_data") or "https://instagram.com").strip()
                subtitle = (data.get("subtitle") or "").strip()
                qr_size = data.get("qr_size") or "large"

                img = generate_social_qr_image(title=title, url=url, subtitle=subtitle, width=512, qr_size=qr_size)

                buffer = BytesIO()
                img.save(buffer, format="PNG")
                stored_b64 = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")

                target_path = database_manager.data_path("social_ticket.png")
                try:
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    img.save(target_path, format="PNG")
                except Exception as e:
                    print(f"[WARN] Sauvegarde social data_path: {e}")

                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_mode', 'qr')")
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_title', ?)", (title,))
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_url', ?)", (url,))
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_subtitle', ?)", (subtitle,))
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_size', ?)", (qr_size,))
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_custom', '1')")
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_b64', ?)", (stored_b64,))
                conn.commit()
                conn.close()

                return 200, {
                    "success": True,
                    "message": "Bloc QR Code généré et enregistré avec succès !",
                    "width": img.width,
                    "height": img.height,
                    "social_url": stored_b64,
                    "mode": "qr",
                    "title": title,
                    "url": url,
                    "subtitle": subtitle,
                    "qr_size": qr_size
                }

            # Cas 3 : Image personnalisée uploadée directement en base64
            else:
                social_b64 = data.get("social") or data.get("social_base64")
                if not social_b64:
                    return 400, {"success": False, "error": "Données manquantes (mode 'qr' avec url/titre ou image en base64 requise)"}

                if "," in social_b64:
                    social_b64 = social_b64.split(",", 1)[1]

                image_data = base64.b64decode(social_b64)
                img = Image.open(BytesIO(image_data))

                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    if img.mode == 'RGBA':
                        bg.paste(img, mask=img.split()[-1])
                    else:
                        bg.paste(img)
                    img = bg

                max_width = 512
                if img.width > max_width:
                    ratio = max_width / float(img.width)
                    new_height = int(float(img.height) * ratio)
                    img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

                target_path = database_manager.data_path("social_ticket.png")
                try:
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    img.save(target_path, format="PNG")
                except Exception as e:
                    print(f"[WARN] Sauvegarde social data_path: {e}")

                buffer = BytesIO()
                img.save(buffer, format="PNG")
                stored_b64 = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")

                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_mode', 'custom_image')")
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_custom', '1')")
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('receipt_social_b64', ?)", (stored_b64,))
                conn.commit()
                conn.close()

                return 200, {
                    "success": True,
                    "message": "Visuel personnalisé enregistré avec succès !",
                    "width": img.width,
                    "height": img.height,
                    "social_url": stored_b64,
                    "mode": "custom_image"
                }
        except Exception as e:
            return 500, {"success": False, "error": f"Erreur lors du traitement du bloc réseaux sociaux : {str(e)}"}

    # 18b. Obtenir le statut et la configuration du bloc réseaux sociaux / QR Code
    elif method == "GET" and path == "/api/settings/social":
        try:
            import os
            import base64

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT cle, valeur FROM Parametres WHERE cle LIKE 'receipt_social_%'")
            params = dict(cursor.fetchall())
            conn.close()

            mode = params.get("receipt_social_mode")
            title = params.get("receipt_social_title", "SUIVEZ-NOUS SUR NOS RÉSEAUX !")
            url = params.get("receipt_social_url", "https://instagram.com")
            subtitle = params.get("receipt_social_subtitle", "")
            qr_size = params.get("receipt_social_size", "large")
            stored_b64 = params.get("receipt_social_b64")

            if mode == "none":
                return 200, {
                    "has_social": False,
                    "mode": "none",
                    "title": title,
                    "url": url,
                    "subtitle": subtitle,
                    "qr_size": qr_size
                }

            if stored_b64 and len(stored_b64) > 50:
                return 200, {
                    "has_social": True,
                    "mode": mode or "custom_image",
                    "social_url": stored_b64,
                    "title": title,
                    "url": url,
                    "subtitle": subtitle,
                    "qr_size": qr_size
                }

            candidate_paths = [
                database_manager.data_path("social_ticket.png"),
                os.path.expanduser("~/Documents/Kodo_POS/social_ticket.png"),
                os.path.expanduser("~/Library/Application Support/Kodo_POS/social_ticket.png"),
                os.path.join(os.path.abspath("."), "social_ticket.png")
            ]
            for target_path in candidate_paths:
                if os.path.exists(target_path) and os.path.getsize(target_path) > 100:
                    with open(target_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    return 200, {
                        "has_social": True,
                        "mode": mode or "custom_image",
                        "social_url": f"data:image/png;base64,{b64}",
                        "title": title,
                        "url": url,
                        "subtitle": subtitle,
                        "qr_size": qr_size
                    }

            from kodo_core.hardware.printer import get_resource_path
            default_p = get_resource_path("instagram_block.png")
            if os.path.exists(default_p) and os.path.getsize(default_p) > 100:
                with open(default_p, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                return 200, {
                    "has_social": False,
                    "mode": "default",
                    "social_url": f"data:image/png;base64,{b64}",
                    "title": "SUIVEZ-NOUS SUR NOS RÉSEAUX !",
                    "url": "https://instagram.com",
                    "subtitle": "",
                    "qr_size": "large"
                }

            return 200, {"has_social": False, "mode": "none"}
        except Exception as e:
            return 500, {"has_social": False, "error": str(e)}

    # 18c. Supprimer le bloc personnalisé (retour au bloc par défaut)
    elif method == "DELETE" and path == "/api/settings/social":
        try:
            import os
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Parametres WHERE cle LIKE 'receipt_social_%'")
                conn.commit()
                conn.close()
            except Exception:
                pass

            candidate_paths = [
                database_manager.data_path("social_ticket.png"),
                os.path.expanduser("~/Documents/Kodo_POS/social_ticket.png"),
                os.path.expanduser("~/Library/Application Support/Kodo_POS/social_ticket.png"),
                os.path.join(os.path.abspath("."), "social_ticket.png")
            ]
            from kodo_core.hardware.printer import get_resource_path
            default_social = get_resource_path("instagram_block.png")
            for p in candidate_paths:
                try:
                    if default_social and os.path.abspath(p) == os.path.abspath(default_social):
                        continue
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

            return 200, {"success": True, "message": "Bloc de communication réinitialisé au bloc par défaut"}
        except Exception as e:
            return 500, {"success": False, "error": str(e)}

    # 18. Statut des imprimantes (détection USB / CUPS / Windows)
    elif method == "GET" and (path == "/api/printers" or path == "/api/printer/status" or path == "/api/settings/printers"):
        try:
            import subprocess, re
            printers_list = []
            default_printer = None

            if sys.platform in ["darwin", "linux"]:
                try:
                    out_d = subprocess.check_output(["lpstat", "-d"], stderr=subprocess.DEVNULL, timeout=2).decode()
                    m_d = re.search(r':\s*(\S+)', out_d)
                    if m_d:
                        default_printer = m_d.group(1)
                except Exception:
                    pass

                try:
                    out_v = subprocess.check_output(["lpstat", "-v"], stderr=subprocess.DEVNULL, timeout=2).decode()
                    for line in out_v.splitlines():
                        m_v = re.search(r'p[ée]riph[ée]rique pour (\S+)\s*:\s*(.+)', line, re.IGNORECASE)
                        if m_v:
                            p_name = m_v.group(1)
                            p_uri = m_v.group(2).strip()
                            is_usb = "usb://" in p_uri.lower()
                            printers_list.append({
                                "name": p_name,
                                "uri": p_uri,
                                "is_usb": is_usb,
                                "is_default": (p_name == default_printer)
                            })
                except Exception:
                    pass

            return 200, {
                "success": True,
                "defaultPrinter": default_printer,
                "printers": printers_list,
                "has_usb_printer": any(p.get("is_usb") for p in printers_list)
            }
        except Exception as e:
            return 200, {"success": False, "error": str(e), "printers": []}

    # 19. Impression d'un ticket test
    elif method == "POST" and (path in [
        "/api/printer/test", "/api/settings/printer/test", "/api/print/test",
        "/api/printer", "/api/printers/test", "/api/hardware/printer/test"
    ]):
        try:
            import ticket_printer
            printer_ip = data.get("printerIP") or data.get("printer_ip")
            printer_name = data.get("printerName") or data.get("printer_name")
            res = ticket_printer.imprimer_ticket_test(printer_name=printer_name, host=printer_ip)
            return 200, {
                "success": True,
                "message": "Ticket de test envoyé à l'imprimante avec succès !",
                "receiptNumber": res.get("receiptNumber"),
                "file": res.get("file"),
                "printerIP": res.get("printerIP")
            }
        except Exception as e:
            return 500, {"success": False, "error": f"Erreur lors de l'impression du ticket test : {str(e)}"}

    # 20. Commande ouverture tiroir-caisse
    elif method == "POST" and (path == "/api/printer/open-drawer" or path == "/api/cash-drawer/open"):
        try:
            import ticket_printer
            res = ticket_printer.ouvrir_tiroir_caisse()
            return 200, {"success": res, "message": "Signal d'ouverture envoyé au tiroir-caisse"}
        except Exception as e:
            return 500, {"success": False, "error": str(e)}

    return None

