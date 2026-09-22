# -*- coding: utf-8 -*-
"""
Routes API Système, Version, Licence et Utilisateurs/PIN - Kōdo POS Core
"""

import os
import sys
import time
import datetime
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Tuple, Optional

import database_manager
from database_manager import get_connection, hash_pin, verify_pin_hash
import license_manager
import services.update_checker as update_checker
from kodo_core.api.session_manager import create_session_token

# Mécanisme de Rate Limiting anti-bruteforce en mémoire pour les vérifications de PIN
# Structure: { client_id: {"failures": int, "locked_until": float} }
_PIN_ATTEMPTS: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------------------------
# ÉTIQUETTES CODE-BARRES : réglages de l'étiqueteuse et inventaire du matériel.
#
# AUCUN FORMAT PAR DÉFAUT N'EST INVENTÉ. Tant que l'étiqueteuse et la taille de l'étiquette ne sont
# pas renseignées, l'impression est refusée. Un format supposé (« on met du 57 × 32, ça passera »)
# produit une étiquette dont les barres sont tronquées ou mal dimensionnées : le code reste lisible
# à l'œil mais la douchette ne le lit plus. La commerçante ne s'en aperçoit qu'en caisse, une fois
# les étiquettes collées sur la marchandise.
# ---------------------------------------------------------------------------------------------

CLES_REGLAGES_ETIQUETTE = (
    "label_printer_name",
    "label_format_id",
    "label_width_mm",
    "label_height_mm",
    "label_margin_mm",
    "label_orientation",
    "label_dpi",
    "label_show_price",
)

# Réglages sans lesquels aucune étiquette ne peut être imprimée.
CLES_ETIQUETTE_OBLIGATOIRES = ("label_printer_name", "label_width_mm", "label_height_mm")

ORIENTATIONS_ETIQUETTE = {"portrait": "portrait", "paysage": "paysage", "landscape": "paysage"}

MESSAGE_ETIQUETEUSE_NON_CONFIGUREE = (
    "Étiqueteuse non configurée : choisissez l'imprimante à étiquettes et indiquez la taille de "
    "l'étiquette (largeur et hauteur en millimètres) dans les réglages avant d'imprimer."
)

# Repères de reconnaissance du matériel, lus dans le `printer-make-and-model` déclaré par CUPS.
# PUREMENT INDICATIF : l'écran s'en sert pour mettre une étiqueteuse en avant, jamais pour filtrer
# la liste. Une imprimante absente de ces tables reste sélectionnable — sinon un modèle non répertorié
# deviendrait inutilisable chez la cliente.
MODELES_ETIQUETEUSE = (
    "dymo", "labelwriter", "label printer", "brother ql", "zebra", "zdesigner", "godex",
    "tsc ", "sato", "bixolon slp", "citizen cl-", "intermec", "pc42", "argox", "seiko slp",
)
MODELES_TICKET = (
    "tm-t", "tm-m", "epson tm", "star tsp", "star tup", "srp-", "citizen ct-",
    "pos-80", "pos80", "pos-58", "receipt",
)


def lire_reglages_etiquette(cursor=None) -> Dict[str, Any]:
    """
    Relit les réglages de l'étiqueteuse depuis la table `Parametres`.

    Retourne toujours les mêmes clés, avec une chaîne VIDE quand le réglage n'a jamais été saisi :
    l'écran doit pouvoir distinguer « jamais configuré » d'une valeur réelle, et rien n'est supposé.
    `est_configuree` reste faux tant qu'un réglage indispensable manque ; `message` est alors
    directement affichable telle quelle à la commerçante.
    """
    conn = None
    if cursor is None:
        conn = get_connection()
        cursor = conn.cursor()
    try:
        marques = ",".join("?" * len(CLES_REGLAGES_ETIQUETTE))
        cursor.execute(
            f"SELECT cle, valeur FROM Parametres WHERE cle IN ({marques})",
            CLES_REGLAGES_ETIQUETTE,
        )
        stockes = {r[0]: (r[1] if r[1] is not None else "") for r in cursor.fetchall()}
    finally:
        if conn is not None:
            conn.close()

    reglages: Dict[str, Any] = {cle: str(stockes.get(cle, "")).strip() for cle in CLES_REGLAGES_ETIQUETTE}
    manquants = [cle for cle in CLES_ETIQUETTE_OBLIGATOIRES if not reglages.get(cle)]
    reglages["est_configuree"] = not manquants
    reglages["reglages_manquants"] = manquants
    reglages["message"] = "" if not manquants else MESSAGE_ETIQUETEUSE_NON_CONFIGUREE
    return reglages


def _classer_imprimante(modele: str, uri: str) -> Tuple[str, str]:
    """
    « etiqueteuse », « ticket » ou « inconnu », avec la source ayant servi au classement.

    L'URI est examinée AVANT le modèle : elle contient le nom que le matériel déclare lui-même
    (`usb://Printer/POS-80`), alors que `printer-make-and-model` ne reflète que le pilote choisi à
    l'installation. Un cas réel du poste de développement le montre : une imprimante à tickets POS-80
    installée avec un PPD DYMO s'annonce « DYMO Label Printer ». Classer sur le modèle seul y
    désignerait l'imprimante à tickets comme étiqueteuse — et les étiquettes partiraient sur le
    rouleau de tickets.

    Le résultat reste INDICATIF : il met une file en avant dans l'écran, il n'en exclut aucune.
    """
    signature_uri = (uri or "").lower()
    if any(mot in signature_uri for mot in MODELES_TICKET):
        return "ticket", "uri"
    if any(mot in signature_uri for mot in MODELES_ETIQUETEUSE):
        return "etiqueteuse", "uri"

    signature_modele = (modele or "").lower()
    if any(mot in signature_modele for mot in MODELES_TICKET):
        return "ticket", "modele"
    if any(mot in signature_modele for mot in MODELES_ETIQUETEUSE):
        return "etiqueteuse", "modele"

    return "inconnu", ""


def _lister_imprimantes_cups(limite: int = 20) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Inventaire des imprimantes installées, INDÉPENDANT DE LA LANGUE DU SYSTÈME.

    `lpstat -v` est inutilisable pour cela : il répond en toutes lettres — « périphérique pour X : »
    en français, « device for X: » en anglais, « apparaat voor X: » en néerlandais. Une lecture par
    expression régulière française renvoie donc une liste VIDE sur un Mac anglais ou néerlandais,
    configuration courante chez une clientèle belge : l'écran des réglages n'affiche alors aucune
    imprimante et la commerçante ne peut plus en choisir une.

    `lpstat -e` ne renvoie que des noms, un par ligne, et `lpoptions -p <nom>` des paires
    clé=valeur : ni l'un ni l'autre n'est traduit.
    """
    if sys.platform not in ("darwin", "linux"):
        return [], None

    import shlex
    import subprocess

    def _executer(args: List[str]) -> str:
        try:
            return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=2).decode("utf-8", "replace")
        except Exception:
            return ""

    noms = [ligne.strip() for ligne in _executer(["lpstat", "-e"]).splitlines() if ligne.strip()][:limite]

    # « destination système par défaut : NOM » / « system default destination: NOM » : seul le nom,
    # après le dernier « : », est commun à toutes les langues. Quand il n'y a pas de destination par
    # défaut, CUPS répond une phrase sans deux-points, dans toutes les langues également.
    defaut = None
    sortie_defaut = _executer(["lpstat", "-d"])
    if ":" in sortie_defaut:
        defaut = sortie_defaut.split(":")[-1].strip() or None

    imprimantes: List[Dict[str, Any]] = []
    for nom in noms:
        options: Dict[str, str] = {}
        try:
            jetons = shlex.split(_executer(["lpoptions", "-p", nom]))
        except ValueError:
            jetons = []
        for jeton in jetons:
            if "=" in jeton:
                cle, valeur = jeton.split("=", 1)
                options[cle] = valeur

        formats: List[str] = []
        resolutions: List[str] = []
        for ligne in _executer(["lpoptions", "-p", nom, "-l"]).splitlines():
            if ":" not in ligne:
                continue
            gauche, droite = ligne.split(":", 1)
            mot_cle = gauche.split("/", 1)[0].strip()
            choix = [c.lstrip("*") for c in droite.split() if c.strip()]
            if mot_cle == "PageSize":
                formats = choix
            elif mot_cle == "Resolution":
                resolutions = choix

        modele = options.get("printer-make-and-model", "")
        uri = options.get("device-uri", "")
        genre, genre_source = _classer_imprimante(modele, uri)
        imprimantes.append({
            "name": nom,
            "uri": uri,
            "model": modele,
            "info": options.get("printer-info", ""),
            "location": options.get("printer-location", ""),
            "is_usb": uri.lower().startswith("usb:"),
            "is_network": uri.lower().startswith(("socket:", "ipp:", "ipps:", "lpd:", "dnssd:")),
            "is_default": (nom == defaut),
            "accepting_jobs": options.get("printer-is-accepting-jobs", "") == "true",
            "kind": genre,
            "kind_source": genre_source,
            "page_sizes": formats,
            "resolutions": resolutions,
        })

    return imprimantes, defaut


def handle_system_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, Any]]:
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

    # 1bis. Bilan de santé complet & diagnostic d'intégrité v2.0
    elif method == "GET" and path == "/api/system/health":
        from kodo_core.services.client_sanitizer import get_system_health_report
        health = get_system_health_report()
        return 200, health

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
        pin = str(data.get('pinCode') or data.get('pin') or '').strip()
        is_admin = 1 if role == 'Gérant' else 0

        if not name:
            return 400, {"error": "Le nom de l'utilisateur est obligatoire"}
        if len(pin) != 4 or not pin.isdigit():
            return 400, {"error": "Le code PIN doit comporter 4 chiffres."}

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
        pin = str(data.get('pin', '')).strip()

        # Protection Anti-Bruteforce (Rate Limiting)
        client_id = (headers.get('x-forwarded-for') or headers.get('remote-addr') or 'local') if headers else 'local'
        now = time.time()
        attempt_info = _PIN_ATTEMPTS.get(client_id, {"failures": 0, "locked_until": 0.0})
        if now < attempt_info.get("locked_until", 0.0):
            retry_after = int(attempt_info["locked_until"] - now) + 1
            return 429, {
                "valid": False,
                "error": f"Trop de tentatives infructueuses. Veuillez patienter {retry_after} secondes.",
                "locked": True,
                "retry_after": retry_after
            }

        conn = get_connection()
        cursor = conn.cursor()

        # Vérifier dans Vendeurs (support PBKDF2 et SHA-256 hérité avec migration)
        cursor.execute("SELECT id, nom, role_admin, pin FROM Vendeurs")
        vendeurs = cursor.fetchall()
        user = None
        rehash_id = None
        for v in vendeurs:
            is_valid, needs_rehash = verify_pin_hash(pin, v[3])
            if is_valid:
                user = v
                if needs_rehash:
                    rehash_id = v[0]
                break

        if user:
            if rehash_id:
                # Migration automatique transparente du hash SHA-256 hérité vers PBKDF2
                cursor.execute("UPDATE Vendeurs SET pin=? WHERE id=?", (hash_pin(pin), rehash_id))
                conn.commit()
            conn.close()
            _PIN_ATTEMPTS[client_id] = {"failures": 0, "locked_until": 0.0}
            role_str = 'Gérant' if user[2] == 1 else 'Caissier'
            user_id_str = str(user[0])
            user_name = user[1]
            token = create_session_token(user_id=user_id_str, user_name=user_name, role=role_str)
            return 200, {
                "valid": True,
                "user": {"id": user_id_str, "name": user_name, "role": role_str},
                "token": token,
                "session_token": token,
                "default_pin_warning": (pin == "0000")
            }

        # Aucun Gérant dans Vendeurs : le PIN maître (pin_admin) fait foi
        cursor.execute("SELECT COUNT(*) FROM Vendeurs WHERE role_admin=1")
        no_admin = cursor.fetchone()[0] == 0
        master_ok = False
        master_rehash = False
        if no_admin:
            cursor.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin'")
            row = cursor.fetchone()
            if row:
                master_ok, master_rehash = verify_pin_hash(pin, row[0])
                if master_ok and master_rehash:
                    cursor.execute("UPDATE Parametres SET valeur=? WHERE cle='pin_admin'", (hash_pin(pin),))
                    conn.commit()
        conn.close()

        if master_ok:
            _PIN_ATTEMPTS[client_id] = {"failures": 0, "locked_until": 0.0}
            token = create_session_token(user_id="0", user_name="Administrateur", role="Gérant")
            return 200, {
                "valid": True,
                "user": {"id": "0", "name": "Administrateur", "role": "Gérant"},
                "token": token,
                "session_token": token,
                "default_pin_warning": (pin == "0000")
            }

        # Échec de vérification : incrémenter le compteur d'échecs
        failures = attempt_info.get("failures", 0) + 1
        locked_until = 0.0
        if failures >= 10:
            locked_until = now + 300.0  # 5 minutes
        elif failures >= 5:
            locked_until = now + 30.0   # 30 secondes

        _PIN_ATTEMPTS[client_id] = {"failures": failures, "locked_until": locked_until}
        if locked_until > now:
            retry_after = int(locked_until - now) + 1
            return 429, {
                "valid": False,
                "error": f"Trop de tentatives infructueuses. Veuillez patienter {retry_after} secondes.",
                "locked": True,
                "retry_after": retry_after
            }

        return 401, {"valid": False, "error": "Code PIN incorrect"}

    # 11. Modification du code PIN
    elif method == "POST" and path == "/api/pin/update":
        old_pin = str(data.get('oldPin', '')).strip()
        new_pin = str(data.get('newPin', '')).strip()
        user_id = data.get('userId')
        if str(user_id or '').strip() in ('', '0', 'None', 'null', 'undefined'):
            user_id = None  # "0" = administrateur maître (base sans vendeur)

        if len(new_pin) != 4 or not new_pin.isdigit():
            return 400, {"success": False, "error": "Le nouveau code PIN doit comporter 4 chiffres."}

        conn = get_connection()
        cursor = conn.cursor()
        new_hash = hash_pin(new_pin)

        # Ancien PIN : doit correspondre à un vendeur, ou au PIN maître (tant qu'aucun Gérant n'existe)
        cursor.execute("SELECT id, role_admin, pin FROM Vendeurs")
        all_vendeurs = cursor.fetchall()
        matched = None
        for v in all_vendeurs:
            is_valid, _ = verify_pin_hash(old_pin, v[2])
            if is_valid:
                matched = v
                break

        cursor.execute("SELECT COUNT(*) FROM Vendeurs WHERE role_admin=1")
        no_admin = cursor.fetchone()[0] == 0
        master_ok = False
        if not matched and no_admin:
            cursor.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin'")
            row = cursor.fetchone()
            if row:
                master_ok, _ = verify_pin_hash(old_pin, row[0])

        if not matched and not master_ok:
            conn.close()
            return 400, {"success": False, "error": "L'ancien code PIN est incorrect."}

        # Cible : l'utilisateur demandé (seulement si l'ancien PIN est le sien ou celui d'un gérant),
        # sinon le vendeur dont l'ancien PIN a été saisi.
        if user_id and matched and str(matched[0]) != str(user_id) and matched[1] != 1:
            conn.close()
            return 400, {"success": False, "error": "L'ancien code PIN est incorrect."}
        target_id = user_id or (matched[0] if matched else None)

        if target_id:
            cursor.execute("SELECT id, role_admin FROM Vendeurs WHERE id=?", (target_id,))
            target = cursor.fetchone()
            if not target:
                conn.close()
                return 400, {"success": False, "error": "Utilisateur introuvable."}

            # Vérifier si le nouveau PIN est déjà utilisé
            cursor.execute("SELECT id, pin FROM Vendeurs WHERE id!=?", (target[0],))
            other_vendeurs = cursor.fetchall()
            taken = any(verify_pin_hash(new_pin, ov[1])[0] for ov in other_vendeurs)
            if not taken and no_admin and target[1] != 1:
                cursor.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin'")
                master_row = cursor.fetchone()
                if master_row:
                    taken = verify_pin_hash(new_pin, master_row[0])[0]

            if taken:
                conn.close()
                return 400, {"success": False, "error": "Ce code PIN est déjà utilisé par un autre utilisateur."}

            cursor.execute("UPDATE Vendeurs SET pin=? WHERE id=?", (new_hash, target[0]))
            update_master = target[1] == 1
        else:
            update_master = True

        # Le PIN maître ne suit que les changements du gérant (ou du PIN maître lui-même)
        if update_master:
            cursor.execute("""
                INSERT INTO Parametres (cle, valeur) VALUES ('pin_admin', ?)
                ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur
            """, (new_hash,))
        conn.commit()
        conn.close()
        return 200, {"success": True, "message": "Code PIN mis à jour avec succès !"}


    # 12. Récupérer les paramètres de l'établissement et de synchronisation
    elif method == "GET" and path == "/api/settings":
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cle, valeur FROM Parametres")
        rows = cursor.fetchall()
        params = {r[0]: r[1] for r in rows}

        # Fond de caisse actuel de la session active (sans réécriture historique)
        try:
            from kodo_core.services.cash_session_service import get_fond_caisse_matin
            fond_caisse = get_fond_caisse_matin(cursor)
        except Exception:
            fond_caisse = 0.0
        conn.close()
        try:
            default_alert = int(params.get("default_seuil_alerte", 5))
        except (ValueError, TypeError):
            default_alert = 5

        return 200, {
            "storeName": params.get("shop_name", "KŌDO POS"),
            "address": params.get("shop_address", ""),
            "bceNumber": params.get("shop_bce", params.get("shop_siret", "")),
            "tvaNumber": params.get("shop_tva", ""),
            # Valeur vide, jamais fictive : l'écran distingue « non renseigné » d'un IBAN réel.
            "iban": params.get("shop_iban", ""),
            "fondCaisse": fond_caisse,
            # Valeur vide plutôt qu'une IP inventée : « 192.168.1.150 » désigne un appareil
            # quelconque du réseau du commerçant, pas forcément son imprimante.
            "printerIP": params.get("printer_ip", ""),
            "shopifyDomain": params.get("shopify_store_url", ""),
            # Le jeton d'administration Shopify n'est JAMAIS renvoyé. Il ouvre le catalogue,
            # les stocks et les commandes de la boutique ; le servir en clair à chaque
            # ouverture de l'écran Réglages le faisait transiter puis dormir dans le stockage
            # du navigateur, hors de la base et hors de toute sauvegarde chiffrée, à la portée
            # de n'importe quelle extension ou de quiconque ouvre la caisse. L'écran n'a pas
            # besoin de le lire : il a besoin de savoir s'il y en a un, et de pouvoir le
            # remplacer. C'est exactement ce que disent les deux champs ci-dessous.
            "shopifyToken": "",
            "shopifyTokenEnregistre": bool(params.get("shopify_access_token")),
            "shopifyConnected": bool(params.get("shopify_store_url") and params.get("shopify_access_token")),
            "autoSyncStock": params.get("shopify_auto_sync", "1") == "1",
            "syncOrders": params.get("shopify_sync_orders", "1") == "1",
            "defaultAlertThreshold": default_alert,
            "default_seuil_alerte": default_alert
        }

    # 13. Enregistrer les paramètres de l'établissement et de synchronisation
    elif method == "POST" and path == "/api/settings":
        # Mise à jour PARTIELLE : seules les clés réellement présentes dans la requête sont écrites.
        # Avant, une requête ne contenant que le fond de caisse (ou le seuil d'alerte) effaçait l'adresse,
        # le n° BCE, le n° TVA (mentions légales des tickets) et remettait l'IP de l'imprimante par défaut ;
        # et un fond de caisse à 0 (valeur « fausse » en Python) était ignoré sans erreur.
        def _pick(*keys):
            for k in keys:
                if k in data and data[k] is not None:
                    return data[k]
            return None

        def _est_un_masque(valeur):
            # Les espaces sont ignorés : un masque affiché peut être groupé (« ••• ••• »),
            # et un vrai jeton Shopify n'en contient jamais.
            texte = "".join(str(valeur or "").split())
            return bool(texte) and all(caractere in "\u2022*\u00b7\u2219\u25cf." for caractere in texte)

        store_name = _pick("storeName", "shop_name")
        address = _pick("address", "shop_address")
        bce = _pick("bceNumber", "shop_bce")
        tva = _pick("tvaNumber", "shop_tva")
        iban = _pick("iban", "shop_iban")
        fond_caisse_val = _pick("fondCaisse", "fond_caisse")
        printer_ip = _pick("printerIP", "printer_ip")
        shopify_domain = _pick("shopifyDomain", "shopify_store_url")
        shopify_token = _pick("shopifyToken", "shopify_access_token")
        auto_sync = data.get("autoSyncStock")
        sync_orders = data.get("syncOrders")

        if fond_caisse_val is not None:
            try:
                fond_caisse_dec = Decimal(str(fond_caisse_val))
            except Exception:
                return 400, {"error": f"Fond de caisse invalide : {fond_caisse_val!r} n'est pas un montant numérique."}
            if fond_caisse_dec < 0:
                return 400, {"error": "Le fond de caisse ne peut pas être négatif."}
            fond_caisse_val = str(fond_caisse_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

        conn = get_connection()
        cursor = conn.cursor()
        if fond_caisse_val is not None:
            # Erreur non journalisée ici de façon volontairement fatale : un échec silencieux
            # (`except: pass`) faisait auparavant croire au commerçant que son fond de caisse
            # avait été sauvegardé (success: true) alors que rien n'avait été persisté.
            from kodo_core.services.cash_session_service import set_fond_caisse_matin
            set_fond_caisse_matin(cursor, fond_caisse_val)
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('fond_caisse_matin', ?)", (fond_caisse_val,))

        if store_name:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_name', ?)", (store_name,))
        if address is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_address', ?)", (address,))
        if bce is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_bce', ?)", (bce,))
        if tva is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_tva', ?)", (tva,))
        if iban is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shop_iban', ?)", (str(iban).strip(),))
        if printer_ip is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('printer_ip', ?)", (printer_ip,))

        if shopify_domain is not None:
            # Même normalisation que le moteur de synchro, pour que la base stocke un
            # domaine propre : un domaine collé depuis l'admin Shopify (« ...myshopify.com/admin »)
            # ne perdait que son protocole et produisait ensuite « /admin/admin/api/... »,
            # un 404 que l'application lisait comme « la boutique n'a aucun produit ».
            try:
                from kodo_core.sync.shopify import normaliser_domaine_boutique
                clean_domain = normaliser_domaine_boutique(shopify_domain)
            except Exception:
                clean_domain = str(shopify_domain).replace("https://", "").replace("http://", "").strip("/")
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_store_url', ?)", (clean_domain,))
        if shopify_token is not None and not _est_un_masque(shopify_token):
            # Une chaîne faite uniquement de puces ou d'étoiles est un AFFICHAGE, pas un jeton :
            # un client qui renverrait le masque qu'il a à l'écran remplacerait la vraie clé par
            # des points et débrancherait la boutique en silence. La chaîne vide, elle, reste un
            # ordre légitime : c'est ainsi que l'écran déconnecte la boutique.
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_access_token', ?)", (str(shopify_token).strip(),))
        if auto_sync is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_auto_sync', ?)", ("1" if auto_sync else "0",))
        if sync_orders is not None:
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('shopify_sync_orders', ?)", ("1" if sync_orders else "0",))

        default_alert_raw = data.get("defaultAlertThreshold") if data.get("defaultAlertThreshold") is not None else data.get("default_seuil_alerte")
        if default_alert_raw is not None:
            try:
                alert_int = max(0, int(default_alert_raw))
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('default_seuil_alerte', ?)", (str(alert_int),))
            except (ValueError, TypeError):
                pass

        conn.commit()
        conn.close()

        # Les réglages Shopify prennent effet tout de suite. Sans cela, brancher (ou
        # débrancher) la boutique n'avait d'effet qu'au redémarrage de la caisse : la
        # commerçante voyait « enregistré » et croyait la synchro active pour la journée.
        # start_auto_sync() est idempotent et relit lui-même la configuration ; il ne
        # démarre rien si la boutique n'est pas configurée ou si les deux sens sont éteints.
        if any(v is not None for v in (shopify_domain, shopify_token, auto_sync, sync_orders)):
            try:
                from kodo_core.sync.shopify import start_auto_sync
                start_auto_sync()
            except Exception as _sync_err:
                print(f"⚠️ [SHOPIFY] Réglages enregistrés mais synchro non relancée : {_sync_err}")

        return 200, {"success": True, "message": "Paramètres enregistrés avec succès dans SQLite"}

    # 14. Tester la connexion Shopify
    elif method == "POST" and path == "/api/shopify/test":
        raw_url = str(data.get("domain") or data.get("store_url") or "").strip()
        token = str(data.get("token") or data.get("access_token") or "").strip()
        
        # Le couple domaine + jeton ne se mélange JAMAIS entre l'appelant et la base. Compléter
        # champ par champ laissait envoyer le jeton d'administration enregistré vers un domaine
        # choisi par l'appelant : une seule requête sans jeton suffisait à l'exfiltrer. L'écran
        # Paramètres envoie toujours les deux champs (il refuse le test autrement), donc seul le
        # repli complet — relire la configuration déjà enregistrée — reste utile.
        if not raw_url and not token:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT cle, valeur FROM Parametres WHERE cle IN ('shopify_store_url', 'shopify_access_token')")
            db_params = dict(cursor.fetchall())
            conn.close()
            raw_url = db_params.get("shopify_store_url", "") or ""
            token = db_params.get("shopify_access_token", "") or ""
            if not raw_url or not token:
                return 400, {"success": False, "error": "Aucune boutique Shopify enregistrée à tester."}
        elif not raw_url or not token:
            return 400, {"success": False, "error": (
                "Indiquez le domaine ET le jeton à tester ensemble, ou aucun des deux pour "
                "retester la boutique déjà enregistrée.")}

        # Le test passe par le MÊME moteur que la synchronisation réelle : même normalisation du
        # domaine et même transport TLS vérifié. La route réimplémentait sa propre requête avec
        # `CERT_NONE` : le jeton d'administration partait dans un tunnel non vérifié, et un
        # « Connexion réussie ! » pouvait s'afficher alors que la synchro, elle, n'aboutissait pas.
        try:
            from kodo_core.sync.shopify import ShopifySync, start_auto_sync
            resultat = ShopifySync(store_url=raw_url, access_token=token).tester_connexion()
            if resultat.get("success"):
                # La boutique répond : c'est le moment où la configuration devient utilisable,
                # donc celui où la synchronisation automatique doit prendre le relais.
                try:
                    start_auto_sync()
                except Exception:
                    pass
                return 200, resultat
            return 400, resultat
        except Exception as e:
            return 400, {"success": False, "error": f"Erreur de communication Shopify: {str(e)}"}

    # 14 bis. État de la dernière synchronisation Shopify
    elif method == "GET" and path == "/api/shopify/status":
        # Sans ce retour, une synchronisation qui échoue en boucle (jeton révoqué, domaine mal
        # saisi) ne remonte nulle part : l'interrupteur reste allumé et rien ne circule.
        from kodo_core.sync.shopify import (
            lire_reglages_shopify, lire_etat_sync, normaliser_domaine_boutique,
            domaine_boutique_valide, auto_sync_actif,
        )
        reglages = lire_reglages_shopify()
        domaine = normaliser_domaine_boutique(reglages["store_url"])
        etat = lire_etat_sync()
        return 200, {
            "success": True,
            "domain": domaine,
            "domainValid": domaine_boutique_valide(domaine),
            "configured": bool(domaine_boutique_valide(domaine) and reglages["access_token"]),
            "autoSyncStock": reglages["auto_sync"],
            "syncOrders": reglages["sync_orders"],
            "running": auto_sync_actif(),
            "lastSyncAt": etat["derniere_synchro"],
            "lastSyncOk": etat["succes"],
            "lastSyncMessage": etat["message"],
        }

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
    elif method == "POST" and path in ("/api/settings/social", "/settings/social"):
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
    elif method == "GET" and path in ("/api/settings/social", "/settings/social"):
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
    elif method == "DELETE" and path in ("/api/settings/social", "/settings/social"):
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
                # Langue des messages CUPS figée : voir `printer_service.env_cups`.
                from kodo_core.hardware.printer_service import env_cups
                env_lp = env_cups()

                try:
                    out_d = subprocess.check_output(["lpstat", "-d"], stderr=subprocess.DEVNULL, timeout=2, env=env_lp).decode()
                    m_d = re.search(r':\s*(\S+)', out_d)
                    if m_d:
                        default_printer = m_d.group(1)
                except Exception:
                    pass

                try:
                    out_v = subprocess.check_output(["lpstat", "-v"], stderr=subprocess.DEVNULL, timeout=2, env=env_lp).decode()
                    for line in out_v.splitlines():
                        # La phrase qui precede etait cherchee en francais uniquement
                        # (« peripherique pour X : usb://... ») : sur un Mac en anglais,
                        # en neerlandais ou en allemand, AUCUNE imprimante n'etait
                        # detectee et l'ecran Reglages restait desesperement vide.
                        # On ne reconnait plus la phrase, on reconnait la structure :
                        # <nom> : <schema>://<...>, vraie dans toutes les langues.
                        m_v = re.search(r'(\S+)\s*:\s*([A-Za-z][A-Za-z0-9+.\-]*:.+?)\s*$', line)
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

    # 21. Réglages de l'étiqueteuse (bloc code-barres)
    elif method == "GET" and path == "/api/labels/settings":
        return 200, {"success": True, "settings": lire_reglages_etiquette()}

    # 22. Enregistrement des réglages de l'étiqueteuse (bloc code-barres)
    elif method == "POST" and path == "/api/labels/settings":
        # Écriture PARTIELLE, comme /api/settings : une requête qui ne porte que sur la largeur ne
        # doit pas effacer le nom de l'imprimante ni l'orientation déjà réglés. Une chaîne vide
        # explicite remet en revanche le réglage à « non configuré » : c'est une demande, pas un oubli.
        def _fourni(*cles):
            for cle in cles:
                if cle in data and data[cle] is not None:
                    return data[cle]
            return None

        a_ecrire: Dict[str, str] = {}

        nom_imprimante = _fourni("labelPrinterName", "label_printer_name")
        if nom_imprimante is not None:
            a_ecrire["label_printer_name"] = str(nom_imprimante).strip()

        format_id = _fourni("labelFormatId", "label_format_id")
        if format_id is not None:
            a_ecrire["label_format_id"] = str(format_id).strip()

        for cle_ecran, cle_param, libelle, zero_permis in (
            ("labelWidthMm", "label_width_mm", "La largeur de l'étiquette", False),
            ("labelHeightMm", "label_height_mm", "La hauteur de l'étiquette", False),
            ("labelMarginMm", "label_margin_mm", "La marge de l'étiquette", True),
        ):
            brut = _fourni(cle_ecran, cle_param)
            if brut is None:
                continue
            texte = str(brut).strip().replace(",", ".")
            if not texte:
                a_ecrire[cle_param] = ""
                continue
            try:
                valeur = Decimal(texte)
            except Exception:
                return 400, {"error": f"{libelle} doit être un nombre de millimètres (par exemple 57)."}
            if valeur < 0 or (valeur == 0 and not zero_permis):
                return 400, {"error": f"{libelle} doit être supérieure à zéro."}
            if valeur > Decimal("500"):
                return 400, {"error": f"{libelle} dépasse 500 mm : vérifiez l'unité, elle s'exprime en millimètres."}
            a_ecrire[cle_param] = str(valeur.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

        orientation = _fourni("labelOrientation", "label_orientation")
        if orientation is not None:
            texte = str(orientation).strip().lower()
            if not texte:
                a_ecrire["label_orientation"] = ""
            elif texte in ORIENTATIONS_ETIQUETTE:
                a_ecrire["label_orientation"] = ORIENTATIONS_ETIQUETTE[texte]
            else:
                return 400, {"error": "L'orientation de l'étiquette doit être « portrait » ou « paysage »."}

        dpi = _fourni("labelDpi", "label_dpi")
        if dpi is not None:
            texte = str(dpi).strip().lower().replace("dpi", "").strip()
            if not texte:
                a_ecrire["label_dpi"] = ""
            else:
                try:
                    valeur_dpi = int(texte)
                except (TypeError, ValueError):
                    return 400, {"error": "La résolution de l'étiqueteuse doit être un nombre de points par pouce (par exemple 203)."}
                if valeur_dpi <= 0 or valeur_dpi > 2400:
                    return 400, {"error": "La résolution de l'étiqueteuse doit être comprise entre 1 et 2400 points par pouce."}
                a_ecrire["label_dpi"] = str(valeur_dpi)

        afficher_prix = _fourni("labelShowPrice", "label_show_price")
        if afficher_prix is not None:
            a_ecrire["label_show_price"] = "1" if afficher_prix in (True, 1, "1", "true", "True", "oui") else "0"

        if not a_ecrire:
            return 400, {"error": "Aucun réglage d'étiquette à enregistrer."}

        conn = get_connection()
        cursor = conn.cursor()
        try:
            for cle, valeur in a_ecrire.items():
                cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
            conn.commit()
            reglages = lire_reglages_etiquette(cursor)
        finally:
            conn.close()

        # Avertissement NON bloquant : la commerçante a le droit d'enregistrer un support étroit
        # (l'étiquette peut porter autre chose qu'un code-barres). Le refus ferme, lui, intervient
        # à l'impression, quand le moteur sait dans quel sens le code sera réellement tracé.
        avertissements: List[str] = []
        try:
            largeur_reglee = str(reglages.get("label_width_mm") or "").strip()
            if largeur_reglee:
                from kodo_core.hardware.pdf import largeur_mini_ean13_mm
                dpi_regle = str(reglages.get("label_dpi") or "").strip()
                mini = (largeur_mini_ean13_mm(int(dpi_regle)) if dpi_regle
                        else largeur_mini_ean13_mm())
                if float(Decimal(largeur_reglee)) < mini:
                    avertissements.append(
                        f"Une étiquette de {largeur_reglee} mm de large est trop étroite pour un "
                        f"code-barres EAN-13 lisible : il en faut au moins {mini:.1f} mm. "
                        f"Choisissez un support plus large, ou l'orientation paysage."
                    )
        except Exception as e:
            print(f"[ETIQUETTE LARGEUR WARNING] {e}")

        return 200, {
            "success": True,
            "message": "Réglages de l'étiqueteuse enregistrés.",
            "settings": reglages,
            "warnings": avertissements
        }

    # 23. Inventaire des imprimantes installées, indépendant de la langue du système (bloc code-barres)
    elif method == "GET" and path == "/api/labels/printers":
        try:
            imprimantes, defaut = _lister_imprimantes_cups()
        except Exception as e:
            print(f"[ETIQUETTE MATERIEL WARNING] {e}")
            return 200, {
                "success": False,
                "error": "La liste des imprimantes installées n'a pas pu être lue sur cet ordinateur.",
                "printers": []
            }

        reglages = lire_reglages_etiquette()
        choisie = reglages.get("label_printer_name") or ""

        # Formats de la file interrogée (celle qu'on est en train de choisir à l'écran, ou celle
        # déjà enregistrée). Chaque format est annoté « un EAN-13 y tient-il, en portrait et en
        # paysage » : c'est ce qui permet à la commerçante d'éviter un support trop étroit AVANT
        # d'étiqueter sa marchandise.
        interrogee = (query.get("printer") or query.get("printerName") or [choisie])[0] or ""
        formats_etiquette: List[Dict[str, Any]] = []
        if interrogee:
            try:
                from kodo_core.hardware.pdf import formats_etiquette_disponibles
                formats_etiquette = formats_etiquette_disponibles(interrogee)
            except Exception as e:
                print(f"[ETIQUETTE FORMATS WARNING] {interrogee} : {e}")

        return 200, {
            "success": True,
            "defaultPrinter": defaut,
            "selectedLabelPrinter": choisie,
            "formatsPrinter": interrogee,
            "labelFormats": formats_etiquette,
            # None = aucune étiqueteuse choisie ; False = celle qui est enregistrée n'est plus
            # installée (imprimante débranchée ou renommée), ce que l'écran doit signaler.
            "selectedIsInstalled": (any(p["name"] == choisie for p in imprimantes) if choisie else None),
            "printers": imprimantes,
            "labelPrinters": [p for p in imprimantes if p["kind"] == "etiqueteuse"]
        }

    # 24. Impression d'une étiquette de test (bloc code-barres)
    elif method == "POST" and (path == "/api/labels/print-test" or path == "/api/labels/test"):
        reglages = lire_reglages_etiquette()
        if not reglages["est_configuree"]:
            return 409, {
                "success": False,
                "error": reglages["message"],
                "code": "LABEL_PRINTER_NOT_CONFIGURED",
                "settings": reglages
            }

        # Code de démonstration : EAN-13 à clé de contrôle valide, dans la plage interne « 200 »,
        # volontairement absent du catalogue. Un scan de cette étiquette ne doit ramener AUCUN
        # article : c'est le test du matériel, pas celui du catalogue.
        ligne_test = {
            "product_id": None,
            "name": "ÉTIQUETTE DE TEST",
            "barcode": "2000000000008",
            "size": "",
            "price": Decimal("0.00"),
            "price_sale": None,
            "quantity": 1,
            "is_test": True
        }

        from kodo_core.api.routes.products_routes import _generer_pdf_etiquettes
        from kodo_core.hardware.pdf import BarcodeTropEtroitError

        try:
            rendu, media = _generer_pdf_etiquettes([ligne_test], reglages, None)
        except BarcodeTropEtroitError as be:
            # Le test remplit ici tout son rôle : il dit à la commerçante, AVANT qu'elle n'étiquette
            # sa marchandise, que le format réglé ne peut pas porter un code-barres lisible.
            return 400, {"success": False, "error": str(be), "code": "LABEL_TOO_NARROW"}
        except Exception as e:
            print(f"[ETIQUETTE TEST ERREUR] {e}")
            return 500, {
                "success": False,
                "error": "L'étiquette de test n'a pas pu être générée.",
                "detail": str(e)
            }

        try:
            from kodo_core.hardware.print_worker import get_print_worker
            worker = get_print_worker()
            job = worker.enqueue_label_print(
                rendu.get("path"),
                printer_name=(data.get("printerName") or data.get("printer_name")
                              or reglages["label_printer_name"]),
                media=media,
                copies=1
            )
            etat = worker.get_label_circuit_status()
            return 200, {
                "success": True,
                "message": "Étiquette de test envoyée à l'étiqueteuse.",
                "print_job_id": job.job_id,
                "status": job.status,
                "warnings": rendu.get("avertissements") or [],
                "printer_available": etat.get("is_available", True),
                "printer_state": etat.get("state")
            }
        except Exception as pe:
            print(f"[ETIQUETTE TEST WARNING] {pe}")
            return 500, {
                "success": False,
                "error": "L'étiquette de test n'a pas pu être envoyée à l'étiqueteuse.",
                "detail": str(pe)
            }

    return None

